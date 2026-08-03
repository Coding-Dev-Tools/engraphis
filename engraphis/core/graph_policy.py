"""Deterministic, opt-in graph-traversal policies.

Policies produce *soft* preferences for Engraphis's existing logical graph layers.
They do not alter SearchFilter enforcement, data visibility, graph construction, or
the local/offline default.  The uniform policy is intentionally byte-for-byte
equivalent to the former weight calculation in the PPR graph arm.
"""
from __future__ import annotations

import re
from typing import Optional

from engraphis.core.interfaces import (
    GraphLayer,
    GraphTraversalPlan,
    SearchFilter,
)


_TOKEN_RE = re.compile(r"[a-z0-9_]+")
_CAUSAL_TERMS = frozenset({
    "because", "cause", "caused", "causes", "effect", "fix", "fixed",
    "impact", "reason", "reasons", "result", "resulted", "trigger", "triggered",
    "why",
})
_TEMPORAL_TERMS = frozenset({
    "after", "before", "during", "earlier", "first", "last", "later", "latest",
    "next", "previous", "then", "timeline", "when",
})
_ENTITY_TERMS = frozenset({
    "belongs", "called", "entity", "member", "owner", "relationship", "related",
    "who", "whose",
})
_PREFERRED_WEIGHT = 4.0
_FALLBACK_WEIGHT = 0.25


class UniformGraphTraversalPolicy:
    """The default policy: preserve the historical, layer-uniform PPR graph arm."""

    identity = "engraphis.graph_traversal.uniform.v1"

    def plan(
        self,
        query: str,
        *,
        filter: Optional[SearchFilter] = None,
    ) -> GraphTraversalPlan:
        del query, filter
        return GraphTraversalPlan()


class DeterministicIntentGraphTraversalPolicy:
    """Prefer one graph layer for strong, dependency-free query signals.

    This deliberately does not attempt a broad natural-language understanding
    problem.  Ambiguous queries stay uniform; a selected layer still leaves every
    visible alternative reachable at a fixed non-zero floor.
    """

    identity = "engraphis.graph_traversal.intent_layered.v1"

    def plan(
        self,
        query: str,
        *,
        filter: Optional[SearchFilter] = None,
    ) -> GraphTraversalPlan:
        del filter  # SearchFilter is a hard retrieval boundary, not a routing hint.
        tokens = frozenset(_TOKEN_RE.findall(str(query or "").casefold()))
        preferred, reason = self._preferred_layer(tokens)
        if preferred is None:
            return GraphTraversalPlan()
        return GraphTraversalPlan(
            intent=preferred.value,
            layer_weights=tuple(
                (layer, _PREFERRED_WEIGHT if layer == preferred else _FALLBACK_WEIGHT)
                for layer in GraphLayer
            ),
            reason_codes=(reason,),
        )

    @staticmethod
    def _preferred_layer(tokens: frozenset[str]) -> tuple[Optional[GraphLayer], str]:
        # Clear interrogatives are stronger evidence than a relation word elsewhere
        # in the question: "when did X cause Y?" is temporal, while "why did X
        # happen after Y?" remains causal.  This small precedence rule avoids
        # pretending that a bag of cue words is a general NLU classifier.
        if "why" in tokens:
            return GraphLayer.CAUSAL, "causal_query_cue"
        if "when" in tokens:
            return GraphLayer.TEMPORAL, "temporal_query_cue"
        if {"who", "whose"} & tokens:
            return GraphLayer.ENTITY, "entity_query_cue"
        if tokens & _TEMPORAL_TERMS:
            return GraphLayer.TEMPORAL, "temporal_query_cue"
        if tokens & _CAUSAL_TERMS:
            return GraphLayer.CAUSAL, "causal_query_cue"
        if tokens & _ENTITY_TERMS:
            return GraphLayer.ENTITY, "entity_query_cue"
        return None, ""
