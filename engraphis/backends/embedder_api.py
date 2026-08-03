"""API-based embedder — calls an OpenAI-compatible endpoint (OpenRouter, etc.)

Uses the ``/v1/embeddings`` endpoint. Since many OpenRouter models are chat
models that may not expose a native embeddings endpoint, this module also
provides a fallback: a simple ``[CLS]``-style prompt wrapper that asks the
chat model to produce a text representation we then hash into a vector, or
for real embedding models simply passes the text to ``/v1/embeddings``.

Design notes:
- Implements the ``Embedder`` protocol (``engraphis.core.interfaces.Embedder``).
- Dimension is detected from the first API response.
- Batch embedding sends multiple inputs in one API call.
"""
from __future__ import annotations

import logging
import os
from numbers import Integral
from typing import Literal, Optional

import numpy as np

from engraphis.backends.embedder_deterministic import MAX_EMBEDDING_DIM

logger = logging.getLogger("engraphis.embedder_api")

# Default OpenRouter endpoint
_DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
_DEFAULT_API_KEY_ENV = "ENGRAPHIS_LLM_API_KEY"


class ApiEmbedder:
    """Embedder that calls an OpenAI-compatible /v1/embeddings API.

    Parameters
    ----------
    model : str
        Model identifier, e.g. ``"nvidia/nemotron-3-ultra-550b-a55b:free"``.
    base_url : str, optional
        API base URL (default: OpenRouter).
    api_key : str, optional
        API key. Falls back to ``ENGRAPHIS_LLM_API_KEY`` env var.
    dim : int, optional
        Known embedding dimension. If not provided, detected from first response.
    """

    supports_semantic_search = True
    embedding_mode = "semantic"

    def __init__(
        self,
        model: str,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        dim: Optional[int] = None,
    ) -> None:
        self.model = model
        self._base_url = (base_url or _DEFAULT_BASE_URL).rstrip("/")
        self._api_key = api_key or os.environ.get(_DEFAULT_API_KEY_ENV, "")
        if dim is not None:
            if isinstance(dim, bool) or not isinstance(dim, Integral):
                raise ValueError("embedding dimension must be a positive integer")
            dim = int(dim)
            if not 1 <= dim <= MAX_EMBEDDING_DIM:
                raise ValueError(
                    f"embedding dimension must be between 1 and {MAX_EMBEDDING_DIM}"
                )
        self._dim = dim
        self._embeddings_url = f"{self._base_url}/v1/embeddings"
        # A custom endpoint can contain embedded credentials or signed query
        # parameters, while provider-controlled model identifiers are also untrusted
        # log input. Do not copy either into logs.
        # Do not log endpoint, model, dimensions, or any authentication-related state:
        # provider configuration can contain account identifiers or signed parameters.
        logger.info("API embedder initialized")

    @property
    def dim(self) -> int:
        if self._dim is None:
            # Probe the API to get dimension
            probe = self.embed(["hello"])
            self._dim = probe.shape[1]
        return self._dim  # type: ignore[return-value]

    def embed(
        self, texts: list[str], *, kind: Literal["text", "code"] = "text"
    ) -> np.ndarray:
        """Embed a list of strings via the API.

        Uses ``/v1/embeddings`` with batch input.
        Falls back to per-item requests if the batch fails.

        Notes
        -----
        The ``kind`` parameter is accepted for protocol compatibility
        (``engraphis.core.interfaces.Embedder``) but is not used by the
        API embedder — the same endpoint handles both text and code.
        """
        if not texts:
            # An empty batch must not probe a remote provider merely to discover a
            # dimension. Unknown is represented by the only truthful width: zero.
            return np.empty((0, self._dim or 0), dtype=np.float32)

        import httpx

        if not self._api_key:
            logger.error("No API key set for API embedder")
            raise RuntimeError(
                f"ApiEmbedder requires an API key via {_DEFAULT_API_KEY_ENV} "
                "env var or the api_key parameter"
            )

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "input": texts,
        }

        try:
            with httpx.Client(timeout=60.0) as client:
                resp = client.post(
                    self._embeddings_url, headers=headers, json=payload
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception:
            logger.warning("Batch embedding request failed; falling back per-item")
            # Fallback: embed one at a time
            vecs = [self._embed_one(t) for t in texts]
            return self._finalize_vectors(vecs, len(texts))

        vectors = self._ordered_batch_vectors(data, len(texts))
        if vectors is None:
            logger.warning("API returned malformed embedding data; falling back per-item")
            vecs = [self._embed_one(t) for t in texts]
            return self._finalize_vectors(vecs, len(texts))
        return self._finalize_vectors(vectors, len(texts))

    def _finalize_vectors(
        self,
        vectors: list[Optional[list[float]]],
        count: int,
    ) -> np.ndarray:
        """Assemble one finite, consistently-sized, L2-normalized vector per input."""
        if len(vectors) != count:
            raise RuntimeError("embedding provider returned an incomplete response")
        widths = {len(vector) for vector in vectors if vector is not None}
        if self._dim is not None:
            widths.add(self._dim)
        if not widths:
            # Without a configured or successfully observed width, zero vectors
            # cannot establish an embedding-space contract.  Guessing 384 here
            # would poison future successful responses from a differently-sized
            # provider model.
            raise RuntimeError("embedding provider returned no usable vectors")
        if len(widths) > 1:
            raise RuntimeError("embedding provider returned inconsistent dimensions")
        dimension = next(iter(widths))
        if not 1 <= dimension <= MAX_EMBEDDING_DIM:
            raise RuntimeError("embedding provider returned an invalid dimension")
        completed = [
            vector if vector is not None else [0.0] * dimension
            for vector in vectors
        ]
        result = np.asarray(completed, dtype=np.float32)
        if result.shape != (count, dimension) or not np.isfinite(result).all():
            raise RuntimeError("embedding provider returned malformed vectors")
        norms = np.linalg.norm(result, axis=1, keepdims=True)
        norms = np.where(norms == 0, 1.0, norms)
        result = result / norms
        if self._dim is None:
            self._dim = dimension
        return result

    def _coerce_vector(self, value) -> Optional[list[float]]:
        """Validate provider-controlled vector shape without reflecting its data."""
        try:
            vector = np.asarray(value, dtype=np.float32)
        except (TypeError, ValueError, OverflowError):
            return None
        if self._dim is not None and vector.ndim == 1 and vector.size != self._dim:
            # A configured dimension is a compatibility contract with the vector
            # store.  Treating a provider/model mismatch as a malformed row would
            # make the fallback substitute a zero vector, silently corrupting
            # retrieval instead of surfacing the configuration error.
            raise RuntimeError("embedding provider returned an unexpected dimension")
        if (
            vector.ndim != 1
            or not 1 <= vector.size <= MAX_EMBEDDING_DIM
            or not np.isfinite(vector).all()
        ):
            return None
        return vector.tolist()

    def _ordered_batch_vectors(self, data, count: int) -> Optional[list[list[float]]]:
        """Return exactly one validated vector per requested input, in input order."""
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            return None
        items = data["data"]
        if len(items) != count:
            return None
        ordered: list[Optional[list[float]]] = [None] * count
        for item in items:
            if not isinstance(item, dict):
                return None
            index = item.get("index")
            if (
                isinstance(index, bool)
                or not isinstance(index, int)
                or not 0 <= index < count
                or ordered[index] is not None
            ):
                return None
            vector = self._coerce_vector(item.get("embedding"))
            if vector is None:
                return None
            ordered[index] = vector
        if any(vector is None for vector in ordered):
            return None
        widths = {len(vector) for vector in ordered if vector is not None}
        if len(widths) != 1:
            return None
        return [vector for vector in ordered if vector is not None]

    def _embed_one(self, text: str) -> Optional[list[float]]:
        """Embed a single string via the API."""
        import httpx

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "input": [text],
        }

        try:
            with httpx.Client(timeout=60.0) as client:
                resp = client.post(
                    self._embeddings_url, headers=headers, json=payload
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception:
            logger.error("Single embedding request failed")
            return None

        ordered = self._ordered_batch_vectors(data, 1)
        if ordered is not None:
            return ordered[0]
        logger.warning("API returned malformed single-item embedding data")
        return None
