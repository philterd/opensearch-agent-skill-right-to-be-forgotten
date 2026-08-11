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

    Limit: two people sharing a display name are merged, since the headers
    cannot tell them apart, and the labels then cover both.
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


def _entry_forms(entry, min_length):
    """The (names, logins) one roster entry generates."""
    names, logins = set(), set()
    for variant in entry["attributes"]["display_name_variants"]:
        names.update(_name_forms(variant))
    # Observed Exchange aliases first; the generated forms are a fallback for
    # addresses whose X- header never carried a distinguished name.
    logins.update(entry["attributes"].get("exchange_logins") or [])
    logins.update(_login_forms(entry["id"]))
    return ({n for n in names if len(n) >= min_length},
            {l for l in logins if len(l) >= min_length})


def variant_owners(entries, min_length=MIN_VARIANT_LENGTH):
    """Map each variant the roster can generate to the addresses generating it.

    A variant more than one person produces identifies none of them. This is
    EVALUATION.md scheme 2b's uniqueness test on the naming channel.
    """
    owners = {}
    for entry in entries:
        names, logins = _entry_forms(entry, min_length)
        for variant in names | logins | {entry["id"]}:
            owners.setdefault(variant.lower(), set()).add(entry["id"])
    return owners


def alias_set(entries, address, min_length=MIN_VARIANT_LENGTH):
    """Build the alias set for one subject.

    Two lists, because masking and labelling need different breadth.

    ``variants`` is anything that might refer to the subject and is what gets
    masked; over-masking only costs context. ``label_variants`` is the subset
    no other person in the roster produces, and is what makes a document a
    positive. Labelling on the broad set made 92% of one subject's positives
    documents about a different Harry.
    """
    by_address = {e["id"]: e for e in entries}
    addresses = related_addresses(entries, address)

    names, logins = set(), set()
    for addr in addresses:
        entry = by_address.get(addr)
        if entry is None:
            continue
        entry_names, entry_logins = _entry_forms(entry, min_length)
        names |= entry_names
        logins |= entry_logins

    names, logins = sorted(names), sorted(logins)
    # An address is masked whole, so its local part being a login form is not a
    # separate risk; both are kept because prose cites bare logins too.
    variants = sorted(set(addresses) | set(names) | set(logins), key=lambda v: (-len(v), v))

    owners = variant_owners(entries, min_length)
    mine = set(addresses)
    label_variants, ambiguous = [], []
    for variant in variants:
        # Unowned means no other roster entry generates it either.
        if owners.get(variant.lower(), set()) <= mine:
            label_variants.append(variant)
        else:
            ambiguous.append(variant)

    return {
        "subject": address,
        "addresses": addresses,
        "name_variants": names,
        "login_variants": logins,
        "variants": variants,
        "label_variants": label_variants,
        "ambiguous_variants": ambiguous,
        "min_variant_length": min_length,
    }


def build_pattern(variants):
    """One case-insensitive alternation, longest variant first.

    Longest-first so a whole address is consumed, not just its local part.

    The alternation must stay grouped: ungrouped, it binds looser than the
    lookarounds and only the first and last branch get boundaries, so `Allen`
    matches inside `Callender`. Lookarounds rather than \\b so a variant
    ending in `.` still anchors.

    The boundary excludes `@` deliberately: forwarded mail wraps addresses as
    `<Lynn.Blair@e| | |nron.com>`, and skipping `Blair@` as address interior
    left the surname in the masked corpus.
    """
    usable = [v for v in variants if v]
    if not usable:
        return None
    ordered = sorted(set(usable), key=lambda v: (-len(v), v))
    alternation = "|".join(re.escape(v) for v in ordered)
    return re.compile(rf"(?<!\w)(?:{alternation})(?!\w)", re.IGNORECASE)


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
