"""MemoryEngine — the high-level facade the API/MCP layer calls (MASTER_PLAN.md §6).

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
from engraphis.core.resolve import ResolutionOp, resolve
from engraphis.core.store import Store, now_ts
from engraphis.core.textutil import jaccard, tokenize


class MemoryEngine:
    def __init__(self, store: Store, embedder, vector_index, reranker=None) -> None:
        self.store = store
        self.embedder = embedder
        self.index = vector_index
        self.reranker = reranker or IdentityReranker()
        self.recall_engine = RecallEngine(store, embedder, vector_index, self.reranker)

    @classmethod
    def create(cls, db_path: str = ":memory:", *, embed_model: Optional[str] = None,
               embed_dim: int = 256, vector_backend: str = "auto",
               rerank_model: Optional[str] = None) -> "MemoryEngine":
        store = Store(db_path)
        embedder = get_embedder(embed_model, embed_dim)
        index = get_vector_index(store, dim=embedder.dim, prefer=vector_backend)
        reranker = get_reranker(rerank_model)
        return cls(store, embedder, index, reranker)

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
        """Store one memory with deterministic conflict resolution (MASTER_PLAN.md §8.3).

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

        decision = None
        if resolve_conflicts:
            decision = self._resolve_against_neighbors(
                text, vec, workspace_id=workspace_id, repo_id=repo_id, scope=scope,
                mtype=mtype, candidate_k=candidate_k,
            )

        if decision is not None and decision.op == ResolutionOp.NOOP:
            self.store.reinforce(decision.target_id, boost=scoring.INTERACTION_BOOST["create"])
            self.store.audit("resolver", "noop", decision.target_id, decision.reason)
            return {"id": decision.target_id, "op": "noop", "reason": decision.reason}

        rec = MemoryRecord(
            id="", content=content, mtype=mtype, scope=scope, workspace_id=workspace_id,
            repo_id=repo_id, session_id=session_id, title=title, importance=importance,
            keywords=keywords or [], metadata=metadata or {}, valid_from=valid_from,
            embedding=vec,
        )
        mid = self.store.add_memory(rec)
        try:
            self.index.upsert([mid], vec.reshape(1, -1))
        except Exception:
            pass

        if decision is not None and decision.op == ResolutionOp.INVALIDATE:
            self.store.close_validity(decision.target_id, reason=decision.reason)
            try:
                self.index.delete([decision.target_id])
            except Exception:
                pass
            self.store.audit("resolver", "invalidate", decision.target_id, decision.reason)
            return {"id": mid, "op": "invalidate", "superseded": [decision.target_id],
                    "reason": decision.reason}

        return {"id": mid, "op": "add", "reason": decision.reason if decision else ""}

    def _resolve_against_neighbors(self, text: str, vec: np.ndarray, *, workspace_id: str,
                                   repo_id: Optional[str], scope: Scope, mtype: MemoryType,
                                   candidate_k: int):
        """Fetch same-scope neighbors via the vector index and run the deterministic
        resolver (``core.resolve``). Never raises — a broken/missing index degrades to
        "no neighbors found" (ADD), not a write failure."""
        flt = SearchFilter(workspace_id=workspace_id, repo_id=repo_id, scopes=[scope],
                           mtypes=[mtype])
        try:
            hits = self.index.search(vec, candidate_k, filter=flt)
        except Exception:
            return None
        now = now_ts()
        neighbors = []
        for nid, sim in hits:
            nrec = self.store.get_memory(nid)
            if (nrec and nrec.workspace_id == workspace_id and nrec.repo_id == repo_id
                    and nrec.scope == scope and nrec.mtype == mtype
                    and nrec.expired_at is None
                    and (nrec.valid_to is None or nrec.valid_to > now)):
                neighbors.append((sim, nrec))
        return resolve(text, neighbors)

    # ── read ──────────────────────────────────────────────────────────────────
    def recall(self, query: str, *, workspace_id: Optional[str] = None,
               repo_id: Optional[str] = None, scopes: Optional[list] = None,
               mtypes: Optional[list] = None, as_of: Optional[float] = None,
               k: int = 8) -> RecallResult:
        flt = SearchFilter(workspace_id=workspace_id, repo_id=repo_id, scopes=scopes,
                           mtypes=mtypes, as_of=as_of)
        return self.recall_engine.recall(query, flt, k=k)

    def why(self, query: str, *, workspace_id: str, repo_id: Optional[str] = None,
            k: int = 5) -> dict:
        """Rationale + history for a decision or fact (MASTER_PLAN.md §11.1): the live
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
        """Chronological, bi-temporal history of a fact: what we believed and when
        (MASTER_PLAN.md §11.1). Includes invalidated versions; sorted by ``valid_from``.
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
        recall (MASTER_PLAN.md §7.2): importance + recency + retention, no semantic arm,
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
        new_id = self.remember(
            new_content, workspace_id=old.workspace_id, repo_id=old.repo_id,
            session_id=old.session_id, mtype=old.mtype, scope=old.scope, title=old.title,
            importance=old.importance, keywords=old.keywords, metadata=metadata,
            resolve_conflicts=False,   # the supersede decision was just made explicitly
        )
        return {"id": new_id, "superseded": [memory_id], "reason": reason}

    # ── linking & events (A-MEM-style; MASTER_PLAN.md §8.4, §11.1) ──────────────
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



    # ── code-symbol graph (MASTER_PLAN.md §9 — the flagship coding-agent wedge) ──
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
        """Symbol-graph + lexical code search (MASTER_PLAN.md §11.1) — far cheaper than
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
