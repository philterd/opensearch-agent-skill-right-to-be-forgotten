"""Unit smoke tests for gdpr-forget-me pure logic (no cluster required).

Run:  uv run --with pytest pytest tests/ -q
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from lib.evaluate import filter_flagged, threshold_for, PRECISION_MODES  # noqa: E402
from lib.actions import build_action_plan, build_curl_script, _REDACT_PAINLESS  # noqa: E402
from lib.discovery import build_hybrid_query, build_bm25_query  # noqa: E402
from lib.direct import (scan_text_pii, normalize_identifiers, build_direct_query,  # noqa: E402
                        _find_verbatim)


def test_thresholds():
    assert threshold_for("strict_precision") == 0.88
    assert threshold_for("balanced") == 0.75
    assert threshold_for("high_recall") == 0.60
    assert threshold_for("unknown") == PRECISION_MODES["balanced"]


def test_filter_flagged_respects_threshold_and_identifiability():
    evals = [
        {"doc_id": "a", "is_identifiable": True, "confidence_score": 0.9, "identifying_snippets": ["x"]},
        {"doc_id": "b", "is_identifiable": True, "confidence_score": 0.70, "identifying_snippets": ["y"]},
        {"doc_id": "c", "is_identifiable": False, "confidence_score": 0.99, "identifying_snippets": []},
    ]
    flagged = filter_flagged(evals, "balanced")  # threshold 0.75
    ids = {f["doc_id"] for f in flagged}
    assert ids == {"a"}  # b below threshold, c not identifiable


def test_filter_flagged_enriches_from_candidates():
    evals = [{"doc_id": "a", "is_identifiable": True, "confidence_score": 0.8, "identifying_snippets": ["x"]}]
    cands = {"a": {"index": "logs-1", "text": "x here", "timestamp": "t"}}
    flagged = filter_flagged(evals, "balanced", cands)
    assert flagged[0]["index"] == "logs-1"


def test_filter_flagged_drops_nonstring_snippets():
    evals = [{"doc_id": "a", "is_identifiable": True, "confidence_score": 0.8,
              "identifying_snippets": ["ok", None, 5, ""]}]
    flagged = filter_flagged(evals, "high_recall")
    assert flagged[0]["identifying_snippets"] == ["ok"]


def test_action_plan_shapes():
    flagged = [{"doc_id": "a", "index": "i", "confidence_score": 0.9, "identifying_snippets": ["s"]}]
    redact = build_action_plan(flagged, "redact_in_place")
    assert redact["document_count"] == 1
    assert redact["operations"][0]["action"] == "redact"
    assert redact["dsl_example"]["path"].endswith("_update_by_query")

    delete = build_action_plan(flagged, "hard_delete")
    assert delete["operations"][0]["action"] == "delete"
    assert delete["dsl_example"]["path"].endswith("_delete_by_query")

    dry = build_action_plan(flagged, "dry_run")
    assert dry["operations"][0]["action"] == "preview_redact"


def test_painless_targets_named_field_and_snippets():
    assert "params.field" in _REDACT_PAINLESS
    assert "params.snippets" in _REDACT_PAINLESS
    assert "params.redaction" in _REDACT_PAINLESS


def test_curl_script_delete_and_redact():
    flagged = [{"doc_id": "d1", "index": "logs-1", "confidence_score": 0.9,
                "identifying_snippets": ["it's a quote 'inside'"]}]
    delete = build_curl_script(flagged, "hard_delete")
    assert 'DELETE "$OS/logs-1/_doc/d1' in delete
    assert delete.startswith("#!/usr/bin/env bash")

    redact = build_curl_script(flagged, "redact_in_place")
    # snippet with quotes must be carried via a quoted heredoc, not shell-escaped
    assert "--data-binary @- <<'JSON'" in redact
    assert "it's a quote 'inside'" in redact
    assert "_update/d1" in redact


def test_hybrid_query_structure():
    q = build_hybrid_query("message", "message_embedding", "kw", "profile", "model-1", 10)
    hybrid = q["query"]["hybrid"]["queries"]
    assert "match" in hybrid[0]
    assert hybrid[1]["neural"]["message_embedding"]["model_id"] == "model-1"
    assert q["_source"]["excludes"] == ["message_embedding"]


def test_bm25_fallback_query_structure():
    q = build_bm25_query("message", "kw", "profile", 10)
    assert q["query"]["bool"]["minimum_should_match"] == 1


def test_scan_text_pii_finds_email_ip_phone_and_ignores_short_numbers():
    text = "reach j.tanaka@example.com or 555-123-4567 from 10.0.0.5; ref #4091 build 5.2"
    kinds = {p["type"] for p in scan_text_pii(text)}
    assert "email" in kinds and "ipv4" in kinds and "phone" in kinds
    values = {p["value"] for p in scan_text_pii(text)}
    assert "j.tanaka@example.com" in values
    # "#4091" and "5.2" are too short to be phone numbers
    assert not any(v in ("4091", "5.2") for v in values)


def test_normalize_and_direct_query():
    idents = normalize_identifiers(email=["a@b.com"], name=["Jun Tanaka"], id=["EMP-1"])
    assert {i["type"] for i in idents} == {"email", "name", "employee_id"}
    q = build_direct_query(idents, "message", 50)
    assert q["query"]["bool"]["minimum_should_match"] == 1
    assert len(q["query"]["bool"]["should"]) == 3


def test_find_verbatim_is_case_preserving():
    assert _find_verbatim("Deploy by J.Tanaka@Example.com now", "j.tanaka@example.com") == "J.Tanaka@Example.com"
    assert _find_verbatim("no match here", "absent") is None


def test_local_audit_chain_write_verify_and_tamper(tmp_path):
    import json as _json
    from lib import audit
    d = str(tmp_path)
    req = {"action_type": "hard_delete", "index_pattern": "logs-*",
           "precision_mode": "balanced", "target_profile": "someone"}
    flagged = [{"doc_id": "a", "index": "logs-1", "confidence_score": 1.0,
                "identifying_snippets": ["x"], "reasoning": "r"}]

    c1 = audit.write_certificate(audit.build_record(req, flagged), certificate_dir=d)
    c2 = audit.write_certificate(audit.build_record(req, flagged), certificate_dir=d)
    assert c1["prev_hash"] == "GENESIS"
    assert c2["prev_hash"] == c1["entry_hash"]  # chained

    ok = audit.verify_chain(d)
    assert ok["intact"] is True and ok["entries"] == 2

    listing = audit.list_entries(d)
    assert listing["count"] == 2

    # Tamper with the first certificate's payload; the chain must break.
    path1 = audit.load_certificates(d)[0][0]
    obj = _json.load(open(path1))
    obj["flagged_count"] = 999
    _json.dump(obj, open(path1, "w"))
    broken = audit.verify_chain(d)
    assert broken["intact"] is False and broken["broken_at"] == 0
