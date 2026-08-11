"""Pick a subject the corpus can actually score.

Two screens, both learned from runs that produced meaningless numbers.

Message count is the wrong ranking signal: it counts header appearances, and a
name in a forty-person recipient list masks to a document about nobody. Rank on
mentions in running prose instead.

A surname that is also an ordinary word ruins both masking and labelling.
Comparing surname frequency against full-name frequency separates the two.
"""

import hashlib
import re

SOURCE_INDEX = "mail-enron"

# Three or more "Last, First" pairs, a run of semicolons, or a header marker:
# a recipient list rather than a sentence.
_NAMEPAIR = re.compile(r"[A-Z][A-Za-z.'-]+,\s+[A-Z][A-Za-z.'-]+")
_HEADER = re.compile(r"(?:From:|To:|Cc:|Sent:|Subject:|-----Original Message-----"
                     r"|Forwarded by|@ENRON|/ENRON|DL-)", re.I)

WINDOW = 120
DEFAULT_SAMPLE = 300
# Above this, the surname carries more of the language than of the person.
WORD_LIKE_RATIO = 8.0
MIN_SURNAME_LENGTH = 4


def is_list_context(window):
    return (len(_NAMEPAIR.findall(window)) >= 3
            or window.count(";") >= 3
            or bool(_HEADER.search(window)))


def classify_mentions(text, pattern, window=WINDOW):
    """Return (descriptive, listed) counts for one document.

    Biased towards calling a mention list context: in a short forwarded message
    a single header marker falls inside every window. Undercounting descriptive
    mentions is the safe direction, since the cost is passing over a usable
    subject rather than scoring an unusable one.
    """
    descriptive = listed = 0
    for match in pattern.finditer(text or ""):
        chunk = text[max(0, match.start() - window):match.end() + window]
        if is_list_context(chunk):
            listed += 1
        else:
            descriptive += 1
    return descriptive, listed


def describes_subject(text, pattern, window=WINDOW):
    return classify_mentions(text, pattern, window)[0] > 0


def near_duplicate_key(text, prefix=300):
    """Announcements recirculate verbatim; forty copies are one document."""
    return hashlib.sha1(" ".join((text or "").split())[:prefix].encode()).hexdigest()


def surname_of(entry):
    for variant in entry["attributes"]["display_name_variants"]:
        value = " ".join(variant.split())
        if "," in value:
            return value.split(",")[0].strip()
        parts = value.split()
        if len(parts) > 1:
            return parts[-1]
    return ""


def given_name_of(entry):
    for variant in entry["attributes"]["display_name_variants"]:
        value = " ".join(variant.split())
        if "," in value:
            rest = value.split(",", 1)[1].strip()
            if rest:
                return rest.split()[0]
        else:
            parts = value.split()
            if len(parts) > 1:
                return parts[0]
    return ""


def word_likeness(surname_hits, full_name_hits):
    """How much more common the surname is than the person's full name."""
    return surname_hits / max(full_name_hits, 1)


def is_word_like(surname_hits, full_name_hits, threshold=WORD_LIKE_RATIO):
    return word_likeness(surname_hits, full_name_hits) >= threshold


def _count(client, index, phrase):
    return client.count(index=index, body={
        "query": {"match_phrase": {"message": phrase}}})["count"]


def screen(client, entry, index=SOURCE_INDEX, threshold=WORD_LIKE_RATIO):
    """Reject a subject whose surname belongs to the language.

    Returns (ok, detail). Measured: `love` outnumbers `Phillip Love` 94 to 1.
    """
    surname, given = surname_of(entry), given_name_of(entry)
    if len(surname) < MIN_SURNAME_LENGTH:
        return False, {"reason": "surname too short to screen", "surname": surname}
    surname_hits = _count(client, index, surname)
    full_hits = _count(client, index, f"{given} {surname}") if given else 0
    ratio = word_likeness(surname_hits, full_hits)
    ok = ratio < threshold
    return ok, {
        "surname": surname,
        "surname_hits": surname_hits,
        "full_name_hits": full_hits,
        "ratio": round(ratio, 1),
        "threshold": threshold,
        "reason": "" if ok else (
            f"'{surname}' appears {ratio:.0f}x more often than the full name, so it "
            f"is an ordinary word or widely shared. Masking it would delete the word "
            f"from the corpus and labelling on it would mark unrelated documents."
        ),
    }


def profile_of(client, entry, index=SOURCE_INDEX, sample=DEFAULT_SAMPLE,
               threshold=WORD_LIKE_RATIO):
    """Score one candidate on descriptive-mention density."""
    ok, detail = screen(client, entry, index=index, threshold=threshold)
    if not ok:
        return {"id": entry["id"], "screened_out": True, **detail}

    surname = detail["surname"]
    hits = client.search(index=index, body={
        "size": sample,
        "query": {"match_phrase": {"message": surname}},
        "_source": ["message"],
    })
    pattern = re.compile(re.escape(surname), re.I)
    descriptive = listed = 0
    digests = set()
    for hit in hits["hits"]["hits"]:
        text = hit["_source"].get("message") or ""
        d, l = classify_mentions(text, pattern)
        if d:
            descriptive += 1
            digests.add(near_duplicate_key(text))
        elif l:
            listed += 1
    return {
        "id": entry["id"],
        "screened_out": False,
        **detail,
        "sampled": len(hits["hits"]["hits"]),
        "descriptive_documents": descriptive,
        "list_only_documents": listed,
        "distinct_descriptive": len(digests),
        "messages": entry["attributes"]["message_count"],
    }


def rank(client, entries, index=SOURCE_INDEX, candidates=80, sample=DEFAULT_SAMPLE,
         threshold=WORD_LIKE_RATIO, min_messages=20):
    """Rank usable roster entries by distinct descriptive mentions."""
    pool = [e for e in entries
            if e["attributes"]["internal"]
            and e["attributes"]["display_name_variants"]
            and e["attributes"]["message_count"] >= min_messages]
    pool.sort(key=lambda e: -e["attributes"]["message_count"])

    scored, rejected = [], []
    for entry in pool[:candidates]:
        row = profile_of(client, entry, index=index, sample=sample, threshold=threshold)
        (rejected if row.get("screened_out") else scored).append(row)
    scored.sort(key=lambda r: -r["distinct_descriptive"])
    return {
        "pool_size": len(pool),
        "considered": min(candidates, len(pool)),
        "ranked": scored,
        "screened_out": rejected,
    }
