"""Core interface contracts and record types.

Define interfaces *before* implementations. Concrete backends — vector index,
embedder, reranker, graph store, LLM — implement these Protocols, so swapping
``sqlite-vec`` for Qdrant, a local embedder for an API, or a Python scorer for a
Rust one is a configuration change rather than a refactor.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Literal, Optional, Protocol, runtime_checkable

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
    # TEAM scope: forward-compatibility marker only. Read-side wiring
    # (SearchFilter predicates in store.list_memories / edges_in_scope,
    # RBAC checks against team_members, promotion semantics) belongs in
    # a dedicated PR. Do not expand wiring inside CodeQL/security fix
    # branches — dead scope surface is a liability until the full
    # feature ships together.
    TEAM = "team"
    USER = "user"


class GraphLayer(str, Enum):
    """Logical graph overlays kept inside the same local SQLite database."""
    TEMPORAL = "temporal"
    ENTITY = "entity"
    CAUSAL = "causal"
    SEMANTIC = "semantic"


def _finite_timestamp(value: Optional[float], name: str) -> Optional[float]:
    """Normalize public temporal anchors and reject SQLite's non-finite values."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite timestamp")
    try:
        timestamp = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite timestamp") from exc
    if not math.isfinite(timestamp):
        raise ValueError(f"{name} must be a finite timestamp")
    return timestamp


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
    subject_key: str = ""                # stable optional claim subject
    claim_kind: str = ""                 # optional claim predicate/category
    pinned: bool = False
    sensitivity: str = "normal"          # normal | sensitive | secret
    provenance: dict[str, Any] = field(default_factory=dict)
    embedding: Optional[np.ndarray] = None
    valid_to_recorded_at: Optional[float] = None  # when valid_to was learned
    # Keep additions after the established positional fields; callers may construct
    # records positionally even though keyword construction is preferred.
    pinned_at: Optional[float] = None     # system-time when a pin last became effective
    unpinned_at: Optional[float] = None   # system-time when an unpin became effective
    confidence: float = 1.0          # 0..1, extraction/model confidence (scoring multiplier)
    # team_id: forward-compat only. Stored and round-tripped but not yet
    # used by read-side filters; see Scope.TEAM note above.
    team_id: Optional[str] = None




@dataclass
class SearchFilter:
    """Scope + temporal filter applied to every read (§7.1)."""
    workspace_id: Optional[str] = None
    repo_id: Optional[str] = None
    session_id: Optional[str] = None
    scopes: Optional[list[Scope]] = None
    mtypes: Optional[list[MemoryType]] = None
    graph_layers: Optional[list[GraphLayer]] = None
    # ``as_of`` remains a compatibility alias for the world-time ``valid_at``
    # anchor.  New callers can independently select what was true and what
    # had been learned at that time.
    as_of: Optional[float] = None
    # Contextual recall sees broader scopes as ancestors: a repo read can see that
    # repo plus workspace/user memories, and a session read can additionally see its
    # exact session.  Storage/governance queries stay exact unless they opt in.
    include_ancestors: bool = False
    # Appended after every 1.x field so positional construction remains compatible.
    valid_at: Optional[float] = None
    known_at: Optional[float] = None
    # Team scope: when set, team-scoped memories visible to this team are included.
    # The caller's team membership and role are resolved at the service/store layer.
    # team_id / caller_id: forward-compat only. Neither is yet consumed
    # by store predicates or RBAC checks; see Scope.TEAM note above.
    team_id: Optional[str] = None
    caller_id: Optional[str] = None

    def __post_init__(self) -> None:
        self.as_of = _finite_timestamp(self.as_of, "as_of")
        self.valid_at = _finite_timestamp(self.valid_at, "valid_at")
        self.known_at = _finite_timestamp(self.known_at, "known_at")
        if self.as_of is not None and self.valid_at is not None:
            if self.as_of != self.valid_at:
                raise ValueError("as_of and valid_at must match when both are supplied")
        # Keep legacy backends that read ``as_of`` correct as callers move to
        # the less ambiguous ``valid_at`` name.
        self.valid_at = self.valid_at if self.valid_at is not None else self.as_of
        self.as_of = self.valid_at

    @property
    def historical(self) -> bool:
        """Whether either time axis was explicitly anchored by the caller."""
        return self.valid_at is not None or self.known_at is not None


@dataclass
class Candidate:
    """A retrieval candidate with its fused score and originating arm."""
    id: str
    score: float
    arm: str = ""                    # semantic | lexical | graph | fused
    record: Optional[MemoryRecord] = None


@dataclass
class PackedChunk:
    """One source excerpt selected by a context-packing implementation."""
    id: str
    excerpt: str
    tokens: int
    truncated: bool = False
    reason: str = ""


@dataclass
class ContextUsage:
    """Token accounting emitted by a context-packing implementation."""
    budget_tokens: int
    context_tokens: int
    source_tokens: int
    saved_tokens: int
    savings_ratio: float
    packed_count: int
    omitted_count: int
    token_counter: str = "estimate_tokens"


@dataclass(frozen=True)
class PlannedQuery:
    """One bounded retrieval query emitted by a ``QueryPlanner``.

    ``priority`` is one-based: lower values contribute more weight during rank
    fusion. ``mtypes`` narrows only this query; the caller's scope, temporal, and
    trust filters remain mandatory for every planned query.
    """
    text: str
    priority: int = 1
    profile: str = "balanced"
    mtypes: tuple[MemoryType, ...] = ()


@dataclass(frozen=True)
class RetrievalPlan:
    """A bounded, inspectable plan for one recall request."""
    queries: tuple[PlannedQuery, ...]
    mtype_limits: dict[MemoryType, int] = field(default_factory=dict)
    reason_codes: tuple[str, ...] = ()


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
    layer: Optional[GraphLayer] = None
    weight: float = 1.0
    workspace_id: Optional[str] = None
    repo_id: Optional[str] = None
    valid_from: Optional[float] = None
    valid_to: Optional[float] = None
    ingested_at: Optional[float] = None
    expired_at: Optional[float] = None
    provenance: dict[str, Any] = field(default_factory=dict)
    valid_to_recorded_at: Optional[float] = None


@dataclass
class ExtractedFact:
    """One distilled, self-contained fact produced by an ``Extractor`` (§8.2).

    ``mtype``/``importance``/``keywords`` are *hints* — the write path may override
    them; ``content`` is the only required field. ``metadata`` is optional structured
    extraction payload (entities/relations/confidence, etc.) and is merged into the
    stored memory metadata by the ingest path.
    """
    content: str
    title: str = ""
    mtype: Optional[MemoryType] = None
    importance: float = 0.0
    keywords: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RetentionDecision:
    """Optional host/LLM supervision signal for a new memory.

    ``retain=False`` never hard-deletes or silently drops a write. The engine records
    the recommendation and applies a short-lived stability preset so normal local
    retention/consolidation policy can make the eventual governed decision.
    """
    label: str = "normal"
    retain: bool = True
    importance: Optional[float] = None
    stability: Optional[float] = None
    reason: str = ""


@dataclass
class ResourceDocument:
    """Text and provenance extracted from a local file/media resource."""
    text: str
    title: str = ""
    kind: str = "document"
    media_type: str = "text/plain"
    metadata: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


@dataclass
class SchemaSnapshot:
    """Portable database-schema graph produced by an optional introspector."""
    title: str
    text: str
    entities: list[dict[str, Any]] = field(default_factory=list)
    relations: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


# ── Protocols ────────────────────────────────────────────────────────────────

@runtime_checkable
class Embedder(Protocol):
    """Turns text or code into dense vectors. Default local; API optional."""
    @property
    def dim(self) -> int: ...
    @property
    def supports_semantic_search(self) -> bool: ...
    @property
    def embedding_mode(self) -> str: ...
    def embed(self, texts: list[str], *, kind: Literal["text", "code"] = "text") -> np.ndarray: ...


def embedder_capabilities(embedder: Any) -> dict[str, Any]:
    """Return public retrieval capabilities for an embedder.

    Semantic retrieval is opt-in: an embedder that does not explicitly advertise it is
    treated as degraded.  This prevents a feature-hashing fallback (or an incomplete
    third-party adapter) from being presented as a semantic model merely because it
    produces vectors.  The returned shape is transport-safe and is included in recall
    and grounded-answer responses.
    """
    semantic_support = bool(getattr(embedder, "supports_semantic_search", False))
    mode = str(getattr(embedder, "embedding_mode", "") or "").strip().casefold()
    if not mode:
        mode = "semantic" if semantic_support else "unknown"
    degraded_mode = not semantic_support
    reason = ""
    if degraded_mode:
        reason = str(getattr(embedder, "semantic_support_reason", "") or "").strip()
        if not reason:
            reason = (
                "embedding backend did not declare semantic capability; semantic "
                "vector retrieval is disabled"
            )
    return {
        "degraded_mode": degraded_mode,
        "semantic_support": semantic_support,
        "embedding_mode": mode,
        "degraded_reason": reason,
    }


@runtime_checkable
class VectorIndex(Protocol):
    """Approximate nearest-neighbour index over embeddings (§6.2).

    ``commit=False`` keeps derived-index writes inside a caller-owned transaction;
    existing callers retain the historical committing default.
    """
    def upsert(self, ids: list[str], vecs: np.ndarray, meta: Optional[list[dict]] = None,
               *, commit: bool = True) -> None: ...
    def search(self, vec: np.ndarray, k: int, *, filter: Optional[SearchFilter] = None) -> list[tuple[str, float]]: ...
    def delete(self, ids: list[str], *, commit: bool = True) -> None: ...


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
    def neighbors(self, node_ids: list[str], *, hops: int = 1, at: Optional[float] = None,
                  layers: Optional[list["GraphLayer"]] = None) -> list[Edge]: ...
    def ppr(self, seeds: list[str], *, at: Optional[float] = None) -> dict[str, float]: ...


@runtime_checkable
class Reranker(Protocol):
    """Cross-encoder reranking of fused candidates (§7.1 stage 4)."""
    def rerank(self, query: str, candidates: list[Candidate], k: int) -> list[Candidate]: ...


@runtime_checkable
class ContextPacker(Protocol):
    """Choose budgeted, explainable source excerpts for an agent context."""
    def pack(self, query: str, candidates: list[Candidate], token_budget: int
             ) -> tuple[str, list[PackedChunk], ContextUsage]: ...
    def count_tokens(self, text: str) -> int: ...


@runtime_checkable
class RetrievalPolicy(Protocol):
    """Select a named retrieval profile without coupling core to a backend."""
    def profile(self, query: str) -> str: ...


@runtime_checkable
class CandidateDepthPolicy(Protocol):
    """Select a bounded per-arm candidate depth for one recall request."""
    def candidate_depth(self, query: str, *, k: int, ceiling: int,
                        profile: str, mode: str) -> tuple[int, str]: ...


@dataclass(frozen=True)
class GraphTraversalPlan:
    """Inspectable, bounded layer preferences for one graph-retrieval query.

    Layer values are soft multipliers, never permissions: ``SearchFilter`` remains
    the only mechanism allowed to include or exclude graph layers, scope, temporal
    visibility, or trust-sensitive records.  An empty tuple means uniform weights
    and is deliberately equivalent to the historical PPR behavior.
    """
    intent: str = "uniform"
    layer_weights: tuple[tuple[GraphLayer, float], ...] = ()
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Canonicalize policy output before it can affect graph ranking.

        Policies are injected extension code.  Keeping their data contract finite,
        typed, and duplicate-free makes failure fall back to uniform traversal
        rather than letting malformed weights turn into an availability issue or
        a non-deterministic first-match choice.
        """
        normalized = []
        seen = set()
        for entry in self.layer_weights:
            if not isinstance(entry, tuple) or len(entry) != 2:
                raise ValueError("graph traversal layer_weights must be (layer, weight) pairs")
            raw_layer, raw_weight = entry
            layer = GraphLayer(raw_layer)
            if layer in seen:
                raise ValueError("graph traversal layer_weights may not repeat a layer")
            try:
                weight = float(raw_weight)
            except (TypeError, ValueError) as exc:
                raise ValueError("graph traversal weights must be finite numbers") from exc
            if not math.isfinite(weight):
                raise ValueError("graph traversal weights must be finite numbers")
            seen.add(layer)
            normalized.append((layer, weight))
        reason_codes = (
            (self.reason_codes,)
            if isinstance(self.reason_codes, str)
            else tuple(str(code) for code in self.reason_codes)
        )
        object.__setattr__(self, "intent", str(self.intent or "uniform"))
        object.__setattr__(self, "layer_weights", tuple(normalized))
        object.__setattr__(self, "reason_codes", reason_codes)

    def multiplier(self, layer: GraphLayer) -> float:
        """Return a safe non-zero multiplier for ``layer``.

        The bounds retain weak reachability through non-preferred layers and stop
        injected policies from turning a local graph edge into an unbounded score
        amplification mechanism.
        """
        for candidate, value in self.layer_weights:
            if candidate == layer:
                try:
                    numeric = float(value)
                    if not math.isfinite(numeric):
                        return 1.0
                    return min(4.0, max(0.25, numeric))
                except (TypeError, ValueError):
                    return 1.0
        return 1.0

    def as_dict(self) -> dict[str, object]:
        return {
            "intent": self.intent,
            "layer_weights": {
                layer.value: self.multiplier(layer)
                for layer in GraphLayer
            },
            "reason_codes": list(self.reason_codes),
        }


@runtime_checkable
class GraphTraversalPolicy(Protocol):
    """Choose soft graph-layer weights without coupling core to an LLM backend."""
    def plan(self, query: str, *, filter: Optional[SearchFilter] = None) -> GraphTraversalPlan: ...


@runtime_checkable
class QueryPlanner(Protocol):
    """Produce a retrieval plan without coupling core to an LLM backend."""
    def plan(self, query: str, *, filter: Optional[SearchFilter] = None,
             timeout_s: Optional[float] = None) -> RetrievalPlan: ...


@runtime_checkable
class LLM(Protocol):
    """External or local model for synthesis and structured extraction (§8.2)."""
    def complete(self, messages: list[dict], **kw: Any) -> str: ...
    def extract_json(self, prompt: str, schema: dict, **kw: Any) -> Any: ...


@runtime_checkable
class Extractor(Protocol):
    """Distills raw text into discrete memory-worthy facts before storage (§8.2).

    The offline default is a no-op passthrough (the caller's text is stored as-is,
    exactly today's behaviour); an LLM-backed implementation can be swapped in by
    configuration — never a hard dependency of ``core/`` (AGENTS.md §3.8).
    """
    def extract(self, text: str, *, context: str = "") -> list[ExtractedFact]: ...


@runtime_checkable
class RetentionSupervisor(Protocol):
    """Optional host-controlled importance/retention classifier."""
    def decide(self, content: str, *, title: str = "", mtype: MemoryType,
               metadata: Optional[dict] = None) -> RetentionDecision: ...


@runtime_checkable
class ResourceExtractor(Protocol):
    """Turns local document/media bytes into text without changing memory semantics."""
    def extract_bytes(self, name: str, data: bytes) -> ResourceDocument: ...
    def extract_path(self, path: str) -> ResourceDocument: ...


@runtime_checkable
class SchemaIntrospector(Protocol):
    """Reads a live database catalog and returns a transport-neutral schema graph."""
    def inspect(self, dsn: str, *, schemas: Optional[list[str]] = None) -> SchemaSnapshot: ...


@runtime_checkable
class SyncTransport(Protocol):
    """Moves opaque sync bundles between devices (cloud-sync layer, core/sync.py).

    Deliberately dumb: it stores and retrieves named byte blobs and knows nothing
    about memory semantics, so a shared folder (Dropbox/iCloud/Syncthing/git), an
    object store, or a managed relay are interchangeable behind these three calls —
    same interface-first swap as ``VectorIndex``/``Embedder``.
    A transport may encrypt ``data`` in ``push`` and decrypt in ``pull``; the sync
    engine treats every pulled bundle as untrusted regardless.
    """
    def push(self, name: str, data: bytes) -> None: ...
    def pull(self) -> Iterable[tuple[str, bytes]]: ...
    def list_names(self) -> list[str]: ...


# Interface contracts only; concrete implementations live in engraphis.backends.
