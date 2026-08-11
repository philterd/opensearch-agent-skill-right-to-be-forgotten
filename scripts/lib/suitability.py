"""Is the indirect pass worth anything on this index?

The direct pass works wherever identifiers appear literally. The indirect pass
only earns its keep where documents *describe* people, and corpora differ
enormously in whether they do. Measured on two real corpora: 0.39% of Enron
email carries role-reference language, against 40% of US court opinions.

This samples an index and answers the question before anyone trusts a result,
rather than after.
"""

import re

# Person-role nouns, deliberately broad and cross-domain.
_ROLES = (r"engineer|analyst|developer|administrator|operator|technician|manager|"
          r"director|officer|deputy|supervisor|contractor|consultant|employee|"
          r"colleague|clinician|doctor|physician|nurse|patient|caregiver|"
          r"defendant|plaintiff|appellant|appellee|petitioner|respondent|witness|"
          r"claimant|customer|client|tenant|applicant|driver|pilot|controller|"
          r"teacher|student|reviewer|author|owner|assignee|reporter|caller")

# "the analyst who", "our on-call", "whoever approved", "the person responsible".
DESCRIPTIVE = re.compile(
    rf"\b(?:the|our|their|a|an|that)\s+(?:\w+\s+){{0,2}}(?:{_ROLES})\b"
    rf"|\bwho(?:ever|m)?\s+(?:was|is|had|has|were|approved|deployed|signed|"
    rf"filed|handled|noticed|reported|owned)\b"
    rf"|\bthe\s+person\s+(?:who|responsible)\b"
    rf"|\bon[- ]call\b", re.I)

# Names in a list, not a sentence: recipient blocks and header dumps.
_NAMEPAIR = re.compile(r"[A-Z][A-Za-z.'-]+,\s+[A-Z][A-Za-z.'-]+")
_HEADER = re.compile(r"(?:From:|To:|Cc:|Sent:|Subject:|-----Original Message-----"
                     r"|Forwarded by|@\w+\.(?:com|org|net)|DL-)", re.I)

WINDOW = 140
# Fields whose name suggests they identify a person.
_IDENTITY_HINT = re.compile(
    r"(?:^|[._-])(?:user|username|userid|actor|principal|subject|author|owner|"
    r"assignee|reporter|custodian|employee|account|email|mail|from|to|cc|"
    r"sender|recipient|caller|agent|operator|created_by|updated_by|reviewer|"
    r"party|parties|name)(?:$|[._-])", re.I)

# References per document, all measured with the detector above on 1,000
# documents each. Percent-of-documents does not separate these corpora; density
# does, by two orders of magnitude.
BENCHMARKS = {
    "US court opinions": 21.0,   # indirect pass has ample material
    "Enron email": 0.20,         # measured: recovered almost nothing
    "synthetic demo logs": 0.03,  # sparse, but its few descriptions are good
}
RICH_ABOVE = 1.0
ABSENT_BELOW = 0.05
PROSE_SHARE_FLOOR = 0.25


def is_list_context(window):
    return (len(_NAMEPAIR.findall(window)) >= 3
            or window.count(";") >= 3
            or bool(_HEADER.search(window)))


MERGE_GAP = 4


def references(text):
    """Descriptive references as (start, end, phrase), adjacent ones merged.

    "the senior engineer who owned it" is one description, but the role clause
    and the "who" clause both match, so raw counts double it.
    """
    spans = []
    for match in DESCRIPTIVE.finditer(text or ""):
        if spans and match.start() - spans[-1][1] <= MERGE_GAP:
            spans[-1][1] = max(spans[-1][1], match.end())
        else:
            spans.append([match.start(), match.end()])
    return [(a, b, " ".join(text[a:b].lower().split())) for a, b in spans]


def classify(text, window=WINDOW):
    """Return (prose_hits, list_hits) for descriptive references in one document."""
    prose = listed = 0
    for start, end, _ in references(text):
        chunk = text[max(0, start - window):end + window]
        if is_list_context(chunk):
            listed += 1
        else:
            prose += 1
    return prose, listed


def naming_channel(mapping_properties, text_field):
    """Fields that could label a description, excluding the searched text.

    ``searchable`` is false for a field mapped `index: false`, which can still
    be read from `_source` to build labels but cannot be queried.
    """
    found = []
    for field, spec in sorted((mapping_properties or {}).items()):
        if field == text_field:
            continue
        spec = spec or {}
        kind = spec.get("type", "object")
        if kind in ("text", "keyword") and _IDENTITY_HINT.search(field):
            found.append({"field": field, "type": kind,
                          "searchable": spec.get("index", True) is not False})
    return found


def verdict(density, prose_share, has_naming_channel, prose_documents):
    """Three bands, not a yes/no: density alone cannot decide it.

    ``density`` is descriptive references per document. A small corpus can hold
    few references and still be worth running if the ones it has are good, so
    the absolute count of documents carrying one is reported alongside.
    """
    if density >= RICH_ABOVE:
        band = "rich"
        summary = (f"{density:.1f} descriptive references per document. There is ample "
                   f"material for the indirect pass.")
    elif density < ABSENT_BELOW:
        band = "absent"
        summary = (f"{density:.2f} descriptive references per document. Documents here "
                   f"barely describe people. {prose_documents} carry a description, so "
                   f"that is the ceiling on what the indirect pass can find, however "
                   f"well it works.")
    else:
        band = "sparse"
        summary = (f"{density:.2f} descriptive references per document, between the "
                   f"corpora measured so far. Whether the pass is worth running depends "
                   f"on the {prose_documents} documents that do carry one.")

    reasons = [summary]
    if prose_share < PROSE_SHARE_FLOOR:
        reasons.append(
            f"Only {100*prose_share:.0f}% of those references sit in running prose; the "
            f"rest are recipient lists or header blocks, which name people without "
            f"describing them and cannot be found by description.")
    if not has_naming_channel:
        reasons.append(
            "No field looks like a naming channel, so the indirect pass may still work "
            "but you cannot measure it on this index: there is nothing to check an "
            "answer against.")
    reasons.append(
        "A role noun is not a description. This counts phrases like \"the customer\" "
        "alongside \"the sole engineer on call that night\", so read the phrase list "
        "before trusting the band.")
    return {
        "material_for_indirect_pass": band,
        "reasons": reasons,
        "benchmark_references_per_document": BENCHMARKS,
    }


def assess(client, index, text_field="message", sample=500):
    """Sample an index and report whether it supports indirect identification."""
    resp = client.search(index=index, body={
        "size": sample,
        "query": {"function_score": {"query": {"match_all": {}},
                                     "random_score": {"seed": 1, "field": "_seq_no"}}},
        "_source": [text_field],
    })
    hits = resp.get("hits", {}).get("hits", [])
    scanned = describing = prose_docs = 0
    prose_total = list_total = 0
    phrases = {}
    for hit in hits:
        text = (hit.get("_source") or {}).get(text_field) or ""
        if not text.strip():
            continue
        scanned += 1
        prose, listed = classify(text)
        if prose or listed:
            describing += 1
        if prose:
            prose_docs += 1
        prose_total += prose
        list_total += listed
        for _, _, phrase in references(text):
            phrases[phrase] = phrases.get(phrase, 0) + 1

    mapping = client.indices.get_mapping(index=index)
    properties = {}
    for body in mapping.values():
        properties.update((body.get("mappings") or {}).get("properties") or {})
    channel = naming_channel(properties, text_field)

    total_refs = prose_total + list_total
    percent = 100.0 * describing / scanned if scanned else 0.0
    density = total_refs / scanned if scanned else 0.0
    prose_share = prose_total / total_refs if total_refs else 0.0
    return {
        "index": index,
        "text_field": text_field,
        "documents_sampled": scanned,
        "describing_a_person": {"documents": describing, "percent": round(percent, 1)},
        "references_per_document": round(density, 2),
        "references": {"in_prose": prose_total, "in_list_context": list_total,
                       "prose_share": round(prose_share, 2)},
        "documents_with_prose_reference": prose_docs,
        "commonest_phrases": sorted(phrases.items(), key=lambda kv: -kv[1])[:10],
        "naming_channel_candidates": channel,
        "verdict": verdict(density, prose_share, bool(channel), prose_docs),
    }


_NOTES = {
    "rich": "This corpus describes people; a thin result is about the subject.",
    "sparse": ("Descriptions are uncommon here. The documents carrying one are the "
               "ceiling on what this pass can return, however well it works."),
    "absent": ("This corpus barely describes people. Report an empty or thin result as "
               "a property of the corpus, not as evidence the subject is absent, and "
               "rely on the direct pass."),
}


def applicability_note(band, candidate_count):
    """How to read an indirect result on a corpus of this kind.

    "Nothing found" and "this corpus does not describe people" produce the same
    empty list and mean opposite things to a compliance reader.
    """
    note = _NOTES[band]
    if candidate_count == 0 and band != "rich":
        note += " No candidates were returned, so say this before reporting the result."
    return note
