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

from pathlib import Path
from typing import Optional

import numpy as np

from engraphis.backends.embedder_st import get_embedder
from engraphis.backends.reranker import IdentityReranker, get_reranker
from engraphis.backends.vector_sqlitevec import get_vector_index
from engraphis.core import scoring
from engraphis.core.interfaces import MemoryRecord, MemoryType, Scope, SearchFilter
from engraphis.core.recall import RecallEngine, RecallResult
from engraphis.core.resolve import RELATED_SIM_FLOOR, ResolutionOp, resolve
from engraphis.core.store import Store, now_ts
from engraphis.core.textutil import estimate_tokens, jaccard, tokenize

# Sensitivity lattice: a merge keeps the *most restrictive* label of its sources, so
# secret/sensitive content can never be laundered into a lower-sensitivity merged fact.
_SENSITIVITY_RANK = {"normal": 0, "sensitive": 1, "secret": 2}

# A-MEM-style evolution: how many related neighbors a new memory auto-links to on write.
# Bounded so hub memories don't accrete unbounded link lists (link quality > quantity).
EVOLVE_MAX_LINKS = 3


class MemoryEngine:
    def __init__(self, store: Store, embedder, vector_index, reranker=None,
                 *, auto_evolve: bool = True, extractor=None,
                 graph_extractor=None) -> None:
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

    @classmethod
    def create(cls, db_path: str = ":memory:", *, embed_model: Optional[str] = None,
               embed_dim: int = 256, vector_backend: str = "auto",
               rerank_model: Optional[str] = None, extractor: str = "none",
               graph_extractor: str = "none",
               auto_evolve: bool = True, connect=None) -> "MemoryEngine":
        from engraphis.backends.extractor import PassthroughExtractor, get_extractor
        from engraphis.backends.graph_extractor import get_graph_extractor as _get_ge
        store = Store(db_path, connect=connect)
        embedder = get_embedder(embed_model, embed_dim)
        index = get_vector_index(store, dim=embedder.dim, prefer=vector_backend)
        reranker = get_reranker(rerank_model)
        ext = get_extractor(extractor)
        if isinstance(ext, PassthroughExtractor):
            ext = None                       # ingest() treats None as passthrough
        ge = _get_ge(graph_extractor) if graph_extractor and graph_extractor != "none" else None
        return cls(store, embedder, index, reranker, auto_evolve=auto_evolve,
                   extractor=ext, graph_extractor=ge)

    # ── write ─────────────────────────────────────────────────────────────────
    def remember(self, content: str, *, workspace_id: str, repo_id: Optional[str] = None,
                 session_id: Optional[str] = None, mtype: MemoryType = MemoryType.SEMANTIC,
                 scope: Scope = Scope.REPO, title: str = "", importance: float = 0.0,
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
                 mtype: MemoryType = MemoryType.SEMANTIC, scope: Scope = Scope.REPO,
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
        text = f"{title}\n{content}" if title else content
        vec = self.embedder.embed([text])[0]

        decision, neighbors = None, []
        if resolve_conflicts:
            decision, neighbors = self._resolve_against_neighbors(
                text, vec, workspace_id=workspace_id, repo_id=repo_id, scope=scope,
                mtype=mtype, candidate_k=candidate_k,
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

        rec = MemoryRecord(
            id="", content=content, mtype=mtype, scope=scope, workspace_id=workspace_id,
            repo_id=repo_id, session_id=session_id, title=title, importance=importance,
            keywords=keywords or [], metadata=meta, valid_from=valid_from,
            # Lift provenance into its dedicated field/column so recall/why/timeline
            # surface it (copied, not popped: consolidate.py still reads
            # metadata["provenance"]).
            provenance=dict(meta.get("provenance") or {}),
            embedding=vec,
        )
        mid = self.store.add_memory(rec)
        try:
            self.index.upsert([mid], vec.reshape(1, -1))
        except Exception:
            pass

        # Optional graph population (backends.graph_extractor)
        if self.graph_extractor is not None:
            try:
                from engraphis.backends.graph_extractor import feed as _graph_feed
                _graph_feed(self.store, content, workspace_id=workspace_id,
                           repo_id=repo_id, title=title, extractor=self.graph_extractor)
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
                                   repo_id: Optional[str], scope: Scope, mtype: MemoryType,
                                   candidate_k: int):
        """Fetch same-scope neighbors via the vector index and run the deterministic
        resolver (``core.resolve``). Returns ``(decision, neighbors)`` so the caller can
        also evolve the neighborhood. Never raises — a broken/missing index degrades to
        "no neighbors found" (ADD), not a write failure."""
        flt = SearchFilter(workspace_id=workspace_id, repo_id=repo_id, scopes=[scope],
                           mtypes=[mtype])
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
                    and nrec.expired_at is None
                    and (nrec.valid_to is None or nrec.valid_to > now)):
                neighbors.append((sim, nrec))
        return resolve(text, neighbors), neighbors

    # ── ingest: extract-then-remember ───────────────────────────────────────────
    def ingest(self, text: str, *, workspace_id: str, repo_id: Optional[str] = None,
               session_id: Optional[str] = None, scope: Scope = Scope.REPO,
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
    def recall(self, query: str, *, workspace_id: Optional[str] = None,
               repo_id: Optional[str] = None, scopes: Optional[list] = None,
               mtypes: Optional[list] = None, as_of: Optional[float] = None,
               k: int = 8) -> RecallResult:
        flt = SearchFilter(workspace_id=workspace_id, repo_id=repo_id, scopes=scopes,
                           mtypes=mtypes, as_of=as_of)
        return self.recall_engine.recall(query, flt, k=k)

    def grounded_recall(self, query: str, *, workspace_id: Optional[str] = None,
                        repo_id: Optional[str] = None, scopes: Optional[list] = None,
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
        flt = SearchFilter(workspace_id=workspace_id, repo_id=repo_id, scopes=scopes,
                           mtypes=mtypes, as_of=as_of)
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
        flt = SearchFilter(workspace_id=workspace_id, repo_id=repo_id)
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
        flt = SearchFilter(workspace_id=workspace_id, repo_id=repo_id)
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
        for mid, vec in self.store.iter_vectors(flt, include_invalid=include_invalid):
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
        flt = SearchFilter(workspace_id=workspace_id, repo_id=repo_id)
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
    def link(self, a: str, b: str, *, relation: str = "related") -> None:
        for mid in (a, b):
            if self.store.get_memory(mid) is None:
                raise KeyError(f"no memory with id '{mid}'")
        self.store.add_link(a, b, relation)

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
        from engraphis.backends.codegraph import detect_lang, get_code_indexer, iter_source_files

        indexer = get_code_indexer(prefer=prefer)
        root = Path(root_path)
        files_indexed = symbols_found = edges_found = 0
        for file_path in iter_source_files(str(root)):
            if files_indexed >= max_files:
                break
            lang = detect_lang(file_path)
            if lang is None or (languages and lang not in languages) or not indexer.supports(lang):
                continue
            p = Path(file_path)
            try:
                if p.stat().st_size > max_file_bytes:
                    continue
                content = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            try:
                rel = str(p.resolve().relative_to(root.resolve()))
            except ValueError:
                rel = file_path
            try:
                fi = indexer.index_file(rel, content, lang)
            except Exception:
                continue  # one bad file shouldn't abort the whole repo index
            self.store.clear_symbols_for_file(repo_id, rel)
            for sym in fi.symbols:
                self.store.upsert_symbol(
                    repo_id=repo_id, kind=sym.kind, name=sym.name, fqname=sym.fqname,
                    file=sym.file, span=sym.span, signature=sym.signature, lang=sym.lang,
                    exported=sym.exported, content_hash=sym.content_hash,
                )
                symbols_found += 1
            for edge in fi.edges:
                self.store.add_code_edge(repo_id=repo_id, src=edge.src, dst=edge.dst,
                                         relation=edge.relation, file=edge.file, line=edge.line)
                edges_found += 1
            files_indexed += 1
        return {"files_indexed": files_indexed, "symbols": symbols_found,
               "edges": edges_found, "backend": type(indexer).__name__}

    def search_code(self, query: str, *, repo_id: str, limit: int = 20) -> dict:
        """Symbol-graph + lexical code search — far cheaper than
        dumping files for structural questions, and (via ``called_by``) answers "what
        breaks if I change X" directly from the call graph."""
        symbols = self.store.search_symbols(repo_id, query, limit=limit)
        for s in symbols:
            s["called_by"] = self.store.get_symbol_callers(repo_id, s["name"], limit=10)
        return {"query": query, "symbols": symbols}

    # ── session passthrough (convenience) ──────────────────────────────────────
    def start_session(self, workspace_id: str, repo_id: Optional[str] = None, **kw) -> str:
        return self.store.start_session(workspace_id, repo_id, **kw)

    def end_session(self, session_id: str, **kw) -> None:
        self.store.end_session(session_id, **kw)
