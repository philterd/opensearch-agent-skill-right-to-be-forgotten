"""Phase 3 — Action Plan & execution.

Three action types (matching the skill spec):
  * dry_run          — build the exact DSL, execute NOTHING (default, safe).
  * redact_in_place  — replace only the identifying snippets with a redaction
                       token via a Painless script, preserving the rest of the
                       operational record.
  * hard_delete      — remove the whole document.

Every operation targets documents by (index, _id) precisely — never a blind
query — so we can only ever touch the exact documents the evaluator flagged.
Legal-hold indices are refused up front.
"""

from urllib.parse import quote

REDACTION_TOKEN = "[GDPR_REDACTED]"


def _url_path(value):
    """Percent-encode an index or document id for a URL path.

    An id containing '/' otherwise yields "no handler found", which curl -sS
    does not treat as an error: a silently failed erasure.
    """
    return quote(str(value), safe="")

# Painless: replace each identifying snippet in the target field with the
# redaction token. Operates on the specific document only.
_REDACT_PAINLESS = (
    "if (ctx._source.containsKey(params.field) && ctx._source[params.field] != null) {"
    " String v = ctx._source[params.field].toString();"
    " for (s in params.snippets) { v = v.replace(s, params.redaction); }"
    " ctx._source[params.field] = v; }"
)


class LegalHoldError(RuntimeError):
    pass


def _assert_not_on_hold(flagged, legal_hold_patterns):
    if not legal_hold_patterns:
        return
    import fnmatch
    for item in flagged:
        idx = item.get("index", "")
        for pat in legal_hold_patterns:
            if fnmatch.fnmatch(idx, pat):
                raise LegalHoldError(
                    f"Document {item.get('doc_id')} is in '{idx}', which matches "
                    f"legal-hold pattern '{pat}'. Erasure refused. Remove the hold "
                    f"or exclude this index before proceeding."
                )


def build_action_plan(flagged, action_type, text_field="message", redaction_token=REDACTION_TOKEN):
    """Build a preview of the exact operations (no execution)."""
    operations = []
    for item in flagged:
        op = {
            "doc_id": item.get("doc_id"),
            "index": item.get("index"),
            "confidence_score": item.get("confidence_score"),
        }
        if action_type == "redact_in_place":
            op["action"] = "redact"
            op["field"] = text_field
            op["snippets"] = item.get("identifying_snippets", [])
            op["redaction_token"] = redaction_token
        elif action_type == "hard_delete":
            op["action"] = "delete"
        else:  # dry_run previews what redact_in_place would do
            op["action"] = "preview_redact"
            op["field"] = text_field
            op["snippets"] = item.get("identifying_snippets", [])
        operations.append(op)

    return {
        "action_type": action_type,
        "document_count": len(operations),
        "operations": operations,
        "dsl_example": _dsl_example(action_type, flagged, text_field, redaction_token),
    }


def _dsl_example(action_type, flagged, text_field, redaction_token):
    if not flagged:
        return None
    ids = [f.get("doc_id") for f in flagged]
    if action_type == "hard_delete":
        return {
            "method": "POST",
            "path": "/<index>/_delete_by_query",
            "body": {"query": {"ids": {"values": ids}}},
        }
    # redact (also shown for dry_run)
    snippets = flagged[0].get("identifying_snippets", [])
    return {
        "method": "POST",
        "path": "/<index>/_update_by_query",
        "body": {
            "script": {
                "lang": "painless",
                "source": _REDACT_PAINLESS,
                "params": {"field": text_field, "redaction": redaction_token, "snippets": snippets},
            },
            "query": {"ids": {"values": ids}},
        },
    }


def build_curl_script(flagged, action_type, text_field="message",
                      redaction_token=REDACTION_TOKEN, os_url_default="http://localhost:9200",
                      target_profile=None, precision_mode=None):
    """Return a reviewable bash script of curl commands — executes NOTHING.

    This is the review-first path: the agent never mutates OpenSearch. It emits
    the exact per-document commands so a human can inspect every call before
    running the file themselves. Each request body is passed via a quoted
    heredoc so snippets containing quotes/apostrophes need no escaping.
    """
    import json as _json

    lines = [
        "#!/usr/bin/env bash",
        "# GDPR Forget-Me for OpenSearch — remediation commands. REVIEW BEFORE RUNNING.",
        "# Nothing in OpenSearch has been modified by generating this file.",
        f"# Action: {action_type}   Documents: {len(flagged)}",
    ]
    if target_profile:
        lines.append(f"# Target profile: {target_profile}")
    if precision_mode:
        lines.append(f"# Precision mode: {precision_mode}")
    lines += [
        "#",
        "# Set OPENSEARCH_URL to your endpoint. Add auth if your cluster requires it,",
        "# e.g.  export CURL_OPTS='-u admin:yourPassword -k'",
        "set -euo pipefail",
        f'OS="${{OPENSEARCH_URL:-{os_url_default}}}"',
        'CURL_OPTS="${CURL_OPTS:-}"',
        "",
    ]

    for item in flagged:
        idx, doc_id = item.get("index"), item.get("doc_id")
        idx_u, doc_u = _url_path(idx), _url_path(doc_id)
        conf = item.get("confidence_score")
        lines.append(f"# --- {doc_id}  (confidence {conf})  index={idx} ---")
        reason = " ".join(str(item.get("reasoning") or "").split())
        if reason:
            lines.append(f"# reason: {reason}")
        if action_type == "hard_delete":
            lines.append(
                f'curl -sS $CURL_OPTS -X DELETE "$OS/{idx_u}/_doc/{doc_u}?refresh=true"'
            )
        else:  # redact_in_place
            body = {
                "script": {
                    "lang": "painless",
                    "source": _REDACT_PAINLESS,
                    "params": {
                        "field": text_field,
                        "redaction": redaction_token,
                        "snippets": item.get("identifying_snippets", []),
                    },
                }
            }
            lines.append(
                f'curl -sS $CURL_OPTS -X POST "$OS/{idx_u}/_update/{doc_u}?refresh=true" \\'
            )
            lines.append("  -H 'Content-Type: application/json' --data-binary @- <<'JSON'")
            lines.append(_json.dumps(body, indent=2))
            lines.append("JSON")
        lines.append("")

    lines.append("# --- verification (read back the affected documents) ---")
    for item in flagged:
        idx_u, doc_u = _url_path(item.get("index")), _url_path(item.get("doc_id"))
        lines.append(
            f'curl -sS $CURL_OPTS "$OS/{idx_u}/_doc/{doc_u}?_source={_url_path(text_field)}"; echo'
        )
    lines.append("")
    return "\n".join(lines)
