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


def test_alias_set_prefers_observed_exchange_logins():
    entry = _entry("lynn.blair@enron.com", ["Blair, Lynn"])
    entry["attributes"]["exchange_logins"] = ["lblair"]
    aliases = masking.alias_set([entry], "lynn.blair@enron.com")
    assert "lblair" in aliases["login_variants"]        # observed
    assert "lynn.blair" in aliases["login_variants"]    # generated fallback


# --- mask broadly, label narrowly ------------------------------------------- #

# Two people share the given name "Phillip"; only one is an Allen.
SHARED = ENTRIES + [_entry("phillip.love@enron.com", ["Phillip Love"])]


def test_variant_owners_records_every_person_producing_a_variant():
    owners = masking.variant_owners(SHARED)
    assert owners["phillip"] == {"phillip.allen@enron.com", "k..allen@enron.com",
                                 "phillip.love@enron.com"}
    assert owners["allen"] == {"phillip.allen@enron.com", "k..allen@enron.com"}


def test_a_shared_given_name_is_masked_but_never_labelled():
    aliases = masking.alias_set(SHARED, "phillip.allen@enron.com")
    assert "Phillip" in aliases["variants"]            # still masked
    assert "Phillip" in aliases["ambiguous_variants"]  # but not a label
    assert "Phillip" not in aliases["label_variants"]
    assert "Allen" in aliases["label_variants"]
    assert "Phillip K Allen" in aliases["label_variants"]
    assert "phillip.allen@enron.com" in aliases["label_variants"]


def test_an_unshared_name_stays_in_both_sets():
    aliases = masking.alias_set(SHARED, "someone.else@enron.com")
    assert aliases["ambiguous_variants"] == []
    assert aliases["label_variants"] == aliases["variants"]


def test_labelling_ignores_documents_that_only_share_the_given_name():
    aliases = masking.alias_set(SHARED, "phillip.allen@enron.com")
    client = _FakeClient([
        ("allen-p/sent/1", {"message": "Phillip Allen signed it.", "@timestamp": "t1"}),
        ("allen-p/sent/2", {"message": "Phillip will handle it.", "@timestamp": "t2"}),
    ])
    positives, stats = corpus.build_masked_corpus(client, aliases, setup_neural=False)

    # Both documents are masked, only the discriminative one is a positive.
    assert stats["documents_masked"] == 2
    assert [p["original_id"] for p in positives] == ["allen-p/sent/1"]
    assert stats["label_variants"] < stats["mask_variants"]
    for source in client.indexed.values():
        assert "Phillip" not in source["message"]


# --- the gate fails closed on a thin alias set ------------------------------ #

def _nameless():
    """What the real corpus produced before the X- headers were indexed."""
    return masking.alias_set([_entry("lynn.blair@enron.com", [])], "lynn.blair@enron.com")


def test_alias_set_failures_flags_an_alias_set_with_no_name():
    aliases = _nameless()
    assert aliases["name_variants"] == []
    failures = leakage.alias_set_failures(aliases)
    assert len(failures) == 1 and "no name variants" in failures[0]


def test_alias_set_failures_accepts_a_named_subject():
    aliases = masking.alias_set(ENTRIES, "phillip.allen@enron.com")
    assert leakage.alias_set_failures(aliases) == []
    assert leakage.alias_set_failures(None) == []


def test_audit_fails_on_a_nameless_alias_set_even_with_clean_text():
    """The regression: this combination used to report passed=True."""
    aliases = _nameless()
    pattern = masking.build_pattern(aliases["variants"])
    docs = [_masked_doc("Lynn Blair approved the schedule.")]
    report = leakage.audit(docs, pattern, aliases=aliases)
    assert report["passed"] is False
    assert report["surviving_variants"]["distinct"] == 0  # nothing to look for
    assert "no name variants" in report["failures"][0]
    assert report["alias_set"]["name_variants"] == 0


def test_two_people_with_the_same_recorded_name_are_merged():
    """A known limit: the roster cannot tell them apart, so it treats them as one.

    Their shared variants then look unique to the merged subject, which is why
    this is a caveat rather than something the gate can catch.
    """
    twins = [_entry("a@enron.com", ["Chris Smith"]), _entry("b@enron.com", ["Chris Smith"])]
    aliases = masking.alias_set(twins, "a@enron.com")
    assert aliases["addresses"] == ["a@enron.com", "b@enron.com"]
    assert aliases["ambiguous_variants"] == []


def test_gate_fails_on_an_alias_set_with_nothing_unique_to_label_on():
    """Reachable through a hand-edited label file, which audit-mask reads back."""
    failures = leakage.alias_set_failures(
        {"subject": "a@enron.com", "name_variants": ["Chris"], "label_variants": []})
    assert any("unique to them" in f for f in failures)


def test_build_masked_corpus_refuses_a_nameless_alias_set():
    with pytest.raises(ValueError, match="no name variants"):
        corpus.build_masked_corpus(_FakeClient([]), _nameless(), setup_neural=False)


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
    assert masked == "write to now"


def test_masking_leaves_unrelated_text_alone(pattern):
    text = "Jane Roe approved the Callender contract in Allentown."
    masked, hits = masking.mask_text(text, pattern)
    assert masked == text and hits == 0


def test_masking_does_not_fire_inside_a_longer_word(pattern):
    masked, hits = masking.mask_text("The allenwrench and mcallen office", pattern)
    assert hits == 0 and masked == "The allenwrench and mcallen office"


def test_masking_reaches_a_name_wrapped_across_ascii_table_cells(pattern):
    """Forwarded mail wraps addresses mid-token, leaving `Allen@e| | |nron.com`.

    An `@` in the right-hand boundary treated that as address interior and left
    the surname in the masked corpus.
    """
    text = "-------> | | \"Phillip\"| | | <Phillip.Allen@e| | | nron.com> | | | 11/07"
    masked, _ = masking.mask_text(text, pattern)
    assert "Allen" not in masked and "Phillip" not in masked


def test_masking_still_consumes_a_whole_intact_address(pattern):
    masked, hits = masking.mask_text("phillip.allen@enron.com", pattern)
    assert masked == "" and hits == 1


def test_masking_leaves_no_marker_to_search_for(pattern):
    """A visible marker would appear in exactly the positives. See #6."""
    assert masking.MASK_REPLACEMENT == ""
    masked, hits = masking.mask_text("Phillip Allen  signed  it.", pattern)
    assert hits == 1
    # No marker, and no run of spaces standing in for one.
    assert masked == "signed it."


def test_masking_can_still_use_a_marker_when_asked(pattern):
    masked, _ = masking.mask_text("ask Allen", pattern, replacement="[X]")
    assert masked == "ask [X]"


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
    docs = [_masked_doc("[MASKED] sent the schedule.")]
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


def test_audit_reports_residual_name_substrings_without_failing(pattern):
    aliases = masking.alias_set(ENTRIES, "phillip.allen@enron.com")
    # "Allenby" is not a variant match, but the loose check still sees "Allen".
    report = leakage.audit([_masked_doc("Allenby Road")], pattern, aliases=aliases)
    assert report["passed"] is True
    assert report["surviving_variants"]["distinct"] == 0
    assert report["residual_name_substrings"]["documents"] == 1


def test_residual_reporting_is_quiet_on_a_genuinely_clean_corpus(pattern):
    aliases = masking.alias_set(ENTRIES, "phillip.allen@enron.com")
    report = leakage.audit([_masked_doc("[MASKED] sent it.")], pattern,
                           aliases=aliases)
    assert report["residual_name_substrings"]["documents"] == 0


def test_audit_fails_on_a_readable_document_id(pattern):
    docs = [("allen-p/sent/17", {"message": "[MASKED]"})]
    report = leakage.audit(docs, pattern)
    assert report["passed"] is False
    assert report["readable_document_ids"] == 1


def test_audit_fails_when_header_or_container_fields_are_carried(pattern):
    doc_id = masking.masked_doc_id("allen-p/sent/1")
    docs = [(doc_id, {"message": "[MASKED]", "custodian": "allen-p", "from": "x"})]
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


# --- the mask marker must not be the label set (#6) ------------------------- #

def test_audit_fails_when_the_marker_sits_only_in_the_positives(pattern):
    docs = [(masking.masked_doc_id(f"d/{i}"), {"message": "[MASKED] sent it."})
            for i in range(5)]
    report = leakage.audit(docs, pattern, marker="[MASKED]", positive_count=5)
    assert report["passed"] is False
    assert "returns the label set" in report["failures"][0]
    assert report["mask_marker"]["documents"] == 5


def test_audit_accepts_a_marker_diluted_well_beyond_the_positives(pattern):
    docs = [(masking.masked_doc_id(f"d/{i}"), {"message": "[MASKED] sent it."})
            for i in range(200)]
    report = leakage.audit(docs, pattern, marker="[MASKED]", positive_count=5)
    assert report["passed"] is True


def test_audit_has_nothing_to_check_when_nothing_is_left_behind(pattern):
    docs = [(masking.masked_doc_id("d/1"), {"message": "sent it."})]
    report = leakage.audit(docs, pattern, marker="", positive_count=1)
    assert report["passed"] is True
    assert report["mask_marker"]["documents"] == 0
    assert leakage.marker_failures("", 0, 1) == []


def test_marker_check_is_skipped_without_a_positive_count(pattern):
    """audit-mask on an older label file that predates the field."""
    assert leakage.marker_failures("[MASKED]", 5, None) == []


def test_the_erasure_workflow_marker_is_untouched():
    """#6 is about evaluation corpora; redaction still marks what it removed."""
    from lib.actions import REDACTION_TOKEN
    assert REDACTION_TOKEN == "[GDPR_REDACTED]"
    assert masking.MASK_REPLACEMENT != REDACTION_TOKEN


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
