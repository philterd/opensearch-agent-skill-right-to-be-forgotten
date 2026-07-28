# Demo Walkthrough

The commands to run a full erasure against real Enron email, in order, and what
each one does. Run everything from the repo root.

Requires Docker, `uv`, and network access to `cs.cmu.edu`.

---

## 1. Start OpenSearch and deploy the embedding model

```bash
uv run python scripts/forget_me.py setup
```

Starts a single-node OpenSearch container (`gdpr-forget-me-os`, pulling the
image on first run), then registers and deploys the local `all-MiniLM-L6-v2`
embedding model into the cluster via ML Commons and creates the ingest and
hybrid search pipelines. No external API or API key.

Run this ahead of time. The model download and deployment is the slow step and
gives no progress output.

## 2. Check it is ready

```bash
uv run python scripts/forget_me.py status
```

Reports connectivity and whether the embedding model is deployed. You want
`"embedding_model_deployed": true`; if it is `false`, hybrid search will fall
back to BM25 only.

```json
{
  "ok": true,
  "reachable": true,
  "endpoint": "http://localhost:9200",
  "embedding_model_deployed": true,
  "model_id": "rYAQqZ8B60JNpoMBNbsE"
}
```

## 3. Index the Enron email

```bash
uv run python scripts/forget_me.py seed-enron --limit 8000
```

Streams the Enron corpus from CMU and stops as soon as it has 8,000 messages,
so it pulls a fraction of the 1.7 Gb archive rather than downloading it. Parses
each message, indexes it into `mail-enron`, and embeds it through the ingest
pipeline. The data is not stored in this repository.

Measured timings, with the model already deployed:

| `--limit` | Time | Custodians |
|---|---|---|
| 2000 (default) | 13 s | 1 (`blair-l`) |
| 8000 | 64 s | 7 |

Useful options: `--custodian blair-l` to index one mailbox, `--source` to point
at a local copy of the tarball, `--no-neural` for BM25 only, `--max-chars` to
change body truncation (default 4000).

Two things to know. The archive groups messages by custodian but not in
alphabetical order, so `--custodian` may have to stream a long way before it
matches; use `--source` with a local tarball if you filter repeatedly. And
message bodies have all whitespace runs collapsed to single spaces at ingest,
because Enron bodies are hard-wrapped at ~72 characters and redaction matches
snippets by exact substring, so a phrase straddling a newline would not match.

To inspect what gets indexed without touching OpenSearch:

```bash
uv run python scripts/seed_enron.py --dry-run --limit 5
```

## 4. Direct pass: find the literal identifier hits

```bash
uv run python scripts/forget_me.py discover-direct \
  --index "mail-enron" \
  --email "lynn.blair@enron.com" \
  --name "Lynn Blair" \
  --phone "713-853-5660" \
  --no-scan-pii \
  --size 50 > direct.json
```

Finds documents containing the subject's own identifiers. These need no agent
reasoning, so the output contains both `candidates` and ready-made
`evaluations` already flagged at confidence 1.0, with the matched values as the
redaction snippets. Pass `direct.json` straight to `plan` or `export-curl`.

Matching is case-insensitive but snippets are recorded exactly as they appear
in the document, so the request's `lynn.blair@enron.com` correctly yields the
snippet `Lynn.Blair@ENRON.com`.

Dropping `--no-scan-pii` also scans matched documents for co-located PII and
redacts that too, so a message matched on the subject's own address comes back
with everyone else's identifiers in it:

```
blair-l/customer___virginia_power_dominion/18
  snippets: ['Lynn.Blair@enron.com', 'greg_hathaway@dom.com', 'Terry.Kowalke@enron.com',
             'Gerry.Medeles@enron.com', 'Jo.Williams@enron.com', 'John.Buchanan@enron.com']
```

Decide deliberately whether you want that. Redacting third-party identifiers
from a document you were already erasing is defensible as data minimisation, but
it is broader than the request asked for and those third parties are data
subjects too. Also see the known issue in section 11 before enabling it.

## 5. Hybrid pass: find the documents that only describe the subject

```bash
uv run python scripts/forget_me.py discover \
  --index "mail-enron" \
  --profile "The gas control manager who organised the Northern Natural Gas winter operations customer meeting in Kansas City." \
  --keywords "gas control winter operations meeting Kansas City northern natural" \
  --size 20 > candidates.json
```

Runs a hybrid query: BM25 on `--keywords` plus k-NN vector search on
`--profile`, combined by the normalization pipeline. This is the pass that
surfaces records where the person is described rather than named. Check
`meta.mode`: `hybrid` if the embedding model is available, `bm25_fallback` if
not.

It over-retrieves on purpose. Scores fall off sharply (1.00, 0.81, 0.03 on this
query), and narrowing the set is the agent's job in the next step.

## 6. Evaluate the candidates

The agent reads `candidates.json`, applies the judgment prompt in `SKILL.md` to
each candidate, and writes an array of evaluation objects to
`evaluations.json`. Each records `is_identifiable`, a `confidence_score`,
verbatim `identifying_snippets`, and one sentence of reasoning.

No command to run here by default; this is the agent's reasoning step. For a
headless run with `GDPR_LLM_BASE_URL` set to an OpenAI-compatible endpoint:

```bash
uv run python scripts/forget_me.py evaluate \
  --candidates @candidates.json \
  --profile "The gas control manager who organised the Northern Natural Gas winter operations customer meeting in Kansas City." \
  --precision-mode balanced > evaluations.json
```

## 7. Preview what would change

```bash
uv run python scripts/forget_me.py plan \
  --evaluations @evaluations.json \
  --candidates @candidates.json \
  --precision-mode balanced \
  --action-type redact_in_place
```

Filters evaluations by the precision threshold and prints the exact documents
and DSL that would be affected. Writes nothing, to OpenSearch or to disk.

Thresholds: `strict_precision` 0.88, `balanced` 0.75, `high_recall` 0.60. Re-run
with a different `--precision-mode` to show the set widen or narrow.

## 8. Generate the remediation script

```bash
uv run python scripts/forget_me.py export-curl \
  --evaluations @evaluations.json \
  --candidates @candidates.json \
  --precision-mode balanced \
  --action-type redact_in_place \
  --index "mail-enron" \
  --profile "Lynn Blair, ETS Gas Control" \
  --legal-hold "billing-*,retention-*" \
  --out forget-me.sh
```

Writes `forget-me.sh`: one curl command per flagged document, targeted by exact
`(index, _id)` with the reason in a comment, followed by read-back verification
commands. Also writes a hash-chained erasure certificate to `gdpr-audit/`.

```bash
# --- blair-l/customer___virginia_power_dominion/18  (confidence 1.0)  index=mail-enron ---
# reason: Direct identifier match (email).
curl -sS $CURL_OPTS -X POST "$OS/mail-enron/_update/blair-l%2Fcustomer___virginia_power_dominion%2F18?refresh=true" \
  -H 'Content-Type: application/json' --data-binary @- <<'JSON'
{ "script": { "lang": "painless", "source": "...", "params": {
    "field": "message", "redaction": "[GDPR_REDACTED]",
    "snippets": ["Lynn.Blair@enron.com"] } } }
JSON

# --- verification (read back the affected documents) ---
curl -sS $CURL_OPTS "$OS/mail-enron/_doc/blair-l%2Fcustomer___virginia_power_dominion%2F18?_source=message"; echo
```

Bodies go through a quoted heredoc, so snippets containing quotes or apostrophes
need no shell escaping. Index names and document ids are percent-encoded, which
matters here: Enron document ids contain slashes.

Changes nothing in OpenSearch. The skill never writes to the cluster; you review
the script and run it.

`--action-type hard_delete` emits `DELETE` commands instead of Painless
redactions. `--legal-hold` takes comma-separated index globs and refuses the
export if any flagged document matches:

```bash
uv run python scripts/forget_me.py export-curl \
  --evaluations @direct.json --candidates @direct.json \
  --index "mail-enron" --legal-hold "mail-*" --out blocked.sh
```

```json
{
  "ok": false,
  "error": "Document blair-l/customer___virginia_power_dominion/18 is in 'mail-enron', which matches legal-hold pattern 'mail-*'. Erasure refused. Remove the hold or exclude this index before proceeding.",
  "legal_hold_violation": true
}
```

## 9. Review, apply, and verify

Open `forget-me.sh` and read it. Then:

```bash
bash forget-me.sh
```

Set `OPENSEARCH_URL` and `CURL_OPTS` first if you are not on the local default.
The script ends with read-back commands, or check a document directly:

```bash
curl -sS "http://localhost:9200/mail-enron/_doc/blair-l%2Fcustomer___virginia_power_dominion%2F18?_source=message" \
  | python3 -m json.tool
```

Before and after:

```
before:  ... cc: "Blair, Lynn" <Lynn.Blair@enron.com>, "Kowalke, Terry" ...
after:   ... cc: "Blair, Lynn" <[GDPR_REDACTED]>, "Kowalke, Terry" ...
```

The document survives; only the identifying snippet is replaced. Note that
`"Blair, Lynn"` remains: redaction replaces only the exact snippets supplied,
and this request gave the email address, not the inverted display-name form.

## 10. Check the audit trail

```bash
uv run python scripts/forget_me.py verify-chain
uv run python scripts/forget_me.py audit-log
```

`verify-chain` recomputes the hash chain over the certificates in `gdpr-audit/`
and reports whether it is intact. `audit-log` lists recent certificates.

To show tamper detection, edit a field in any certificate and re-run
`verify-chain`:

```json
{
  "ok": true,
  "intact": false,
  "entries": 1,
  "broken_at": 0,
  "broken_certificate": "erasure-20260728T142310442728+0000-74a2ab1a3051.json"
}
```

## 11. Known issue

The co-located PII scanner matches `HH:MM:SS` timestamps as IPv6 addresses, so
timestamps are flagged for redaction when `--no-scan-pii` is omitted. The same
pattern also misses genuine compressed IPv6 (`fe80::1`, `::1`). Use
`--no-scan-pii` until this is fixed.

## 12. Reset and teardown

Reset between runs:

```bash
rm -rf gdpr-audit candidates.json direct.json evaluations.json forget-me.sh
curl -sS -X DELETE "http://localhost:9200/mail-enron" > /dev/null
```

Tear down completely:

```bash
docker rm -f gdpr-forget-me-os
```

The Enron data lives only in the container, so removing it removes the data.

---

## Alternative: the synthetic dataset

`seed-demo` loads ~493 synthetic log documents offline in seconds, with the
ground truth recorded in `scripts/seed_demo.py`. It demonstrates indirect
identification more crisply than Enron does, and it works with no network.

```bash
uv run python scripts/forget_me.py seed-demo

uv run python scripts/forget_me.py discover \
  --index "logs-application-demo" \
  --profile "Senior frontend engineer who owned the Checkout service, sole on-call during incident #4091, resigned end of March 2024." \
  --keywords "checkout frontend incident 4091 resigned on-call" \
  --size 50 > candidates.json

uv run python scripts/forget_me.py discover-direct \
  --index "logs-application-demo" \
  --name "Jun Tanaka" --email "j.tanaka@example.com" --id "EMP-4471" > direct.json
```

From there the steps are identical, against `logs-application-demo`.
