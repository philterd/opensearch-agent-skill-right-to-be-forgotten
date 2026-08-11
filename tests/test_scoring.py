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
    assert out["recall@4"]["found"] == 1
    assert out["recall@4"]["of"] == 2
    assert out["recall@4"]["percent"] == 50.0
    assert out["recall@4"]["k_caps_recall"] is False


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


# --- k must be able to reach every positive --------------------------------- #

def test_extend_ks_adds_a_k_that_covers_the_positives():
    assert scoring.extend_ks([10, 50], 451) == (10, 50, 451)


def test_extend_ks_leaves_an_already_sufficient_k_alone():
    assert scoring.extend_ks([10, 500], 451) == (10, 500)


def test_extend_ks_deduplicates_and_sorts():
    assert scoring.extend_ks([50, 10, 50], 5) == (10, 50)


def test_extended_k_removes_the_cap_warning():
    ks = scoring.extend_ks([10], 3)
    out = scoring.recall_at_k(["a", "b", "c"], {"a", "b", "c"}, ks=ks)
    assert all(not v["k_caps_recall"] for v in out.values())


# --- the ablation must be stated, not just tabulated ------------------------- #

def _run(mode, percent, key="recall@500", of=76):
    return {"mode": mode, "recall_descriptive": {key: {"percent": percent, "of": of}}}


def test_ablation_says_plainly_when_bm25_wins():
    runs = [_run("hybrid", 13.0), _run("bm25_only", 20.5)]
    out = scoring.compare_modes(runs, "recall@500")
    assert out["gap_points"] == -7.5
    assert "BM25-only beat hybrid" in out["verdict"]
    assert "contradicted" in out["verdict"]


def test_ablation_says_plainly_when_hybrid_wins():
    out = scoring.compare_modes([_run("hybrid", 30.0), _run("bm25_only", 20.0)],
                                "recall@500")
    assert "Hybrid beat BM25-only" in out["verdict"]


def test_ablation_calls_a_small_gap_noise_and_withholds_the_claim():
    out = scoring.compare_modes([_run("hybrid", 20.4), _run("bm25_only", 20.0)],
                                "recall@500")
    assert "within noise" in out["verdict"]
    assert "does not support the claim" in out["verdict"]


def test_ablation_takes_the_best_wording_per_mode():
    runs = [_run("hybrid", 5.0), _run("hybrid", 18.0), _run("bm25_only", 10.0)]
    out = scoring.compare_modes(runs, "recall@500")
    assert out["best_hybrid_percent"] == 18.0


# --- interpretation ---------------------------------------------------------- #

def test_assumptions_carry_the_precision_thresholds_and_what_is_measured():
    out = scoring.assumptions(LABELS, (10,), [])
    assert out["precision_thresholds"]["strict_precision"] == 0.88
    assert out["precision_thresholds"]["high_recall"] == 0.60
    assert "topical authorship retrieval" in out["measures"]


def test_interpretation_refuses_the_stronger_reading():
    assert "adjacent, not the same" in scoring.INTERPRETATION
    assert "demo corpus" in scoring.INTERPRETATION


# --- interval reporting on small denominators -------------------------------- #

def test_wilson_interval_widens_as_the_denominator_shrinks():
    assert scoring.wilson_interval(24, 76) == (22.2, 42.7)
    narrow = scoring.wilson_interval(240, 760)
    assert narrow[1] - narrow[0] < 42.7 - 22.2


def test_wilson_interval_handles_the_edges():
    assert scoring.wilson_interval(0, 0) == (0.0, 0.0)
    assert scoring.wilson_interval(0, 10)[0] == 0.0
    assert scoring.wilson_interval(10, 10)[1] == 100.0


def test_recall_carries_an_interval():
    out = scoring.recall_at_k(["a", "b"], {"a", "b", "c"}, ks=(2,))
    assert out["recall@2"]["ci95"] == list(scoring.wilson_interval(2, 3))


def test_separable_matches_the_measured_case():
    assert scoring.separable(24, 14, 76) is False   # the run we made
    assert scoring.separable(60, 5, 76) is True


def test_verdict_withholds_the_magnitude_when_intervals_overlap():
    out = scoring.compare_modes([_run("hybrid", 18.4), _run("bm25_only", 31.6)],
                                "recall@500")
    assert out["intervals_disjoint"] is False
    assert "not settled" in out["verdict"]
    assert "term bags" in out["verdict"]
