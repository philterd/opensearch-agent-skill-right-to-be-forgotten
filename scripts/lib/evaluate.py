"""Phase 2 — Contextual Disambiguation & Scoring.

The default and recommended evaluator is the *host agent itself* (Claude Code,
Cursor, Kiro, ...). The discovery step returns candidates as JSON; the agent
applies the judgment prompt below and returns structured evaluations, which are
filtered here by the precision-mode threshold. This keeps the skill fully
vendor-neutral — no LLM provider is hard-wired and no API key is required.

An OPTIONAL headless evaluator (OpenAI-compatible chat endpoint) is provided
for CI / non-interactive runs, activated only when GDPR_LLM_BASE_URL is set.
"""

import json
import os
import urllib.request

# precision_mode -> confidence threshold (from the skill spec)
PRECISION_MODES = {
    "strict_precision": 0.88,
    "balanced": 0.75,
    "high_recall": 0.60,
}
DEFAULT_PRECISION_MODE = "balanced"

# The judgment prompt the host agent applies to each candidate. Kept here so the
# SKILL.md instructions and the optional headless evaluator stay in lockstep.
EVAL_PROMPT = """\
Evaluate if the following document uniquely identifies the subject target.

Target Context:
\"\"\"
{target_profile}
\"\"\"

Document Context:
\"\"\"
Doc ID: {doc_id}
Index: {index_name}
Timestamp: {timestamp}
Text: {text}
\"\"\"

Precision mode: {precision_mode} (flag only at confidence >= {threshold}).

Evaluation criteria:
1. Does this document explicitly or IMPLICITLY single out the subject described \
in the target context (role + incident + timeline + behavior)?
2. Could this document reasonably describe a different individual in the \
organization? If yes, lower the confidence.
3. The subject's literal name/email/ID need NOT appear — indirect contextual \
identification counts under GDPR Recital 26.

Return STRICT JSON only:
{{
  "doc_id": "{doc_id}",
  "is_identifiable": true,
  "confidence_score": 0.00,
  "identifying_snippets": ["exact substring(s) from the text that identify the subject"],
  "reasoning": "one concise sentence"
}}
"""


def threshold_for(precision_mode):
    return PRECISION_MODES.get(precision_mode, PRECISION_MODES[DEFAULT_PRECISION_MODE])


def filter_flagged(evaluations, precision_mode, candidates_by_id=None):
    """Keep evaluations that are identifiable AND meet the confidence threshold.

    Enriches each flagged item with the candidate's index/text when available so
    downstream actions can target the exact document precisely.
    """
    threshold = threshold_for(precision_mode)
    candidates_by_id = candidates_by_id or {}
    flagged = []
    for ev in evaluations:
        try:
            conf = float(ev.get("confidence_score", 0) or 0)
        except (TypeError, ValueError):
            conf = 0.0
        if ev.get("is_identifiable") and conf >= threshold:
            item = dict(ev)
            item["confidence_score"] = conf
            cand = candidates_by_id.get(ev.get("doc_id"))
            if cand:
                item.setdefault("index", cand.get("index"))
                item.setdefault("text", cand.get("text"))
                item.setdefault("timestamp", cand.get("timestamp"))
            snippets = item.get("identifying_snippets") or []
            item["identifying_snippets"] = [s for s in snippets if isinstance(s, str) and s]
            flagged.append(item)
    return flagged


# --------------------------------------------------------------------------- #
# OPTIONAL headless evaluator (OpenAI-compatible). Off unless configured.      #
# --------------------------------------------------------------------------- #

def headless_available():
    return bool(os.getenv("GDPR_LLM_BASE_URL"))


def evaluate_headless(candidates, target_profile, precision_mode):
    """Score candidates via an OpenAI-compatible chat endpoint.

    Env: GDPR_LLM_BASE_URL (e.g. http://localhost:11434/v1), GDPR_LLM_MODEL,
    GDPR_LLM_API_KEY (optional). Uses only the stdlib so no extra dependency.
    """
    base = os.environ["GDPR_LLM_BASE_URL"].rstrip("/")
    model = os.getenv("GDPR_LLM_MODEL", "gpt-4o-mini")
    api_key = os.getenv("GDPR_LLM_API_KEY", "")
    threshold = threshold_for(precision_mode)

    evaluations = []
    for cand in candidates:
        prompt = EVAL_PROMPT.format(
            target_profile=target_profile,
            doc_id=cand.get("doc_id"),
            index_name=cand.get("index"),
            timestamp=cand.get("timestamp"),
            text=cand.get("text"),
            precision_mode=precision_mode,
            threshold=threshold,
        )
        payload = json.dumps({
            "model": model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": "You are a precise privacy-compliance evaluator. Reply with strict JSON only."},
                {"role": "user", "content": prompt},
            ],
        }).encode()
        req = urllib.request.Request(f"{base}/chat/completions", data=payload, method="POST")
        req.add_header("Content-Type", "application/json")
        if api_key:
            req.add_header("Authorization", f"Bearer {api_key}")
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
        content = data["choices"][0]["message"]["content"]
        evaluations.append(_parse_json_object(content, cand.get("doc_id")))
    return evaluations


def _parse_json_object(content, doc_id):
    content = content.strip()
    start, end = content.find("{"), content.rfind("}")
    if start != -1 and end != -1:
        content = content[start:end + 1]
    try:
        obj = json.loads(content)
    except json.JSONDecodeError:
        return {"doc_id": doc_id, "is_identifiable": False, "confidence_score": 0.0,
                "identifying_snippets": [], "reasoning": "unparseable evaluator output"}
    obj.setdefault("doc_id", doc_id)
    return obj
