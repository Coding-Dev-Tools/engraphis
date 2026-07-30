"""Deterministic retrieval-profile selection.

``balanced`` preserves the established hybrid path.  ``auto`` is explicit and
conservative: it only selects a specialized profile when the query has a strong,
locally-observable signal.  This keeps automatic routing measurable and prevents
an unbenchmarked policy change from silently altering existing callers.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


RETRIEVAL_PROFILES = frozenset({"balanced", "auto", "lexical", "graph", "code"})

_CODE_RE = re.compile(
    r"(?:\w+[./\\])+\w+|::|->|\b(?:class|def|function|import|module)\b|"
    r"\b[A-Za-z_]\w*\([^)]*\)",
    re.IGNORECASE,
)
_GRAPH_RE = re.compile(
    r"\b(?:calls?|causes?|depends?|impact|path|related|relationship|why)\b",
    re.IGNORECASE,
)
_LEXICAL_RE = re.compile(
    r"\"[^\"]+\"|'[^']+'|\b[A-Z][A-Z0-9_]{2,}\b|"
    r"\b(?:exact|identifier|literal|named|spelled)\b",
)


@dataclass(frozen=True)
class ProfileConfig:
    name: str
    vector: bool
    lexical: bool
    graph: bool
    code: bool
    semantic_scale: float = 1.0
    lexical_scale: float = 1.0
    graph_scale: float = 1.0
    code_scale: float = 1.0
    graph_presence_bonus: float = 0.0
    code_presence_bonus: float = 0.0


_CONFIGS = {
    "balanced": ProfileConfig("balanced", True, True, True, False),
    "lexical": ProfileConfig("lexical", False, True, False, False),
    # Specialized profiles retain supporting arms but make their declared
    # evidence type decisive. ``balanced`` stays byte-for-byte equivalent to
    # the established scoring behavior, and ``auto`` remains opt-in.
    "graph": ProfileConfig(
        "graph", True, True, True, False,
        graph_scale=3.0, graph_presence_bonus=1.5,
    ),
    "code": ProfileConfig(
        "code", True, True, True, True,
        code_scale=3.0, code_presence_bonus=1.5,
    ),
}


def profile_config(name: str) -> ProfileConfig:
    """Return a validated immutable configuration for a concrete profile."""
    normalized = str(name or "").strip().casefold()
    if normalized not in _CONFIGS:
        choices = ", ".join(sorted(_CONFIGS))
        raise ValueError(f"retrieval profile must resolve to one of: {choices}")
    return _CONFIGS[normalized]


class DeterministicRetrievalPolicy:
    """Offline automatic profile selector with stable, inspectable rules."""

    identity = "engraphis.deterministic.v1"

    def profile(self, query: str) -> str:
        if _CODE_RE.search(query or ""):
            return "code"
        if _GRAPH_RE.search(query or ""):
            return "graph"
        if _LEXICAL_RE.search(query or ""):
            return "lexical"
        return "balanced"

    def resolve(self, requested: str, query: str) -> ProfileConfig:
        normalized = str(requested or "balanced").strip().casefold()
        if normalized not in RETRIEVAL_PROFILES:
            choices = ", ".join(sorted(RETRIEVAL_PROFILES))
            raise ValueError(f"retrieval_profile must be one of: {choices}")
        selected = self.profile(query) if normalized == "auto" else normalized
        return profile_config(selected)
