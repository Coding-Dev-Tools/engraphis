"""Rerankers.

Cross-encoder reranking is the single biggest precision win on top of hybrid
candidates. ``IdentityReranker`` is the offline default (sorts by fused score);
``CrossEncoderReranker`` uses a sentence-transformers cross-encoder when available.
Both satisfy the ``Reranker`` interface, so they are swapped via config.
"""
from __future__ import annotations

from typing import Optional

from engraphis.core.interfaces import Candidate


class IdentityReranker:
    """No-op reranker: trust the fused score. Default for offline/CI."""

    def rerank(self, query: str, candidates: list[Candidate], k: int) -> list[Candidate]:
        return sorted(candidates, key=lambda c: c.score, reverse=True)[:k]


class CrossEncoderReranker:
    """Cross-encoder reranker (e.g. BGE-reranker-v2 / Qwen3-Reranker / ms-marco)."""

    def __init__(self, model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2") -> None:
        from sentence_transformers import CrossEncoder  # lazy: optional dependency
        self.model = CrossEncoder(model_name)

    def rerank(self, query: str, candidates: list[Candidate], k: int) -> list[Candidate]:
        if not candidates:
            return []
        pairs = [
            (query, (c.record.summary or c.record.content) if c.record else "")
            for c in candidates
        ]
        scores = self.model.predict(pairs)
        for c, s in zip(candidates, scores):
            c.score = float(s)
        return sorted(candidates, key=lambda c: c.score, reverse=True)[:k]


def get_reranker(model_name: Optional[str] = None) -> object:
    """Return a cross-encoder reranker if a model is given and loads, else identity."""
    if model_name:
        try:
            return CrossEncoderReranker(model_name)
        except Exception:
            pass
    return IdentityReranker()
