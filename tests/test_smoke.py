"""Unit smoke tests for gdpr-forget-me pure logic (no cluster required).

Run:  uv run --with pytest pytest tests/ -q
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from lib.evaluate import filter_flagged, threshold_for, PRECISION_MODES  # noqa: E402
from lib.actions import build_action_plan, build_curl_script, _REDACT_PAINLESS  # noqa: E402
from lib.discovery import build_hybrid_query, build_bm25_query  # noqa: E402
from lib.direct import (scan_text_pii, normalize_identifiers, build_direct_query,  # noqa: E402
                        _find_verbatim)
import seed_demo  # noqa: E402


def test_thresholds():
    assert threshold_for("strict_precision") == 0.88
    assert threshold_for("balanced") == 0.75
    assert threshold_for("high_recall") == 0.60
    # Recall-first: an unrecognised mode falls back to the loosest threshold,
    # because leaving a subject in the index is the worse failure.
    assert threshold_for("unknown") == PRECISION_MODES["high_recall"]


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


# --- seed_enron: Enron corpus parsing (offline, no network, no cluster) ----- #

import seed_enron  # noqa: E402

_SAMPLE_MESSAGE = b"""Message-ID: <8012132.1075853083164.JavaMail.evans@thyme>\r
Date: Fri, 14 Sep 2001 14:05:43 -0700 (PDT)\r
From: fran.fagan@enron.com\r
To: lynn.blair@enron.com, jodie.floyd@enron.com\r
Cc: bradley.holmes@enron.com\r
Subject: FW: Promotions and Transfers\r
Mime-Version: 1.0\r
Content-Type: text/plain; charset=us-ascii\r
\r
The gas control manager who ran the\r
Kansas City winter operations training\r
is transferring effective 7/16/01.\r
"""


def test_parse_member_extracts_headers_and_body():
    doc = seed_enron.parse_member(_SAMPLE_MESSAGE, "blair-l", "personnel", 4000)
    assert doc["from"] == "fran.fagan@enron.com"
    assert doc["to"] == ["lynn.blair@enron.com", "jodie.floyd@enron.com"]
    assert doc["cc"] == ["bradley.holmes@enron.com"]
    assert doc["subject"] == "FW: Promotions and Transfers"
    assert doc["custodian"] == "blair-l" and doc["folder"] == "personnel"
    assert doc["@timestamp"] == "2001-09-14T21:05:43+00:00"  # normalised to UTC


def test_parse_member_collapses_hard_wrapping():
    """Hard-wrapped bodies must be joined: redaction matches exact substrings."""
    doc = seed_enron.parse_member(_SAMPLE_MESSAGE, "blair-l", "personnel", 4000)
    assert "\n" not in doc["message"]
    assert "the gas control manager who ran the kansas city winter operations" \
        in doc["message"].lower()


def test_parse_member_rejects_empty_message():
    assert seed_enron.parse_member(b"From: a@b.com\r\n\r\n", "c", "f", 4000) is None


def test_clean_body_truncates_and_prefers_quote_boundary():
    body = "real content " * 40 + "-----Original Message----- " + "quoted " * 100
    out = seed_enron._clean_body(body, 600)
    assert len(out) <= 600
    assert "-----Original Message-----" not in out


def test_member_regex_matches_maildir_layout():
    m = seed_enron._MEMBER_RE.match("maildir/blair-l/meetings___nng_customer_mtg/16.")
    assert m and m.groups() == ("blair-l", "meetings___nng_customer_mtg", "16")
    assert seed_enron._MEMBER_RE.match("maildir/blair-l/notes") is None


def test_curl_script_percent_encodes_document_ids():
    """Raw '/' in an id yields 'no handler found', which curl -sS ignores."""
    flagged = [{"doc_id": "blair-l/customer/18", "index": "mail-enron",
                "confidence_score": 1.0, "identifying_snippets": ["a@b.com"],
                "reasoning": "direct"}]

    redact = build_curl_script(flagged, "redact_in_place")
    assert "_update/blair-l%2Fcustomer%2F18?refresh=true" in redact
    assert "_update/blair-l/customer/18" not in redact
    # read-back verification must be encoded too, or it 404s
    assert "_doc/blair-l%2Fcustomer%2F18?_source=message" in redact

    delete = build_curl_script(flagged, "hard_delete")
    assert "_doc/blair-l%2Fcustomer%2F18?refresh=true" in delete
    assert "_doc/blair-l/customer/18" not in delete

    # The human-readable comment keeps the real id, unencoded.
    assert "# --- blair-l/customer/18  (confidence 1.0)" in redact


# --- IPv6 detection: timestamps must not be mistaken for addresses --------- #

def test_ipv6_does_not_match_timestamps():
    """A loose 2-7 group pattern would redact every timestamp in a log index."""
    for text in ["Sent: 10/12/2001 09:11:28 AM", "at 23:59:59 UTC",
                 "elapsed 14:23:10", "ratio 3:2:1", "12:34:56:78"]:
        assert not [p for p in scan_text_pii(text) if p["type"] == "ipv6"], text


def test_ipv6_does_not_match_mac_addresses_or_scope_operators():
    for text in ["00:1A:2B:3C:4D:5E", "std::vector<int>", "Foo::bar()",
                 "http://example.com:8080"]:
        assert not [p for p in scan_text_pii(text) if p["type"] == "ipv6"], text


def test_ipv6_matches_real_addresses_including_compressed():
    for addr in ["2001:db8:85a3:0000:0000:8a2e:0370:7334", "fe80::1", "::1",
                 "2001:db8::8a2e:370:7334", "fe80::a00:27ff:fe4e:66a1"]:
        found = [p["value"] for p in scan_text_pii(f"host {addr} responded")]
        assert addr in found, addr


def test_ipv6_ignores_bare_unspecified_address():
    """Valid address, but identifies nobody."""
    assert not [p for p in scan_text_pii("see :: for details") if p["type"] == "ipv6"]


# --- demo corpus: the agent must not be able to read the answer key --------- #

def test_demo_doc_ids_carry_no_label():
    """An id like 'sub-1' would hand the agent the ground truth via `discover`."""
    gt = seed_demo.build_ground_truth(noise_count=5)
    every_id = [i for k, v in gt.items() if k.endswith("_doc_ids") for i in v]
    assert every_id, "expected some ids"
    for doc_id in every_id:
        assert doc_id.startswith("d-")
        for label in ("sub", "dsub", "dec", "noise"):
            assert label not in doc_id


def test_demo_doc_ids_are_deterministic_and_unique():
    a = seed_demo.build_ground_truth(noise_count=5)
    b = seed_demo.build_ground_truth(noise_count=5)
    assert a["subject_doc_ids"] == b["subject_doc_ids"]
    every_id = [i for k, v in a.items() if k.endswith("_doc_ids") for i in v]
    assert len(set(every_id)) == len(every_id)


def test_split_ground_truth_withholds_key_from_output(tmp_path):
    path = tmp_path / "gt.json"
    result = {"index": "x", "ground_truth": seed_demo.build_ground_truth(5)}
    out = seed_demo.split_ground_truth(result, str(path))
    assert "ground_truth" not in out
    assert out["ground_truth_file"] == str(path)
    assert json.loads(path.read_text())["subject_doc_ids"]


def test_split_ground_truth_reveal_is_opt_in(tmp_path):
    result = {"ground_truth": seed_demo.build_ground_truth(5)}
    out = seed_demo.split_ground_truth(result, str(tmp_path / "gt.json"), reveal=True)
    assert out["ground_truth"]["subject_doc_ids"]


def test_seeded_docs_use_the_opaque_ids():
    gt = seed_demo.build_ground_truth(noise_count=3)
    ids = [doc_id for doc_id, _ in seed_demo._all_docs(noise_count=3)]
    assert set(gt["subject_doc_ids"]).issubset(ids)
    assert len(ids) == len(seed_demo.SUBJECT_DOCS) + len(seed_demo.DIRECT_DOCS) \
        + len(seed_demo.DECOY_DOCS) + 3
