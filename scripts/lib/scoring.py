"""Score the three pipeline stages separately.

Stage one scores automatically: recall@k needs only the labels. Stages two and
three need an `evaluations.json` from the Phase 2 judgment, so they live behind
a second entry point. One judgment pass serves every `precision_mode`, since
thresholds only reread the confidence scores it already produced.
"""

import hashlib
import re

from lib.leakage import assert_scorable
from lib.subjects import classify_mentions, near_duplicate_key

DEFAULT_KS = (10, 25, 50, 100, 200, 500)
PRECISION_THRESHOLDS = {"strict_precision": 0.88, "balanced": 0.75, "high_recall": 0.60}
MIN_TERM_LENGTH = 3


def split_positives(positives, namespace="derive/v1"):
    """Halve the positives deterministically into derive and score sets.

    The profile is built from one half and recall measured on the other, so no
    document both writes the query and is judged by it.
    """
    derive, score = [], []
    for item in positives:
        digest = hashlib.sha1(f"{namespace}:{item['doc_id']}".encode()).hexdigest()
        (derive if int(digest, 16) % 2 == 0 else score).append(item)
    return derive, score


def extend_ks(ks, positive_count):
    """Add a k that can reach every positive, so recall is never silently capped."""
    ks = sorted(set(int(k) for k in ks))
    if positive_count and (not ks or max(ks) < positive_count):
        ks.append(int(positive_count))
    return tuple(ks)


def recall_at_k(ranked_ids, positive_ids, ks=DEFAULT_KS):
    positive_ids = set(positive_ids)
    total = len(positive_ids)
    out = {}
    for k in ks:
        found = len(positive_ids & set(ranked_ids[:k]))
        out[f"recall@{k}"] = {
            "found": found,
            "of": total,
            "percent": round(100.0 * found / total, 1) if total else 0.0,
            "k_caps_recall": k < total,
        }
    return out


def significant_terms(client, index, doc_ids, size=30, field="message"):
    """Terms distinctive to a set of documents against the corpus background."""
    resp = client.search(index=index, body={
        "size": 0,
        "query": {"ids": {"values": list(doc_ids)}},
        "aggs": {"sig": {"significant_text": {
            "field": field, "size": size, "filter_duplicate_text": True}}},
    })
    return [b["key"] for b in resp["aggregations"]["sig"]["buckets"]]


def usable_terms(terms, aliases, min_length=MIN_TERM_LENGTH):
    """Drop anything that is the subject's own name, or too short to mean much.

    A term matching an alias variant would put the answer back into the query.
    """
    banned = {v.lower() for v in (aliases or {}).get("variants", [])}
    banned |= {p.lower() for v in banned for p in v.replace("@", " ").replace(".", " ").split()}
    return [t for t in terms if len(t) >= min_length and t.lower() not in banned]


_WORDINGS = (
    "Someone whose work involved {a}, {b}, {c} and {d}.",
    "A person responsible for {a} and {b}, working regularly on {c}, {d} and {e}.",
    "Records concerning {a}, {b}, {c}, {d} and {e}, produced by one individual.",
)


def build_profiles(terms, wordings=_WORDINGS, per_profile=5):
    """Generate several phrasings of the same evidence.

    Hand-written prose measures the prose. Generating N wordings and reporting
    the spread measures how much the phrasing matters, which is itself a
    finding: three wordings of one subject varied threefold in recall@50.
    """
    if not terms:
        return []
    picked = terms[:per_profile]
    while len(picked) < per_profile:
        picked.append(picked[-1])
    slots = dict(zip("abcde", picked))
    return [{"profile": w.format(**slots), "keywords": " ".join(terms[:8])}
            for w in wordings]


def compare_modes(runs, top_key, margin=1.0):
    """Say in words which retrieval mode won.

    The skill's mechanism claim is that hybrid surfaces what BM25 misses. A
    table the reader has to interpret does not test that.
    """
    best = {}
    for run in runs:
        pct = run["recall_descriptive"][top_key]["percent"]
        best[run["mode"]] = max(best.get(run["mode"], 0.0), pct)
    hybrid, bm25 = best.get("hybrid", 0.0), best.get("bm25_only", 0.0)
    gap = round(hybrid - bm25, 1)
    if abs(gap) < margin:
        verdict = (f"Hybrid and BM25-only are within noise at {top_key} "
                   f"({hybrid}% against {bm25}%). This run does not support the claim "
                   f"that hybrid retrieval surfaces documents BM25-only misses.")
    elif gap > 0:
        verdict = (f"Hybrid beat BM25-only at {top_key} by {gap} points "
                   f"({hybrid}% against {bm25}%).")
    else:
        verdict = (f"BM25-only beat hybrid at {top_key} by {abs(gap)} points "
                   f"({bm25}% against {hybrid}%). The skill's mechanism claim, that "
                   f"hybrid surfaces documents BM25-only misses, is contradicted here.")
    return {"best_hybrid_percent": hybrid, "best_bm25_only_percent": bm25,
            "gap_points": gap, "verdict": verdict}


INTERPRETATION = (
    "This measures topical authorship retrieval: the profile is built from terms "
    "distinctive to the subject's own documents, so a hit means the document reads "
    "like the rest of their output. The skill's claim is different, that a document "
    "describing a person identifies them. These are adjacent, not the same. Do not "
    "read this figure as evidence for indirect identification from a description; "
    "the demo corpus, whose documents are written to describe people, is where that "
    "claim is testable."
)


def flagged_at(evaluations, threshold):
    return {e["doc_id"] for e in evaluations
            if e.get("is_identifiable") and (e.get("confidence_score") or 0) >= threshold}


def precision_recall(flagged_ids, positive_ids, documents_scanned):
    flagged_ids, positive_ids = set(flagged_ids), set(positive_ids)
    hits = flagged_ids & positive_ids
    false_positives = len(flagged_ids - positive_ids)
    return {
        "flagged": len(flagged_ids),
        "true_positives": len(hits),
        "false_positives": false_positives,
        "precision": round(len(hits) / len(flagged_ids), 3) if flagged_ids else 0.0,
        "recall": round(len(hits) / len(positive_ids), 3) if positive_ids else 0.0,
        "false_positives_per_1000_documents":
            round(1000.0 * false_positives / documents_scanned, 2) if documents_scanned else 0.0,
    }


def span_validity(evaluations, texts):
    """A snippet that is not a verbatim substring makes the Painless update no-op.

    The document is then correctly flagged and silently not redacted.
    """
    checked = valid = 0
    offenders = 0
    for item in evaluations:
        text = texts.get(item["doc_id"])
        if text is None:
            continue
        bad = False
        for snippet in item.get("identifying_snippets") or []:
            if not isinstance(snippet, str) or not snippet:
                continue
            checked += 1
            if snippet in text:
                valid += 1
            else:
                bad = True
        offenders += 1 if bad else 0
    return {
        "snippets_checked": checked,
        "verbatim": valid,
        "percent_verbatim": round(100.0 * valid / checked, 1) if checked else 0.0,
        "documents_with_an_invalid_span": offenders,
    }


def over_redaction(evaluations, texts):
    """Share of each document the snippets would replace."""
    ratios = []
    for item in evaluations:
        text = texts.get(item["doc_id"])
        if not text:
            continue
        covered = sum(len(s) for s in (item.get("identifying_snippets") or [])
                      if isinstance(s, str) and s in text)
        ratios.append(covered / len(text))
    if not ratios:
        return {"documents": 0, "mean_share_redacted": 0.0, "max_share_redacted": 0.0}
    return {
        "documents": len(ratios),
        "mean_share_redacted": round(sum(ratios) / len(ratios), 4),
        "max_share_redacted": round(max(ratios), 4),
    }


def descriptive_split(positives, texts, surname):
    """Partition positives into those a profile could plausibly retrieve.

    Most mentions in email are recipient lists, and masking one yields a
    document about nobody. Recall against the raw set understates the system;
    recall against the descriptive set is the number worth reading.
    """
    pattern = re.compile(re.escape(surname), re.I)
    descriptive, listed, digests = [], [], set()
    for item in positives:
        text = texts.get(item["doc_id"]) or ""
        if classify_mentions(text, pattern)[0] > 0:
            descriptive.append(item)
            digests.add(near_duplicate_key(text))
        else:
            listed.append(item)
    return {
        "descriptive": descriptive,
        "list_only": listed,
        "distinct_descriptive": len(digests),
    }


def fetch_texts(client, index, ids, field="message", batch=500):
    """Document text keyed by id, for span checks and mention classification."""
    out, ids = {}, list(ids)
    for start in range(0, len(ids), batch):
        chunk = ids[start:start + batch]
        resp = client.mget(body={"docs": [
            {"_index": index, "_id": i, "_source": [field]} for i in chunk]})
        for doc in resp.get("docs", []):
            if doc.get("found"):
                out[doc["_id"]] = doc.get("_source", {}).get(field) or ""
    return out


def without(evaluations, doc_ids):
    """Drop judgments on documents that helped write the query."""
    doc_ids = set(doc_ids)
    return [e for e in evaluations if e.get("doc_id") not in doc_ids]


def assumptions(labels, ks, profiles, thresholds=None):
    """Every report states what it rests on, so a reader can attack an input."""
    aliases = labels.get("aliases", {})
    return {
        "subject": labels.get("subject"),
        "masked_index": labels.get("masked_index"),
        "mask_variants": len(aliases.get("variants") or []),
        "label_variants": aliases.get("label_variants") or [],
        "ambiguous_variants_masked_but_not_labelled": aliases.get("ambiguous_variants") or [],
        "mask_replacement": labels.get("mask_replacement", ""),
        "phone_policy": labels.get("phone_policy"),
        "k_values": list(ks),
        "precision_thresholds": dict(thresholds or PRECISION_THRESHOLDS),
        "profile_wordings": [p["profile"] for p in profiles],
        "measures": INTERPRETATION,
        "caveats": [
            "Masking manufactures the indirect case: a sentence written without a "
            "name would have been phrased differently from one with the name removed.",
            "Results do not transfer to another organization's corpus.",
            "The embedding model truncates long documents, so the neural clause sees "
            "only each document's opening.",
        ],
    }


def guard(labels, index):
    """Refuse to score the wrong index or an unaudited label set."""
    return assert_scorable(labels, index)
