"""Build the masked corpus and its label set.

Reads `mail-enron`, masks one subject's alias variants out of the searched text
field, and writes the result to a separate index under opaque ids. The original
index is never modified: it is the source of the labels, and stage three needs
it intact to check what was there before masking.

Only the masked text and its timestamp are carried over. Every header field
stays behind, which is what keeps the naming channel out of the search path and
stops a mailbox named after the subject from being searchable.
"""

import json
import os

from lib.leakage import audit
from lib.masking import (LABELS_PATH, MASKED_INDEX, MASK_TOKEN, build_pattern,
                         find_variants, mask_text, masked_doc_id)

SOURCE_INDEX = "mail-enron"
_BULK_CHUNK = 100


def iter_source_documents(client, index=SOURCE_INDEX, text_field="message",
                          timestamp_field="@timestamp", page_size=1000, scroll="2m"):
    """Yield (doc_id, text, timestamp) for every document in the source index."""
    resp = client.search(
        index=index,
        scroll=scroll,
        body={
            "size": page_size,
            "query": {"match_all": {}},
            "_source": [text_field, timestamp_field],
        },
    )
    scroll_id = resp.get("_scroll_id")
    try:
        while True:
            hits = resp.get("hits", {}).get("hits", [])
            if not hits:
                return
            for hit in hits:
                source = hit.get("_source", {})
                yield hit.get("_id"), source.get(text_field) or "", source.get(timestamp_field)
            if scroll_id is None:
                return
            resp = client.scroll(body={"scroll_id": scroll_id, "scroll": scroll})
            scroll_id = resp.get("_scroll_id")
    finally:
        if scroll_id:
            try:
                client.clear_scroll(body={"scroll_id": [scroll_id]})
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass


def _create_masked_index(client, index, text_field, timestamp_field, setup_neural,
                         embedding_field):
    from lib.model import setup_neural_search, create_knn_index

    if client.indices.exists(index=index):
        client.indices.delete(index=index)

    extra_props = {timestamp_field: {"type": "date"}}
    if setup_neural:
        neural = setup_neural_search(client, text_field, embedding_field)
        create_knn_index(client, index, text_field, embedding_field,
                         dim=neural["embedding_dim"], extra_properties=extra_props)
        return neural

    client.indices.create(index=index, body={
        "mappings": {"properties": dict(extra_props, **{text_field: {"type": "text"}})}
    })
    return None


def build_masked_corpus(client, aliases, source_index=SOURCE_INDEX,
                        masked_index=MASKED_INDEX, text_field="message",
                        timestamp_field="@timestamp", setup_neural=True,
                        embedding_field="message_embedding", mask_token=MASK_TOKEN,
                        progress=None):
    """Mask the corpus for one subject.

    Returns (positives, stats). ``positives`` are the masked ids of documents
    whose pre-mask text contained at least one variant, paired with their
    original ids so stage three can trace a result back.
    """
    pattern = build_pattern(aliases["variants"])
    if pattern is None:
        raise ValueError("Alias set is empty; nothing to mask.")

    neural = _create_masked_index(client, masked_index, text_field, timestamp_field,
                                  setup_neural, embedding_field)

    positives, batch = [], []
    scanned = masked_docs = total_hits = 0

    def flush():
        nonlocal batch
        if not batch:
            return
        lines = []
        for doc_id, source in batch:
            lines.append(json.dumps({"index": {"_index": masked_index, "_id": doc_id}}))
            lines.append(json.dumps(source))
        resp = client.bulk(body="\n".join(lines) + "\n", refresh=False)
        if resp.get("errors"):
            first = next((i for i in resp["items"] if i.get("index", {}).get("error")), None)
            raise RuntimeError(f"Bulk load had errors: {json.dumps(first)}")
        batch = []

    for original_id, text, timestamp in iter_source_documents(
            client, source_index, text_field, timestamp_field):
        scanned += 1
        masked, hits = mask_text(text, pattern, mask_token)
        new_id = masked_doc_id(original_id)
        if hits:
            masked_docs += 1
            total_hits += hits
            positives.append({"doc_id": new_id, "original_id": original_id,
                              "variant_hits": hits})
        batch.append((new_id, {text_field: masked, timestamp_field: timestamp}))
        if len(batch) >= _BULK_CHUNK:
            flush()
            if progress:
                progress(scanned)
    flush()
    client.indices.refresh(index=masked_index)

    return positives, {
        "source_index": source_index,
        "masked_index": masked_index,
        "documents_scanned": scanned,
        "documents_masked": masked_docs,
        "variant_occurrences_removed": total_hits,
        "neural_search": neural is not None,
        "fields_carried": [text_field, timestamp_field],
    }


def iter_masked_documents(client, index=MASKED_INDEX, page_size=1000, scroll="2m"):
    """Yield (doc_id, full _source) from the masked index, for the audit.

    Unlike the source scan this asks for the whole `_source`, because the audit
    has to notice a header field that should not be there.
    """
    resp = client.search(index=index, scroll=scroll,
                         body={"size": page_size, "query": {"match_all": {}}})
    scroll_id = resp.get("_scroll_id")
    try:
        while True:
            hits = resp.get("hits", {}).get("hits", [])
            if not hits:
                return
            for hit in hits:
                yield hit.get("_id"), hit.get("_source", {})
            if scroll_id is None:
                return
            resp = client.scroll(body={"scroll_id": scroll_id, "scroll": scroll})
            scroll_id = resp.get("_scroll_id")
    finally:
        if scroll_id:
            try:
                client.clear_scroll(body={"scroll_id": [scroll_id]})
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass


def audit_masked_index(client, aliases, index=MASKED_INDEX,
                       phone_policy="identification", text_field="message"):
    pattern = build_pattern(aliases["variants"])
    return audit(iter_masked_documents(client, index), pattern,
                 phone_policy=phone_policy, text_field=text_field)


def verify_positives(client, aliases, positives, source_index=SOURCE_INDEX,
                     text_field="message", sample=5):
    """Confirm a sample of positives really did contain a variant pre-mask.

    Cheap guard against a label set built from the wrong index or a stale
    alias list, which would silently produce labels nobody can trust.
    """
    pattern = build_pattern(aliases["variants"])
    checked = confirmed = 0
    for item in positives[:sample]:
        try:
            doc = client.get(index=source_index, id=item["original_id"])
        except Exception:  # noqa: BLE001 - a missing source doc is itself the finding
            continue
        checked += 1
        if find_variants(doc.get("_source", {}).get(text_field) or "", pattern):
            confirmed += 1
    return {"checked": checked, "confirmed": confirmed}


def write_labels(path, aliases, positives, stats, audit_report, phone_policy):
    """Write the answer key to disk.

    Never printed: this output lands in the context of the agent that then
    judges the corpus, and the positives are exactly what its judgment is
    supposed to determine. Same discipline as seed-demo's answer key.
    """
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    payload = {
        "subject": aliases["subject"],
        "masked_index": stats["masked_index"],
        "source_index": stats["source_index"],
        "text_field": stats["fields_carried"][0],
        "phone_policy": phone_policy,
        "aliases": aliases,
        "audit": audit_report,
        "stats": stats,
        "positive_count": len(positives),
        "positives": positives,
        "id_map": {p["doc_id"]: p["original_id"] for p in positives},
        "assumptions": (
            "Positives are documents whose pre-mask text contained at least one alias "
            "variant. Masking manufactures the indirect case: a sentence written without "
            "a name would have been phrased differently from one with the name removed, "
            "so these labels support a proxy measurement, not a natural sample. Results "
            "do not transfer to another corpus."
        ),
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    return path


def load_labels(path=LABELS_PATH):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)
