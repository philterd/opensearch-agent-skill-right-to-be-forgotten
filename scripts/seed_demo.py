#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["opensearch-py>=2.4"]
# ///
"""Seed a synthetic multi-index log dataset for the gdpr-forget-me demo.

The story: an organisation receives a GDPR erasure request for a former
employee. His name and staff ID never appear in the logs — but he is still
*identifiable* from context:

    "The senior frontend engineer who owned the Checkout service, was the sole
     on-call during the incident #4091 outage, and resigned at the end of
     March 2024."

The dataset mixes a handful of documents that indirectly identify that person
with many decoys designed to fool naive keyword search: other engineers (named),
other incidents, other squads, and generic operational noise. Ground-truth
labels live here in the seed script, never in the index.

The labels below (``sub-``, ``dsub-``, ``dec-``, ``noise-``) are *internal* names
for the maintainer of this file. They are hashed into opaque document ids before
indexing, and the answer key is written to a separate file rather than printed,
so that an agent evaluating the corpus cannot read the labels off the candidates
it is meant to judge. See ``doc_id_for`` and ``split_ground_truth``.
"""

import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DEMO_INDEX = "logs-application-demo"

# Where the answer key is written, relative to the working directory.
GROUND_TRUTH_PATH = os.path.join("gdpr-eval", "demo-ground-truth.json")

# Namespace for the id digest. Bump it only if you want existing demo indices to
# be considered stale; every id changes with it.
_ID_NAMESPACE = "gdpr-forget-me/demo/v1"


def doc_id_for(label_id):
    """Map an internal label ('sub-1') to the opaque id used in the index.

    Class-revealing ids would defeat the evaluation: `discover` hands each
    candidate's `doc_id` to the agent, so an id of 'sub-1' or 'dec-7' announces
    its own ground-truth label before the agent judges anything. The digest is
    deterministic, so the corpus stays byte-stable across runs.
    """
    digest = hashlib.sha1(f"{_ID_NAMESPACE}:{label_id}".encode()).hexdigest()
    return f"d-{digest[:12]}"

# --- Documents that INDIRECTLY identify the subject (true positives) -------- #
SUBJECT_DOCS = [
    ("sub-1", "2024-03-28T02:14:03Z", "prod", "WARN",
     "The lead frontend engineer who owned the Checkout service logged off at "
     "02:14 during the #4091 outage, leaving the on-call rotation uncovered."),
    ("sub-2", "2024-03-29T09:30:00Z", "prod", "INFO",
     "Post-incident review #4091: the sole senior FE on duty had already filed "
     "resignation effective end of March; handover of the checkout payment "
     "widget was left incomplete."),
    ("sub-3", "2024-03-31T18:05:12Z", "iam", "INFO",
     "Access revoked for the departing checkout frontend owner who transferred "
     "ownership of the cart UI shortly before leaving the company in Q1."),
    ("sub-4", "2024-03-31T18:06:44Z", "iam", "INFO",
     "Exit deprovisioning: senior frontend engineer on the checkout squad who "
     "resigned in March 2024 - revoke VPN and production deploy keys."),
    ("sub-5", "2024-03-29T15:10:00Z", "prod", "INFO",
     "Incident #4091 retrospective action item: the checkout UI owner who had "
     "just resigned never documented the payment-widget failover procedure."),
    ("sub-6", "2024-03-25T08:00:00Z", "hr", "INFO",
     "Out-of-office set through end of March for the departing senior frontend "
     "engineer on Checkout; cart deploys paused until the role is backfilled."),
    ("sub-7", "2024-03-30T12:00:00Z", "iam", "INFO",
     "CODEOWNERS updated: removed the outgoing Checkout frontend lead, who is "
     "leaving at the end of Q1, from the cart and payment-widget code paths."),
    ("sub-8", "2024-03-27T23:40:00Z", "prod", "WARN",
     "Handover ticket: the sole frontend engineer on-call the night of incident "
     "#4091 is offboarding March 31; reassign the checkout PagerDuty rotation."),
]

# --- Direct identifiers: the subject named/emailed/id'd literally ----------- #
# The DSAR gives you these; a fast direct pass finds the easy hits, while the
# indirect subject docs above never name the person. Subject: Jun Tanaka,
# j.tanaka@example.com, EMP-4471.
DIRECT_DOCS = [
    ("dsub-1", "2024-03-31T18:04:00Z", "iam", "INFO",
     "Deprovisioning account for Jun Tanaka (EMP-4471): disable SSO and revoke tokens."),
    ("dsub-2", "2024-03-10T14:22:00Z", "prod", "INFO",
     "Deploy by j.tanaka@example.com: checkout cart-widget build 5.2 promoted to prod."),
    ("dsub-3", "2024-02-02T09:15:00Z", "web", "INFO",
     "Support ticket from customer contacting j.tanaka@example.com regarding a checkout error."),
]

# --- Decoys: superficially similar, must NOT be flagged --------------------- #
DECOY_DOCS = [
    ("dec-1", "2024-03-28T02:20:00Z", "prod", "INFO",
     "Engineer Priya Rao deployed checkout hotfix v2.3 to mitigate the #4091 outage."),
    ("dec-2", "2024-03-15T11:00:00Z", "prod", "INFO",
     "Backend engineer on the payments team resolved incident #4102 in 20 minutes."),
    ("dec-3", "2024-06-03T14:00:00Z", "hr", "INFO",
     "A frontend intern joined the checkout team in June 2024."),
    ("dec-4", "2024-03-28T02:00:00Z", "prod", "ERROR",
     "Checkout service p99 latency exceeded 800ms; circuit breaker tripped."),
    ("dec-5", "2024-03-28T05:00:00Z", "prod", "INFO",
     "Incident #4091 root cause identified: CDN cache misconfiguration on the edge."),
    ("dec-6", "2024-02-10T10:00:00Z", "web", "INFO",
     "Marketing requested a copy change to the checkout confirmation banner."),
    ("dec-7", "2024-04-05T13:30:00Z", "prod", "INFO",
     "The senior frontend engineer on the Search team shipped a new autocomplete widget."),
    ("dec-8", "2024-03-20T16:45:00Z", "hr", "INFO",
     "Staff engineer Marcus Lee announced his resignation, last day in April 2024."),
    ("dec-9", "2024-03-31T08:00:00Z", "iam", "INFO",
     "Quarterly access review completed for the platform team; no changes required."),
    ("dec-10", "2024-03-27T22:00:00Z", "prod", "WARN",
     "On-call for the checkout rotation acknowledged the #4091 page within SLA."),
    ("dec-11", "2024-03-28T02:35:00Z", "prod", "INFO",
     "SRE Dana Whitfield led the #4091 bridge call and coordinated the rollback."),
    ("dec-12", "2024-02-14T09:00:00Z", "hr", "INFO",
     "The senior backend engineer who owned the Orders service resigned in February 2024."),
    ("dec-13", "2024-03-28T03:10:00Z", "prod", "INFO",
     "A frontend engineer on the Search team was paged for #4091 but was not on the checkout rotation."),
    ("dec-14", "2024-05-02T10:00:00Z", "hr", "INFO",
     "New senior frontend engineer hired for the Checkout squad, starting May 2024."),
    ("dec-15", "2024-03-22T11:20:00Z", "prod", "INFO",
     "Checkout frontend manager approved the payment-widget redesign proposal."),
    ("dec-16", "2024-01-30T16:00:00Z", "prod", "INFO",
     "Incident #3987 postmortem: database failover on the Orders service took 12 minutes."),
    ("dec-17", "2024-03-28T04:00:00Z", "prod", "WARN",
     "Checkout service error rate returned to baseline after the #4091 CDN fix."),
    ("dec-18", "2024-04-18T13:00:00Z", "hr", "INFO",
     "Backend engineer Sofia Nunez on the payments team gave notice, last day in April."),
    ("dec-19", "2024-03-19T09:45:00Z", "prod", "INFO",
     "QA engineer completed regression testing for the checkout cart flow."),
    ("dec-20", "2024-03-28T02:05:00Z", "prod", "ERROR",
     "PaymentGateway returned 503 during the #4091 window; retries exhausted."),
    ("dec-21", "2024-03-26T17:30:00Z", "prod", "INFO",
     "The Platform team's senior frontend engineer migrated the design system to v4."),
    ("dec-22", "2024-03-11T10:00:00Z", "iam", "INFO",
     "VPN access renewed for the checkout backend on-call engineer for Q2."),
    ("dec-23", "2024-03-28T02:50:00Z", "prod", "INFO",
     "Two engineers from the checkout rotation joined the #4091 incident channel."),
    ("dec-24", "2024-06-20T14:00:00Z", "hr", "INFO",
     "A frontend contractor finished a three-month engagement on the checkout team."),
    ("dec-25", "2024-03-05T08:30:00Z", "prod", "INFO",
     "Frontend engineer Liang Chen shipped the new checkout address autocomplete."),
    ("dec-26", "2024-02-28T15:00:00Z", "prod", "INFO",
     "Incident #4055 on the Search service resolved by the platform on-call."),
    ("dec-27", "2024-03-31T19:00:00Z", "iam", "INFO",
     "Offboarding completed for a marketing manager; no engineering access to revoke."),
    ("dec-28", "2024-03-28T02:12:00Z", "prod", "WARN",
     "The checkout frontend build pipeline failed once during the #4091 outage window."),
    ("dec-29", "2024-04-10T09:00:00Z", "hr", "INFO",
     "The senior frontend engineer on the Growth team transferred to a partner org in April."),
    ("dec-30", "2024-03-24T21:00:00Z", "prod", "INFO",
     "Checkout rotation swap: two engineers exchanged on-call shifts for the last week of March."),
    ("dec-31", "2024-03-18T13:15:00Z", "prod", "INFO",
     "The backend owner of the payment widget deployed a schema migration."),
    ("dec-32", "2024-03-28T06:00:00Z", "prod", "INFO",
     "#4091 outage closed; total customer-facing impact 41 minutes on checkout."),
]

# Deterministic generic operational noise (never relevant to any person).
_NOISE_TEMPLATES = [
    ("prod", "INFO", "Nightly backup for {svc} completed in {a}m{b}s."),
    ("prod", "INFO", "Autoscaling added {a} nodes to the {svc} tier."),
    ("web", "INFO", "TLS certificate renewed for {svc}.example.com."),
    ("prod", "ERROR", "{Svc}Service timed out calling the inventory API after {a}00ms."),
    ("prod", "INFO", "Feature flag 'flag_{a}' rolled out to {b}% of traffic."),
    ("prod", "INFO", "Cron job 'reindex-{svc}' finished in {a}m{b}s."),
    ("prod", "WARN", "Disk usage on data-node-{a} reached {b}%."),
    ("web", "INFO", "A/B test 'exp-{a}' concluded; variant {v} won."),
    ("prod", "INFO", "Deployment pipeline green across {a} services."),
    ("prod", "INFO", "Cache hit ratio for {svc} steady at {b}% for the hour."),
    ("iam", "INFO", "Scheduled access review for the {svc} team found {a} stale grants."),
    ("prod", "INFO", "Kafka consumer lag on '{svc}-events' dropped to {a} messages."),
]
_NOISE_SVCS = ["orders", "search", "catalog", "inventory", "payments", "shipping",
               "recommendations", "auth", "cdn", "billing"]


def generate_noise(count):
    """Yield ``count`` deterministic noise docs (label id, source). No randomness,
    so the dataset is byte-stable and reproducible for the demo/video."""
    for i in range(count):
        svc, lvl, tmpl = _NOISE_SVCS[i % len(_NOISE_SVCS)], None, None
        service, level, template = _NOISE_TEMPLATES[i % len(_NOISE_TEMPLATES)]
        a, b = (i % 9) + 1, (i * 7) % 100
        msg = template.format(svc=svc, Svc=svc.capitalize(), a=a, b=b,
                              v="AB"[i % 2])
        month = (i % 3) + 1  # Jan-Mar 2024
        day = (i % 28) + 1
        hour = i % 24
        minute = (i * 13) % 60
        ts = f"2024-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:00Z"
        yield f"noise-{i+1}", {"@timestamp": ts, "service": service,
                               "level": level, "message": msg}

DEFAULT_NOISE_COUNT = 450


def _all_docs(noise_count):
    for label_id, ts, service, level, message in SUBJECT_DOCS + DIRECT_DOCS + DECOY_DOCS:
        yield doc_id_for(label_id), {"@timestamp": ts, "service": service,
                                     "level": level, "message": message}
    for label_id, source in generate_noise(noise_count):
        yield doc_id_for(label_id), source


def build_ground_truth(noise_count):
    """The answer key: which class each indexed document belongs to."""
    classes = {
        "subject_doc_ids": [d[0] for d in SUBJECT_DOCS],
        "direct_doc_ids": [d[0] for d in DIRECT_DOCS],
        "decoy_doc_ids": [d[0] for d in DECOY_DOCS],
        "noise_doc_ids": [f"noise-{i+1}" for i in range(noise_count)],
    }
    gt = {key: [doc_id_for(l) for l in labels] for key, labels in classes.items()}
    gt["counts"] = {key.replace("_doc_ids", ""): len(v) for key, v in gt.items()}
    # Reverse map so a maintainer can trace a flagged id back to the source doc.
    gt["label_by_doc_id"] = {doc_id_for(l): l
                             for labels in classes.values() for l in labels}
    return gt


def split_ground_truth(result, path=GROUND_TRUTH_PATH, reveal=False):
    """Move the answer key out of ``result`` and onto disk.

    ``result`` is what the CLI prints, which in this skill lands in the context
    of the agent that then evaluates the corpus. Printing the subject ids there
    contaminates any score measured from that run, so the key goes to a file the
    scoring step reads afterwards. ``reveal`` puts it back for a human who is
    deliberately inspecting the corpus.
    """
    ground_truth = result.pop("ground_truth")
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(ground_truth, fh, indent=2)
    result["ground_truth_file"] = path
    result["ground_truth_note"] = (
        "Answer key withheld from this output and written to the file above. "
        "Do not read it while evaluating: it lists which documents identify the "
        "subject, which is the question the evaluation is meant to answer."
    )
    if reveal:
        result["ground_truth"] = ground_truth
    return result


def load(client, setup_neural=True, text_field="message", embedding_field="message_embedding",
         noise_count=DEFAULT_NOISE_COUNT):
    """(Re)create the demo index and bulk-load the documents.

    The corpus is subject docs (8) + hard decoys (32) + ``noise_count`` generic
    noise docs, so it reads like a real log index while only the ~40 relevant
    docs are semantically close enough for hybrid discovery to surface.

    When setup_neural is True, the embedding model + pipelines are deployed and
    the index is k-NN enabled so full hybrid search works. Returns a summary
    The returned summary carries the answer key under ``ground_truth``; callers
    that print it must pass it through ``split_ground_truth`` first.
    """
    from lib.model import setup_neural_search, create_knn_index, INGEST_PIPELINE_ID

    if client.indices.exists(index=DEMO_INDEX):
        client.indices.delete(index=DEMO_INDEX)

    neural = None
    extra_props = {
        "@timestamp": {"type": "date"},
        "service": {"type": "keyword"},
        "level": {"type": "keyword"},
    }
    if setup_neural:
        neural = setup_neural_search(client, text_field, embedding_field)
        create_knn_index(client, DEMO_INDEX, text_field, embedding_field,
                         dim=neural["embedding_dim"], extra_properties=extra_props)
    else:
        client.indices.create(index=DEMO_INDEX, body={
            "mappings": {"properties": dict(extra_props, **{text_field: {"type": "text"}})}
        })

    docs = list(_all_docs(noise_count))
    # Bulk in chunks to keep request bodies reasonable for larger corpora.
    for start in range(0, len(docs), 500):
        lines = []
        for doc_id, source in docs[start:start + 500]:
            lines.append(json.dumps({"index": {"_index": DEMO_INDEX, "_id": doc_id}}))
            lines.append(json.dumps(source))
        resp = client.bulk(body="\n".join(lines) + "\n", refresh=True)
        if resp.get("errors"):
            first = next((i for i in resp["items"] if i.get("index", {}).get("error")), None)
            raise RuntimeError(f"Bulk load had errors: {json.dumps(first)}")

    return {
        "index": DEMO_INDEX,
        "documents_loaded": len(docs),
        "neural_search": neural is not None,
        "neural": neural,
        "ground_truth": build_ground_truth(noise_count),
        "suggested_profile": (
            "Senior frontend engineer who owned the Checkout service, was the "
            "sole on-call during the incident #4091 outage, and resigned at the "
            "end of March 2024."
        ),
        "suggested_keywords": "checkout frontend engineer incident 4091 resigned on-call",
        "suggested_identifiers": {
            "name": "Jun Tanaka", "email": "j.tanaka@example.com", "id": "EMP-4471",
        },
    }


if __name__ == "__main__":
    import argparse
    from lib.client import create_client
    ap = argparse.ArgumentParser(description="Seed the gdpr-forget-me demo dataset")
    ap.add_argument("--noise", type=int, default=DEFAULT_NOISE_COUNT)
    ap.add_argument("--no-neural", action="store_true")
    ap.add_argument("--ground-truth-out", default=GROUND_TRUTH_PATH)
    ap.add_argument("--reveal-ground-truth", action="store_true",
                    help="Also print the answer key (contaminates agent evaluation)")
    a = ap.parse_args()
    result = load(create_client(bootstrap=True), setup_neural=not a.no_neural,
                  noise_count=a.noise)
    print(json.dumps(split_ground_truth(result, a.ground_truth_out,
                                        a.reveal_ground_truth), indent=2))
