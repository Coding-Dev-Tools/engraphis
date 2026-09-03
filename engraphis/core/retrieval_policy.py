"""Deterministic retrieval-profile selection.

``balanced`` preserves the established hybrid path.  ``fast`` is an explicit
small-vault profile that keeps vector + lexical recall but skips graph traversal.
``auto`` is explicit and conservative: it only selects a specialized profile when
the query has a strong, locally-observable signal.  This keeps automatic routing
measurable and prevents an unbenchmarked policy change from silently altering
existing callers.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


RETRIEVAL_PROFILES = frozenset({"balanced", "auto", "fast", "lexical", "graph", "code"})
CANDIDATE_DEPTH_MODES = frozenset({"fixed", "adaptive"})

_CODE_RE = re.compile(
    r"(?:\w+[./\\])+\w+|::|->|\b(?:class|def|function|import|interface|module|struct)\b|"
    r"\b[A-Za-z_]\w*\([^)]*\)",
    re.IGNORECASE,
)
_GRAPH_RE = re.compile(
    r"\b(?:calls?|causes?|connected|connections?|depends?|impact|path|related|relationship|superseded|supersedes|why)\b",
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
    # The vector backend returns cosine similarity while the other arms use opaque
    # scores, so a singleton vector result min-max normalizes to 1.0 even when its
    # raw cosine is near zero. Calibration multiplies rank evidence by the clamped
    # raw cosine, killing that false 1.0. Enabled by default on every profile.
    semantic_confidence_calibration: bool = True
    # Blend between fused retrieval evidence and cross-encoder rerank evidence,
    # applied after per-source normalization: (fusion_weight, rerank_weight).
    rerank_blend: tuple[float, float] = (0.7, 0.3)
    # Deterministic post-normalization preference for consolidated digests and
    # entity profiles over the raw episodes they summarize. Small by design: a
    # raw episode that actually matches the query still outranks a digest the
    # query merely grazes.
    consolidation_bonus: float = 0.05
    # Default per-arm candidate depth when the operator sets neither
    # ``arm_candidate_k_cap=`` nor ``ENGRAPHIS_RECALL_ARM_CANDIDATE_K``. Keeps
    # the prompt-only escalation loop bounded without touching the caller's
    # requested candidate_k floor.
    arm_candidate_k_default: int = 200
    # Opt-in graph-seed fallback: when the query names no known entity, project
    # lexical top-m hits onto entities they mention and link (paraphrase rescue
    # for the graph arm). Off by default so published retrieval-evidence numbers
    # stay byte-stable; enable via ProfileConfig or per-recall arm_config.
    graph_seed_fallback: bool = False


_CONFIGS = {
    "balanced": ProfileConfig("balanced", True, True, True, False),
    # Small-vault / latency-sensitive path: retain dense + lexical evidence while
    # avoiding graph traversal when multi-hop evidence is not the caller's goal.
    "fast": ProfileConfig("fast", True, True, False, False),
    "lexical": ProfileConfig("lexical", False, True, False, False),
    # Specialized profiles retain supporting arms but make their declared
    # evidence type decisive. Scoring knobs (calibration, rerank blend,
    # consolidation bonus, arm depth) are shared profile defaults; only the
    # arm scales and presence bonuses above specialize behavior.
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

    def candidate_depth(
        self,
        query: str,
        *,
        k: int,
        ceiling: int,
        profile: str,
        mode: str,
    ) -> tuple[int, str]:
        """Return a deterministic bounded candidate depth and its explanation.

        ``fixed`` preserves the historical depth exactly.  The explicit ``adaptive``
        mode reduces routine lexical/balanced recalls but deliberately retains a
        wider pool when the selected profile depends on graph traversal or code
        bridges.  It is a per-arm cap, not a result-count change.
        """
        limit = max(1, int(ceiling))
        requested_mode = str(mode or "fixed").strip().casefold()
        if requested_mode not in CANDIDATE_DEPTH_MODES:
            choices = ", ".join(sorted(CANDIDATE_DEPTH_MODES))
            raise ValueError(f"candidate_depth must be one of: {choices}")
        if requested_mode == "fixed":
            return limit, "fixed requested depth"

        # Lower bounds are intentionally tied to output k. The graph/code floors
        # remain larger because their useful evidence may enter through a bridge
        # that is not top-ranked by the first retrieval arm.
        floors = {
            "fast": max(8, k * 2),
            "lexical": max(8, k * 2),
            "balanced": max(12, k * 3),
            "graph": max(30, k * 6),
            "code": max(30, k * 6),
        }
        selected = str(profile or "balanced").strip().casefold()
        # ``balanced`` is the backwards-compatible default, but adaptive depth
        # can still use a high-confidence query signal.  This matters when a
        # caller intentionally keeps the balanced scoring profile while opting
        # into candidate-depth control: relationship and code queries often need
        # a wider first-stage pool for graph/code bridge evidence.  Do not let
        # query text override an explicitly specialized profile.
        if selected == "balanced":
            query_profile = self.profile(query)
            if query_profile in {"graph", "code"}:
                selected = query_profile
                return min(limit, floors[selected]), f"adaptive {selected} intent floor"
        depth = min(limit, floors.get(selected, max(12, k * 3)))
        return depth, f"adaptive {selected} floor"
