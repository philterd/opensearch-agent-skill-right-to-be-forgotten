"""Roster extraction from the Enron corpus headers.

Stage one of the evaluation method in EVALUATION.md. The method labels from the
structured channel that names people and evaluates on the unstructured channel
that describes them. This module builds the naming-channel side: who appears in
the corpus, under what name variants, and when.

Nothing here reads `message`. Only `from`, `to`, `cc`, `custodian`, and
`@timestamp` are scanned, so the field discovery searches stays held out.

Header mining is research scaffolding, not the production path. A real
deployment supplies a roster from a system the organization already operates (an
HR export, a SCIM pull, an on-call schedule, CODEOWNERS history). Enron gets
mined only because there is no such system to ask.
"""

import datetime
import json
import os
import re
from email.utils import getaddresses

_ADDRESS_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

ENRON_INDEX = "mail-enron"
INTERNAL_DOMAIN = "enron.com"

ROSTER_PATH = os.path.join("gdpr-eval", "enron-roster.json")

DEFAULT_SCAN_FIELDS = ("from", "to", "cc", "custodian", "@timestamp")

# A subject needs enough surviving context to be findable after masking, a name
# variant to mask in the first place, and a window to place them in.
DEFAULT_MIN_MESSAGES = 20
DEFAULT_MIN_SUBJECTS = 5
DEFAULT_TOP_CORRESPONDENTS = 5


def _parse_timestamp(value):
    if not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    # Comparing a naive datetime against an aware one raises, and one undated
    # message would then abort the whole scan. Assume UTC when the offset is
    # missing, matching how seed_enron normalizes.
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


def parse_addresses(value):
    """Return [(display_name, email)] from a header value or a list of them.

    seed_enron splits `to`/`cc` on commas, which breaks the `"Allen, Phillip K"
    <addr>` form Enron uses constantly: one recipient arrives as two list
    entries. Rejoining with ", " restores the original header text (whitespace
    was already collapsed at index time), and getaddresses honours the quoting
    that the naive split ignored.
    """
    if not value:
        return []
    if isinstance(value, str):
        joined = value
    else:
        joined = ", ".join(v for v in value if v)
    out = []
    for display, addr in getaddresses([joined]):
        addr = addr.strip().lower()
        if "@" not in addr:
            continue
        display = " ".join((display or "").split()).strip("'\"")
        # A display name identical to the address carries no extra variant.
        if display.lower() == addr:
            display = ""
        out.append((display, addr))

    # getaddresses returns ('', '') for malformed forms real mail is full of,
    # such as an unquoted address used as its own display name. Dropping those
    # would undercount addresses and skew the coverage report, so recover any
    # address the strict parse missed.
    if not out and "@" in joined:
        seen = set()
        for addr in _ADDRESS_RE.findall(joined):
            addr = addr.lower()
            if addr not in seen:
                seen.add(addr)
                out.append(("", addr))
    return out


class _Person:
    __slots__ = ("address", "names", "sent", "received", "custodians",
                 "correspondents", "first", "last")

    def __init__(self, address):
        self.address = address
        self.names = {}          # variant -> times observed
        self.sent = 0
        self.received = 0
        self.custodians = set()
        self.correspondents = {}  # address -> times co-occurring
        self.first = None
        self.last = None

    def observe(self, display, timestamp, custodian):
        """Record everything that can repeat within a single message."""
        if display:
            self.names[display] = self.names.get(display, 0) + 1
        if custodian:
            self.custodians.add(custodian)
        if timestamp is not None:
            if self.first is None or timestamp < self.first:
                self.first = timestamp
            if self.last is None or timestamp > self.last:
                self.last = timestamp


def accumulate(documents):
    """Fold header dicts into {address: _Person}.

    ``documents`` yields the `_source` of each message. Only header fields are
    read.
    """
    people = {}

    def person(addr):
        if addr not in people:
            people[addr] = _Person(addr)
        return people[addr]

    for doc in documents:
        timestamp = _parse_timestamp(doc.get("@timestamp"))
        custodian = doc.get("custodian") or ""
        senders = parse_addresses(doc.get("from"))
        recipients = parse_addresses(doc.get("to")) + parse_addresses(doc.get("cc"))

        # Roles are per message, not per header occurrence: people cc their own
        # address constantly, and counting that twice inflates their volume.
        roles = {}
        for display, addr in senders:
            person(addr).observe(display, timestamp, custodian)
            roles[addr] = "sent"
        for display, addr in recipients:
            person(addr).observe(display, timestamp, custodian)
            roles.setdefault(addr, "received")

        for addr, role in roles.items():
            if role == "sent":
                people[addr].sent += 1
            else:
                people[addr].received += 1

        unique = set(roles)
        for addr in unique:
            counts = people[addr].correspondents
            for other in unique:
                if other != addr:
                    counts[other] = counts.get(other, 0) + 1

    return people


def to_entries(people, top_correspondents=DEFAULT_TOP_CORRESPONDENTS):
    """Render accumulated people as the adapter shape from EVALUATION.md."""
    entries = []
    for addr, p in sorted(people.items()):
        variants = [n for n, _ in sorted(p.names.items(), key=lambda kv: (-kv[1], kv[0]))]
        top = sorted(p.correspondents.items(), key=lambda kv: (-kv[1], kv[0]))
        entries.append({
            "id": addr,
            "identifiers": [addr] + variants,
            "attributes": {
                "address": addr,
                "display_name_variants": variants,
                "domain": addr.split("@", 1)[1] if "@" in addr else "",
                "internal": addr.endswith("@" + INTERNAL_DOMAIN),
                "message_count": p.sent + p.received,
                "sent_count": p.sent,
                "received_count": p.received,
                "custodians": sorted(p.custodians),
                "distinct_correspondents": len(p.correspondents),
                "top_correspondents": [
                    {"address": a, "messages": n} for a, n in top[:top_correspondents]
                ],
            },
            "active_from": p.first.isoformat() if p.first else None,
            "active_to": p.last.isoformat() if p.last else None,
        })
    return entries


def _percentile(values, pct):
    """Nearest-rank percentile over a pre-sorted list."""
    if not values:
        return 0
    rank = max(1, min(len(values), round(pct / 100 * len(values))))
    return values[rank - 1]


_HISTOGRAM_BUCKETS = ((1, 1), (2, 4), (5, 9), (10, 49), (50, None))


def _histogram(counts):
    out = {}
    for low, high in _HISTOGRAM_BUCKETS:
        label = f"{low}" if high == low else (f"{low}+" if high is None else f"{low}-{high}")
        out[label] = sum(1 for c in counts if c >= low and (high is None or c <= high))
    return out


def usable_subjects(entries, min_messages=DEFAULT_MIN_MESSAGES):
    """Entries with enough signal to mask and to describe.

    Requires a name variant (nothing to mask without one), an active window, and
    enough messages that residual context survives masking.
    """
    return [
        e for e in entries
        if e["attributes"]["display_name_variants"]
        and e["active_from"] and e["active_to"]
        and e["attributes"]["message_count"] >= min_messages
    ]


def coverage(entries, documents_scanned, min_messages=DEFAULT_MIN_MESSAGES):
    """Aggregate metrics only. No individual appears in this report."""
    counts = sorted(e["attributes"]["message_count"] for e in entries)
    total = len(entries)
    named = sum(1 for e in entries if e["attributes"]["display_name_variants"])
    windowed = sum(1 for e in entries if e["active_from"] and e["active_to"])
    internal = sum(1 for e in entries if e["attributes"]["internal"])

    def pct(n):
        return round(100.0 * n / total, 1) if total else 0.0

    return {
        "documents_scanned": documents_scanned,
        "distinct_addresses": total,
        "with_display_name": {"count": named, "percent": pct(named)},
        "with_active_window": {"count": windowed, "percent": pct(windowed)},
        "internal_addresses": {"count": internal, "percent": pct(internal)},
        "external_addresses": {"count": total - internal, "percent": pct(total - internal)},
        "message_count_distribution": {
            "min": counts[0] if counts else 0,
            "p25": _percentile(counts, 25),
            "median": _percentile(counts, 50),
            "p75": _percentile(counts, 75),
            "p90": _percentile(counts, 90),
            "max": counts[-1] if counts else 0,
            "mean": round(sum(counts) / total, 2) if total else 0.0,
            "histogram": _histogram(counts),
        },
        "usable_subjects": {
            "count": len(usable_subjects(entries, min_messages)),
            "criteria": {
                "min_messages": min_messages,
                "requires_display_name": True,
                "requires_active_window": True,
            },
        },
    }


# The ceiling is structural, not a sampling artifact: Enron headers carry no job
# titles, so no larger subset changes which *kinds* of attribute are available.
_ATTRIBUTE_CEILING = (
    "Headers carry no job titles, teams, or seniority, so generated profiles can "
    "only describe a person relationally and temporally (who they corresponded "
    "with, over what window, in whose mailbox). That is a weaker test than the "
    "role-and-incident descriptions the skill's claim is about, and no larger "
    "sample changes it. Treat a recall figure from this corpus as evidence about "
    "relational description only."
)


def go_no_go(cov, min_subjects=DEFAULT_MIN_SUBJECTS):
    """Record whether the available attributes can support stage two.

    A judgment, computed from stated thresholds so it can be contested by
    attacking a threshold rather than the conclusion.
    """
    count = cov["usable_subjects"]["count"]
    min_messages = cov["usable_subjects"]["criteria"]["min_messages"]
    go = count >= min_subjects

    if go:
        reasoning = (
            f"{count} addresses carry a display name, an active window, and at least "
            f"{min_messages} messages, meeting the {min_subjects}-subject minimum for "
            f"stage two. " + _ATTRIBUTE_CEILING
        )
    else:
        named = cov["with_display_name"]["count"]
        reasoning = (
            f"Only {count} addresses meet all three criteria (display name, active "
            f"window, at least {min_messages} messages) against a {min_subjects}-subject "
            f"minimum, out of {cov['distinct_addresses']} distinct addresses of which "
            f"{named} carry any display name at all. Raise --limit on seed-enron and "
            f"re-run before lowering the thresholds: a larger corpus adds subjects, "
            f"whereas a lower bar admits subjects with too little residual context to "
            f"measure. " + _ATTRIBUTE_CEILING
        )

    return {
        "decision": "go" if go else "no_go",
        "usable_subject_count": count,
        "min_subjects": min_subjects,
        "reasoning": reasoning,
    }


def write_roster(entries, coverage_report, decision, path=ROSTER_PATH):
    """Write the roster to disk and return a pointer to it.

    The roster names real people, so it never goes to stdout: this output lands
    in the context of the agent working the corpus. Same discipline as
    seed-demo's answer key.
    """
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({
            "coverage": coverage_report,
            "decision": decision,
            "entries": entries,
        }, fh, indent=2)
    return path


def scan_headers(client, index=ENRON_INDEX, page_size=1000, scroll="2m"):
    """Yield header-only `_source` dicts for every document in ``index``.

    Scroll rather than search_after: no deterministic tiebreaker field exists
    here, and the evaluation harness runs against a local corpus.
    """
    resp = client.search(
        index=index,
        scroll=scroll,
        body={
            "size": page_size,
            "query": {"match_all": {}},
            "_source": list(DEFAULT_SCAN_FIELDS),
        },
    )
    scroll_id = resp.get("_scroll_id")
    try:
        while True:
            hits = resp.get("hits", {}).get("hits", [])
            if not hits:
                return
            for hit in hits:
                yield hit.get("_source", {})
            if scroll_id is None:
                return
            resp = client.scroll(body={"scroll_id": scroll_id, "scroll": scroll})
            scroll_id = resp.get("_scroll_id")
    finally:
        if scroll_id:
            try:
                client.clear_scroll(body={"scroll_id": [scroll_id]})
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass


def build(client, index=ENRON_INDEX, min_messages=DEFAULT_MIN_MESSAGES,
          min_subjects=DEFAULT_MIN_SUBJECTS,
          top_correspondents=DEFAULT_TOP_CORRESPONDENTS, page_size=1000):
    """Scan ``index`` and return (entries, coverage, decision)."""
    scanned = 0

    def counting():
        nonlocal scanned
        for source in scan_headers(client, index=index, page_size=page_size):
            scanned += 1
            yield source

    people = accumulate(counting())
    entries = to_entries(people, top_correspondents=top_correspondents)
    cov = coverage(entries, scanned, min_messages=min_messages)
    return entries, cov, go_no_go(cov, min_subjects=min_subjects)
