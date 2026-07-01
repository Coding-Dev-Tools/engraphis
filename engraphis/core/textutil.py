"""Tiny, dependency-free text-overlap utilities shared by the write and recall paths.

Used by ``core.resolve`` (conflict resolution) and ``core.engine`` (the ``why``/
``timeline`` tools) to estimate "is this about the same thing" from raw text alone —
no embedder, no LLM, no network. Deliberately simple and explainable, matching
``eval/metrics.py``'s tokenizer so eval and runtime relatedness judgments agree.
"""
from __future__ import annotations

_STOPWORDS = {
    "the", "a", "an", "to", "of", "in", "on", "for", "and", "or", "is", "are", "was",
    "were", "we", "our", "with", "by", "it", "that", "this", "did", "do", "as", "at",
    "be", "has", "have", "from", "into", "now", "will", "all",
}


def tokenize(text: str) -> set[str]:
    """Lowercase, alnum-split, stopword-filtered token set."""
    sep = "".join(c if c.isalnum() else " " for c in (text or "").lower())
    return {t for t in sep.split() if t and t not in _STOPWORDS and len(t) > 1}


def jaccard(a: set[str], b: set[str]) -> float:
    """Jaccard similarity of two token sets; 0.0 if either is empty."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def text_overlap(a: str, b: str) -> float:
    """Convenience: tokenize both strings and return their Jaccard overlap."""
    return jaccard(tokenize(a), tokenize(b))
