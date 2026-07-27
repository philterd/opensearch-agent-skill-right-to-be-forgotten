#!/bin/bash
# Start a local single-node OpenSearch cluster in Docker for the gdpr-forget-me
# skill. Security plugin is disabled for a friction-free demo; ML Commons is
# configured so local pretrained embedding models (used for neural/hybrid
# search) can be registered and deployed on the single node.
#
# Usage: ./start_opensearch.sh
# Outputs JSON status to stdout.

set -euo pipefail

CONTAINER_NAME="${OPENSEARCH_DOCKER_CONTAINER:-gdpr-forget-me-os}"
IMAGE="${OPENSEARCH_DOCKER_IMAGE:-opensearchproject/opensearch:latest}"
PORT="${OPENSEARCH_PORT:-9200}"

if docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
    echo "{\"status\":\"already_running\",\"endpoint\":\"http://localhost:${PORT}\"}"
    exit 0
fi

docker rm -f "$CONTAINER_NAME" 2>/dev/null || true

echo "Starting OpenSearch (this pulls the image on first run)..." >&2

docker run -d \
    --pull always \
    --name "$CONTAINER_NAME" \
    -p "${PORT}:9200" \
    -p 9600:9600 \
    -e "discovery.type=single-node" \
    -e "DISABLE_SECURITY_PLUGIN=true" \
    -e "OPENSEARCH_INITIAL_ADMIN_PASSWORD=myStrongPassword123!" \
    -e "plugins.ml_commons.only_run_on_ml_node=false" \
    -e "plugins.ml_commons.allow_registering_model_via_url=true" \
    -e "plugins.ml_commons.native_memory_threshold=99" \
    -e "plugins.ml_commons.model_access_control_enabled=false" \
    "$IMAGE" >/dev/null

for _ in $(seq 1 60); do
    code=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:${PORT}" 2>/dev/null || true)
    if echo "$code" | grep -qE "200|401"; then
        echo "{\"status\":\"started\",\"endpoint\":\"http://localhost:${PORT}\"}"
        exit 0
    fi
    sleep 2
done

echo "{\"status\":\"error\",\"message\":\"OpenSearch did not start within 120 seconds\"}"
exit 1
