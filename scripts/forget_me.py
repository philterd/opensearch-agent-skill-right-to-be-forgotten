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
    assess         Is the indirect pass worth running on this index?
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
    mask-corpus    Evaluation only: mask a subject out of mail-enron into a new index
    audit-mask     Evaluation only: re-run the leakage gate against the masked index
    subjects       Evaluation only: rank roster subjects the corpus can score
    score-discovery Evaluation only: recall@k for Phase 1, with a BM25 ablation
    score-judgment Evaluation only: score Phase 2 and 3 from an evaluations file
    score-corpus   Evaluation only: hit-rate across many subjects, one document each

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


def cmd_assess(args):
    from lib.client import create_client
    from lib import suitability
    client = create_client(bootstrap=False)
    if not client.indices.exists(index=args.index):
        _fail(f"Index '{args.index}' does not exist.")
    report = suitability.assess(client, args.index, text_field=args.text_field,
                                sample=args.sample)
    _out({
        "ok": True, **report,
        "note": (
            "The direct pass works wherever identifiers appear literally, and is "
            "unaffected by this. This measures only whether documents describe "
            "people well enough for the indirect pass to have something to find."
        ),
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


def _load_roster_entries(path):
    from lib import roster
    path = path or roster.ROSTER_PATH
    if not os.path.exists(path):
        _fail(f"No roster at '{path}'. Run `roster` first.")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh).get("entries", [])


def cmd_subjects(args):
    from lib.client import create_client
    from lib import subjects
    client = create_client(bootstrap=False)
    entries = _load_roster_entries(args.roster)
    result = subjects.rank(client, entries, index=args.index,
                           candidates=args.candidates, sample=args.sample,
                           threshold=args.word_like_ratio,
                           min_messages=args.min_messages)
    _out({
        "ok": True,
        "index": args.index,
        "pool_size": result["pool_size"],
        "considered": result["considered"],
        "ranked": result["ranked"][:args.size],
        "screened_out": result["screened_out"][:args.size],
        "note": (
            "Ranked by distinct descriptive mentions, not message count: a name in a "
            "recipient list masks to a document about nobody. Subjects whose surname "
            "is an ordinary word are screened out, since masking one deletes the word "
            "from the corpus."
        ),
    })


def cmd_mask_corpus(args):
    from lib.client import create_client
    from lib import corpus, masking
    client = create_client(bootstrap=False)

    entries = _load_roster_entries(args.roster)
    if not any(e["id"] == args.subject for e in entries):
        _fail(f"Subject '{args.subject}' is not in the roster.")
    aliases = masking.alias_set(entries, args.subject, min_length=args.min_variant_length)

    positives, stats = corpus.build_masked_corpus(
        client, aliases,
        source_index=args.source_index,
        masked_index=args.masked_index,
        text_field=args.text_field,
        embedding_field=args.embedding_field,
        setup_neural=not args.no_neural,
        mask_replacement=args.mask_replacement,
    )
    audit_report = corpus.audit_masked_index(
        client, aliases, index=args.masked_index,
        phone_policy=args.phone_policy, text_field=args.text_field,
        marker=args.mask_replacement, positive_count=len(positives))
    verification = corpus.verify_positives(
        client, aliases, positives, source_index=args.source_index,
        text_field=args.text_field)

    path = corpus.write_labels(args.labels_out or masking.LABELS_PATH, aliases,
                               positives, stats, audit_report, args.phone_policy)

    # Aggregates only: the positives are what the agent's judgment is meant to
    # determine, and the alias variants are the subject's own name.
    summary = {
        "ok": audit_report["passed"],
        "labels_file": path,
        "stats": stats,
        "alias_variant_count": len(aliases["variants"]),
        "positive_count": len(positives),
        "positive_verification": verification,
        "audit": audit_report,
        "note": (
            "Labels withheld from this output and written to the file above. Masking "
            "manufactures the indirect case, so results from this corpus are a proxy "
            "for naturally occurring indirect reference, not a sample of it."
        ),
    }
    if not audit_report["passed"]:
        _out({**summary, "error": "Leakage audit failed; this corpus cannot produce a score."})
        sys.exit(1)
    _out(summary)


def cmd_audit_mask(args):
    from lib.client import create_client
    from lib import corpus, masking
    client = create_client(bootstrap=False)
    labels = corpus.load_labels(args.labels or masking.LABELS_PATH)
    report = corpus.audit_masked_index(
        client, labels["aliases"], index=args.masked_index or labels["masked_index"],
        phone_policy=args.phone_policy or labels.get("phone_policy", "identification"),
        text_field=labels.get("text_field", "message"),
        marker=labels.get("mask_replacement", ""),
        positive_count=labels.get("positive_count"))
    _out({"ok": report["passed"], "masked_index": labels["masked_index"], "audit": report})
    if not report["passed"]:
        sys.exit(1)


def _positive_breakdown(client, labels, held):
    """Descriptive vs list-only counts for the score half.

    Classification needs the pre-mask text, so it reads the source index.
    """
    from lib import scoring, subjects
    texts = scoring.fetch_texts(
        client, labels["source_index"], [p["original_id"] for p in held])
    by_original = {p["original_id"]: p["doc_id"] for p in held}
    surname = subjects.surname_of(
        {"attributes": {"display_name_variants": labels["aliases"]["name_variants"]}})
    split = scoring.descriptive_split(
        [{"doc_id": oid} for oid in texts], texts, surname)
    descriptive_ids = {by_original[d["doc_id"]] for d in split["descriptive"]}
    return descriptive_ids, {
        "total": len(labels["positives"]),
        "score_half": len(held),
        "descriptive_in_score_half": len(descriptive_ids),
        "list_only_in_score_half": len(held) - len(descriptive_ids),
        "distinct_descriptive": split["distinct_descriptive"],
    }


def cmd_score_discovery(args):
    from lib.client import create_client
    from lib.discovery import discover
    from lib.model import find_deployed_model
    from lib import corpus, scoring, subjects
    client = create_client(bootstrap=False)
    labels = corpus.load_labels(args.labels)
    scoring.guard(labels, args.index)

    derive, held = scoring.split_positives(labels["positives"])
    if not held:
        _fail("No positives left to score after the split.")

    terms = scoring.usable_terms(
        scoring.significant_terms(client, args.index, [p["doc_id"] for p in derive],
                                  size=args.terms),
        labels["aliases"])
    profiles = scoring.build_profiles(terms)
    if not profiles:
        _fail("The derive half yielded no usable terms to build a profile from.")

    descriptive_ids, breakdown = _positive_breakdown(client, labels, held)
    breakdown["derive_half"] = len(derive)

    held_ids = {p["doc_id"] for p in held}
    derive_ids = {p["doc_id"] for p in derive}
    model_id = find_deployed_model(client)
    # Extend k so recall cannot be capped below 100% without saying so.
    ks = scoring.extend_ks([int(k) for k in args.ks.split(",")], len(held_ids))
    size = max(ks)

    runs = []
    for mode, mid in (("hybrid", model_id), ("bm25_only", None)):
        for i, prof in enumerate(profiles, 1):
            candidates, meta = discover(
                client, index_pattern=args.index, profile=prof["profile"],
                keywords=prof["keywords"], model_id=mid, size=size)
            ranked = [c["doc_id"] for c in candidates]
            runs.append({
                "mode": mode,
                "search_mode": meta.get("mode"),
                "wording": i,
                "recall_all_positives": scoring.recall_at_k(ranked, held_ids, ks),
                "recall_descriptive": scoring.recall_at_k(ranked, descriptive_ids, ks),
                # Same shape as the score half, so the two are comparable.
                "recall_derive_half": scoring.recall_at_k(ranked, derive_ids, ks),
            })

    top = f"recall@{max(ks)}"
    spread = {m: sorted(r["recall_descriptive"][top]["percent"]
                        for r in runs if r["mode"] == m)
              for m in ("hybrid", "bm25_only")}
    halves = {
        "score_half_percent": max(r["recall_descriptive"][top]["percent"] for r in runs),
        "derive_half_percent": max(r["recall_derive_half"][top]["percent"] for r in runs),
    }
    halves["note"] = (
        "A derive half retrieved far more often than the score half means the "
        "profile memorised specifics rather than describing a role."
    )
    _out({
        "ok": True,
        "stage": "discover",
        "positives": breakdown,
        "terms": terms[:15],
        "runs": runs,
        "spread_at_top_k": spread,
        "halves_at_top_k": halves,
        "ablation": scoring.compare_modes(runs, top),
        "headline": (
            "Read recall_descriptive: the list-only positives are documents whose "
            "only mention was a recipient list, which no profile can retrieve."
        ),
        "measures": scoring.INTERPRETATION,
        "assumptions": scoring.assumptions(labels, ks, profiles),
    })


def cmd_score_judgment(args):
    from lib.client import create_client
    from lib import corpus, scoring
    client = create_client(bootstrap=False)

    evaluations = _load_json_arg(args.evaluations)
    if isinstance(evaluations, dict):
        evaluations = evaluations.get("evaluations", [])

    if args.ground_truth:
        # Demo corpus: the answer key is a class map, not a masked label set.
        with open(args.ground_truth, encoding="utf-8") as fh:
            truth = json.load(fh)
        labels = {"subject": "demo subject", "masked_index": args.index,
                  "audit": {"passed": True, "failures": []}, "aliases": {},
                  "stats": {"documents_scanned": sum(truth["counts"].values())}}
        positive_ids = scoring.demo_positive_ids(truth)
        breakdown = {"corpus": "demo", **truth["counts"]}
        categories = scoring.per_category
    else:
        labels = corpus.load_labels(args.labels)
        scoring.guard(labels, args.index)
        truth, categories = None, None
        derive, held = scoring.split_positives(labels["positives"])
        evaluations = scoring.without(evaluations, {p["doc_id"] for p in derive})
        # Headline against the descriptive subset, as score-discovery does: the
        # list-only positives are unretrievable, so scoring against them
        # understates by construction.
        descriptive_ids, breakdown = _positive_breakdown(client, labels, held)
        positive_ids = descriptive_ids
        raw_ids = {p["doc_id"] for p in held}
    if not evaluations:
        _fail("No judgments left to score.")
    scanned = labels["stats"]["documents_scanned"]
    texts = scoring.fetch_texts(client, args.index, [e["doc_id"] for e in evaluations])

    by_threshold = {}
    for mode, threshold in scoring.PRECISION_THRESHOLDS.items():
        flagged = scoring.flagged_at(evaluations, threshold)
        label = "descriptive positives" if not truth else "positives"
        row = {"threshold": threshold,
               **scoring.precision_recall(flagged, positive_ids, scanned, label)}
        if categories:
            row["by_decoy_category"] = categories(evaluations, truth, threshold)
        else:
            row["against_all_positives"] = scoring.precision_recall(
                flagged, raw_ids, scanned, "positives including list-only")
        by_threshold[mode] = row
    _out({
        "ok": True,
        "stages": ["agent judgment + filter_flagged", "identifying_snippets"],
        "positives": breakdown,
        "judgments_scored": len(evaluations),
        "flagged_set": by_threshold,
        "spans": scoring.span_validity(evaluations, texts),
        "over_redaction": scoring.over_redaction(evaluations, texts),
        "note": (
            "One judgment pass serves every threshold; they only reread its "
            "confidence scores. Judgments on the derive half are excluded, since "
            "those documents helped write the query."
        ),
        "measures": scoring.INTERPRETATION,
        "assumptions": scoring.assumptions(labels, (), []),
    })


def cmd_score_corpus(args):
    """Score retrieval across subjects, for corpora with one document per person.

    Recall within a subject is 0% or 100% when they have a single document, so
    it says nothing. This asks, for each of many subjects, whether the held-out
    half of their record is retrieved from a profile built on the other half.
    """
    from lib.client import create_client
    from lib.discovery import discover
    from lib.model import find_deployed_model
    from lib import scoring
    client = create_client(bootstrap=False)
    if not client.indices.exists(index=args.index):
        _fail(f"Index '{args.index}' does not exist. Run seed-courtlistener first.")

    resp = client.search(index=args.index, body={
        "size": 0, "query": {"term": {"half": "a"}},
        "aggs": {"s": {"terms": {"field": "subject_id", "size": args.subjects}}}})
    subject_ids = [b["key"] for b in resp["aggregations"]["s"]["buckets"]]
    if not subject_ids:
        _fail(f"No half-'a' documents in '{args.index}'; seed with splitting enabled.")

    model_id = find_deployed_model(client)
    ks = tuple(int(k) for k in args.ks.split(","))
    size = max(ks) + 1                      # room to drop the query's own half
    trials = {"hybrid": [], "bm25_only": []}
    skipped = 0

    for subject in subject_ids:
        terms = scoring.usable_terms(
            scoring.significant_terms(client, args.index, [f"{subject}-a"],
                                      size=args.terms, min_doc_count=1,
                                      filter_duplicate_text=False),
            {"variants": []})[:8]
        if len(terms) < 3:
            skipped += 1
            continue
        profile = f"Records concerning {', '.join(terms[:5])}."
        for mode, mid in (("hybrid", model_id), ("bm25_only", None)):
            candidates, _ = discover(client, index_pattern=args.index, profile=profile,
                                     keywords=" ".join(terms), model_id=mid, size=size)
            ranked = [c["doc_id"] for c in candidates if c["doc_id"] != f"{subject}-a"]
            trials[mode].append((ranked, f"{subject}-b"))

    runs = [{"mode": mode,
              "hit_rate": scoring.hit_rate_at_k(trials[mode], ks),
              "mean_reciprocal_rank": scoring.mean_reciprocal_rank(trials[mode])}
             for mode in ("hybrid", "bm25_only")]
    top = f"hit_rate@{max(ks)}"
    _out({
        "ok": True,
        "stage": "discover, scored across subjects",
        "index": args.index,
        "subjects_scored": len(trials["hybrid"]),
        "subjects_skipped_too_few_terms": skipped,
        "runs": runs,
        "ablation": scoring.compare_modes(runs, top),
        "measures": (
            "Whether a profile built from one half of a person's record retrieves the "
            "other half. Many subjects with one document each, so the interval is tight "
            "where a single subject's recall would not be."
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

    sp = sub.add_parser("assess")
    sp.add_argument("--index", required=True)
    sp.add_argument("--text-field", default="message")
    sp.add_argument("--sample", type=int, default=500,
                    help="Documents to sample (default 500)")
    sp.set_defaults(func=cmd_assess)

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
    sp.add_argument("--size", type=int, default=100,
                    help="Candidates to retrieve (default 100). Recall rises with depth: measured 41.7%% at k=10 against 49.7%% at k=50")
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
    sp.add_argument("--precision-mode", default="high_recall",
                    choices=["strict_precision", "balanced", "high_recall"])
    sp.set_defaults(func=cmd_evaluate)

    def add_action_opts(sp):
        sp.add_argument("--evaluations", required=True, help="Inline JSON or @path")
        sp.add_argument("--candidates", default=None, help="Inline JSON or @path (enrichment)")
        sp.add_argument("--precision-mode", default="high_recall",
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

    sp = sub.add_parser("mask-corpus", help="Evaluation only: not part of the erasure workflow")
    sp.add_argument("--subject", required=True,
                    help="Roster id (email address) of the person to mask out")
    sp.add_argument("--roster", default=None,
                    help="Roster file (default gdpr-eval/enron-roster.json)")
    sp.add_argument("--source-index", default="mail-enron")
    sp.add_argument("--masked-index", default="mail-enron-masked")
    sp.add_argument("--labels-out", default=None,
                    help="Where to write the answer key (default gdpr-eval/enron-labels.json)")
    sp.add_argument("--phone-policy", default="identification",
                    choices=["identification", "leakage"],
                    help="Whether surviving phone numbers count as identification "
                         "(default) or as leakage that fails the run")
    sp.add_argument("--min-variant-length", type=int, default=3,
                    help="Shortest alias variant to mask (default 3)")
    sp.add_argument("--mask-replacement", default="",
                    help="Text left where a variant was. Empty by default: a visible "
                         "marker appears only in positives and so leaks the label set")
    sp.add_argument("--no-neural", action="store_true",
                    help="Build the masked index without deploying the embedding model")
    add_field_opts(sp)
    sp.set_defaults(func=cmd_mask_corpus)

    sp = sub.add_parser("subjects", help="Evaluation only: not part of the erasure workflow")
    sp.add_argument("--roster", default=None,
                    help="Roster file (default gdpr-eval/enron-roster.json)")
    sp.add_argument("--index", default="mail-enron")
    sp.add_argument("--candidates", type=int, default=80,
                    help="Roster entries to examine, highest volume first (default 80)")
    sp.add_argument("--sample", type=int, default=300,
                    help="Documents sampled per candidate (default 300)")
    sp.add_argument("--word-like-ratio", type=float, default=8.0,
                    help="Reject a surname this many times more common than the full name")
    sp.add_argument("--min-messages", type=int, default=20)
    sp.add_argument("--size", type=int, default=20, help="Rows to print (default 20)")
    sp.set_defaults(func=cmd_subjects)

    def add_score_opts(sp):
        sp.add_argument("--labels", default="gdpr-eval/enron-labels.json")
        sp.add_argument("--index", default="mail-enron-masked")

    sp = sub.add_parser("score-discovery", help="Evaluation only: not part of the erasure workflow")
    add_score_opts(sp)
    sp.add_argument("--ks", default="10,25,50,100,200,500")
    sp.add_argument("--terms", type=int, default=30,
                    help="Distinctive terms pulled from the derive half (default 30)")
    sp.set_defaults(func=cmd_score_discovery)

    sp = sub.add_parser("score-judgment", help="Evaluation only: not part of the erasure workflow")
    add_score_opts(sp)
    sp.add_argument("--evaluations", required=True, help="Inline JSON or @path")
    sp.add_argument("--ground-truth", default=None,
                    help="Demo answer key (gdpr-eval/demo-ground-truth.json). "
                         "Scores the demo corpus instead of an Enron label set")
    sp.set_defaults(func=cmd_score_judgment)

    sp = sub.add_parser("score-corpus", help="Evaluation only: not part of the erasure workflow")
    sp.add_argument("--index", default="case-law")
    sp.add_argument("--subjects", type=int, default=300,
                    help="Subjects to score (default 300)")
    sp.add_argument("--ks", default="1,5,10,25,50")
    sp.add_argument("--terms", type=int, default=20)
    sp.set_defaults(func=cmd_score_corpus)

    sp = sub.add_parser("audit-mask", help="Evaluation only: not part of the erasure workflow")
    sp.add_argument("--labels", default=None,
                    help="Label file (default gdpr-eval/enron-labels.json)")
    sp.add_argument("--masked-index", default=None,
                    help="Override the index recorded in the label file")
    sp.add_argument("--phone-policy", default=None,
                    choices=["identification", "leakage"])
    sp.set_defaults(func=cmd_audit_mask)

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
