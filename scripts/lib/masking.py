"""Alias sets and corpus masking.

Stage two of the evaluation method in EVALUATION.md, scheme 2a: take every
document whose `message` contained one of a subject's identifiers as the
positive set, remove those identifiers, and evaluate on what remains. The label
is a fact about the corpus, since the string was there before it was removed.

Masking manufactures the indirect case. A sentence written without a name would
have been phrased differently from one with the name removed, so the masked set
is a proxy for naturally occurring indirect reference, not a sample of it.
"""

import hashlib
import os
import re

MASKED_INDEX = "mail-enron-masked"
LABELS_PATH = os.path.join("gdpr-eval", "enron-labels.json")
MASK_TOKEN = "[MASKED]"

# Namespace for masked document ids. Bump it to invalidate an existing masked
# index; every id changes with it.
_ID_NAMESPACE = "gdpr-forget-me/enron-masked/v1"

# Two-character variants are dropped: initials like "PA" collide with state
# abbreviations and ordinary words, and masking those destroys the residual
# context the evaluation is trying to measure.
MIN_VARIANT_LENGTH = 3

_OPAQUE_ID_RE = re.compile(r"^m-[0-9a-f]{12}$")


def masked_doc_id(original_id):
    """Map an original id to an opaque one.

    `seed_enron` ids are `{custodian}/{folder}/{num}`, so the custodian surname
    rides along in every id. `discover` hands each candidate's doc_id to the
    agent that judges it, so a readable id leaks the answer into the judgment.
    """
    digest = hashlib.sha1(f"{_ID_NAMESPACE}:{original_id}".encode()).hexdigest()
    return f"m-{digest[:12]}"


def is_opaque_id(doc_id):
    return bool(_OPAQUE_ID_RE.match(str(doc_id or "")))


def _clean(text):
    return " ".join((text or "").split()).strip("'\"")


def _name_forms(variant):
    """Expand one display-name variant into the forms it is written in.

    Enron headers use both `Phillip K Allen` and `Allen, Phillip K`, and the
    prose uses first name, surname, and initials interchangeably.
    """
    variant = _clean(variant)
    if not variant:
        return []

    forms = {variant}
    if "," in variant:
        last, _, rest = variant.partition(",")
        last, rest = _clean(last), _clean(rest)
        if last and rest:
            forms.add(f"{rest} {last}")
            tokens = rest.split() + [last]
        else:
            tokens = variant.replace(",", " ").split()
    else:
        tokens = variant.split()

    tokens = [t for t in tokens if t]
    if not tokens:
        return sorted(forms)

    given, surname = tokens[0], tokens[-1]
    forms.add(given)
    forms.add(surname)
    if len(tokens) > 1:
        forms.add(f"{given} {surname}")
        forms.add(f"{given[0]} {surname}")
        forms.add(f"{given[0]}. {surname}")
        initials = "".join(t[0] for t in tokens)
        forms.add(initials)
        forms.add(".".join(initials) + ".")
    return sorted(forms)


def _login_forms(address):
    """Login and alias forms derivable from an email local part."""
    local = address.split("@", 1)[0]
    forms = {local}
    parts = [p for p in re.split(r"[._-]+", local) if p]
    if len(parts) >= 2:
        first, last = parts[0], parts[-1]
        forms.update({
            f"{first}.{last}", f"{first}_{last}", f"{first}{last}",
            f"{first[0]}{last}", f"{last}{first[0]}",
        })
    return sorted(forms)


def related_addresses(entries, address):
    """Addresses that share a display-name variant with ``address``.

    People hold several addresses in this corpus. The roster is keyed by
    address, so the person is reassembled by matching name variants.
    """
    by_address = {e["id"]: e for e in entries}
    subject = by_address.get(address)
    if subject is None:
        return [address]
    wanted = {_clean(v).lower() for v in subject["attributes"]["display_name_variants"]}
    if not wanted:
        return [address]
    found = {address}
    for entry in entries:
        variants = {_clean(v).lower() for v in entry["attributes"]["display_name_variants"]}
        if variants & wanted:
            found.add(entry["id"])
    return sorted(found)


def alias_set(entries, address, min_length=MIN_VARIANT_LENGTH):
    """Build the alias set for one subject.

    Returns the variants grouped by origin so a report can state exactly what
    was masked, plus the flat list masking and the audit both consume.
    """
    by_address = {e["id"]: e for e in entries}
    addresses = related_addresses(entries, address)

    names, logins = set(), set()
    for addr in addresses:
        entry = by_address.get(addr)
        if entry is None:
            continue
        for variant in entry["attributes"]["display_name_variants"]:
            names.update(_name_forms(variant))
        logins.update(_login_forms(addr))

    def keep(values):
        return sorted({v for v in values if len(v) >= min_length})

    names, logins = keep(names), keep(logins)
    # An address is masked whole, so its local part being a login form is not a
    # separate risk; both are kept because prose cites bare logins too.
    variants = sorted(set(addresses) | set(names) | set(logins), key=lambda v: (-len(v), v))
    return {
        "subject": address,
        "addresses": addresses,
        "name_variants": names,
        "login_variants": logins,
        "variants": variants,
        "min_variant_length": min_length,
    }


def build_pattern(variants):
    """One case-insensitive alternation, longest variant first.

    Longest-first matters: `phillip.allen@enron.com` must be consumed whole
    rather than leaving `@enron.com` behind after matching `phillip.allen`.

    The alternation must be grouped. Alternation binds looser than the
    lookarounds, so an ungrouped pattern anchors only its first and last branch
    and every variant between them matches mid-word, masking `Callender` for
    `Allen`. Lookarounds rather than \\b so variants ending in `.` still anchor.
    """
    usable = [v for v in variants if v]
    if not usable:
        return None
    ordered = sorted(set(usable), key=lambda v: (-len(v), v))
    alternation = "|".join(re.escape(v) for v in ordered)
    return re.compile(rf"(?<![\w@])(?:{alternation})(?![\w@])", re.IGNORECASE)


def mask_text(text, pattern, token=MASK_TOKEN):
    """Return (masked_text, hit_count).

    seed_enron flattens subject, body, and quoted reply chains into one
    `message` string, so a single pass over the field covers quoted blocks too.
    """
    if not text or pattern is None:
        return text or "", 0
    hits = 0

    def _sub(match):
        nonlocal hits
        hits += 1
        return token

    return pattern.sub(_sub, text), hits


def find_variants(text, pattern):
    """Distinct verbatim matches in ``text``. Used by the audit."""
    if not text or pattern is None:
        return []
    seen, out = set(), []
    for match in pattern.finditer(text):
        value = match.group(0)
        key = value.lower()
        if key not in seen:
            seen.add(key)
            out.append(value)
    return out
