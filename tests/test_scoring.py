"""Unit tests for scoring (no cluster required).

Run:  uv run --with pytest pytest tests/ -q
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from lib import scoring  # noqa: E402


def _positives(n):
    return [{"doc_id": f"m-{i:012x}", "original_id": f"c/f/{i}", "label_hits": 1}
            for i in range(n)]


# --- the derive/score split -------------------------------------------------- #

def test_split_is_deterministic_and_disjoint():
    pos = _positives(200)
    d1, s1 = scoring.split_positives(pos)
    d2, s2 = scoring.split_positives(pos)
    assert [p["doc_id"] for p in d1] == [p["doc_id"] for p in d2]
    assert set(p["doc_id"] for p in s1) == set(p["doc_id"] for p in s2)
    assert not (set(p["doc_id"] for p in d1) & set(p["doc_id"] for p in s1))
    assert len(d1) + len(s1) == 200


def test_split_is_roughly_even():
    d, s = scoring.split_positives(_positives(400))
    assert 150 < len(d) < 250 and 150 < len(s) < 250


# --- recall ------------------------------------------------------------------ #

def test_recall_at_k_counts_only_the_top_k():
    ranked = ["a", "b", "c", "d"]
    out = scoring.recall_at_k(ranked, {"c", "z"}, ks=(2, 4))
    assert out["recall@2"]["found"] == 0
    assert out["recall@4"] == {"found": 1, "of": 2, "percent": 50.0, "k_caps_recall": False}


def test_recall_flags_a_k_that_cannot_reach_full_recall():
    out = scoring.recall_at_k(["a"], {"a", "b", "c"}, ks=(2,))
    assert out["recall@2"]["k_caps_recall"] is True


def test_recall_with_no_positives_does_not_divide_by_zero():
    assert scoring.recall_at_k(["a"], set(), ks=(10,))["recall@10"]["percent"] == 0.0


# --- profile generation ------------------------------------------------------ #

def test_usable_terms_drops_the_subject_s_own_name():
    aliases = {"variants": ["monika.causholli@enron.com", "Causholli", "Monika"]}
    terms = ["pulp", "causholli", "foex", "monika", "enron", "norscan"]
    assert scoring.usable_terms(terms, aliases) == ["pulp", "foex", "norscan"]


def test_usable_terms_drops_very_short_tokens():
    assert scoring.usable_terms(["ab", "pulp"], {"variants": []}) == ["pulp"]


def test_build_profiles_produces_several_distinct_wordings():
    profiles = scoring.build_profiles(["pulp", "foex", "norscan", "pppc", "inventory"])
    assert len(profiles) == 3
    assert len({p["profile"] for p in profiles}) == 3
    assert all("pulp" in p["profile"] for p in profiles)
    assert all(p["keywords"].startswith("pulp foex") for p in profiles)


def test_build_profiles_copes_with_fewer_terms_than_slots():
    profiles = scoring.build_profiles(["pulp", "foex"])
    assert len(profiles) == 3 and all(p["profile"] for p in profiles)


def test_build_profiles_returns_nothing_without_terms():
    assert scoring.build_profiles([]) == []


# --- flagged-set metrics ----------------------------------------------------- #

EVALS = [
    {"doc_id": "a", "is_identifiable": True, "confidence_score": 0.95,
     "identifying_snippets": ["the pulp desk analyst"]},
    {"doc_id": "b", "is_identifiable": True, "confidence_score": 0.80,
     "identifying_snippets": ["weekly wrap author"]},
    {"doc_id": "c", "is_identifiable": True, "confidence_score": 0.65,
     "identifying_snippets": ["someone on the desk"]},
    {"doc_id": "d", "is_identifiable": False, "confidence_score": 0.99,
     "identifying_snippets": []},
]


def test_one_judgment_pass_serves_every_threshold():
    assert scoring.flagged_at(EVALS, 0.88) == {"a"}
    assert scoring.flagged_at(EVALS, 0.75) == {"a", "b"}
    assert scoring.flagged_at(EVALS, 0.60) == {"a", "b", "c"}


def test_precision_recall_and_false_positive_rate():
    out = scoring.precision_recall({"a", "b"}, {"a", "z"}, documents_scanned=10_000)
    assert out["true_positives"] == 1 and out["false_positives"] == 1
    assert out["precision"] == 0.5 and out["recall"] == 0.5
    assert out["false_positives_per_1000_documents"] == 0.1


def test_precision_recall_handles_an_empty_flagged_set():
    out = scoring.precision_recall(set(), {"a"}, 100)
    assert out["precision"] == 0.0 and out["recall"] == 0.0


# --- span metrics ------------------------------------------------------------ #

TEXTS = {"a": "the pulp desk analyst circulated it", "b": "no such phrase here"}


def test_span_validity_catches_a_snippet_that_is_not_verbatim():
    out = scoring.span_validity(EVALS[:2], TEXTS)
    assert out["snippets_checked"] == 2
    assert out["verbatim"] == 1
    assert out["documents_with_an_invalid_span"] == 1
    assert out["percent_verbatim"] == 50.0


def test_span_validity_ignores_documents_it_has_no_text_for():
    assert scoring.span_validity(EVALS, {})["snippets_checked"] == 0


def test_over_redaction_measures_the_share_replaced():
    out = scoring.over_redaction(EVALS[:1], TEXTS)
    assert out["documents"] == 1
    assert 0.5 < out["mean_share_redacted"] < 0.7  # 21 of 35 characters


def test_over_redaction_ignores_a_snippet_that_does_not_occur():
    assert scoring.over_redaction(EVALS[1:2], TEXTS)["mean_share_redacted"] == 0.0


# --- descriptive split ------------------------------------------------------- #

def test_descriptive_split_separates_prose_from_recipient_lists():
    positives = [{"doc_id": "a"}, {"doc_id": "b"}]
    texts = {
        "a": "Causholli circulated the weekly pulp market wrap to the desk.",
        "b": "To: Neal, Scott; Causholli, Monika; Ermis, Frank; Roe, Jane",
    }
    out = scoring.descriptive_split(positives, texts, "Causholli")
    assert [p["doc_id"] for p in out["descriptive"]] == ["a"]
    assert [p["doc_id"] for p in out["list_only"]] == ["b"]
    assert out["distinct_descriptive"] == 1


# --- assumptions and the guard ----------------------------------------------- #

LABELS = {
    "subject": "monika.causholli@enron.com",
    "masked_index": "mail-enron-masked",
    "mask_replacement": "",
    "phone_policy": "identification",
    "audit": {"passed": True, "failures": []},
    "aliases": {"variants": ["Causholli", "Monika"], "label_variants": ["Causholli"],
                "ambiguous_variants": ["Monika"]},
}


def test_assumptions_record_what_the_number_rests_on():
    out = scoring.assumptions(LABELS, (10, 50), [{"profile": "p1"}, {"profile": "p2"}])
    assert out["subject"] == "monika.causholli@enron.com"
    assert out["label_variants"] == ["Causholli"]
    assert out["ambiguous_variants_masked_but_not_labelled"] == ["Monika"]
    assert out["profile_wordings"] == ["p1", "p2"]
    assert any("do not transfer" in c for c in out["caveats"])


def test_guard_refuses_the_unmasked_index():
    assert scoring.guard(LABELS, "mail-enron-masked") is True
    with pytest.raises(ValueError, match="Refusing to score"):
        scoring.guard(LABELS, "mail-enron")


def test_without_drops_judgments_on_the_derive_half():
    kept = scoring.without(EVALS, {"a", "d"})
    assert [e["doc_id"] for e in kept] == ["b", "c"]


class _MgetClient:
    def mget(self, body):
        return {"docs": [{"_id": d["_id"], "found": d["_id"] != "missing",
                          "_source": {"message": f"text of {d['_id']}"}}
                         for d in body["docs"]]}


def test_fetch_texts_skips_documents_that_are_not_there():
    out = scoring.fetch_texts(_MgetClient(), "idx", ["a", "missing", "b"])
    assert out == {"a": "text of a", "b": "text of b"}
