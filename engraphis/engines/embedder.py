"""Embedding engine — wraps sentence-transformers for local vector generation.

The model is loaded lazily on first use (first call downloads ~80-400 MB).
A simple in-process cache keeps the model resident for the process lifetime.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

import numpy as np

from engraphis.config import settings

logger = logging.getLogger("neocortex.embedder")

_model = None
_dim: Optional[int] = None


def _get_model():
    global _model, _dim
    if _model is None:
        from sentence_transformers import SentenceTransformer

        logger.info("Loading embedding model: %s", settings.embed_model)
        _model = SentenceTransformer(settings.embed_model)
        _dim = _model.get_sentence_embedding_dimension()
        logger.info("Embedding model loaded (dim=%d)", _dim)
    return _model


def embed_dim() -> int:
    """Return the embedding dimension (loads model if needed)."""
    if _dim is None:
        _get_model()
    return _dim or settings.embed_dim or 384


def embed(text: str) -> np.ndarray:
    """Embed a single string into a float32 numpy vector."""
    model = _get_model()
    vec = model.encode(text, normalize_embeddings=True, show_progress_bar=False)
    return np.asarray(vec, dtype=np.float32)


def embed_batch(texts: list[str]) -> np.ndarray:
    """Embed multiple strings at once (more efficient)."""
    model = _get_model()
    vecs = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
    return np.asarray(vecs, dtype=np.float32)


def chunk_text(text: str, chunk_size: int = 800, overlap: int = 100) -> list[str]:
    """Split long text into overlapping chunks for better retrieval.

    Splits on paragraph/line boundaries first, then by character count.
    """
    if not text or not text.strip():
        return []
    if len(text) <= chunk_size:
        return [text.strip()]

    paragraphs = re.split(r"\n\s*\n", text)
    chunks: list[str] = []
    current = ""

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        if len(current) + len(para) + 2 <= chunk_size:
            current = (current + "\n\n" + para) if current else para
        else:
            if current:
                chunks.append(current)
            if len(para) <= chunk_size:
                current = para
            else:
                words = para.split(" ")
                current = ""
                for w in words:
                    if len(current) + len(w) + 1 <= chunk_size:
                        current = (current + " " + w) if current else w
                    else:
                        if current:
                            chunks.append(current)
                        current = w
                if current:
                    chunks.append(current)
                    current = ""

    if current:
        chunks.append(current)

    if overlap > 0 and len(chunks) > 1:
        overlapped = [chunks[0]]
        for i in range(1, len(chunks)):
            prev_tail = chunks[i - 1][-overlap:] if len(chunks[i - 1]) > overlap else chunks[i - 1]
            overlapped.append(prev_tail + " " + chunks[i])
        chunks = overlapped

    return chunks
