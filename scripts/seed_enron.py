#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["opensearch-py>=2.4"]
# ///
"""Seed a subset of the Enron email corpus for the gdpr-forget-me demo.

Why this corpus: it is the only substantial collection of real, public email,
so it exercises the workflow on documents nobody engineered to be findable. The
direct-identifier pass in particular gets a real workout — genuine enron.com
addresses, phone numbers, and PII co-located in the same message.

Calibrate expectations on the indirect half. Role-reference language ("whoever
has taken her place", "the person that has been calling") appears in only about
1% of messages by regex proxy, and most instances either describe a generic role
rather than an identifiable individual, or name the person elsewhere in the same
message anyway. seed_demo.py remains the clearer demonstration of indirect
contextual identification; this corpus is the realism check, not a richer supply
of that pattern.

It is also the honest test case for erasure. CMU's distribution page notes that
some messages were already deleted "as part of a redaction effort due to
requests from affected employees" — this corpus has been receiving erasure
requests for two decades, and remains fully indexed and searchable regardless.

The data is NOT redistributed with this skill. CMU grants no license; the page
asks users to "be sensitive to the privacy of the people involved (and remember
that many of these people were certainly not involved in any of the actions
which precipitated the investigation)". This script fetches from CMU on demand,
streaming the archive and stopping as soon as --limit messages are parsed, so a
demo run pulls a fraction of the 1.7Gb archive.
"""

import datetime
import email
import email.utils
import json
import os
import re
import sys
import tarfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ENRON_INDEX = "mail-enron"
ENRON_URL = "https://www.cs.cmu.edu/~enron/enron_mail_20150507.tar.gz"

DEFAULT_LIMIT = 2000
DEFAULT_MAX_CHARS = 4000

# maildir/<custodian>/<folder>/<n>  — anything else in the tar is skipped.
_MEMBER_RE = re.compile(r"^maildir/([^/]+)/(.+)/(\d+)[._]?$")

# Quoted-reply markers. We keep quoted text (it often carries the contextual
# identification) but use these to find a sane truncation point.
_QUOTE_MARKERS = ("-----Original Message-----", "---------------------- Forwarded")


def _clean_body(raw, max_chars):
    """Collapse whitespace and truncate.

    Collapsing every whitespace run to a single space is deliberate, not
    cosmetic. Enron bodies are hard-wrapped at ~72 characters, so a phrase that
    identifies someone routinely straddles a newline. Redaction replaces
    `identifying_snippets` by exact substring match (see lib/actions.py), so a
    snippet the agent copies out of a wrapped body would fail to match the
    stored text. Normalising at ingest makes copied snippets matchable.
    """
    if not raw:
        return ""
    text = " ".join(raw.split())
    if len(text) <= max_chars:
        return text
    # Prefer cutting at a quoted-reply boundary over mid-sentence.
    window = text[:max_chars]
    for marker in _QUOTE_MARKERS:
        idx = window.rfind(marker)
        if idx > max_chars // 3:
            return window[:idx].strip()
    return window.rsplit(" ", 1)[0] + " ..."


def _body_text(msg):
    """Extract the plain-text body. The corpus ships no attachments."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode("utf-8", errors="replace")
        return ""
    payload = msg.get_payload(decode=True)
    if payload is None:
        return msg.get_payload() or ""
    return payload.decode("utf-8", errors="replace")


def _timestamp(msg):
    raw = msg.get("Date")
    if not raw:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    try:
        return dt.astimezone(datetime.timezone.utc).isoformat()
    except (ValueError, OSError):
        return None


def _addresses(msg, header):
    value = msg.get(header)
    if not value:
        return []
    return [a.strip() for a in " ".join(value.split()).split(",") if a.strip()]


def parse_member(raw, custodian, folder, max_chars):
    """Parse one maildir message into an indexable document, or None.

    Takes bytes rather than a file object: email.message_from_binary_file wraps
    the stream in a TextIOWrapper, which calls seekable() — and tarfile's
    streaming _Stream has no such method, so it raises on every message.
    """
    try:
        msg = email.message_from_bytes(raw)
    except (ValueError, TypeError):
        return None

    subject = " ".join((msg.get("Subject") or "").split())
    body = _clean_body(_body_text(msg), max_chars)
    if not body and not subject:
        return None

    # `message` is the single text field the rest of the pipeline reads and
    # embeds. Subject leads so it carries weight in both BM25 and the vector.
    combined = f"{subject}. {body}" if subject else body

    return {
        "@timestamp": _timestamp(msg),
        "message": combined,
        "subject": subject,
        "from": (msg.get("From") or "").strip(),
        "to": _addresses(msg, "To"),
        "cc": _addresses(msg, "Cc"),
        "custodian": custodian,
        "folder": folder,
        "message_id": (msg.get("Message-ID") or "").strip(),
    }


def _open_stream(source):
    """Return (tarfile, closer) for a local path or the CMU URL.

    Uses stream mode ("r|gz") throughout so we never need to download or
    extract the whole 1.7Gb archive — iteration stops as soon as the caller has
    enough messages.
    """
    if source and os.path.exists(source):
        tar = tarfile.open(source, mode="r|gz")
        return tar, tar.close

    import urllib.request
    req = urllib.request.Request(
        source or ENRON_URL,
        headers={"User-Agent": "gdpr-forget-me-skill/1.0 (OpenSearch agent skill demo)"},
    )
    resp = urllib.request.urlopen(req, timeout=120)  # noqa: S310 - fixed https host
    tar = tarfile.open(fileobj=resp, mode="r|gz")

    def _close():
        try:
            tar.close()
        finally:
            resp.close()

    return tar, _close


def iter_documents(source=None, limit=DEFAULT_LIMIT, custodians=None,
                   folders=None, max_chars=DEFAULT_MAX_CHARS):
    """Yield (doc_id, source_dict) for up to ``limit`` messages.

    Members are grouped by custodian but the groups are not in alphabetical
    order (the archive opens on blair-l), so there is no way to predict how far
    a --custodian filter must stream before it finds a match; worst case it
    reads the whole 1.7Gb. Point --source at a local copy of the tarball if you
    intend to filter by custodian repeatedly.
    """
    wanted_custodians = set(custodians or [])
    wanted_folders = set(folders or [])
    tar, close = _open_stream(source)
    seen = 0
    try:
        for member in tar:
            if seen >= limit:
                break
            if not member.isfile():
                continue
            match = _MEMBER_RE.match(member.name)
            if not match:
                continue
            custodian, folder, num = match.groups()
            if wanted_custodians and custodian not in wanted_custodians:
                continue
            if wanted_folders and folder.split("/")[0] not in wanted_folders:
                continue
            fileobj = tar.extractfile(member)
            if fileobj is None:
                continue
            doc = parse_member(fileobj.read(), custodian, folder, max_chars)
            if doc is None:
                continue
            seen += 1
            yield f"{custodian}/{folder}/{num}", doc
    finally:
        close()


def load(client, setup_neural=True, text_field="message",
         embedding_field="message_embedding", source=None, limit=DEFAULT_LIMIT,
         custodians=None, folders=None, max_chars=DEFAULT_MAX_CHARS,
         progress=None):
    """(Re)create the Enron index and bulk-load a subset of the corpus."""
    from lib.model import setup_neural_search, create_knn_index

    if client.indices.exists(index=ENRON_INDEX):
        client.indices.delete(index=ENRON_INDEX)

    extra_props = {
        "@timestamp": {"type": "date"},
        "subject": {"type": "text"},
        "from": {"type": "keyword"},
        "to": {"type": "keyword"},
        "cc": {"type": "keyword"},
        "custodian": {"type": "keyword"},
        "folder": {"type": "keyword"},
        "message_id": {"type": "keyword"},
    }

    neural = None
    if setup_neural:
        neural = setup_neural_search(client, text_field, embedding_field)
        create_knn_index(client, ENRON_INDEX, text_field, embedding_field,
                         dim=neural["embedding_dim"], extra_properties=extra_props)
    else:
        client.indices.create(index=ENRON_INDEX, body={
            "mappings": {"properties": dict(extra_props, **{text_field: {"type": "text"}})}
        })

    loaded, custodians_seen, batch = 0, set(), []

    def flush():
        nonlocal batch
        if not batch:
            return
        lines = []
        for doc_id, src in batch:
            lines.append(json.dumps({"index": {"_index": ENRON_INDEX, "_id": doc_id}}))
            lines.append(json.dumps(src))
        resp = client.bulk(body="\n".join(lines) + "\n", refresh=False)
        if resp.get("errors"):
            first = next((i for i in resp["items"] if i.get("index", {}).get("error")), None)
            raise RuntimeError(f"Bulk load had errors: {json.dumps(first)}")
        batch = []

    # Smaller chunks than seed_demo: every document is embedded in the ingest
    # pipeline, so oversized bulk requests risk a gateway timeout.
    for doc_id, doc in iter_documents(source, limit, custodians, folders, max_chars):
        batch.append((doc_id, doc))
        custodians_seen.add(doc["custodian"])
        loaded += 1
        if len(batch) >= 100:
            flush()
            if progress:
                progress(loaded)
    flush()
    client.indices.refresh(index=ENRON_INDEX)

    return {
        "index": ENRON_INDEX,
        "documents_loaded": loaded,
        "custodians": sorted(custodians_seen),
        "custodian_count": len(custodians_seen),
        "neural_search": neural is not None,
        "neural": neural,
        "source": source or ENRON_URL,
        "provenance": (
            "Enron Email Dataset (May 7 2015 version), CMU. Fetched on demand, not "
            "redistributed with this skill. CMU grants no license and asks users to be "
            "sensitive to the privacy of the people involved, many of whom were not "
            "implicated in the investigation."
        ),
        "next_step": (
            "Pick a data subject from the loaded custodians, then run `discover` with a "
            "contextual profile and `discover-direct` with their enron.com address. "
            "Unlike seed-demo there is no ground-truth label set: this is real "
            "correspondence, so verify flagged documents by reading them."
        ),
    }


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Seed Enron email data for gdpr-forget-me")
    ap.add_argument("--source", default=None,
                    help="Local path to enron_mail_20150507.tar.gz (default: stream from CMU)")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    ap.add_argument("--custodian", action="append", default=None,
                    help="Only index this custodian's maildir (repeatable)")
    ap.add_argument("--folder", action="append", default=None,
                    help="Only index this top-level folder, e.g. sent (repeatable)")
    ap.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    ap.add_argument("--no-neural", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help="Parse and print sample documents without touching OpenSearch")
    a = ap.parse_args()

    if a.dry_run:
        docs = list(iter_documents(a.source, a.limit, a.custodian, a.folder, a.max_chars))
        print(json.dumps({
            "parsed": len(docs),
            "custodians": sorted({d["custodian"] for _, d in docs}),
            "sample": [{"doc_id": i, **d} for i, d in docs[:3]],
        }, indent=2))
        raise SystemExit(0)

    from lib.client import create_client
    result = load(create_client(bootstrap=True), setup_neural=not a.no_neural,
                  source=a.source, limit=a.limit, custodians=a.custodian,
                  folders=a.folder, max_chars=a.max_chars)
    print(json.dumps(result, indent=2))
