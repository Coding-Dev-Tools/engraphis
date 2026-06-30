"""Engraphis core — interfaces, identifiers, schema, and the v2 store.

This package is the architectural backbone defined in MASTER_PLAN.md §6.3 and §12.
Everything in the system is built against the Protocols in ``interfaces`` so that
concrete implementations (vector index, embedder, reranker, graph, LLM) can be
swapped — including Python → Rust hot paths — without rearchitecting.
"""
from __future__ import annotations

from engraphis.core.ids import new_id, ulid
from engraphis.core.interfaces import (
    Candidate,
    Edge,
    Embedder,
    GraphStore,
    LexicalIndex,
    LLM,
    MemoryRecord,
    MemoryType,
    Node,
    Reranker,
    Scope,
    SearchFilter,
    VectorIndex,
)

__all__ = [
    "new_id",
    "ulid",
    "Candidate",
    "Edge",
    "Embedder",
    "GraphStore",
    "LexicalIndex",
    "LLM",
    "MemoryRecord",
    "MemoryType",
    "Node",
    "Reranker",
    "Scope",
    "SearchFilter",
    "VectorIndex",
]
