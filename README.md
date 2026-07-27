# GDPR Forget-Me for OpenSearch

An OpenSearch Agent Skill that fulfils GDPR "right to be forgotten" requests, including the data that keyword-based PII tools miss: people who are identifiable without their name ever appearing.

## The problem

When an organisation receives a GDPR erasure ("right to be forgotten") or CCPA
deletion request, some of the data is easy to find. Where the person is named or
their email appears, a regex or dictionary scan matches it directly:

> *"Deploy by j.tanaka@example.com: checkout cart-widget build 5.2 promoted to prod"*

`gdpr-forget-me` does exactly that with its direct-identifier pass, and it is fast
and exact:

```bash
$ uv run python scripts/forget_me.py discover-direct \
    --index "logs-application-*" --email "j.tanaka@example.com"
# flags the document, snippet ["j.tanaka@example.com"], confidence 1.0
```

The hard part is the indirect personal data scattered through logs, traces,
incident reviews, and tickets. GDPR Recital 26 is explicit that a person is
personal data whenever they are identifiable indirectly, "by reference to one or
more factors specific to their identity." In practice that looks like:

> *"the solo senior frontend engineer on-call during the #4091 outage who
> resigned at the end of March"*

No name, no employee ID, or other direct identifier. Direct scanning alone leaves this
data behind, and the organisation is out of compliance without knowing it. This
is the gap the skill closes: it runs the direct pass and then adds hybrid search
plus agent reasoning to find the records where the person is only described.

## What this skill does

`gdpr-forget-me` turns any Agent-Skills-compatible IDE (Claude Code, Cursor,
Kiro, Copilot, Windsurf, Gemini CLI, Codex) into a privacy-engineering agent that:

1. Discovers candidate documents two ways: a direct-identifier pass (exact name,
   email, employee id, phone, IP) for the literal hits, and hybrid BM25 and
   neural/vector search so paraphrased, name-free descriptions are surfaced too.
2. Disambiguates each candidate by reasoning (agent-native) whether it uniquely
   identifies the subject, scoring confidence and extracting the exact
   identifying snippets. A `precision_mode` threshold (`strict_precision`,
   `balanced`, or `high_recall`) controls precision versus recall.
3. Previews the exact documents and DSL before anything is written.
4. Remediates with the least-destructive option: `redact_in_place` (replace only
   the identifying snippets with `[GDPR_REDACTED]`, preserving operational logs)
   or `hard_delete`. It never writes to the cluster itself; it emits a reviewable
   curl script the human runs, and legal-hold indices are refused.
5. Verifies every targeted document: the generated curl script includes read-back
   commands to confirm each snippet is gone or the document is deleted after you
   run it.
6. Records every run in a local, hash-chained erasure certificate written at
   generation time (no cluster writes), the evidence GDPR Art. 5(2) and Art. 30
   require you to produce.

## Quickstart

Requires `uv` and, for the local demo, Docker.

```bash
# Install into your agent (Claude Code / Cursor / Kiro / ...)
npx skills add philterd/opensearch-agent-right-to-be-forgotten

# ...or clone and point your agent at this directory.
```

Then ask your agent, for example:

> "We got a GDPR erasure request. Scrub the senior frontend engineer who owned
> Checkout, was sole on-call during incident #4091, and resigned end of March
> 2024, from `logs-application-*`. Redact, don't delete."

### Try the built-in demo end to end

```bash
# 1. Start OpenSearch + deploy the local embedding model + load the demo data
uv run python scripts/forget_me.py seed-demo

# 2. Discover candidates (hybrid search) — over-retrieves on purpose
uv run python scripts/forget_me.py discover \
  --index "logs-application-demo" \
  --profile "Senior frontend engineer who owned the Checkout service, sole on-call during incident #4091, resigned end of March 2024." \
  --keywords "checkout frontend incident 4091 resigned on-call" \
  --size 50 > candidates.json

# 2b. Direct pass: find the easy literal hits from identifiers in the request
uv run python scripts/forget_me.py discover-direct \
  --index "logs-application-demo" \
  --name "Jun Tanaka" --email "j.tanaka@example.com" --id "EMP-4471" > direct.json

# 3. The agent evaluates the hybrid candidates with the judgment prompt in
#    SKILL.md and writes evaluations.json (or use `evaluate` with GDPR_LLM_BASE_URL).
#    direct.json is already flagged and needs no agent step; act on both sets.

# 4. Preview the filtered plan (writes nothing)
uv run python scripts/forget_me.py plan \
  --evaluations @evaluations.json --candidates @candidates.json \
  --precision-mode balanced --action-type redact_in_place

# 5. Emit reviewable curl commands + a local erasure certificate.
#    Nothing is changed in OpenSearch; the skill never writes to the cluster.
uv run python scripts/forget_me.py export-curl \
  --evaluations @evaluations.json --candidates @candidates.json \
  --precision-mode balanced --action-type hard_delete \
  --index "logs-application-demo" --profile "..." --out forget-me.sh

# 6. Review forget-me.sh, then run it yourself to apply
bash forget-me.sh

# 7. Confirm the certificate chain (reads local files only)
uv run python scripts/forget_me.py verify-chain
uv run python scripts/forget_me.py audit-log
```

The demo dataset is about 493 log documents: one subject identified indirectly
across 8 of them (described but never named), 3 more that name the subject
directly for the direct-identifier pass, roughly 32 hard decoys engineered to
fool keyword search (other named engineers, other incidents, the same role on a
different squad, on-call rotation members, a same-role intern with the wrong
timeline), and about 450 generic noise entries so the corpus reads like a real
log index. Use `--noise` on `seed-demo` to change the volume. Watching hybrid
search pull the relevant candidates out of the haystack and the agent's reasoning
reject the decoys is the story for the 5-minute video.

## What export-curl produces

`export-curl` writes a self-contained, reviewable bash script and changes nothing
in the cluster until you run it yourself. Each flagged document gets one precise
`(index, _id)` command, followed by read-back verification. It also writes a
local, hash-chained erasure certificate under `gdpr-audit/` recording exactly
what the script will erase. Example for `--action-type hard_delete`, trimmed to
two documents:

```bash
#!/usr/bin/env bash
# GDPR Forget-Me for OpenSearch — remediation commands. REVIEW BEFORE RUNNING.
# Nothing in OpenSearch has been modified by generating this file.
# Action: hard_delete   Documents: 8
# Target profile: Senior frontend engineer who owned the Checkout service ...
# Precision mode: balanced
#
# Set OPENSEARCH_URL to your endpoint. Add auth if your cluster requires it,
# e.g.  export CURL_OPTS='-u admin:yourPassword -k'
set -euo pipefail
OS="${OPENSEARCH_URL:-http://localhost:9200}"
CURL_OPTS="${CURL_OPTS:-}"

# --- sub-1  (confidence 0.92)  index=logs-application-demo ---
# reason: Lead frontend engineer who owned Checkout, off during the #4091 outage.
curl -sS $CURL_OPTS -X DELETE "$OS/logs-application-demo/_doc/sub-1?refresh=true"

# --- sub-8  (confidence 0.9)  index=logs-application-demo ---
# reason: Sole frontend engineer on-call the night of #4091, offboarding March 31.
curl -sS $CURL_OPTS -X DELETE "$OS/logs-application-demo/_doc/sub-8?refresh=true"

# ... one DELETE per flagged document ...

# --- verification (read back the affected documents) ---
curl -sS $CURL_OPTS "$OS/logs-application-demo/_doc/sub-1?_source=message"; echo
```

For `--action-type redact_in_place`, each command is a `POST .../_update/<id>`
whose body is passed through a quoted heredoc, so snippets containing quotes or
apostrophes need no shell escaping:

```bash
# --- sub-1  (confidence 0.92)  index=logs-application-demo ---
curl -sS $CURL_OPTS -X POST "$OS/logs-application-demo/_update/sub-1?refresh=true" \
  -H 'Content-Type: application/json' --data-binary @- <<'JSON'
{
  "script": {
    "lang": "painless",
    "source": "if (ctx._source.containsKey(params.field) && ctx._source[params.field] != null) { String v = ctx._source[params.field].toString(); for (s in params.snippets) { v = v.replace(s, params.redaction); } ctx._source[params.field] = v; }",
    "params": {
      "field": "message",
      "redaction": "[GDPR_REDACTED]",
      "snippets": ["lead frontend engineer who owned the Checkout service"]
    }
  }
}
JSON
```

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `OPENSEARCH_HOST` / `OPENSEARCH_PORT` | `localhost` / `9200` | Cluster endpoint |
| `OPENSEARCH_URL` | `http://<host>:<port>` | Endpoint baked into the exported curl script |
| `OPENSEARCH_AUTH_MODE` | `default` | `default`, `none`, or `custom` |
| `OPENSEARCH_USER` / `OPENSEARCH_PASSWORD` | none | Used when auth mode is `custom` |
| `GDPR_AUDIT_DIR` | `gdpr-audit` | Where erasure certificates are written |
| `GDPR_ACTOR` | OS user | Recorded as the actor in the audit trail |
| `GDPR_LLM_BASE_URL` / `GDPR_LLM_MODEL` / `GDPR_LLM_API_KEY` | none | Optional headless evaluator (OpenAI-compatible) |

## Safety

The skill never writes to the cluster. It emits a reviewable curl script that the
human inspects and runs. `export-curl` refuses any index matching a
`--legal-hold` pattern, and every generated command targets documents by exact
`(index, _id)` rather than a blind query. Each run also writes a local,
hash-chained erasure certificate (no cluster writes) recording exactly what the
script will erase, and the script itself carries read-back verification commands.
See the Safety rules in `SKILL.md`.

## This is not legal advice

`gdpr-forget-me` is an illustrative example of how one might automate "forgetting"
a user's personal data in OpenSearch: the discovery, review, redaction or
deletion, verification, and audit mechanics. It is not legal advice, not a
certified compliance product, and not a guarantee of GDPR, CCPA, or any other
regulatory compliance.

Whether a given erasure is required, permitted, or complete, and which data,
retention obligations, and legal holds apply, is a legal determination. Consult
your Data Protection Officer and legal counsel, validate the behavior against
your own obligations and data, and treat the demo dataset and outputs as a
worked example rather than a turnkey solution. You are responsible for anything
you run against your own clusters.
