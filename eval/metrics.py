"""Retrieval metrics (MASTER_PLAN.md §14.3).

Kept deliberately simple and transparent so scores are explainable. Phase 1 adds
RAGAS-style context precision/recall and an optional LLM-as-judge answer metric.
"""
from __future__ import annotations


def recall_at_k(retrieved_ids: list[str], supporting_ids: list[str]) -> float:
    """Fraction of the gold supporting facts that appear in the retrieved set."""
    if not supporting_ids:
        return 1.0
    hits = sum(1 for s in supporting_ids if s in retrieved_ids)
    return hits / len(supporting_ids)


def hit_at_k(retrieved_ids: list[str], supporting_ids: list[str]) -> float:
    """1.0 if any supporting fact was retrieved, else 0.0."""
    return 1.0 if any(s in retrieved_ids for s in supporting_ids) else 0.0


def answer_token_recall(retrieved_texts: list[str], answer: str) -> float:
    """Fraction of the gold answer's content tokens present in retrieved text."""
    gold = _tokens(answer)
    if not gold:
        return 1.0
    pool = set()
    for t in retrieved_texts:
        pool |= _tokens(t)
    return sum(1 for g in gold if g in pool) / len(gold)


_STOP = {"the", "a", "an", "to", "of", "in", "on", "for", "and", "or", "is", "are",
         "was", "were", "we", "our", "with", "by", "it", "that", "this", "did", "do"}


def _tokens(text: str) -> set[str]:
    sep = "".join(c if c.isalnum() else " " for c in (text or "").lower())
    return {t for t in sep.split() if t and t not in _STOP and len(t) > 1}
