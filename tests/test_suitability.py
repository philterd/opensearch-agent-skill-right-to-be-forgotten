"""Unit tests for the corpus-suitability check (no cluster required).

Run:  uv run --with pytest pytest tests/ -q
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from lib import suitability  # noqa: E402


# --- detecting a description ------------------------------------------------- #

def test_role_descriptions_are_detected_across_domains():
    for text in ("the senior frontend engineer who owned Checkout",
                 "the treating physician noted improvement overnight",
                 "the defendant drove north after leaving the bar",
                 "our on-call was paged at 02:14",
                 "whoever approved the deployment should comment",
                 "the person responsible for the region"):
        prose, listed = suitability.classify(text)
        assert prose == 1, text


def test_text_about_systems_is_not_a_description():
    for text in ("Cron job reindex-billing finished in 9m43s.",
                 "PaymentGateway returned 503; retries exhausted.",
                 "Nightly backup for orders completed in 4m40s."):
        assert suitability.classify(text) == (0, 0), text


def test_a_recipient_list_counts_as_list_context():
    text = ("To: Grigsby, Mike; Neal, Scott; Arora, Harry; Ermis, Frank "
            "please forward to the analyst who owns this")
    prose, listed = suitability.classify(text)
    assert listed >= 1 and prose == 0


def test_a_forwarded_header_block_is_list_context():
    assert suitability.is_list_context("-----Original Message----- From: a@b.com")
    assert not suitability.is_list_context("the engineer who deployed the fix")


# --- naming channel detection ------------------------------------------------ #

def test_identity_looking_fields_are_found():
    props = {"message": {"type": "text"}, "user.id": {"type": "keyword"},
             "assignee": {"type": "keyword"}, "latency_ms": {"type": "long"},
             "created_by": {"type": "keyword"}}
    fields = [f["field"] for f in suitability.naming_channel(props, "message")]
    assert fields == ["assignee", "created_by", "user.id"]


def test_an_unindexed_field_is_reported_but_not_searchable():
    """seed_enron maps the X- headers index:false; querying one is a 400."""
    props = {"message": {"type": "text"},
             "x_to": {"type": "keyword", "index": False},
             "to": {"type": "keyword"}}
    by_field = {f["field"]: f for f in suitability.naming_channel(props, "message")}
    assert by_field["x_to"]["searchable"] is False
    assert by_field["to"]["searchable"] is True


def test_the_searched_field_is_never_a_naming_channel():
    props = {"from": {"type": "text"}}
    assert suitability.naming_channel(props, "from") == []


def test_no_identity_fields_yields_nothing():
    props = {"message": {"type": "text"}, "level": {"type": "keyword"},
             "duration": {"type": "float"}}
    assert suitability.naming_channel(props, "message") == []


def test_missing_mapping_does_not_raise():
    assert suitability.naming_channel(None, "message") == []


# --- the verdict ------------------------------------------------------------- #

def test_the_bands_match_the_corpora_they_were_calibrated_on():
    """Court opinions 21 refs/doc, Enron 0.20, demo logs 0.03."""
    assert suitability.verdict(21.0, 0.9, True, 987)["material_for_indirect_pass"] == "rich"
    assert suitability.verdict(0.20, 0.9, True, 144)["material_for_indirect_pass"] == "sparse"
    assert suitability.verdict(0.03, 1.0, False, 15)["material_for_indirect_pass"] == "absent"


def test_a_sparse_band_points_at_the_documents_that_do_carry_one():
    out = suitability.verdict(0.20, 0.9, True, 144)
    assert "144 documents" in " ".join(out["reasons"])


def test_a_corpus_of_recipient_lists_is_called_out():
    out = suitability.verdict(21.0, 0.05, True, 900)
    assert any("recipient lists" in r for r in out["reasons"])


def test_every_verdict_warns_that_a_role_noun_is_not_a_description():
    for density in (21.0, 0.2, 0.01):
        out = suitability.verdict(density, 0.9, True, 10)
        assert any("not a description" in r for r in out["reasons"])


def test_a_missing_naming_channel_warns_without_changing_the_band():
    out = suitability.verdict(21.0, 0.8, False, 900)
    assert out["material_for_indirect_pass"] == "rich"
    assert any("cannot measure it" in r for r in out["reasons"])


# --- end to end against a stub ----------------------------------------------- #

class _FakeClient:
    def __init__(self, texts, props):
        self.texts, self.props = texts, props

    def search(self, index, body):
        return {"hits": {"hits": [{"_source": {"message": t}} for t in self.texts]}}

    class _Indices:
        def __init__(self, props):
            self.props = props

        def get_mapping(self, index):
            return {index: {"mappings": {"properties": self.props}}}

    @property
    def indices(self):
        return self._Indices(self.props)


def test_assess_reports_a_usable_corpus():
    texts = ["the defendant drove north after leaving the bar at 11pm"] * 8 + \
            ["Cron job finished in 9m43s."] * 2
    client = _FakeClient(texts, {"message": {"type": "text"},
                                 "party_surnames": {"type": "keyword"}})
    out = suitability.assess(client, "case-law")
    assert out["documents_sampled"] == 10
    assert out["describing_a_person"]["percent"] == 80.0
    assert out["references_per_document"] >= 0.8
    assert out["naming_channel_candidates"][0]["field"] == "party_surnames"


def test_assess_reports_an_unusable_corpus():
    texts = ["Cron job reindex-billing finished in 9m43s."] * 99 + \
            ["the engineer who deployed it"]
    client = _FakeClient(texts, {"message": {"type": "text"}})
    out = suitability.assess(client, "logs")
    assert out["describing_a_person"]["percent"] == 1.0
    assert out["verdict"]["material_for_indirect_pass"] == "absent"


def test_assess_skips_empty_documents():
    client = _FakeClient(["", "   ", "the analyst who filed it"], {"message": {"type": "text"}})
    assert suitability.assess(client, "x")["documents_sampled"] == 1


# --- how to read an indirect result ------------------------------------------ #

def test_an_empty_result_on_a_thin_corpus_must_be_explained():
    note = suitability.applicability_note("absent", candidate_count=0)
    assert "property of the corpus" in note
    assert "No candidates were returned" in note


def test_an_empty_result_on_a_rich_corpus_needs_no_excuse():
    note = suitability.applicability_note("rich", candidate_count=0)
    assert "about the subject" in note
    assert "No candidates were returned" not in note


def test_a_sparse_corpus_names_the_ceiling():
    assert "ceiling" in suitability.applicability_note("sparse", candidate_count=12)


def test_every_band_has_a_note():
    for band in ("rich", "sparse", "absent"):
        assert suitability.applicability_note(band, 5)
