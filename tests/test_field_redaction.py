"""Unit tests for identity-field redaction (no cluster required).

The script itself is exercised against a live cluster; these cover the
plan and script generation around it.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from lib.actions import (_REDACT_FIELDS_PAINLESS, build_action_plan,  # noqa: E402
                         build_curl_script, field_values)


def _flagged(**extra):
    return dict({"doc_id": "d1", "index": "mail", "confidence_score": 1.0,
                 "identifying_snippets": [], "identifying_fields": []}, **extra)


FIELD_HIT = _flagged(identifying_fields=[
    {"field": "to", "value": "lynn.blair@enron.com", "matched": "lynn.blair@enron.com",
     "type": "email"},
    {"field": "from", "value": "Lynn Blair <lynn.blair@enron.com>",
     "matched": "lynn.blair@enron.com", "type": "email"},
])


def test_field_values_groups_by_field_and_deduplicates():
    item = _flagged(identifying_fields=[
        {"field": "to", "matched": "a@x"}, {"field": "to", "matched": "a@x"},
        {"field": "to", "matched": "b@x"}, {"field": "cc", "matched": "a@x"}])
    assert field_values(item) == {"to": ["a@x", "b@x"], "cc": ["a@x"]}


def test_field_values_falls_back_to_the_whole_value():
    item = _flagged(identifying_fields=[{"field": "from", "value": "Lynn <l@x>"}])
    assert field_values(item) == {"from": ["Lynn <l@x>"]}


def test_a_document_with_no_field_hits_yields_nothing():
    assert field_values(_flagged()) == {}
    assert field_values({}) == {}


def test_the_plan_records_identity_fields_alongside_snippets():
    plan = build_action_plan([FIELD_HIT], "redact_in_place")
    op = plan["operations"][0]
    assert op["action"] == "redact"
    assert op["field"] == "message"
    assert op["identity_fields"] == {"to": ["lynn.blair@enron.com"],
                                     "from": ["lynn.blair@enron.com"]}


def test_a_dry_run_previews_the_field_redaction_too():
    op = build_action_plan([FIELD_HIT], "dry_run")["operations"][0]
    assert op["action"] == "preview_redact"
    assert "identity_fields" in op


def test_hard_delete_needs_no_field_detail():
    op = build_action_plan([FIELD_HIT], "hard_delete")["operations"][0]
    assert op["action"] == "delete" and "identity_fields" not in op


def test_the_script_emits_a_field_update_for_a_field_only_match():
    script = build_curl_script([FIELD_HIT], "redact_in_place")
    assert _REDACT_FIELDS_PAINLESS in script
    assert "# identity fields: from, to" in script
    # No text update, because there is nothing to replace in the text.
    assert script.count("_update/d1") == 1


def test_a_document_matching_both_gets_two_updates():
    both = _flagged(identifying_snippets=["lynn.blair@enron.com"],
                    identifying_fields=[{"field": "to", "matched": "lynn.blair@enron.com"}])
    script = build_curl_script([both], "redact_in_place")
    assert script.count("_update/d1") == 2


def test_a_text_only_match_emits_no_field_update():
    text_only = _flagged(identifying_snippets=["lynn.blair@enron.com"])
    script = build_curl_script([text_only], "redact_in_place")
    assert _REDACT_FIELDS_PAINLESS not in script


def test_verification_reads_back_every_field_it_changed():
    script = build_curl_script([FIELD_HIT], "redact_in_place")
    assert "_source=message%2Cfrom%2Cto" in script


def test_the_field_script_params_are_valid_json():
    script = build_curl_script([FIELD_HIT], "redact_in_place")
    body = script.split("<<'JSON'")[1].split("JSON")[0]
    params = json.loads(body)["script"]["params"]
    assert params["fields"]["to"] == ["lynn.blair@enron.com"]
    assert params["redaction"] == "[GDPR_REDACTED]"


def test_the_script_iterates_the_map_with_entryset():
    """Painless cannot iterate a Map directly; this fails at runtime, not compile."""
    assert "params.fields.entrySet()" in _REDACT_FIELDS_PAINLESS
    assert "for (entry in params.fields)" not in _REDACT_FIELDS_PAINLESS
