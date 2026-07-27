# Indirect contextual identification

## The problem keyword search can't solve

Most PII tooling scans for **direct identifiers**: names, emails, phone numbers,
national IDs, IP addresses — things you can match with a regex or a dictionary.
But GDPR Recital 26 says a person is also personal data when identifiable
*indirectly*, "by reference to one or more factors specific to [their] identity."

In real logs, traces, incident reviews, chat exports, and tickets, people are
constantly described without being named:

> "the solo senior frontend engineer on-call during the #4091 outage who
> resigned at the end of March"

No name. No employee ID. Yet in an organisation with one such person, that
sentence identifies them as surely as their badge number. A regex-based PII
scanner sees nothing to redact. An erasure request handled only with keyword
search leaves this data behind — a compliance gap.

## Why hybrid (BM25 + neural) search

Finding indirect identifiers is a *semantic* retrieval problem:

- **BM25 (lexical)** catches sharp anchors — an incident number (`#4091`), a
  service name (`Checkout`), a date. High precision, but blind to paraphrase.
- **Neural / k-NN (semantic)** catches descriptions that mean the same thing in
  different words — "lead FE who owned the cart UI" vs. "senior frontend
  engineer on the checkout squad." High recall for paraphrase, but noisy.

**Hybrid search** combines them with score normalization so a candidate ranks
highly when it is *both* lexically anchored *and* semantically on-target. This
is exactly the surface where indirect identifiers live, and it is a first-class
OpenSearch capability (the `hybrid` query + `normalization-processor` search
pipeline over a k-NN vector field populated by an ML Commons `text_embedding`
ingest processor).

`gdpr-forget-me` deploys a **local pretrained** embedding model
(`sentence-transformers/all-MiniLM-L6-v2`, 384-dim) inside the cluster, so this
works on any OpenSearch distribution with no external service and no API key.

## Retrieval is not identification

Hybrid search produces *candidates* — it over-retrieves on purpose (high
recall). Deciding whether a candidate **uniquely** identifies the subject is a
reasoning task, and it is where false positives must be killed:

- "Engineer Priya Rao deployed a checkout hotfix during #4091" — names a
  *different* person; not the subject.
- "The senior frontend engineer on the **Search** team shipped autocomplete" —
  right role, wrong squad; not the subject.
- "A frontend intern joined the checkout team in June" — wrong seniority and
  timeline; not the subject.

The host agent performs this disambiguation with the judgment prompt in
`SKILL.md`, assigning a confidence score. The `precision_mode` threshold then
decides what to act on:

- **strict_precision (>=0.88)** — minimise false positives (require multiple
  converging markers). Use when over-redaction is costly.
- **balanced (>=0.75)** — default.
- **high_recall (>=0.60)** — minimise false negatives (bias toward compliance).
  Use when leaving any identifying data behind is the greater risk.

## Why redaction of *snippets*, not whole fields

Once a document is confirmed, only the **identifying substrings** are replaced
with `[GDPR_REDACTED]`. This removes the person from the record while keeping the
log line's operational meaning ("... latency exceeded 800ms ...") intact — the
minimum necessary change to satisfy erasure.
