"""Core interface contracts and record types.

Define interfaces *before* implementations. Concrete backends — vector index,
embedder, reranker, graph store, LLM — implement these Protocols, so swapping
``sqlite-vec`` for Qdrant, a local embedder for an API, or a Python scorer for a
Rust one is a configuration change rather than a refactor.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, Optional, Protocol, runtime_checkable

import numpy as np


# ── Enums ────────────────────────────────────────────────────────────────────

class MemoryType(str, Enum):
    """The four memory types, each with a distinct lifecycle (§5.2)."""
    WORKING = "working"        # transient state for the current step/session
    EPISODIC = "episodic"      # what happened — events, decisions, failures
    SEMANTIC = "semantic"      # de-contextualized facts, preferences, conventions
    PROCEDURAL = "procedural"  # reusable skills / playbooks / recipes


class Scope(str, Enum):
    """Visibility/ownership level, narrowest → broadest (§5.1)."""
    SESSION = "session"
    REPO = "repo"
    WORKSPACE = "workspace"
    USER = "user"


# ── Records ──────────────────────────────────────────────────────────────────

@dataclass
class MemoryRecord:
    """The atomic memory note (§5.3). Bi-temporal, typed, scoped, provenanced."""
    id: str
    content: str
    mtype: MemoryType = MemoryType.SEMANTIC
    scope: Scope = Scope.REPO
    workspace_id: Optional[str] = None
    repo_id: Optional[str] = None
    session_id: Optional[str] = None
    title: str = ""
    summary: str = ""
    keywords: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    importance: float = 0.0          # 0..1, salience scored at creation
    surprise: float = 1.0            # novelty weight (1 + |prediction error|)
    stability: float = 1.0           # Ebbinghaus S; grows with reinforcement
    access_count: int = 0
    last_access: Optional[float] = None
    valid_from: Optional[float] = None   # world-time: when the fact became true
    valid_to: Optional[float] = None     # world-time: when it stopped being true
    ingested_at: Optional[float] = None  # system-time: when we learned it
    expired_at: Optional[float] = None   # system-time: when we retired it
    pinned: bool = False
    sensitivity: str = "normal"          # normal | sensitive | secret
    provenance: dict[str, Any] = field(default_factory=dict)
    embedding: Optional[np.ndarray] = None


@dataclass
class SearchFilter:
    """Scope + temporal filter applied to every read (§7.1)."""
    workspace_id: Optional[str] = None
    repo_id: Optional[str] = None
    session_id: Optional[str] = None
    scopes: Optional[list[Scope]] = None
    mtypes: Optional[list[MemoryType]] = None
    as_of: Optional[float] = None    # bi-temporal time anchor; None = now


@dataclass
class Candidate:
    """A retrieval candidate with its fused score and originating arm."""
    id: str
    score: float
    arm: str = ""                    # semantic | lexical | graph | fused
    record: Optional[MemoryRecord] = None


@dataclass
class Node:
    """A knowledge-graph node (entity or concept)."""
    id: str
    name: str
    ntype: str = ""
    workspace_id: Optional[str] = None
    repo_id: Optional[str] = None
    canonical_id: Optional[str] = None   # cross-repo entity resolution


@dataclass
class Edge:
    """A bi-temporal knowledge-graph edge (§8.3)."""
    id: str
    src: str
    dst: str
    relation: str
    weight: float = 1.0
    workspace_id: Optional[str] = None
    repo_id: Optional[str] = None
    valid_from: Optional[float] = None
    valid_to: Optional[float] = None
    ingested_at: Optional[float] = None
    expired_at: Optional[float] = None
    provenance: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExtractedFact:
    """One distilled, self-contained fact produced by an ``Extractor`` (§8.2).

    ``mtype``/``importance``/``keywords`` are *hints* — the write path may override
    them; ``content`` is the only required field.
    """
    content: str
    title: str = ""
    mtype: Optional[MemoryType] = None
    importance: float = 0.0
    keywords: list[str] = field(default_factory=list)


# ── Protocols ────────────────────────────────────────────────────────────────

@runtime_checkable
class Embedder(Protocol):
    """Turns text or code into dense vectors. Default local; API optional."""
    @property
    def dim(self) -> int: ...
    def embed(self, texts: list[str], *, kind: Literal["text", "code"] = "text") -> np.ndarray: ...


@runtime_checkable
class VectorIndex(Protocol):
    """Approximate nearest-neighbour index over embeddings (§6.2)."""
    def upsert(self, ids: list[str], vecs: np.ndarray, meta: Optional[list[dict]] = None) -> None: ...
    def search(self, vec: np.ndarray, k: int, *, filter: Optional[SearchFilter] = None) -> list[tuple[str, float]]: ...
    def delete(self, ids: list[str]) -> None: ...


@runtime_checkable
class LexicalIndex(Protocol):
    """BM25 / full-text arm of hybrid retrieval (§7.1)."""
    def search(self, query: str, k: int, *, filter: Optional[SearchFilter] = None) -> list[tuple[str, float]]: ...


@runtime_checkable
class GraphStore(Protocol):
    """Bi-temporal knowledge graph with PPR (§6.3, §13.5)."""
    def upsert_node(self, node: Node) -> None: ...
    def upsert_edge(self, edge: Edge) -> None: ...
    def invalidate_edge(self, edge_id: str, at: float) -> None: ...
    def neighbors(self, node_ids: list[str], *, hops: int = 1, at: Optional[float] = None) -> list[Edge]: ...
    def ppr(self, seeds: list[str], *, at: Optional[float] = None) -> dict[str, float]: ...


@runtime_checkable
class Reranker(Protocol):
    """Cross-encoder reranking of fused candidates (§7.1 stage 4)."""
    def rerank(self, query: str, candidates: list[Candidate], k: int) -> list[Candidate]: ...


@runtime_checkable
class LLM(Protocol):
    """External or local model for synthesis and structured extraction (§8.2)."""
    def complete(self, messages: list[dict], **kw: Any) -> str: ...
    def extract_json(self, prompt: str, schema: dict) -> dict: ...


@runtime_checkable
class Extractor(Protocol):
    """Distills raw text into discrete memory-worthy facts before storage (§8.2).

    The offline default is a no-op passthrough (the caller's text is stored as-is,
    exactly today's behaviour); an LLM-backed implementation can be swapped in by
    configuration — never a hard dependency of ``core/`` (AGENTS.md §3.8).
    """
    def extract(self, text: str, *, context: str = "") -> list[ExtractedFact]: ...


# Interface contracts only; concrete implementations live in engraphis.backends.
