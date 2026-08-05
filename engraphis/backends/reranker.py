"""Rerankers.

Cross-encoder reranking is the single biggest precision win on top of hybrid
candidates. ``IdentityReranker`` is the offline default (sorts by fused score);
``CrossEncoderReranker`` uses a sentence-transformers cross-encoder when available.
Both satisfy the ``Reranker`` interface, so they are swapped via config.
"""
from __future__ import annotations

import logging
import math
from typing import Any, Optional

from engraphis.backends.model_source import validate_model_source
from engraphis.core.interfaces import Candidate

logger = logging.getLogger("engraphis")


class IdentityReranker:
    """No-op reranker: trust the fused score. Default for offline/CI."""

    def rerank(self, query: str, candidates: list[Candidate], k: int) -> list[Candidate]:
        return sorted(candidates, key=lambda c: c.score, reverse=True)[:k]


class CrossEncoderReranker:
    """Cross-encoder reranker (e.g. BGE-reranker-v2 / Qwen3-Reranker / ms-marco)."""

    def __init__(self, model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2", *,
                 revision: Optional[str] = None,
                 require_immutable_models: Optional[bool] = None) -> None:
        validate_model_source(
            model_name,
            revision,
            require_immutable_models=require_immutable_models,
            loader="cross-encoder reranker",
        )
        local_files_only = model_name.startswith("local:")
        resolved_model_name = (
            model_name[len("local:"):].strip() if local_files_only else model_name
        )
        if not resolved_model_name:
            raise ValueError("local reranker selector requires a path or cached model name")
        from sentence_transformers import CrossEncoder  # pyright: ignore[reportMissingImports]  # lazy: optional dependency
        kwargs: dict[str, Any] = {"trust_remote_code": False}
        if revision:
            kwargs["revision"] = revision
        if local_files_only:
            kwargs["local_files_only"] = True
        self.model = CrossEncoder(resolved_model_name, **kwargs)

    def rerank(self, query: str, candidates: list[Candidate], k: int) -> list[Candidate]:
        if not candidates:
            return []
        pairs = [
            (query, (c.record.summary or c.record.content) if c.record else "")
            for c in candidates
        ]
        try:
            scores = list(self.model.predict(pairs))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("cross-encoder returned malformed scores") from exc
        if len(scores) != len(candidates):
            raise RuntimeError("cross-encoder returned an incomplete score array")
        for c, s in zip(candidates, scores):
            try:
                value = float(s)
            except (TypeError, ValueError, OverflowError) as exc:
                raise RuntimeError("cross-encoder returned a non-numeric score") from exc
            if not math.isfinite(value):
                raise RuntimeError("cross-encoder returned a non-finite score")
            c.score = value
        return sorted(candidates, key=lambda c: c.score, reverse=True)[:k]


def get_reranker(
    model_name: Optional[str] = None,
    *,
    revision: Optional[str] = None,
    require_immutable_models: Optional[bool] = None,
) -> object:
    """Return a cross-encoder reranker if a model is given and loads, else identity."""
    if model_name:
        # Policy errors stay outside the optional-loader fallback: strict mode must
        # reject a mutable remote source rather than quietly disabling reranking.
        validate_model_source(
            model_name,
            revision,
            require_immutable_models=require_immutable_models,
            loader="cross-encoder reranker",
        )
        try:
            return CrossEncoderReranker(
                model_name,
                revision=revision,
                require_immutable_models=require_immutable_models,
            )
        except Exception as exc:  # noqa: BLE001 - optional dependency fallback
            # Third-party loader errors can include credentials, signed URLs, local
            # paths, and model identifiers. Keep diagnostics actionable but redacted.
            logger.warning(
                "Configured cross-encoder reranker unavailable (%s); using identity reranker",
                type(exc).__name__,
            )
    return IdentityReranker()
