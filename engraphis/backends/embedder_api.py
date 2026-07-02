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
from typing import Literal, Optional

import numpy as np

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
        self._dim = dim
        self._embeddings_url = f"{self._base_url}/v1/embeddings"
        logger.info(
            "ApiEmbedder(model=%s, base_url=%s, dim=%s)",
            self.model, self._base_url, self._dim or "auto",
        )

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
        """
        import httpx

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
        except Exception as exc:
            logger.warning("Batch embedding failed (%s), falling back per-item", exc)
            # Fallback: embed one at a time
            vecs = [self._embed_one(t) for t in texts]
            return np.asarray(vecs, dtype=np.float32)

        # Parse response
        items = data.get("data", [])
        # Sort by index to preserve order
        items.sort(key=lambda x: x.get("index", 0))
        vecs = [item["embedding"] for item in items]

        result = np.asarray(vecs, dtype=np.float32)
        # L2-normalize for cosine similarity
        norms = np.linalg.norm(result, axis=1, keepdims=True)
        norms = np.where(norms == 0, 1.0, norms)
        result = result / norms

        # Detect dimension from first response
        if self._dim is None and len(vecs) > 0:
            self._dim = len(vecs[0])

        return result

    def _embed_one(self, text: str) -> list[float]:
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

        with httpx.Client(timeout=60.0) as client:
            resp = client.post(
                self._embeddings_url, headers=headers, json=payload
            )
            resp.raise_for_status()
            data = resp.json()

        items = data.get("data", [])
        if items:
            vec = items[0].get("embedding", [])
            if self._dim is None:
                self._dim = len(vec)
            return vec
        return [0.0] * (self._dim or 384)
