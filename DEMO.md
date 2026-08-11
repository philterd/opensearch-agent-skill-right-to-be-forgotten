# Demo Walkthrough

The commands to run a full erasure against real US court opinions, in order,
and what each one does. Run everything from the repo root.

Requires Docker, `uv`, and network access to `storage.courtlistener.com`.

Court opinions are used rather than the Enron email corpus because opinions
*describe* people: an opinion's purpose is to recount what someone did, while
calling them "the defendant". Measured with the same detector, opinions carry
8.4 descriptive references per document against Enron's 0.11, so Enron cannot
demonstrate the indirect pass at all. `seed-enron` still exists and step 4
explains how to see the difference.

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

## 3. Index the court opinions

```bash
uv run python scripts/forget_me.py seed-courtlistener --limit 1200
```

The bulk opinions export is 51 Gb compressed, so this requests a byte range of
it, decompresses the blocks that arrive, and keeps only opinions that refer to
someone by role. Captions come from the clusters export, joined by id. Both are
cached under `gdpr-eval/courtlistener/`, so the first run spends a few minutes
building the caption cache and later runs start in seconds.

```json
{
  "index": "case-law",
  "opinions_with_role_language": 1200,
  "subjects": 926,
  "indexed": 944,
  "dropped_no_person_party": 251,
  "masked": false
}
```

Documents carry the opinion text in `message` and the parties in `case_name`,
`party_surnames` and `party_given_names`. The caption is stored unindexed: it
is the naming channel, kept out of the field discovery searches.

`dropped_no_person_party` counts opinions with an institution on both sides,
which name nobody to erase.

Useful options: `--slice-mb` for how much of the export to cache, `--max-chars`
for body truncation, `--no-neural` for BM25 only. `--mask` and `--split` build
the evaluation artifact instead of the corpus and are not wanted here; see
`EVALUATION.md`.

To inspect what gets indexed without touching OpenSearch:

```bash
uv run python scripts/seed_courtlistener.py --dry-run --limit 5
```

## 4. Check whether the indirect pass applies

```bash
uv run python scripts/forget_me.py assess --index "case-law"
```

The direct pass works wherever identifiers appear literally. The indirect pass
only finds people where documents *describe* them, and corpora differ by two
orders of magnitude in whether they do. Run this before trusting an indirect
result, not after.

```json
{
  "documents_sampled": 500,
  "describing_a_person": { "documents": 481, "percent": 96.2 },
  "references_per_document": 8.35,
  "naming_channel_candidates": ["case_name", "party_given_names", "party_surnames"],
  "verdict": { "material_for_indirect_pass": "rich" }
}
```

`rich` means the indirect pass has something to find here. Run the same command
against `mail-enron` after `seed-enron` and it returns `sparse` at 0.11
references per document, which is why this walkthrough does not use it: email
records who sent what and rarely describes anyone.

The band matters when you read step 6. An empty indirect result on a `sparse` or
`absent` index says something about the corpus, not about the subject, and
`discover` repeats the verdict in its own output so the two cannot be confused.

`naming_channel_candidates` also tells you the form identifiers must take in
step 5: this corpus records parties as surnames and given names separately.

## 5. Direct pass: find the literal identifier hits

```bash
uv run python scripts/forget_me.py discover-direct \
  --index "case-law" \
  --name "Van Gordon" \
  --size 20 > direct.json
```

Finds documents containing the subject's own identifiers, in the message text
**and in the fields that record who a document is about**. `meta` reports which
fields it searched:

```json
"identity_fields_searched": ["cc", "custodian", "from", "subject", "to"]
```

They are detected from the mapping, skipping any mapped `index: false`, which
is why `case_name` appears in step 4's naming channel but not here.
`--identity-fields` overrides the list and `--no-identity-fields` searches text
only.

This is not a detail. Run the same subject against `mail-enron` and the
difference is stark: `lynn.blair@enron.com` appears in 94 message bodies and in
3,709 documents once `from`, `to` and `cc` are counted, so a text-only pass
finds 3% of that subject's footprint.

**Identifiers must match how the corpus records them.** `--name "Van Gordon"`
returns the one case; `--name "Gordon"` returns 20, because many parties share
that surname; `--name "Boyd Van Gordon"` returns nothing, because the caption
field holds surnames and given names separately and no field holds the full
string. Step 4 told you which fields exist, and this is what that was for.

These need no agent reasoning, so the output contains both `candidates` and
ready-made `evaluations` already flagged at confidence 1.0. Text matches become
`identifying_snippets`, structural matches become `identifying_fields`, and the
reasoning line says which:

```
Direct identifier match (name) in message and in party_surnames.
```

`meta.flagged_by_field_only` counts documents matched only structurally, where
the caption names the person but the opinion text calls them "the appellant"
throughout. Those are invisible to a text-only search and are exactly the
documents an erasure request must not miss. Pass `direct.json` straight to
`plan` or `export-curl`.

Matching is case-insensitive, but snippets are recorded exactly as they appear
in the document, so the redaction replaces the real string rather than the one
the request happened to use.

Supplying both forms exercises both halves. `--name "Van Gordon" --name "Gordon"`
returns 20 candidates, 4 with an identity-field hit and 2 matched only in a
field:

```
doc     : cl-6848497-6735923
reason  : Direct identifier match (name) in message and in party_surnames.
snippets: ['Van Gordon', 'Gordon']
fields  : [{'field': 'party_surnames', 'value': 'Gordon', 'matched': 'Gordon'}]
```

It also widens the net to every other Gordon in the corpus, which is the
precision and recall trade in miniature and precisely what the judgment in step
7 exists to resolve.

By default it also scans matched documents for co-located PII (other emails,
phones, IPs) and redacts that too. Court opinions carry little of it, but
operational records carry a great deal, so decide deliberately: redacting third
parties from a document you were already erasing is defensible as data
minimisation, yet it is broader than the request asked for and those third
parties are data subjects too. Pass `--no-scan-pii` to restrict redaction to
the subject's own identifiers.

## 6. Hybrid pass: find the documents that only describe the subject

```bash
uv run python scripts/forget_me.py discover \
  --index "case-law" \
  --profile "A farmer who worked a large tract of land in Nelson county between 1903 and 1905 under a cropping contract he later asked the court to reform." \
  --keywords "reformation contract farmed land Nelson county 1903 1904 1905" \
  --size 20 > candidates.json
```

The description names nobody. The top hit is the right case:

```
0.033  Spalding, J. This action was commenced for the reformation of a contract
       under which the plaintiff farmed a large amount of land in Nelson county,
       during the years 1903, 1904 and 1905, belonging to the defendant ...
```

Runs a hybrid query: BM25 on `--keywords` plus k-NN vector search on
`--profile`, combined by reciprocal rank fusion. This is the pass that surfaces
records where the person is described rather than named. Check `meta.mode`:
`hybrid` if the embedding model is available, `bm25_fallback` if not.

Fusion is RRF rather than score normalization because normalization stretches an
uninformative neural clause's noise across the full score range and averages it
into the lexical score, pushing good lexical hits down. Measured on 300 subjects
of a corpus that does describe people, that cost 30 points of hit-rate at k=5.
Set `GDPR_HYBRID_FUSION=normalization` to compare.

The output also carries an `applicability` block repeating the step 4 verdict,
so a thin result is never reported without the reason beside it:

```json
"applicability": {
  "material_for_indirect_pass": "rich",
  "references_per_document": 8.77,
  "note": "This corpus describes people; a thin result is about the subject."
}
```

On a `sparse` or `absent` index the note says the opposite, that a thin result
is about the corpus, which is the distinction a compliance reader cannot afford
to get backwards.

It over-retrieves on purpose (default `--size 100`). Narrowing the set is the
agent's job in the next step.

## 7. Evaluate the candidates

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
  --precision-mode high_recall > evaluations.json
```

## 8. Preview what would change

```bash
uv run python scripts/forget_me.py plan \
  --evaluations @evaluations.json \
  --candidates @candidates.json \
  --precision-mode high_recall \
  --action-type redact_in_place
```

Filters evaluations by the precision threshold and prints the exact documents
and DSL that would be affected. Writes nothing, to OpenSearch or to disk.

Thresholds: `strict_precision` 0.88, `balanced` 0.75, `high_recall` 0.60, which
is the default. Recall-first is deliberate: leaving someone in the index after
an erasure request is a compliance failure, while an over-flagged document is
caught by the review in step 10. Re-run with a different `--precision-mode` to
show the set widen or narrow.

The trade is not free. Over-redaction averaged 7.2% of a document and peaked at
59% in measurement, and under `hard_delete` a false positive destroys someone
else's record rather than a phrase of it. Confirm deliberately before pairing
`high_recall` with `hard_delete`.

## 9. Generate the remediation script

```bash
uv run python scripts/forget_me.py export-curl \
  --evaluations @evaluations.json \
  --candidates @candidates.json \
  --precision-mode high_recall \
  --action-type redact_in_place \
  --index "case-law" \
  --profile "Boyd Van Gordon, plaintiff in a 1900s Nelson county contract action" \
  --legal-hold "billing-*,retention-*" \
  --out forget-me.sh
```

Writes `forget-me.sh`: one curl command per flagged document, targeted by exact
`(index, _id)` with the reason in a comment, followed by read-back verification
commands. Also writes a hash-chained erasure certificate to `gdpr-audit/`.

```bash
# --- cl-6848497-6735923  (confidence 1.0)  index=case-law ---
# reason: Direct identifier match (name) in message and in party_surnames.
curl -sS $CURL_OPTS -X POST "$OS/case-law/_update/cl-6848497-6735923?refresh=true" \
  -H 'Content-Type: application/json' --data-binary @- <<'JSON'
{ "script": { "lang": "painless", "source": "...", "params": {
    "field": "message", "redaction": "[GDPR_REDACTED]",
    "snippets": ["Van Gordon", "Gordon"] } } }
JSON
# identity fields: party_surnames
curl -sS $CURL_OPTS -X POST "$OS/case-law/_update/cl-6848497-6735923?refresh=true" \
  -H 'Content-Type: application/json' --data-binary @- <<'JSON'
{ "script": { "lang": "painless", "source": "...", "params": {
    "redaction": "[GDPR_REDACTED]",
    "fields": { "party_surnames": ["Gordon"] } } } }
JSON

# --- verification (read back the affected documents) ---
curl -sS $CURL_OPTS "$OS/case-law/_doc/cl-6848497-6735923?_source=message%2Cparty_surnames"; echo
```

A document matching in both text and identity fields gets two updates. The text
script replaces the snippets; the field script replaces the whole field value,
because `Lynn Blair <lynn.blair@enron.com>` is personal data in both halves. In
an array such as `to`, the matching element is replaced and the list keeps its
length, since a shortened recipient list leaks the fact that someone was removed.
Matching is case-insensitive there: headers vary in case where text does not.

Bodies go through a quoted heredoc, so snippets containing quotes or apostrophes
need no shell escaping. Index names and document ids are percent-encoded, which
matters on corpora whose ids contain slashes, as Enron's do.

Changes nothing in OpenSearch. The skill never writes to the cluster; you review
the script and run it.

`--action-type hard_delete` emits `DELETE` commands instead of Painless
redactions. `--legal-hold` takes comma-separated index globs and refuses the
export if any flagged document matches:

```bash
uv run python scripts/forget_me.py export-curl \
  --evaluations @direct.json --candidates @direct.json \
  --index "case-law" --legal-hold "case-*" --out blocked.sh
```

```json
{
  "ok": false,
  "error": "Document cl-6848497-6735923 is in 'case-law', which matches legal-hold pattern 'case-*'. Erasure refused. Remove the hold or exclude this index before proceeding.",
  "legal_hold_violation": true
}
```

## 10. Review, apply, and verify

Open `forget-me.sh` and read it. Then:

```bash
bash forget-me.sh
```

Set `OPENSEARCH_URL` and `CURL_OPTS` first if you are not on the local default.
The script ends with read-back commands, or check a document directly:

```bash
curl -sS "http://localhost:9200/case-law/_doc/cl-6848497-6735923?_source=message,party_surnames" \
  | python3 -m json.tool
```

Before and after:

```
message before:        ... the plaintiff, Boyd Van Gordon, farmed a large amount of land ...
message after:         ... the plaintiff, Boyd [GDPR_REDACTED], farmed a large amount of land ...

party_surnames before: ["Gordon", "Baird"]
party_surnames after:  ["[GDPR_REDACTED]", "Baird"]
```

The document survives, and the surrounding account of the contract dispute is
untouched. Two things are worth noticing.

In the text field only the exact snippets supplied are replaced, so `"Boyd"`
remains: the request gave surname forms, not the given name. Supply `--name
"Boyd"` to catch it. This is the same lesson as step 5 in a different guise, and
it is why an erasure request should carry every form of the name.

In `party_surnames` the whole element is replaced and the array keeps its
length, because a shortened list would itself reveal that someone was removed.

## 11. Check the audit trail

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

## 12. Reset and teardown

Reset between runs:

```bash
rm -rf gdpr-audit candidates.json direct.json evaluations.json forget-me.sh
curl -sS -X DELETE "http://localhost:9200/case-law" > /dev/null
```

The downloaded corpus stays in `gdpr-eval/courtlistener/` so a re-run starts in
seconds. Delete that directory too if you want the disk back; it is gitignored
and re-fetched on demand.

Tear down completely:

```bash
docker rm -f gdpr-forget-me-os
```

The indexed data lives only in the container, so removing it removes the data.
The cached corpus under `gdpr-eval/` survives; delete it separately.

---

## Alternative: the synthetic dataset

`seed-demo` loads ~493 synthetic log documents offline in seconds, with the
ground truth recorded in `scripts/seed_demo.py`. It works with no network, and
because its documents are written to describe people and its decoys vary one
marker each, it is the corpus where judgment quality is measurable rather than
merely demonstrable.

Document ids are opaque digests and the answer key is written to
`gdpr-eval/demo-ground-truth.json` rather than printed, so an agent working the
request cannot read the labels off the candidates it is judging. Pass
`--reveal-ground-truth` when you want to inspect the corpus yourself, but not on
a run you intend to score.

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
