"""Embedding engine — wraps sentence-transformers for local vector generation.

The model is loaded lazily on first use (first call downloads ~80-400 MB).
A simple in-process cache keeps the model resident for the process lifetime.
"""
from __future__ import annotations

import logging
import re
import threading
from typing import Optional

import numpy as np

from engraphis.backends.model_source import validate_model_source
from engraphis.config import settings

logger = logging.getLogger("engraphis.embedder")

_model = None
_dim: Optional[int] = None
# Guards the lazy model load. Without this, concurrent recall calls each
# see `_model is None`, and the forked PM2 worker tries to load the
# 80-400 MB model N times at once — the process wedges and every
# recall atop it times out. One lock + one load for the process lifetime.
_lock = threading.Lock()


def _get_model():
    global _model, _dim
    if _model is not None:
        return _model
    with _lock:
        if _model is not None:  # double-checked: another thread won the race
            return _model
        model_name = str(settings.embed_model or "").strip()
        revision = settings.embed_revision or None
        # Validate before sentence-transformers can resolve a remote source, so
        # strict mode cannot continue through a mutable-model fallback.
        validate_model_source(
            model_name,
            revision,
            require_immutable_models=settings.require_immutable_models,
            loader="legacy sentence-transformers model",
        )
        local_files_only = model_name.startswith("local:")
        if local_files_only:
            model_name = model_name[len("local:"):].strip()
            if not model_name:
                raise ValueError("local embedder selector requires a path or cached model name")
        from sentence_transformers import SentenceTransformer

        kwargs = {"trust_remote_code": False}
        if revision:
            kwargs["revision"] = revision
        if local_files_only:
            kwargs["local_files_only"] = True
        logger.info("Loading configured embedding model")
        _model = SentenceTransformer(model_name, **kwargs)
        get_dimension = getattr(_model, "get_embedding_dimension", None)
        if get_dimension is None:
            get_dimension = _model.get_sentence_embedding_dimension
        _dim = get_dimension()
        logger.info("Embedding model loaded (dim=%d)", _dim)
    return _model


def warmup():
    """Load the model eagerly so the first recall call isn't paid under request pressure.

    Safe to call from startup; returns True on success. Never raises.
    """
    try:
        _get_model()
        return True
    except Exception as exc:  # pragma: no cover - defensive; model may be missing
        # Loader/provider messages can expose credentialed URLs or local paths.
        logger.warning("Embedder warmup failed (%s)", type(exc).__name__)
        return False


def embed_dim() -> int:
    """Return the embedding dimension (loads model if needed)."""
    if _dim is None:
        _get_model()
    return _dim if _dim is not None else (settings.embed_dim if settings.embed_dim is not None else 384)


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
        # Clamp overlap so overlapped chunks never exceed chunk_size.
        safe_overlap = min(overlap, max(chunk_size // 2, 1))

        def take_piece(source: str, limit: int) -> tuple[str, str]:
            """Take a bounded piece without dropping the remainder."""
            if not source:
                return "", ""
            if len(source) <= limit:
                return source, ""
            # Prefer a whitespace boundary so overlap does not split an ordinary
            # word. A long unbroken token still falls back to the hard limit.
            boundary = max(source.rfind(" ", 0, limit), source.rfind("\n", 0, limit))
            end = boundary + 1 if boundary >= 0 else limit
            if end <= 0:
                end = limit
            return source[:end], source[end:]

        def append_preserving_source(output: list[str], prefix: str, source: str) -> None:
            """Add overlap plus all source text, splitting instead of truncating."""
            if not source:
                return
            # A normal source chunk already fits by itself. Reduce the duplicated
            # prefix until the complete chunk fits; never split a source chunk merely
            # to preserve an overlap, since that can lose a suffix or split a token.
            if len(source) <= chunk_size:
                prefix_budget = max(chunk_size - len(source), 0)
                if len(prefix) > prefix_budget:
                    prefix = prefix[-prefix_budget:] if prefix_budget else ""
                output.append(prefix + source)
                return
            if len(prefix) >= chunk_size:
                prefix = prefix[: max(chunk_size - 1, 0)]
            content_budget = max(chunk_size - len(prefix), 1)
            first, remaining = take_piece(source, content_budget)
            output.append(prefix + first)
            while remaining:
                piece, remaining = take_piece(remaining, chunk_size)
                output.append(piece)

        overlapped = [chunks[0]]
        for i in range(1, len(chunks)):
            previous = chunks[i - 1]
            prev_tail = previous[-safe_overlap:] if len(previous) > safe_overlap else previous
            prefix = (prev_tail + " ") if prev_tail else ""
            append_preserving_source(overlapped, prefix, chunks[i])
        chunks = overlapped

    return chunks
