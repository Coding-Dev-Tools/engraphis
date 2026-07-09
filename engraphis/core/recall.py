"""Hybrid recall engine.

Pipeline: scope/time filter → hybrid candidate generation (vector + lexical + graph)
→ RRF fusion → six-term weighted scoring → rerank → context packing → reinforce.

The arms are pluggable:
* vector  — any ``VectorIndex`` (NumPy reference now; sqlite-vec/Qdrant later)
* lexical — ``Store.fts_search`` (FTS5/BM25, with fallback)
* graph   — Personalized PageRank over the entity/link graph (``core.graphrank``),
            seeded at the query's entities; ``graph_mode="1hop"`` keeps the older
            1-hop entity expansion for comparison/ablation
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from engraphis.core import scoring
from engraphis.core.graphrank import personalized_pagerank
from engraphis.core.interfaces import (
    Candidate,
    MemoryRecord,
    Reranker,
    SearchFilter,
)
from engraphis.core.store import Store, now_ts


@dataclass
class RecallResult:
    chunks: list[dict] = field(default_factory=list)
    context: str = ""
    count: int = 0


class RecallEngine:
    def __init__(self, store: Store, embedder, vector_index, reranker: Optional[Reranker] = None,
                 *, weights: Optional[dict] = None, recency_tau_days: float = 30.0,
                 token_budget: int = 1500, graph_mode: str = "ppr") -> None:
        self.store = store
        self.embedder = embedder
        self.index = vector_index
        self.reranker = reranker
        self.weights = weights or scoring.DEFAULT_WEIGHTS
        self.recency_tau_days = recency_tau_days
        self.token_budget = token_budget
        # "ppr" (default) = Personalized PageRank over entities+links (multi-hop);
        # "1hop" = the Phase-1 entity expansion, kept for fallback and ablation.
        self.graph_mode = graph_mode

    def recall(self, query: str, flt: Optional[SearchFilter] = None, *, k: int = 8,
               candidate_k: int = 50, reinforce: bool = True) -> RecallResult:
        flt = flt or SearchFilter()
        now = flt.as_of if flt.as_of is not None else now_ts()

        # ── arms ─────────────────────────────────────────────────────────────
        qvec = self.embedder.embed([query])[0]
        vec = dict(self.index.search(qvec, candidate_k, filter=flt))   # id -> cosine
        lex = dict(self.store.fts_search(query, candidate_k))          # id -> lexical
        graph = self._graph_arm(query, flt, now)                       # id -> weight

        # ── gather candidates and enforce visibility (lexical arm is unfiltered) ──
        recs: dict[str, MemoryRecord] = {}
        for mid in set(vec) | set(lex) | set(graph):
            rec = self.store.get_memory(mid)
            if rec and self._visible(rec, flt, now):
                recs[mid] = rec
        if not recs:
            return RecallResult()

        sem_n = scoring.normalize({i: vec[i] for i in vec if i in recs})
        lex_n = scoring.normalize({i: lex[i] for i in lex if i in recs})
        grp_n = scoring.normalize({i: graph[i] for i in graph if i in recs})
        rrf = scoring.reciprocal_rank_fusion([
            _ranked(vec, recs), _ranked(lex, recs), _ranked(graph, recs),
        ])

        # ── six-term weighted score (+ small RRF nudge for cross-arm agreement) ──
        scored: list[Candidate] = []
        for mid, rec in recs.items():
            w = self.weights.get(rec.mtype, scoring.Weights())
            base = scoring.score_memory(
                rec, now=now, weights=w,
                semantic=sem_n.get(mid, 0.0), lexical=lex_n.get(mid, 0.0),
                graph=grp_n.get(mid, 0.0), recency_tau_days=self.recency_tau_days,
            )
            arm = "semantic" if mid in vec else ("lexical" if mid in lex else "graph")
            scored.append(Candidate(id=mid, score=base + 0.5 * rrf.get(mid, 0.0),
                                    arm=arm, record=rec))
        scored.sort(key=lambda c: c.score, reverse=True)

        # ── rerank top-N, keep k ─────────────────────────────────────────────
        pool = scored[: max(k * 4, k)]
        final = self.reranker.rerank(query, pool, k) if self.reranker else pool[:k]

        if reinforce:
            for c in final:
                self.store.reinforce(c.id, boost=scoring.INTERACTION_BOOST["recall"])

        chunks = [{
            "id": c.id, "title": c.record.title, "content": c.record.content,
            "scope": c.record.scope.value, "mtype": c.record.mtype.value,
            "repo_id": c.record.repo_id, "score": round(c.score, 4), "arm": c.arm,
            "retention": round(scoring.retention(c.record.stability, c.record.last_access, now), 4),
            "provenance": c.record.provenance,
        } for c in final]
        return RecallResult(chunks=chunks, context=self._pack(final), count=len(final))

    # ── arms / helpers ────────────────────────────────────────────────────────
    def _graph_arm(self, query: str, flt: SearchFilter, now: float) -> dict[str, float]:
        if self.graph_mode == "1hop":
            return self._graph_arm_1hop(query, flt, now)
        return self._graph_arm_ppr(query, flt, now)

    def _graph_arm_ppr(self, query: str, flt: SearchFilter, now: float) -> dict[str, float]:
        """Personalized PageRank arm: build the scoped
        entity/memory graph — entity↔entity edges (bi-temporal), memory↔entity
        mentions, memory↔memory links — seed at the query's entities, and rank
        memories by walk probability. Multi-hop associations surface without
        expanding an explicit hop count; entity nodes are prefixed so names can
        never collide with memory ids."""
        ql = query.lower()
        all_names = [n for n in self._entities(flt) if n]
        seeds = [n for n in all_names if n.lower() in ql]
        if not seeds:
            return {}

        ent = "ent::{}".format
        adj: dict[str, list[tuple[str, float]]] = {}

        def connect(a: str, b: str, w: float) -> None:
            adj.setdefault(a, []).append((b, w))
            adj.setdefault(b, []).append((a, w))

        for e in self.store.edges_in_scope(flt, at=now):
            connect(ent(e.src), ent(e.dst), max(float(e.weight or 1.0), 1e-6))

        recs = self.store.list_memories(flt, limit=500)
        lowered = {n: n.lower() for n in all_names}
        for rec in recs:
            hay = f"{rec.title} {rec.content}".lower()
            for name, low in lowered.items():
                if low in hay:
                    connect(rec.id, ent(name), 1.0)
        for link in self.store.links_among([r.id for r in recs]):
            connect(link["a"], link["b"], 1.0)

        # Dense power iteration is O(n^2) memory: past this cap (far beyond any sane
        # local scope) degrade gracefully to the 1-hop arm instead of allocating big.
        if len(adj) > 4000:
            return self._graph_arm_1hop(query, flt, now)

        ranked = personalized_pagerank(adj, [ent(s) for s in seeds])
        return {nid: score for nid, score in ranked.items()
                if not nid.startswith("ent::") and score > 0.0}

    def _graph_arm_1hop(self, query: str, flt: SearchFilter, now: float) -> dict[str, float]:
        ql = query.lower()
        seed_names = [name for name in self._entities(flt) if name and name.lower() in ql]
        if not seed_names:
            return {}
        # Resolve entity names to their node IDs so neighbors() matches the edges table.
        seed_ids: list[str] = []
        marks = ",".join("?" for _ in seed_names)
        for row in self.store.conn.execute(
            f"SELECT id FROM entities WHERE name IN ({marks})", seed_names
        ).fetchall():
            seed_ids.append(row["id"])
        if not seed_ids:
            return {}
        names = set(seed_names)
        for e in self.store.neighbors(seed_ids, at=now):
            names.add(e.src)
            names.add(e.dst)
        out: dict[str, float] = {}
        for rec in self.store.list_memories(flt, limit=500):
            hay = f"{rec.title} {rec.content}".lower()
            hits = sum(1 for n in names if n and n.lower() in hay)
            if hits:
                out[rec.id] = float(hits)
        return out

    def _entities(self, flt: SearchFilter) -> list[str]:
        sql = "SELECT DISTINCT name FROM entities"
        clauses, params = [], []
        if flt.workspace_id:
            clauses.append("workspace_id=?")
            params.append(flt.workspace_id)
        if flt.repo_id:
            clauses.append("repo_id=?")
            params.append(flt.repo_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        return [r["name"] for r in self.store.conn.execute(sql, params).fetchall()]

    @staticmethod
    def _visible(rec: MemoryRecord, flt: SearchFilter, now: float) -> bool:
        if flt.workspace_id and rec.workspace_id != flt.workspace_id:
            return False
        if flt.repo_id and rec.repo_id != flt.repo_id:
            return False
        if flt.session_id and rec.session_id != flt.session_id:
            return False
        if flt.scopes and rec.scope not in flt.scopes:
            return False
        if flt.mtypes and rec.mtype not in flt.mtypes:
            return False
        if rec.expired_at is not None:
            return False
        if rec.valid_from is not None and rec.valid_from > now:
            return False
        if rec.valid_to is not None and now >= rec.valid_to:
            return False
        return True

    def _pack(self, cands: list[Candidate]) -> str:
        parts, used = [], 0
        for c in cands:
            r = c.record
            header = f"[{r.scope.value}:{r.repo_id or '-'}]"
            if r.title:
                header += f" {r.title}"
            block = f"{header}\n{r.summary or r.content}"
            used += len(block) // 4
            if used > self.token_budget and parts:
                break
            parts.append(block)
        return "\n\n".join(parts)


def _ranked(arm: dict[str, float], recs: dict) -> list[str]:
    return [i for i, _ in sorted(arm.items(), key=lambda x: -x[1]) if i in recs]
