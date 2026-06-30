"""MemoryEngine — the high-level facade the API/MCP layer calls (MASTER_PLAN.md §6).

Wires together store + embedder + vector index + reranker + recall engine, and
exposes the two operations agents actually use: ``remember`` and ``recall`` (plus
session lifecycle passthrough). Construct with ``MemoryEngine.create(...)`` for
sensible, offline-capable defaults, or inject your own backends for production.
"""
from __future__ import annotations

from typing import Optional

from engraphis.backends.embedder_st import get_embedder
from engraphis.backends.reranker import IdentityReranker, get_reranker
from engraphis.backends.vector_sqlitevec import get_vector_index
from engraphis.core.interfaces import MemoryRecord, MemoryType, Scope, SearchFilter
from engraphis.core.recall import RecallEngine, RecallResult
from engraphis.core.store import Store


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
                 valid_from: Optional[float] = None) -> str:
        text = f"{title}\n{content}" if title else content
        vec = self.embedder.embed([text])[0]
        rec = MemoryRecord(
            id="", content=content, mtype=mtype, scope=scope, workspace_id=workspace_id,
            repo_id=repo_id, session_id=session_id, title=title, importance=importance,
            keywords=keywords or [], metadata=metadata or {}, valid_from=valid_from,
            embedding=vec,
        )
        mid = self.store.add_memory(rec)
        # keep an ANN index in sync if it maintains its own table (sqlite-vec);
        # the NumPy reference reads vectors straight from the store, so this is a no-op there.
        try:
            self.index.upsert([mid], vec.reshape(1, -1))
        except Exception:
            pass
        return mid

    # ── read ──────────────────────────────────────────────────────────────────
    def recall(self, query: str, *, workspace_id: Optional[str] = None,
               repo_id: Optional[str] = None, scopes: Optional[list] = None,
               mtypes: Optional[list] = None, as_of: Optional[float] = None,
               k: int = 8) -> RecallResult:
        flt = SearchFilter(workspace_id=workspace_id, repo_id=repo_id, scopes=scopes,
                           mtypes=mtypes, as_of=as_of)
        return self.recall_engine.recall(query, flt, k=k)

    # ── session passthrough (convenience) ──────────────────────────────────────
    def start_session(self, workspace_id: str, repo_id: Optional[str] = None, **kw) -> str:
        return self.store.start_session(workspace_id, repo_id, **kw)

    def end_session(self, session_id: str, **kw) -> None:
        self.store.end_session(session_id, **kw)
