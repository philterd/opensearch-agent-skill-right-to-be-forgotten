"""Unit tests for hybrid fusion configuration (no cluster required)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from lib import model  # noqa: E402


def _processor(body):
    return list(body["phase_results_processors"][0].items())[0]


def test_rrf_is_the_default():
    name, config = _processor(model.hybrid_pipeline_body())
    assert name == "score-ranker-processor"
    assert config["combination"] == {"technique": "rrf", "rank_constant": 60}


def test_normalization_is_still_available():
    name, config = _processor(model.hybrid_pipeline_body("normalization", (0.5, 0.5)))
    assert name == "normalization-processor"
    assert config["combination"]["parameters"]["weights"] == [0.5, 0.5]
    assert config["normalization"]["technique"] == "min_max"


def test_the_rank_constant_is_configurable():
    _, config = _processor(model.hybrid_pipeline_body("rrf", constant=20))
    assert config["combination"]["rank_constant"] == 20


def test_weights_do_not_apply_to_rrf():
    """RRF ranks rather than scores, so GDPR_HYBRID_WEIGHTS is inert under it."""
    body = model.hybrid_pipeline_body("rrf", weights=(0.9, 0.1))
    assert "weights" not in str(body)


def test_an_unknown_technique_is_rejected(monkeypatch):
    monkeypatch.setenv("GDPR_HYBRID_FUSION", "magic")
    with pytest.raises(RuntimeError, match="must be 'rrf' or 'normalization'"):
        model.hybrid_fusion()


def test_the_environment_selects_the_technique(monkeypatch):
    monkeypatch.setenv("GDPR_HYBRID_FUSION", "normalization")
    assert model.hybrid_fusion() == "normalization"
    monkeypatch.delenv("GDPR_HYBRID_FUSION")
    assert model.hybrid_fusion() == "rrf"


def test_a_non_integer_rank_constant_is_rejected(monkeypatch):
    monkeypatch.setenv("GDPR_RRF_RANK_CONSTANT", "sixty")
    with pytest.raises(RuntimeError, match="must be an integer"):
        model.rank_constant()
