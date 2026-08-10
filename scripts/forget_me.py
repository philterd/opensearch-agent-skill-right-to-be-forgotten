#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["opensearch-py>=2.4"]
# ///
"""gdpr-forget-me — CLI for the OpenSearch privacy-erasure agent skill.

Run every command with uv (deps are declared inline above):

    uv run python scripts/forget_me.py <command> [options]

Commands:
    status         Check OpenSearch connectivity and deployed embedding model
    setup          Bootstrap cluster (if needed) + deploy embedding model & pipelines
    seed-demo      Load the synthetic multi-index demo dataset
    seed-enron     Load a subset of the real Enron email corpus (fetched from CMU)
    discover       Phase 1: hybrid BM25 + neural retrieval of candidate documents
    discover-direct Phase 1b: find docs containing the subject's direct identifiers
    evaluate       Phase 2 (optional headless): score candidates via a configured LLM
    plan           Phase 3 preview: filter by precision threshold, show exact DSL (no writes)
    export-curl    Phase 4: write reviewable curl commands + a local erasure certificate
    verify-chain   Verify the local certificates' tamper-evident hash chain
    audit-log      Show recent erasure certificates
    roster         Evaluation only: extract a roster from mail-enron headers

Data flow: `discover` emits candidates as JSON -> the host agent evaluates each
using the judgment prompt in SKILL.md -> agent passes evaluations to `plan` /
`export-curl`. The skill never writes to OpenSearch; `export-curl` produces a
reviewable script the human runs, plus a local hash-chained erasure certificate.
Evaluation is agent-native by default (no API key, vendor-neutral); `evaluate` is
only for headless/CI runs with GDPR_LLM_BASE_URL set.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _out(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def _fail(message, **extra):
    _out({"ok": False, "error": message, **extra})
    sys.exit(1)


def _load_json_arg(value):
    """Accept inline JSON, or @path to read JSON from a file."""
    if value is None:
        return None
    if value.startswith("@"):
        with open(value[1:], encoding="utf-8") as fh:
            return json.load(fh)
    return json.loads(value)


def _candidates_by_id(candidates):
    return {c.get("doc_id"): c for c in (candidates or []) if c.get("doc_id")}


# --------------------------------------------------------------------------- #

def cmd_status(args):
    from lib.client import create_client, endpoint_label
    from lib.model import find_deployed_model
    try:
        client = create_client(bootstrap=False)
    except Exception as e:
        _fail(str(e), reachable=False)
    model_id = find_deployed_model(client)
    _out({
        "ok": True,
        "reachable": True,
        "endpoint": endpoint_label(client),
        "embedding_model_deployed": bool(model_id),
        "model_id": model_id,
    })


def cmd_setup(args):
    from lib.client import create_client, endpoint_label
    from lib.model import setup_neural_search
    client = create_client(bootstrap=True)
    info = setup_neural_search(client, args.text_field, args.embedding_field)
    _out({"ok": True, "endpoint": endpoint_label(client), **info})


def cmd_seed_demo(args):
    from lib.client import create_client
    import seed_demo
    client = create_client(bootstrap=True)
    result = seed_demo.load(
        client,
        setup_neural=not args.no_neural,
        text_field=args.text_field,
        embedding_field=args.embedding_field,
        noise_count=args.noise,
    )
    # The answer key goes to a file, not to stdout: this output lands in the
    # context of the agent that then evaluates the corpus.
    result = seed_demo.split_ground_truth(
        result,
        path=args.ground_truth_out or seed_demo.GROUND_TRUTH_PATH,
        reveal=args.reveal_ground_truth)
    _out({"ok": True, **result})


def cmd_seed_enron(args):
    from lib.client import create_client
    import seed_enron
    client = create_client(bootstrap=True)
    result = seed_enron.load(
        client,
        setup_neural=not args.no_neural,
        text_field=args.text_field,
        embedding_field=args.embedding_field,
        source=args.source,
        limit=args.limit,
        custodians=args.custodian,
        folders=args.folder,
        max_chars=args.max_chars,
    )
    _out({"ok": True, **result})


def cmd_discover(args):
    from lib.client import create_client
    from lib.discovery import discover
    from lib.model import find_deployed_model
    client = create_client(bootstrap=False)
    model_id = args.model_id or find_deployed_model(client)
    candidates, meta = discover(
        client,
        index_pattern=args.index,
        profile=args.profile,
        keywords=args.keywords,
        text_field=args.text_field,
        embedding_field=args.embedding_field,
        timestamp_field=args.timestamp_field,
        model_id=model_id,
        size=args.size,
    )
    _out({"ok": True, "meta": meta, "candidate_count": len(candidates), "candidates": candidates})


def cmd_discover_direct(args):
    from lib.client import create_client
    from lib.direct import discover_direct, normalize_identifiers
    client = create_client(bootstrap=False)
    identifiers = normalize_identifiers(
        email=args.email, phone=args.phone, ip=args.ip,
        name=args.name, id=args.id, term=args.term,
    )
    if not identifiers:
        _fail("Provide at least one identifier: --email/--phone/--ip/--name/--id/--term.")
    candidates, evaluations, meta = discover_direct(
        client, index_pattern=args.index, identifiers=identifiers,
        text_field=args.text_field, timestamp_field=args.timestamp_field,
        size=args.size, scan_pii=not args.no_scan_pii,
    )
    _out({"ok": True, "meta": meta, "candidate_count": len(candidates),
          "candidates": candidates, "evaluations": evaluations})


def cmd_evaluate(args):
    from lib.evaluate import headless_available, evaluate_headless
    if not headless_available():
        _fail(
            "Headless evaluation requires GDPR_LLM_BASE_URL (OpenAI-compatible endpoint). "
            "For interactive use, the host agent should evaluate candidates directly using "
            "the judgment prompt in SKILL.md and pass results to `plan`/`export-curl`.",
            headless_available=False,
        )
    candidates = _load_json_arg(args.candidates)
    if isinstance(candidates, dict):
        candidates = candidates.get("candidates", [])
    evaluations = evaluate_headless(candidates, args.profile, args.precision_mode)
    _out({"ok": True, "precision_mode": args.precision_mode, "evaluations": evaluations})


def _resolve_flagged(args):
    from lib.evaluate import filter_flagged
    evaluations = _load_json_arg(args.evaluations)
    if isinstance(evaluations, dict):
        evaluations = evaluations.get("evaluations", [])
    candidates = _load_json_arg(args.candidates) if args.candidates else None
    if isinstance(candidates, dict):
        candidates = candidates.get("candidates", [])
    flagged = filter_flagged(evaluations, args.precision_mode, _candidates_by_id(candidates))
    return evaluations, flagged


def cmd_plan(args):
    from lib.actions import build_action_plan
    from lib.evaluate import threshold_for
    evaluations, flagged = _resolve_flagged(args)
    plan = build_action_plan(flagged, args.action_type, text_field=args.text_field)
    _out({
        "ok": True,
        "precision_mode": args.precision_mode,
        "threshold": threshold_for(args.precision_mode),
        "action_type": args.action_type,
        "evaluated": len(evaluations),
        "flagged": flagged,
        "plan": plan,
    })


def cmd_export_curl(args):
    from lib.actions import build_curl_script, _assert_not_on_hold, LegalHoldError
    from lib.evaluate import threshold_for
    from lib.audit import build_record, write_certificate, script_sha256
    _, flagged = _resolve_flagged(args)
    if not flagged:
        _fail("No documents met the confidence threshold; nothing to export.", flagged_count=0)
    try:
        _assert_not_on_hold(flagged, _parse_list(args.legal_hold))
    except LegalHoldError as e:
        _fail(str(e), legal_hold_violation=True)
    host = os.getenv("OPENSEARCH_HOST", "localhost")
    port = os.getenv("OPENSEARCH_PORT", "9200")
    os_url = os.getenv("OPENSEARCH_URL", f"http://{host}:{port}")
    script = build_curl_script(
        flagged, args.action_type, text_field=args.text_field,
        os_url_default=os_url, target_profile=args.profile,
        precision_mode=args.precision_mode,
    )
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(script)
    os.chmod(args.out, 0o755)

    # Write a local, hash-chained erasure certificate recording exactly what the
    # script will erase. No cluster writes occur.
    request = {
        "target_profile": args.profile,
        "index_pattern": args.index,
        "precision_mode": args.precision_mode,
        "action_type": args.action_type,
    }
    record = build_record(request, flagged, curl_script_path=args.out,
                          curl_script_hash=script_sha256(args.out))
    certificate = write_certificate(record, certificate_dir=args.audit_dir)

    _out({
        "ok": True,
        "action_type": args.action_type,
        "threshold": threshold_for(args.precision_mode),
        "documents": len(flagged),
        "output_file": os.path.abspath(args.out),
        "certificate": certificate,
        "note": "No changes were made to OpenSearch. Review the script, then run it to apply.",
    })


def cmd_verify_chain(args):
    from lib.audit import verify_chain
    _out({"ok": True, **verify_chain(args.audit_dir)})


def cmd_audit_log(args):
    from lib.audit import list_entries
    _out({"ok": True, **list_entries(args.audit_dir, limit=args.size)})


def cmd_roster(args):
    from lib.client import create_client
    from lib import roster
    client = create_client(bootstrap=False)
    if not client.indices.exists(index=args.index):
        _fail(f"Index '{args.index}' does not exist. Run seed-enron first.")
    entries, coverage, decision = roster.build(
        client,
        index=args.index,
        min_messages=args.min_messages,
        min_subjects=args.min_subjects,
        top_correspondents=args.top_correspondents,
    )
    path = roster.write_roster(entries, coverage, decision,
                               path=args.out or roster.ROSTER_PATH)
    # Aggregates only. The roster itself names real people and stays on disk.
    _out({
        "ok": True,
        "index": args.index,
        "roster_file": path,
        "coverage": coverage,
        "decision": decision,
        "note": (
            "Roster withheld from this output and written to the file above; it names "
            "real people. Stage one of EVALUATION.md: the decision above records "
            "whether the available attributes can support the masking and scoring "
            "stages."
        ),
    })


def _parse_list(value):
    if not value:
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


# --------------------------------------------------------------------------- #

def build_parser():
    p = argparse.ArgumentParser(description="gdpr-forget-me OpenSearch privacy-erasure skill")
    sub = p.add_subparsers(dest="command", required=True)

    def add_field_opts(sp):
        sp.add_argument("--text-field", default="message")
        sp.add_argument("--embedding-field", default="message_embedding")

    sp = sub.add_parser("status"); sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("setup"); add_field_opts(sp); sp.set_defaults(func=cmd_setup)

    sp = sub.add_parser("seed-demo")
    add_field_opts(sp)
    sp.add_argument("--no-neural", action="store_true",
                    help="Seed plain BM25 data without deploying the embedding model")
    sp.add_argument("--noise", type=int, default=450,
                    help="Number of generic noise docs to pad the corpus (default 450)")
    sp.add_argument("--ground-truth-out", default=None,
                    help="Where to write the answer key (default gdpr-eval/demo-ground-truth.json)")
    sp.add_argument("--reveal-ground-truth", action="store_true",
                    help="Also print the answer key; contaminates any agent evaluation of this run")
    sp.set_defaults(func=cmd_seed_demo)

    sp = sub.add_parser("seed-enron")
    add_field_opts(sp)
    sp.add_argument("--source", default=None,
                    help="Local enron_mail_20150507.tar.gz (default: stream from CMU)")
    sp.add_argument("--limit", type=int, default=2000,
                    help="Maximum messages to index (default 2000)")
    sp.add_argument("--custodian", action="append", default=None,
                    help="Only index this custodian's maildir (repeatable)")
    sp.add_argument("--folder", action="append", default=None,
                    help="Only index this top-level folder, e.g. sent (repeatable)")
    sp.add_argument("--max-chars", type=int, default=4000,
                    help="Truncate message bodies to this length (default 4000)")
    sp.add_argument("--no-neural", action="store_true",
                    help="Seed plain BM25 data without deploying the embedding model")
    sp.set_defaults(func=cmd_seed_enron)

    sp = sub.add_parser("discover")
    sp.add_argument("--index", required=True)
    sp.add_argument("--profile", required=True)
    sp.add_argument("--keywords", default=None,
                    help="Contextual keywords for the BM25 clause (defaults to the profile)")
    sp.add_argument("--size", type=int, default=50)
    sp.add_argument("--model-id", default=None)
    sp.add_argument("--timestamp-field", default="@timestamp")
    add_field_opts(sp)
    sp.set_defaults(func=cmd_discover)

    sp = sub.add_parser("discover-direct")
    sp.add_argument("--index", required=True)
    sp.add_argument("--email", action="append", help="Subject email (repeatable)")
    sp.add_argument("--phone", action="append", help="Subject phone (repeatable)")
    sp.add_argument("--ip", action="append", help="Subject IP address (repeatable)")
    sp.add_argument("--name", action="append", help="Subject name (repeatable)")
    sp.add_argument("--id", action="append", help="Subject employee/user id (repeatable)")
    sp.add_argument("--term", action="append", help="Any other exact identifier (repeatable)")
    sp.add_argument("--size", type=int, default=200)
    sp.add_argument("--timestamp-field", default="@timestamp")
    sp.add_argument("--no-scan-pii", action="store_true",
                    help="Do not also redact other PII co-located in matched docs")
    sp.add_argument("--text-field", default="message")
    sp.set_defaults(func=cmd_discover_direct)

    sp = sub.add_parser("evaluate")
    sp.add_argument("--candidates", required=True, help="Inline JSON or @path")
    sp.add_argument("--profile", required=True)
    sp.add_argument("--precision-mode", default="balanced",
                    choices=["strict_precision", "balanced", "high_recall"])
    sp.set_defaults(func=cmd_evaluate)

    def add_action_opts(sp):
        sp.add_argument("--evaluations", required=True, help="Inline JSON or @path")
        sp.add_argument("--candidates", default=None, help="Inline JSON or @path (enrichment)")
        sp.add_argument("--precision-mode", default="balanced",
                        choices=["strict_precision", "balanced", "high_recall"])
        sp.add_argument("--text-field", default="message")

    sp = sub.add_parser("plan")
    add_action_opts(sp)
    sp.add_argument("--action-type", default="dry_run",
                    choices=["dry_run", "redact_in_place", "hard_delete"])
    sp.set_defaults(func=cmd_plan)

    sp = sub.add_parser("export-curl")
    add_action_opts(sp)
    sp.add_argument("--action-type", default="hard_delete",
                    choices=["redact_in_place", "hard_delete"])
    sp.add_argument("--out", required=True, help="Path to write the reviewable curl script")
    sp.add_argument("--index", default=None, help="Index pattern (recorded in the certificate)")
    sp.add_argument("--profile", default=None, help="Target profile (comment + certificate)")
    sp.add_argument("--legal-hold", default=None,
                    help="Comma-separated index glob patterns to refuse exporting")
    sp.add_argument("--audit-dir", default=None,
                    help="Directory for the erasure certificate (default gdpr-audit)")
    sp.set_defaults(func=cmd_export_curl)

    sp = sub.add_parser("verify-chain")
    sp.add_argument("--audit-dir", default=None)
    sp.set_defaults(func=cmd_verify_chain)

    sp = sub.add_parser("audit-log")
    sp.add_argument("--size", type=int, default=20)
    sp.add_argument("--audit-dir", default=None)
    sp.set_defaults(func=cmd_audit_log)

    sp = sub.add_parser("roster", help="Evaluation only: not part of the erasure workflow")
    sp.add_argument("--index", default="mail-enron")
    sp.add_argument("--out", default=None,
                    help="Where to write the roster (default gdpr-eval/enron-roster.json)")
    sp.add_argument("--min-messages", type=int, default=20,
                    help="Messages a subject needs to be usable for evaluation (default 20)")
    sp.add_argument("--min-subjects", type=int, default=5,
                    help="Usable subjects required for a 'go' decision (default 5)")
    sp.add_argument("--top-correspondents", type=int, default=5,
                    help="Correspondents recorded per person (default 5)")
    sp.set_defaults(func=cmd_roster)

    return p


def main():
    args = build_parser().parse_args()
    try:
        args.func(args)
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 - surface a clean JSON error to the agent
        _fail(f"{type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
