"""MemoryEngine — the high-level facade the API/MCP layer calls.

Wires together store + embedder + vector index + reranker + recall engine, and exposes
everything an agent does against memory: write (``remember``, with deterministic conflict
resolution), read (``recall``, ``why``, ``timeline``, ``recall_proactive``), governance
(``forget``, ``pin``, ``correct``), session lifecycle (with cross-session handoff), and the
A-MEM-style linking/event primitives (``link``, ``record_event``). Construct with
``MemoryEngine.create(...)`` for sensible, offline-capable defaults, or inject your own
backends for production.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Optional

import numpy as np

from engraphis.backends.embedder_st import get_embedder
from engraphis.backends.reranker import IdentityReranker, get_reranker
from engraphis.backends.vector_sqlitevec import get_vector_index
from engraphis.core import scoring
from engraphis.core.interfaces import (
    MemoryRecord,
    MemoryType,
    RetentionDecision,
    Scope,
    SearchFilter,
)
from engraphis.core.recall import RecallEngine, RecallResult
from engraphis.core.resolve import RELATED_SIM_FLOOR, ResolutionOp, resolve
from engraphis.core.store import Store, now_ts
from engraphis.core.textutil import estimate_tokens, jaccard, tokenize

logger = logging.getLogger("engraphis.core.engine")

# Sensitivity lattice: a merge keeps the *most restrictive* label of its sources, so
# secret/sensitive content can never be laundered into a lower-sensitivity merged fact.
_SENSITIVITY_RANK = {"normal": 0, "sensitive": 1, "secret": 2}
_SCOPE_RANK = {
    Scope.SESSION: 0,
    Scope.REPO: 1,
    Scope.WORKSPACE: 2,
    Scope.USER: 3,
}

# A-MEM-style evolution: how many related neighbors a new memory auto-links to on write.
# Bounded so hub memories don't accrete unbounded link lists (link quality > quantity).
EVOLVE_MAX_LINKS = 3

# Metadata keys that feed the entity/edge graph under the *trusted*
# provenance.source="structured_extractor" label — i.e. "a configured Extractor produced
# this". See _has_structured_graph_metadata / _trusted_graph_hints.
GRAPH_HINT_KEYS = ("entities", "relations", "structured_extraction")

# code↔memory linking (see _CodeSymbolMatcher / _link_memory_to_code)
CODE_LINK_MAX_LINKS = 200      # per-memory fan-out cap (unchanged behaviour)
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
CODE_EXPORT_MAX_LIMIT = 20_000


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
    untrusted = [k for k in GRAPH_HINT_KEYS if k in metadata and k not in vouched]
    if not untrusted:
        return metadata
    out = {k: v for k, v in metadata.items() if k not in untrusted}
    existing = out.get("client_supplied_graph")
    hints = dict(existing) if isinstance(existing, dict) else {}
    hints.update({k: metadata[k] for k in untrusted})
    hints["source"] = "client_supplied"
    out["client_supplied_graph"] = hints
    return out


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
    def __init__(self, store: Store, embedder, vector_index, reranker=None,
                 *, auto_evolve: bool = True, extractor=None,
                 graph_extractor=None, retention_supervisor=None) -> None:
        self.store = store
        self.embedder = embedder
        self.index = vector_index
        self.reranker = reranker or IdentityReranker()
        self.recall_engine = RecallEngine(store, embedder, vector_index, self.reranker)
        # Memory evolution (A-MEM-style): writing a new note also updates
        # how its neighbors are connected, so the network improves bidirectionally.
        self.auto_evolve = auto_evolve
        # Optional fact extractor (core.interfaces.Extractor). None = raw passthrough.
        self.extractor = extractor
        # Optional graph extractor (backends.graph_extractor). None = no graph population.
        self.graph_extractor = graph_extractor
        self.retention_supervisor = retention_supervisor
        # Serializes the resolve→insert critical section of the write path (see
        # remember_with_resolution). RLock: ingest()/import paths may nest writes.
        self._write_lock = threading.RLock()
        # Depth of the engine's own trusted producers on THIS thread (consolidate()).
        # Thread-local on purpose: a sweep must never vouch for another thread's
        # concurrent caller-driven write. See _resolve_and_store.
        self._internal_writes = threading.local()
        # repo_id -> (symbol-set fingerprint, _CodeSymbolMatcher). Bounded; see
        # _code_matcher for the invalidation contract.
        self._code_matchers: dict = {}

    @classmethod
    def create(cls, db_path: str = ":memory:", *, embed_model: Optional[str] = None,
               embed_dim: int = 384, vector_backend: str = "auto",
               rerank_model: Optional[str] = None, extractor: str = "none",
               graph_extractor: str = "none",
               retention_supervisor: str = "none",
               auto_evolve: bool = True, connect=None) -> "MemoryEngine":
        from engraphis.backends.extractor import PassthroughExtractor, get_extractor
        from engraphis.backends.graph_extractor import get_graph_extractor as _get_ge
        from engraphis.backends.retention import get_retention_supervisor
        store = Store(db_path, connect=connect)
        embedder = get_embedder(embed_model, embed_dim)
        index = get_vector_index(store, dim=embedder.dim, prefer=vector_backend)
        reranker = get_reranker(rerank_model)
        ext = get_extractor(extractor)
        if isinstance(ext, PassthroughExtractor):
            ext = None                       # ingest() treats None as passthrough
        ge = _get_ge(graph_extractor) if graph_extractor and graph_extractor != "none" else None
        supervisor = get_retention_supervisor(retention_supervisor)
        return cls(store, embedder, index, reranker, auto_evolve=auto_evolve,
                   extractor=ext, graph_extractor=ge,
                   retention_supervisor=supervisor)

    # ── write ─────────────────────────────────────────────────────────────────
    def remember(self, content: str, *, workspace_id: str, repo_id: Optional[str] = None,
                 session_id: Optional[str] = None, mtype: MemoryType = MemoryType.SEMANTIC,
                 scope: Optional[Scope] = None, title: str = "", importance: float = 0.0,
                 keywords: Optional[list] = None, metadata: Optional[dict] = None,
                 valid_from: Optional[float] = None, resolve_conflicts: bool = True,
                 candidate_k: int = 5,
                 _trusted_graph_keys: Optional[frozenset] = None) -> str:
        """Store one memory. Returns the id of the *live* record: a new id for ADD/
        INVALIDATE, or the existing memory's id if this was resolved as a NOOP
        (near-duplicate). See ``remember_with_resolution`` for the full decision detail.
        """
        return self.remember_with_resolution(
            content, workspace_id=workspace_id, repo_id=repo_id, session_id=session_id,
            mtype=mtype, scope=scope, title=title, importance=importance, keywords=keywords,
            metadata=metadata, valid_from=valid_from, resolve_conflicts=resolve_conflicts,
            candidate_k=candidate_k, _trusted_graph_keys=_trusted_graph_keys,
        )["id"]

    def remember_with_resolution(self, content: str, *, workspace_id: str,
                 repo_id: Optional[str] = None, session_id: Optional[str] = None,
                 mtype: MemoryType = MemoryType.SEMANTIC, scope: Optional[Scope] = None,
                 title: str = "", importance: float = 0.0, keywords: Optional[list] = None,
                 metadata: Optional[dict] = None, valid_from: Optional[float] = None,
                 resolve_conflicts: bool = True, candidate_k: int = 5,
                 _trusted_graph_keys: Optional[frozenset] = None) -> dict:
        """Store one memory with deterministic conflict resolution.

        Returns ``{"id", "op", ...}`` where ``op`` is one of:

        * ``"add"``        — genuinely new; inserted.
        * ``"noop"``        — a near-duplicate of an existing memory; that memory was
          reinforced instead of inserting a copy. ``id`` is the *existing* memory's id.
        * ``"invalidate"``  — same subject as an existing memory but new content; the old
          one's validity was closed (never deleted) and this was inserted. ``superseded``
          lists the closed id(s).
        """
        scope_was_omitted = scope is None
        scope = (
            Scope.REPO if (repo_id or session_id) else Scope.WORKSPACE
        ) if scope is None else Scope(scope)
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
        text = f"{title}\n{content}" if title else content
        # Embedding is the expensive, thread-safe part — compute it BEFORE taking the
        # write lock so concurrent writers only serialize the fast resolve+insert step.
        vec = self.embedder.embed([text])[0]

        # One writer at a time from neighbor-lookup through insert/invalidate: without
        # this, two concurrent near-duplicate remembers can BOTH observe "no neighbor"
        # and both resolve ADD — duplicating instead of NOOP/INVALIDATE — because the
        # store's per-statement serialization cannot span this read-decide-write
        # sequence. Same single-process posture as the rest of the engine (the store is
        # one shared connection); multi-process writers are out of scope by design.
        with self._write_lock:
            return self._resolve_and_store(
                content, text=text, vec=vec, workspace_id=workspace_id, repo_id=repo_id,
                session_id=session_id, mtype=mtype, scope=scope, title=title,
                importance=importance, keywords=keywords, metadata=metadata,
                valid_from=valid_from, resolve_conflicts=resolve_conflicts,
                candidate_k=candidate_k, trusted_graph_keys=_trusted_graph_keys,
            )

    def _resolve_and_store(self, content: str, *, text: str, vec: np.ndarray,
                           workspace_id: str, repo_id: Optional[str],
                           session_id: Optional[str], mtype: MemoryType, scope: Scope,
                           title: str, importance: float, keywords: Optional[list],
                           metadata: Optional[dict], valid_from: Optional[float],
                           resolve_conflicts: bool, candidate_k: int,
                           trusted_graph_keys: Optional[frozenset] = None) -> dict:
        """The resolve→insert body of ``remember_with_resolution``. The caller holds
        ``self._write_lock`` for the whole call (atomicity of the resolve decision).

        ``trusted_graph_keys`` names the ``GRAPH_HINT_KEYS`` this write's ``metadata``
        genuinely inherited from an ``Extractor``; everything else is treated as
        caller-supplied — see ``_rehome_untrusted_graph_hints``."""
        decision, neighbors = None, []
        if resolve_conflicts:
            decision, neighbors = self._resolve_against_neighbors(
                text, vec, workspace_id=workspace_id, repo_id=repo_id,
                session_id=session_id, scope=scope, mtype=mtype,
                candidate_k=candidate_k,
            )

        if decision is not None and decision.op == ResolutionOp.NOOP:
            self.store.reinforce(decision.target_id, boost=scoring.INTERACTION_BOOST["create"])
            self.store.audit("resolver", "noop", decision.target_id, decision.reason)
            return {"id": decision.target_id, "op": "noop", "reason": decision.reason}

        # Before anything reads it: demote graph hints this write cannot prove came from
        # an Extractor, so the "structured_extractor" feed below can only ever see
        # genuine extractor output (defense in depth for direct-engine callers that never
        # pass through service.py::_clean_metadata). A consolidation sweep on this thread
        # is one of the engine's own producers and vouches for all of them.
        if trusted_graph_keys is None and getattr(self._internal_writes, "depth", 0):
            trusted_graph_keys = frozenset(GRAPH_HINT_KEYS)
        meta = _rehome_untrusted_graph_hints(dict(metadata or {}), trusted_graph_keys)
        if decision is not None and decision.op == ResolutionOp.INVALIDATE:
            # Persist the supersession pointer on the new record so the chain is
            # queryable later (why/timeline/inspector), not only in the audit log.
            meta["supersedes"] = [decision.target_id]

        importance, stability, retention_signal = self._retention_signal(
            content, title=title, mtype=mtype, metadata=meta, importance=importance,
        )
        if retention_signal:
            meta["retention_supervision"] = retention_signal

        rec = MemoryRecord(
            id="", content=content, mtype=mtype, scope=scope, workspace_id=workspace_id,
            repo_id=repo_id, session_id=session_id, title=title, importance=importance,
            stability=stability,
            keywords=keywords or [], metadata=meta, valid_from=valid_from,
            # Lift provenance into its dedicated field/column so recall/why/timeline
            # surface it (copied, not popped: consolidate.py still reads
            # metadata["provenance"]).
            provenance=dict(meta.get("provenance") or {}),
            embedding=vec,
        )
        mid = self.store.add_memory(rec)
        if retention_signal:
            self.store.audit(
                retention_signal.get("source", "retention"),
                "retention_supervised",
                mid,
                f"{retention_signal.get('label', 'normal')}: "
                f"{retention_signal.get('reason', '')}"[:1000],
            )
        try:
            self.index.upsert([mid], vec.reshape(1, -1))
        except Exception as exc:  # noqa: BLE001 — a failed index write must not lose the memory
            # …but it must not be silent either: without the vector row this memory is
            # invisible to the semantic-recall arm until re-indexed, so leave a trace in
            # both the log and the audit trail (best-effort — never fail the write twice).
            logger.warning("vector-index upsert failed for %s (%s)",
                           mid, type(exc).__name__)
            try:
                self.store.audit(
                    "engine", "index_upsert_failed", mid,
                    "failure_type=%s" % type(exc).__name__)
            except Exception:  # noqa: BLE001
                pass
        if repo_id:
            self._link_memory_to_code(mid, content=f"{title}\n{content}", repo_id=repo_id)

        # Optional graph population (backends.graph_extractor). Structured fact metadata
        # from llm_structured is already validated before storage, so feed it directly
        # into the graph even when the regex graph extractor is disabled; then run the
        # configured text extractor too (idempotent via feed/store de-duping).
        # ``meta`` was demoted above, so any hint still under a GRAPH_HINT_KEYS name here
        # was vouched for by ingest() — the "structured_extractor" label below is earned,
        # not merely asserted by whoever built the metadata dict.
        if self._has_structured_graph_metadata(meta):
            try:
                from engraphis.backends.graph_extractor import (
                    StructuredMetadataGraphExtractor, feed as _graph_feed,
                )
                _graph_feed(self.store, content, workspace_id=workspace_id,
                            repo_id=repo_id, title=title,
                            extractor=StructuredMetadataGraphExtractor(meta),
                            provenance={"source": "structured_extractor", "memory_id": mid})
            except Exception:
                pass
        if self.graph_extractor is not None:
            try:
                from engraphis.backends.graph_extractor import feed as _graph_feed
                _graph_feed(self.store, content, workspace_id=workspace_id,
                            repo_id=repo_id, title=title, extractor=self.graph_extractor,
                            provenance={"source": "graph_extractor", "memory_id": mid})
            except Exception:
                pass

        if decision is not None and decision.op == ResolutionOp.INVALIDATE:
            self.store.close_validity(decision.target_id, reason=decision.reason)
            try:
                self.index.delete([decision.target_id])
            except Exception as exc:  # noqa: BLE001 — merely stale in the index; recall
                # re-checks validity on read, so log (don't audit) and continue.
                logger.warning("vector-index delete failed for %s: %s",
                               decision.target_id, exc)
            self.store.audit("resolver", "invalidate", decision.target_id, decision.reason)
            linked = self._evolve(mid, neighbors, exclude={decision.target_id})
            out = {"id": mid, "op": "invalidate", "superseded": [decision.target_id],
                   "reason": decision.reason}
            if linked:
                out["linked"] = linked
            return out

        linked = self._evolve(mid, neighbors)
        out = {"id": mid, "op": "add", "reason": decision.reason if decision else ""}
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
        if not decision.retain:
            label = "ephemeral"
        preset_stability = {"ephemeral": 0.25, "normal": 1.0, "critical": 8.0}[label]
        preset_importance = {"ephemeral": 0.1, "normal": 0.5, "critical": 0.9}[label]
        if decision.importance is not None:
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
            decision.stability if decision.stability is not None else preset_stability,
            default=preset_stability, minimum=0.05, maximum=100.0,
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
        if isinstance(metadata.get("entities"), list) or isinstance(metadata.get("relations"), list):
            return True
        structured = metadata.get("structured_extraction")
        return isinstance(structured, dict) and (
            isinstance(structured.get("entities"), list)
            or isinstance(structured.get("relations"), list)
        )

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
                if sim < RELATED_SIM_FLOOR or nrec.id in exclude or nrec.id == new_id:
                    continue
                if self.store.has_link(new_id, nrec.id):
                    continue
                self.store.add_link(new_id, nrec.id, "related")
                self.store.reinforce(nrec.id, boost=scoring.INTERACTION_BOOST["view"])
                linked.append(nrec.id)
            if linked:
                self.store.audit("resolver", "evolve", new_id,
                                 f"auto-linked to {len(linked)} related: {', '.join(linked)}")
        except Exception:
            return linked
        return linked

    def _resolve_against_neighbors(self, text: str, vec: np.ndarray, *, workspace_id: str,
                                   repo_id: Optional[str], session_id: Optional[str],
                                   scope: Scope, mtype: MemoryType, candidate_k: int):
        """Fetch same-scope neighbors via the vector index and run the deterministic
        resolver (``core.resolve``). Returns ``(decision, neighbors)`` so the caller can
        also evolve the neighborhood. Never raises — a broken/missing index degrades to
        "no neighbors found" (ADD), not a write failure."""
        flt = SearchFilter(
            workspace_id=workspace_id, repo_id=repo_id,
            session_id=session_id if scope == Scope.SESSION else None,
            scopes=[scope], mtypes=[mtype],
        )
        try:
            hits = self.index.search(vec, candidate_k, filter=flt)
        except Exception:
            return None, []
        now = now_ts()
        neighbors = []
        for nid, sim in hits:
            nrec = self.store.get_memory(nid)
            if (nrec and nrec.workspace_id == workspace_id and nrec.repo_id == repo_id
                    and nrec.scope == scope and nrec.mtype == mtype
                    and (scope != Scope.SESSION or nrec.session_id == session_id)
                    and nrec.expired_at is None
                    and (nrec.valid_to is None or nrec.valid_to > now)):
                neighbors.append((sim, nrec))
        return resolve(text, neighbors), neighbors

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
        facts = None
        extracted = False
        if self.extractor is not None:
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
        for f in facts:
            fact_own = getattr(f, "metadata", {}) or {}
            # This is the one place that can tell the two apart: ``fact_own`` is computed
            # fresh from the Extractor's real output, while ``base_metadata`` is the
            # caller's argument. The extractor's keys win the merge, so vouching by name
            # is exact — and a hint key present only in ``base_metadata`` stays untrusted
            # even though it shares a name with one the extractor could have produced.
            trusted = frozenset(k for k in GRAPH_HINT_KEYS if k in fact_own)
            results.append(self.remember_with_resolution(
                f.content, workspace_id=workspace_id, repo_id=repo_id,
                session_id=session_id, mtype=f.mtype or default_mtype, scope=scope,
                title=f.title, importance=f.importance, keywords=f.keywords,
                metadata={**base_metadata, **fact_own},
                resolve_conflicts=resolve_conflicts, _trusted_graph_keys=trusted,
            ))
        return {"facts": results, "count": len(results), "extracted": extracted}

    # ── consolidation: the sleep-time loop, callable on demand (Phase 4) ───────
    def consolidate(self, *, workspace_id: str, repo_id: Optional[str] = None,
                    dry_run: bool = False, llm=None, **kw) -> dict:
        """One sleep-time consolidation sweep — episodic→semantic distillation plus
        decayed-transient archival. See ``core.consolidate.consolidate`` for knobs.

        Marks the sweep as an engine-internal producer for its duration, so the structured
        digests it writes keep their graph hints (they are distilled from this device's
        own memories by the operator's configured LLM, exactly like an ``Extractor``'s
        output — not caller-supplied metadata). Every production entry point goes through
        here; see ``_rehome_untrusted_graph_hints`` for why this cannot be signalled
        in-band through the metadata dict.
        """
        from engraphis.core.consolidate import consolidate as _consolidate
        depth = getattr(self._internal_writes, "depth", 0)
        self._internal_writes.depth = depth + 1
        try:
            return _consolidate(self, workspace_id=workspace_id, repo_id=repo_id,
                                dry_run=dry_run, llm=llm, **kw)
        finally:
            self._internal_writes.depth = depth

    # ── read ──────────────────────────────────────────────────────────────────
    def _recall_filter(self, *, workspace_id: Optional[str], repo_id: Optional[str],
                       session_id: Optional[str], scopes: Optional[list],
                       mtypes: Optional[list], as_of: Optional[float]) -> SearchFilter:
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
            scopes=scopes, mtypes=mtypes, as_of=as_of, include_ancestors=True,
        )

    def recall(self, query: str, *, workspace_id: Optional[str] = None,
               repo_id: Optional[str] = None, session_id: Optional[str] = None,
               scopes: Optional[list] = None,
               mtypes: Optional[list] = None, as_of: Optional[float] = None,
               k: int = 8) -> RecallResult:
        flt = self._recall_filter(
            workspace_id=workspace_id, repo_id=repo_id, session_id=session_id,
            scopes=scopes, mtypes=mtypes, as_of=as_of,
        )
        return self.recall_engine.recall(query, flt, k=k)

    def grounded_recall(self, query: str, *, workspace_id: Optional[str] = None,
                        repo_id: Optional[str] = None, session_id: Optional[str] = None,
                        scopes: Optional[list] = None,
                        mtypes: Optional[list] = None, as_of: Optional[float] = None,
                        k: int = 8, llm=None, min_support: Optional[float] = None,
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
            scopes=scopes, mtypes=mtypes, as_of=as_of,
        )
        # Recall without reinforcing here: a grounded read should reward only the memories
        # it actually cites, and an abstain should reward nothing — don't reinforce the
        # irrelevant nearest-neighbours an off-topic query happened to surface.
        result = self.recall_engine.recall(query, flt, k=k, reinforce=False)
        floor = _grounded.GROUNDED_SUPPORT_FLOOR if min_support is None else min_support
        answer = _grounded.build_grounded_answer(query, result, self.embedder, llm=llm,
                                                 min_support=floor, max_citations=max_citations)
        if reinforce and answer.grounded:
            for cite in answer.citations:
                if cite.get("id"):
                    self.store.reinforce(cite["id"], boost=scoring.INTERACTION_BOOST["recall"])
        return answer

    def why(self, query: str, *, workspace_id: str, repo_id: Optional[str] = None,
            k: int = 5) -> dict:
        """Rationale + history for a decision or fact: the live
        answer, plus whatever it superseded, if anything. This is the bi-temporal "why"
        that a flat-namespace store (or a plain vector store) cannot answer — the
        superseded fact still exists, just outside the default visibility window.
        """
        flt = SearchFilter(
            workspace_id=workspace_id, repo_id=repo_id, include_ancestors=True,
        )
        live = [r for _, r in self._relatedness(query, flt, include_invalid=False)[:k]]
        history: list[MemoryRecord] = []
        if live:
            seen = {r.id for r in live}
            anchor = f"{live[0].title} {live[0].content}"
            for _, r in self._relatedness(anchor, flt, include_invalid=True):
                if r.id in seen or r.valid_to is None:
                    continue
                history.append(r)
                seen.add(r.id)
                if len(history) >= k:
                    break
        return {"answer": live, "supersedes": history}

    def timeline(self, query: str, *, workspace_id: str, repo_id: Optional[str] = None,
                limit: int = 20) -> list[MemoryRecord]:
        """Chronological, bi-temporal history of a fact: what we believed and when.
        Includes invalidated versions; sorted by ``valid_from``.
        """
        flt = SearchFilter(
            workspace_id=workspace_id, repo_id=repo_id, include_ancestors=True,
        )
        recs = [r for _, r in self._relatedness(query, flt, include_invalid=True)[:limit]]
        recs.sort(key=lambda r: r.valid_from or r.ingested_at or 0.0)
        return recs

    def _relatedness(self, query: str, flt: SearchFilter, *,
                     include_invalid: bool) -> list[tuple[float, MemoryRecord]]:
        """Score every matching memory — optionally including invalidated ones — by the
        max of semantic similarity and lexical token overlap. ``why``/``timeline`` need to
        search *through* bi-temporal history, which the normal vector index ``search()``
        deliberately excludes (it's the live-recall path), so this recomputes similarity
        directly from ``Store.iter_vectors(..., include_invalid=True)`` instead.
        """
        qvec = self.embedder.embed([query])[0]
        qn = qvec / (float(np.linalg.norm(qvec)) or 1.0)
        sem: dict[str, float] = {}
        for mid, vec in self.store.iter_vectors(
                flt, include_invalid=include_invalid, dim=int(qn.shape[0])):
            sem[mid] = float(np.dot(qn, vec))
        q_tokens = tokenize(query)
        out: list[tuple[float, MemoryRecord]] = []
        for rec in self.store.list_memories(flt, include_invalid=include_invalid, limit=500):
            lex = jaccard(q_tokens, tokenize(f"{rec.title} {rec.content}"))
            score = max(sem.get(rec.id, 0.0), lex)
            if score > 0.05:
                out.append((score, rec))
        out.sort(key=lambda t: t[0], reverse=True)
        return out

    def recall_proactive(self, *, workspace_id: str, repo_id: Optional[str] = None,
                         k: int = 10) -> dict:
        """"What should I know right now" with no explicit query — conscious/proactive
        recall: importance + recency + retention, no semantic arm,
        plus the repo's last-session handoff (open threads / summary) if there is one.
        """
        flt = SearchFilter(
            workspace_id=workspace_id, repo_id=repo_id, include_ancestors=True,
        )
        now = now_ts()
        scored = []
        for rec in self.store.list_memories(flt, limit=500):
            w = scoring.weights_for(rec.mtype)
            s = (w.i * (rec.importance or 0.0)
                 + w.c * scoring.recency(rec.valid_from or rec.ingested_at, now)
                 + w.r * scoring.retention(rec.stability, rec.last_access, now))
            scored.append((s, rec))
        scored.sort(key=lambda t: t[0], reverse=True)
        top = [r for _, r in scored[:k]]

        last_session: dict = {}
        if repo_id:
            last = self.store.get_last_session(workspace_id, repo_id)
            if last:
                last_session = {
                    "session_id": last["id"], "summary": last.get("summary") or "",
                    "open_threads": last.get("open_threads") or [],
                    "outcome": last.get("outcome") or "",
                }
        return {"memories": top, "last_session": last_session}

    # ── governance (audited; never a silent hard delete — AGENTS.md §3.2) ───────
    def forget(self, memory_id: str, *, reason: str = "", actor: str = "user") -> dict:
        if self.store.get_memory(memory_id) is None:
            raise KeyError(f"no memory with id '{memory_id}'")
        self.store.close_validity(memory_id, actor=actor, reason=reason or "forgotten by request")
        try:
            self.index.delete([memory_id])
        except Exception:
            pass
        return {"id": memory_id, "status": "forgotten", "reason": reason}

    def pin(self, memory_id: str, *, pinned: bool = True, actor: str = "user") -> dict:
        if self.store.get_memory(memory_id) is None:
            raise KeyError(f"no memory with id '{memory_id}'")
        self.store.set_pinned(memory_id, pinned)
        self.store.audit(actor, "pin" if pinned else "unpin", memory_id, "")
        return {"id": memory_id, "pinned": pinned}

    def correct(self, memory_id: str, new_content: str, *, reason: str = "",
               actor: str = "user") -> dict:
        """Replace a memory's content without losing history: insert a new memory
        carrying the same scope/type/title, then close the old validity window — an
        explicit INVALIDATE, not an in-place edit (AGENTS.md §3.2/§3.3: never overwrite).

        Write-then-retire order is load-bearing (same as ``promote``/``merge``): if the
        replacement write raises, the original must still be live. A record whose
        ``scope``/``repo_id`` disagree — reachable through the sync apply path, which
        doesn't go through ``remember``'s validation — used to be retired *first* and
        then hit ``ValueError`` on the way back in, destroying it with no replacement.
        """
        old = self.store.get_memory(memory_id)
        if old is None:
            raise KeyError(f"no memory with id '{memory_id}'")
        metadata = dict(old.metadata)
        metadata["corrects"] = memory_id
        if old.provenance:
            metadata["provenance"] = dict(old.provenance)
        new_id = self.remember(
            new_content, workspace_id=old.workspace_id, repo_id=old.repo_id,
            session_id=old.session_id, mtype=old.mtype,
            scope=_writable_scope(old.scope, old.repo_id), title=old.title,
            importance=old.importance, keywords=old.keywords, metadata=metadata,
            resolve_conflicts=False,   # the supersede decision was just made explicitly
        )
        # Persist inherited protection + confidentiality (the write path defaults
        # pinned to False and sensitivity to 'normal' — a correction must not silently
        # unpin a protected memory or downgrade a sensitive one; mirrors ``merge``).
        if old.sensitivity and old.sensitivity != "normal":
            self.store.conn.execute("UPDATE memories SET sensitivity=? WHERE id=?",
                                    (old.sensitivity, new_id))
            self.store.conn.commit()
        if old.pinned:
            self.store.set_pinned(new_id, True)
        self.store.close_validity(memory_id, actor=actor, reason=reason or "corrected")
        try:
            self.index.delete([memory_id])
        except Exception:
            pass
        return {"id": new_id, "superseded": [memory_id], "reason": reason}

    def promote(self, memory_id: str, target_scope: Scope, *, reason: str = "",
                actor: str = "user") -> dict:
        """Widen one live memory's scope without rewriting it in place.

        Promotion creates (or deduplicates into) a wider-scoped record first, then
        bi-temporally closes the narrow source and links the two. This preserves the
        provenance/history contract while preventing duplicate recall in the source
        context. Protection, confidentiality, and learned stability never decrease.
        """
        old = self.store.get_memory(memory_id)
        if old is None:
            raise KeyError(f"no memory with id '{memory_id}'")
        now = now_ts()
        if (old.expired_at is not None
                or (old.valid_from is not None and old.valid_from > now)
                or (old.valid_to is not None and old.valid_to <= now)):
            raise ValueError("only a live memory can be promoted")
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
        if old.provenance:
            metadata["provenance"] = dict(old.provenance)

        result = self.remember_with_resolution(
            old.content,
            workspace_id=old.workspace_id,
            repo_id=target_repo_id,
            session_id=None,
            mtype=old.mtype,
            scope=target_scope,
            title=old.title,
            importance=old.importance,
            keywords=old.keywords,
            metadata=metadata,
            valid_from=old.valid_from,
            resolve_conflicts=True,
        )
        promoted_id = result["id"]
        promoted = self.store.get_memory(promoted_id)
        if promoted is None:  # defensive: the write path must return a durable record
            raise RuntimeError("promotion target was not stored")

        # Fail closed on an unrecognised label (same rule as ``merge``): an unknown
        # sensitivity outranks every known one rather than silently downgrading to
        # 'normal', so a corrupt/foreign label can never widen exposure.
        sensitivity = max(
            (old.sensitivity, promoted.sensitivity),
            key=lambda value: _SENSITIVITY_RANK.get(value, len(_SENSITIVITY_RANK)),
        )
        promoted_metadata = dict(promoted.metadata)
        inherited_from = promoted_metadata.get("promoted_from")
        inherited_from = list(inherited_from) if isinstance(inherited_from, list) else []
        old_chain = old.metadata.get("promoted_from")
        for source_id in [*(old_chain if isinstance(old_chain, list) else []), old.id]:
            if source_id not in inherited_from:
                inherited_from.append(source_id)
        promoted_metadata["promoted_from"] = inherited_from
        promoted_metadata["promotion"] = {
            "from_scope": old.scope.value,
            "to_scope": target_scope.value,
            "reason": reason[:500],
        }
        promoted_provenance = dict(promoted.provenance)
        trusted = all(bool((record.provenance or {}).get("trusted", True))
                      for record in (old, promoted))
        if not trusted:
            promoted_provenance["trusted"] = False
        promoted_metadata["provenance"] = promoted_provenance
        self.store.conn.execute(
            "UPDATE memories SET pinned=?, sensitivity=?, stability=?, access_count=?, "
            "last_access=?, metadata=?, provenance=? WHERE id=?",
            (
                int(old.pinned or promoted.pinned),
                sensitivity,
                max(old.stability, promoted.stability),
                max(old.access_count, promoted.access_count),
                max(old.last_access or 0.0, promoted.last_access or 0.0) or None,
                json.dumps(promoted_metadata, ensure_ascii=False, separators=(",", ":")),
                json.dumps(promoted_provenance, ensure_ascii=False, separators=(",", ":")),
                promoted_id,
            ),
        )
        self.store.conn.commit()

        self.store.close_validity(
            old.id, actor=actor,
            reason=reason or f"promoted from {old.scope.value} to {target_scope.value}",
        )
        try:
            self.index.delete([old.id])
        except Exception:
            pass
        if not self.store.has_link(promoted_id, old.id, relation="promotes"):
            self.store.add_link(
                promoted_id, old.id, "promotes", reason=reason or "scope promotion"
            )
        self.store.audit(
            actor, "promote", promoted_id,
            f"from {old.id} ({old.scope.value}->{target_scope.value}): {reason}"[:1000],
        )
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
        """Merge several memories into one, retiring the sources into history.

        A manual N→1 governance operation — the multi-input generalization of
        ``correct``. Unlike ``consolidate`` (automatic, episodic-only, and
        *non-destructive*: sources stay live), ``merge`` is user-driven, works on any
        type, and retires every source: each source's validity window is closed (never
        a hard delete — AGENTS.md §3.2), the new memory records ``supersedes`` on every
        source so the version chain renders in why/timeline/inspector, and a ``merges``
        link is written back to each source.

        Safety (this is a write path over possibly-untrusted memories — SECURITY.md §5):
        the merged memory inherits the *most restrictive* ``sensitivity`` of its sources
        and is marked ``trusted: false`` if any source is untrusted, so a merge can never
        launder secret/untrusted content into a trusted, lower-sensitivity fact. If any
        source is pinned the result is pinned (a merge can't silently strip protection).
        Audited on both sides, with a token-compaction number (§3.7).
        """
        ids, sources, seen = [], [], set()
        for sid in source_ids:
            if sid in seen:
                continue
            seen.add(sid)
            rec = self.store.get_memory(sid)
            if rec is None:
                raise KeyError(f"no memory with id '{sid}'")
            ids.append(sid)
            sources.append(rec)
        if len(sources) < 2:
            raise ValueError("merge needs at least two distinct source memories")
        # Scope confinement (defense in depth — the service also authorizes the
        # workspace): a merge can never cross a workspace boundary.
        if len({r.workspace_id for r in sources}) != 1:
            raise ValueError("cannot merge memories from different workspaces")

        primary = sources[0]
        repo_id = primary.repo_id if len({r.repo_id for r in sources}) == 1 else None
        mt = mtype or primary.mtype
        # Cross-repo merges are explicitly permitted (``service.merge``), which drops
        # ``repo_id`` to None — so a 'repo' scope inherited from the primary source would
        # be an unstorable combination. Widen it to the workspace the sources already
        # share rather than failing (see ``_writable_scope``).
        sc = _writable_scope(scope or primary.scope, repo_id)
        importance = max([r.importance or 0.0 for r in sources] + [0.5])
        pinned_any = any(r.pinned for r in sources)
        sensitivity = max((r.sensitivity or "normal" for r in sources),
                          key=lambda s: _SENSITIVITY_RANK.get(s, len(_SENSITIVITY_RANK)))
        trusted = all(bool((r.provenance or {}).get("trusted", True)) for r in sources)
        if keywords is None:
            keywords, kseen = [], set()
            for r in sources:
                for kw in (r.keywords or []):
                    if kw not in kseen:
                        kseen.add(kw)
                        keywords.append(kw)
            keywords = keywords[:32]

        tokens_before = sum(estimate_tokens(f"{r.title} {r.content}") for r in sources)
        title_final = title if title is not None else (primary.title or "")

        # Write the merged record BEFORE retiring anything (same ordering as
        # ``promote``/``correct``). Retiring first meant a failed ``remember()`` — e.g.
        # an unstorable scope/repo combination, a full disk, a bad session_id — left
        # every source closed with no merged record to replace them: unrecoverable data
        # loss from a governance operation that is supposed to preserve history. The
        # resolver is skipped here (the supersede decision is explicit), so the
        # still-live sources can't be deduplicated into, and evolution stays a no-op.
        merged_id = self.remember(
            merged_content, workspace_id=primary.workspace_id, repo_id=repo_id,
            session_id=primary.session_id, mtype=mt, scope=sc, title=title_final,
            importance=importance, keywords=keywords,
            metadata={"supersedes": list(ids),
                      "provenance": {"source": "merge", "trusted": trusted,
                                     "merges": list(ids)}},
            resolve_conflicts=False,   # the supersede decision was just made explicitly
        )
        # Persist inherited confidentiality + protection (the write path defaults
        # sensitivity to 'normal' and pinned to False; a merge must not downgrade either).
        if sensitivity != "normal":
            self.store.conn.execute("UPDATE memories SET sensitivity=? WHERE id=?",
                                    (sensitivity, merged_id))
            self.store.conn.commit()
        if pinned_any:
            self.store.set_pinned(merged_id, True)
        for r in sources:
            self.store.close_validity(r.id, actor=actor,
                                      reason=reason or "merged into a combined memory")
            try:
                self.index.delete([r.id])
            except Exception:
                pass
        # Linking/auditing stays a separate pass so the audit trail keeps its original
        # shape: every source's invalidate entry, then every source's merge entry.
        for r in sources:
            self.store.add_link(merged_id, r.id, "merges")
            self.store.audit(actor, "merge", r.id, f"merged into {merged_id}")
        self.store.audit(actor, "merge", merged_id,
                         f"merged {len(ids)} memories: {', '.join(ids)}")

        tokens_after = estimate_tokens(f"{title_final} {merged_content}")
        saved = max(0, tokens_before - tokens_after)
        return {"id": merged_id, "merged": list(ids), "count": len(ids),
                "sensitivity": sensitivity, "trusted": trusted, "pinned": pinned_any,
                "reason": reason,
                "compaction": {"tokens_before": tokens_before,
                               "tokens_after": tokens_after, "tokens_saved": saved,
                               "reduction_pct": round(100.0 * saved / tokens_before, 1)
                               if tokens_before else 0.0, "units": len(ids)}}

    # ── linking & events (A-MEM-style) ──────────────────────────────────────────
    def link(self, a: str, b: str, *, relation: str = "related", layer=None,
             reason: str = "") -> None:
        for mid in (a, b):
            if self.store.get_memory(mid) is None:
                raise KeyError(f"no memory with id '{mid}'")
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
        from engraphis.backends.codegraph import (
            SourceWalkLimitExceeded,
            detect_lang,
            get_code_indexer,
            iter_source_files,
        )

        indexer = get_code_indexer(prefer=prefer)
        root = Path(root_path).expanduser().resolve()
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
            for file_path in iter_source_files(str(root)):
                lang = detect_lang(file_path)
                if (
                    lang is None
                    or (languages and lang not in languages)
                    or not indexer.supports(lang)
                ):
                    continue
                if files_scanned >= max_files:
                    scan_complete = False
                    break
                p = Path(file_path)
                try:
                    rel = p.resolve().relative_to(root).as_posix()
                except ValueError:
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
                    stat = p.stat()
                    if stat.st_size > max_file_bytes:
                        files_skipped += 1
                        continue
                    raw = p.read_bytes()
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
        except SourceWalkLimitExceeded:
            scan_complete = False

        removed = 0
        if scan_complete:
            for rel in sorted(set(existing) - present):
                self.store.remove_code_file(repo_id, rel, commit=False)
                removed += 1
        self.store.conn.commit()
        code_memory_links = self.rebuild_code_memory_links(repo_id=repo_id)

        primary_lang = max(lang_counts, key=lang_counts.get) if lang_counts else ""
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

    def search_code(self, query: str, *, repo_id: str, limit: int = 20,
                    flt: Optional[SearchFilter] = None) -> dict:
        """Symbol-graph + lexical code search — far cheaper than
        dumping files for structural questions, and (via ``called_by``) answers "what
        breaks if I change X" directly from the call graph."""
        symbols = self.store.search_symbols(repo_id, query, limit=limit)
        for s in symbols:
            s["called_by"] = self.store.get_symbol_callers(repo_id, s["name"], limit=10)
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
            records = self.store.list_memories_page(
                memory_filter, after_id=after_memory_id, limit=250,
            )
            if not records:
                break
            memory_ids = [record.id for record in records]
            self.store.clear_code_memory_links_for_memories(
                repo_id, memory_ids, commit=False,
            )
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
            after_memory_id = records[-1].id
        self.store.prune_code_memory_links(repo_id)
        return linked

    def code_path(self, source: str, target: str, *, repo_id: str,
                  max_depth: int = 8, flt: Optional[SearchFilter] = None) -> dict:
        """Shortest path across definitions, calls, imports, and symbol aliases."""
        symbols = self.store.list_symbols(repo_id)
        stored_edges = self.store.list_code_edges(repo_id)
        adjacency: dict[str, list[tuple[str, dict, bool]]] = defaultdict(list)
        node_meta: dict[str, dict] = {}
        for sym in symbols:
            meta = {
                "kind": "symbol", "name": sym["name"], "fqname": sym["fqname"],
                "symbol_kind": sym["kind"], "file": sym["file"], "span": sym["span"],
            }
            for key in {sym["name"], sym["fqname"]}:
                if key:
                    node_meta.setdefault(key, meta)
            if sym["name"] and sym["fqname"] and sym["name"] != sym["fqname"]:
                alias = {"relation": "alias", "layer": "entity", "file": sym["file"],
                         "line": 0}
                adjacency[sym["name"]].append((sym["fqname"], alias, True))
                adjacency[sym["fqname"]].append((sym["name"], alias, False))
        for edge in stored_edges:
            src, dst = edge["src"], edge["dst"]
            adjacency[src].append((dst, edge, True))
            adjacency[dst].append((src, edge, False))
            node_meta.setdefault(src, {"kind": "code", "name": src})
            node_meta.setdefault(dst, {"kind": "code", "name": dst})
        symbol_by_id = {symbol["id"]: symbol for symbol in symbols}
        now = now_ts()
        for link in self.store.list_code_memory_links(repo_id, flt=flt):
            if link.get("expired_at") is not None:
                continue
            valid_to = link.get("valid_to")
            if valid_to is not None and now >= float(valid_to):
                continue
            symbol = symbol_by_id.get(link.get("symbol_id"))
            if not symbol or not link.get("memory_id"):
                continue
            code_node = symbol.get("fqname") or symbol.get("name")
            if not code_node:
                continue
            memory_node = link["memory_id"]
            bridge = {
                "relation": link.get("relation") or "mentions",
                "layer": "semantic",
                "file": symbol.get("file") or "",
                "line": 0,
            }
            adjacency[code_node].append((memory_node, bridge, True))
            adjacency[memory_node].append((code_node, bridge, False))
            node_meta[memory_node] = {
                "kind": "memory",
                "name": link.get("title") or memory_node,
                "mtype": link.get("mtype") or "",
            }

        resolved_source = self._resolve_code_node(source, symbols, adjacency)
        resolved_target = self._resolve_code_node(target, symbols, adjacency)
        if not resolved_source or not resolved_target:
            return {
                "found": False, "source": source, "target": target,
                "reason": "source or target was not found in the indexed graph",
                "path": [], "edges": [],
            }
        max_depth = max(1, min(32, int(max_depth)))
        queue = deque([resolved_source])
        depth = {resolved_source: 0}
        parent: dict[str, tuple[str, dict, bool]] = {}
        while queue:
            current = queue.popleft()
            if current == resolved_target:
                break
            if depth[current] >= max_depth:
                continue
            for neighbor, edge, forward in adjacency.get(current, []):
                if neighbor in depth:
                    continue
                depth[neighbor] = depth[current] + 1
                parent[neighbor] = (current, edge, forward)
                queue.append(neighbor)

        if resolved_target not in depth:
            return {
                "found": False, "source": resolved_source, "target": resolved_target,
                "reason": f"no path within {max_depth} hops", "path": [], "edges": [],
            }
        nodes = [resolved_target]
        path_edges: list[dict] = []
        cursor = resolved_target
        while cursor != resolved_source:
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
            nodes.append(previous)
            cursor = previous
        nodes.reverse()
        path_edges.reverse()
        return {
            "found": True,
            "source": resolved_source,
            "target": resolved_target,
            "hops": len(path_edges),
            "path": [{"id": node, **node_meta.get(node, {"kind": "code", "name": node})}
                     for node in nodes],
            "edges": path_edges,
        }

    @staticmethod
    def _resolve_code_node(query: str, symbols: list[dict],
                           adjacency: dict) -> Optional[str]:
        raw = str(query or "").strip()
        if raw in adjacency:
            return raw
        lowered = raw.lower()
        exact = [
            s for s in symbols
            if str(s.get("name") or "").lower() == lowered
            or str(s.get("fqname") or "").lower() == lowered
            or str(s.get("file") or "").lower() == lowered
        ]
        candidates = exact or [
            s for s in symbols
            if lowered in str(s.get("name") or "").lower()
            or lowered in str(s.get("fqname") or "").lower()
            or lowered in str(s.get("file") or "").lower()
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda s: (
            0 if str(s.get("fqname") or "").lower() == lowered else 1,
            len(str(s.get("fqname") or "")),
        ))
        chosen = candidates[0]
        for key in (chosen.get("fqname"), chosen.get("name"), chosen.get("file")):
            if key in adjacency:
                return key
        return chosen.get("fqname") or chosen.get("name")

    def analyze_code_graph(self, *, repo_id: str,
                           limit: Optional[int] = None,
                           edge_limit: Optional[int] = None) -> dict:
        """Deterministic weighted communities, hotspots, and cross-file connections.

        ``limit``/``edge_limit`` bound the symbol/edge fetch. They default to ``None``
        (unbounded) so ``analyze_impact`` keeps today's exact answer; ``export_code_graph``
        passes its own caps because that payload is reachable by a ``viewer``.
        """
        edges = self.store.list_code_edges(repo_id, limit=edge_limit)
        symbols = self.store.list_symbols(repo_id, limit=limit)
        adjacency: dict[str, dict[str, float]] = defaultdict(dict)
        degree: dict[str, int] = defaultdict(int)
        for edge in edges:
            src, dst = edge["src"], edge["dst"]
            if not src or not dst:
                continue
            weight = (
                1.5 if edge.get("relation") in {"calls", "inherits", "implements"} else 1.0
            )
            adjacency[src][dst] = adjacency[src].get(dst, 0.0) + weight
            adjacency[dst][src] = adjacency[dst].get(src, 0.0) + weight
            degree[src] += 1
            degree[dst] += 1

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
        for cid, members in enumerate(communities):
            for node in members:
                node_community[node] = cid
            ranked = sorted(members, key=lambda node: (-degree[node], node))
            summaries.append({
                "id": cid,
                "size": len(members),
                "top_nodes": [
                    {"node": node, "degree": degree[node]} for node in ranked[:8]
                ],
            })

        symbol_file = {}
        for symbol in symbols:
            for key in (symbol.get("name"), symbol.get("fqname")):
                if key:
                    symbol_file.setdefault(key, symbol.get("file") or "")
        cross_file: list[dict] = []
        cross_degree: dict[str, int] = defaultdict(int)
        for edge in edges:
            src, dst = edge.get("src") or "", edge.get("dst") or ""
            src_file = symbol_file.get(src) or edge.get("file") or ""
            dst_file = symbol_file.get(dst) or ""
            if not src_file or not dst_file or src_file == dst_file:
                continue
            cross_degree[src] += 1
            cross_degree[dst] += 1
            cross_file.append({
                "src": src, "dst": dst, "relation": edge.get("relation") or "",
                "src_file": src_file, "dst_file": dst_file,
            })
        cross_file.sort(key=lambda item: (
            -(degree[item["src"]] + degree[item["dst"]]),
            item["src_file"], item["dst_file"], item["src"], item["dst"],
        ))
        threshold = max(
            5,
            sorted(degree.values())[max(0, int(len(degree) * 0.9) - 1)]
            if degree else 5,
        )
        hotspots = [
            {
                "node": node, "degree": count,
                "cross_file_degree": cross_degree.get(node, 0),
                "god_node": count >= threshold,
            }
            for node, count in sorted(
                degree.items(), key=lambda item: (-item[1], item[0])
            )[:20]
        ]
        return {
            "nodes": len(adjacency),
            "edges": len(edges),
            "algorithm": "weighted_label_propagation",
            "communities": summaries,
            "hotspots": hotspots,
            "surprising_connections": cross_file[:50],
            "_node_community": node_community,
        }

    def analyze_impact(self, changed_files: list[str], *, repo_id: str,
                       flt: Optional[SearchFilter] = None) -> dict:
        """Estimate graph and memory impact for a git diff / PR file list."""
        normalized = []
        seen = set()
        for file in changed_files:
            rel = str(file or "").strip().replace("\\", "/")
            while rel.startswith("./"):
                rel = rel[2:]
            if rel.startswith("/"):
                rel = rel[1:]
            if rel and rel not in seen:
                seen.add(rel)
                normalized.append(rel)
        symbols = self.store.symbols_for_files(repo_id, normalized)
        touched_names = {
            name for sym in symbols for name in (sym.get("name"), sym.get("fqname")) if name
        }
        touched_leaf_names = {str(name).split(".")[-1] for name in touched_names}
        edges = self.store.list_code_edges(repo_id)
        inbound = [
            edge for edge in edges
            if edge.get("dst") in touched_names
            or str(edge.get("dst") or "").split(".")[-1]
            in touched_leaf_names
        ]
        dependent_files = sorted({
            edge.get("file") for edge in inbound
            if edge.get("file") and edge.get("file") not in normalized
        })

        memory_mentions: dict[str, dict] = {}
        touched_symbol_ids = {symbol["id"] for symbol in symbols}
        now = now_ts()
        for link in self.store.list_code_memory_links(repo_id, flt=flt):
            if link.get("expired_at") is not None:
                continue
            valid_to = link.get("valid_to")
            if valid_to is not None and now >= float(valid_to):
                continue
            if link.get("symbol_id") not in touched_symbol_ids:
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
            symbol_name = link.get("fqname") or link.get("name") or ""
            if symbol_name and symbol_name not in item["symbols"]:
                item["symbols"].append(symbol_name)
        names_for_mentions = sorted(
            {str(s.get("name")) for s in symbols
             if s.get("name") and len(str(s.get("name"))) >= 3}
        )[:80]
        for name in names_for_mentions:
            rows = self.store.memories_mentioning(
                repo_id, name, flt=flt, limit=10,
            )
            for row in rows:
                item = memory_mentions.setdefault(
                    row["id"],
                    {"id": row["id"], "title": row["title"] or "",
                     "mtype": row["mtype"], "symbols": []},
                )
                item["symbols"].append(name)

        analysis = self.analyze_code_graph(repo_id=repo_id)
        node_community = analysis.pop("_node_community")
        communities_affected = sorted({
            node_community[name] for name in touched_names if name in node_community
        })
        score = min(
            100,
            len(normalized) * 5
            + len(symbols) * 2
            + len(inbound) * 3
            + len(memory_mentions) * 2
            + len(communities_affected) * 5,
        )
        level = "low" if score < 25 else "medium" if score < 55 else "high" if score < 80 else "critical"
        hotspot_names = {item["node"] for item in analysis["hotspots"][:10]}
        conflict_zones = sorted(touched_names & hotspot_names)
        return {
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
            "potential_conflict_zones": conflict_zones,
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
        edge_cap = max(limit * 8, 2_000)
        analysis = self.analyze_code_graph(repo_id=repo_id, limit=limit,
                                           edge_limit=edge_cap)
        analysis.pop("_node_community", None)
        # Fetch one sentinel row beyond the payload cap so truncation stays observable
        # without materializing every indexed file in a large repository.
        files = self.store.list_code_files(repo_id, limit=limit + 1)
        truncated_files = len(files) > limit
        files = files[:limit]
        nodes = self.store.list_symbols(repo_id, limit=limit)
        edges = self.store.list_code_edges(repo_id, limit=edge_cap)
        memory_links = self.store.list_code_memory_links(
            repo_id, flt=flt, limit=edge_cap
        )
        return {
            "format": "engraphis-code-graph/1",
            "generated_at": time.time(),
            "repo_id": repo_id,
            "limit": limit,
            "edge_limit": edge_cap,
            "truncated": bool(
                truncated_files or len(nodes) >= limit or len(edges) >= edge_cap
                or len(memory_links) >= edge_cap
            ),
            "files": files,
            "nodes": nodes,
            "edges": edges,
            "memory_links": memory_links,
            "analysis": analysis,
        }

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
