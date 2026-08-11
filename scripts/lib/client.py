"""OpenSearch client creation, connectivity checks, and local Docker bootstrap.

Adapted from the opensearch-agent-skills client conventions so this skill
behaves identically to the rest of the ecosystem: same env vars, same auth
modes, same default local credentials. Pure opensearch-py — no proprietary
dependencies, works against any OpenSearch distribution (local, self-managed,
Amazon OpenSearch Service, or Serverless).
"""

import os
import platform
import shutil
import subprocess
import sys
import time

from opensearchpy import OpenSearch

OPENSEARCH_HOST = os.getenv("OPENSEARCH_HOST", "localhost")
OPENSEARCH_PORT = int(os.getenv("OPENSEARCH_PORT", "9200"))
OPENSEARCH_DEFAULT_USER = "admin"
OPENSEARCH_DEFAULT_PASSWORD = "myStrongPassword123!"
OPENSEARCH_DOCKER_IMAGE = os.getenv(
    "OPENSEARCH_DOCKER_IMAGE", "opensearchproject/opensearch:latest"
)
OPENSEARCH_DOCKER_CONTAINER = os.getenv("OPENSEARCH_DOCKER_CONTAINER", "gdpr-forget-me-os")
OPENSEARCH_DOCKER_START_TIMEOUT = int(os.getenv("OPENSEARCH_DOCKER_START_TIMEOUT", "120"))
# The image defaults to a 1Gb heap. Deploying the embedding model leaves heap
# use above the ML Commons jvm_heap_memory_threshold (85% by default), so every
# inference call is refused with a circuit_breaking_exception: model deploy or
# the first neural bulk request dies. Raising the heap clears it; raising the
# threshold instead would trade a clean refusal for a real OOM.
#
# Measured on this image: 1Gb sits at 92% and 2Gb at 93%, both refused. 3Gb
# deploys the model and ingests 2000 embedded documents, ending at 11%. Override
# for a larger corpus or a memory-constrained Docker VM.
OPENSEARCH_JAVA_OPTS = os.getenv("OPENSEARCH_JAVA_OPTS", "-Xms3g -Xmx3g")

_AUTH_FAILURE_TOKENS = (
    "401", "403", "unauthorized", "forbidden",
    "authentication", "security_exception",
    "missing authentication credentials",
)


def _normalize(value: object) -> str:
    return "" if value is None else " ".join(str(value).split())


def resolve_http_auth() -> "tuple[str, str] | None":
    """Resolve basic-auth credentials from the environment.

    OPENSEARCH_AUTH_MODE: default (admin creds) | none (no auth) | custom
    (OPENSEARCH_USER + OPENSEARCH_PASSWORD).
    """
    mode = os.getenv("OPENSEARCH_AUTH_MODE", "default").strip().lower()
    if mode == "none":
        return None
    if mode == "custom":
        user = os.getenv("OPENSEARCH_USER", "").strip()
        password = os.getenv("OPENSEARCH_PASSWORD", "").strip()
        if not user or not password:
            raise RuntimeError(
                "OPENSEARCH_AUTH_MODE=custom requires OPENSEARCH_USER and OPENSEARCH_PASSWORD."
            )
        return user, password
    return OPENSEARCH_DEFAULT_USER, OPENSEARCH_DEFAULT_PASSWORD


def build_client(use_ssl: bool, http_auth: "tuple[str, str] | None" = None) -> OpenSearch:
    # TLS verification is disabled only for loopback hosts (self-signed dev
    # certs). For non-loopback hosts, certificates are verified to prevent
    # credential theft via MITM.
    is_local = (
        OPENSEARCH_HOST in ("localhost", "127.0.0.1", "::1")
        or OPENSEARCH_HOST.startswith("127.")
    )
    verify = use_ssl and not is_local
    kwargs = {
        "hosts": [{"host": OPENSEARCH_HOST, "port": OPENSEARCH_PORT}],
        "use_ssl": use_ssl,
        "verify_certs": verify,
        "ssl_show_warn": verify,
        "timeout": 60,
    }
    if http_auth is not None:
        kwargs["http_auth"] = http_auth
    return OpenSearch(**kwargs)


def can_connect(client: OpenSearch) -> "tuple[bool, bool]":
    """Return (reachable, auth_failure)."""
    try:
        client.info()
        return True, False
    except Exception as e:  # noqa: BLE001 - probing, any error is informative
        lowered = _normalize(e).lower()
        if "404" in lowered or "notfounderror" in lowered:
            try:
                client.cat.indices(format="json")
                return True, False
            except Exception:
                pass
        auth_failure = any(t in lowered for t in _AUTH_FAILURE_TOKENS)
        return False, auth_failure


# --------------------------------------------------------------------------- #
# Local Docker bootstrap                                                       #
# --------------------------------------------------------------------------- #

def _resolve_docker() -> str:
    system = platform.system().lower()
    candidates = {
        "darwin": [
            "/usr/local/bin/docker",
            "/opt/homebrew/bin/docker",
            "/Applications/Docker.app/Contents/Resources/bin/docker",
        ],
        "linux": ["/usr/bin/docker", "/usr/local/bin/docker", "/snap/bin/docker"],
    }.get(system, [])

    from_env = os.getenv("OPENSEARCH_DOCKER_CLI_PATH", "").strip()
    if from_env:
        candidates.insert(0, from_env)
    from_path = shutil.which("docker")
    if from_path:
        candidates.insert(0, from_path)

    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    raise RuntimeError("Docker CLI not found. Install Docker or set OPENSEARCH_DOCKER_CLI_PATH.")


def _run_docker(command: "list[str]", timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_resolve_docker()] + command, capture_output=True, text=True, timeout=timeout
    )


def _start_local_container() -> None:
    result = _run_docker(["ps", "--format", "{{.Names}}"])
    if OPENSEARCH_DOCKER_CONTAINER in (result.stdout or "").split():
        print(f"Container '{OPENSEARCH_DOCKER_CONTAINER}' already running.", file=sys.stderr)
        return

    _run_docker(["rm", "-f", OPENSEARCH_DOCKER_CONTAINER])
    # Pull first, with a generous timeout — the image is ~1GB and the initial
    # pull can far exceed the default docker timeout on a cold cache.
    print("Pulling OpenSearch image (first run only, may take a few minutes)...", file=sys.stderr)
    pull = _run_docker(["pull", OPENSEARCH_DOCKER_IMAGE], timeout=600)
    if pull.returncode != 0:
        raise RuntimeError(f"Failed to pull image {OPENSEARCH_DOCKER_IMAGE}: {pull.stderr}")
    print(f"Starting OpenSearch container '{OPENSEARCH_DOCKER_CONTAINER}'...", file=sys.stderr)
    # Security plugin disabled for a friction-free local demo. ML Commons is
    # configured to allow registering local pretrained models on any node so
    # neural/hybrid search works out of the box.
    result = _run_docker([
        "run", "-d",
        "--name", OPENSEARCH_DOCKER_CONTAINER,
        "-p", f"{OPENSEARCH_PORT}:9200",
        "-p", "9600:9600",
        "-e", "discovery.type=single-node",
        "-e", "DISABLE_SECURITY_PLUGIN=true",
        "-e", "OPENSEARCH_INITIAL_ADMIN_PASSWORD=" + OPENSEARCH_DEFAULT_PASSWORD,
        "-e", "OPENSEARCH_JAVA_OPTS=" + OPENSEARCH_JAVA_OPTS,
        "-e", "plugins.ml_commons.only_run_on_ml_node=false",
        "-e", "plugins.ml_commons.allow_registering_model_via_url=true",
        "-e", "plugins.ml_commons.native_memory_threshold=99",
        "-e", "plugins.ml_commons.model_access_control_enabled=false",
        OPENSEARCH_DOCKER_IMAGE,
    ])
    if result.returncode != 0:
        raise RuntimeError(f"Failed to start container: {result.stderr}")


def _wait_for_cluster() -> OpenSearch:
    http_auth = resolve_http_auth()
    secure = build_client(use_ssl=True, http_auth=http_auth)
    insecure = build_client(use_ssl=False, http_auth=http_auth)
    deadline = time.time() + OPENSEARCH_DOCKER_START_TIMEOUT
    while time.time() < deadline:
        for client in (insecure, secure):
            ok, _ = can_connect(client)
            if ok:
                return client
        time.sleep(2)
    raise RuntimeError(
        f"OpenSearch did not become ready within {OPENSEARCH_DOCKER_START_TIMEOUT}s."
    )


def create_client(bootstrap: bool = True) -> OpenSearch:
    """Return a connected client, bootstrapping a local Docker cluster if needed.

    Set bootstrap=False to fail fast instead of starting a container.
    """
    http_auth = resolve_http_auth()
    for use_ssl in (False, True):
        client = build_client(use_ssl=use_ssl, http_auth=http_auth)
        ok, auth_fail = can_connect(client)
        if ok:
            return client
        if auth_fail:
            raise RuntimeError(
                f"Authentication failed connecting to OpenSearch at "
                f"{OPENSEARCH_HOST}:{OPENSEARCH_PORT}. Check OPENSEARCH_AUTH_MODE/credentials."
            )
    if not bootstrap:
        raise RuntimeError(
            f"No OpenSearch cluster reachable at {OPENSEARCH_HOST}:{OPENSEARCH_PORT}."
        )
    _start_local_container()
    return _wait_for_cluster()


def endpoint_label(client: OpenSearch) -> str:
    try:
        info = client.info()
        return f"{OPENSEARCH_HOST}:{OPENSEARCH_PORT} (v{info['version']['number']})"
    except Exception:
        return f"{OPENSEARCH_HOST}:{OPENSEARCH_PORT}"
