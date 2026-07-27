"""Direct-identifier discovery — the complement to indirect contextual search.

A real erasure request has two halves:

  * INDIRECT — the person is described without being named. Handled by the
    hybrid search + agent-reasoning path (discovery.py + evaluate.py).
  * DIRECT — the person's own identifiers (name, email, employee id, phone, IP)
    appear literally. Those matches are unambiguous: a document containing the
    subject's exact email IS about the subject, no reasoning needed.

This module handles the direct half. Given the identifiers supplied with the
request, it finds the documents that contain them and produces ready-to-act
evaluations (is_identifiable=true, confidence 1.0) with the matched values as
the redaction snippets. It also scans matched documents for *co-located* PII
(other emails/phones/IPs) so those get redacted in the same pass.
"""

import re

# Conservative PII patterns. Kept deliberately strict to limit false positives;
# phone matches are post-filtered by digit count.
_PATTERNS = {
    "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "ipv4": re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b"),
    "ipv6": re.compile(r"\b(?:[A-Fa-f0-9]{1,4}:){2,7}[A-Fa-f0-9]{1,4}\b"),
    "phone": re.compile(r"\+?\d[\d\s().\-]{7,}\d"),
}


def scan_text_pii(text):
    """Return a list of {type, value} PII matches found verbatim in ``text``."""
    if not text:
        return []
    found, seen = [], set()
    for kind, pattern in _PATTERNS.items():
        for m in pattern.finditer(text):
            value = m.group(0).strip()
            if kind == "phone" and len(re.sub(r"\D", "", value)) < 10:
                continue  # too few digits to be a real phone number
            key = (kind, value)
            if key not in seen:
                seen.add(key)
                found.append({"type": kind, "value": value})
    return found


def normalize_identifiers(email=None, phone=None, ip=None, name=None, id=None, term=None):
    """Build a flat identifier list from CLI-style option groups."""
    groups = {"email": email, "phone": phone, "ip": ip, "name": name,
              "employee_id": id, "term": term}
    identifiers = []
    for kind, values in groups.items():
        for v in (values or []):
            v = (v or "").strip()
            if v:
                identifiers.append({"type": kind, "value": v})
    return identifiers


def build_direct_query(identifiers, text_field, size):
    """Bool-should of exact phrase matches for each identifier value."""
    shoulds = [{"match_phrase": {text_field: ident["value"]}} for ident in identifiers]
    return {
        "size": size,
        "query": {"bool": {"should": shoulds, "minimum_should_match": 1}},
    }


def _find_verbatim(text, value):
    """Case-insensitive locate; return the exact-cased substring from ``text``."""
    if not text or not value:
        return None
    idx = text.lower().find(value.lower())
    return text[idx:idx + len(value)] if idx >= 0 else None


def discover_direct(client, index_pattern, identifiers, text_field="message",
                    timestamp_field="@timestamp", size=200, scan_pii=True):
    """Find documents containing the subject's direct identifiers.

    Returns (candidates, evaluations, meta). Evaluations for matched documents
    are auto-flagged (confidence 1.0) with verbatim matched values as snippets,
    so they can be passed straight to plan / export-curl with no agent
    step. Analyzer tokenisation can over-match, so every hit is re-checked to
    confirm an identifier (or scanned PII) is actually present as a substring.
    """
    if not identifiers:
        return [], [], {"mode": "direct", "reason": "no identifiers supplied",
                        "total_candidates": 0}

    body = build_direct_query(identifiers, text_field, size)
    resp = client.search(index=index_pattern, body=body)
    hits = resp.get("hits", {}).get("hits", [])

    candidates, evaluations = [], []
    for hit in hits:
        src = hit.get("_source", {})
        text = src.get(text_field)
        doc_id, index = hit.get("_id"), hit.get("_index")
        candidates.append({"doc_id": doc_id, "index": index, "score": hit.get("_score"),
                           "timestamp": src.get(timestamp_field), "text": text})

        snippets, matched_types = [], []
        for ident in identifiers:
            verbatim = _find_verbatim(text, ident["value"])
            if verbatim and verbatim not in snippets:
                snippets.append(verbatim)
                matched_types.append(ident["type"])
        if scan_pii:
            for pii in scan_text_pii(text or ""):
                if pii["value"] not in snippets:
                    snippets.append(pii["value"])
                    matched_types.append(pii["type"])

        if snippets:
            evaluations.append({
                "doc_id": doc_id, "index": index, "text": text,
                "timestamp": src.get(timestamp_field),
                "is_identifiable": True, "confidence_score": 1.0,
                "identifying_snippets": snippets,
                "reasoning": f"Direct identifier match ({', '.join(sorted(set(matched_types)))}).",
            })
        else:
            evaluations.append({"doc_id": doc_id, "is_identifiable": False,
                                "confidence_score": 0.0, "identifying_snippets": [],
                                "reasoning": "Matched by the analyzer but no verbatim identifier present."})

    meta = {
        "mode": "direct",
        "index_pattern": index_pattern,
        "identifier_count": len(identifiers),
        "total_candidates": len(candidates),
        "flagged": sum(1 for e in evaluations if e["is_identifiable"]),
        "query_dsl": body,
    }
    return candidates, evaluations, meta
