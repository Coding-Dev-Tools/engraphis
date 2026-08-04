"""Retrieval metrics.

Kept deliberately simple and transparent so scores are explainable. Phase 1 adds
RAGAS-style context precision/recall and an optional LLM-as-judge answer metric.
"""
from __future__ import annotations

import math


def _unique(values: list[str], name: str) -> list[str]:
    """Validate and de-duplicate metric IDs while preserving rank order."""
    if not isinstance(values, list):
        raise ValueError(f"{name} must be a list")
    result = []
    seen = set()
    for value in values:
        if not isinstance(value, str):
            raise ValueError(f"{name} must contain only strings")
        if value not in seen:
            result.append(value)
            seen.add(value)
    return result


def recall_at_k(retrieved_ids: list[str], supporting_ids: list[str]) -> float:
    """Fraction of unique gold supporting facts present in retrieved results."""
    retrieved = set(_unique(retrieved_ids, "retrieved_ids"))
    supporting = _unique(supporting_ids, "supporting_ids")
    if not supporting:
        return 0.0
    return sum(item in retrieved for item in supporting) / len(supporting)


def hit_at_k(retrieved_ids: list[str], supporting_ids: list[str]) -> float:
    """1.0 if any unique supporting fact was retrieved, else 0.0."""
    retrieved = set(_unique(retrieved_ids, "retrieved_ids"))
    supporting = _unique(supporting_ids, "supporting_ids")
    return 1.0 if any(item in retrieved for item in supporting) else 0.0


def reciprocal_rank(retrieved_ids: list[str], supporting_ids: list[str]) -> float:
    """Reciprocal rank of the first unique evidence item (zero when absent)."""
    supporting = set(_unique(supporting_ids, "supporting_ids"))
    for position, item in enumerate(_unique(retrieved_ids, "retrieved_ids"), start=1):
        if item in supporting:
            return 1.0 / position
    return 0.0


def mrr_at_k(retrieved_ids: list[str], supporting_ids: list[str], k: int) -> float:
    """MRR truncated to a non-negative declared depth."""
    if isinstance(k, bool) or not isinstance(k, int) or k < 0:
        raise ValueError("k must be a non-negative integer")
    return reciprocal_rank(retrieved_ids[:k], supporting_ids)


def ndcg_at_k(retrieved_ids: list[str], supporting_ids: list[str], k: int) -> float:
    """Binary-relevance normalized discounted cumulative gain at depth ``k``."""
    if isinstance(k, bool) or not isinstance(k, int) or k < 0:
        raise ValueError("k must be a non-negative integer")
    supporting = set(_unique(supporting_ids, "supporting_ids"))
    if not supporting:
        return 0.0
    ranked = _unique(retrieved_ids, "retrieved_ids")[:max(0, k)]
    dcg = sum(
        1.0 / math.log2(position + 1)
        for position, item in enumerate(ranked, start=1)
        if item in supporting
    )
    ideal = sum(
        1.0 / math.log2(position + 1)
        for position in range(1, min(len(supporting), k) + 1)
    )
    return dcg / ideal if ideal else 0.0


def retrieval_metrics_at_depths(
    retrieved_ids: list[str], supporting_ids: list[str],
    depths: tuple[int, ...] = (1, 5, 10),
) -> dict:
    """Return the conventional Recall/Hit/MRR/nDCG suite at declared depths."""
    if not isinstance(depths, (tuple, list)) or not depths:
        raise ValueError("depths must be a non-empty sequence of positive integers")
    normalized = []
    for value in depths:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("depths must contain only positive integers")
        normalized.append(value)
    result = {}
    for depth in sorted(set(normalized)):
        retrieved = retrieved_ids[:depth]
        result[f"recall_at_{depth}"] = recall_at_k(retrieved, supporting_ids)
        result[f"hit_at_{depth}"] = hit_at_k(retrieved, supporting_ids)
        result[f"mrr_at_{depth}"] = mrr_at_k(retrieved_ids, supporting_ids, depth)
        result[f"ndcg_at_{depth}"] = ndcg_at_k(retrieved_ids, supporting_ids, depth)
    return result


def binary_precision_recall_f1(
    predicted_positive: list[bool], expected_positive: list[bool]
) -> dict[str, float | int]:
    """Return transparent binary precision/recall/F1 with explicit counts.

    An empty predicted-positive set has precision 1 only when there are no true
    positives either.  This keeps an all-abstain system from receiving a free
    precision score on answerable questions while making the all-negative edge
    case well-defined.
    """
    if not isinstance(predicted_positive, list) or not isinstance(expected_positive, list):
        raise ValueError("binary metric inputs must be lists of booleans")
    if len(predicted_positive) != len(expected_positive):
        raise ValueError("predicted_positive and expected_positive must have equal length")
    if any(not isinstance(value, bool) for value in predicted_positive + expected_positive):
        raise ValueError("binary metric inputs must be lists of booleans")
    tp = sum(predicted and expected
             for predicted, expected in zip(predicted_positive, expected_positive))
    fp = sum(predicted and not expected
             for predicted, expected in zip(predicted_positive, expected_positive))
    fn = sum(not predicted and expected
             for predicted, expected in zip(predicted_positive, expected_positive))
    precision = tp / (tp + fp) if tp + fp else (1.0 if not any(expected_positive) else 0.0)
    recall = tp / (tp + fn) if tp + fn else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "true_positive": tp,
        "false_positive": fp,
        "false_negative": fn,
        "n": len(predicted_positive),
    }


def grounded_precision_recall_f1(
    grounded: list[bool], answerable: list[bool]
) -> dict[str, float | int]:
    """Score grounded answers as positives against answerable questions."""
    return binary_precision_recall_f1(grounded, answerable)


def abstention_precision_recall_f1(
    abstained: list[bool], answerable: list[bool]
) -> dict[str, float | int]:
    """Score abstentions as positives against questions without answer evidence."""
    return binary_precision_recall_f1(abstained, [not item for item in answerable])


def answer_token_recall(retrieved_texts: list[str], answer: str) -> float:
    """Fraction of the gold answer's content tokens present in retrieved text."""
    gold = _tokens(answer)
    # Missing/empty answers are not evidence of a correct retrieval.  Returning
    # zero avoids a vacuous perfect score for malformed or partial records.
    if not gold:
        return 0.0
    pool = set()
    for text in retrieved_texts:
        pool |= _tokens(text)
    return sum(1 for token in gold if token in pool) / len(gold)


_STOP = {"the", "a", "an", "to", "of", "in", "on", "for", "and", "or", "is", "are",
         "was", "were", "we", "our", "with", "by", "it", "that", "this", "did", "do"}


def _tokens(text: str) -> set[str]:
    sep = "".join(c if c.isalnum() else " " for c in (text or "").lower())
    return {t for t in sep.split() if t and t not in _STOP and len(t) > 1}
