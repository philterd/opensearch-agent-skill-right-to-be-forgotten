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
    # All 8 groups, or a '::' compression. Looser patterns match HH:MM:SS.
    "ipv6": re.compile(
        r"(?<![\w:.])(?:[A-Fa-f0-9]{1,4}:){7}[A-Fa-f0-9]{1,4}(?![\w:.])"
        r"|(?<![\w:.])(?:[A-Fa-f0-9]{1,4})?(?::[A-Fa-f0-9]{1,4}){0,6}::"
        r"(?:[A-Fa-f0-9]{1,4})?(?::[A-Fa-f0-9]{1,4}){0,6}(?![\w:.])"
    ),
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
            if kind == "ipv6" and not re.search(r"[A-Fa-f0-9]", value):
                continue  # bare '::' identifies nobody
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


def build_direct_query(identifiers, text_field, size, identity_fields=()):
    """Bool-should of matches for each identifier, across text and identity fields.

    Searching only the text field misses the person wherever the corpus records
    them structurally instead of mentioning them. Measured on Enron, one
    subject's address appears in 94 message bodies and 3,692 header fields, so
    a text-only pass found 3% of their footprint.

    Identity fields are keyword-typed, so an analyzer phrase match does not
    apply: `term` catches a field holding the bare value and a case-insensitive
    `wildcard` catches it embedded in a longer one such as `Name <addr>`.
    """
    shoulds = [{"match_phrase": {text_field: ident["value"]}} for ident in identifiers]
    for field in identity_fields:
        for ident in identifiers:
            value = ident["value"]
            shoulds.append({"term": {field: value}})
            shoulds.append({"wildcard": {field: {"value": f"*{value}*",
                                                 "case_insensitive": True}}})
    return {
        "size": size,
        "query": {"bool": {"should": shoulds, "minimum_should_match": 1}},
    }


def identity_fields_of(client, index_pattern, text_field):
    """Identity-bearing fields in the mapping, excluding the searched text."""
    from lib.suitability import naming_channel
    properties = {}
    for body in client.indices.get_mapping(index=index_pattern).values():
        properties.update((body.get("mappings") or {}).get("properties") or {})
    # An unindexed field can label an answer but cannot be queried.
    return [f["field"] for f in naming_channel(properties, text_field) if f["searchable"]]


def _field_matches(source, identifiers, fields):
    """Which identity fields hold which identifier value, verbatim."""
    found = []
    for field in fields:
        value = source.get(field)
        if value is None:
            continue
        values = value if isinstance(value, list) else [value]
        for ident in identifiers:
            needle = ident["value"].lower()
            for entry in values:
                if needle in str(entry).lower():
                    found.append({"field": field, "value": str(entry),
                                  "matched": ident["value"], "type": ident["type"]})
                    break
    return found


def _find_verbatim(text, value):
    """Case-insensitive locate; return the exact-cased substring from ``text``."""
    if not text or not value:
        return None
    idx = text.lower().find(value.lower())
    return text[idx:idx + len(value)] if idx >= 0 else None


def discover_direct(client, index_pattern, identifiers, text_field="message",
                    timestamp_field="@timestamp", size=200, scan_pii=True,
                    identity_fields=None):
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

    if identity_fields is None:
        try:
            identity_fields = identity_fields_of(client, index_pattern, text_field)
        except Exception:  # noqa: BLE001 - a mapping failure must not stop the pass
            identity_fields = []
    body = build_direct_query(identifiers, text_field, size, identity_fields)
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

        field_hits = _field_matches(src, identifiers, identity_fields)
        if field_hits:
            matched_types.extend(h["type"] for h in field_hits)

        if snippets or field_hits:
            where = []
            if snippets:
                where.append(f"in {text_field}")
            if field_hits:
                where.append("in " + ", ".join(sorted({h["field"] for h in field_hits})))
            evaluations.append({
                "doc_id": doc_id, "index": index, "text": text,
                "timestamp": src.get(timestamp_field),
                "is_identifiable": True, "confidence_score": 1.0,
                "identifying_snippets": snippets,
                "identifying_fields": field_hits,
                "reasoning": (f"Direct identifier match "
                              f"({', '.join(sorted(set(matched_types)))}) "
                              f"{' and '.join(where)}."),
            })
        else:
            evaluations.append({"doc_id": doc_id, "is_identifiable": False,
                                "confidence_score": 0.0, "identifying_snippets": [],
                                "reasoning": "Matched by the analyzer but no verbatim identifier present."})

    field_only = sum(1 for e in evaluations
                     if e.get("identifying_fields") and not e.get("identifying_snippets"))
    meta = {
        "mode": "direct",
        "index_pattern": index_pattern,
        "identifier_count": len(identifiers),
        "identity_fields_searched": list(identity_fields),
        "total_candidates": len(candidates),
        "flagged": sum(1 for e in evaluations if e["is_identifiable"]),
        "flagged_by_field_only": field_only,
        "query_dsl": body,
    }
    if field_only:
        meta["remediation_note"] = (
            f"{field_only} document(s) match only in an identity field, not in "
            f"{text_field}. `redact_in_place` rewrites {text_field} only, so it would "
            f"not remove them; use `hard_delete`, or redact the field by hand."
        )
    return candidates, evaluations, meta
