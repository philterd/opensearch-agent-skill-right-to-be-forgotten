"""Phase 1 — Semantic & Lexical Candidate Retrieval.

Builds and executes an OpenSearch hybrid query that combines BM25 exact matching
on contextual keywords with k-NN neural search on a semantic description of the
target. This is what lets the skill surface *indirect* identifiers — documents
that describe a person without naming them.

If the target index has no embedding field (or no model is deployed), discovery
degrades gracefully to BM25-only so the skill still works everywhere.
"""

from .model import SEARCH_PIPELINE_ID


def index_has_field(client, index_pattern, field):
    """True if any index matching the pattern maps ``field`` (dotted path ok)."""
    try:
        mappings = client.indices.get_mapping(index=index_pattern)
    except Exception:
        return False
    parts = field.split(".")
    for meta in mappings.values():
        props = meta.get("mappings", {}).get("properties", {})
        node, ok = props, True
        for i, part in enumerate(parts):
            if part not in node:
                ok = False
                break
            node = node[part].get("properties", {}) if i < len(parts) - 1 else node[part]
        if ok:
            return True
    return False


def build_hybrid_query(text_field, embedding_field, keywords, profile, model_id, size):
    return {
        "size": size,
        "_source": {"excludes": [embedding_field]},
        "query": {
            "hybrid": {
                "queries": [
                    {"match": {text_field: {"query": keywords}}},
                    {
                        "neural": {
                            embedding_field: {
                                "query_text": profile,
                                "model_id": model_id,
                                "k": size,
                            }
                        }
                    },
                ]
            }
        },
    }


def build_bm25_query(text_field, keywords, profile, size):
    # Fallback: OR of contextual keywords and the free-text profile.
    return {
        "size": size,
        "query": {
            "bool": {
                "should": [
                    {"match": {text_field: {"query": keywords, "boost": 2.0}}},
                    {"match": {text_field: {"query": profile}}},
                ],
                "minimum_should_match": 1,
            }
        },
    }


def _hit_to_candidate(hit, text_field, timestamp_field):
    src = hit.get("_source", {})
    text = src.get(text_field)
    if text is None and "." in text_field:  # dotted path
        node = src
        for part in text_field.split("."):
            node = node.get(part, {}) if isinstance(node, dict) else {}
        text = node if isinstance(node, str) else None
    return {
        "doc_id": hit.get("_id"),
        "index": hit.get("_index"),
        "score": hit.get("_score"),
        "timestamp": src.get(timestamp_field),
        "text": text,
    }


def discover(
    client,
    index_pattern,
    profile,
    keywords,
    text_field="message",
    embedding_field="message_embedding",
    timestamp_field="@timestamp",
    model_id=None,
    size=50,
    search_pipeline=SEARCH_PIPELINE_ID,
):
    """Return (candidates, meta). ``meta.mode`` is 'hybrid' or 'bm25_fallback'."""
    keywords = keywords or profile
    use_hybrid = bool(model_id) and index_has_field(client, index_pattern, embedding_field)

    if use_hybrid:
        body = build_hybrid_query(text_field, embedding_field, keywords, profile, model_id, size)
        params = {"search_pipeline": search_pipeline}
        mode = "hybrid"
    else:
        body = build_bm25_query(text_field, keywords, profile, size)
        params = {}
        mode = "bm25_fallback"

    resp = client.search(index=index_pattern, body=body, params=params)
    hits = resp.get("hits", {}).get("hits", [])
    candidates = [_hit_to_candidate(h, text_field, timestamp_field) for h in hits]

    meta = {
        "mode": mode,
        "index_pattern": index_pattern,
        "total_candidates": len(candidates),
        "text_field": text_field,
        "embedding_field": embedding_field,
        "query_dsl": body,
    }
    if mode == "bm25_fallback" and bool(model_id):
        meta["fallback_reason"] = (
            f"No '{embedding_field}' field found in '{index_pattern}'. "
            f"Run `setup-index`/`enrich` to add neural embeddings for full hybrid recall."
        )
    return candidates, meta
