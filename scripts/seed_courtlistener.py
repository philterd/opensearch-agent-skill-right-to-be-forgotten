#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["opensearch-py>=2.4"]
# ///
"""Seed US court opinions from the CourtListener bulk export.

Court opinions have the two channels the method needs: the body describes a
person's conduct at length while calling them "the defendant", and the caption
names them in a separate structured field. Enron email had the naming channel
without the descriptions.

Not redistributed with this skill. Free Law Project publishes the bulk data
openly; this fetches a byte range on demand and caches it.

Everything is cached and every stage takes --limit, because the uncached loop
was ten minutes per iteration and three of those runs died in the first two
hundred rows of parsing.
"""

import bz2
import csv
import io
import json
import os
import re
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

CASELAW_INDEX = "case-law"
BULK = "https://storage.courtlistener.com/bulk-data/"
OPINIONS = "opinions-2025-12-31.csv.bz2"
CLUSTERS = "opinion-clusters-2025-12-31.csv.bz2"
UA = "gdpr-forget-me-skill/1.0 (evaluation corpus fetch)"

DEFAULT_CACHE = os.path.join("gdpr-eval", "courtlistener")
DEFAULT_LIMIT = 2000
DEFAULT_SLICE_MB = 40
DEFAULT_MAX_CHARS = 8000

# Opinion bodies run to megabytes.
csv.field_size_limit(50_000_000)

# The role language that makes this corpus worth using.
ROLE = re.compile(r"\bthe (defendant|plaintiff|appellant|appellee|petitioner|"
                  r"respondent|victim|claimant|movant)\b", re.I)
_TAG = re.compile(r"<[^>]+>")


def _cache_path(cache_dir, name):
    os.makedirs(cache_dir, exist_ok=True)
    return os.path.join(cache_dir, name)


def fetch_slice(cache_dir, name=OPINIONS, megabytes=DEFAULT_SLICE_MB):
    """Download and cache the leading blocks of a bulk file, decompressed.

    bz2 decompresses incrementally, so a range request yields whole blocks
    without pulling the 51Gb opinions export.
    """
    target = _cache_path(cache_dir, f"{name}.{megabytes}mb.txt")
    if os.path.exists(target) and os.path.getsize(target) > 0:
        return target
    nbytes = megabytes * 1024 * 1024
    req = urllib.request.Request(BULK + name, headers={
        "User-Agent": UA, "Range": f"bytes=0-{nbytes - 1}"})
    with urllib.request.urlopen(req, timeout=300) as resp:  # noqa: S310 - fixed host
        raw = resp.read()
    try:
        text = bz2.BZ2Decompressor().decompress(raw).decode("utf-8", errors="replace")
    except (OSError, EOFError):
        text = ""
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(text)
    return target


def fetch_clusters(cache_dir):
    """The clusters export whole, since captions are needed by id."""
    target = _cache_path(cache_dir, CLUSTERS)
    if os.path.exists(target) and os.path.getsize(target) > 1_000_000:
        return target
    req = urllib.request.Request(BULK + CLUSTERS, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=1800) as resp, open(target, "wb") as fh:  # noqa: S310
        while chunk := resp.read(1 << 20):
            fh.write(chunk)
    return target


def _body_of(row, columns):
    for name in ("plain_text", "html_with_citations", "html"):
        index = columns.get(name)
        if index is not None and index < len(row) and row[index].strip():
            text = row[index]
            return " ".join(_TAG.sub(" ", text).split()) if "<" in text else \
                " ".join(text.split())
    return ""


def iter_opinions(path, limit=DEFAULT_LIMIT, max_chars=DEFAULT_MAX_CHARS,
                  min_chars=400):
    """Yield (cluster_id, opinion_id, text) for role-bearing opinions."""
    with open(path, encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh, escapechar="\\")
        header = next(reader)
        columns = {name: i for i, name in enumerate(header)}
        cid_col = columns.get("cluster_id")
        id_col = columns.get("id")
        if cid_col is None:
            raise RuntimeError("opinions export has no cluster_id column")
        seen = 0
        for row in reader:
            if seen >= limit:
                return
            if len(row) <= cid_col:
                continue
            body = _body_of(row, columns)
            if len(body) < min_chars or not ROLE.search(body):
                continue
            seen += 1
            yield row[cid_col], (row[id_col] if id_col is not None else ""), body[:max_chars]


def caption_cache(cache_dir, slice_mb=DEFAULT_SLICE_MB):
    """Captions for every cluster the cached slice refers to, built once.

    The clusters export is 2.3Gb and a pass over it costs minutes, so this
    resolves the whole slice rather than one --limit worth. Later runs at any
    limit read the cache. Scoped to the slice, not the ten million captions in
    the export, which was an out-of-memory kill.
    """
    target = _cache_path(cache_dir, f"captions.{slice_mb}mb.json")
    if os.path.exists(target):
        with open(target, encoding="utf-8") as fh:
            return {k: tuple(v) for k, v in json.load(fh).items()}

    opinions_path = fetch_slice(cache_dir, OPINIONS, slice_mb)
    wanted = {cid for cid, _, _ in iter_opinions(opinions_path, limit=10 ** 9)}
    print(f"building caption cache for {len(wanted)} clusters (one pass over the "
          f"clusters export, a few minutes)", file=sys.stderr)
    captions = load_captions(fetch_clusters(cache_dir), wanted)
    with open(target, "w", encoding="utf-8") as fh:
        json.dump(captions, fh)
    return captions


def load_captions(path, wanted):
    """Captions for just the cluster ids in ``wanted``."""
    captions = {}
    with bz2.open(path, "rt", encoding="utf-8", errors="replace", newline="") as fh:
        reader = csv.reader(fh, escapechar="\\")
        header = next(reader)
        columns = {name: i for i, name in enumerate(header)}
        id_col = columns["id"]
        name_cols = [columns[c] for c in
                     ("case_name_full", "case_name", "case_name_short") if c in columns]
        date_col = columns.get("date_filed")
        for row in reader:
            if len(row) <= id_col or row[id_col] not in wanted:
                continue
            caption = next((row[c] for c in name_cols
                            if c < len(row) and row[c].strip()), "")
            date = row[date_col] if date_col is not None and date_col < len(row) else ""
            captions[row[id_col]] = (caption, date)
            if len(captions) == len(wanted):
                break
    return captions


def build_documents(cache_dir, limit=DEFAULT_LIMIT, slice_mb=DEFAULT_SLICE_MB,
                    max_chars=DEFAULT_MAX_CHARS):
    """Return (documents, stats). Text and captions stay in separate fields."""
    from lib import caselaw

    opinions_path = fetch_slice(cache_dir, OPINIONS, slice_mb)
    rows = list(iter_opinions(opinions_path, limit=limit, max_chars=max_chars))
    captions = caption_cache(cache_dir, slice_mb)

    documents, no_caption, no_person = [], 0, 0
    for cluster_id, opinion_id, text in rows:
        caption, date = captions.get(cluster_id, ("", ""))
        if not caption:
            no_caption += 1
            continue
        surnames = caselaw.party_surnames(caption)
        if not surnames:
            no_person += 1
            continue
        documents.append((f"cl-{cluster_id}-{opinion_id}", {
            "@timestamp": f"{date}T00:00:00Z" if re.match(r"^\d{4}-\d\d-\d\d$", date) else None,
            "message": text,
            # Naming channel. Never merged into `message`, which is what
            # discovery searches.
            "case_name": caption,
            "party_surnames": surnames,
            "party_given_names": caselaw.given_names(caption),
            "cluster_id": cluster_id,
        }))
    return documents, {
        "opinions_with_role_language": len(rows),
        "indexed": len(documents),
        "dropped_no_caption": no_caption,
        "dropped_no_person_party": no_person,
    }


def load(client, cache_dir=DEFAULT_CACHE, limit=DEFAULT_LIMIT,
         slice_mb=DEFAULT_SLICE_MB, max_chars=DEFAULT_MAX_CHARS, setup_neural=True,
         text_field="message", embedding_field="message_embedding", index=CASELAW_INDEX):
    """(Re)create the case-law index and load role-bearing opinions."""
    from lib.model import setup_neural_search, create_knn_index

    documents, stats = build_documents(cache_dir, limit, slice_mb, max_chars)
    if client.indices.exists(index=index):
        client.indices.delete(index=index)

    extra = {
        "@timestamp": {"type": "date"},
        "case_name": {"type": "keyword", "index": False, "doc_values": False},
        "party_surnames": {"type": "keyword"},
        "party_given_names": {"type": "keyword"},
        "cluster_id": {"type": "keyword"},
    }
    neural = None
    if setup_neural:
        neural = setup_neural_search(client, text_field, embedding_field)
        create_knn_index(client, index, text_field, embedding_field,
                         dim=neural["embedding_dim"], extra_properties=extra)
    else:
        client.indices.create(index=index, body={
            "mappings": {"properties": dict(extra, **{text_field: {"type": "text"}})}})

    batch = []

    def flush():
        if not batch:
            return
        lines = []
        for doc_id, source in batch:
            lines.append(json.dumps({"index": {"_index": index, "_id": doc_id}}))
            lines.append(json.dumps(source))
        resp = client.bulk(body="\n".join(lines) + "\n", refresh=False)
        if resp.get("errors"):
            first = next((i for i in resp["items"] if i.get("index", {}).get("error")), None)
            raise RuntimeError(f"Bulk load had errors: {json.dumps(first)}")
        batch.clear()

    for doc_id, source in documents:
        batch.append((doc_id, source))
        if len(batch) >= 100:
            flush()
    flush()
    client.indices.refresh(index=index)

    return {
        "index": index, **stats,
        "neural_search": neural is not None,
        "cache_dir": cache_dir,
        "provenance": (
            "CourtListener bulk data, Free Law Project. Fetched on demand, not "
            "redistributed with this skill. Opinions are public record."
        ),
    }


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Seed court opinions for gdpr-forget-me")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                    help=f"Opinions to index (default {DEFAULT_LIMIT})")
    ap.add_argument("--slice-mb", type=int, default=DEFAULT_SLICE_MB,
                    help="Compressed megabytes of the opinions export to cache")
    ap.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    ap.add_argument("--cache-dir", default=DEFAULT_CACHE)
    ap.add_argument("--no-neural", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help="Build documents and print a sample without touching OpenSearch")
    a = ap.parse_args()

    if a.dry_run:
        docs, stats = build_documents(a.cache_dir, a.limit, a.slice_mb, a.max_chars)
        print(json.dumps({**stats, "sample": [
            {"doc_id": i, "case_name": s["case_name"],
             "party_surnames": s["party_surnames"],
             "message": s["message"][:160]} for i, s in docs[:3]]}, indent=2))
        raise SystemExit(0)

    from lib.client import create_client
    print(json.dumps(load(create_client(bootstrap=True), cache_dir=a.cache_dir,
                          limit=a.limit, slice_mb=a.slice_mb, max_chars=a.max_chars,
                          setup_neural=not a.no_neural), indent=2))
