"""Real embedding model adapter + factory (MASTER_PLAN.md §6.2).

Wraps a sentence-transformers model (BGE-M3, Qwen3-Embedding, E5, MiniLM, …)
behind the ``Embedder`` interface. ``get_embedder`` returns a real model when one
is configured and importable, and otherwise falls back to the dependency-free
``DeterministicEmbedder`` so the system always runs (offline, CI).
"""
from __future__ import annotations

from typing import Literal, Optional

import numpy as np

from engraphis.backends.embedder_deterministic import DeterministicEmbedder


class SentenceTransformerEmbedder:
    def __init__(self, model_name: str) -> None:
        from sentence_transformers import SentenceTransformer  # lazy: optional dependency
        self.model = SentenceTransformer(model_name)
        self._dim = int(self.model.get_sentence_embedding_dimension())

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: list[str], *, kind: Literal["text", "code"] = "text") -> np.ndarray:
        vecs = self.model.encode(texts, normalize_embeddings=True, convert_to_numpy=True)
        return np.asarray(vecs, dtype=np.float32)


def get_embedder(model_name: Optional[str] = None, dim: int = 256):
    """A real model if available, else the deterministic offline embedder."""
    if model_name:
        try:
            return SentenceTransformerEmbedder(model_name)
        except Exception:
            pass
    return DeterministicEmbedder(dim)
