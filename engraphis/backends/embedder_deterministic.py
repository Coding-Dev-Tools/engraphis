"""Deterministic hashing embedder — offline, dependency-free, reproducible.

Maps text to a fixed-dim vector via feature hashing (the "hashing trick"): each
token is hashed to a dimension and a sign, counts are accumulated, then the
vector is L2-normalized. It captures lexical overlap, so cosine similarity is
meaningful enough to exercise the retrieval pipeline and write deterministic
tests — without downloading a model.

It is NOT a semantic model. Production uses a real embedder (BGE-M3 / Qwen3 /
Voyage / OpenAI) behind the same ``Embedder`` interface; swap via config.
"""
from __future__ import annotations

import hashlib
from typing import Literal

import numpy as np


class DeterministicEmbedder:
    def __init__(self, dim: int = 384) -> None:
        self._dim = dim

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: list[str], *, kind: Literal["text", "code"] = "text") -> np.ndarray:
        out = np.zeros((len(texts), self._dim), dtype=np.float32)
        for i, text in enumerate(texts):
            for token in _tokenize(text, kind):
                h = hashlib.sha1(token.encode("utf-8")).digest()
                idx = int.from_bytes(h[:4], "big") % self._dim
                sign = 1.0 if h[4] & 1 else -1.0
                out[i, idx] += sign
            norm = float(np.linalg.norm(out[i]))
            if norm > 0:
                out[i] /= norm
        return out


def _tokenize(text: str, kind: str) -> list[str]:
    text = (text or "").lower()
    # For code, keep identifier-ish boundaries; for text, split on non-alphanumerics.
    sep = "".join(c if c.isalnum() else " " for c in text)
    tokens = [t for t in sep.split() if t]
    # add character trigrams for short/OOV robustness
    trigrams = [text[j:j + 3] for j in range(max(0, len(text) - 2))][:512]
    return tokens + trigrams
