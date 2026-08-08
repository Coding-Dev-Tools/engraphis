"""MemoryEngine — the high-level facade the API/MCP layer calls.

Wires together store + embedder + vector index + reranker + recall engine, and exposes
everything an agent does against memory: write (``remember``, with deterministic conflict
resolution), read (``recall``, ``why``, ``timeline``, ``recall_proactive``), governance
(``retire``, ``secure_erase``, ``pin``, ``correct``), session lifecycle (with cross-session handoff), and the
A-MEM-style linking/event primitives (``link``, ``record_event``). Construct with
``MemoryEngine.create(...)`` for sensible, offline-capable defaults, or inject your own
backends for production.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import tempfile
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np

from engraphis.core import scoring
from engraphis.core.adaptive_context import AdaptiveContextResult, fit_recent_history
from engraphis.core.conflicts import detect_conflicts
from engraphis.core.interfaces import (
    MemoryRecord,
    MemoryType,
    GraphTraversalPolicy,
    QueryPlanner,
    RetentionDecision,
    Scope,
    SearchFilter,
    embedder_capabilities,
    embedding_space_fingerprint,
    vector_index_requires_sync,
    vector_index_shares_store_transaction,
)
from engraphis.core.poisoning import (
    REVIEW_APPROVED,
    REVIEW_PENDING,
    PoisoningDecision,
    apply_quarantine_metadata,
    assess_untrusted_payload,
    inspection_eligible,
    metadata_is_quarantined,
    pending_llm_extraction_envelope,
    prompt_eligible,
    provenance_is_approved,
)

from engraphis.core.recall import RecallEngine, RecallResult
from engraphis.core.retrieval_policy import (
    CANDIDATE_DEPTH_MODES,
    RETRIEVAL_PROFILES,
)
from engraphis.core.retention_policy import MAX_STABILITY_DAYS, MIN_STABILITY_DAYS
from engraphis.core.resolve import (
    CONFLICT_RELATION,
    RELATED_SIM_FLOOR,
    Resolution,
    ResolutionOp,
    resolve,
)
from engraphis.core.secrets import reject_secrets
from engraphis.core.store import Store, memory_matches_filter, now_ts
from engraphis.core.textutil import estimate_tokens, jaccard, tokenize


def _safe_upsert(index, ids, vecs, meta=None, *, commit=True):
    """Call ``index.upsert`` with backward-compatible metadata handling.

    Older third-party ``VectorIndex`` implementations may predate the optional
    ``meta`` positional argument and accept only ``(ids, vecs, *, commit)``.
    Passing metadata positionally would raise ``TypeError`` and abort engine
    creation.  This shim tries the full signature first and falls back to the
    legacy shape when needed.
    """
    try:
        index.upsert(ids, vecs, meta, commit=commit)
    except TypeError:
        index.upsert(ids, vecs, commit=commit)

logger = logging.getLogger("engraphis.core.engine")

_ENGINE_FACTORY: Optional[Callable] = None


def configure_engine_factory(factory: Callable) -> None:
    """Install the outer composition provider used by ``MemoryEngine.create``."""
    if not callable(factory):
        raise TypeError("engine factory must be callable")
    global _ENGINE_FACTORY
    _ENGINE_FACTORY = factory

BEST_EFFORT_FAILURE_WARNING_INTERVAL_SECONDS = 60.0

# Sensitivity lattice: a merge keeps the *most restrictive* label of its sources, so
# secret/sensitive content can never be laundered into a lower-sensitivity merged fact.
_SENSITIVITY_RANK = {"normal": 0, "sensitive": 1, "secret": 2}
_SCOPE_RANK = {
    Scope.SESSION: 0,
    Scope.REPO: 1,
    Scope.WORKSPACE: 2,
    Scope.USER: 3,
}

_USER_SCOPE_WRITE_ERROR = (
    "user scope is not supported until owner-aware memories are implemented; "
    "use workspace, repo, or session"
)

# A-MEM-style evolution: how many related neighbors a new memory auto-links to on write.
# Bounded so hub memories don't accrete unbounded link lists (link quality > quantity).
EVOLVE_MAX_LINKS = 3

# The deterministic detector's contradiction/obsolete reports below this severity are
# too weak to justify a durable ``conflicts_with`` relation. The detector floors its
# own reports at 0.74 (numeric) / 0.78 (polarity) / 0.82 (assertion), so this only
# filters out margin-of-error edge cases, keeping the repair trigger conservative.
CONFLICT_MIN_SEVERITY = 0.7

# Deterministic confidence penalty applied to both sides of a persisted conflict
# repair. Bounded and explainable: the ``conflicts_with`` link + audit row make the
# discount auditable, and an explicit human resolution can restore confidence.
CONFLICT_CONFIDENCE_FACTOR = 0.8

# Metadata keys that feed the entity/edge graph under the *trusted*
# provenance.source="structured_extractor" label — i.e. "a configured Extractor produced
# this". See _has_structured_graph_metadata / _trusted_graph_hints.
GRAPH_HINT_KEYS = ("entities", "relations", "structured_extraction")
_INTERNAL_DERIVED_GRAPH_KEY = "unverified_derived_graph"
# Extractors produce these bounded metadata shapes.  Everything else in an
# ``ExtractedFact.metadata`` mapping is untrusted extension data and must not
# override the service-owned ingress envelope (notably provenance/quarantine).
EXTRACTOR_METADATA_KEYS = frozenset(
    (*GRAPH_HINT_KEYS, "chunking", "llm_extraction", "extraction_fallback")
)

# code↔memory linking (see _CodeSymbolMatcher / _link_memory_to_code)
CODE_LINK_MAX_LINKS = 200      # per-memory fan-out cap (unchanged behaviour)
EMBEDDING_REBUILD_BATCH = 200
CODE_MATCHER_CACHE_SIZE = 4    # compiled matchers kept in memory, keyed by repo
# Alternatives per compiled sub-pattern. One giant alternation risks `re`'s internal
# code-size limit on a big repo, so the alternation is chunked; chunking cannot change
# the result because matches are resolved per *offset*, not per pattern (see below).
CODE_ALTERNATION_CHUNK = 500

# Exactly the `\w` class the per-symbol regexes used for their word boundaries, so the
# compiled-alternation path and the old per-symbol path agree character for character.
_WORD_CHAR_RE = re.compile(r"\w")

# Default payload caps for export_code_graph — mirrors MemoryService.graph(), which caps
# nodes and edges because the export is reachable at the lowest ('viewer') role.
CODE_EXPORT_DEFAULT_LIMIT = 5_000
CODE_TRAVERSAL_DEFAULT_CAPACITY = 10_000
CODE_TRAVERSAL_MAX_CAPACITY = 50_000


def _code_traversal_capacity(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("capacity must be an integer between 1 and 50000")
    try:
        capacity = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("capacity must be an integer between 1 and 50000") from exc
    if capacity < 1 or capacity > CODE_TRAVERSAL_MAX_CAPACITY:
        raise ValueError("capacity must be between 1 and 50000")
    return capacity
CODE_EXPORT_MAX_LIMIT = 20_000


def _approved_local_index_roots() -> tuple[str, ...]:
    """Return canonical roots available to the explicit local indexing capability.

    ``ENGRAPHIS_INDEX_ROOTS`` is an operator-owned, path-separator-delimited allow-list.
    ``ENGRAPHIS_HTTP_INDEX_ROOT`` is a separately configured, single HTTP boundary;
    include it here too so the checked path handed off by the HTTP route remains usable
    by the engine.  Configured paths must be absolute: silently resolving a relative
    operator setting against the process working directory would make the boundary
    deployment-dependent.
    When it is unset, the working directory, home directory, and system temporary
    directory preserve the local-first defaults used by ordinary agent/project checkouts.
    """
    def canonical_configured_root(value: str, setting: str) -> str:
        if not os.path.isabs(value):
            raise ValueError(f"{setting} must contain only absolute paths")
        return os.path.normcase(os.path.realpath(os.path.expanduser(value)))

    configured = [
        canonical_configured_root(value.strip(), "ENGRAPHIS_INDEX_ROOTS")
        for value in os.environ.get("ENGRAPHIS_INDEX_ROOTS", "").split(os.pathsep)
        if value.strip()
    ]
    if configured:
        roots = configured
    else:
        roots = [
            os.path.normcase(os.path.realpath(os.getcwd())),
            os.path.normcase(os.path.realpath(os.path.expanduser("~"))),
            os.path.normcase(os.path.realpath(tempfile.gettempdir())),
        ]

    http_root = os.environ.get("ENGRAPHIS_HTTP_INDEX_ROOT", "").strip()
    if http_root:
        roots.append(canonical_configured_root(http_root, "ENGRAPHIS_HTTP_INDEX_ROOT"))

    return tuple(dict.fromkeys(roots))


def _bounded_finite(value, *, default: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return max(minimum, min(maximum, number))


def _rehome_untrusted_graph_hints(metadata: dict,
                                  trusted: Optional[frozenset] = None) -> dict:
    """Strip forged extractor provenance out of a write's metadata.

    ``GRAPH_HINT_KEYS`` are how a configured ``Extractor`` hands the engine graph hints,
    and the engine feeds them into the entity/edge graph tagged
    ``provenance.source="structured_extractor"``. But ``metadata`` is caller-controlled on
    every direct engine path (MCP tool, HTTP route, the sync apply path), and by the time
    it reaches ``_resolve_and_store`` a caller's value is indistinguishable from the
    extractor's own output — so anyone who can write a memory could mint graph edges
    wearing the trusted label for content no extractor ever saw.

    Vouching therefore has to be **out of band**. ``ingest()`` alone knows which keys came
    from ``ExtractedFact.metadata`` rather than from its own ``metadata`` argument, and
    says so through a *keyword argument* — a channel untrusted JSON cannot reach;
    ``consolidate()`` marks its sweep the same way. No in-band signal would do: every
    field a caller can see is a field a caller can set (``metadata["provenance"]["source"]``
    included — ``service.remember(source=...)`` writes it verbatim). Every unvouched hint
    is re-homed (preserved, never dropped) under a key the structured-graph check does not
    recognize, with an honest source label.

    ``engraphis/service.py::_clean_metadata`` does the same at the service boundary; this
    is the defense-in-depth copy for callers that bypass it. Both are idempotent — a value
    re-homed at either layer has no hint keys left to relabel at the other.
    """
    vouched = trusted or frozenset()
    # The deferred review envelope is also internal. A direct caller must not be able
    # to pre-seed it and have a genuine LLM activity marker relabel that payload as
    # model-derived evidence. Internal producers vouch for it out of band just like
    # executable graph hints.
    caller_graph_keys = (*GRAPH_HINT_KEYS, _INTERNAL_DERIVED_GRAPH_KEY)
    untrusted = [
        key for key in caller_graph_keys
        if key in metadata and key not in vouched
    ]
    if not untrusted:
        return metadata
    out = {k: v for k, v in metadata.items() if k not in untrusted}
    existing = out.get("client_supplied_graph")
    hints = dict(existing) if isinstance(existing, dict) else {}
    hints.update({k: metadata[k] for k in untrusted})
    hints["source"] = "client_supplied"
    out["client_supplied_graph"] = hints
    return out


def _required_resolution_target(decision: Resolution) -> str:
    """Return a resolver target only when the selected operation requires one."""
    target_id = decision.target_id
    if not isinstance(target_id, str) or not target_id:
        raise RuntimeError(f"{decision.op.value} resolution requires a target memory id")
    return target_id


def _required_memory_workspace_id(record: MemoryRecord) -> str:
    """Defend the persisted workspace invariant at engine-to-engine boundaries."""
    workspace_id = record.workspace_id
    if not isinstance(workspace_id, str) or not workspace_id:
        raise RuntimeError(f"memory {record.id!r} has no workspace id")
    return workspace_id


def _governable_source(record: MemoryRecord, *, at: float) -> bool:
    """Accept current truth and quarantined evidence for governed derivations."""
    if record.expired_at is not None:
        return False
    if (
        metadata_is_quarantined(record.metadata)
        or bool((record.provenance or {}).get("quarantined"))
    ):
        return True
    return (
        (record.valid_from is None or record.valid_from <= at)
        and (record.valid_to is None or record.valid_to > at)
    )

def _writable_scope(scope: Scope, repo_id: Optional[str]) -> Scope:
    """The nearest scope ``remember()`` will actually accept for ``repo_id``.

    ``repo`` scope with no repo (a cross-repo ``merge``, or a record the sync apply path
    wrote without going through ``remember``'s validation) is not a storable combination
    — ``remember`` raises ``ValueError('repo scope requires repo_id')``. Rewriting it as
    ``workspace`` keeps the memory reachable instead of failing the whole operation; a
    ``repo``-scoped row with a NULL ``repo_id`` matches no repo read anyway.

    Deliberately narrow: no other scope is rewritten. ``session`` scope without a session
    still raises, because silently widening a session-private memory to repo/workspace
    visibility is a worse outcome than an explicit error — and with the write now
    happening *before* the source is retired, that error is no longer destructive.
    """
    return Scope.WORKSPACE if (Scope(scope) == Scope.REPO and not repo_id) else Scope(scope)


class _CodeSymbolMatcher:
    """Precompiled, repo-wide index behind ``MemoryEngine._link_memory_to_code``.

    The naive path walked *every* symbol for *every* memory and ran ``re.compile`` twice
    per symbol — O(symbols) regex compiles per repo-scoped write, and
    O(records × symbols) on every ``index_repo()``, even a one-file incremental change.
    This builds the equivalent state once per repo:

    * a chunked alternation over every candidate name/fqname, matched against the memory
      text in one C-level pass;
    * ``name → symbol positions`` and ``token → symbol positions`` inverted indexes, so
      only symbols that *can* link are scored.

    Two details keep the produced links byte-identical to the old per-symbol loop:

    1. The alternation is wrapped in a **zero-width lookahead**. A plain ``finditer``
       returns non-overlapping matches, so a long fqname would swallow a shorter name
       nested inside it (``engraphis.core.engine`` hides ``engine``) and silently
       downgrade that symbol's confidence from 0.9 to the 0.75 token fallback. The
       lookahead reports every offset instead, and every candidate length is then tested
       at that offset — so overlapping names all still match.
    2. Candidate positions are returned **in ``store.list_symbols`` order**, so the
       ``CODE_LINK_MAX_LINKS`` cutoff keeps the same first-N links.

    The 0.75 fallback (``tokenize(name) <= tokenize(text)``) is indexed on each symbol's
    *rarest* name token: a subset match implies that token is present, so the candidate
    set is complete while staying small.
    """

    __slots__ = ("symbols", "_by_len", "_lengths", "_patterns", "_by_name", "_by_token")

    def __init__(self, symbols: list) -> None:
        self.symbols = symbols
        by_len: dict[int, set] = {}
        by_name: dict[str, list] = {}
        by_token: dict[str, list] = {}
        token_freq: dict[str, int] = {}
        pending: list[tuple[int, set]] = []
        for position, symbol in enumerate(symbols):
            name = str(symbol.get("name") or "").strip()
            fqname = str(symbol.get("fqname") or "").strip()
            if len(name) < 3:
                continue          # exactly the per-symbol skip in _link_memory_to_code
            # fqname first, mirroring the elif-chain's precedence; both gates copy the
            # original's length checks on the *pre-lowercase* string.
            candidates = ([fqname] if (fqname and len(fqname) >= 3) else []) + [name]
            for raw in candidates:
                lowered = raw.lower()
                if not lowered:
                    continue
                by_len.setdefault(len(lowered), set()).add(lowered)
                by_name.setdefault(lowered, []).append(position)
            name_tokens = tokenize(name)
            if name_tokens:
                pending.append((position, name_tokens))
                for token in name_tokens:
                    token_freq[token] = token_freq.get(token, 0) + 1
        for position, name_tokens in pending:
            key = min(name_tokens, key=lambda token: (token_freq[token], token))
            by_token.setdefault(key, []).append(position)
        self._by_len = by_len
        self._lengths = sorted(by_len, reverse=True)
        self._by_name = by_name
        self._by_token = by_token
        ordered = sorted((s for group in by_len.values() for s in group),
                         key=lambda s: (-len(s), s))
        self._patterns = [
            re.compile(r"(?<!\w)(?=(?:"
                       + "|".join(re.escape(s) for s in ordered[i:i + CODE_ALTERNATION_CHUNK])
                       + r")(?!\w))")
            for i in range(0, len(ordered), CODE_ALTERNATION_CHUNK)
        ]

    def match(self, hay_lower: str, hay_tokens: set) -> tuple[set, list]:
        """``(matched lowercase names, candidate symbol positions)`` for one memory."""
        matched: set = set()
        offsets: set = set()
        for pattern in self._patterns:
            for hit in pattern.finditer(hay_lower):
                offsets.add(hit.start())
        size = len(hay_lower)
        for offset in offsets:
            for length in self._lengths:
                end = offset + length
                if end > size or (end < size and _WORD_CHAR_RE.match(hay_lower, end)):
                    continue
                candidate = hay_lower[offset:end]
                if candidate in self._by_len[length]:
                    matched.add(candidate)
        positions: set = set()
        for name in matched:
            positions.update(self._by_name.get(name, ()))
        for token in hay_tokens:
            positions.update(self._by_token.get(token, ()))
        return matched, sorted(positions)


class MemoryEngine:
    def __init__(
        self,
        store: Store,
        embedder,
        vector_index,
        reranker=None,
        *,
        auto_evolve: bool = True,
        extractor=None,
        graph_extractor=None,
        graph_feeder: Optional[Callable] = None,
        retention_supervisor=None,
        allow_automatic_critical_retention: bool = False,
        graph_traversal_policy: Optional[GraphTraversalPolicy] = None,
        query_planner: Optional[QueryPlanner] = None,
        code_indexer_factory: Optional[Callable] = None,
        code_language_detector: Optional[Callable] = None,
        code_source_iterator: Optional[Callable] = None,
        code_source_policy: Optional[Callable] = None,
        code_walk_limit_error=RuntimeError,
    ) -> None:
        self.store = store
        self.embedder = embedder
        self.embedding_space = embedding_space_fingerprint(embedder)
        self.index = vector_index
        self.reranker = reranker
        self.recall_engine = RecallEngine(
            store,
            embedder,
            vector_index,
            reranker,
            graph_traversal_policy=graph_traversal_policy,
            query_planner=query_planner,
        )
        # Memory evolution (A-MEM-style): writing a new note also updates
        # how its neighbors are connected, so the network improves bidirectionally.
        self.auto_evolve = auto_evolve
        # Optional implementations are injected by the outer package factory. Core owns
        # policy and orchestration, never concrete backend selection.
        self.extractor = extractor
        self.graph_extractor = graph_extractor
        self.graph_feeder = graph_feeder
        if graph_extractor is not None and graph_feeder is None:
            raise ValueError("graph_extractor requires an injected graph_feeder")
        self.retention_supervisor = retention_supervisor
        self._code_indexer_factory = code_indexer_factory
        self._code_language_detector = code_language_detector
        self._code_source_iterator = code_source_iterator
        self._code_source_policy = code_source_policy
        self._code_walk_limit_error = code_walk_limit_error
        # A remote classifier is advisory. It cannot silently grant the long-lived
        # "critical" class unless the host deliberately opts into that policy.
        self.allow_automatic_critical_retention = bool(
            allow_automatic_critical_retention
        )
        # Serializes the resolve→insert critical section of the write path (see
        # remember_with_resolution). RLock: ingest()/import paths may nest writes.
        self._write_lock = threading.RLock()
        # Best-effort derivations can fail repeatedly while a backend is unavailable.
        # Keep their payload-redacted warnings useful without letting one outage flood logs.
        self._failure_warning_lock = threading.Lock()
        self._failure_warning_last_emitted: dict[str, float] = {}
        self._failure_warning_suppressed: dict[str, int] = {}
        self._failure_warning_clock = time.monotonic
        # repo_id -> (symbol-set fingerprint, _CodeSymbolMatcher). Bounded; see
        # _code_matcher for the invalidation contract.
        self._code_matchers: dict = {}
        self._resource_lock = threading.Lock()
        self._owned_resources: tuple[Any, ...] = (store,)
        self._closed = False

    def _adopt_resources(self, resources: list[Any]) -> None:
        """Take ownership of factory-created collaborators after composition succeeds."""
        with self._resource_lock:
            if self._closed:
                raise RuntimeError("cannot transfer resources to a closed MemoryEngine")
            self._owned_resources = tuple(resources)

    def close(self) -> None:
        """Close every owned collaborator exactly once, with the Store last."""
        with self._resource_lock:
            if self._closed:
                return
            self._closed = True
            resources = self._owned_resources
            self._owned_resources = ()

        first_error: Optional[BaseException] = None
        seen: set[int] = set()
        for resource in reversed(resources):
            identity = id(resource)
            if identity in seen:
                continue
            seen.add(identity)
            close = getattr(resource, "close", None)
            if not callable(close):
                continue
            try:
                close()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    def _warn_redacted_failure(self, operation: str, exc: Exception) -> None:
        """Log bounded, payload-free warnings for non-fatal derived-work failures."""
        now = self._failure_warning_clock()
        with self._failure_warning_lock:
            last_emitted = self._failure_warning_last_emitted.get(operation)
            if (
                last_emitted is not None
                and now - last_emitted < BEST_EFFORT_FAILURE_WARNING_INTERVAL_SECONDS
            ):
                self._failure_warning_suppressed[operation] = (
                    self._failure_warning_suppressed.get(operation, 0) + 1
                )
                return
            suppressed = self._failure_warning_suppressed.pop(operation, 0)
            self._failure_warning_last_emitted[operation] = now
        if suppressed:
            logger.warning(
                "%s failed (%s); suppressed %d similar failures",
                operation,
                type(exc).__name__,
                suppressed,
            )
        else:
            logger.warning("%s failed (%s)", operation, type(exc).__name__)

    @classmethod
    def create(
        cls,
        db_path: str = ":memory:",
        *,
        embed_model: Optional[str] = None,
        embed_revision: Optional[str] = None,
        require_immutable_models: Optional[bool] = None,
        embed_dim: int = 384,
        vector_backend: str = "numpy",
        rerank_model: Optional[str] = None,
        rerank_revision: Optional[str] = None,
        extractor: str = "none",
        graph_extractor: str = "none",
        retention_supervisor: str = "none",
        allow_automatic_critical_retention: bool = False,
        auto_evolve: bool = True,
        connect=None,
        graph_traversal_policy: Optional[GraphTraversalPolicy] = None,
        query_planner: Optional[QueryPlanner] = None,
        read_only: bool = False,
    ) -> "MemoryEngine":
        """Compose the default engine through the package-level backend provider."""
        if _ENGINE_FACTORY is None:
            raise RuntimeError(
                "no MemoryEngine factory is configured; import the engraphis package "
                "or inject dependencies into MemoryEngine directly"
            )
        return _ENGINE_FACTORY(
            engine_cls=cls,
            db_path=db_path,
            embed_model=embed_model,
            embed_revision=embed_revision,
            require_immutable_models=require_immutable_models,
            embed_dim=embed_dim,
            vector_backend=vector_backend,
            rerank_model=rerank_model,
            rerank_revision=rerank_revision,
            extractor=extractor,
            graph_extractor=graph_extractor,
            retention_supervisor=retention_supervisor,
            allow_automatic_critical_retention=allow_automatic_critical_retention,
            auto_evolve=auto_evolve,
            connect=connect,
            graph_traversal_policy=graph_traversal_policy,
            query_planner=query_planner,
            read_only=read_only,
        )

    def _rebuild_versioned_embeddings(self) -> None:
        """Re-embed records when an opt-in backend changes its vector mapping.

        Backends advertise a durable ``embedding_identity`` and ``embedding_version``
        only when their stored vectors need this lifecycle. The marker is committed
        *after* every eligible record is indexed, so an interrupted rebuild safely
        repeats on the next startup rather than leaving a mixed mapping marked current.
        """
        identity = str(getattr(self.embedder, "embedding_identity", "") or "").strip()
        version = str(getattr(self.embedder, "embedding_version", "") or "").strip()
        fingerprint = self.embedding_space
        if not identity or not version or not fingerprint:
            logger.warning(
                "embedder has no durable identity/version; persistent vector recall "
                "will remain disabled"
            )
            return
        if self.store.embedding_space_ready(fingerprint):
            self._hydrate_separate_vector_index(fingerprint)
            return

        # Guard: never let a degraded/fallback embedder overwrite a semantic space
        # that was previously built by a real semantic model.  A transient missing
        # dependency, model download failure, or provider outage must not
        # permanently downgrade semantic recall to lexical hashing.
        #
        # Legacy markers (e.g. ``"legacy-unverified"``) are excluded: they indicate
        # vectors from before proper model tracking, so rebuilding them with the
        # current deterministic version is the intended upgrade path — there is no
        # semantic information to lose.
        active = self.store.active_embedding_space()
        if (
            active
            and active != fingerprint
            and active.startswith("emb:v1:")
        ):
            caps = embedder_capabilities(self.embedder)
            if caps.get("degraded_mode"):
                logger.warning(
                    "skipping embedding rebuild: active space %s was built by a "
                    "semantic embedder but the current embedder is degraded (%s); "
                    "vector recall remains available under the existing space",
                    active, caps.get("degraded_reason", "unknown"),
                )
                return

        self.store.begin_embedding_rebuild(fingerprint)
        rebuilt = 0
        removed = 0
        after_id = ""
        try:
            while True:
                records = self.store.list_memories_page(
                    after_id=after_id, limit=EMBEDDING_REBUILD_BATCH, include_invalid=True,
                )
                if not records:
                    break
                after_id = records[-1].id
                eligible = [
                    record for record in records
                    if inspection_eligible(record.provenance, record.metadata)
                ]
                excluded_ids = [
                    record.id for record in records
                    if not inspection_eligible(record.provenance, record.metadata)
                ]
                vectors = None
                ids = []
                metadata = []
                if eligible:
                    texts = [
                        f"{record.title}\n{record.content}" if record.title else record.content
                        for record in eligible
                    ]
                    vectors = np.asarray(self.embedder.embed(texts), dtype=np.float32)
                    if vectors.ndim != 2 or vectors.shape != (
                            len(eligible), int(self.embedder.dim)):
                        raise ValueError(
                            "embedder returned an invalid batch shape during rebuild"
                        )
                    if not np.all(np.isfinite(vectors)):
                        raise ValueError(
                            "embedder returned non-finite values during rebuild"
                        )
                    ids = [record.id for record in eligible]
                    metadata = [{"model": fingerprint} for _ in eligible]

                # Embed outside the database lock, then atomically verify that this
                # process still owns the target marker before publishing one batch.
                # A competing process can supersede the marker, but the loser cannot
                # write vectors or clear the winner's rebuild gate.
                self.store.conn.execute("BEGIN IMMEDIATE")
                if self.store.embedding_rebuild_target() != fingerprint:
                    self.store.conn.rollback()
                    if self.store.embedding_space_ready(fingerprint):
                        return
                    raise RuntimeError(
                        "embedding rebuild was superseded by another process"
                    )
                if excluded_ids:
                    if vector_index_requires_sync(self.index, self.store):
                        self.index.delete(excluded_ids, commit=False)
                    marks = ",".join("?" for _ in excluded_ids)
                    self.store.conn.execute(
                        f"DELETE FROM mem_vectors WHERE id IN ({marks})", excluded_ids
                    )
                # Keep the portable mirror current even when the active index is
                # sqlite-vec. A later NumPy fallback must see the same vector space.
                if vectors is not None:
                    for record, vector in zip(eligible, vectors):
                        self.store.put_vector(record.id, vector, model=fingerprint)
                    if vector_index_requires_sync(self.index, self.store):
                        _safe_upsert(self.index, ids, vectors, metadata, commit=False)
                self.store.conn.commit()
                removed += len(excluded_ids)
                rebuilt += len(eligible)

            self.store.conn.execute("BEGIN IMMEDIATE")
            if self.store.embedding_rebuild_target() != fingerprint:
                self.store.conn.rollback()
                if self.store.embedding_space_ready(fingerprint):
                    return
                raise RuntimeError(
                    "embedding rebuild was superseded by another process"
                )
            stale_row = self.store.conn.execute(
                "SELECT COUNT(*) AS n FROM mem_vectors "
                "WHERE COALESCE(model, '') <> ?",
                (fingerprint,),
            ).fetchone()
            stale = int(stale_row["n"]) if stale_row is not None else 0
            if stale:
                self.store.conn.rollback()
                raise RuntimeError(
                    f"embedding rebuild left {stale} stale vector rows"
                )
            self.store.finish_embedding_rebuild(
                fingerprint, identity=identity, version=version
            )
            self._mark_separate_vector_index_rebuild_complete()
        except BaseException as exc:
            if self.store.conn.transaction_owned_by_current_thread():
                self.store.conn.rollback()
            logger.error(
                "embedding rebuild failed; vector recall remains disabled (%s)",
                type(exc).__name__,
            )
            raise

        if rebuilt:
            self.store.audit(
                "system", "embedding_rebuild", identity,
                f"version={version}; fingerprint={fingerprint}; "
                f"records={rebuilt}; removed={removed}",
            )

    def _hydrate_separate_vector_index(self, fingerprint: str) -> None:
        """Repair a separate ANN backend from the canonical Store mirror.

        Historical/superseded vectors intentionally retained in ``mem_vectors``
        for ``valid_at``/``as_of`` recall must also be hydrated into separate
        indexes like sqlite-vec.  Using ``include_invalid=False`` would omit
        closed-but-inspection-eligible memories, making historical semantic
        recall through the separate index incomplete until a full rebuild.
        """
        if not vector_index_requires_sync(self.index, self.store):
            return
        ids: list[str] = []
        vectors: list[np.ndarray] = []
        for memory_id, vector in self.store.iter_vectors(
                include_invalid=True, dim=int(self.embedder.dim)):
            ids.append(memory_id)
            vectors.append(vector)
            if len(ids) < EMBEDDING_REBUILD_BATCH:
                continue
            _safe_upsert(
                self.index,
                ids, np.asarray(vectors, dtype=np.float32),
                [{"model": fingerprint} for _ in ids],
                commit=True,
            )
            ids, vectors = [], []
        if ids:
            _safe_upsert(
                self.index,
                ids, np.asarray(vectors, dtype=np.float32),
                [{"model": fingerprint} for _ in ids],
                commit=True,
            )
        self._mark_separate_vector_index_rebuild_complete()

    def _mark_separate_vector_index_rebuild_complete(self) -> None:
        """Publish an optional ANN backend's readiness after full hydration."""
        if getattr(self.index, "requires_rebuild", False) is not True:
            return
        mark_complete = getattr(self.index, "mark_rebuild_complete", None)
        if callable(mark_complete):
            mark_complete()

    # ── write ─────────────────────────────────────────────────────────────────
    def remember(self, content: str, *, workspace_id: str, repo_id: Optional[str] = None,
                 session_id: Optional[str] = None, mtype: MemoryType = MemoryType.SEMANTIC,
                 scope: Optional[Scope] = None, title: str = "", importance: float = 0.0,
                 confidence: Optional[float] = None, keywords: Optional[list] = None,
                 metadata: Optional[dict] = None,
                 valid_from: Optional[float] = None, resolve_conflicts: bool = True,
                 candidate_k: int = 5, subject_key: str = "", claim_kind: str = "",
                 _trusted_graph_keys: Optional[frozenset] = None,
                 _transactional_finalizer: Optional[Callable[[str], None]] = None) -> str:
        """Store one memory. Returns the resulting record id: a new id for ADD/
        INVALIDATE/quarantine, or the existing memory's id if this was resolved as a
        NOOP (near-duplicate). See ``remember_with_resolution`` for decision detail.
        """
        return self.remember_with_resolution(
            content, workspace_id=workspace_id, repo_id=repo_id, session_id=session_id,
            mtype=mtype, scope=scope, title=title, importance=importance,
            confidence=confidence, keywords=keywords,
            metadata=metadata, valid_from=valid_from, resolve_conflicts=resolve_conflicts,
            candidate_k=candidate_k, subject_key=subject_key, claim_kind=claim_kind,
            _trusted_graph_keys=_trusted_graph_keys,
            _transactional_finalizer=_transactional_finalizer,
        )["id"]

    def remember_with_resolution(self, content: str, *, workspace_id: str,
                 repo_id: Optional[str] = None, session_id: Optional[str] = None,
                 mtype: MemoryType = MemoryType.SEMANTIC, scope: Optional[Scope] = None,
                 title: str = "", importance: float = 0.0,
                 confidence: Optional[float] = None, keywords: Optional[list] = None,
                 metadata: Optional[dict] = None, valid_from: Optional[float] = None,
                 resolve_conflicts: bool = True, candidate_k: int = 5,
                 subject_key: str = "", claim_kind: str = "",
                 _trusted_graph_keys: Optional[frozenset] = None,
                 _approval_override: bool = False,
                 _transactional_finalizer: Optional[Callable[[str], None]] = None) -> dict:
        """Store one memory with deterministic conflict resolution.

        Returns ``{"id", "op", ...}`` where ``op`` is one of:

        * ``"add"``        — genuinely new; inserted.
        * ``"noop"``        — a near-duplicate of an existing memory; that memory was
          reinforced instead of inserting a copy. ``id`` is the *existing* memory's id.
        * ``"invalidate"``  — same subject as an existing memory but new content; the old
          one's validity was closed (never deleted) and this was inserted. ``superseded``
          lists the closed id(s).
        * ``"relate"``      — evidence shows a nearby claim but not a safe contradiction;
          both remain live and a semantic relation is persisted.
        * ``"quarantined"`` — an explicitly untrusted payload matched the deterministic
          poisoning policy; retained only for governed historical inspection.
        """
        # Reject credentials before embedding, conflict resolution, graph extraction, or
        # any SQLite mirror sees them. Store.add_memory repeats this for direct callers.
        reject_secrets((("title", title), ("content", content), ("keywords", keywords),
                        ("metadata", metadata), ("subject_key", subject_key),
                        ("claim_kind", claim_kind)))
        if valid_from is not None:
            if isinstance(valid_from, bool):
                raise ValueError("valid_from must be a finite timestamp")
            try:
                valid_from = float(valid_from)
            except (TypeError, ValueError) as exc:
                raise ValueError("valid_from must be a finite timestamp") from exc
            if not math.isfinite(valid_from):
                raise ValueError("valid_from must be a finite timestamp")
        subject_key = str(subject_key or "").strip()
        claim_kind = str(claim_kind or "").strip()
        scope_was_omitted = scope is None
        scope = (
            Scope.REPO if (repo_id or session_id) else Scope.WORKSPACE
        ) if scope is None else Scope(scope)
        if scope == Scope.USER:
            raise ValueError(_USER_SCOPE_WRITE_ERROR)
        if session_id:
            session = self.store.get_session(session_id)
            if session is None:
                raise ValueError(f"no session with id '{session_id}'")
            if session["workspace_id"] != workspace_id or (
                    repo_id is not None and session.get("repo_id") != repo_id):
                raise ValueError("session_id does not belong to that workspace/repo")
            if scope in (Scope.SESSION, Scope.REPO) and repo_id is None:
                repo_id = session.get("repo_id")
        if scope == Scope.SESSION and not session_id:
            raise ValueError("session scope requires session_id")
        if scope == Scope.REPO and not repo_id:
            if scope_was_omitted:
                scope = Scope.WORKSPACE
            else:
                raise ValueError("repo scope requires repo_id")
        if scope in (Scope.WORKSPACE, Scope.USER) and repo_id:
            raise ValueError(f"{scope.value} scope requires repo_id to be omitted")
        # Every new record carries an explicit trust assertion.  The direct engine is
        # a local, programmatic capability; external entry points set their own
        # canonical ``trusted: false`` provenance before reaching this layer.  Keeping
        # the default here preserves the core's offline API while making recall fail
        # closed for genuinely legacy/unlabelled records.
        write_metadata = dict(metadata or {})
        provenance = write_metadata.get("provenance")
        provenance = dict(provenance) if isinstance(provenance, dict) else {}
        if "trusted" not in provenance:
            provenance["trusted"] = True
            provenance.setdefault("trust_origin", "local_engine")
        provenance.setdefault("source", "local_engine")
        # The direct engine is an in-process capability.  Public transports set an
        # explicit pending state before reaching it; a direct trusted write remains
        # compatible and is the only implicit local approval boundary.
        if provenance.get("trusted") is True:
            provenance.setdefault("review_state", REVIEW_APPROVED)
        else:
            provenance.setdefault("review_state", REVIEW_PENDING)
        write_metadata["provenance"] = provenance
        poisoning = (
            PoisoningDecision(False)
            if _approval_override else
            assess_untrusted_payload(content, title=title, metadata=write_metadata)
        )
        # Resolution changes existing validity and links. It therefore runs only for
        # content that already satisfies the full prompt/derived-state predicate;
        # pending evidence is stored passively and cannot reinforce or supersede it.
        trusted_write = prompt_eligible(provenance, write_metadata)
        text = f"{title}\n{content}" if title else content
        persistent_store = (
            self.store.path != ":memory:"
            and not self.store.path.startswith("file::memory:")
        )
        if not poisoning.quarantined and persistent_store:
            if not self.embedding_space:
                raise RuntimeError(
                    "persistent writes require an embedder with a durable "
                    "embedding_identity and embedding_version"
                )
            if not self.store.embedding_space_ready(self.embedding_space):
                raise RuntimeError(
                    "the configured embedding space is not active; restart through "
                    "MemoryEngine.create() to complete the guarded rebuild"
                )
        if self.embedding_space:
            write_metadata["embed_model"] = self.embedding_space
        # Embedding is the expensive, thread-safe part — compute it BEFORE taking the
        # write lock so concurrent writers only serialize the fast resolve+insert step.
        # Quarantine happens before embedding: payloads retained only for inspection
        # must never consume a vector slot or become semantic retrieval candidates.
        vec = None if poisoning.quarantined else self.embedder.embed([text])[0]

        # One writer at a time from neighbor-lookup through insert/invalidate: without
        # this, two concurrent near-duplicate remembers can BOTH observe "no neighbor"
        # and both resolve ADD — duplicating instead of NOOP/INVALIDATE — because the
        # store's per-statement serialization cannot span this read-decide-write
        # sequence. Same single-process posture as the rest of the engine (the store is
        # one shared connection); multi-process writers are out of scope by design.
        with self._write_lock:
            caller_owned_transaction = (
                self.store.conn.transaction_owned_by_current_thread()
            )
            if (
                caller_owned_transaction
                and vec is not None
                and vector_index_requires_sync(self.index, self.store)
                and not vector_index_shares_store_transaction(self.index, self.store)
            ):
                # A separate backend has no hook into a caller's later commit/rollback.
                # Publishing now can orphan a vector; waiting would silently leave a
                # committed memory unindexed. Fail before any Store mutation and leave
                # ownership and rollback policy entirely with the caller.
                raise RuntimeError(
                    "caller-owned transactions cannot write through a separate vector "
                    "index; commit or roll back before remembering"
                )
            owns_session_transaction = False
            owns_lifecycle_transaction = False
            try:
                if (_transactional_finalizer is not None
                        and not self.store.conn.transaction_owned_by_current_thread()):
                    self.store.conn.execute("BEGIN IMMEDIATE")
                    owns_lifecycle_transaction = True
                if session_id:
                    owns_session_transaction = self.store.begin_session_write(
                        session_id, workspace_id=workspace_id, repo_id=repo_id
                    )
                # A separate index cannot participate in the Store transaction. Delay
                # publication until every remaining engine mutation has succeeded; an
                # engine-owned session/lifecycle transaction is committed first.
                # Caller-owned transactions with a separate backend were rejected above;
                # Store-sharing indexes need no duplicate publication.
                defer_external_index = bool(
                    self.store.conn.transaction_owned_by_current_thread()
                    and vector_index_requires_sync(self.index, self.store)
                    and not vector_index_shares_store_transaction(
                        self.index, self.store,
                    )
                )
                if _transactional_finalizer is None:
                    result = self._resolve_and_store(
                        content, text=text, vec=vec, workspace_id=workspace_id,
                        repo_id=repo_id, session_id=session_id, mtype=mtype, scope=scope,
                        title=title, importance=importance, confidence=confidence,
                        keywords=keywords, metadata=write_metadata,
                        valid_from=valid_from, resolve_conflicts=resolve_conflicts,
                        candidate_k=candidate_k, subject_key=subject_key,
                        claim_kind=claim_kind, trusted_graph_keys=_trusted_graph_keys,
                        poisoning=poisoning, trusted_write=trusted_write,
                        defer_external_index=defer_external_index,
                    )
                    if (
                        owns_session_transaction
                        and self.store.conn.transaction_owned_by_current_thread()
                    ):
                        self.store.conn.commit()
                    if defer_external_index:
                        self._publish_result_vector(result, vec)
                    return result
                with self.store.conn.defer_commits():
                    result = self._resolve_and_store(
                        content, text=text, vec=vec, workspace_id=workspace_id,
                        repo_id=repo_id, session_id=session_id, mtype=mtype, scope=scope,
                        title=title, importance=importance, confidence=confidence,
                        keywords=keywords, metadata=write_metadata,
                        valid_from=valid_from, resolve_conflicts=resolve_conflicts,
                        candidate_k=candidate_k, subject_key=subject_key,
                        claim_kind=claim_kind, trusted_graph_keys=_trusted_graph_keys,
                        poisoning=poisoning, trusted_write=trusted_write,
                        transactional_finalizer=_transactional_finalizer,
                        defer_external_index=defer_external_index,
                    )
                if owns_lifecycle_transaction:
                    self.store.conn.commit()
                if defer_external_index:
                    self._publish_result_vector(result, vec)
                return result
            except BaseException:
                if ((owns_session_transaction or owns_lifecycle_transaction)
                        and self.store.conn.transaction_owned_by_current_thread()):
                    self.store.conn.rollback()
                raise

    def _publish_result_vector(self, result: dict, vec: Optional[np.ndarray]) -> None:
        """Publish one newly committed Store vector to a separate injected index."""
        if vec is None or result.get("op") not in {"add", "invalidate", "relate"}:
            return
        memory_id = result.get("id")
        if not isinstance(memory_id, str) or not memory_id:
            raise RuntimeError("stored memory result is missing its id")
        self._upsert_external_vector(memory_id, vec)

    def _upsert_external_vector(self, memory_id: str, vec: np.ndarray) -> None:
        """Best-effort synchronization for indexes outside the canonical Store."""
        if not vector_index_requires_sync(self.index, self.store):
            return
        try:
            self.index.upsert(
                [memory_id], vec.reshape(1, -1),
                [{"model": self.embedding_space}],
            )
        except Exception as exc:  # noqa: BLE001 — a failed index write must not lose the memory
            # The canonical Store vector is authoritative. Keep the write, but make the
            # derived-index gap content-free and visible to operators. Never commit a
            # caller-owned Store transaction merely to persist this diagnostic.
            logger.warning("vector-index upsert failed for %s (%s)",
                           memory_id, type(exc).__name__)
            try:
                self.store.audit(
                    "engine", "index_upsert_failed", memory_id,
                    "failure_type=%s" % type(exc).__name__,
                    commit=not self.store.conn.transaction_owned_by_current_thread(),
                )
            except Exception as audit_exc:  # noqa: BLE001
                self._warn_redacted_failure("vector-index failure audit", audit_exc)

    def _resolve_and_store(self, content: str, *, text: str, vec: Optional[np.ndarray],
                           workspace_id: str, repo_id: Optional[str],
                           session_id: Optional[str], mtype: MemoryType, scope: Scope,
                           title: str, importance: float, confidence: Optional[float],
                           keywords: Optional[list],
                           metadata: Optional[dict], valid_from: Optional[float],
                           resolve_conflicts: bool, candidate_k: int,
                           subject_key: str, claim_kind: str,
                           trusted_graph_keys: Optional[frozenset] = None,
                           poisoning: Optional[PoisoningDecision] = None,
                           trusted_write: bool = True,
                           transactional_finalizer: Optional[Callable[[str], None]] = None,
                           defer_external_index: bool = False) -> dict:
        """The resolve→insert body of ``remember_with_resolution``. The caller holds
        ``self._write_lock`` for the whole call (atomicity of the resolve decision).

        ``trusted_graph_keys`` names the ``GRAPH_HINT_KEYS`` this write's ``metadata``
        genuinely inherited from an ``Extractor``; everything else is treated as
        caller-supplied — see ``_rehome_untrusted_graph_hints``."""
        poisoning = poisoning or PoisoningDecision(False)
        decision, neighbors, conflicted_with = None, [], None
        # Untrusted records are retained as passive inspection evidence.  They may
        # not deduplicate into, invalidate, relate to, reinforce, or otherwise
        # mutate higher-trust memory; that is a trust lattice, not a detector score.
        if resolve_conflicts and trusted_write and not poisoning.quarantined:
            if vec is None:
                raise ValueError("non-quarantined writes require an embedding")
            decision, neighbors, conflicted_with = self._resolve_against_neighbors(
                text, vec, workspace_id=workspace_id, repo_id=repo_id,
                session_id=session_id, scope=scope, mtype=mtype,
                candidate_k=candidate_k, subject_key=subject_key,
                claim_kind=claim_kind, valid_at=valid_from, content=content,
            )
        if (resolve_conflicts and trusted_write and not poisoning.quarantined
                and subject_key and valid_from is not None):
            # A durable claim has a temporal identity in addition to its text. A
            # scheduled successor can be a better prose match than the version visible
            # at this write's effective time, but it is not the version being replaced.
            # Select that visible predecessor directly so a backfill is spliced into the
            # recorded chain rather than rejected for predating a future match.
            claim_history = self.store.list_claim_history(
                workspace_id=workspace_id, repo_id=repo_id,
                session_id=session_id if scope == Scope.SESSION else None,
                scope=scope, mtype=mtype, subject_key=subject_key,
                claim_kind=claim_kind,
            )
            predecessors = [
                record for record in claim_history
                if record.valid_from is not None and record.valid_from <= valid_from
                and (record.valid_to is None or valid_from < record.valid_to)
                and prompt_eligible(record.provenance, record.metadata)
            ]
            if predecessors:
                predecessor = max(
                    predecessors,
                    key=lambda record: (record.valid_from or float("-inf"), record.id),
                )
                if " ".join(content.split()).casefold() == (
                    " ".join(predecessor.content.split()).casefold()
                ):
                    decision = Resolution(
                        ResolutionOp.NOOP, target_id=predecessor.id,
                        reason=f"exact duplicate of keyed claim {predecessor.id}",
                    )
                else:
                    decision = Resolution(
                        ResolutionOp.INVALIDATE, target_id=predecessor.id,
                        reason=(f"supersedes temporal predecessor {predecessor.id} "
                                f"for keyed claim"),
                    )
        if (decision is not None
                and decision.op == ResolutionOp.INVALIDATE
                and valid_from is not None):
            previous = self.store.get_memory(_required_resolution_target(decision))
            if (previous is not None
                    and previous.valid_from is not None
                    and valid_from < previous.valid_from):
                raise ValueError(
                    "valid_from cannot predate the memory it supersedes; "
                    "record the historical interval separately or correct the older memory"
                )
        if decision is not None and decision.op == ResolutionOp.INVALIDATE:
            # A keyed/temporal supersession is the resolution: the detector may have
            # flagged the superseded predecessor earlier, but a closed record is no
            # longer a live conflict — drop the repair so no ``conflicts_with`` link,
            # metadata marker, or confidence discount is written for this write.
            conflicted_with = None

        if decision is not None and decision.op == ResolutionOp.NOOP:
            target_id = _required_resolution_target(decision)
            self.store.reinforce(target_id, boost=scoring.INTERACTION_BOOST["create"])
            self.store.audit("resolver", "noop", target_id, decision.reason)
            if transactional_finalizer is not None:
                transactional_finalizer(target_id)
            return {"id": target_id, "op": "noop", "reason": decision.reason}

        # Before anything reads it: demote graph hints this write cannot prove came from
        # an Extractor, so the "structured_extractor" feed below can only ever see
        # genuine extractor output (defense in depth for direct-engine callers that never
        # pass through service.py::_clean_metadata). Internal producers must vouch for
        # individual keys explicitly; there is no ambient thread-wide trust elevation.
        meta = _rehome_untrusted_graph_hints(dict(metadata or {}), trusted_graph_keys)
        if poisoning.quarantined:
            # Policy values are written after caller-owned metadata. This prevents a
            # payload from forging a trusted/quarantine-clear provenance flag.
            meta = apply_quarantine_metadata(meta, poisoning)
        if decision is not None and decision.op == ResolutionOp.INVALIDATE:
            # Persist the supersession pointer on the new record so the chain is
            # queryable later (why/timeline/inspector), not only in the audit log.
            meta["supersedes"] = [_required_resolution_target(decision)]
        if conflicted_with:
            # Surface the deterministic conflict repair on the new record so
            # downstream (recall/why/inspector) can explain the lowered confidence
            # without a separate graph walk. The neighbor side already carries the
            # durable ``conflicts_with`` link; this is a minimal, queryable mirror.
            meta["conflict_with"] = [conflicted_with]
        if poisoning.quarantined:
            # Retained only for governance inspection: an untrusted payload must not
            # elevate itself through caller-supplied retention supervision.
            importance, stability, retention_signal = 0.0, MIN_STABILITY_DAYS, {}
        else:
            importance, stability, retention_signal = self._retention_signal(
                content, title=title, mtype=mtype, metadata=meta, importance=importance,
            )
        if retention_signal:
            meta["retention_supervision"] = retention_signal

        quarantine_at = valid_from if valid_from is not None else now_ts()
        # Confidence defaults to 1.0 (no scoring change for ordinary writes); a
        # caller-supplied value wins, and the structured-extraction metadata hint is
        # honored when present so persisted verdicts actually reach scoring.
        raw_confidence: object = confidence if confidence is not None else meta.get("confidence", 1.0)
        try:
            confidence = (
                float(raw_confidence)
                if isinstance(raw_confidence, (str, int, float))
                else 1.0
            )
        except (TypeError, ValueError, OverflowError):
            confidence = 1.0
        confidence = max(0.0, min(1.0, confidence)) if math.isfinite(confidence) else 1.0
        if conflicted_with:
            # The new fact directly contradicts a live memory without a safe
            # supersession; neither side gets to claim full confidence.
            confidence = round(confidence * CONFLICT_CONFIDENCE_FACTOR, 4)
        rec = MemoryRecord(
            id="", content=content, mtype=mtype, scope=scope, workspace_id=workspace_id,
            repo_id=repo_id, session_id=session_id, title=title, importance=importance,
            stability=stability, confidence=confidence,
            subject_key=subject_key, claim_kind=claim_kind,
            keywords=keywords or [], metadata=meta,
            # A zero-length validity interval retains the record/audit trail while the
            # existing temporal filters keep it out of every normal recall arm.
            valid_from=quarantine_at if poisoning.quarantined else valid_from,
            valid_to=quarantine_at if poisoning.quarantined else None,
            valid_to_recorded_at=now_ts() if poisoning.quarantined else None,
            # Lift provenance into its dedicated field/column so recall/why/timeline
            # surface it (copied, not popped: consolidate.py still reads
            # metadata["provenance"]).
            provenance=dict(meta.get("provenance") or {}),
            embedding=None if poisoning.quarantined else vec,
        )
        invalidating = decision is not None and decision.op == ResolutionOp.INVALIDATE
        try:
            # A replacement and the predecessor interval it closes are one authoritative
            # state transition. Portable vector/FTS mirrors participate in the same
            # transaction; external vector-index and graph enrichments run only after it commits.
            mid = self.store.add_memory(rec, commit=not invalidating)
            if invalidating:
                if decision is None:
                    raise RuntimeError("invalidating write is missing its resolution")
                target_id = _required_resolution_target(decision)
                resolution_reason = decision.reason
                predecessor = self.store.get_memory(target_id)
                predecessor_end = predecessor.valid_to if predecessor is not None else None
                if (predecessor_end is not None and rec.valid_from is not None
                        and rec.valid_from < predecessor_end):
                    # Splice a backfilled interval between its recorded predecessor and
                    # successor without widening either historical interval.
                    self.store.conn.execute(
                        "UPDATE memories SET valid_to=?, valid_to_recorded_at=? WHERE id=?",
                        (rec.valid_from, now_ts(), target_id),
                    )
                    self.store.invalidate_edges_for_memory(
                        target_id, at=rec.valid_from, commit=False
                    )
                    self.store.audit(
                        "system", "invalidate", target_id, resolution_reason,
                        commit=False,
                    )
                    self.store.close_validity(
                        mid, at=predecessor_end,
                        reason="bounded by the recorded successor interval",
                        commit=False,
                    )
                else:
                    self.store.close_validity(
                        target_id, at=rec.valid_from,
                        reason=resolution_reason, commit=False,
                    )
                self.store.audit(
                    "resolver", "invalidate", target_id, resolution_reason,
                    commit=False,
                )
            if transactional_finalizer is not None:
                transactional_finalizer(mid)
            if invalidating:
                self.store.conn.commit()
        except BaseException:
            if invalidating and self.store.conn.transaction_owned_by_current_thread():
                self.store.conn.rollback()
            raise
        if poisoning.quarantined:
            # Deliberately content-free: a reviewer can inspect the retained record,
            # while audit exports never reflect prompt-injection text into another UI.
            self.store.audit(
                "poisoning_policy", "quarantine", mid,
                "policy=%s; reasons=%s" % (
                    poisoning.policy, ",".join(poisoning.reasons),
                ),
            )
            return {
                "id": mid,
                "op": "quarantined",
                "quarantined": True,
                "policy": poisoning.policy,
                "reasons": list(poisoning.reasons),
            }
        if retention_signal:
            self.store.audit(
                retention_signal.get("source", "retention"),
                "retention_supervised",
                mid,
                f"{retention_signal.get('label', 'normal')}: "
                f"{retention_signal.get('reason', '')}"[:1000],
            )
        if vec is None:
            raise RuntimeError("non-quarantined memory was stored without an embedding")
        if not defer_external_index:
            self._upsert_external_vector(mid, vec)
        if trusted_write and repo_id and scope != Scope.SESSION:
            self._link_memory_to_code(mid, content=f"{title}\n{content}", repo_id=repo_id)

        # Structured fact metadata is already validated before storage, so an injected
        # graph feeder may ingest it even when the configured text extractor is disabled;
        # then the configured extractor runs too (idempotent via Store de-duplication).
        # ``meta`` was demoted above, so any graph hint still here was vouched for by
        # ingest(), not merely asserted by whoever built the metadata dictionary.
        if (
            trusted_write
            and scope != Scope.SESSION
            and self.graph_feeder is not None
            and self._has_structured_graph_metadata(meta)
        ):
            try:
                self.graph_feeder(
                    self.store,
                    content,
                    workspace_id=workspace_id,
                    repo_id=repo_id,
                    title=title,
                    extractor=None,
                    structured_metadata=meta,
                    provenance={
                        "source": "structured_extractor",
                        "memory_id": mid,
                    },
                    valid_from=rec.valid_from,
                    ingested_at=rec.ingested_at,
                )
            except Exception as exc:
                self._warn_redacted_failure("structured graph enrichment", exc)
        if (
            trusted_write
            and scope != Scope.SESSION
            and self.graph_extractor is not None
            and self.graph_feeder is not None
        ):
            try:
                self.graph_feeder(
                    self.store,
                    content,
                    workspace_id=workspace_id,
                    repo_id=repo_id,
                    title=title,
                    extractor=self.graph_extractor,
                    structured_metadata=None,
                    provenance={
                        "source": "graph_extractor",
                        "memory_id": mid,
                    },
                    valid_from=rec.valid_from,
                    ingested_at=rec.ingested_at,
                )
            except Exception as exc:
                self._warn_redacted_failure("graph extraction", exc)
        if trusted_write and scope != Scope.SESSION:
            self._link_memory_entities(
                mid, f"{title}\n{content}", workspace_id=workspace_id, repo_id=repo_id,
                valid_from=rec.valid_from,
            )

        if decision is not None and decision.op == ResolutionOp.INVALIDATE:
            # Keep the superseded vector. Every vector backend applies the same temporal
            # SearchFilter as lexical/graph retrieval, so it is hidden from current recall
            # but remains available for historical ``as_of`` queries. Deleting it made
            # time travel silently lose the semantic arm.
            linked = self._evolve(mid, neighbors, exclude={decision.target_id}) if trusted_write else []
            out = {"id": mid, "op": "invalidate", "superseded": [decision.target_id],
                   "reason": decision.reason}
            if linked:
                out["linked"] = linked
            return out

        linked = self._evolve(mid, neighbors) if trusted_write else []
        if conflicted_with:
            # Deterministic conflict repair: persist the ``conflicts_with`` relation
            # (with the real new-memory id), the audit row, and a bounded confidence
            # discount on BOTH sides. Non-fatal: a storage hiccup here must not fail
            # the write — the conflict metadata on the new record already surfaced it.
            try:
                self.store.add_link(
                    mid, conflicted_with, CONFLICT_RELATION,
                    reason=(
                        "detector=contradiction; deterministic contradiction "
                        "(no safe supersession)"
                    ),
                    valid_from=valid_from,
                )
                self.store.audit(
                    "resolver", "conflict_detected", conflicted_with,
                    f"new_memory={mid}; deterministic contradiction (no safe supersession)",
                )
                self.store.advance_memory_modified_hlc(
                    conflicted_with, commit=False,
                )
                self.store.conn.execute(
                    "UPDATE memories SET confidence=MIN(confidence, ?) WHERE id=?",
                    (round(CONFLICT_CONFIDENCE_FACTOR, 4), conflicted_with),
                )
                self.store.conn.commit()
            except Exception as exc:  # noqa: BLE001 — best-effort repair, never fail the write
                if self.store.conn.transaction_owned_by_current_thread():
                    self.store.conn.rollback()
                self._warn_redacted_failure("conflict repair", exc)
        out: dict[str, object]
        if decision is not None and decision.op == ResolutionOp.RELATE:
            related_to = decision.target_id
            if related_to and not self.store.has_link(mid, related_to):
                self.store.add_link(mid, related_to, "related", reason=decision.reason)
            out = {
                "id": mid, "op": "relate", "related_to": related_to,
                "reason": decision.reason,
            }
        else:
            out = {"id": mid, "op": "add", "reason": decision.reason if decision else ""}
        if conflicted_with:
            out["conflict_with"] = conflicted_with
        if linked:
            out["linked"] = linked
        return out

    def _retention_signal(self, content: str, *, title: str, mtype: MemoryType,
                          metadata: dict, importance: float) -> tuple[float, float, dict]:
        """Apply an explicit host hint or the optional supervisor.

        Supervision is advisory and bounded: failures preserve today's default
        ``importance``/``stability`` and ``retain=False`` becomes an ephemeral candidate,
        never a dropped write.
        """
        raw = metadata.get("retention_supervision")
        decision = None
        source = "host"
        if isinstance(raw, dict) and raw.get("label"):
            decision = RetentionDecision(
                label=str(raw.get("label") or "normal"),
                retain=bool(raw.get("retain", True)),
                importance=raw.get("importance"),
                stability=raw.get("stability"),
                reason=str(raw.get("reason") or ""),
            )
        elif self.retention_supervisor is not None:
            source = "llm"
            try:
                decision = self.retention_supervisor.decide(
                    content, title=title, mtype=mtype, metadata=metadata,
                )
            except Exception:
                decision = None
        if decision is None:
            # Same clamp as the decision path below, so direct engine callers get
            # identical importance validation whether or not supervision applies.
            return _bounded_finite(importance, default=0.0, minimum=0.0, maximum=1.0), 1.0, {}

        label = str(decision.label or "normal").lower()
        if label not in {"ephemeral", "normal", "critical"}:
            label = "normal"
        demoted_automatic_critical = False
        if source == "llm" and label == "critical" and not self.allow_automatic_critical_retention:
            # The supervisor sees text it does not authoritatively vouch for. Its
            # "critical" label therefore defaults to normal retention; an explicit
            # user/host retention_class remains a separate, bounded path.
            label = "normal"
            demoted_automatic_critical = True
        if not decision.retain:
            label = "ephemeral"
        preset_stability = {"ephemeral": 0.25, "normal": 1.0, "critical": 8.0}[label]
        preset_importance = {"ephemeral": 0.1, "normal": 0.5, "critical": 0.9}[label]
        if decision.importance is not None and not demoted_automatic_critical:
            proposed_importance = _bounded_finite(
                decision.importance, default=preset_importance,
                minimum=0.0, maximum=1.0,
            )
        else:
            proposed_importance = preset_importance
        # An explicit caller-provided importance remains a floor; supervision cannot
        # silently downgrade a user-marked critical memory.
        caller_importance = _bounded_finite(
            importance, default=0.0, minimum=0.0, maximum=1.0
        )
        final_importance = max(caller_importance, proposed_importance)
        final_stability = _bounded_finite(
            decision.stability if decision.stability is not None
            and not demoted_automatic_critical else preset_stability,
            default=preset_stability,
            minimum=MIN_STABILITY_DAYS, maximum=MAX_STABILITY_DAYS,
        )
        signal = {
            "source": source,
            "label": label,
            "retain": bool(decision.retain),
            "importance": final_importance,
            "stability": final_stability,
            "reason": str(decision.reason or "")[:500],
        }
        return final_importance, final_stability, signal

    def _has_structured_graph_metadata(self, metadata: dict) -> bool:
        if (
            isinstance(metadata.get("entities"), list)
            or isinstance(metadata.get("relations"), list)
        ):
            return True
        structured = metadata.get("structured_extraction")
        return isinstance(structured, dict) and (
            isinstance(structured.get("entities"), list)
            or isinstance(structured.get("relations"), list)
        )

    def _link_memory_entities(self, memory_id: str, content: str, *,
                              workspace_id: str, repo_id: Optional[str],
                              valid_from: Optional[float]) -> None:
        """Persist edge-derived and exact textual entity evidence for one memory."""
        owns_transaction = not self.store.conn.transaction_owned_by_current_thread()
        try:
            self.store.backfill_memory_entities_for_memory(memory_id)
            entities = self.store.list_entities(SearchFilter(
                # New repo memories must attach to workspace entities already
                # visible to that repo, not only entities owned by the repo.
                workspace_id=workspace_id, repo_id=repo_id, include_ancestors=True,
            ))
            for entity in entities:
                name = (entity.name or "").strip()
                if len(name) < 2:
                    continue
                if re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", content, re.IGNORECASE):
                    self.store.link_memory_entity(
                        memory_id=memory_id, entity_id=entity.id,
                        workspace_id=workspace_id, repo_id=repo_id,
                        source_kind="text_mention", confidence=0.8,
                        valid_from=valid_from, commit=False,
                    )
            if owns_transaction:
                self.store.conn.commit()
        except Exception as exc:
            # ``link_memory_entity(..., commit=False)`` opens a transaction. Never
            # leave a failed best-effort graph enrichment transaction pinned to this
            # thread: it could be committed by an unrelated later write.
            if (owns_transaction
                    and self.store.conn.transaction_owned_by_current_thread()):
                self.store.conn.rollback()
            self._warn_redacted_failure("memory-entity linking", exc)

    def _evolve(self, new_id: str, neighbors: list, *, exclude: Optional[set] = None) -> list[str]:
        """A-MEM-style memory evolution on write: a new memory
        auto-links to its closest still-live neighbors and gives them a small
        reinforcement touch, so old notes gain connectivity (and resist decay a little
        more) when new related knowledge arrives — the network improves in both
        directions, not just for the incoming note. Deterministic, bounded
        (``EVOLVE_MAX_LINKS``), audited, and never raises into the write path.
        """
        if not self.auto_evolve or not neighbors:
            return []
        exclude = exclude or set()
        linked: list[str] = []
        try:
            ranked = sorted(neighbors, key=lambda t: -t[0])
            for sim, nrec in ranked:
                if len(linked) >= EVOLVE_MAX_LINKS:
                    break
                try:
                    similarity = float(sim)
                except (TypeError, ValueError, OverflowError):
                    continue
                if (not math.isfinite(similarity) or similarity < RELATED_SIM_FLOOR
                        or nrec.id in exclude or nrec.id == new_id):
                    continue
                if self.store.has_link(new_id, nrec.id):
                    continue
                self.store.add_link(new_id, nrec.id, "related")
                self.store.reinforce(nrec.id, boost=scoring.INTERACTION_BOOST["view"])
                linked.append(nrec.id)
            if linked:
                self.store.audit("resolver", "evolve", new_id,
                                 f"auto-linked to {len(linked)} related: {', '.join(linked)}")
        except Exception as exc:
            self._warn_redacted_failure("memory evolution", exc)
            return linked
        return linked

    def _search_resolution_vectors(
        self,
        vec: np.ndarray,
        candidate_k: int,
        flt: SearchFilter,
        *,
        canonical_only: bool = False,
    ) -> tuple[list[tuple[str, float]], bool]:
        """Search the injected index, falling back to canonical stored vectors.

        Contradiction resolution is part of write integrity. Treating an index outage
        as an empty neighborhood would silently turn a NOOP/INVALIDATE into ADD. The
        store-backed exact scan preserves portable NumPy semantics without importing a
        concrete backend into core. If both paths fail, the write aborts before mutation.
        """
        if not canonical_only:
            try:
                indexed = self.index.search(vec, candidate_k, filter=flt)
                valid_indexed: list[tuple[str, float]] = []
                for item in indexed:
                    try:
                        memory_id, similarity = item
                        similarity = float(similarity)
                    except (TypeError, ValueError, OverflowError):
                        continue
                    if (isinstance(memory_id, str) and memory_id and
                            math.isfinite(similarity)):
                        valid_indexed.append((memory_id, similarity))
                if valid_indexed:
                    return valid_indexed, False
                # An empty injected result is not enough evidence that no related
                # memory exists: an asynchronously rebuilt or partially populated
                # index can be empty while the canonical Store still has vectors.
                # Fall through to the authoritative scan so resolution cannot turn
                # a NOOP/INVALIDATE into an ADD merely because the index is stale.
            except Exception as exc:
                failure_type = type(exc).__name__
                logger.warning(
                    "vector-index search failed during resolution (%s); "
                    "using canonical vector scan",
                    failure_type,
                )
                try:
                    self.store.audit(
                        "resolver",
                        "index_search_fallback",
                        flt.workspace_id or "resolution",
                        "failure_type=%s" % failure_type,
                        commit=not self.store.conn.transaction_owned_by_current_thread(),
                    )
                except Exception as audit_exc:
                    logger.warning(
                        "could not audit resolution vector fallback (%s)",
                        type(audit_exc).__name__,
                    )

        try:
            query = np.asarray(vec, dtype=np.float32)
            if query.ndim != 1 or query.shape[0] < 1 or not np.isfinite(query).all():
                raise ValueError("resolution query vector must be a finite one-dimensional array")
            with np.errstate(over="ignore", invalid="ignore"):
                norm = float(np.linalg.norm(query))
            if not math.isfinite(norm):
                raise ValueError("resolution query vector norm must be finite")
            if norm > 0:
                query = query / norm
            scores: list[tuple[str, float]] = []
            for memory_id, stored in self.store.iter_vectors(
                flt, dim=int(query.shape[0])
            ):
                if stored.shape != query.shape:
                    continue
                score = float(stored @ query)
                if math.isfinite(score):
                    scores.append((memory_id, score))
            scores.sort(key=lambda item: (-item[1], item[0]))
            return scores[:max(0, int(candidate_k))], True
        except Exception as exc:
            raise RuntimeError("vector neighbor resolution unavailable") from exc

    def _resolve_against_neighbors(self, text: str, vec: np.ndarray, *, workspace_id: str,
                                   repo_id: Optional[str], session_id: Optional[str],
                                   scope: Scope, mtype: MemoryType, candidate_k: int,
                                   subject_key: str = "", claim_kind: str = "",
                                   valid_at: Optional[float] = None,
                                   content: Optional[str] = None):
        """Fetch same-scope neighbors via the vector index and run the deterministic
        resolver (``core.resolve``). Returns ``(decision, neighbors, conflicted_with)``
        so the caller can also evolve the neighborhood and persist a conflict repair.
        An injected-index failure uses the canonical stored-vector mirror; if that scan
        also fails, resolution aborts rather than blindly inserting overlapping truth."""
        flt = SearchFilter(
            workspace_id=workspace_id, repo_id=repo_id,
            session_id=session_id if scope == Scope.SESSION else None,
            scopes=[scope], mtypes=[mtype], valid_at=valid_at,
        )
        hits, canonical_fallback = self._search_resolution_vectors(
            vec, candidate_k, flt
        )
        current_fallback = False
        if not hits and valid_at is not None:
            # A candidate may be backdated before an already-recorded claim. That claim
            # is intentionally outside the candidate's valid-time view, but it still
            # has to be found so the caller can reject an impossible supersession rather
            # than silently creating overlapping history. This fallback is only a guard
            # for an otherwise-empty temporal neighborhood; normal scheduled resolution
            # remains anchored at the candidate's validity time above.
            current_filter = SearchFilter(
                workspace_id=workspace_id, repo_id=repo_id,
                session_id=session_id if scope == Scope.SESSION else None,
                scopes=[scope], mtypes=[mtype],
            )
            hits, canonical_fallback = self._search_resolution_vectors(
                vec,
                candidate_k,
                current_filter,
                canonical_only=canonical_fallback,
            )
            current_fallback = True
        neighbors = []
        for nid, sim in hits:
            nrec = self.store.get_memory(nid)
            if (nrec and nrec.workspace_id == workspace_id and nrec.repo_id == repo_id
                    and nrec.scope == scope and nrec.mtype == mtype
                    and nrec.session_id == session_id
                    and prompt_eligible(nrec.provenance, nrec.metadata)
                    and (memory_matches_filter(nrec, flt)
                         or (current_fallback and nrec.expired_at is None
                             and nrec.valid_to is None))):
                neighbors.append((sim, nrec))
        if subject_key:
            # A claim identity is authoritative, while vector retrieval is only a
            # bounded candidate-discovery aid.  Always add its visible predecessor(s): a
            # reworded update can have very low lexical/hash-vector similarity and fall
            # outside top-K even though it names the exact fact being updated.  Limiting
            # this lookup to ``valid_at`` made ordinary present-time keyed writes depend
            # on vector rank and could let an unkeyed distractor win resolution.
            #
            # Visibility is essential here: ``valid_to IS NULL`` alone also includes a
            # scheduled future successor.  An ordinary present-time write must splice
            # before that successor, not invalidate it merely because its prose is the
            # closest keyed match.
            #
            # ``resolve()`` gives exact claim identities priority over all unkeyed
            # neighbors, and filters claim_kind there, so this remains scoped and never
            # makes different predicates of the same subject conflict.
            known_ids = {rec.id for _, rec in neighbors}
            claim_history = self.store.list_claim_history(
                workspace_id=workspace_id, repo_id=repo_id,
                session_id=session_id if scope == Scope.SESSION else None,
                scope=scope, mtype=mtype, subject_key=subject_key,
                claim_kind=claim_kind,
            )
            authoritative = [
                record for record in claim_history
                if memory_matches_filter(record, flt, at=valid_at)
                and prompt_eligible(record.provenance, record.metadata)
            ]
            if not authoritative and valid_at is not None:
                # A backfill before the first recorded version has no visible
                # predecessor to splice. Preserve the existing chronology guard by
                # surfacing the earliest later version; the caller then rejects an
                # impossible supersession instead of creating overlapping history.
                later = [
                    record for record in claim_history
                    if record.expired_at is None
                    and record.valid_from is not None
                    and record.valid_from > valid_at
                    and prompt_eligible(record.provenance, record.metadata)
                ]
                if later:
                    authoritative = [min(
                        later,
                        key=lambda record: (record.valid_from or float("inf"), record.id),
                    )]
            for record in authoritative:
                if record.id not in known_ids:
                    neighbors.append((1.0, record))
        decision = resolve(
            text, neighbors, subject_key=subject_key, claim_kind=claim_kind,
            candidate_content=content,
        )
        # Repair trigger: when the resolver cannot safely supersede (INVALIDATE/NOOP),
        # surface a genuine high-severity contradiction as a persisted relation instead
        # of a silent coin-flip ADD. ``_repair_conflicts`` is a pure detector (self-
        # guarding, no-op on any failure); persistence happens in ``_resolve_and_store``
        # once the new memory exists and its real id is known.
        conflicted_with: Optional[str] = None
        if decision.op not in (ResolutionOp.INVALIDATE, ResolutionOp.NOOP):
            conflicted_with = self._repair_conflicts(
                "", text, neighbors, workspace_id=workspace_id,
                repo_id=repo_id, valid_at=valid_at,
            )
        return decision, neighbors, conflicted_with

    def _repair_conflicts(self, new_id: str, new_text: str, neighbors: list, *,
                          workspace_id: str, repo_id: Optional[str],
                          valid_at: Optional[float]) -> Optional[str]:
        """Detect a deterministic, high-severity contradiction among the neighbors the
        resolver could not safely supersede.

        The resolver only INVALIDATEs on a shared claim key or on strong joint
        lexical+semantic evidence; a true semantic contradiction with little token
        overlap otherwise lands as a plain ADD — a silent coin-flip between two live
        facts. This hook runs ``core.conflicts.detect_conflicts`` over the same scoped,
        prompt-eligible neighbor set that resolution already saw and returns the
        highest-severity genuine contradiction (``contradiction`` or ``obsolete``)
        when no safe supersession happened. The caller persists the ``conflicts_with``
        relation, audit row, and confidence discount after the new memory exists.

        Conservative by construction: the detector is deterministic and precision-first,
        this runs only for trusted, non-quarantined writes whose resolution produced no
        INVALIDATE/NOOP, only top-K neighbors are considered (the same bounded set the
        resolver saw), duplicates/refinements never create a link, and any failure
        degrades to a no-op — never a write error.
        """
        try:
            conflicts = detect_conflicts(new_text, (rec for _, rec in neighbors))
        except Exception:
            return None
        if not conflicts:
            return None
        conflict = conflicts[0]
        if conflict.type not in ("contradiction", "obsolete"):
            return None
        if conflict.severity < CONFLICT_MIN_SEVERITY:
            return None
        target_id = conflict.memory_id
        if not target_id or target_id == new_id:
            return None
        return target_id

    # ── ingest: extract-then-remember ───────────────────────────────────────────
    def ingest(self, text: str, *, workspace_id: str, repo_id: Optional[str] = None,
               session_id: Optional[str] = None, scope: Optional[Scope] = None,
               default_mtype: MemoryType = MemoryType.SEMANTIC,
               metadata: Optional[dict] = None, resolve_conflicts: bool = True) -> dict:
        """Store raw, undistilled text. When an ``Extractor`` is configured, the text is
        first distilled into discrete facts (each stored with resolution + evolution,
        like any ``remember``); without one this is exactly ``remember`` — the offline
        default never changes behaviour. Extraction failures degrade to passthrough:
        ingest never loses the write."""
        # Raw input may be sent to a configured extractor, so block credentials before
        # extraction rather than relying only on the final derived-memory write.
        reject_secrets((("ingest content", text), ("metadata", metadata)))
        if scope is not None and Scope(scope) == Scope.USER:
            raise ValueError(_USER_SCOPE_WRITE_ERROR)
        facts = None
        extracted = False
        # Quarantine precedes optional extraction. An explicitly untrusted payload that
        # already matches the deterministic policy must not be sent to an LLM extractor
        # or transformed into benign-looking derived facts before the write path has a
        # chance to retain it safely for inspection.
        input_poisoning = assess_untrusted_payload(text, metadata=metadata)
        if self.extractor is not None and not input_poisoning.quarantined:
            try:
                facts = self.extractor.extract(text)
                extracted = bool(facts)
            except Exception:
                facts = None
        if not facts:
            from engraphis.core.interfaces import ExtractedFact
            facts = [ExtractedFact(content=text)]
            extracted = False

        results = []
        base_metadata = dict(metadata or {})
        source_sha256 = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
        fallback_detected = False
        for fact_index, f in enumerate(facts, start=1):
            fact_own = dict(getattr(f, "metadata", {}) or {})
            extracted_metadata = {
                key: value for key, value in fact_own.items()
                if key in EXTRACTOR_METADATA_KEYS
            }
            raw_fallback = extracted_metadata.get("extraction_fallback")
            if isinstance(raw_fallback, dict):
                mode = str(raw_fallback.get("mode") or "")
                reason = str(raw_fallback.get("reason") or "")
                if (
                    mode in {"llm", "llm_structured"}
                    and reason == "provider_or_output_error"
                ):
                    extracted_metadata["extraction_fallback"] = {
                        "mode": mode,
                        "reason": reason,
                    }
                    fallback_detected = True
                else:
                    extracted_metadata.pop("extraction_fallback", None)
            else:
                extracted_metadata.pop("extraction_fallback", None)
            if isinstance(extracted_metadata.get("llm_extraction"), dict):
                # Group all facts derived from one source without retaining the raw
                # source or prompt. The dashboard activity viewer can therefore explain
                # one input -> N memories while keeping provider payloads private.
                extracted_metadata["llm_extraction"] = {
                    **extracted_metadata["llm_extraction"],
                    "source_sha256": source_sha256,
                    "fact_index": fact_index,
                    "fact_count": len(facts),
                }
            # This is the one place that can tell the two apart: ``fact_own`` is computed
            # fresh from the Extractor's real output, while ``base_metadata`` is the
            # caller's ingress envelope. Only documented extraction fields may cross
            # that boundary, so provenance/quarantine and other authority fields remain
            # service-owned even when an extractor returns arbitrary metadata.
            trusted = frozenset(k for k in GRAPH_HINT_KEYS if k in extracted_metadata)
            fact_metadata = {**base_metadata, **extracted_metadata}
            if isinstance(extracted_metadata.get("llm_extraction"), dict):
                # First separate caller-supplied hints from the extractor's own output.
                # Then preserve the model-produced hints as review evidence without
                # allowing either set to materialize graph state before approval.
                fact_metadata = _rehome_untrusted_graph_hints(fact_metadata, trusted)
                _, fact_metadata, _ = pending_llm_extraction_envelope(
                    fact_metadata.get("provenance"), fact_metadata,
                )
                trusted = frozenset({_INTERNAL_DERIVED_GRAPH_KEY})
            results.append(self.remember_with_resolution(
                f.content, workspace_id=workspace_id, repo_id=repo_id,
                session_id=session_id, mtype=f.mtype or default_mtype, scope=scope,
                title=f.title, importance=f.importance, keywords=f.keywords,
                metadata=fact_metadata,
                resolve_conflicts=resolve_conflicts, _trusted_graph_keys=trusted,
            ))
        return {
            "facts": results,
            "count": len(results),
            "extracted": bool(extracted and not fallback_detected),
        }

    # ── consolidation: the sleep-time loop, callable on demand (Phase 4) ───────
    def consolidate(
        self, *, workspace_id: str, repo_id: Optional[str] = None,
        min_cluster: int = 3, subject_jaccard: float = 0.40,
        archive_below: float = 0.05, dry_run: bool = False,
        profiles: bool = False, min_mentions: int = 3,
        infer: bool = False, structured: bool = False,
        llm=None, now: Optional[float] = None,
    ) -> dict:
        """One sleep-time consolidation sweep — episodic→semantic distillation plus
        decayed-transient archival. See ``core.consolidate.consolidate`` for knobs.

        LLM-derived facts, profiles, and graph hints remain review-pending; valid source
        IDs establish lineage but do not establish semantic entailment. Deterministic
        fallback digests retain the ordinary local trust policy.
        """
        from engraphis.core.consolidate import consolidate as _consolidate
        return _consolidate(
            self,
            workspace_id=workspace_id,
            repo_id=repo_id,
            min_cluster=min_cluster,
            subject_jaccard=subject_jaccard,
            archive_below=archive_below,
            dry_run=dry_run,
            profiles=profiles,
            min_mentions=min_mentions,
            infer=infer,
            structured=structured,
            llm=llm,
            now=now,
        )

    # ── read ──────────────────────────────────────────────────────────────────
    def _recall_filter(self, *, workspace_id: Optional[str], repo_id: Optional[str],
                       session_id: Optional[str], scopes: Optional[list],
                       mtypes: Optional[list], as_of: Optional[float],
                       valid_at: Optional[float] = None,
                       known_at: Optional[float] = None) -> SearchFilter:
        """Build an ancestor-aware filter, resolving a session's parent repo in core.

        The service performs the same validation for friendly error payloads, but direct
        ``MemoryEngine`` callers must get identical hierarchy semantics.
        """
        if session_id:
            session = self.store.get_session(session_id)
            if session is None:
                raise ValueError(f"no session with id '{session_id}'")
            if workspace_id is not None and session["workspace_id"] != workspace_id:
                raise ValueError("session_id does not belong to that workspace")
            if repo_id is not None and session.get("repo_id") != repo_id:
                raise ValueError("session_id does not belong to that repo")
            workspace_id = workspace_id or session["workspace_id"]
            repo_id = repo_id or session.get("repo_id")
        return SearchFilter(
            workspace_id=workspace_id, repo_id=repo_id, session_id=session_id,
            scopes=scopes, mtypes=mtypes, as_of=as_of, valid_at=valid_at,
            known_at=known_at, include_ancestors=True,
        )

    def recall(self, query: str, *, workspace_id: Optional[str] = None,
               repo_id: Optional[str] = None, session_id: Optional[str] = None,
               scopes: Optional[list] = None,
               mtypes: Optional[list] = None, as_of: Optional[float] = None,
               valid_at: Optional[float] = None, known_at: Optional[float] = None,
               k: int = 8, token_budget: Optional[int] = None,
               retrieval_profile: str = "balanced", candidate_depth: str = "fixed",
               diagnostics: bool = False,
               include_untrusted: bool = False,
               prompt_only: bool = False,
               planning: str = "off",
               mtype_limits: Optional[dict] = None,
               reinforce: bool = False) -> RecallResult:
        flt = self._recall_filter(
            workspace_id=workspace_id, repo_id=repo_id, session_id=session_id,
            scopes=scopes, mtypes=mtypes, as_of=as_of, valid_at=valid_at,
            known_at=known_at,
        )
        # Recall is observational unless the caller has an explicit use signal.
        # Historical inspection is always observational: reinforcement would make a
        # past reconstruction alter future ranking.
        return self.recall_engine.recall(
            query, flt, k=k, reinforce=bool(reinforce) and not flt.historical,
            token_budget=token_budget, retrieval_profile=retrieval_profile,
            candidate_depth=candidate_depth,
            diagnostics=diagnostics,
            include_untrusted=bool(include_untrusted),
            prompt_only=bool(prompt_only),
            planning=planning,
            mtype_limits=mtype_limits,
        )

    def adaptive_context(
        self,
        query: str,
        history: str,
        *,
        workspace_id: Optional[str] = None,
        repo_id: Optional[str] = None,
        session_id: Optional[str] = None,
        scopes: Optional[list] = None,
        mtypes: Optional[list] = None,
        as_of: Optional[float] = None,
        valid_at: Optional[float] = None,
        known_at: Optional[float] = None,
        k: int = 8,
        max_context_tokens: int = 4096,
        retrieval_token_budget: Optional[int] = None,
        confidence_floor: float = 0.25,
        retrieval_profile: str = "balanced",
        candidate_depth: str = "adaptive",
        diagnostics: bool = False,
        planning: str = "off",
        mtype_limits: Optional[dict] = None,
        reinforce: bool = False,
    ) -> AdaptiveContextResult:
        """Choose raw history, compact recall, or a wider raw-history fallback.

        The host supplies the exact history text it is considering for the next
        model prompt.  If that text already fits ``max_context_tokens``, Engraphis
        performs no embedding, search, or retrieval.  Otherwise it first attempts
        a smaller packed recall.  Absolute query-to-source support (the same
        calibrated signal used by grounded recall) decides whether to trust that
        compact result; weak support widens back to the most recent raw history
        that fits the overall budget.

        This method never reinforces bypassed or weak retrievals.  Strong packed
        evidence is reinforced only when the caller supplies an explicit use
        signal through ``reinforce=True``.
        """
        from engraphis.core.grounded import support_scores

        if isinstance(max_context_tokens, bool):
            raise ValueError("max_context_tokens must be a non-negative integer")
        try:
            max_budget = int(max_context_tokens)
        except (TypeError, ValueError) as exc:
            raise ValueError("max_context_tokens must be a non-negative integer") from exc
        if max_budget < 0:
            raise ValueError("max_context_tokens must be a non-negative integer")
        try:
            floor = float(confidence_floor)
        except (TypeError, ValueError) as exc:
            raise ValueError("confidence_floor must be between 0 and 1") from exc
        if not math.isfinite(floor) or not 0.0 <= floor <= 1.0:
            raise ValueError("confidence_floor must be between 0 and 1")
        if isinstance(k, bool):
            raise ValueError("k must be a positive integer")
        try:
            k = int(k)
        except (TypeError, ValueError) as exc:
            raise ValueError("k must be a positive integer") from exc
        if k <= 0:
            raise ValueError("k must be a positive integer")
        retrieval_profile = str(retrieval_profile or "balanced").strip().casefold()
        if retrieval_profile not in RETRIEVAL_PROFILES:
            choices = ", ".join(sorted(RETRIEVAL_PROFILES))
            raise ValueError(f"retrieval_profile must be one of: {choices}")
        candidate_depth = str(candidate_depth or "adaptive").strip().casefold()
        if candidate_depth not in CANDIDATE_DEPTH_MODES:
            choices = ", ".join(sorted(CANDIDATE_DEPTH_MODES))
            raise ValueError(f"candidate_depth must be one of: {choices}")

        counter = getattr(self.recall_engine.context_packer, "count_tokens", None)
        if not callable(counter):
            raise ValueError("adaptive context requires a context packer token counter")
        counter_name = str(
            getattr(self.recall_engine.context_packer, "token_counter_identity", None)
            or getattr(counter, "identity", None)
            or getattr(counter, "__name__", None)
            or type(counter).__name__
        )
        def count_tokens(value: str) -> int:
            raw_count: object = counter(value)
            if isinstance(raw_count, bool) or not isinstance(raw_count, int):
                raise ValueError("adaptive context token counter must return a non-negative integer")
            if raw_count < 0:
                raise ValueError("adaptive context token counter must return a non-negative integer")
            return raw_count

        source_history = str(history or "")
        history_tokens = count_tokens(source_history)

        if retrieval_token_budget is None:
            retrieval_budget = min(max_budget, max(1, max_budget // 2)) if max_budget else 0
        else:
            if isinstance(retrieval_token_budget, bool):
                raise ValueError(
                    "retrieval_token_budget must be between 0 and max_context_tokens"
                )
            try:
                retrieval_budget = int(retrieval_token_budget)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "retrieval_token_budget must be between 0 and max_context_tokens"
                ) from exc
            if not 0 <= retrieval_budget <= max_budget:
                raise ValueError(
                    "retrieval_token_budget must be between 0 and max_context_tokens"
                )

        if history_tokens <= max_budget:
            return AdaptiveContextResult(
                context=source_history,
                mode="history_bypass",
                reason="provided history already fits the prompt budget",
                history_tokens=history_tokens,
                context_tokens=history_tokens,
                max_context_tokens=max_budget,
                retrieval_budget_tokens=retrieval_budget,
                token_counter=counter_name,
            )

        result = self.recall(
            query,
            workspace_id=workspace_id,
            repo_id=repo_id,
            session_id=session_id,
            scopes=scopes,
            mtypes=mtypes,
            as_of=as_of,
            valid_at=valid_at,
            known_at=known_at,
            k=k,
            token_budget=retrieval_budget,
            retrieval_profile=retrieval_profile,
            candidate_depth=candidate_depth,
            diagnostics=diagnostics,
            prompt_only=True,
            planning=planning,
            mtype_limits=mtype_limits,
            reinforce=False,
        )
        # Confidence must describe evidence the agent will actually see, not a
        # high-scoring candidate that the hard-budget packer omitted.
        packed_titles = {
            str(chunk.get("id") or ""): " ".join(
                str(chunk.get("title") or "").split()
            )[:120]
            for chunk in result.chunks
        }
        per_source_support = support_scores(
            query,
            [
                f"{packed_titles.get(str(packed.id), '')}\n{packed.excerpt}".strip()
                for packed in result.packed_chunks
            ],
            self.embedder,
        )
        support = max(per_source_support, default=0.0)
        if not result.packed_chunks or support < floor:
            wider, truncated = fit_recent_history(
                source_history,
                token_budget=max_budget,
                count_tokens=count_tokens,
            )
            if wider:
                return AdaptiveContextResult(
                    context=wider,
                    mode="history_fallback",
                    reason="retrieval support was weak, so raw recent history was widened",
                    history_tokens=history_tokens,
                    context_tokens=count_tokens(wider),
                    max_context_tokens=max_budget,
                    retrieval_budget_tokens=retrieval_budget,
                    retrieval_support=support,
                    retrieved=True,
                    widened=True,
                    truncated_history=truncated,
                    token_counter=counter_name,
                    recall=result,
                )
            return AdaptiveContextResult(
                context="",
                mode="low_confidence_abstain",
                reason="retrieval support was weak and no raw history fit the prompt budget",
                history_tokens=history_tokens,
                context_tokens=0,
                max_context_tokens=max_budget,
                retrieval_budget_tokens=retrieval_budget,
                retrieval_support=support,
                retrieved=True,
                truncated_history=truncated,
                token_counter=counter_name,
                recall=result,
            )

        historical = any(
            anchor is not None for anchor in (as_of, valid_at, known_at)
        )
        if reinforce and not historical:
            for packed in result.packed_chunks:
                self.store.reinforce(
                    packed.id,
                    boost=scoring.INTERACTION_BOOST["recall"],
                )
        return AdaptiveContextResult(
            context=result.context,
            mode="retrieval",
            reason="history exceeded the prompt budget and retrieved evidence was strong",
            history_tokens=history_tokens,
            context_tokens=count_tokens(result.context),
            max_context_tokens=max_budget,
            retrieval_budget_tokens=retrieval_budget,
            retrieval_support=support,
            retrieved=True,
            token_counter=counter_name,
            recall=result,
        )

    def grounded_recall(self, query: str, *, workspace_id: Optional[str] = None,
                        repo_id: Optional[str] = None, session_id: Optional[str] = None,
                        scopes: Optional[list] = None,
                        mtypes: Optional[list] = None, as_of: Optional[float] = None,
                        valid_at: Optional[float] = None, known_at: Optional[float] = None,
                        k: int = 8, llm=None, min_support: Optional[float] = None,
                        token_budget: Optional[int] = None,
                        retrieval_profile: str = "balanced", candidate_depth: str = "fixed",
                        diagnostics: bool = False,
                        planning: str = "off",
                        mtype_limits: Optional[dict] = None,
                        max_citations: int = 5, reinforce: bool = True):
        """Recall, then answer *strictly from* what was recalled — with citations and an
        explicit abstain when the evidence is too weak (``core.grounded``). Offline and
        deterministic (extractive answer) unless an ``LLM`` is injected to synthesise
        prose under the same source/abstain contract. Returns a ``GroundedAnswer``.

        This is the "grounded, not guessed" read: it will not surface an answer just
        because the vector index returned its nearest neighbour — an off-topic query
        abstains. See ``core.grounded`` for the support signal and the security note.
        """
        from engraphis.core import grounded as _grounded
        flt = self._recall_filter(
            workspace_id=workspace_id, repo_id=repo_id, session_id=session_id,
            scopes=scopes, mtypes=mtypes, as_of=as_of, valid_at=valid_at,
            known_at=known_at,
        )
        # Recall without reinforcing here: a grounded read should reward only the memories
        # it actually cites, and an abstain should reward nothing — don't reinforce the
        # irrelevant nearest-neighbours an off-topic query happened to surface.
        result = self.recall_engine.recall(
            query, flt, k=k, reinforce=False, token_budget=token_budget,
            retrieval_profile=retrieval_profile, candidate_depth=candidate_depth,
            diagnostics=diagnostics,
            prompt_only=True,
            planning=planning,
            mtype_limits=mtype_limits,
        )
        floor = _grounded.GROUNDED_SUPPORT_FLOOR if min_support is None else min_support
        answer = _grounded.build_grounded_answer(query, result, self.embedder, llm=llm,
                                                 min_support=floor, max_citations=max_citations)
        if reinforce and not flt.historical and answer.grounded:
            for cite in answer.citations:
                if cite.get("id"):
                    self.store.reinforce(cite["id"], boost=scoring.INTERACTION_BOOST["recall"])
        return answer

    def why(self, query: str, *, workspace_id: str, repo_id: Optional[str] = None,
            k: int = 5, valid_at: Optional[float] = None,
            known_at: Optional[float] = None, prompt_only: bool = False) -> dict:
        """Rationale + history for a decision or fact: the live
        answer, plus whatever it superseded, if anything. This is the bi-temporal "why"
        that a flat-namespace store (or a plain vector store) cannot answer — the
        superseded fact still exists, just outside the default visibility window.
        """
        flt = SearchFilter(
            workspace_id=workspace_id, repo_id=repo_id, include_ancestors=True,
            valid_at=valid_at, known_at=known_at,
        )
        live = [
            r for _, r in self._relatedness(
                query, flt, include_invalid=False, prompt_only=prompt_only,
            )[:k]
        ]
        history: list[MemoryRecord] = []
        if live:
            seen = {r.id for r in live}
            anchor = f"{live[0].title} {live[0].content}"
            for _, r in self._relatedness(
                    anchor, flt, include_invalid=True, prompt_only=prompt_only):
                if r.id in seen or r.valid_to is None:
                    continue
                history.append(r)
                seen.add(r.id)
                if len(history) >= k:
                    break
        return {"answer": live, "supersedes": history}

    def timeline(self, query: str, *, workspace_id: str, repo_id: Optional[str] = None,
                limit: int = 20, valid_at: Optional[float] = None,
                known_at: Optional[float] = None, prompt_only: bool = False) -> list[MemoryRecord]:
        """Chronological, bi-temporal history of a fact: what we believed and when.
        Includes invalidated versions; sorted by ``valid_from``.
        """
        flt = SearchFilter(
            workspace_id=workspace_id, repo_id=repo_id, include_ancestors=True,
            valid_at=valid_at, known_at=known_at,
        )
        recs = [
            r for _, r in self._relatedness(
                query, flt, include_invalid=True, prompt_only=prompt_only,
            )[:limit]
        ]
        recs.sort(key=lambda r: r.valid_from or r.ingested_at or 0.0)
        return recs

    def _relatedness(self, query: str, flt: SearchFilter, *,
                     include_invalid: bool,
                     prompt_only: bool = False) -> list[tuple[float, MemoryRecord]]:
        """Score every matching memory — optionally including invalidated ones — by the
        max of semantic similarity and lexical token overlap. ``why``/``timeline`` need to
        search *through* bi-temporal history, which the normal vector index ``search()``
        deliberately excludes (it's the live-recall path), so this recomputes similarity
        directly from ``Store.iter_vectors(..., include_invalid=True)`` instead.
        """
        semantic_ready = bool(getattr(self.embedder, "supports_semantic_search", False))
        persistent_store = (
            self.store.path != ":memory:"
            and not self.store.path.startswith("file::memory:")
        )
        if semantic_ready and persistent_store:
            # History helpers bypass RecallEngine's readiness gate and read the
            # portable vector mirror directly; never compare a query against a
            # stale/mixed embedding space on that path.
            semantic_ready = bool(
                self.embedding_space
                and self.store.embedding_space_ready(self.embedding_space)
            )
        sem: dict[str, float] = {}
        if semantic_ready:
            qvec = self.embedder.embed([query])[0]
            qn = qvec / (float(np.linalg.norm(qvec)) or 1.0)
            for mid, vec in self.store.iter_vectors(
                    flt, include_invalid=include_invalid, dim=int(qn.shape[0])):
                sem[mid] = float(np.dot(qn, vec))
        q_tokens = tokenize(query)
        out: list[tuple[float, MemoryRecord]] = []
        records = self.store.list_memories(
            flt, include_invalid=include_invalid, limit=500, prompt_only=prompt_only,
        )
        if include_invalid and flt.known_at is not None:
            # History must retain closed valid-time intervals, but cannot expose a
            # record that was not known at the requested system-time snapshot.
            records = [
                rec for rec in records
                if (rec.ingested_at is None or rec.ingested_at <= flt.known_at)
                and (rec.expired_at is None or flt.known_at < rec.expired_at)
            ]
        for rec in records:
            # Public history is model-adjacent just like ordinary recall: tool output
            # can be inserted into an agent transcript. Pending records therefore stay
            # in explicit inspection workflows only, while direct core callers retain
            # an opt-in inspection mode for governed local maintenance.
            eligible = (
                prompt_eligible(rec.provenance, rec.metadata)
                if prompt_only
                else inspection_eligible(rec.provenance, rec.metadata)
            )
            if not eligible:
                continue
            lex = jaccard(q_tokens, tokenize(f"{rec.title} {rec.content}"))
            score = max(sem.get(rec.id, 0.0), lex)
            if score > 0.05:
                out.append((score, rec))
        out.sort(key=lambda t: t[0], reverse=True)
        return out

    def recall_proactive(self, *, workspace_id: str, repo_id: Optional[str] = None,
                         k: int = 10, user_id: Optional[str] = None,
                         agent: Optional[str] = None,
                         prompt_only: bool = False) -> dict:
        """"What should I know right now" with no explicit query — conscious/proactive
        recall: importance + recency + retention, no semantic arm,
        plus the repo's last-session handoff (open threads / summary) if there is one.
        """
        flt = SearchFilter(
            workspace_id=workspace_id, repo_id=repo_id, include_ancestors=True,
        )
        now = now_ts()
        scored = []
        always: list = []
        candidates = self.store.list_memories(flt, limit=500, prompt_only=prompt_only)
        overrides = self.store.list_proactive_overrides(flt, prompt_only=prompt_only)
        records = {rec.id: rec for rec in [*candidates, *overrides]}.values()
        # SQLite's timestamp ordering is not total when records share an ingestion
        # timestamp.  Use the id as a stable final key so the agenda does not change
        # between calls (or between the bounded and override queries).
        def stable_record_key(rec: MemoryRecord) -> tuple:
            ingested_at = rec.ingested_at
            return (
                ingested_at is None,
                -(float(ingested_at) if ingested_at is not None else 0.0),
                rec.id,
            )
        for rec in records:
            eligible = (
                prompt_eligible(rec.provenance, rec.metadata)
                if prompt_only
                else inspection_eligible(rec.provenance, rec.metadata)
            )
            if not eligible:
                continue
            # Per-memory proactive rules: a user-flagged ``metadata["proactive"]`` value
            # ("always" | "never") overrides the score, and ``pinned`` always includes
            # (the Mem0-style add-to-context analogue). "never" excludes regardless of
            # importance so a user can silence a memory from the agenda.
            proactive = (rec.metadata or {}).get("proactive")
            if str(proactive).lower() == "never":
                continue
            if str(proactive).lower() == "always" or rec.pinned:
                always.append(rec)
                continue
            scored.append((scoring.score_proactive(rec, now=now), rec))
        always.sort(key=stable_record_key)
        scored.sort(key=lambda t: (-t[0], *stable_record_key(t[1])))
        top = [r for _, r in scored[:k]]
        if always:
            # Keep the user's explicit choices first, then the score-ranked remainder.
            top = always + [r for r in top if r.id not in {a.id for a in always}]
            top = top[:k]

        last_session: dict = {}
        if repo_id:
            last = self.store.get_last_session(
                workspace_id, repo_id, user_id=user_id, agent=agent,
            )
            if last:
                last_session = {
                    "session_id": last["id"], "summary": last.get("summary") or "",
                    "open_threads": last.get("open_threads") or [],
                    "outcome": last.get("outcome") or "",
                }
        return {"memories": top, "last_session": last_session}

    # ── governance (audited; never a silent hard delete — AGENTS.md §3.2) ───────
    def retire(self, memory_id: str, *, reason: str = "", actor: str = "user") -> dict:
        """Remove a memory from live recall while retaining temporal history.

        This is deliberately distinct from :meth:`secure_erase`: retirement is the
        routine, reversible-by-history governance action; it does not remove the row,
        FTS entry, vector, or historical graph evidence.
        """
        if self.store.get_memory(memory_id) is None:
            raise KeyError(f"no memory with id '{memory_id}'")
        self.store.close_validity(memory_id, actor=actor, reason=reason or "retired by request")
        # Preserve the vector for explicit historical/as_of recall. Temporal filtering
        # keeps this retired row out of the current live view.
        return {"id": memory_id, "status": "retired", "reason": reason}

    def forget(self, memory_id: str, *, reason: str = "", actor: str = "user") -> dict:
        """Deprecated compatibility alias for :meth:`retire`.

        Keep the old status string for programmatic consumers that used this legacy
        method; new callers must use ``retire`` so its temporal semantics are clear.
        """
        result = self.retire(memory_id, reason=reason, actor=actor)
        return {**result, "status": "forgotten", "deprecated": True}

    def secure_erase(self, memory_id: str, *, actor: str = "user") -> dict:
        """Irreversibly erase a leaked secret from this local Store and derivatives.

        ``VectorIndex`` may be an injected external backend. Request its deletion first,
        but do not leave the local SQLite copy intact if that backend is unavailable; the
        returned status explicitly reports that incomplete external cleanup.
        """
        index_cleanup = "not_configured"
        try:
            self.index.delete([memory_id])
            index_cleanup = "deleted"
        except Exception:  # noqa: BLE001 - must still erase the authoritative local copy
            index_cleanup = "failed"
        result = self.store.secure_erase_memory(memory_id, actor=actor)
        result["vector_index_cleanup"] = index_cleanup
        if index_cleanup == "failed":
            result["external_index_limitation"] = (
                "The configured vector index did not confirm deletion; remediate that backend "
                "separately before treating the secret as fully erased."
            )
        return result

    def pin(self, memory_id: str, *, pinned: bool = True, actor: str = "user") -> dict:
        if self.store.get_memory(memory_id) is None:
            raise KeyError(f"no memory with id '{memory_id}'")
        self.store.set_pinned(memory_id, pinned)
        self.store.audit(actor, "pin" if pinned else "unpin", memory_id, "")
        return {"id": memory_id, "pinned": pinned}

    def correct(self, memory_id: str, new_content: str, *, reason: str = "",
                actor: str = "user") -> dict:
        with self._write_lock:
            return self._correct_locked(
                memory_id, new_content, reason=reason, actor=actor,
            )

    def _correct_locked(self, memory_id: str, new_content: str, *, reason: str,
                        actor: str) -> dict:
        """Insert a replacement and close its predecessor as one atomic transition."""
        old = self.store.get_memory(memory_id)
        if old is None:
            raise KeyError(f"no memory with id '{memory_id}'")
        effective_at = now_ts()
        if not _governable_source(old, at=effective_at):
            raise ValueError("only a current or quarantined memory can be corrected")
        metadata = dict(old.metadata)
        metadata["corrects"] = memory_id
        metadata["supersedes"] = [memory_id]
        # Missing/legacy provenance must not fall through to the direct-engine trusted
        # default. A correction preserves trust only when the source was approved.
        metadata["provenance"] = (
            dict(old.provenance)
            if provenance_is_approved(old.provenance)
            else {
                "source": str((old.provenance or {}).get("source") or "legacy_unverified"),
                "trusted": False,
                "review_state": REVIEW_PENDING,
                "trust_origin": "derived_unapproved",
            }
        )

        def finalize_correction(new_id: str) -> None:
            self.store.advance_memory_modified_hlc(new_id, commit=False)
            self.store.conn.execute(
                "UPDATE memories SET pinned=?, sensitivity=?, stability=?, access_count=?, "
                "last_access=? WHERE id=?",
                (
                    int(old.pinned),
                    old.sensitivity or "normal",
                    old.stability,
                    old.access_count,
                    old.last_access,
                    new_id,
                ),
            )
            self.store.close_validity(
                memory_id, at=effective_at, actor=actor,
                reason=reason or "corrected",
            )

        new_id = self.remember(
            new_content,
            workspace_id=_required_memory_workspace_id(old),
            repo_id=old.repo_id,
            session_id=old.session_id,
            mtype=old.mtype,
            scope=_writable_scope(old.scope, old.repo_id),
            title=old.title,
            importance=old.importance,
            confidence=old.confidence,
            keywords=old.keywords,
            metadata=metadata,
            valid_from=effective_at,
            resolve_conflicts=False,
            subject_key=old.subject_key,
            claim_kind=old.claim_kind,
            _transactional_finalizer=finalize_correction,
        )
        # The old vector is historical evidence; temporal filtering hides it from
        # current recall while keeping semantic time travel complete.
        return {"id": new_id, "superseded": [memory_id], "reason": reason}

    def approve_for_prompt(self, memory_id: str, *, reviewer: str,
                           reason: str = "", replacement_content: Optional[str] = None) -> dict:
        """Create an explicitly approved successor for governed human review.

        This is intentionally an engine-only primitive.  MCP and ordinary REST ingress
        never expose it: their caller can be prompted by the very content under review.
        The interactive dashboard/TTY owner ceremony is responsible for choosing a
        reviewer identity before it calls this method.
        """
        reviewer = str(reviewer or "").strip()
        if not reviewer:
            raise ValueError("reviewer is required for approval")
        reason = str(reason or "").strip()
        if not reason:
            raise ValueError("approval reason is required")
        # Keep lookup and insert in the engine's write critical section. Without it two
        # retries of the same pending source could each observe no successor and create
        # duplicate prompt-visible records. The normal remember path re-enters this RLock.
        with self._write_lock:
            old = self.store.get_memory(memory_id)
            if old is None:
                raise KeyError(f"no memory with id '{memory_id}'")
            # Normal local-agent writes are already approved and do not need an owner
            # ceremony. Treat an explicit retry against such a record as an idempotent
            # no-op so older clients that still call the former approval step do not fail
            # after upgrading. A human correction still uses the governed correction path.
            if provenance_is_approved(old.provenance):
                return {
                    "id": old.id,
                    "approved_from": old.provenance.get("approved_from"),
                    "reviewer": str(
                        old.metadata.get("approval", {}).get("reviewer", reviewer)
                    ),
                }

            now = now_ts()
            if (
                old.expired_at is not None
                or (old.valid_from is not None and old.valid_from > now)
                or (old.valid_to is not None and old.valid_to <= now)
            ):
                raise ValueError("only a live pending memory can be approved")
            if old.provenance.get("review_state") != REVIEW_PENDING:
                raise ValueError("only a pending memory can be approved")

            # ``approved_from`` lives in structured provenance and metadata rather than a
            # mutable text field. Approval is an owner-driven, infrequent ceremony, so a
            # bounded exact-scope scan is both portable to SQLite builds without JSON1 and
            # avoids adding a denormalized trust index solely for retry idempotency.
            source_scope = SearchFilter(
                workspace_id=_required_memory_workspace_id(old),
                repo_id=old.repo_id,
                session_id=old.session_id if old.scope == Scope.SESSION else None,
            )
            # Include retired successors in this audit lookup. A retry may return a
            # live successor, but it must never create a fresh one after the original
            # approved record was deliberately retired: that would resurrect content
            # without a new governed write.
            for candidate in self.store.list_memories(source_scope, include_invalid=True):
                approved_from = candidate.provenance.get("approved_from")
                if approved_from is None:
                    approved_from = candidate.metadata.get("approved_from")
                if (approved_from == old.id and provenance_is_approved(candidate.provenance)):
                    if (
                        candidate.expired_at is not None
                        or (candidate.valid_from is not None and candidate.valid_from > now)
                        or (candidate.valid_to is not None and candidate.valid_to <= now)
                    ):
                        raise ValueError("memory has already been approved and retired")
                    return {
                        "id": candidate.id,
                        "approved_from": old.id,
                        "reviewer": str(candidate.metadata.get("approval", {}).get(
                            "reviewer", reviewer
                        )),
                    }

            content = str(replacement_content if replacement_content is not None else old.content)
            metadata = {
                "approved_from": old.id,
                "approval": {
                    "reviewer": reviewer[:200],
                    "reason": reason[:500],
                },
                "provenance": {
                    "source": "human_review",
                    "trusted": True,
                    "review_state": REVIEW_APPROVED,
                    "trust_origin": "human_approval",
                    "approved_from": old.id,
                },
            }
            if (old.metadata or {}).get("proactive"):
                metadata["proactive"] = old.metadata["proactive"]

            def finalize_approval(new_id: str) -> None:
                self.store.advance_memory_modified_hlc(new_id, commit=False)
                self.store.conn.execute(
                    "UPDATE memories SET pinned=?, sensitivity=?, stability=?, "
                    "access_count=?, last_access=? WHERE id=?",
                    (
                        int(old.pinned),
                        old.sensitivity or "normal",
                        old.stability,
                        old.access_count,
                        old.last_access,
                        new_id,
                    ),
                )
                self.store.audit(
                    "human_review", "approve", new_id,
                    f"from={old.id}; reviewer={reviewer[:200]}; reason={reason[:500]}",
                )
            result = self.remember_with_resolution(
                content,
                workspace_id=_required_memory_workspace_id(old),
                repo_id=old.repo_id,
                session_id=old.session_id,
                mtype=old.mtype,
                scope=_writable_scope(old.scope, old.repo_id),
                title=old.title,
                importance=old.importance,
                confidence=old.confidence,
                keywords=old.keywords,
                metadata=metadata,
                valid_from=old.valid_from,
                resolve_conflicts=False,
                subject_key=old.subject_key,
                claim_kind=old.claim_kind,
                _approval_override=True,
                _transactional_finalizer=finalize_approval,
            )
            return {"id": result["id"], "approved_from": old.id, "reviewer": reviewer}

    def promote(self, memory_id: str, target_scope: Scope, *, reason: str = "",
                actor: str = "user") -> dict:
        with self._write_lock:
            return self._promote_locked(
                memory_id, target_scope, reason=reason, actor=actor,
            )

    def _promote_locked(self, memory_id: str, target_scope: Scope, *, reason: str,
                        actor: str) -> dict:
        """Widen one live memory's scope without rewriting it in place.

        Promotion creates (or deduplicates into) a wider-scoped record first, then
        bi-temporally closes the narrow source and links the two. This preserves the
        provenance/history contract while preventing duplicate recall in the source
        context. Protection, confidentiality, and learned stability never decrease.
        """
        old = self.store.get_memory(memory_id)
        if old is None:
            raise KeyError(f"no memory with id '{memory_id}'")
        if not inspection_eligible(old.provenance, old.metadata):
            raise ValueError("untrusted memory cannot be promoted: record is quarantined")
        if not provenance_is_approved(old.provenance):
            raise ValueError("untrusted memory cannot be promoted; create a fresh approved local memory")
        now = now_ts()
        if (old.expired_at is not None
                or (old.valid_from is not None and old.valid_from > now)
                or (old.valid_to is not None and old.valid_to <= now)):
            raise ValueError("only a live memory can be promoted")
        if old.scope == Scope.SESSION:
            source_session = self.store.get_session(str(old.session_id or ""))
            if source_session is None or source_session.get("status") != "active":
                raise ValueError("cannot promote memory from a closed session")
        target_scope = Scope(target_scope)
        if target_scope == Scope.USER:
            raise ValueError(
                "promotion to user scope is not supported by workspace-bound records"
            )
        if _SCOPE_RANK[target_scope] <= _SCOPE_RANK[old.scope]:
            raise ValueError(
                f"promotion must widen scope beyond '{old.scope.value}' "
                f"(got '{target_scope.value}')"
            )
        target_repo_id = old.repo_id if target_scope == Scope.REPO else None
        if target_scope == Scope.REPO and not target_repo_id:
            raise ValueError("cannot promote to repo scope: source has no repo")

        metadata = dict(old.metadata)
        raw_promoted_from = metadata.get("promoted_from")
        promoted_from = list(raw_promoted_from) if isinstance(raw_promoted_from, list) else []
        if old.id not in promoted_from:
            promoted_from.append(old.id)
        metadata["promoted_from"] = promoted_from
        metadata["promotion"] = {
            "from_scope": old.scope.value,
            "to_scope": target_scope.value,
            "reason": reason[:500],
        }
        metadata["provenance"] = (
            dict(old.provenance)
            if provenance_is_approved(old.provenance)
            else {
                "source": str((old.provenance or {}).get("source") or "legacy_unverified"),
                "trusted": False,
                "review_state": REVIEW_PENDING,
                "trust_origin": "derived_unapproved",
            }
        )

        def finalize_promotion(promoted_id: str) -> None:
            promoted = self.store.get_memory(promoted_id)
            if promoted is None:
                raise RuntimeError("promotion target was not stored")
            # Unknown labels fail closed by outranking every known sensitivity.
            sensitivity = max(
                (old.sensitivity, promoted.sensitivity),
                key=lambda value: _SENSITIVITY_RANK.get(
                    value, len(_SENSITIVITY_RANK)
                ),
            )
            promoted_metadata = dict(promoted.metadata)
            inherited_from = promoted_metadata.get("promoted_from")
            inherited_from = (
                list(inherited_from) if isinstance(inherited_from, list) else []
            )
            old_chain = old.metadata.get("promoted_from")
            for source_id in [
                *(old_chain if isinstance(old_chain, list) else []),
                old.id,
            ]:
                if source_id not in inherited_from:
                    inherited_from.append(source_id)
            promoted_metadata["promoted_from"] = inherited_from
            promoted_metadata["promotion"] = {
                "from_scope": old.scope.value,
                "to_scope": target_scope.value,
                "reason": reason[:500],
            }
            promoted_provenance = dict(promoted.provenance)
            trusted = all(
                provenance_is_approved(record.provenance)
                for record in (old, promoted)
            )
            if not trusted:
                promoted_provenance["trusted"] = False
                promoted_provenance["review_state"] = REVIEW_PENDING
            promoted_metadata["provenance"] = promoted_provenance
            self.store.advance_memory_modified_hlc(promoted_id, commit=False)
            self.store.conn.execute(
                "UPDATE memories SET pinned=?, sensitivity=?, confidence=?, stability=?, "
                "access_count=?, last_access=?, metadata=?, provenance=? WHERE id=?",
                (
                    int(old.pinned or promoted.pinned),
                    sensitivity,
                    min(old.confidence, promoted.confidence),
                    max(old.stability, promoted.stability),
                    max(old.access_count, promoted.access_count),
                    max(old.last_access or 0.0, promoted.last_access or 0.0) or None,
                    json.dumps(
                        promoted_metadata, ensure_ascii=False, separators=(",", ":")
                    ),
                    json.dumps(
                        promoted_provenance, ensure_ascii=False, separators=(",", ":")
                    ),
                    promoted_id,
                ),
            )
            self.store.close_validity(
                old.id, at=now, actor=actor,
                reason=(
                    reason
                    or f"promoted from {old.scope.value} to {target_scope.value}"
                ),
            )
            if not self.store.has_link(promoted_id, old.id, relation="promotes"):
                self.store.add_link(
                    promoted_id, old.id, "promotes",
                    reason=reason or "scope promotion",
                    allow_scope_transition=True,
                )
            self.store.audit(
                actor, "promote", promoted_id,
                (
                    f"from {old.id} ({old.scope.value}->{target_scope.value}): "
                    f"{reason}"
                )[:1000],
            )

        result = self.remember_with_resolution(
            old.content,
            workspace_id=_required_memory_workspace_id(old),
            repo_id=target_repo_id,
            session_id=None,
            mtype=old.mtype,
            scope=target_scope,
            title=old.title,
            importance=old.importance,
            confidence=old.confidence,
            keywords=old.keywords,
            metadata=metadata,
            valid_from=old.valid_from,
            resolve_conflicts=True,
            subject_key=old.subject_key,
            claim_kind=old.claim_kind,
            # This copies a record already approved by the owner; it is not ingress.
            _approval_override=True,
            _transactional_finalizer=finalize_promotion,
        )
        promoted_id = result["id"]
        return {
            "id": promoted_id,
            "promoted_from": old.id,
            "from_scope": old.scope.value,
            "scope": target_scope.value,
            "op": result["op"],
            "reason": reason,
        }

    def merge(self, source_ids: list, merged_content: str, *,
              title: Optional[str] = None, mtype: Optional[MemoryType] = None,
              scope: Optional[Scope] = None, keywords: Optional[list] = None,
              reason: str = "", actor: str = "user") -> dict:
        """Merge live memories into one atomic, temporally bounded successor."""
        with self._write_lock:
            return self._merge_locked(
                source_ids, merged_content, title=title, mtype=mtype, scope=scope,
                keywords=keywords, reason=reason, actor=actor,
            )

    def _merge_locked(self, source_ids: list, merged_content: str, *,
                      title: Optional[str], mtype: Optional[MemoryType],
                      scope: Optional[Scope], keywords: Optional[list],
                      reason: str, actor: str) -> dict:
        ids: list[str] = []
        sources: list[MemoryRecord] = []
        seen: set[str] = set()
        effective_at = now_ts()
        for raw_id in source_ids:
            memory_id = str(raw_id)
            if memory_id in seen:
                continue
            seen.add(memory_id)
            record = self.store.get_memory(memory_id)
            if record is None:
                raise KeyError(f"no memory with id '{memory_id}'")
            ids.append(memory_id)
            sources.append(record)
        if len(sources) < 2:
            raise ValueError("merge needs at least two distinct source memories")
        if len({record.workspace_id for record in sources}) != 1:
            raise ValueError("cannot merge memories from different workspaces")

        primary = sources[0]
        repo_id = (
            primary.repo_id
            if len({record.repo_id for record in sources}) == 1
            else None
        )
        target_type = MemoryType(mtype or primary.mtype)
        target_scope = _writable_scope(scope or primary.scope, repo_id)
        if target_scope == Scope.WORKSPACE:
            repo_id = None
        target_session_id: Optional[str] = None
        if target_scope == Scope.SESSION:
            session_ids = {record.session_id for record in sources}
            if len(session_ids) != 1 or None in session_ids or "" in session_ids:
                raise ValueError(
                    "session-scoped merge requires sources from one session; "
                    "choose repo or workspace scope for a cross-session merge"
                )
            target_session_id = str(next(iter(session_ids)))
            session = self.store.get_session(target_session_id)
            if session is None or session.get("status") != "active":
                raise ValueError("session-scoped merge requires one active session")
            if (
                session.get("workspace_id") != primary.workspace_id
                or session.get("repo_id") != repo_id
            ):
                raise ValueError("merge session does not match source workspace/repo")

        importance = max([record.importance or 0.0 for record in sources] + [0.5])
        pinned_any = any(record.pinned for record in sources)
        sensitivity = max(
            (record.sensitivity or "normal" for record in sources),
            key=lambda value: _SENSITIVITY_RANK.get(
                value, len(_SENSITIVITY_RANK)
            ),
        )
        trusted = all(
            provenance_is_approved(record.provenance) for record in sources
        )
        if keywords is None:
            merged_keywords: list = []
            seen_keywords: set = set()
            for record in sources:
                for keyword in record.keywords or []:
                    if keyword in seen_keywords:
                        continue
                    seen_keywords.add(keyword)
                    merged_keywords.append(keyword)
            keywords = merged_keywords[:32]

        tokens_before = sum(
            estimate_tokens(f"{record.title} {record.content}")
            for record in sources
        )
        title_final = title if title is not None else (primary.title or "")
        source_set = set(ids)
        merge_key = hashlib.sha256(json.dumps(
            {
                "source_ids": sorted(source_set),
                "content": merged_content,
                "title": title_final,
                "mtype": target_type.value,
                "scope": target_scope.value,
                "workspace_id": primary.workspace_id,
                "repo_id": repo_id,
                "session_id": target_session_id,
                "keywords": list(keywords or []),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        merge_link_reason = f"merge-key:{merge_key}"
        merge_metadata = {
            "merge_key": merge_key,
            "supersedes": list(ids),
            "provenance": {
                "source": "merge",
                "trusted": trusted,
                "review_state": REVIEW_APPROVED if trusted else REVIEW_PENDING,
                "merges": list(ids),
            },
        }
        if any(
            metadata_is_quarantined(record.metadata)
            or bool((record.provenance or {}).get("quarantined"))
            for record in sources
        ):
            merge_metadata = apply_quarantine_metadata(
                merge_metadata,
                PoisoningDecision(True, reasons=("inherited_quarantine",)),
            )

        subject_keys = {record.subject_key for record in sources}
        claim_kinds = {record.claim_kind for record in sources}
        subject_key = next(iter(subject_keys)) if len(subject_keys) == 1 else ""
        claim_kind = next(iter(claim_kinds)) if len(claim_kinds) == 1 else ""
        confidence = min(record.confidence for record in sources)

        def merge_result(merged_id: str) -> dict:
            tokens_after = estimate_tokens(f"{title_final} {merged_content}")
            saved = max(0, tokens_before - tokens_after)
            return {
                "id": merged_id,
                "merged": list(ids),
                "count": len(ids),
                "sensitivity": sensitivity,
                "trusted": trusted,
                "pinned": pinned_any,
                "reason": reason,
                "compaction": {
                    "tokens_before": tokens_before,
                    "tokens_after": tokens_after,
                    "tokens_saved": saved,
                    "reduction_pct": (
                        round(100.0 * saved / tokens_before, 1)
                        if tokens_before else 0.0
                    ),
                    "units": len(ids),
                },
            }

        retry_links = self.store.conn.execute(
            "SELECT a, b FROM mem_links "
            "WHERE relation='merges' AND reason=? "
            "AND valid_to IS NULL AND expired_at IS NULL "
            "AND (a=? OR b=?) "
            "ORDER BY a, b LIMIT 2",
            (merge_link_reason, ids[0], ids[0]),
        ).fetchall()
        for link in retry_links:
            candidate_id = (
                str(link["b"]) if str(link["a"]) == ids[0] else str(link["a"])
            )
            candidate = self.store.get_memory(candidate_id)
            supersedes = (
                (candidate.metadata or {}).get("supersedes")
                if candidate is not None
                else None
            )
            if (
                candidate is not None
                and _governable_source(candidate, at=effective_at)
                and candidate.metadata.get("merge_key") == merge_key
                and isinstance(supersedes, list)
                and {str(source_id) for source_id in supersedes} == source_set
                and candidate.content == merged_content
                and candidate.title == title_final
                and candidate.mtype == target_type
                and candidate.scope == target_scope
                and candidate.workspace_id == primary.workspace_id
                and candidate.repo_id == repo_id
                and candidate.session_id == target_session_id
                and list(candidate.keywords or []) == list(keywords or [])
            ):
                return merge_result(candidate.id)

        for record in sources:
            if not _governable_source(record, at=effective_at):
                raise ValueError(
                    "only current or quarantined source memories can be merged"
                )

        def finalize_merge(merged_id: str) -> None:
            self.store.advance_memory_modified_hlc(merged_id, commit=False)
            self.store.conn.execute(
                "UPDATE memories SET pinned=?, sensitivity=?, stability=?, "
                "access_count=?, last_access=? WHERE id=?",
                (
                    int(pinned_any),
                    sensitivity,
                    max(record.stability for record in sources),
                    max(record.access_count for record in sources),
                    max(
                        (record.last_access or 0.0 for record in sources),
                        default=0.0,
                    ) or None,
                    merged_id,
                ),
            )
            for record in sources:
                self.store.close_validity(
                    record.id, at=effective_at, actor=actor,
                    reason=reason or "merged into a combined memory",
                )
                self.store.add_link(
                    merged_id,
                    record.id,
                    "merges",
                    allow_scope_transition=True,
                    reason=merge_link_reason,
                )
                self.store.audit(
                    actor, "merge", record.id, f"merged into {merged_id}"
                )
            self.store.audit(
                actor, "merge", merged_id,
                f"merged {len(ids)} memories: {', '.join(ids)}",
            )

        merged_id = self.remember(
            merged_content,
            workspace_id=_required_memory_workspace_id(primary),
            repo_id=repo_id,
            session_id=target_session_id,
            mtype=target_type,
            scope=target_scope,
            title=title_final,
            importance=importance,
            confidence=confidence,
            keywords=keywords,
            metadata=merge_metadata,
            valid_from=effective_at,
            resolve_conflicts=False,
            subject_key=subject_key,
            claim_kind=claim_kind,
            _transactional_finalizer=finalize_merge,
        )

        return merge_result(merged_id)

    # ── linking & events (A-MEM-style) ──────────────────────────────────────────
    def link(self, a: str, b: str, relation: str = "related", *, layer=None,
             reason: str = "") -> None:
        records = []
        for mid in (a, b):
            record = self.store.get_memory(mid)
            if record is None:
                raise KeyError(f"no memory with id '{mid}'")
            records.append(record)
        if not all(inspection_eligible(record.provenance, record.metadata)
                   for record in records):
            raise ValueError("quarantined memories cannot be linked")
        if not all(provenance_is_approved(record.provenance) for record in records):
            raise ValueError("links require explicitly approved memories")
        self.store.add_link(a, b, relation, layer=layer, reason=reason)

    def record_event(self, kind: str, content: str, *, workspace_id: str = "",
                     repo_id: str = "", session_id: str = "",
                     refs: Optional[list] = None) -> str:
        return self.store.append_event(kind=kind, content=content, workspace_id=workspace_id,
                                       repo_id=repo_id, session_id=session_id, refs=refs)



    # ── code-symbol graph (the flagship coding-agent wedge) ──────────────────────
    def index_repo(self, repo_id: str, root_path: str, *, languages: Optional[set] = None,
                   prefer: str = "auto", max_files: int = 5_000,
                   max_file_bytes: int = 2_000_000) -> dict:
        """Walk ``root_path`` and populate the code symbol graph: function/class/method
        definitions plus best-effort calls/imports edges (AST via tree-sitter when
        installed, a dependency-free regex fallback otherwise — see
        ``backends.codegraph``). Re-indexing is idempotent per file (old symbols/edges
        for a changed file are replaced, not accumulated).

        Trust note: this reads files from the local filesystem at ``root_path`` — the
        same trust boundary as any other local tool the agent has (AGENTS.md/SECURITY.md
        §"Network exposure"). ``max_files``/``max_file_bytes`` just bound resource use
        on an unexpectedly large tree, not a security sandbox.
        """
        indexer_factory = self._code_indexer_factory
        language_detector = self._code_language_detector
        source_iterator = self._code_source_iterator
        if not (
            callable(indexer_factory)
            and callable(language_detector)
            and callable(source_iterator)
        ):
            raise RuntimeError("code indexing backend is not configured")
        indexer = indexer_factory(prefer=prefer)
        # index_repo is an explicit local-filesystem capability: callers select a
        # repository in one of the approved local roots. Canonicalizing then checking
        # containment confines the capability to the operator's configured/default
        # filesystem boundary. Every walked child is re-contained below before it is read.
        # Normalize before the root-prefix check.  Keep this check at the filesystem
        # call site (rather than hiding it in a helper) so both human review and static
        # analysis can establish that every later path is constrained by this boundary.
        canonical_root = os.path.normcase(
            os.path.realpath(os.path.expanduser(os.fspath(root_path)))
        )
        # Retain a separator on the checked value.  This makes an approved root
        # itself and every child use the same normalized-prefix check (and avoids
        # accepting a sibling such as ``/work/repo-copy`` for ``/work/repo``).
        # It also keeps the checked value, rather than the untrusted input, as
        # the only path passed to filesystem APIs below.
        canonical_root_with_sep = canonical_root.rstrip(os.sep) + os.sep
        safe_root: Optional[str] = None
        for approved_root in _approved_local_index_roots():
            normalized_approved = os.path.normcase(os.path.realpath(approved_root))
            approved_prefix = normalized_approved.rstrip(os.sep) + os.sep
            if canonical_root_with_sep.startswith(approved_prefix):
                safe_root = canonical_root_with_sep
                break
        if safe_root is None:
            raise ValueError("repo root is outside approved local roots")
        root = Path(safe_root)
        if not root.exists():
            raise ValueError(f"repo root not found: {root_path}")
        if not root.is_dir():
            raise ValueError(f"repo root is not a directory: {root_path}")
        max_files = max(1, int(max_files))
        max_file_bytes = max(1, int(max_file_bytes))
        existing = {
            row["file"]: row
            for row in self.store.list_code_files(repo_id, languages=languages)
        }
        present: set[str] = set()
        lang_counts: dict[str, int] = defaultdict(int)
        files_scanned = files_indexed = files_unchanged = files_failed = files_skipped = 0
        symbols_indexed = edges_indexed = 0
        backend_name = type(indexer).__name__
        scan_complete = True
        try:
            for file_path in source_iterator(str(root)):
                lang = language_detector(file_path)
                if (
                    lang is None
                    or (languages and lang not in languages)
                    or not indexer.supports(lang)
                ):
                    continue
                if files_scanned >= max_files:
                    scan_complete = False
                    break
                candidate = Path(file_path)
                try:
                    # Resolve once, verify containment, then use this checked path for
                    # every filesystem operation.  The walker skips symlinks, and this
                    # closes the remaining defense-in-depth gap if it ever changes.
                    source_file = candidate.resolve(strict=True)
                    rel = source_file.relative_to(root).as_posix()
                except (OSError, ValueError):
                    files_failed += 1
                    continue
                files_scanned += 1
                lang_counts[lang] += 1
                # Presence and successful indexing are deliberately separate. A file
                # that still exists but is temporarily unreadable, oversized, or fails
                # parsing must not have its last known-good symbols deleted at the end
                # of an otherwise complete scan.
                present.add(rel)
                try:
                    stat = source_file.stat()
                    if stat.st_size > max_file_bytes:
                        files_skipped += 1
                        continue
                    raw = source_file.read_bytes()
                except OSError:
                    files_failed += 1
                    continue
                content_hash = hashlib.sha256(raw).hexdigest()
                previous = existing.get(rel)
                if previous and previous.get("content_hash") == content_hash:
                    files_unchanged += 1
                    continue
                content = raw.decode("utf-8", errors="replace")
                try:
                    fi = indexer.index_file(rel, content, lang)
                except Exception:
                    files_failed += 1
                    continue  # one bad file shouldn't abort the whole repo index
                self.store.clear_symbols_for_file(repo_id, rel, commit=False)
                for sym in fi.symbols:
                    self.store.upsert_symbol(
                        repo_id=repo_id, kind=sym.kind, name=sym.name, fqname=sym.fqname,
                        file=sym.file, span=sym.span, signature=sym.signature,
                        docstring=sym.docstring, lang=sym.lang,
                        exported=sym.exported, content_hash=sym.content_hash,
                        commit=False,
                    )
                    symbols_indexed += 1
                for edge in fi.edges:
                    self.store.add_code_edge(
                        repo_id=repo_id, src=edge.src, dst=edge.dst,
                        relation=edge.relation, file=edge.file, line=edge.line,
                        commit=False,
                    )
                    edges_indexed += 1
                self.store.upsert_code_file(
                    repo_id=repo_id, file=rel, lang=lang, content_hash=content_hash,
                    size_bytes=stat.st_size, mtime_ns=getattr(stat, "st_mtime_ns", 0),
                    backend=backend_name, commit=False,
                )
                files_indexed += 1
        except self._code_walk_limit_error:
            scan_complete = False

        removed = 0
        if scan_complete:
            for rel in sorted(set(existing) - present):
                self.store.remove_code_file(repo_id, rel, commit=False)
                removed += 1
        self.store.conn.commit()
        code_memory_links = self.rebuild_code_memory_links(repo_id=repo_id)

        primary_lang = max(lang_counts.items(), key=lambda item: item[1])[0] if lang_counts else ""
        self.store.update_repo_index(
            repo_id, root_path=str(root), primary_lang=primary_lang,
            settings={
                "code_graph_backend": backend_name,
                "code_graph_languages": sorted(lang_counts),
                "code_graph_last_report": {
                    "files_scanned": files_scanned,
                    "files_indexed": files_indexed,
                    "files_unchanged": files_unchanged,
                    "files_removed": removed,
                    "scan_complete": scan_complete,
                },
            },
        )
        return {
            "root_path": str(root),
            "files_scanned": files_scanned,
            "files_indexed": files_indexed,
            "files_unchanged": files_unchanged,
            "files_removed": removed,
            "files_failed": files_failed,
            "files_skipped": files_skipped,
            "symbols_indexed": symbols_indexed,
            "edges_indexed": edges_indexed,
            # Backward-compatible totals: callers that previously compared a second
            # idempotent run to the first still see stable symbol/edge counts.
            "symbols": self.store.count_symbols(repo_id),
            "edges": self.store.count_code_edges(repo_id),
            "languages": dict(sorted(lang_counts.items())),
            "backend": backend_name,
            "incremental": True,
            "scan_complete": scan_complete,
            "code_memory_links": code_memory_links,
        }

    def index_repo_incremental(
        self, repo_id: str, root_path: str, paths: list[str], *,
        languages: Optional[set] = None, prefer: str = "auto",
        max_file_bytes: int = 2_000_000,
    ) -> dict:
        """Re-index explicit paths under the same source policy as the full walk."""
        indexer_factory = self._code_indexer_factory
        language_detector = self._code_language_detector
        source_policy = self._code_source_policy
        if not (
            callable(indexer_factory)
            and callable(language_detector)
            and callable(source_policy)
        ):
            raise RuntimeError("code indexing backend is not configured")

        canonical_root = os.path.normcase(
            os.path.realpath(os.path.expanduser(os.fspath(root_path)))
        )
        canonical_root_with_sep = canonical_root.rstrip(os.sep) + os.sep
        safe_root: Optional[str] = None
        for approved_root in _approved_local_index_roots():
            normalized_approved = os.path.normcase(os.path.realpath(approved_root))
            approved_prefix = normalized_approved.rstrip(os.sep) + os.sep
            if canonical_root_with_sep.startswith(approved_prefix):
                safe_root = canonical_root_with_sep
                break
        if safe_root is None:
            raise ValueError("repo root is outside approved local roots")
        root = Path(safe_root)
        if not root.exists():
            raise ValueError(f"repo root not found: {root_path}")
        if not root.is_dir():
            raise ValueError(f"repo root is not a directory: {root_path}")

        indexer = indexer_factory(prefer=prefer)
        max_file_bytes = max(1, int(max_file_bytes))
        existing = {
            row["file"]: row
            for row in self.store.list_code_files(repo_id, languages=languages)
        }
        files_scanned = files_indexed = files_unchanged = 0
        files_removed = files_failed = files_skipped = 0
        symbols_indexed = edges_indexed = 0
        lang_counts: dict[str, int] = defaultdict(int)
        backend_name = type(indexer).__name__
        seen_relative: set[str] = set()

        for supplied_path in paths:
            # This predicate performs containment, symlink, excluded-directory, ignore
            # file, and supported-extension checks before this method stats or reads the
            # candidate. Missing eligible paths remain allowed so deletions can retire
            # their prior index rows.
            if not source_policy(str(root), os.fspath(supplied_path)):
                files_skipped += 1
                continue
            raw_candidate = os.fspath(supplied_path)
            if not os.path.isabs(raw_candidate):
                raw_candidate = os.path.join(str(root), raw_candidate)
            safe_candidate = Path(os.path.realpath(os.path.abspath(raw_candidate)))
            try:
                relative = safe_candidate.relative_to(root).as_posix()
            except ValueError:
                files_skipped += 1
                continue
            if relative in seen_relative:
                continue
            seen_relative.add(relative)
            files_scanned += 1
            if not safe_candidate.exists():
                if relative in existing:
                    self.store.remove_code_file(repo_id, relative, commit=False)
                    files_removed += 1
                continue
            if not safe_candidate.is_file():
                files_skipped += 1
                continue
            language = language_detector(str(safe_candidate))
            if (
                language is None
                or (languages and language not in languages)
                or not indexer.supports(language)
            ):
                files_skipped += 1
                continue
            lang_counts[language] += 1
            try:
                stat = safe_candidate.stat()
                if stat.st_size > max_file_bytes:
                    files_skipped += 1
                    continue
                raw = safe_candidate.read_bytes()
            except OSError:
                files_failed += 1
                continue
            content_hash = hashlib.sha256(raw).hexdigest()
            previous = existing.get(relative)
            if previous and previous.get("content_hash") == content_hash:
                files_unchanged += 1
                continue
            try:
                indexed = indexer.index_file(
                    relative, raw.decode("utf-8", errors="replace"), language
                )
            except Exception:
                files_failed += 1
                continue
            self.store.clear_symbols_for_file(repo_id, relative, commit=False)
            for symbol in indexed.symbols:
                self.store.upsert_symbol(
                    repo_id=repo_id, kind=symbol.kind, name=symbol.name,
                    fqname=symbol.fqname, file=symbol.file, span=symbol.span,
                    signature=symbol.signature, docstring=symbol.docstring,
                    lang=symbol.lang, exported=symbol.exported,
                    content_hash=symbol.content_hash, commit=False,
                )
                symbols_indexed += 1
            for edge in indexed.edges:
                self.store.add_code_edge(
                    repo_id=repo_id, src=edge.src, dst=edge.dst,
                    relation=edge.relation, file=edge.file, line=edge.line,
                    commit=False,
                )
                edges_indexed += 1
            self.store.upsert_code_file(
                repo_id=repo_id, file=relative, lang=language,
                content_hash=content_hash, size_bytes=stat.st_size,
                mtime_ns=getattr(stat, "st_mtime_ns", 0),
                backend=backend_name, commit=False,
            )
            files_indexed += 1

        self.store.conn.commit()
        code_memory_links = self.rebuild_code_memory_links(repo_id=repo_id)
        primary_lang = (
            max(lang_counts.items(), key=lambda item: item[1])[0]
            if lang_counts else ""
        )
        self.store.update_repo_index(
            repo_id, root_path=str(root), primary_lang=primary_lang,
            settings={
                "code_graph_backend": backend_name,
                "code_graph_languages": sorted(lang_counts),
                "code_graph_last_report": {
                    "files_scanned": files_scanned,
                    "files_indexed": files_indexed,
                    "files_unchanged": files_unchanged,
                    "files_removed": files_removed,
                    "incremental": True,
                },
            },
        )
        return {
            "root_path": str(root),
            "files_scanned": files_scanned,
            "files_indexed": files_indexed,
            "files_unchanged": files_unchanged,
            "files_removed": files_removed,
            "files_failed": files_failed,
            "files_skipped": files_skipped,
            "symbols_indexed": symbols_indexed,
            "edges_indexed": edges_indexed,
            "symbols": self.store.count_symbols(repo_id),
            "edges": self.store.count_code_edges(repo_id),
            "languages": dict(sorted(lang_counts.items())),
            "backend": backend_name,
            "incremental": True,
            "scan_complete": True,
            "code_memory_links": code_memory_links,
        }

    def search_code(self, query: str, *, repo_id: str, limit: int = 20,
                    flt: Optional[SearchFilter] = None) -> dict:
        """Symbol-graph + lexical code search — far cheaper than
        dumping files for structural questions, and (via ``called_by``) answers "what
        breaks if I change X" directly from the call graph."""
        self._validate_code_filter(repo_id, flt)
        symbols = self.store.search_symbols(repo_id, query, limit=limit, flt=flt)
        for s in symbols:
            s["called_by"] = self.store.get_symbol_callers(
                repo_id, s["name"], limit=10, flt=flt
            )
            s["linked_memories"] = self.store.memories_for_symbol(
                repo_id, s["id"], flt=flt, limit=10
            )
        return {"query": query, "symbols": symbols}

    def _code_matcher(self, repo_id: str) -> _CodeSymbolMatcher:
        """The repo's cached ``_CodeSymbolMatcher``, rebuilt when its symbols change.

        The fingerprint is one ``COUNT(*)/MAX(id)`` probe: symbol ids are ULIDs, so any
        insert moves ``MAX(id)`` and any delete moves the count. That keeps a symbol
        table written by some other path (``index_repo``, a migration, a test) from
        serving a stale matcher, while costing far less than re-materialising every
        symbol row on every repo-scoped ``remember()``.
        """
        row = self.store.conn.execute(
            "SELECT COUNT(*) AS n, MAX(id) AS newest FROM symbols WHERE repo_id=?",
            (repo_id,),
        ).fetchone()
        version = (int(row["n"]) if row else 0, row["newest"] if row else None)
        cached = self._code_matchers.get(repo_id)
        if cached is not None and cached[0] == version:
            return cached[1]
        matcher = _CodeSymbolMatcher(self.store.list_symbols(repo_id))
        self._code_matchers.pop(repo_id, None)
        while len(self._code_matchers) >= CODE_MATCHER_CACHE_SIZE:
            self._code_matchers.pop(next(iter(self._code_matchers)), None)
        self._code_matchers[repo_id] = (version, matcher)
        return matcher

    def _link_memory_to_code(self, memory_id: str, *, content: str,
                             repo_id: str, commit: bool = True,
                             symbols: Optional[list[dict]] = None,
                             matcher: Optional[_CodeSymbolMatcher] = None,
                             max_links: int = CODE_LINK_MAX_LINKS) -> int:
        """Persist deterministic bridges from one memory to symbols in its repo.

        Scoring is unchanged (fqname 1.0 > name 0.9 > token-subset 0.75, capped at
        ``max_links`` in symbol order); only the *search* changed — see
        ``_CodeSymbolMatcher`` for why the compiled alternation produces the same links.
        """
        if matcher is None:
            matcher = (self._code_matcher(repo_id) if symbols is None
                       else _CodeSymbolMatcher(symbols))
        symbols = matcher.symbols
        hay = str(content or "")
        hay_lower = hay.lower()
        hay_tokens = tokenize(hay)
        matched, positions = matcher.match(hay_lower, hay_tokens)
        linked = 0
        for position in positions:
            symbol = symbols[position]
            name = str(symbol.get("name") or "").strip()
            fqname = str(symbol.get("fqname") or "").strip()
            confidence = 0.0
            if fqname and len(fqname) >= 3 and fqname.lower() in matched:
                confidence = 1.0
            elif name.lower() in matched:
                confidence = 0.9
            else:
                name_tokens = tokenize(name)
                if name_tokens and name_tokens <= hay_tokens:
                    confidence = 0.75
            if confidence <= 0.0:
                continue
            self.store.link_memory_symbol(
                repo_id=repo_id, symbol_id=symbol["id"], memory_id=memory_id,
                relation="mentions", confidence=confidence, commit=False,
            )
            linked += 1
            if linked >= max_links:
                break
        if commit and linked:
            self.store.conn.commit()
        return linked

    def rebuild_code_memory_links(self, *, repo_id: str) -> int:
        """Rebuild every live repo-associated bridge using bounded keyset pages."""
        memory_filter = SearchFilter(repo_id=repo_id, include_ancestors=False)
        linked = 0
        after_memory_id = ""
        while True:
            page = self.store.list_memories_page(
                memory_filter, after_id=after_memory_id, limit=250,
            )
            if not page:
                break
            records = [
                record for record in page
                if prompt_eligible(record.provenance, record.metadata)
            ]
            if not records:
                after_memory_id = page[-1].id
                continue
            linked_per_memory = {record.id: 0 for record in records}
            symbol_cursor: Optional[tuple[str, str, str]] = None
            while True:
                symbols = self.store.list_symbols_page(
                    repo_id, after=symbol_cursor, limit=500,
                )
                if not symbols:
                    break
                matcher = _CodeSymbolMatcher(symbols)
                for record in records:
                    remaining = 200 - linked_per_memory[record.id]
                    if remaining <= 0:
                        continue
                    count = self._link_memory_to_code(
                        record.id,
                        content=f"{record.title}\n{record.content}",
                        repo_id=repo_id,
                        commit=False,
                        matcher=matcher,
                        max_links=remaining,
                    )
                    linked_per_memory[record.id] += count
                    linked += count
                last_symbol = symbols[-1]
                symbol_cursor = (
                    last_symbol["file"], last_symbol["fqname"], last_symbol["id"],
                )
            self.store.conn.commit()
            after_memory_id = page[-1].id
        self.store.prune_code_memory_links(repo_id)
        return linked

    def _load_bounded_code_graph(
        self, *, repo_id: str, flt: Optional[SearchFilter],
        capacity: int, include_memory: bool,
    ) -> dict:
        """Load one bounded graph whose indexed symbol nodes are stable symbol IDs."""
        capacity = _code_traversal_capacity(capacity)
        symbol_rows = self.store.list_symbols(
            repo_id, limit=capacity + 1, flt=flt,
        )
        edge_rows = self.store.list_code_edges(
            repo_id, limit=capacity + 1, flt=flt,
        )
        memory_rows = (
            self.store.list_code_memory_links(
                repo_id, flt=flt, limit=capacity + 1,
            )
            if include_memory
            else []
        )
        truncated_sources = {
            "symbols": len(symbol_rows) > capacity,
            "edges": len(edge_rows) > capacity,
            "memory_links": len(memory_rows) > capacity,
        }
        symbols = symbol_rows[:capacity]
        stored_edges = edge_rows[:capacity]
        memory_links = memory_rows[:capacity]

        exact: dict[str, list[str]] = defaultdict(list)
        folded: dict[str, list[str]] = defaultdict(list)
        node_meta: dict[str, dict] = {}
        for symbol in symbols:
            symbol_id = str(symbol.get("id") or "")
            if not symbol_id:
                continue
            node_meta[symbol_id] = {
                "kind": "code",
                "name": symbol.get("name") or "",
                "fqname": symbol.get("fqname") or "",
                "file": symbol.get("file") or "",
                "span": symbol.get("span") or "",
            }
            for value in {
                symbol_id,
                str(symbol.get("name") or ""),
                str(symbol.get("fqname") or ""),
            }:
                if not value:
                    continue
                exact[value].append(symbol_id)
                folded[value.casefold()].append(symbol_id)

        def endpoint_ids(value: object, *, edge_id: str, side: str) -> list[str]:
            raw = str(value or "").strip()
            matches = sorted(set(exact.get(raw) or folded.get(raw.casefold()) or []))
            if len(matches) == 1:
                return matches
            if len(matches) > 1:
                fallback = f"ambiguous:{edge_id}:{side}"
                node_meta[fallback] = {
                    "kind": "ambiguous_code",
                    "name": raw,
                    "fqname": raw,
                    "file": "",
                    "candidates": matches,
                }
                return [fallback]
            fallback = f"code:{raw}"
            node_meta.setdefault(
                fallback,
                {"kind": "code", "name": raw, "fqname": raw, "file": ""},
            )
            return [fallback]

        expanded_edges: list[dict] = []
        expansion_truncated = False
        for edge_index, edge in enumerate(stored_edges):
            edge_id = str(edge.get("id") or edge_index)
            for source_id in endpoint_ids(
                edge.get("src"), edge_id=edge_id, side="source",
            ):
                for target_id in endpoint_ids(
                    edge.get("dst"), edge_id=edge_id, side="target",
                ):
                    if len(expanded_edges) >= capacity:
                        expansion_truncated = True
                        break
                    expanded_edges.append({
                        **edge,
                        "source_id": source_id,
                        "target_id": target_id,
                    })
                if expansion_truncated:
                    break
            if expansion_truncated:
                break
        truncated_sources["expanded_edges"] = expansion_truncated

        adjacency: dict[str, list[tuple[str, dict, bool]]] = defaultdict(list)
        for edge in expanded_edges:
            source_id = edge["source_id"]
            target_id = edge["target_id"]
            adjacency[source_id].append((target_id, edge, True))
            adjacency[target_id].append((source_id, edge, False))
        for link in memory_links:
            memory_id = str(link.get("memory_id") or "")
            symbol_id = str(link.get("symbol_id") or "")
            if not memory_id or not symbol_id or symbol_id not in node_meta:
                continue
            node_meta[memory_id] = {
                "kind": "memory",
                "name": link.get("title") or memory_id,
                "fqname": "",
                "file": "",
            }
            bridge = {
                "source_id": memory_id,
                "target_id": symbol_id,
                "relation": "memory_mentions",
                "layer": "memory",
                "file": link.get("file") or "",
                "line": 0,
            }
            adjacency[memory_id].append((symbol_id, bridge, True))
            adjacency[symbol_id].append((memory_id, bridge, False))
        for node in adjacency:
            adjacency[node].sort(
                key=lambda item: (
                    item[0],
                    str(item[1].get("relation") or ""),
                    not item[2],
                )
            )
        return {
            "capacity": capacity,
            "truncated": any(truncated_sources.values()),
            "truncated_sources": truncated_sources,
            "symbols": symbols,
            "stored_edges": stored_edges,
            "expanded_edges": expanded_edges,
            "memory_links": memory_links,
            "adjacency": adjacency,
            "node_meta": node_meta,
        }

    def code_path(
        self, source: str, target: str, *, repo_id: str,
        max_depth: int = 8,
        capacity: int = CODE_TRAVERSAL_DEFAULT_CAPACITY,
        flt: Optional[SearchFilter] = None,
    ) -> dict:
        """Return one deterministic shortest path within a bounded stable-ID graph."""
        self._validate_code_filter(repo_id, flt)
        graph = self._load_bounded_code_graph(
            repo_id=repo_id,
            flt=flt,
            capacity=capacity,
            include_memory=True,
        )
        source_id, source_candidates = self._resolve_code_node(
            source, graph["symbols"], graph["adjacency"],
        )
        target_id, target_candidates = self._resolve_code_node(
            target, graph["symbols"], graph["adjacency"],
        )
        common = {
            "capacity": graph["capacity"],
            "truncated": graph["truncated"],
            "truncated_sources": graph["truncated_sources"],
        }
        if source_candidates or target_candidates:
            return {
                "found": False,
                "source": source,
                "target": target,
                "reason": "source or target is ambiguous",
                "ambiguous": {
                    "source": source_candidates,
                    "target": target_candidates,
                },
                "path": [],
                "edges": [],
                **common,
            }
        if not source_id or not target_id:
            return {
                "found": False,
                "source": source,
                "target": target,
                "reason": "source or target was not found in the bounded indexed graph",
                "path": [],
                "edges": [],
                **common,
            }
        try:
            max_depth = int(max_depth)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("max_depth must be an integer") from exc
        max_depth = max(1, min(32, max_depth))
        queue = deque([source_id])
        depth = {source_id: 0}
        parent: dict[str, tuple[str, dict, bool]] = {}
        while queue:
            current = queue.popleft()
            if current == target_id:
                break
            if depth[current] >= max_depth:
                continue
            for neighbor, edge, forward in graph["adjacency"].get(current, []):
                if neighbor in depth:
                    continue
                depth[neighbor] = depth[current] + 1
                parent[neighbor] = (current, edge, forward)
                queue.append(neighbor)
        if target_id not in depth:
            return {
                "found": False,
                "source": source_id,
                "target": target_id,
                "reason": f"no path within {max_depth} hops"
                + (" in the bounded graph" if graph["truncated"] else ""),
                "path": [],
                "edges": [],
                **common,
            }
        node_ids = [target_id]
        path_edges: list[dict] = []
        cursor = target_id
        while cursor != source_id:
            previous, edge, forward = parent[cursor]
            path_edges.append({
                "from": previous,
                "to": cursor,
                "relation": edge.get("relation") or "",
                "layer": edge.get("layer") or "entity",
                "direction": "forward" if forward else "reverse",
                "file": edge.get("file") or "",
                "line": edge.get("line") or 0,
            })
            node_ids.append(previous)
            cursor = previous
        node_ids.reverse()
        path_edges.reverse()
        return {
            "found": True,
            "source": source_id,
            "target": target_id,
            "hops": len(path_edges),
            "path": [
                {
                    "id": node_id,
                    **graph["node_meta"].get(
                        node_id,
                        {"kind": "code", "name": node_id, "fqname": "", "file": ""},
                    ),
                }
                for node_id in node_ids
            ],
            "edges": path_edges,
            **common,
        }

    @staticmethod
    def _resolve_code_node(
        query: str, symbols: list[dict], adjacency: dict,
    ) -> tuple[Optional[str], list[str]]:
        raw = str(query or "").strip()
        if not raw:
            return None, []
        symbol_ids = {str(symbol.get("id") or "") for symbol in symbols}
        if raw in symbol_ids or raw in adjacency:
            return raw, []
        fallback = f"code:{raw}"
        if fallback in adjacency:
            return fallback, []
        folded = raw.casefold()
        tiers = (
            [s for s in symbols if str(s.get("fqname") or "") == raw],
            [s for s in symbols if str(s.get("name") or "") == raw],
            [s for s in symbols if str(s.get("file") or "") == raw],
            [
                s for s in symbols
                if folded in {
                    str(s.get("fqname") or "").casefold(),
                    str(s.get("name") or "").casefold(),
                    str(s.get("file") or "").casefold(),
                }
            ],
            [
                s for s in symbols
                if folded in str(s.get("fqname") or "").casefold()
                or folded in str(s.get("name") or "").casefold()
                or folded in str(s.get("file") or "").casefold()
            ],
        )
        candidates = next((tier for tier in tiers if tier), [])
        candidate_ids = sorted({
            str(candidate.get("id") or "")
            for candidate in candidates
            if candidate.get("id")
        })
        if len(candidate_ids) == 1:
            return candidate_ids[0], []
        if candidate_ids:
            return None, candidate_ids
        return None, []

    def analyze_code_graph(
        self, *, repo_id: str,
        capacity: int = CODE_TRAVERSAL_DEFAULT_CAPACITY,
        flt: Optional[SearchFilter] = None,
    ) -> dict:
        """Analyze one explicitly bounded stable-ID code graph."""
        self._validate_code_filter(repo_id, flt)
        graph = self._load_bounded_code_graph(
            repo_id=repo_id,
            flt=flt,
            capacity=capacity,
            include_memory=False,
        )
        adjacency: dict[str, dict[str, float]] = defaultdict(dict)
        degree: dict[str, int] = defaultdict(int)
        for edge in graph["expanded_edges"]:
            source_id, target_id = edge["source_id"], edge["target_id"]
            weight = (
                1.5
                if edge.get("relation") in {"calls", "inherits", "implements"}
                else 1.0
            )
            adjacency[source_id][target_id] = (
                adjacency[source_id].get(target_id, 0.0) + weight
            )
            adjacency[target_id][source_id] = (
                adjacency[target_id].get(source_id, 0.0) + weight
            )
            degree[source_id] += 1
            degree[target_id] += 1
        labels = {node: node for node in adjacency}
        for _ in range(30):
            changed = False
            for node in sorted(adjacency):
                scores: dict[str, float] = defaultdict(float)
                for neighbor, weight in adjacency[node].items():
                    scores[labels[neighbor]] += weight
                if not scores:
                    continue
                best = min(scores, key=lambda label: (-scores[label], label))
                if best != labels[node]:
                    labels[node] = best
                    changed = True
            if not changed:
                break
        grouped: dict[str, list[str]] = defaultdict(list)
        for node, label in labels.items():
            grouped[label].append(node)
        communities = sorted(
            grouped.values(), key=lambda members: (-len(members), min(members))
        )
        node_community: dict[str, int] = {}
        summaries = []
        for community_id, members in enumerate(communities):
            for node in members:
                node_community[node] = community_id
            ranked = sorted(members, key=lambda node: (-degree[node], node))
            summaries.append({
                "id": community_id,
                "size": len(members),
                "top_nodes": [
                    {
                        "node": node,
                        "name": graph["node_meta"].get(node, {}).get("name") or node,
                        "file": graph["node_meta"].get(node, {}).get("file") or "",
                        "degree": degree[node],
                    }
                    for node in ranked[:8]
                ],
            })
        cross_file = []
        cross_degree: dict[str, int] = defaultdict(int)
        for edge in graph["expanded_edges"]:
            source_id, target_id = edge["source_id"], edge["target_id"]
            source_file = (
                graph["node_meta"].get(source_id, {}).get("file")
                or edge.get("file")
                or ""
            )
            target_file = graph["node_meta"].get(target_id, {}).get("file") or ""
            if not source_file or not target_file or source_file == target_file:
                continue
            cross_degree[source_id] += 1
            cross_degree[target_id] += 1
            cross_file.append({
                "src": source_id,
                "dst": target_id,
                "relation": edge.get("relation") or "",
                "src_file": source_file,
                "dst_file": target_file,
            })
        cross_file.sort(key=lambda item: (
            -(degree[item["src"]] + degree[item["dst"]]),
            item["src_file"],
            item["dst_file"],
            item["src"],
            item["dst"],
        ))
        threshold = max(
            5,
            sorted(degree.values())[max(0, int(len(degree) * 0.9) - 1)]
            if degree else 5,
        )
        hotspots = [
            {
                "node": node,
                "name": graph["node_meta"].get(node, {}).get("name") or node,
                "file": graph["node_meta"].get(node, {}).get("file") or "",
                "degree": count,
                "cross_file_degree": cross_degree.get(node, 0),
                "god_node": count >= threshold,
            }
            for node, count in sorted(
                degree.items(), key=lambda item: (-item[1], item[0])
            )[:20]
        ]
        return {
            "nodes": len(adjacency),
            "edges": len(graph["expanded_edges"]),
            "source_edge_rows": len(graph["stored_edges"]),
            "capacity": graph["capacity"],
            "truncated": graph["truncated"],
            "truncated_sources": graph["truncated_sources"],
            "algorithm": "weighted_label_propagation",
            "communities": summaries,
            "hotspots": hotspots,
            "surprising_connections": cross_file[:50],
            "_node_community": node_community,
        }

    def analyze_impact(
        self, changed_files: list[str], *, repo_id: str,
        capacity: int = CODE_TRAVERSAL_DEFAULT_CAPACITY,
        flt: Optional[SearchFilter] = None,
    ) -> dict:
        """Estimate impact from one explicitly bounded stable-ID graph."""
        self._validate_code_filter(repo_id, flt)
        capacity = _code_traversal_capacity(capacity)
        normalized: list[str] = []
        seen = set()
        files_truncated = False
        for index, file in enumerate(changed_files):
            if index >= capacity:
                files_truncated = True
                break
            relative = str(file or "").strip().replace("\\", "/")
            while relative.startswith("./"):
                relative = relative[2:]
            if relative.startswith("/"):
                relative = relative[1:]
            if relative and relative not in seen:
                seen.add(relative)
                normalized.append(relative)
        graph = self._load_bounded_code_graph(
            repo_id=repo_id,
            flt=flt,
            capacity=capacity,
            include_memory=True,
        )
        normalized_set = set(normalized)
        symbols = [
            symbol for symbol in graph["symbols"]
            if str(symbol.get("file") or "").replace("\\", "/") in normalized_set
        ]
        touched_ids = {str(symbol.get("id") or "") for symbol in symbols}
        inbound = [
            edge for edge in graph["expanded_edges"]
            if edge["target_id"] in touched_ids
        ]
        dependent_files = sorted({
            file
            for edge in inbound
            if isinstance((file := edge.get("file")), str)
            and file
            and file not in normalized_set
        })
        memory_mentions: dict[str, dict] = {}
        for link in graph["memory_links"]:
            if str(link.get("symbol_id") or "") not in touched_ids:
                continue
            item = memory_mentions.setdefault(
                link["memory_id"],
                {
                    "id": link["memory_id"],
                    "title": link.get("title") or "",
                    "mtype": link.get("mtype") or "",
                    "symbols": [],
                },
            )
            name = link.get("fqname") or link.get("name") or ""
            if name and name not in item["symbols"]:
                item["symbols"].append(name)
        mention_names = sorted({
            str(symbol.get("name"))
            for symbol in symbols
            if symbol.get("name") and len(str(symbol.get("name"))) >= 3
        })[:80]
        for name in mention_names:
            for row in self.store.memories_mentioning(
                repo_id, name, flt=flt, limit=10,
            ):
                item = memory_mentions.setdefault(
                    row["id"],
                    {
                        "id": row["id"],
                        "title": row["title"] or "",
                        "mtype": row["mtype"],
                        "symbols": [],
                    },
                )
                if name not in item["symbols"]:
                    item["symbols"].append(name)
        analysis = self.analyze_code_graph(
            repo_id=repo_id, capacity=capacity, flt=flt,
        )
        node_community = analysis.pop("_node_community")
        communities_affected = sorted({
            node_community[symbol_id]
            for symbol_id in touched_ids
            if symbol_id in node_community
        })
        score = min(
            100,
            len(normalized) * 5
            + len(symbols) * 2
            + len(inbound) * 3
            + len(memory_mentions) * 2
            + len(communities_affected) * 5,
        )
        level = (
            "low" if score < 25
            else "medium" if score < 55
            else "high" if score < 80
            else "critical"
        )
        hotspot_ids = {item["node"] for item in analysis["hotspots"][:10]}
        truncated_sources = dict(graph["truncated_sources"])
        truncated_sources["changed_files"] = files_truncated
        return {
            "capacity": capacity,
            "truncated": graph["truncated"] or files_truncated,
            "truncated_sources": truncated_sources,
            "changed_files": normalized,
            "risk": {"score": score, "level": level},
            "metrics": {
                "files_touched": len(normalized),
                "symbols_touched": len(symbols),
                "inbound_edges": len(inbound),
                "dependent_files": len(dependent_files),
                "memory_mentions": len(memory_mentions),
                "communities_affected": len(communities_affected),
            },
            "symbols": symbols[:200],
            "inbound": inbound[:200],
            "dependent_files": dependent_files[:200],
            "memory_mentions": list(memory_mentions.values())[:100],
            "communities_affected": communities_affected,
            "potential_conflict_zones": sorted(touched_ids & hotspot_ids),
            "graph": analysis,
        }

    def export_code_graph(self, *, repo_id: str,
                          limit: int = CODE_EXPORT_DEFAULT_LIMIT,
                          flt: Optional[SearchFilter] = None) -> dict:
        """Portable graph.json payload for external tooling.

        Bounded like its sibling ``MemoryService.graph()``, and for the same reason: the
        export is reachable at the lowest (``viewer``) role through three surfaces
        (``engraphis_export_code_graph``, ``GET /code/export``, ``GET /api/code/export``)
        and the payload is re-serialized twice more by ``code_graph_report``/
        ``code_graph_html`` — so an indexed monorepo let the least-privileged caller pull
        (and make the server build) an unbounded response. ``limit`` caps files and
        symbols; edges and memory links get the same ``limit * 8`` headroom ``graph()``
        gives entity edges. ``payload['truncated']`` says whether a cap actually bit.
        """
        limit = max(1, min(CODE_EXPORT_MAX_LIMIT, int(limit)))
        edge_cap = min(
            CODE_TRAVERSAL_MAX_CAPACITY,
            max(limit * 8, 2_000),
        )
        self._validate_code_filter(repo_id, flt)
        analysis = self.analyze_code_graph(
            repo_id=repo_id, capacity=edge_cap, flt=flt,
        )
        analysis.pop("_node_community", None)
        files = self.store.list_code_files(repo_id, flt=flt, limit=limit + 1)
        nodes = self.store.list_symbols(repo_id, limit=limit + 1, flt=flt)
        edges = self.store.list_code_edges(repo_id, limit=edge_cap + 1, flt=flt)
        memory_links = self.store.list_code_memory_links(
            repo_id, flt=flt, limit=edge_cap + 1,
        )
        truncated = bool(
            len(files) > limit
            or len(nodes) > limit
            or len(edges) > edge_cap
            or len(memory_links) > edge_cap
            or analysis.get("truncated")
        )
        return {
            "format": "engraphis-code-graph/1",
            "generated_at": time.time(),
            "repo_id": repo_id,
            "limit": limit,
            "edge_limit": edge_cap,
            "truncated": truncated,
            "files": files[:limit],
            "nodes": nodes[:limit],
            "edges": edges[:edge_cap],
            "memory_links": memory_links[:edge_cap],
            "analysis": analysis,
        }

    def _validate_code_filter(
        self, repo_id: str, flt: Optional[SearchFilter]
    ) -> None:
        """Reject inconsistent repo/workspace filters before any code row is read.

        Code-history tables are keyed by ``repo_id`` rather than duplicating a
        workspace column. Without this check, a direct engine caller could pair a
        workspace-A filter with a workspace-B repo id and receive B's symbols even
        though memory reads correctly returned nothing.
        """
        if flt is None:
            return
        if flt.repo_id is not None and flt.repo_id != repo_id:
            raise ValueError("code filter repo_id does not match the requested repo")
        if flt.workspace_id is None:
            return
        row = self.store.conn.execute(
            "SELECT workspace_id FROM repos WHERE id=?", (repo_id,)
        ).fetchone()
        if row is None or row["workspace_id"] != flt.workspace_id:
            raise ValueError("code filter workspace_id does not own the requested repo")

    def code_graph_report(self, *, repo_id: str, payload: Optional[dict] = None,
                          flt: Optional[SearchFilter] = None) -> str:
        """Human-readable GRAPH_REPORT.md companion to :meth:`export_code_graph`.

        Rendering lives in :mod:`engraphis.core.codegraph_export` (pure function of the
        payload) so the engine facade stays thin."""
        from engraphis.core.codegraph_export import render_report
        return render_report(payload or self.export_code_graph(repo_id=repo_id, flt=flt))

    def code_graph_html(self, *, repo_id: str, payload: Optional[dict] = None,
                        flt: Optional[SearchFilter] = None) -> str:
        """Self-contained, dependency-free graph.html export (see
        :mod:`engraphis.core.codegraph_export`)."""
        from engraphis.core.codegraph_export import render_html
        return render_html(payload or self.export_code_graph(repo_id=repo_id, flt=flt))

    # ── session passthrough (convenience) ──────────────────────────────────────
    def start_session(self, workspace_id: str, repo_id: Optional[str] = None, **kw) -> str:
        return self.store.start_session(workspace_id, repo_id, **kw)

    def end_session(self, session_id: str, **kw) -> None:
        self.store.end_session(session_id, **kw)
