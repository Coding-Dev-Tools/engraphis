"""Logical graph-layer classification.

Engraphis stores all graph data in one SQLite file, but edges are tagged with a
Mnemon-style logical overlay so recall, visualization, and exports can select temporal,
entity, causal, and semantic relationships independently or combine them.
"""
from __future__ import annotations

from typing import Optional

from engraphis.core.interfaces import GraphLayer

_TEMPORAL = {
    "after", "before", "during", "follows", "followed_by", "happened_after",
    "happened_before", "next", "precedes", "previous", "simultaneous_with",
}
_CAUSAL = {
    "blocks", "caused_by", "causes", "contributes_to", "depends_on", "enables",
    "fixed_by", "fixes", "leads_to", "prevents", "results_in", "triggered_by",
    "triggers",
}
_ENTITY = {
    "belongs_to", "calls", "contains", "defined_by", "defines", "depends_on_code",
    "has", "implements", "imports", "inherits", "located_in", "member_of", "mentions",
    "owned_by", "owns", "part_of", "profiles", "references", "tests", "uses",
    "works_at",
}

# Sync format v1 has no per-field clock for link metadata. This ordering is a stable
# join for concurrent classifications: the generic semantic overlay yields to a more
# specific entity/causal/temporal classification, and peers converge independent of
# bundle arrival order.
_MERGE_RANK = {
    GraphLayer.SEMANTIC: 0,
    GraphLayer.ENTITY: 1,
    GraphLayer.CAUSAL: 2,
    GraphLayer.TEMPORAL: 3,
}


def normalize_graph_layer(value: object, relation: str = "") -> GraphLayer:
    """Return an explicit layer or infer one from ``relation``."""
    if isinstance(value, GraphLayer):
        return value
    raw = str(value or "").strip().lower()
    if raw:
        try:
            return GraphLayer(raw)
        except ValueError:
            pass
    return infer_graph_layer(relation)


def merge_graph_layers(left: object, right: object, relation: str = "") -> GraphLayer:
    """Deterministically merge concurrent graph-layer classifications."""
    a = normalize_graph_layer(left, relation)
    b = normalize_graph_layer(right, relation)
    return max((a, b), key=lambda layer: (_MERGE_RANK[layer], layer.value))


def infer_graph_layer(relation: Optional[str]) -> GraphLayer:
    """Classify a relationship label into one of the four logical overlays."""
    rel = str(relation or "").strip().lower().replace(" ", "_").replace("-", "_")
    if rel in _TEMPORAL or rel.endswith(("_before", "_after")):
        return GraphLayer.TEMPORAL
    if rel in _CAUSAL or rel.startswith(("cause", "trigger", "fix")):
        return GraphLayer.CAUSAL
    if rel in _ENTITY:
        return GraphLayer.ENTITY
    return GraphLayer.SEMANTIC
