#!/usr/bin/env bash
# Bring the gdpr-forget-me demo environment up and leave it ready to use.
#
#   ./scripts/demo_up.sh              start, deploy the model, seed if empty
#   ./scripts/demo_up.sh --reseed     force a re-seed even if the index exists
#   LIMIT=2000 ./scripts/demo_up.sh   seed a different number of opinions
#
# Safe to run repeatedly. It never deletes the index unless you ask for
# --reseed, and it never writes to a cluster other than the local one.
#
# It exists because three steps are easy to get wrong by hand:
#
#   1. The skill's own bootstrap only checks *running* containers, so calling
#      `forget_me.py setup` while the container is stopped runs `docker rm -f`
#      and destroys the seeded index. Compose starts the existing one instead.
#   2. ML Commons keeps the model marked DEPLOYED across a restart without
#      actually loading it, so `setup` short-circuits on the stale state and
#      `discover` silently falls back to BM25. This forces a real deploy.
#   3. Deploying is asynchronous, so the cluster answers before the model can
#      serve. This waits for it and then proves hybrid search works.

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

OS_URL="${OPENSEARCH_URL:-http://localhost:9200}"
INDEX="${INDEX:-case-law}"
LIMIT="${LIMIT:-1200}"
MODEL_NAME="huggingface/sentence-transformers/all-MiniLM-L6-v2"
RESEED=false
[ "${1:-}" = "--reseed" ] && RESEED=true

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
fail() { printf '\033[31mERROR: %s\033[0m\n' "$1" >&2; exit 1; }

command -v uv >/dev/null 2>&1 || fail "uv not found. Install with: brew install uv"
command -v docker >/dev/null 2>&1 || fail "docker CLI not found."

# --- 1. Docker daemon ------------------------------------------------------
step "Checking Docker daemon"
if ! docker info >/dev/null 2>&1; then
    echo "Daemon not responding, starting Docker Desktop..."
    docker desktop start >/dev/null 2>&1 || true
    for _ in $(seq 1 60); do
        docker info >/dev/null 2>&1 && break
        sleep 2
    done
    docker info >/dev/null 2>&1 || fail "Docker daemon did not come up. Start Docker Desktop and retry."
fi
echo "Docker is running."

# --- 2. Cluster ------------------------------------------------------------
step "Starting OpenSearch (compose)"
docker compose up -d --wait
echo "Cluster healthy at ${OS_URL}"

# --- 3. Pipelines + model registration -------------------------------------
step "Creating pipelines and registering the embedding model"
uv run python scripts/forget_me.py setup >/dev/null
echo "Pipelines created."

# --- 4. Force a real model deploy ------------------------------------------
# setup trusts a stale model_state, so ask ML Commons directly and redeploy.
step "Deploying the embedding model"
# A registered model is stored as a parent document plus N chunk documents that
# share its name. Only the parent carries model_state and has no chunk_number,
# and deploying a chunk id fails, so filter the chunks out.
MODEL_ID=$(curl -sf -XPOST "${OS_URL}/_plugins/_ml/models/_search" \
    -H 'Content-Type: application/json' \
    -d "{\"query\":{\"bool\":{\"must\":[{\"term\":{\"name.keyword\":\"${MODEL_NAME}\"}},{\"exists\":{\"field\":\"model_state\"}}],\"must_not\":[{\"exists\":{\"field\":\"chunk_number\"}},{\"term\":{\"model_state\":\"DEPLOY_FAILED\"}}]}},\"size\":1,\"_source\":false}" \
    | python3 -c 'import json,sys; h=json.load(sys.stdin)["hits"]["hits"]; print(h[0]["_id"] if h else "")')

[ -n "$MODEL_ID" ] || fail "No embedding model registered. Check: uv run python scripts/forget_me.py setup"

curl -sf -XPOST "${OS_URL}/_plugins/_ml/models/${MODEL_ID}/_deploy" >/dev/null || true

for _ in $(seq 1 60); do
    STATE=$(curl -sf "${OS_URL}/_plugins/_ml/models/${MODEL_ID}" \
        | python3 -c 'import json,sys; print(json.load(sys.stdin).get("model_state",""))' 2>/dev/null || echo "")
    [ "$STATE" = "DEPLOYED" ] && break
    [ "$STATE" = "DEPLOY_FAILED" ] && fail "Model deployment failed. Check container memory (needs ~3Gb heap)."
    sleep 3
done
[ "$STATE" = "DEPLOYED" ] || fail "Model did not reach DEPLOYED (last state: ${STATE:-unknown})."
echo "Model ${MODEL_ID} deployed."

# --- 5. Corpus -------------------------------------------------------------
step "Checking the corpus"
COUNT=$(curl -sf "${OS_URL}/${INDEX}/_count" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("count",0))' 2>/dev/null || echo 0)

if [ "$RESEED" = true ] || [ "$COUNT" -eq 0 ]; then
    echo "Seeding ${LIMIT} opinions (first run downloads ~2.5Gb to gdpr-eval/, later runs are cached)..."
    uv run python scripts/forget_me.py seed-courtlistener --limit "$LIMIT" >/dev/null
    COUNT=$(curl -sf "${OS_URL}/${INDEX}/_count" \
        | python3 -c 'import json,sys; print(json.load(sys.stdin).get("count",0))')
fi
echo "Index '${INDEX}' holds ${COUNT} documents."

# --- 6. Prove hybrid search actually works ---------------------------------
step "Verifying hybrid search"
MODE=$(uv run python scripts/forget_me.py discover --index "$INDEX" \
    --profile "farmer in Nelson county 1903 1905 cropping contract reformation" \
    --size 5 2>/dev/null \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("meta",{}).get("mode","error"))')

if [ "$MODE" = "hybrid" ]; then
    echo "Hybrid search confirmed."
else
    printf '\033[33mWARNING: discover returned mode=%s, not hybrid.\033[0m\n' "$MODE"
    echo "The indirect pass will be weaker than the demo expects."
fi

printf '\n\033[32mReady.\033[0m Open DEMO-AGENT.md and paste prompt 1 to your agent.\n'
printf 'To stop, keeping the index:  docker compose stop\n'
