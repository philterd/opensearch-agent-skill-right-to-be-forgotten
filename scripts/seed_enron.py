#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["opensearch-py>=2.4"]
# ///
"""Seed a subset of the Enron email corpus for the gdpr-forget-me demo.

Real public email, so nothing in it was written to be findable. Good for the
direct-identifier pass; seed_demo.py is the clearer indirect demonstration
(role-reference language is ~1% of messages here).

The data is NOT redistributed with this skill. CMU grants no license and asks
users to be sensitive to the privacy of the people involved. This streams from
CMU on demand and stops at --limit, pulling a fraction of the 1.7Gb archive.
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

# maildir/<custodian>/<folder>/<n>; anything else in the tar is skipped.
_MEMBER_RE = re.compile(r"^maildir/([^/]+)/(.+)/(\d+)[._]?$")

# Truncation points. Quoted text is kept: it often carries the identification.
_QUOTE_MARKERS = ("-----Original Message-----", "---------------------- Forwarded")


def _clean_body(raw, max_chars):
    """Collapse whitespace and truncate.

    Bodies are hard-wrapped at ~72 chars and redaction matches snippets by
    exact substring, so a phrase spanning a newline would not match.
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


def _header(msg, name):
    """Header value as collapsed text, or "" when absent.

    str() is required: under compat32 a header with encoded words returns an
    email.header.Header, which has no .split(), aborting the whole run.
    """
    value = msg.get(name)
    return "" if value is None else " ".join(str(value).split())


def _addresses(msg, header):
    value = _header(msg, header)
    if not value:
        return []
    return [a.strip() for a in value.split(",") if a.strip()]


def parse_member(raw, custodian, folder, max_chars):
    """Parse one maildir message into an indexable document, or None.

    Takes bytes: message_from_binary_file needs seekable(), which tarfile's
    streaming _Stream lacks.
    """
    try:
        msg = email.message_from_bytes(raw)
    except (ValueError, TypeError):
        return None

    subject = _header(msg, "Subject")
    body = _clean_body(_body_text(msg), max_chars)
    if not body and not subject:
        return None

    # Single text field the pipeline embeds; subject leads so it carries weight.
    combined = f"{subject}. {body}" if subject else body

    return {
        "@timestamp": _timestamp(msg),
        "message": combined,
        "subject": subject,
        "from": _header(msg, "From"),
        "to": _addresses(msg, "To"),
        "cc": _addresses(msg, "Cc"),
        # From/To/Cc are bare addresses; readable names and Exchange logins
        # live only here, as `Fagan, Fran </O=ENRON/...CN=FFAGAN>`. Kept
        # verbatim and paired positionally with the lists above.
        "x_from": _header(msg, "X-From"),
        "x_to": _header(msg, "X-To"),
        "x_cc": _header(msg, "X-cc"),
        "custodian": custodian,
        "folder": folder,
        "message_id": _header(msg, "Message-ID"),
    }


def _open_stream(source):
    """Return (tarfile, closer) for a local path or the CMU URL.

    Stream mode ("r|gz") so iteration can stop without downloading 1.7Gb.
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

    Custodian groups are not alphabetical, so a --custodian filter may stream
    the whole archive before matching. Use --source for repeated filtering.
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
        # Read from _source by the roster scan, never queried. Left unindexed so
        # a long recipient list cannot exceed the keyword term limit.
        "x_from": {"type": "keyword", "index": False, "doc_values": False},
        "x_to": {"type": "keyword", "index": False, "doc_values": False},
        "x_cc": {"type": "keyword", "index": False, "doc_values": False},
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

    # Smaller chunks than seed_demo: ingest embeds each doc, so large bulk
    # requests risk a gateway timeout.
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
