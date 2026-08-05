"""Real embedding model adapter + factory.

Wraps a sentence-transformers model (BGE-M3, Qwen3-Embedding, E5, MiniLM, …)
behind the ``Embedder`` interface. ``get_embedder`` returns a real model when one
is configured and importable, and otherwise falls back to the dependency-free
``DeterministicEmbedder`` so the system always runs (offline, CI).

``local:<path>`` is an explicit local-only selector.  It asks sentence-transformers
to load only files already present at ``<path>`` or in its local cache.  Engraphis
does not ship a model in this package, so a missing local model degrades to lexical
hashing and reports that fact through the normal embedder capability response.
"""
from __future__ import annotations

import hashlib
import logging
from numbers import Integral
from typing import Any, Literal, Optional

import numpy as np

from engraphis.backends.embedder_deterministic import DeterministicEmbedder
from engraphis.backends.model_source import validate_model_source


LOCAL_MODEL_PREFIX = "local:"


class SentenceTransformerEmbedder:
    supports_semantic_search = True
    embedding_mode = "semantic"
    embedding_identity = "sentence_transformers"

    def __init__(
        self,
        model_name: str,
        *,
        revision: Optional[str] = None,
        local_files_only: bool = False,
        require_immutable_models: Optional[bool] = None,
    ) -> None:
        validate_model_source(
            model_name,
            revision,
            require_immutable_models=require_immutable_models,
            loader="sentence-transformers model",
        )
        from sentence_transformers import SentenceTransformer  # lazy: optional dependency
        kwargs: dict[str, Any] = {"trust_remote_code": False}
        if revision:
            kwargs["revision"] = revision
        if local_files_only:
            # This avoids a Hub request when an operator explicitly selected the
            # local mode.  It still supports both a local model directory and an
            # already-populated sentence-transformers cache.
            kwargs["local_files_only"] = True
        # Keep declared model provenance beside the loaded object.  Benchmark
        # artifacts must be able to distinguish a pinned model from a mutable
        # fallback without inspecting implementation-specific internals.
        self.model_name = model_name
        self.revision = revision
        self.local_files_only = local_files_only
        self.model = SentenceTransformer(model_name, **kwargs)
        dimension = self.model.get_embedding_dimension()
        if isinstance(dimension, bool) or not isinstance(dimension, Integral) or int(dimension) <= 0:
            raise ValueError('sentence-transformers model did not report a positive embedding dimension')
        self._dim = int(dimension)

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def embedding_version(self) -> str:
        """Identify the configured model space without exposing local paths or tokens."""
        configured = f"{self.model_name}\0{self.revision or 'unversioned'}"
        digest = hashlib.sha256(configured.encode("utf-8")).hexdigest()[:24]
        return f"st:{digest}"

    def embed(self, texts: list[str], *, kind: Literal["text", "code"] = "text") -> np.ndarray:
        if not texts:
            return np.empty((0, self._dim), dtype=np.float32)
        try:
            vecs = self.model.encode(texts, normalize_embeddings=True, convert_to_numpy=True)
            result = np.asarray(vecs, dtype=np.float32)
        except (TypeError, ValueError, OverflowError) as exc:
            raise RuntimeError("sentence-transformers returned malformed embeddings") from exc
        if result.ndim == 1 and len(texts) == 1:
            result = result.reshape(1, -1)
        if result.shape != (len(texts), self._dim) or not np.isfinite(result).all():
            raise RuntimeError("sentence-transformers returned malformed embeddings")
        with np.errstate(over="ignore", invalid="ignore"):
            norms = np.linalg.norm(result, axis=1, keepdims=True)
        if not np.isfinite(norms).all():
            raise RuntimeError("sentence-transformers returned malformed embeddings")
        result = result / np.where(norms == 0, 1.0, norms)
        return result


#: Why the real embedder last failed to load ("" when it loaded fine). The dashboard
#: surfaces this so a user can see and fix a broken semantic-search setup.
LAST_EMBEDDER_ERROR = ""


def get_embedder(
    model_name: Optional[str] = None,
    dim: int = 256,
    *,
    revision: Optional[str] = None,
    require_immutable_models: Optional[bool] = None,
):
    """Return a semantic model when available, else explicit lexical degradation.

    Prefix a configured model with ``local:`` to require a local path or cached
    model.  That mode never asks sentence-transformers to download the model.  It
    is deliberately opt-in because a regular model identifier retains the existing
    behavior for operators who want sentence-transformers to resolve it normally.
    """
    global LAST_EMBEDDER_ERROR
    if model_name:
        raw_model_name = str(model_name).strip()
        validate_model_source(
            raw_model_name,
            revision,
            require_immutable_models=require_immutable_models,
            loader="sentence-transformers model",
        )
        local_files_only = raw_model_name.startswith(LOCAL_MODEL_PREFIX)
        resolved_model_name = (
            raw_model_name[len(LOCAL_MODEL_PREFIX):].strip()
            if local_files_only
            else raw_model_name
        )
        try:
            if not resolved_model_name:
                raise ValueError("local embedder selector requires a path or cached model name")
            factory_kwargs: dict[str, Any] = {'revision': revision}
            if local_files_only:
                factory_kwargs["local_files_only"] = True
            emb = SentenceTransformerEmbedder(resolved_model_name, **factory_kwargs)
            LAST_EMBEDDER_ERROR = ""
            return emb
        except Exception as exc:  # noqa: BLE001 - optional dep; record why we fall back
            # Provider and local-loader exception text can contain credentials, signed
            # URLs, or filesystem paths. Keep only the exception class in diagnostics.
            error_kind = type(exc).__name__
            LAST_EMBEDDER_ERROR = error_kind
            log = logging.getLogger("engraphis")
            emit = log.info if isinstance(exc, ModuleNotFoundError) else log.warning
            emit(
                "Configured semantic embedder unavailable (%s); using the %d-dim deterministic "
                "embedder; semantic recall/why/timeline will not match stored vectors.",
                error_kind, dim)
            source = "requested local semantic model" if local_files_only else "requested semantic model"
            return DeterministicEmbedder(
                dim,
                semantic_support_reason=(
                    f"{source} is unavailable; deterministic feature hashing captures "
                    "lexical overlap only, so semantic vector retrieval and semantic "
                    "grounding are disabled"
                ),
            )
    return DeterministicEmbedder(dim)
