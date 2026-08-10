"""Unit tests for roster extraction (no cluster required).

Run:  uv run --with pytest pytest tests/ -q
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from lib import roster  # noqa: E402


def _doc(sender, to=None, cc=None, ts="2001-05-01T12:00:00+00:00", custodian="allen-p"):
    return {"from": sender, "to": to or [], "cc": cc or [],
            "@timestamp": ts, "custodian": custodian}


def test_parse_addresses_plain_and_display():
    assert roster.parse_addresses("phillip.allen@enron.com") == [("", "phillip.allen@enron.com")]
    assert roster.parse_addresses("Phillip K Allen <Phillip.Allen@ENRON.com>") == [
        ("Phillip K Allen", "phillip.allen@enron.com")
    ]


def test_parse_addresses_rejoins_comma_split_display_name():
    """seed_enron splits `to` on commas, which cuts `"Allen, Phillip K" <addr>` in two."""
    split_by_seed_enron = ['"Allen', 'Phillip K" <phillip.allen@enron.com>']
    assert roster.parse_addresses(split_by_seed_enron) == [
        ("Allen, Phillip K", "phillip.allen@enron.com")
    ]


def test_parse_addresses_multiple_recipients():
    parsed = roster.parse_addresses(["a@enron.com", "B User <b@enron.com>"])
    assert parsed == [("", "a@enron.com"), ("B User", "b@enron.com")]


def test_parse_addresses_drops_display_name_equal_to_address():
    assert roster.parse_addresses('"a@enron.com" <a@enron.com>') == [("", "a@enron.com")]


def test_parse_addresses_recovers_from_malformed_headers():
    """An unquoted address as its own display name makes getaddresses return ('', '')."""
    assert roster.parse_addresses("a@enron.com <a@enron.com>") == [("", "a@enron.com")]
    assert roster.parse_addresses("a@enron.com; b@enron.com") == [
        ("", "a@enron.com"), ("", "b@enron.com")
    ]


def test_parse_addresses_skips_entries_without_an_address():
    assert roster.parse_addresses("undisclosed-recipients") == []
    assert roster.parse_addresses(None) == []


def test_accumulate_counts_sent_received_and_window():
    docs = [
        _doc("A One <a@enron.com>", to=["b@enron.com"], ts="2001-01-01T00:00:00+00:00"),
        _doc("b@enron.com", to=["A One <a@enron.com>"], ts="2001-06-01T00:00:00+00:00"),
    ]
    people = roster.accumulate(docs)
    a = people["a@enron.com"]
    assert a.sent == 1 and a.received == 1
    assert a.names == {"A One": 2}
    assert a.first.year == 2001 and a.first.month == 1
    assert a.last.month == 6
    assert people["b@enron.com"].correspondents == {"a@enron.com": 2}


def test_accumulate_counts_a_self_addressed_message_once():
    people = roster.accumulate([_doc("a@enron.com", to=["a@enron.com"], cc=["a@enron.com"])])
    a = people["a@enron.com"]
    assert (a.sent, a.received) == (1, 0)
    assert a.correspondents == {}


def test_accumulate_tolerates_missing_timestamp():
    people = roster.accumulate([_doc("a@enron.com", to=["b@enron.com"], ts=None)])
    assert people["a@enron.com"].first is None


def test_accumulate_mixes_naive_and_offset_timestamps_without_raising():
    docs = [
        _doc("a@enron.com", ts="2001-01-01T00:00:00"),
        _doc("a@enron.com", ts="2001-06-01T00:00:00+00:00"),
    ]
    people = roster.accumulate(docs)
    assert people["a@enron.com"].first.month == 1
    assert people["a@enron.com"].last.month == 6


def test_entries_match_the_adapter_shape():
    people = roster.accumulate([_doc("A One <a@enron.com>", to=["b@ext.example"])])
    entry = roster.to_entries(people)[0]
    assert set(entry) == {"id", "identifiers", "attributes", "active_from", "active_to"}
    assert entry["id"] == "a@enron.com"
    assert entry["identifiers"] == ["a@enron.com", "A One"]
    assert entry["attributes"]["internal"] is True
    assert entry["attributes"]["message_count"] == 1
    assert entry["active_from"] == "2001-05-01T12:00:00+00:00"


def test_entries_rank_name_variants_by_frequency():
    docs = [_doc("Common Name <a@enron.com>") for _ in range(3)]
    docs.append(_doc("Rare Variant <a@enron.com>"))
    entry = roster.to_entries(roster.accumulate(docs))[0]
    assert entry["attributes"]["display_name_variants"] == ["Common Name", "Rare Variant"]


def test_external_addresses_are_not_internal():
    people = roster.accumulate([_doc("x@partner.example", to=["a@enron.com"])])
    entries = {e["id"]: e for e in roster.to_entries(people)}
    assert entries["x@partner.example"]["attributes"]["internal"] is False
    assert entries["x@partner.example"]["attributes"]["domain"] == "partner.example"


def _entry(addr, messages, named=True, windowed=True):
    return {
        "id": addr,
        "identifiers": [addr],
        "attributes": {
            "address": addr,
            "display_name_variants": ["A Name"] if named else [],
            "domain": addr.split("@")[1],
            "internal": addr.endswith("@enron.com"),
            "message_count": messages,
            "sent_count": messages,
            "received_count": 0,
            "custodians": [],
            "distinct_correspondents": 0,
            "top_correspondents": [],
        },
        "active_from": "2001-01-01T00:00:00+00:00" if windowed else None,
        "active_to": "2001-12-31T00:00:00+00:00" if windowed else None,
    }


def test_usable_subjects_requires_name_window_and_volume():
    entries = [
        _entry("ok@enron.com", 50),
        _entry("quiet@enron.com", 3),
        _entry("nameless@enron.com", 50, named=False),
        _entry("undated@enron.com", 50, windowed=False),
    ]
    usable = roster.usable_subjects(entries, min_messages=20)
    assert [e["id"] for e in usable] == ["ok@enron.com"]


def test_coverage_reports_aggregates_only():
    entries = [_entry("a@enron.com", 50), _entry("b@ext.example", 1, named=False)]
    cov = roster.coverage(entries, documents_scanned=51, min_messages=20)
    assert cov["distinct_addresses"] == 2
    assert cov["with_display_name"] == {"count": 1, "percent": 50.0}
    assert cov["internal_addresses"]["count"] == 1
    assert cov["message_count_distribution"]["max"] == 50
    assert cov["message_count_distribution"]["histogram"]["50+"] == 1
    assert cov["usable_subjects"]["count"] == 1
    serialized = str(cov)
    assert "a@enron.com" not in serialized and "A Name" not in serialized


def test_go_no_go_go_path_states_the_attribute_ceiling():
    cov = roster.coverage([_entry(f"p{i}@enron.com", 50) for i in range(6)], 300)
    decision = roster.go_no_go(cov, min_subjects=5)
    assert decision["decision"] == "go"
    assert decision["usable_subject_count"] == 6
    assert "no job titles" in decision["reasoning"]


def test_go_no_go_no_go_path_suggests_more_data_before_lower_thresholds():
    cov = roster.coverage([_entry("only@enron.com", 50)], 50)
    decision = roster.go_no_go(cov, min_subjects=5)
    assert decision["decision"] == "no_go"
    assert "seed-enron" in decision["reasoning"]


def test_percentiles_on_a_known_series():
    values = list(range(1, 11))
    assert roster._percentile(values, 50) == 5
    assert roster._percentile(values, 90) == 9
    assert roster._percentile([], 50) == 0


class _FakeClient:
    """Minimal scroll-capable stand-in; records what the scan asked for."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.cleared = []
        self.search_body = None

    def _page(self):
        hits = self.pages.pop(0) if self.pages else []
        return {"_scroll_id": "s1", "hits": {"hits": [{"_source": h} for h in hits]}}

    def search(self, index, scroll, body):
        self.search_body = body
        return self._page()

    def scroll(self, body):
        return self._page()

    def clear_scroll(self, body):
        self.cleared.append(body)


def test_scan_headers_pages_until_empty_and_clears_the_scroll():
    client = _FakeClient([[{"from": "a@enron.com"}], [{"from": "b@enron.com"}], []])
    sources = list(roster.scan_headers(client, index="mail-enron"))
    assert [s["from"] for s in sources] == ["a@enron.com", "b@enron.com"]
    assert client.cleared == [{"scroll_id": ["s1"]}]


def test_scan_headers_requests_headers_only():
    client = _FakeClient([[]])
    list(roster.scan_headers(client))
    assert "message" not in client.search_body["_source"]
    assert set(client.search_body["_source"]) == set(roster.DEFAULT_SCAN_FIELDS)


def test_build_returns_entries_coverage_and_decision():
    client = _FakeClient([
        [_doc("A One <a@enron.com>", to=["B Two <b@enron.com>"]) for _ in range(3)],
        [],
    ])
    entries, cov, decision = roster.build(client, min_messages=1, min_subjects=2)
    assert {e["id"] for e in entries} == {"a@enron.com", "b@enron.com"}
    assert cov["documents_scanned"] == 3
    assert decision["decision"] == "go"


def test_write_roster_keeps_people_out_of_the_return_value(tmp_path):
    entries = [_entry("a@enron.com", 50)]
    cov = roster.coverage(entries, 50)
    path = roster.write_roster(entries, cov, roster.go_no_go(cov),
                               path=str(tmp_path / "roster.json"))
    assert os.path.exists(path)
    assert path == str(tmp_path / "roster.json")
