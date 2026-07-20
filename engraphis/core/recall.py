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

import re
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
from engraphis.core.store import Store, memory_matches_filter, now_ts


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
        lex = dict(self.store.fts_search(query, candidate_k, filter=flt))  # id -> lexical
        graph = self._graph_arm(query, flt, now)                       # id -> weight

        # ── gather candidates and enforce visibility defensively ─────────────
        # Sorted, not raw set order: a set of ids iterates in hash order, which varies with
        # PYTHONHASHSEED, so equal-scored results used to come back in a different order in
        # every process. Sorting here (and on the final sort below) makes recall reproducible.
        # One batched lookup replaces ~150 single-row get_memory() calls per recall.
        candidate_ids = sorted(set(vec) | set(lex) | set(graph))
        fetched = self.store.get_memories(candidate_ids)
        recs: dict[str, MemoryRecord] = {}
        for mid in candidate_ids:
            rec = fetched.get(mid)
            if rec and memory_matches_filter(rec, flt, at=now):
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
        # Tie-break on id so equal scores get a stable, process-independent order.
        scored.sort(key=lambda c: (-c.score, c.id))

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
        entity_map = self._entity_map(flt)
        patterns = {eid: _entity_pattern(name) for eid, name in entity_map.items() if name}
        seeds = [eid for eid, pattern in patterns.items() if pattern.search(query)]
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
        for rec in recs:
            hay = f"{rec.title} {rec.content}"
            for eid, pattern in patterns.items():
                if pattern.search(hay):
                    connect(rec.id, ent(eid), 1.0)
        for link in self.store.links_among(
            [r.id for r in recs], layers=flt.graph_layers
        ):
            connect(link["a"], link["b"], 1.0)

        # Dense power iteration is O(n^2) memory: past this cap (far beyond any sane
        # local scope) degrade gracefully to the 1-hop arm instead of allocating big.
        if len(adj) > 4000:
            return self._graph_arm_1hop(query, flt, now)

        ranked = personalized_pagerank(adj, [ent(eid) for eid in seeds])
        return {nid: score for nid, score in ranked.items()
                if not nid.startswith("ent::") and score > 0.0}

    def _graph_arm_1hop(self, query: str, flt: SearchFilter, now: float) -> dict[str, float]:
        entity_map = self._entity_map(flt)
        patterns = {eid: _entity_pattern(name) for eid, name in entity_map.items() if name}
        seed_ids = [eid for eid, pattern in patterns.items() if pattern.search(query)]
        if not seed_ids:
            return {}
        names = {entity_map[eid] for eid in seed_ids if entity_map.get(eid)}
        for e in self.store.neighbors(seed_ids, at=now, layers=flt.graph_layers):
            if e.src in entity_map:
                names.add(entity_map[e.src])
            if e.dst in entity_map:
                names.add(entity_map[e.dst])
        out: dict[str, float] = {}
        name_patterns = [_entity_pattern(name) for name in names if name]
        for rec in self.store.list_memories(flt, limit=500):
            hay = f"{rec.title} {rec.content}"
            hits = sum(1 for pattern in name_patterns if pattern.search(hay))
            if hits:
                out[rec.id] = float(hits)
        return out

    def _entity_map(self, flt: SearchFilter) -> dict[str, str]:
        sql = "SELECT DISTINCT id, name FROM entities"
        clauses, params = [], []
        if flt.workspace_id:
            # Ancestor widening applies to workspace_id exactly as to repo_id below:
            # entities recorded without a workspace (user-scope/global) are visible to a
            # contextual read, matching SearchFilter.include_ancestors's contract.
            if flt.include_ancestors:
                clauses.append("(workspace_id=? OR workspace_id IS NULL)")
            else:
                clauses.append("workspace_id=?")
            params.append(flt.workspace_id)
        if flt.repo_id:
            if flt.include_ancestors:
                clauses.append("(repo_id=? OR repo_id IS NULL)")
            else:
                clauses.append("repo_id=?")
            params.append(flt.repo_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        return {r["id"]: r["name"] for r in self.store.conn.execute(sql, params).fetchall()}

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


def _entity_pattern(name: str) -> re.Pattern[str]:
    """Match an entity as a complete token/phrase, not inside unrelated words."""
    return re.compile(r"(?<!\w)" + re.escape(name) + r"(?!\w)", re.IGNORECASE)


def _ranked(arm: dict[str, float], recs: dict) -> list[str]:
    # Tie-break on id: RRF depends on rank position, so equal arm scores must not order
    # differently between runs (they feed the final score).
    return [i for i, _ in sorted(arm.items(), key=lambda x: (-x[1], x[0])) if i in recs]
