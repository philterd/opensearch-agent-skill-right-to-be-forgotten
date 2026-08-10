"""The leakage audit: a gate, not a warning.

If masking is incomplete the metric is fiction, because a single surviving
mention makes the document trivially retrievable by the identifier the
evaluation claims to have removed. So the audit fails the run rather than
reporting a caveat alongside a score.

It checks five things, matching the checklist in EVALUATION.md step 3:

  1. no alias variant survives anywhere in the masked text;
  2. every masked document id is opaque, so the custodian surname cannot be
     read off `{custodian}/{folder}/{num}`;
  3. no header or container field (custodian, folder, from/to/cc) was carried
     into the masked index;
  4. signature-block phone numbers and extensions are counted, with the
     decision recorded on whether they count as leakage or as identification;
  5. the index a score is computed from is the masked one.
"""

import re

from lib.masking import find_variants, is_opaque_id

# Fields that name people or the container they sit in. None may reach the
# masked index: `custodian` and `folder` are named after the mailbox owner, and
# the address headers are the label source held out of the search path.
FORBIDDEN_FIELDS = ("from", "to", "cc", "custodian", "folder", "message_id", "subject")

# Deliberately loose. Over-reporting a phone number is cheap; missing a direct
# line in a signature block is not.
_PHONE_RE = re.compile(r"\+?\d[\d\s().\-]{7,}\d")
_EXTENSION_RE = re.compile(r"\b(?:x|ext\.?|extension)\s*\d{3,6}\b", re.IGNORECASE)

PHONE_POLICIES = {
    "identification": (
        "Counted as true identification, not leakage. An unmasked direct line "
        "identifies as well as a name does, so leaving it in measures whether the "
        "pipeline can identify a person from residual context, which is the "
        "capability under test. Recall measured this way includes documents "
        "findable by phone number."
    ),
    "leakage": (
        "Counted as leakage. Any surviving direct identifier makes the document "
        "trivially retrievable, so the run fails until phone numbers are masked "
        "too. Use this to measure identification from prose alone."
    ),
}


def count_phone_signals(text):
    if not text:
        return 0
    digits_ok = [m for m in _PHONE_RE.findall(text) if len(re.sub(r"\D", "", m)) >= 10]
    return len(digits_ok) + len(_EXTENSION_RE.findall(text))


def audit(documents, pattern, phone_policy="identification", text_field="message"):
    """Audit masked documents. ``documents`` yields (doc_id, source).

    Returns a report whose ``passed`` decides whether a score may be produced.
    """
    if phone_policy not in PHONE_POLICIES:
        raise ValueError(f"Unknown phone policy '{phone_policy}'.")

    scanned = 0
    surviving_docs = 0
    surviving_variants = set()
    readable_ids = 0
    forbidden_seen = set()
    phone_docs = 0
    phone_hits = 0

    for doc_id, source in documents:
        scanned += 1
        if not is_opaque_id(doc_id):
            readable_ids += 1
        forbidden_seen.update(f for f in FORBIDDEN_FIELDS if f in source)

        text = source.get(text_field) or ""
        found = find_variants(text, pattern)
        if found:
            surviving_docs += 1
            surviving_variants.update(v.lower() for v in found)

        hits = count_phone_signals(text)
        if hits:
            phone_docs += 1
            phone_hits += hits

    phones_are_leakage = phone_policy == "leakage"
    failures = []
    if surviving_variants:
        failures.append(
            f"{len(surviving_variants)} alias variant(s) survived masking in "
            f"{surviving_docs} document(s)."
        )
    if readable_ids:
        failures.append(
            f"{readable_ids} document(s) carry a readable id; masked ids must be opaque."
        )
    if forbidden_seen:
        failures.append(
            "Masked index carries header or container fields: "
            + ", ".join(sorted(forbidden_seen))
        )
    if phones_are_leakage and phone_hits:
        failures.append(
            f"{phone_hits} phone number(s) or extension(s) survive in {phone_docs} "
            f"document(s), and the phone policy counts those as leakage."
        )

    return {
        "passed": not failures,
        "failures": failures,
        "documents_scanned": scanned,
        "surviving_variants": {
            # Counts only. The variants are the subject's own name.
            "distinct": len(surviving_variants),
            "documents": surviving_docs,
        },
        "readable_document_ids": readable_ids,
        "forbidden_fields_present": sorted(forbidden_seen),
        "container_names": {
            "custodian_and_folder_carried": bool(
                {"custodian", "folder"} & set(forbidden_seen)
            ),
            "decision": (
                "Removed rather than judged: the masked index carries only the masked "
                "text and its timestamp, so a mailbox or folder named after the subject "
                "cannot reach a search over it."
            ),
        },
        "phone_signals": {
            "documents": phone_docs,
            "occurrences": phone_hits,
            "policy": phone_policy,
            "decision": PHONE_POLICIES[phone_policy],
        },
    }


def assert_scorable(labels, index):
    """Raise unless ``index`` is the masked index this label set was built for.

    The unmasked corpus stays in the cluster, so a run pointed at the wrong
    index would score perfectly for the wrong reason. Scoring calls this before
    reading any result.
    """
    expected = (labels or {}).get("masked_index")
    if not expected:
        raise ValueError(
            "Label set records no masked_index; it was not produced by mask-corpus."
        )
    if index != expected:
        raise ValueError(
            f"Refusing to score '{index}': these labels were built for '{expected}'. "
            f"The unmasked corpus is still in the cluster, so scoring the wrong index "
            f"would report a perfect result for the wrong reason."
        )
    audit_report = (labels or {}).get("audit") or {}
    if not audit_report.get("passed"):
        raise ValueError(
            "Refusing to score: the leakage audit for this label set did not pass. "
            + " ".join(audit_report.get("failures", []))
        )
    return True
