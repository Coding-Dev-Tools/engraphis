"""Real embedding model adapter + factory.

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
        get_dimension = getattr(self.model, "get_embedding_dimension", None)
        if get_dimension is None:
            get_dimension = self.model.get_sentence_embedding_dimension
        self._dim = int(get_dimension())

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: list[str], *, kind: Literal["text", "code"] = "text") -> np.ndarray:
        vecs = self.model.encode(texts, normalize_embeddings=True, convert_to_numpy=True)
        return np.asarray(vecs, dtype=np.float32)


#: Why the real embedder last failed to load ("" when it loaded fine). The dashboard
#: surfaces this so a user can see and fix a broken semantic-search setup.
LAST_EMBEDDER_ERROR = ""


def get_embedder(model_name: Optional[str] = None, dim: int = 256):
    """A real model if available, else the deterministic offline embedder."""
    global LAST_EMBEDDER_ERROR
    if model_name:
        try:
            emb = SentenceTransformerEmbedder(model_name)
            LAST_EMBEDDER_ERROR = ""
            return emb
        except Exception as exc:  # noqa: BLE001 - optional dep; record why we fall back
            LAST_EMBEDDER_ERROR = "%s: %s" % (type(exc).__name__, exc)
            import logging
            logging.getLogger("engraphis").warning(
                "embedder '%s' unavailable (%s) - using the %d-dim deterministic "
                "embedder; semantic recall/why/timeline will not match stored vectors.",
                model_name, LAST_EMBEDDER_ERROR, dim)
    return DeterministicEmbedder(dim)
