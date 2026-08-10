"""Unit tests for alias sets, masking, and the leakage gate (no cluster required).

Run:  uv run --with pytest pytest tests/ -q
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from lib import corpus, leakage, masking  # noqa: E402


def _entry(address, variants):
    return {
        "id": address,
        "identifiers": [address] + variants,
        "attributes": {
            "address": address,
            "display_name_variants": variants,
            "domain": address.split("@")[1],
            "internal": address.endswith("@enron.com"),
            "message_count": 40,
            "sent_count": 20,
            "received_count": 20,
            "custodians": ["allen-p"],
            "distinct_correspondents": 3,
            "top_correspondents": [],
        },
        "active_from": "2001-01-01T00:00:00+00:00",
        "active_to": "2001-12-31T00:00:00+00:00",
    }


ENTRIES = [
    _entry("phillip.allen@enron.com", ["Phillip K Allen", "Allen, Phillip K"]),
    _entry("k..allen@enron.com", ["Phillip K Allen"]),
    _entry("someone.else@enron.com", ["Jane Roe"]),
]


# --- alias sets ------------------------------------------------------------ #

def test_alias_set_covers_every_form_the_criteria_name():
    aliases = masking.alias_set(ENTRIES, "phillip.allen@enron.com")
    variants = set(aliases["variants"])
    assert "Phillip K Allen" in variants          # full name
    assert "Allen" in variants                    # surname alone
    assert "Phillip" in variants                  # given name alone
    assert "P. Allen" in variants                 # initial + surname
    assert "P.K.A." in variants                   # initials
    assert "phillip.allen" in variants            # login form
    assert "pallen" in variants                   # generated login form
    assert "phillip.allen@enron.com" in variants  # every observed address
    assert "k..allen@enron.com" in variants


def test_alias_set_reassembles_a_person_from_several_addresses():
    aliases = masking.alias_set(ENTRIES, "phillip.allen@enron.com")
    assert aliases["addresses"] == ["k..allen@enron.com", "phillip.allen@enron.com"]


def test_alias_set_does_not_pull_in_unrelated_people():
    aliases = masking.alias_set(ENTRIES, "phillip.allen@enron.com")
    assert "someone.else@enron.com" not in aliases["variants"]
    assert "Roe" not in aliases["variants"]


def test_alias_set_normalizes_the_last_first_form():
    forms = masking._name_forms("Allen, Phillip K")
    assert "Phillip K Allen" in forms
    assert "Allen" in forms and "Phillip" in forms


def test_alias_set_drops_variants_below_the_minimum_length():
    aliases = masking.alias_set(ENTRIES, "phillip.allen@enron.com", min_length=3)
    assert all(len(v) >= 3 for v in aliases["variants"])
    assert "PKA" in aliases["variants"]  # three characters, kept
    aliases_long = masking.alias_set(ENTRIES, "phillip.allen@enron.com", min_length=6)
    assert "Allen" not in aliases_long["variants"]


def test_alias_set_for_an_unknown_subject_still_yields_the_address():
    aliases = masking.alias_set(ENTRIES, "ghost@enron.com")
    assert aliases["addresses"] == ["ghost@enron.com"]
    assert aliases["variants"] == ["ghost@enron.com"]


# --- masking --------------------------------------------------------------- #

@pytest.fixture
def pattern():
    return masking.build_pattern(masking.alias_set(ENTRIES, "phillip.allen@enron.com")["variants"])


def test_masking_is_case_insensitive(pattern):
    masked, hits = masking.mask_text("Spoke to PHILLIP ALLEN and phillip today.", pattern)
    assert "PHILLIP" not in masked and "phillip" not in masked
    # Two hits, not three: longest-first consumes "PHILLIP ALLEN" as one variant.
    assert hits == 2


def test_masking_reaches_inside_quoted_reply_blocks(pattern):
    text = ("Re: schedule. Sounds fine. -----Original Message----- From: Phillip K Allen "
            "Sent: Monday To: crew Subject: schedule Please ask Allen for the file.")
    masked, _ = masking.mask_text(text, pattern)
    assert "Phillip" not in masked and "Allen" not in masked
    assert "-----Original Message-----" in masked  # structure preserved


def test_masking_consumes_a_whole_address_not_just_its_local_part(pattern):
    masked, _ = masking.mask_text("write to phillip.allen@enron.com now", pattern)
    assert "@enron.com" not in masked
    assert masked == f"write to {masking.MASK_TOKEN} now"


def test_masking_leaves_unrelated_text_alone(pattern):
    text = "Jane Roe approved the Callender contract in Allentown."
    masked, hits = masking.mask_text(text, pattern)
    assert masked == text and hits == 0


def test_masking_does_not_fire_inside_a_longer_word(pattern):
    masked, hits = masking.mask_text("The allenwrench and mcallen office", pattern)
    assert hits == 0 and masked == "The allenwrench and mcallen office"


def test_build_pattern_returns_none_for_an_empty_alias_set():
    assert masking.build_pattern([]) is None
    assert masking.mask_text("text", None) == ("text", 0)


def test_masked_ids_are_opaque_and_stable():
    first = masking.masked_doc_id("allen-p/sent/17")
    assert first == masking.masked_doc_id("allen-p/sent/17")
    assert "allen" not in first
    assert masking.is_opaque_id(first)
    assert not masking.is_opaque_id("allen-p/sent/17")


# --- leakage audit --------------------------------------------------------- #

def _masked_doc(text):
    return masking.masked_doc_id("allen-p/sent/1"), {"message": text, "@timestamp": "t"}


def test_audit_passes_on_a_clean_masked_corpus(pattern):
    docs = [_masked_doc(f"{masking.MASK_TOKEN} sent the schedule.")]
    report = leakage.audit(docs, pattern)
    assert report["passed"] is True and report["failures"] == []
    assert report["documents_scanned"] == 1


def test_audit_fails_when_a_variant_survives(pattern):
    docs = [_masked_doc("Allen sent the schedule.")]
    report = leakage.audit(docs, pattern)
    assert report["passed"] is False
    assert report["surviving_variants"] == {"distinct": 1, "documents": 1}
    assert "survived masking" in report["failures"][0]


def test_audit_reports_counts_not_the_surviving_names(pattern):
    report = leakage.audit([_masked_doc("Phillip K Allen")], pattern)
    assert "Phillip" not in json.dumps(report)


def test_audit_fails_on_a_readable_document_id(pattern):
    docs = [("allen-p/sent/17", {"message": masking.MASK_TOKEN})]
    report = leakage.audit(docs, pattern)
    assert report["passed"] is False
    assert report["readable_document_ids"] == 1


def test_audit_fails_when_header_or_container_fields_are_carried(pattern):
    doc_id = masking.masked_doc_id("allen-p/sent/1")
    docs = [(doc_id, {"message": masking.MASK_TOKEN, "custodian": "allen-p", "from": "x"})]
    report = leakage.audit(docs, pattern)
    assert report["passed"] is False
    assert report["forbidden_fields_present"] == ["custodian", "from"]
    assert report["container_names"]["custodian_and_folder_carried"] is True


def test_phone_policy_identification_reports_but_does_not_fail(pattern):
    docs = [_masked_doc("Call me on 713-853-1234 or x4455.")]
    report = leakage.audit(docs, pattern, phone_policy="identification")
    assert report["passed"] is True
    assert report["phone_signals"]["occurrences"] == 2
    assert "true identification" in report["phone_signals"]["decision"]


def test_phone_policy_leakage_fails_the_run(pattern):
    docs = [_masked_doc("Call me on 713-853-1234.")]
    report = leakage.audit(docs, pattern, phone_policy="leakage")
    assert report["passed"] is False
    assert "phone" in report["failures"][0]


def test_phone_detection_ignores_short_digit_runs(pattern):
    assert leakage.count_phone_signals("meeting at 10 on the 4th, room 210") == 0


def test_audit_rejects_an_unknown_phone_policy(pattern):
    with pytest.raises(ValueError):
        leakage.audit([], pattern, phone_policy="ignore")


# --- the scoring guard ----------------------------------------------------- #

def _labels(**overrides):
    base = {"masked_index": "mail-enron-masked", "audit": {"passed": True, "failures": []}}
    base.update(overrides)
    return base


def test_assert_scorable_accepts_the_masked_index():
    assert leakage.assert_scorable(_labels(), "mail-enron-masked") is True


def test_assert_scorable_refuses_the_unmasked_index():
    with pytest.raises(ValueError, match="Refusing to score 'mail-enron'"):
        leakage.assert_scorable(_labels(), "mail-enron")


def test_assert_scorable_refuses_labels_from_a_failed_audit():
    labels = _labels(audit={"passed": False, "failures": ["2 variants survived."]})
    with pytest.raises(ValueError, match="did not pass"):
        leakage.assert_scorable(labels, "mail-enron-masked")


def test_assert_scorable_refuses_a_label_set_with_no_masked_index():
    with pytest.raises(ValueError, match="no masked_index"):
        leakage.assert_scorable({"audit": {"passed": True}}, "mail-enron-masked")


# --- corpus build ---------------------------------------------------------- #

class _FakeClient:
    """Scroll-capable stand-in that records the documents bulk-indexed."""

    def __init__(self, docs):
        self.pages = [[{"_id": i, "_source": s} for i, s in docs], []]
        self.indexed = {}
        self.deleted = []
        self.created = []
        self.refreshed = []

        outer = self

        class _Indices:
            def exists(self, index):
                return index in outer.created

            def delete(self, index):
                outer.deleted.append(index)

            def create(self, index, body):
                outer.created.append(index)

            def refresh(self, index):
                outer.refreshed.append(index)

        self.indices = _Indices()

    def search(self, index, scroll, body):
        return self._page()

    def scroll(self, body):
        return self._page()

    def _page(self):
        hits = self.pages.pop(0) if self.pages else []
        return {"_scroll_id": "s1", "hits": {"hits": hits}}

    def clear_scroll(self, body):
        pass

    def bulk(self, body, refresh=False):
        lines = body.strip().split("\n")
        for meta_line, doc_line in zip(lines[::2], lines[1::2]):
            meta = json.loads(meta_line)["index"]
            self.indexed[meta["_id"]] = json.loads(doc_line)
        return {"errors": False, "items": []}

    def get(self, index, id):
        raise RuntimeError("source lookup not stubbed")


def test_build_masked_corpus_masks_labels_and_drops_headers():
    aliases = masking.alias_set(ENTRIES, "phillip.allen@enron.com")
    client = _FakeClient([
        ("allen-p/sent/1", {"message": "Phillip K Allen approved it.", "@timestamp": "t1"}),
        ("allen-p/sent/2", {"message": "Nothing to see here.", "@timestamp": "t2"}),
    ])
    positives, stats = corpus.build_masked_corpus(client, aliases, setup_neural=False)

    assert stats["documents_scanned"] == 2
    assert stats["documents_masked"] == 1
    assert [p["original_id"] for p in positives] == ["allen-p/sent/1"]
    assert positives[0]["doc_id"] == masking.masked_doc_id("allen-p/sent/1")

    # Only masked text and timestamp are carried, under opaque ids.
    assert set(stats["fields_carried"]) == {"message", "@timestamp"}
    for doc_id, source in client.indexed.items():
        assert masking.is_opaque_id(doc_id)
        assert set(source) == {"message", "@timestamp"}
        assert "Allen" not in source["message"]
    assert client.refreshed == ["mail-enron-masked"]


def test_build_masked_corpus_refuses_an_empty_alias_set():
    empty = {"subject": "nobody@enron.com", "variants": []}
    with pytest.raises(ValueError, match="Alias set is empty"):
        corpus.build_masked_corpus(_FakeClient([]), empty, setup_neural=False)


def test_write_labels_records_the_index_and_assumptions(tmp_path):
    aliases = masking.alias_set(ENTRIES, "phillip.allen@enron.com")
    stats = {"masked_index": "mail-enron-masked", "source_index": "mail-enron",
             "fields_carried": ["message", "@timestamp"]}
    positives = [{"doc_id": "m-abc123abc123", "original_id": "allen-p/sent/1",
                  "variant_hits": 2}]
    path = corpus.write_labels(str(tmp_path / "labels.json"), aliases, positives, stats,
                               {"passed": True, "failures": []}, "identification")
    written = json.loads(open(path, encoding="utf-8").read())
    assert written["masked_index"] == "mail-enron-masked"
    assert written["positive_count"] == 1
    assert written["id_map"] == {"m-abc123abc123": "allen-p/sent/1"}
    assert "manufactures the indirect case" in written["assumptions"]
    # The written file is the answer key, so the guard must accept it.
    assert leakage.assert_scorable(written, "mail-enron-masked") is True
