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

# A marker must be at least this much more common than the positives before its
# presence stops being a usable oracle. At 20x, seeing it makes a document 5%
# likely to be a positive rather than certain.
MARKER_DILUTION = 20

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


def alias_set_failures(aliases):
    """Reasons an alias set cannot support a trustworthy audit.

    The audit only looks for variants it was given, so a nameless alias set
    passes while the corpus still prints the subject's name. Observed live: six
    address-derived variants passed while 89 documents held the full name.
    """
    if aliases is None:
        return []
    subject = aliases.get("subject", "unknown")
    failures = []
    if not aliases.get("name_variants"):
        failures.append(
            f"Alias set for '{subject}' contains no name variants, only addresses "
            f"and logins, so masking cannot remove the subject's name and the audit "
            f"would pass without checking for it. Give the roster a display name for "
            f"this subject, or pick another."
        )
    if not aliases.get("label_variants"):
        failures.append(
            f"No variant of '{subject}' is unique to them in the roster, so every "
            f"document a label would rest on could be about somebody else. There is "
            f"nothing to score against. Pick a subject whose name is not shared."
        )
    return failures


def marker_failures(marker, marker_documents, positive_count):
    """Fail when the replacement marker is itself the label set.

    A visible marker lands in exactly the documents that contained a variant,
    so one query returns the answer key. Diluting it into negatives only helps
    if nearly every document gets one, so the practical fix is to leave no
    marker at all.
    """
    if not marker or not positive_count:
        return []
    if marker_documents < positive_count * MARKER_DILUTION:
        return [
            f"Mask marker {marker!r} appears in {marker_documents} document(s) "
            f"against {positive_count} positive(s), so searching for it returns "
            f"the label set. Mask with an empty replacement, or dilute the marker "
            f"to at least {MARKER_DILUTION}x the positive count."
        ]
    return []


def _substring_pattern(aliases):
    """A looser pattern than the masker uses, for reporting only.

    The audit shares the masker's pattern, so a boundary rule that skips a name
    skips it on the way back out too. This ignores boundaries entirely and only
    reports, since it over-reports by construction.
    """
    names = [n for n in (aliases or {}).get("name_variants") or [] if len(n) >= 4]
    if not names:
        return None
    ordered = sorted(set(names), key=lambda v: (-len(v), v))
    return re.compile("|".join(re.escape(n) for n in ordered), re.IGNORECASE)


def audit(documents, pattern, phone_policy="identification", text_field="message",
          aliases=None, marker=None, positive_count=None):
    """Audit masked documents. ``documents`` yields (doc_id, source).

    Returns a report whose ``passed`` decides whether a score may be produced.
    Pass ``aliases`` so the gate can also fail on an alias set too thin to
    audit against.
    """
    if phone_policy not in PHONE_POLICIES:
        raise ValueError(f"Unknown phone policy '{phone_policy}'.")

    loose = _substring_pattern(aliases)
    loose_docs = 0
    marker_docs = 0

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

        if loose is not None and loose.search(text):
            loose_docs += 1
        if marker and marker in text:
            marker_docs += 1

        hits = count_phone_signals(text)
        if hits:
            phone_docs += 1
            phone_hits += hits

    phones_are_leakage = phone_policy == "leakage"
    failures = alias_set_failures(aliases)
    failures += marker_failures(marker, marker_docs, positive_count)
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
        "alias_set": {
            "name_variants": len((aliases or {}).get("name_variants") or []),
            "login_variants": len((aliases or {}).get("login_variants") or []),
            "addresses": len((aliases or {}).get("addresses") or []),
        },
        "surviving_variants": {
            # Counts only. The variants are the subject's own name.
            "distinct": len(surviving_variants),
            "documents": surviving_docs,
        },
        "residual_name_substrings": {
            "documents": loose_docs,
            "note": (
                "Reported, not enforced. Ignores word boundaries, so it over-reports "
                "by matching short surnames inside longer words. A number well above "
                "zero here with zero surviving variants means the masking pattern's "
                "boundary rule is skipping something it should remove."
            ),
        },
        "mask_marker": {
            "marker": marker or "",
            "documents": marker_docs,
            "note": ("Empty marker: masking removes the variant and leaves nothing "
                     "to search for." if not marker else
                     "A marker confined to the positives is an oracle for them."),
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
