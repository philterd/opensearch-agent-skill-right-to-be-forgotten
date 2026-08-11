"""Case captions as a naming channel.

Court opinions describe a person's conduct at length while referring to them by
role, and the caption names them in a separate structured field. That is the
two-channel shape the method needs, and the shape Enron email lacked.

Parsing captions is fiddlier than `X v. Y`: parties carry role designations,
suffixes and corporate forms. A naive split made "Defendant-Appellant" the most
common surname in a sample, and "State of Louisiana" yield "Louisiana".
"""

import re

# Stripped from a party before its surname is taken.
_ROLE = re.compile(
    r"[,;]?\s*\b(?:defendants?|plaintiffs?|appell(?:ants?|ees?)|petitioners?|"
    r"respondents?|movants?|intervenors?|claimants?|garnishees?|relators?|"
    r"cross[-\s]?\w+|et\s+al|et\s+ux|pro\s+se|individually|d/b/a|a/k/a|"
    r"attorney\s+general|solicitor\s+general|district\s+attorney|"
    # Delaware styles divorce captions "Husband v. Wife"; the party is anonymous.
    r"husband|wife|minor\s+child|next\s+friend|guardian\s+ad\s+litem|"
    r"in\s+his\s+\w+\s+capacity|in\s+her\s+\w+\s+capacity)\b\.?", re.I)

# "the STATE of Texas" would otherwise read as a person headed by "the".
_ARTICLE = re.compile(r"^(?:the|a|an)\s+", re.I)

_SUFFIX = re.compile(r"[,\s]+\b(?:jr|sr|ii|iii|iv|esq|md|phd)\b\.?$", re.I)

# A party headed by one of these is an institution, however it continues:
# "State of Louisiana", "United States", "County of Kern".
INSTITUTIONAL_HEAD = {
    "united", "state", "states", "people", "commonwealth", "city", "county",
    "town", "village", "township", "department", "board", "bureau", "district",
    "court", "commissioner", "secretary", "director", "warden", "sheriff",
    "estate", "matter", "university", "hospital", "school", "administration",
    "division", "agency", "republic", "government",
}

# A party containing one of these is an organisation wherever it appears.
CORPORATE = {
    "inc", "llc", "llp", "ltd", "corp", "corporation", "company", "companies",
    "co", "bank", "insurance", "holdings", "associates", "partners", "trust",
    "systems", "group", "enterprises", "industries", "services", "association",
    "railroad", "railway", "airlines", "motors", "foundation",
}

GENERIC = INSTITUTIONAL_HEAD | CORPORATE

_VERSUS = re.compile(r"\s+v[s]?\.?\s+", re.I)
# "State of Tennessee, ex rel. Latonya Campbell" names a real individual behind
# an institutional party; the relator is a party in their own right.
_RELATOR = re.compile(r"[,;]?\s*\bex\s+rel(?:atione)?\.?\s+", re.I)
_PREFIX = re.compile(r"^\s*(?:in\s+re|ex\s+parte|matter\s+of|estate\s+of)\b[:\s]*", re.I)
MIN_SURNAME = 4


def _tokens(party):
    return [t for t in (p.strip(".,;'\"") for p in re.split(r"[\s,]+", party or "")) if t]


def _clean_party(text):
    text = _ROLE.sub(" ", text or "")
    text = re.sub(r"[\s\-]{2,}", " ", text)
    text = re.sub(r"\s{2,}", " ", text).strip(" ,;.-")
    text = _ARTICLE.sub("", text)
    return _SUFFIX.sub("", text).strip(" ,;.-")


def _person_part(party):
    """Trim an institution appended to a person, or return [] if it leads.

    Captions concatenate parties and offices into one string, comma delimited:
    "James E. Gaines, United States of America" holds two parties, and
    "Alexander M. Hunter, Twentieth Judicial District, Boulder, Colorado"
    trails a court and a place. Cut at the first comma-delimited segment that
    names an institution, which is the unit captions actually use.
    """
    segments = [seg.strip() for seg in (party or "").split(",") if seg.strip()]
    if not segments:
        return []
    kept = []
    for segment in segments:
        tokens = _tokens(segment)
        heads = [i for i, t in enumerate(tokens) if t.lower() in INSTITUTIONAL_HEAD]
        if heads:
            if not kept and heads[0] == 0:
                return []                       # the institution leads
            if not kept:
                # Institution inside the first segment: keep what precedes it,
                # but only if that still looks like a name rather than an
                # adjective, so "British Government General" yields nothing.
                return tokens[:heads[0]] if heads[0] >= 2 else []
            break                               # a later segment: stop here
        kept.extend(tokens)
    return kept


def parse_caption(caption):
    """Return the party strings in a caption, role designations removed."""
    caption = " ".join((caption or "").split())
    if not caption:
        return []
    sides = _VERSUS.split(_PREFIX.sub("", caption))
    parts = [piece for side in sides for piece in _RELATOR.split(side)]
    return [p for p in (_clean_party(p) for p in parts) if p]


def is_organisation(party):
    """Whether the party is an institution or company rather than a person."""
    tokens = [t.lower() for t in _tokens(party)]
    if not tokens:
        return True
    if any(t in CORPORATE for t in tokens):
        return True
    return not _person_part(party)


def surname_of(party):
    """The token an opinion would use for this party, or "" if it is not a person."""
    if is_organisation(party):
        return ""
    tokens = _person_part(_SUFFIX.sub("", party or ""))
    if not tokens:
        return ""
    surname = tokens[-1]
    if len(surname) < MIN_SURNAME or surname.lower() in GENERIC:
        return ""
    return surname if re.match(r"^[A-Za-z][A-Za-z'\-]+$", surname) else ""


def party_surnames(caption):
    """Distinct person-like surnames a caption names, in order."""
    out = []
    for party in parse_caption(caption):
        surname = surname_of(party)
        if surname and surname.lower() not in {s.lower() for s in out}:
            out.append(surname)
    return out


def given_names(caption):
    """First tokens of each personal party, to widen an alias set."""
    out = []
    for party in parse_caption(caption):
        if is_organisation(party):
            continue
        tokens = _person_part(party)
        if len(tokens) >= 2 and re.match(r"^[A-Z][a-z]{2,}$", tokens[0]):
            out.append(tokens[0])
    return out
