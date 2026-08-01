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
import re
from typing import Literal

import numpy as np


DETERMINISTIC_EMBEDDING_IDENTITY = "deterministic_hashing"
DETERMINISTIC_EMBEDDING_VERSION = "v2_aliases_measurements"


class DeterministicEmbedder:
    def __init__(self, dim: int = 384) -> None:
        self._dim = dim

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def embedding_identity(self) -> str:
        """Stable storage identity for versioned deterministic vectors."""
        return DETERMINISTIC_EMBEDDING_IDENTITY

    @property
    def embedding_version(self) -> str:
        """Bump when emitted hashing features change persisted-vector meaning."""
        return DETERMINISTIC_EMBEDDING_VERSION

    def embed(self, texts: list[str], *, kind: Literal["text", "code"] = "text") -> np.ndarray:
        out = np.zeros((len(texts), self._dim), dtype=np.float32)
        for i, text in enumerate(texts):
            for feature in _tokenize(text, kind):
                # SHA-1 is retained only to keep the established offline embedding
                # mapping stable across upgrades. This is feature hashing, never a
                # password, signature, integrity check, or other security primitive.
                h = hashlib.sha1(
                    feature.encode("utf-8"), usedforsecurity=False
                ).digest()
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
    return tokens + trigrams + _variant_features(text, tokens)


# These are deliberately small, high-confidence equivalence classes.  They are
# emitted as additional features (rather than replacing the original tokens) so
# existing feature-hash behaviour remains stable for ordinary text.
_LEXICAL_ALIASES = {
    "req": "request",
    "requests": "request",
    "resp": "response",
    "responses": "response",
    "config": "configuration",
    "configs": "configuration",
    "db": "database",
    "repos": "repository",
    "repo": "repository",
    "auth": "authentication",
    "authn": "authentication",
    "authz": "authorization",
    "app": "application",
    "apps": "application",
}

_NUMBER_WORDS = {
    "zero": 0.0,
    "one": 1.0,
    "two": 2.0,
    "three": 3.0,
    "four": 4.0,
    "five": 5.0,
    "six": 6.0,
    "seven": 7.0,
    "eight": 8.0,
    "nine": 9.0,
    "ten": 10.0,
    "hundred": 100.0,
}

# Canonical dimensions make common unit rewrites comparable, e.g. ``1 minute``
# and ``60 seconds``.  Only unambiguous time/data/rate units are included.
_UNITS = {
    "ms": ("second", 0.001),
    "millisecond": ("second", 0.001),
    "milliseconds": ("second", 0.001),
    "s": ("second", 1.0),
    "sec": ("second", 1.0),
    "second": ("second", 1.0),
    "seconds": ("second", 1.0),
    "min": ("second", 60.0),
    "minute": ("second", 60.0),
    "minutes": ("second", 60.0),
    "hr": ("second", 3600.0),
    "hour": ("second", 3600.0),
    "hours": ("second", 3600.0),
    "day": ("second", 86400.0),
    "days": ("second", 86400.0),
    "b": ("byte", 1.0),
    "byte": ("byte", 1.0),
    "bytes": ("byte", 1.0),
    "kb": ("byte", 1_000.0),
    "mb": ("byte", 1_000_000.0),
    "gb": ("byte", 1_000_000_000.0),
}

_NUMBER_RE = re.compile(r"^\d+(?:\.\d+)?$")


def _variant_features(text: str, tokens: list[str]) -> list[str]:
    """Return conservative, additive features for common lexical rewrites."""
    features: list[str] = []
    for token in tokens:
        canonical = _LEXICAL_ALIASES.get(token)
        if canonical:
            # Include the canonical token itself so an abbreviation shares a
            # feature with ordinary historical text that contains the expanded
            # spelling.  The namespaced feature still links two abbreviations
            # even when neither side carries the canonical word literally.
            features.extend((canonical, f"alias:{canonical}"))

    values = []
    for index, token in enumerate(tokens):
        if _NUMBER_RE.fullmatch(token):
            values.append((index, float(token)))
        elif token in _NUMBER_WORDS:
            values.append((index, _NUMBER_WORDS[token]))

    unit_positions = [(index, _UNITS[token]) for index, token in enumerate(tokens)
                      if token in _UNITS]
    for number_index, number in values:
        for unit_index, (dimension, multiplier) in unit_positions:
            # A short window handles both "60 seconds" and "60 per second"
            # without making unrelated numbers in a long sentence collide.
            if abs(number_index - unit_index) <= 3:
                normalized = number * multiplier
                features.append(f"measure:{dimension}:{normalized:g}")

    # Preserve a lightweight rate marker for forms such as ``100 req/min`` and
    # ``100 requests per minute``.  It complements, rather than replaces, the
    # canonical unit feature above.
    if any(token in {"per", "each"} for token in tokens) or "/" in text:
        for number_index, number in values:
            for unit_index, (dimension, _) in unit_positions:
                if abs(number_index - unit_index) <= 3:
                    features.append(f"rate:{dimension}:{number:g}")
                    break
    return features
