"""Vendor-neutral neural search setup via OpenSearch ML Commons.

Registers and deploys a *local pretrained* embedding model (default:
sentence-transformers/all-MiniLM-L6-v2) so hybrid BM25 + k-NN search works on
any OpenSearch distribution with ZERO cloud dependencies. No Bedrock, no
OpenAI, no API keys. The model runs inside the cluster.

All ML Commons calls go through the REST plugin API via
``client.transport.perform_request`` so we don't depend on any plugin-specific
Python client.
"""

import time

# Local pretrained model shipped with OpenSearch ML Commons. TORCH_SCRIPT
# format, 384-dim embeddings, Apache-2.0 friendly, no external download at
# query time. See https://opensearch.org/docs/latest/ml-commons-plugin/pretrained-models/
DEFAULT_MODEL_NAME = "huggingface/sentence-transformers/all-MiniLM-L6-v2"
DEFAULT_MODEL_VERSION = "1.0.1"
DEFAULT_MODEL_FORMAT = "TORCH_SCRIPT"
DEFAULT_EMBEDDING_DIM = 384

INGEST_PIPELINE_ID = "gdpr-forget-me-embed"
SEARCH_PIPELINE_ID = "gdpr-forget-me-hybrid"

# Task lifecycle: CREATED -> RUNNING -> COMPLETED. Only COMPLETED carries the
# resulting model_id, so it is the sole success state; CREATED/RUNNING mean
# "keep polling".
_TERMINAL_OK = {"COMPLETED"}
_TERMINAL_BAD = {"FAILED", "COMPLETED_WITH_ERROR", "CANCELLED"}


def _req(client, method, path, body=None):
    return client.transport.perform_request(method, path, body=body)


def ensure_ml_settings(client) -> None:
    """Apply persistent ML Commons settings.

    Idempotent. Needed when running against a cluster we did not bootstrap
    ourselves (the local Docker bootstrap already sets these at startup).
    """
    _req(client, "PUT", "/_cluster/settings", {
        "persistent": {
            "plugins.ml_commons.only_run_on_ml_node": False,
            "plugins.ml_commons.allow_registering_model_via_url": True,
            "plugins.ml_commons.native_memory_threshold": 99,
            "plugins.ml_commons.model_access_control_enabled": False,
        }
    })


def _wait_for_task(client, task_id, timeout=300):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = _req(client, "GET", f"/_plugins/_ml/tasks/{task_id}")
        state = last.get("state")
        if state in _TERMINAL_OK:
            return last
        if state in _TERMINAL_BAD:
            raise RuntimeError(f"ML task {task_id} failed: {last}")
        time.sleep(3)
    raise RuntimeError(f"ML task {task_id} did not complete within {timeout}s (last={last})")


def find_deployed_model(client, model_name=DEFAULT_MODEL_NAME):
    """Return the model_id of an already-deployed model with this name, or None."""
    try:
        resp = _req(client, "POST", "/_plugins/_ml/models/_search", {
            "size": 5,
            "query": {
                "bool": {
                    "must": [
                        {"term": {"name.keyword": model_name}},
                        {"term": {"model_state": "DEPLOYED"}},
                    ]
                }
            },
        })
    except Exception:
        return None
    for hit in resp.get("hits", {}).get("hits", []):
        mid = hit.get("_id")
        if mid:
            return mid
    return None


def register_and_deploy_model(
    client,
    model_name=DEFAULT_MODEL_NAME,
    model_version=DEFAULT_MODEL_VERSION,
    model_format=DEFAULT_MODEL_FORMAT,
):
    """Register + deploy the embedding model. Idempotent; returns model_id."""
    existing = find_deployed_model(client, model_name)
    if existing:
        return existing

    ensure_ml_settings(client)

    register = _req(client, "POST", "/_plugins/_ml/models/_register", {
        "name": model_name,
        "version": model_version,
        "model_format": model_format,
    })
    task = _wait_for_task(client, register["task_id"])
    model_id = task.get("model_id")
    if not model_id:
        raise RuntimeError(f"Registration returned no model_id: {task}")

    deploy = _req(client, "POST", f"/_plugins/_ml/models/{model_id}/_deploy")
    _wait_for_task(client, deploy["task_id"])
    return model_id


def create_embedding_pipeline(
    client, model_id, text_field, embedding_field, pipeline_id=INGEST_PIPELINE_ID
):
    """Create an ingest pipeline that embeds ``text_field`` into ``embedding_field``."""
    _req(client, "PUT", f"/_ingest/pipeline/{pipeline_id}", {
        "description": "gdpr-forget-me: embed text for neural search",
        "processors": [
            {
                "text_embedding": {
                    "model_id": model_id,
                    "field_map": {text_field: embedding_field},
                }
            }
        ],
    })
    return pipeline_id


def create_hybrid_search_pipeline(client, pipeline_id=SEARCH_PIPELINE_ID):
    """Create a search pipeline with the normalization processor for hybrid queries.

    Combines BM25 and neural scores with min-max normalization and an
    arithmetic-mean combination (0.4 lexical / 0.6 semantic) — biased toward
    semantic recall, which is what indirect identification depends on.
    """
    _req(client, "PUT", f"/_search/pipeline/{pipeline_id}", {
        "description": "gdpr-forget-me: hybrid BM25 + neural normalization",
        "phase_results_processors": [
            {
                "normalization-processor": {
                    "normalization": {"technique": "min_max"},
                    "combination": {
                        "technique": "arithmetic_mean",
                        "parameters": {"weights": [0.4, 0.6]},
                    },
                }
            }
        ],
    })
    return pipeline_id


def create_knn_index(
    client, index, text_field, embedding_field, dim=DEFAULT_EMBEDDING_DIM,
    extra_properties=None,
):
    """Create a k-NN enabled index whose text field is auto-embedded on ingest."""
    if client.indices.exists(index=index):
        return index
    properties = {
        text_field: {"type": "text"},
        embedding_field: {
            "type": "knn_vector",
            "dimension": dim,
            "method": {
                "name": "hnsw",
                "space_type": "cosinesimil",
                "engine": "lucene",
            },
        },
    }
    if extra_properties:
        properties.update(extra_properties)
    client.indices.create(index=index, body={
        "settings": {
            "index.knn": True,
            "default_pipeline": INGEST_PIPELINE_ID,
        },
        "mappings": {"properties": properties},
    })
    return index


def setup_neural_search(client, text_field, embedding_field):
    """One-shot: deploy model + create ingest & search pipelines.

    Returns a dict describing the deployed resources. Safe to call repeatedly.
    """
    model_id = register_and_deploy_model(client)
    create_embedding_pipeline(client, model_id, text_field, embedding_field)
    create_hybrid_search_pipeline(client)
    return {
        "model_id": model_id,
        "model_name": DEFAULT_MODEL_NAME,
        "embedding_dim": DEFAULT_EMBEDDING_DIM,
        "ingest_pipeline": INGEST_PIPELINE_ID,
        "search_pipeline": SEARCH_PIPELINE_ID,
        "text_field": text_field,
        "embedding_field": embedding_field,
    }
