"""Local, tamper-evident erasure certificates.

The skill never writes to the OpenSearch cluster — it only generates a reviewable
curl script for a human to run. To preserve GDPR Art. 5(2) (accountability) and
Art. 30 (records of processing) evidence without touching the cluster, every
`export-curl` run writes a local JSON "erasure certificate" describing exactly
what the generated script will erase, and chains it to the previous certificate
by hash. Any retroactive edit or deletion of a certificate breaks the chain and
is detectable via `verify-chain`.

Certificates live in the audit directory (``GDPR_AUDIT_DIR`` or ``gdpr-audit``),
one file per run: ``erasure-<timestamp>-<shorthash>.json``.
"""

import glob
import hashlib
import json
import os
from datetime import datetime, timezone

DEFAULT_AUDIT_DIR = "gdpr-audit"


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def resolve_dir(certificate_dir=None):
    return certificate_dir or os.getenv("GDPR_AUDIT_DIR", DEFAULT_AUDIT_DIR)


def _record_of(cert):
    """The hash-covered portion of a certificate (everything but the chain)."""
    return {k: v for k, v in cert.items() if k != "chain"}


def load_certificates(directory):
    """Return [(path, cert)] sorted by timestamp (chain/write order)."""
    certs = []
    for path in glob.glob(os.path.join(directory, "erasure-*.json")):
        try:
            with open(path, encoding="utf-8") as fh:
                certs.append((path, json.load(fh)))
        except (OSError, json.JSONDecodeError):
            continue
    certs.sort(key=lambda pc: pc[1].get("timestamp", ""))
    return certs


def _latest_hash(directory):
    certs = load_certificates(directory)
    if certs:
        return certs[-1][1].get("chain", {}).get("entry_hash", "GENESIS")
    return "GENESIS"


def script_sha256(path):
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return None


def build_record(request, flagged, curl_script_path=None, curl_script_hash=None):
    """Assemble the certificate payload for a generated (not yet applied) erasure.

    Records the target, the exact documents the script will touch, and the action
    — but not execution/verification results, because the human runs the script.
    """
    actor = os.getenv("GDPR_ACTOR") or os.getenv("USER") or os.getenv("USERNAME") or "unknown"
    indices = sorted({f.get("index") for f in flagged if f.get("index")})
    return {
        "event": "erasure_generated",
        "status": "GENERATED — run the curl script to apply; this certificate records the plan",
        "timestamp": _now_iso(),
        "actor": actor,
        "action_type": request.get("action_type"),
        "index_pattern": request.get("index_pattern") or ",".join(indices),
        "indices": indices,
        "precision_mode": request.get("precision_mode"),
        "target_profile": request.get("target_profile"),
        "curl_script": os.path.basename(curl_script_path) if curl_script_path else None,
        "curl_script_sha256": curl_script_hash,
        "flagged_count": len(flagged),
        "flagged": [
            {
                "doc_id": f.get("doc_id"),
                "index": f.get("index"),
                "confidence_score": f.get("confidence_score"),
                "identifying_snippets": f.get("identifying_snippets", []),
                "reasoning": f.get("reasoning"),
            }
            for f in flagged
        ],
    }


def write_certificate(record, certificate_dir=None):
    """Chain ``record`` to the latest certificate and write it locally.

    Returns {entry_hash, prev_hash, certificate_path, directory}. No cluster
    writes occur.
    """
    directory = resolve_dir(certificate_dir)
    os.makedirs(directory, exist_ok=True)
    prev_hash = _latest_hash(directory)
    entry_hash = hashlib.sha256((_canonical(record) + prev_hash).encode("utf-8")).hexdigest()

    cert = dict(record)
    cert["chain"] = {"prev_hash": prev_hash, "entry_hash": entry_hash}

    stamp = record["timestamp"].replace(":", "").replace("-", "").replace(".", "")
    path = os.path.join(directory, f"erasure-{stamp}-{entry_hash[:12]}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cert, fh, indent=2, ensure_ascii=False)

    return {"entry_hash": entry_hash, "prev_hash": prev_hash,
            "certificate_path": path, "directory": directory}


def verify_chain(certificate_dir=None):
    """Re-walk the local certificates and confirm the hash chain is intact.

    Returns {intact, entries, broken_at, ...}. A break means a certificate was
    altered, reordered, or deleted after the fact.
    """
    directory = resolve_dir(certificate_dir)
    certs = load_certificates(directory)
    prev = "GENESIS"
    for i, (path, cert) in enumerate(certs):
        chain = cert.get("chain", {})
        recomputed = hashlib.sha256((_canonical(_record_of(cert)) + prev).encode("utf-8")).hexdigest()
        if chain.get("prev_hash") != prev or chain.get("entry_hash") != recomputed:
            return {"intact": False, "entries": len(certs), "broken_at": i,
                    "broken_certificate": os.path.basename(path)}
        prev = chain["entry_hash"]
    return {"intact": True, "entries": len(certs), "broken_at": None,
            "directory": directory}


def list_entries(certificate_dir=None, limit=20):
    """Return recent certificate summaries, newest first."""
    directory = resolve_dir(certificate_dir)
    certs = load_certificates(directory)
    entries = []
    for path, cert in reversed(certs[-limit:]):
        entries.append({
            "certificate": os.path.basename(path),
            "timestamp": cert.get("timestamp"),
            "actor": cert.get("actor"),
            "action_type": cert.get("action_type"),
            "index_pattern": cert.get("index_pattern"),
            "precision_mode": cert.get("precision_mode"),
            "flagged_count": cert.get("flagged_count"),
            "entry_hash": cert.get("chain", {}).get("entry_hash"),
        })
    return {"directory": directory, "count": len(entries), "entries": entries}
