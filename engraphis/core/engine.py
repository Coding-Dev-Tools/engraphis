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
import math
import re
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


def _bounded_finite(value, *, default: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return max(minimum, min(maximum, number))


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
                 candidate_k: int = 5) -> str:
        """Store one memory. Returns the id of the *live* record: a new id for ADD/
        INVALIDATE, or the existing memory's id if this was resolved as a NOOP
        (near-duplicate). See ``remember_with_resolution`` for the full decision detail.
        """
        return self.remember_with_resolution(
            content, workspace_id=workspace_id, repo_id=repo_id, session_id=session_id,
            mtype=mtype, scope=scope, title=title, importance=importance, keywords=keywords,
            metadata=metadata, valid_from=valid_from, resolve_conflicts=resolve_conflicts,
            candidate_k=candidate_k,
        )["id"]

    def remember_with_resolution(self, content: str, *, workspace_id: str,
                 repo_id: Optional[str] = None, session_id: Optional[str] = None,
                 mtype: MemoryType = MemoryType.SEMANTIC, scope: Optional[Scope] = None,
                 title: str = "", importance: float = 0.0, keywords: Optional[list] = None,
                 metadata: Optional[dict] = None, valid_from: Optional[float] = None,
                 resolve_conflicts: bool = True, candidate_k: int = 5) -> dict:
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
        vec = self.embedder.embed([text])[0]

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

        meta = dict(metadata or {})
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
        except Exception:
            pass
        if repo_id:
            self._link_memory_to_code(mid, content=f"{title}\n{content}", repo_id=repo_id)

        # Optional graph population (backends.graph_extractor). Structured fact metadata
        # from llm_structured is already validated before storage, so feed it directly
        # into the graph even when the regex graph extractor is disabled; then run the
        # configured text extractor too (idempotent via feed/store de-duping).
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
            except Exception:
                pass
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
            fact_metadata = {**base_metadata, **(getattr(f, "metadata", {}) or {})}
            results.append(self.remember_with_resolution(
                f.content, workspace_id=workspace_id, repo_id=repo_id,
                session_id=session_id, mtype=f.mtype or default_mtype, scope=scope,
                title=f.title, importance=f.importance, keywords=f.keywords,
                metadata=fact_metadata, resolve_conflicts=resolve_conflicts,
            ))
        return {"facts": results, "count": len(results), "extracted": extracted}

    # ── consolidation: the sleep-time loop, callable on demand (Phase 4) ───────
    def consolidate(self, *, workspace_id: str, repo_id: Optional[str] = None,
                    dry_run: bool = False, llm=None, **kw) -> dict:
        """One sleep-time consolidation sweep — episodic→semantic distillation plus
        decayed-transient archival. See ``core.consolidate.consolidate`` for knobs."""
        from engraphis.core.consolidate import consolidate as _consolidate
        return _consolidate(self, workspace_id=workspace_id, repo_id=repo_id,
                            dry_run=dry_run, llm=llm, **kw)

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
        """Replace a memory's content without losing history: close the old validity
        window and insert a new memory carrying the same scope/type/title — an explicit
        INVALIDATE, not an in-place edit (AGENTS.md §3.2/§3.3: never overwrite)."""
        old = self.store.get_memory(memory_id)
        if old is None:
            raise KeyError(f"no memory with id '{memory_id}'")
        self.store.close_validity(memory_id, actor=actor, reason=reason or "corrected")
        try:
            self.index.delete([memory_id])
        except Exception:
            pass
        metadata = dict(old.metadata)
        metadata["corrects"] = memory_id
        if old.provenance:
            metadata["provenance"] = dict(old.provenance)
        new_id = self.remember(
            new_content, workspace_id=old.workspace_id, repo_id=old.repo_id,
            session_id=old.session_id, mtype=old.mtype, scope=old.scope, title=old.title,
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

        sensitivity = max(
            (old.sensitivity, promoted.sensitivity),
            key=lambda value: _SENSITIVITY_RANK.get(value, 0),
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
        sc = scope or primary.scope
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

        # Close every source first (mirrors ``correct``): the supersede decision is
        # explicit, so the new write skips the resolver, and evolution won't relink the
        # merged memory to a source that is about to be retired.
        for r in sources:
            self.store.close_validity(r.id, actor=actor,
                                      reason=reason or "merged into a combined memory")
            try:
                self.index.delete([r.id])
            except Exception:
                pass

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

    def search_code(self, query: str, *, repo_id: str, limit: int = 20) -> dict:
        """Symbol-graph + lexical code search — far cheaper than
        dumping files for structural questions, and (via ``called_by``) answers "what
        breaks if I change X" directly from the call graph."""
        symbols = self.store.search_symbols(repo_id, query, limit=limit)
        for s in symbols:
            s["called_by"] = self.store.get_symbol_callers(repo_id, s["name"], limit=10)
            s["linked_memories"] = self.store.memories_for_symbol(
                repo_id, s["id"], limit=10
            )
        return {"query": query, "symbols": symbols}

    def _link_memory_to_code(self, memory_id: str, *, content: str,
                             repo_id: str, commit: bool = True,
                             symbols: Optional[list[dict]] = None) -> int:
        """Persist deterministic bridges from one memory to symbols in its repo."""
        hay = str(content or "")
        hay_lower = hay.lower()
        hay_tokens = tokenize(hay)
        linked = 0
        for symbol in symbols if symbols is not None else self.store.list_symbols(repo_id):
            name = str(symbol.get("name") or "").strip()
            fqname = str(symbol.get("fqname") or "").strip()
            if len(name) < 3:
                continue
            confidence = 0.0
            fqname_lower = fqname.lower()
            name_lower = name.lower()
            if fqname and len(fqname) >= 3 and fqname_lower in hay_lower and re.search(
                r"(?<!\w)" + re.escape(fqname.lower()) + r"(?!\w)", hay_lower
            ):
                confidence = 1.0
            elif name_lower in hay_lower and re.search(
                r"(?<!\w)" + re.escape(name_lower) + r"(?!\w)", hay_lower
            ):
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
            if linked >= 200:
                break
        if commit and linked:
            self.store.conn.commit()
        return linked

    def rebuild_code_memory_links(self, *, repo_id: str) -> int:
        """Rebuild the code↔memory bridge after an incremental repository index."""
        self.store.clear_code_memory_links(repo_id, commit=False)
        records = self.store.list_memories(SearchFilter(repo_id=repo_id), limit=5_000)
        symbols = self.store.list_symbols(repo_id)
        linked = 0
        for record in records:
            linked += self._link_memory_to_code(
                record.id, content=f"{record.title}\n{record.content}",
                repo_id=repo_id, commit=False, symbols=symbols,
            )
        self.store.conn.commit()
        return linked

    def code_path(self, source: str, target: str, *, repo_id: str,
                  max_depth: int = 8) -> dict:
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
        for link in self.store.list_code_memory_links(repo_id):
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

    def analyze_code_graph(self, *, repo_id: str) -> dict:
        """Deterministic weighted communities, hotspots, and cross-file connections."""
        edges = self.store.list_code_edges(repo_id)
        symbols = self.store.list_symbols(repo_id)
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

    def analyze_impact(self, changed_files: list[str], *, repo_id: str) -> dict:
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
        for link in self.store.list_code_memory_links(repo_id):
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
            escaped = str(name).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            rows = self.store.conn.execute(
                "SELECT id, title, mtype FROM memories WHERE repo_id=? "
                "AND (valid_from IS NULL OR valid_from<=?) "
                "AND (valid_to IS NULL OR ?<valid_to) AND expired_at IS NULL "
                "AND (title LIKE ? ESCAPE '\\' OR content LIKE ? ESCAPE '\\') LIMIT 10",
                (repo_id, now, now, f"%{escaped}%", f"%{escaped}%"),
            ).fetchall()
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

    def export_code_graph(self, *, repo_id: str) -> dict:
        """Portable graph.json payload for external tooling."""
        analysis = self.analyze_code_graph(repo_id=repo_id)
        analysis.pop("_node_community", None)
        return {
            "format": "engraphis-code-graph/1",
            "generated_at": time.time(),
            "repo_id": repo_id,
            "files": self.store.list_code_files(repo_id),
            "nodes": self.store.list_symbols(repo_id),
            "edges": self.store.list_code_edges(repo_id),
            "memory_links": self.store.list_code_memory_links(repo_id),
            "analysis": analysis,
        }

    def code_graph_report(self, *, repo_id: str, payload: Optional[dict] = None) -> str:
        """Human-readable GRAPH_REPORT.md companion to :meth:`export_code_graph`."""
        payload = payload or self.export_code_graph(repo_id=repo_id)
        analysis = payload["analysis"]
        lines = [
            "# Engraphis Code Graph Report",
            "",
            f"- Generated: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(payload['generated_at']))}",
            f"- Files indexed: {len(payload['files'])}",
            f"- Symbols: {len(payload['nodes'])}",
            f"- Relationships: {len(payload['edges'])}",
            f"- Communities: {len(analysis['communities'])}",
            "",
            "## Hotspots",
            "",
        ]
        if analysis["hotspots"]:
            lines.extend(
                f"- `{item['node']}` — degree {item['degree']}"
                for item in analysis["hotspots"]
            )
        else:
            lines.append("- No connected code nodes yet.")
        lines.extend(["", "## Communities", ""])
        for community in analysis["communities"][:20]:
            top = ", ".join(
                f"`{item['node']}` ({item['degree']})"
                for item in community["top_nodes"]
            )
            lines.append(
                f"- Community {community['id']}: {community['size']} nodes"
                + (f" — {top}" if top else "")
            )
        return "\n".join(lines) + "\n"

    def code_graph_html(self, *, repo_id: str, payload: Optional[dict] = None) -> str:
        """Self-contained, dependency-free graph.html export."""
        import html
        import json
        payload = payload or self.export_code_graph(repo_id=repo_id)
        safe_json = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")
        rows = []
        for node in payload["nodes"][:5_000]:
            rows.append(
                "<tr><td><code>{}</code></td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                    html.escape(str(node.get("fqname") or node.get("name") or "")),
                    html.escape(str(node.get("kind") or "")),
                    html.escape(str(node.get("file") or "")),
                    html.escape(str(node.get("span") or "")),
                )
            )
        return """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Engraphis Code Graph</title>
<style>
body{font:14px system-ui;margin:0;color:#17202a;background:#f8fafc}
main{max-width:1600px;margin:auto;padding:1.5rem}
.toolbar{display:flex;gap:.7rem;flex-wrap:wrap;align-items:center}
input,select{padding:.65rem;border:1px solid #94a3b8;border-radius:.4rem;background:white}
input{width:min(42rem,80vw)}
.layout{display:grid;grid-template-columns:minmax(0,1fr) 18rem;gap:1rem;margin-top:1rem}
.canvas{background:#0f172a;border-radius:.6rem;overflow:hidden;min-height:36rem}
svg{width:100%;height:70vh;min-height:36rem;display:block;touch-action:none}
.edge{stroke:#64748b;stroke-opacity:.42;stroke-width:1}
.edge.memory{stroke:#f59e0b;stroke-dasharray:4 3}
.node circle{stroke:#e2e8f0;stroke-width:1.2;cursor:pointer}
.node text{fill:#e2e8f0;font:10px ui-monospace,monospace;pointer-events:none}
.node.memory circle{fill:#f59e0b}.node.external circle{fill:#64748b}
.node.code circle{fill:#38bdf8}.node.match circle{stroke:#fb7185;stroke-width:4}
aside{background:white;border:1px solid #e2e8f0;border-radius:.6rem;padding:1rem}
aside code{overflow-wrap:anywhere}.muted{color:#64748b}
table{width:100%;border-collapse:collapse;margin-top:1rem;background:white}
th,td{text-align:left;padding:.55rem;border-bottom:1px solid #e2e8f0}
code{font:12px ui-monospace,monospace}
@media(max-width:850px){.layout{grid-template-columns:1fr}aside{order:-1}}
</style></head><body>
<main>
<h1>Engraphis Code Graph</h1>
<p class="muted"><span id="summary"></span></p>
<div class="toolbar">
<input id="filter" placeholder="Filter symbols, files, kinds, or relations"
 aria-label="Filter graph">
<select id="relation" aria-label="Filter by relationship"><option value="">All relations</option>
</select>
<span class="muted">Scroll to zoom; drag the canvas to pan.</span>
</div>
<div class="layout">
<div class="canvas"><svg id="graph" role="img"
 aria-label="Interactive code and memory relationship graph"></svg></div>
<aside><h2>Selection</h2><div id="details" class="muted">
Select a node to inspect its type, file, signature, and connections.</div>
<hr><p class="muted" id="render-note"></p></aside>
</div>
<details><summary>Accessible symbol table</summary>
<table><thead><tr><th>Symbol</th><th>Kind</th><th>File</th><th>Span</th></tr></thead>
<tbody id="rows">""" + "".join(rows) + """</tbody></table></details>
<script type="application/json" id="graph-data">""" + safe_json + """</script>
<script>
const graph=JSON.parse(document.getElementById('graph-data').textContent);
document.getElementById('summary').textContent =
  `${graph.files.length} files · ${graph.nodes.length} symbols · ` +
  `${graph.edges.length} relations · ${graph.memory_links.length} memory links`;
const rows=[...document.querySelectorAll('#rows tr')];
const svg=document.getElementById('graph');
const NS='http://www.w3.org/2000/svg';
const MAX_NODES=1000,MAX_EDGES=3000;
const nodes=[],byKey=new Map();
function rememberKey(key,node){if(key&&!byKey.has(String(key)))byKey.set(String(key),node)}
function addNode(raw,kind='code'){
  if(nodes.length>=MAX_NODES)return null;
  const node={...raw,_kind:kind,_i:nodes.length};
  node.label=String(raw.fqname||raw.name||raw.title||raw.file||raw.id||'unknown');
  nodes.push(node);
  [raw.id,raw.fqname,raw.name,raw.file].forEach(key=>rememberKey(key,node));
  return node;
}
(graph.nodes||[]).slice(0,900).forEach(node=>addNode(node));
function endpoint(value){
  const key=String(value||'');
  if(!key)return null;
  return byKey.get(key)||addNode({id:`external:${key}`,name:key,kind:'external'},'external');
}
const edges=[];
(graph.edges||[]).slice(0,MAX_EDGES).forEach(edge=>{
  const source=endpoint(edge.src),target=endpoint(edge.dst);
  if(source&&target)edges.push({...edge,source,target,_kind:'code'});
});
(graph.memory_links||[]).forEach(link=>{
  if(edges.length>=MAX_EDGES)return;
  const source=byKey.get(String(link.symbol_id||''));
  let target=byKey.get(String(link.memory_id||''));
  if(!target)target=addNode({
    id:link.memory_id,name:link.title||link.memory_id,kind:link.mtype||'memory'
  },'memory');
  if(source&&target)edges.push({
    source,target,relation:link.relation||'mentions',_kind:'memory'
  });
});
const groups=new Map();
nodes.forEach(node=>{
  const group=node._kind==='memory'?'Memories':String(node.file||'(external)');
  if(!groups.has(group))groups.set(group,[]);
  groups.get(group).push(node);
});
const cols=Math.max(1,Math.ceil(Math.sqrt(groups.size)));
const cellW=300,cellH=230,width=Math.max(900,cols*cellW);
const height=Math.max(650,Math.ceil(groups.size/cols)*cellH);
svg.setAttribute('viewBox',`0 0 ${width} ${height}`);
[...groups.entries()].forEach(([group,items],index)=>{
  const cx=(index%cols)*cellW+cellW/2;
  const cy=Math.floor(index/cols)*cellH+cellH/2;
  const radius=Math.min(92,32+items.length*2.2);
  items.forEach((node,i)=>{
    const angle=(Math.PI*2*i/Math.max(1,items.length))-(Math.PI/2);
    node.x=cx+(items.length===1?0:Math.cos(angle)*radius);
    node.y=cy+(items.length===1?0:Math.sin(angle)*radius);
    node.group=group;
  });
});
const relationSelect=document.getElementById('relation');
[...new Set(edges.map(edge=>edge.relation||''))].sort().forEach(relation=>{
  const option=document.createElement('option');option.value=relation;
  option.textContent=relation||'(unlabeled)';relationSelect.appendChild(option);
});
const edgeLayer=document.createElementNS(NS,'g');
const nodeLayer=document.createElementNS(NS,'g');
svg.append(edgeLayer,nodeLayer);
edges.forEach(edge=>{
  const line=document.createElementNS(NS,'line');
  line.setAttribute('x1',edge.source.x);line.setAttribute('y1',edge.source.y);
  line.setAttribute('x2',edge.target.x);line.setAttribute('y2',edge.target.y);
  line.setAttribute('class',`edge ${edge._kind}`);
  const title=document.createElementNS(NS,'title');
  title.textContent=edge.relation||'';line.appendChild(title);
  edge.el=line;edgeLayer.appendChild(line);
});
nodes.forEach(node=>{
  const group=document.createElementNS(NS,'g');
  group.setAttribute('class',`node ${node._kind}`);
  group.setAttribute('transform',`translate(${node.x} ${node.y})`);
  group.setAttribute('tabindex','0');group.setAttribute('role','button');
  group.setAttribute('aria-label',node.label);
  const circle=document.createElementNS(NS,'circle');
  circle.setAttribute('r',node._kind==='memory'?8:node._kind==='external'?5:6);
  const title=document.createElementNS(NS,'title');title.textContent=node.label;
  circle.appendChild(title);group.appendChild(circle);
  if(nodes.length<=260){
    const text=document.createElementNS(NS,'text');text.setAttribute('x',9);
    text.setAttribute('y',3);text.textContent=node.label.slice(0,38);group.appendChild(text);
  }
  const select=()=>showNode(node);
  group.addEventListener('click',select);
  group.addEventListener('keydown',event=>{
    if(event.key==='Enter'||event.key===' '){event.preventDefault();select()}
  });
  node.el=group;nodeLayer.appendChild(group);
});
function showNode(node){
  const connected=edges.filter(edge=>edge.source===node||edge.target===node);
  const box=document.getElementById('details');box.textContent='';
  const title=document.createElement('h3');title.textContent=node.label;box.appendChild(title);
  const facts=[
    ['Type',node._kind==='memory'?(node.kind||'memory'):(node.kind||node._kind)],
    ['File',node.file||''],['Span',node.span||''],['Group',node.group||''],
    ['Connections',String(connected.length)]
  ];
  facts.filter(item=>item[1]).forEach(([label,value])=>{
    const p=document.createElement('p'),strong=document.createElement('strong');
    strong.textContent=`${label}: `;p.append(strong,document.createTextNode(String(value)));
    box.appendChild(p);
  });
  if(node.signature){
    const pre=document.createElement('code');pre.textContent=node.signature;box.appendChild(pre);
  }
  if(node.docstring){
    const p=document.createElement('p');p.textContent=node.docstring;box.appendChild(p);
  }
  connected.slice(0,20).forEach(edge=>{
    const p=document.createElement('p');p.className='muted';
    const other=edge.source===node?edge.target:edge.source;
    p.textContent=`${edge.relation||'related'} → ${other.label}`;box.appendChild(p);
  });
}
function applyFilters(){
  const q=document.getElementById('filter').value.trim().toLowerCase();
  const relation=relationSelect.value;
  nodes.forEach(node=>{
    const match=!q||[node.label,node.file,node.kind,node.docstring]
      .some(value=>String(value||'').toLowerCase().includes(q));
    node.el.classList.toggle('match',Boolean(q&&match));
    node.el.style.opacity=match?'1':q?'.18':'1';
  });
  edges.forEach(edge=>{
    const qMatch=!q||[edge.relation,edge.source.label,edge.target.label]
      .some(value=>String(value||'').toLowerCase().includes(q));
    edge.el.style.display=(!relation||edge.relation===relation)&&qMatch?'':'none';
  });
  rows.forEach(row=>{row.hidden=Boolean(q&&!row.textContent.toLowerCase().includes(q))});
}
document.getElementById('filter').addEventListener('input',applyFilters);
relationSelect.addEventListener('change',applyFilters);
let view={x:0,y:0,w:width,h:height},drag=null;
function setView(){svg.setAttribute('viewBox',`${view.x} ${view.y} ${view.w} ${view.h}`)}
svg.addEventListener('wheel',event=>{
  event.preventDefault();const factor=event.deltaY>0?1.12:.88;
  const rect=svg.getBoundingClientRect();
  const px=view.x+(event.clientX-rect.left)/rect.width*view.w;
  const py=view.y+(event.clientY-rect.top)/rect.height*view.h;
  view.x=px-(px-view.x)*factor;view.y=py-(py-view.y)*factor;
  view.w*=factor;view.h*=factor;setView();
},{passive:false});
svg.addEventListener('pointerdown',event=>{
  drag={x:event.clientX,y:event.clientY,vx:view.x,vy:view.y};
  svg.setPointerCapture(event.pointerId);
});
svg.addEventListener('pointermove',event=>{
  if(!drag)return;const rect=svg.getBoundingClientRect();
  view.x=drag.vx-(event.clientX-drag.x)/rect.width*view.w;
  view.y=drag.vy-(event.clientY-drag.y)/rect.height*view.h;setView();
});
svg.addEventListener('pointerup',()=>{drag=null});
document.getElementById('render-note').textContent=
  `Rendered ${nodes.length}/${graph.nodes.length} nodes and `+
  `${edges.length}/${graph.edges.length+graph.memory_links.length} edges.`;
</script></main></body></html>"""

    # ── session passthrough (convenience) ──────────────────────────────────────
    def start_session(self, workspace_id: str, repo_id: Optional[str] = None, **kw) -> str:
        return self.store.start_session(workspace_id, repo_id, **kw)

    def end_session(self, session_id: str, **kw) -> None:
        self.store.end_session(session_id, **kw)
