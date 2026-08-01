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

import inspect
import re
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Optional

from engraphis.core import scoring
from engraphis.core.context import DeterministicContextPacker
from engraphis.core.graphrank import personalized_pagerank
from engraphis.core.interfaces import (
    Candidate,
    ContextPacker,
    ContextUsage,
    CandidateDepthPolicy,
    MemoryRecord,
    PackedChunk,
    Reranker,
    RetrievalPolicy,
    SearchFilter,
)
from engraphis.core.retrieval_policy import (
    CANDIDATE_DEPTH_MODES,
    DeterministicRetrievalPolicy,
    ProfileConfig,
    RETRIEVAL_PROFILES,
    profile_config,
)
from engraphis.core.poisoning import inspection_eligible, prompt_eligible
from engraphis.core.store import Store, memory_matches_filter, now_ts
from engraphis.core.textutil import jaccard, tokenize


# Prompt-safe recall may search farther than ordinary recall because backends cannot
# filter provenance. Keep the second page bounded so a mostly untrusted import never
# turns one prompt build into a full-scope scan.
PROMPT_ONLY_MIN_CANDIDATES = 256
PROMPT_ONLY_MAX_CANDIDATES = 1024


@dataclass
class RecallResult:
    chunks: list[dict] = field(default_factory=list)
    context: str = ""
    count: int = 0
    packed_chunks: list[PackedChunk] = field(default_factory=list)
    usage: Optional[ContextUsage] = None
    valid_at: Optional[float] = None
    known_at: Optional[float] = None
    historical: bool = False
    retrieval_profile: str = "balanced"
    candidate_depth_mode: str = "fixed"
    candidate_k_requested: int = 50
    candidate_k_used: int = 50
    candidate_depth_reason: str = "fixed requested depth"
    retrieval_trace: Optional[list[dict[str, Any]]] = None
    token_counter: Optional[Callable[[str], int]] = field(default=None, repr=False)
    # Safety metadata is kept off the public chunk projection.  Consumers which make
    # a trust-sensitive decision (grounded recall) can still honour a record's
    # quarantine state without exposing arbitrary user metadata through recall().
    source_metadata: dict[str, dict] = field(default_factory=dict, repr=False)


class RecallEngine:
    def __init__(self, store: Store, embedder, vector_index, reranker: Optional[Reranker] = None,
                 *, weights: Optional[dict] = None, recency_tau_days: float = 30.0,
                 token_budget: int = 1500, graph_mode: str = "ppr",
                 context_packer: Optional[ContextPacker] = None,
                 retrieval_policy: Optional[RetrievalPolicy] = None,
                 candidate_depth_policy: Optional[CandidateDepthPolicy] = None) -> None:
        self.store = store
        self.embedder = embedder
        self.index = vector_index
        self.reranker = reranker
        self.weights = weights or scoring.DEFAULT_WEIGHTS
        self.recency_tau_days = recency_tau_days
        self.token_budget = token_budget
        self.context_packer = context_packer or DeterministicContextPacker()
        self.retrieval_policy = retrieval_policy or DeterministicRetrievalPolicy()
        self.candidate_depth_policy = candidate_depth_policy or DeterministicRetrievalPolicy()
        # "ppr" (default) = Personalized PageRank over entities+links (multi-hop);
        # "1hop" = the Phase-1 entity expansion, kept for fallback and ablation.
        self.graph_mode = graph_mode

    def recall(self, query: str, flt: Optional[SearchFilter] = None, *, k: int = 8,
               candidate_k: int = 50, reinforce: bool = False,
               token_budget: Optional[int] = None,
               retrieval_profile: str = "balanced",
               candidate_depth: str = "fixed",
               diagnostics: bool = False,
               include_untrusted: bool = False,
               prompt_only: bool = False,
               arm_config: Optional[ProfileConfig] = None) -> RecallResult:
        flt = flt or SearchFilter()
        requested_historical = flt.historical
        snapshot = now_ts()
        effective_valid_at = (
            flt.valid_at if flt.valid_at is not None else snapshot
        )
        effective_known_at = (
            flt.known_at if flt.known_at is not None else snapshot
        )
        flt = replace(
            flt,
            as_of=effective_valid_at,
            valid_at=effective_valid_at,
            known_at=effective_known_at,
        )
        now = effective_valid_at
        budget = self.token_budget if token_budget is None else max(0, int(token_budget))
        requested_profile = str(retrieval_profile or "balanced").strip().casefold()
        if requested_profile not in RETRIEVAL_PROFILES:
            choices = ", ".join(sorted(RETRIEVAL_PROFILES))
            raise ValueError(f"retrieval_profile must be one of: {choices}")
        selected_profile = (
            self.retrieval_policy.profile(query)
            if requested_profile == "auto"
            else requested_profile
        )
        requested_depth_mode = str(candidate_depth or "fixed").strip().casefold()
        if requested_depth_mode not in CANDIDATE_DEPTH_MODES:
            choices = ", ".join(sorted(CANDIDATE_DEPTH_MODES))
            raise ValueError(f"candidate_depth must be one of: {choices}")
        requested_candidate_k = max(1, int(candidate_k))
        candidate_k, candidate_depth_reason = self.candidate_depth_policy.candidate_depth(
            query,
            k=max(1, int(k)),
            ceiling=requested_candidate_k,
            profile=selected_profile,
            mode=requested_depth_mode,
        )
        candidate_k = max(1, min(requested_candidate_k, int(candidate_k)))
        # ``arm_config`` is a composition-time override for controlled offline
        # ablations. Normal callers still use only named RetrievalPolicy profiles,
        # so benchmark labels do not expand the public routing contract.
        config = arm_config or profile_config(selected_profile)

        # ── arms ─────────────────────────────────────────────────────────────
        # Prompt-facing consumers filter untrusted records after retrieval because
        # vector indexes do not carry provenance. A bounded second page gives trusted
        # evidence a fair chance to survive without turning one prompt-safe recall
        # into repeated full-scope scans when a large import is untrusted.
        prompt_only = bool(prompt_only or not include_untrusted)
        prompt_target = max(1, int(k))
        candidate_ceiling = candidate_k
        arm_candidate_k = candidate_k
        if prompt_only:
            arm_candidate_k = candidate_k + min(250, candidate_k * 3)
            candidate_ceiling = max(
                arm_candidate_k,
                min(
                    PROMPT_ONLY_MAX_CANDIDATES,
                    max(PROMPT_ONLY_MIN_CANDIDATES, candidate_k * 16),
                ),
            )
        qvec = self.embedder.embed([query])[0] if config.vector else None

        while True:
            if qvec is not None:
                vec = dict(self.index.search(qvec, arm_candidate_k, filter=flt))
            else:
                vec = {}
            lex = (
                dict(self.store.fts_search(query, arm_candidate_k, filter=flt))
                if config.lexical else {}
            )
            graph = (
                self._graph_arm(query, flt, now, candidate_k=arm_candidate_k)
                if config.graph else {}
            )
            code = (
                self._code_arm(
                    query, flt, arm_candidate_k, historical=requested_historical
                )
                if config.code else {}
            )

            # Sorted, not raw set order: a set of ids iterates in hash order, which varies
            # with PYTHONHASHSEED, so equal-scored results used to come back in a different
            # order in every process. One batched lookup replaces per-id lookups.
            candidate_ids = sorted(set(vec) | set(lex) | set(graph) | set(code))
            fetched = self.store.get_memories(candidate_ids)
            recs: dict[str, MemoryRecord] = {}
            for mid in candidate_ids:
                rec = fetched.get(mid)
                if (
                    rec
                    and memory_matches_filter(rec, flt, at=now)
                    and (
                        prompt_eligible(rec.provenance, rec.metadata)
                        if prompt_only
                        else inspection_eligible(rec.provenance, rec.metadata)
                    )
                ):
                    recs[mid] = rec

            can_expand = any(
                enabled and len(values) >= arm_candidate_k
                for enabled, values in (
                    (config.vector, vec),
                    (config.lexical, lex),
                    (config.graph, graph),
                    (config.code, code),
                )
            )
            if (
                not prompt_only
                or len(recs) >= prompt_target
                or arm_candidate_k >= candidate_ceiling
                or not can_expand
            ):
                break
            arm_candidate_k = candidate_ceiling
        if not recs:
            context, packed, usage = self.context_packer.pack(query, [], budget)
            return RecallResult(
                context=context,
                packed_chunks=packed,
                usage=usage,
                valid_at=flt.valid_at,
                known_at=flt.known_at,
                historical=requested_historical,
                retrieval_profile=selected_profile,
                candidate_depth_mode=requested_depth_mode,
                candidate_k_requested=requested_candidate_k,
                candidate_k_used=candidate_k,
                candidate_depth_reason=candidate_depth_reason,
                retrieval_trace=[] if diagnostics else None,
                token_counter=getattr(self.context_packer, "count_tokens", None),
            )

        sem_n = scoring.normalize({i: vec[i] for i in vec if i in recs})
        lex_n = scoring.normalize({i: lex[i] for i in lex if i in recs})
        grp_n = scoring.normalize({i: graph[i] for i in graph if i in recs})
        code_n = scoring.normalize({i: code[i] for i in code if i in recs})
        rrf = scoring.reciprocal_rank_fusion([
            ranked for ranked in (
                _ranked(vec, recs),
                _ranked(lex, recs),
                _ranked(graph, recs),
                _ranked(code, recs),
            ) if ranked
        ])

        # ── six-term weighted score (+ small RRF nudge for cross-arm agreement) ──
        scored: list[Candidate] = []
        score_details: dict[str, dict[str, Any]] = {}
        for mid, rec in recs.items():
            w = self.weights.get(rec.mtype, scoring.Weights())
            adjusted_semantic = sem_n.get(mid, 0.0) * config.semantic_scale
            adjusted_lexical = lex_n.get(mid, 0.0) * config.lexical_scale
            adjusted_graph = (
                grp_n.get(mid, 0.0) * config.graph_scale
                + (config.graph_presence_bonus if mid in graph else 0.0)
            )
            adjusted_code = (
                code_n.get(mid, 0.0) * config.code_scale
                + (config.code_presence_bonus if mid in code else 0.0)
            )
            semantic_score = max(adjusted_semantic, adjusted_code)
            base = scoring.score_memory(
                rec, now=now, weights=w,
                semantic=semantic_score, lexical=adjusted_lexical,
                graph=adjusted_graph, recency_tau_days=self.recency_tau_days,
            )
            arms = [
                name for name, values in (
                    ("semantic", vec),
                    ("lexical", lex),
                    ("graph", graph),
                    ("code", code),
                ) if mid in values
            ]
            fusion_score = base + 0.5 * rrf.get(mid, 0.0)
            arm = (
                "code" if "code" in arms
                else (arms[0] if len(arms) == 1 else ("hybrid" if arms else "fused"))
            )
            scored.append(Candidate(
                id=mid, score=fusion_score, arm=arm, record=rec
            ))
            score_details[mid] = {
                "raw": {
                    "semantic": vec.get(mid),
                    "lexical": lex.get(mid),
                    "graph": graph.get(mid),
                    "code": code.get(mid),
                },
                "normalized": {
                    "semantic": sem_n.get(mid, 0.0),
                    "lexical": lex_n.get(mid, 0.0),
                    "graph": grp_n.get(mid, 0.0),
                    "code": code_n.get(mid, 0.0),
                },
                "profile_adjusted": {
                    "semantic": adjusted_semantic,
                    "lexical": adjusted_lexical,
                    "graph": adjusted_graph,
                    "code": adjusted_code,
                },
                "six_term_score": base,
                "rrf_score": rrf.get(mid, 0.0),
                "fusion_score": fusion_score,
                "rerank_score": None,
                "calibrated_score": fusion_score,
                "arm_agreement": len(arms),
                "arms": arms,
            }
        # Tie-break on id so equal scores get a stable, process-independent order.
        scored.sort(key=lambda c: (-c.score, c.id))

        # ── rerank top-N, keep k ─────────────────────────────────────────────
        pool = scored[: max(k * 4, k)]
        if self.reranker:
            fused_before = {candidate.id: candidate.score for candidate in pool}
            reranked = self.reranker.rerank(query, pool, k)
            rerank_raw = {
                candidate.id: float(candidate.score) for candidate in reranked
            }
            changed = any(
                abs(rerank_raw[candidate.id] - fused_before.get(candidate.id, 0.0)) > 1e-12
                for candidate in reranked
            )
            if changed:
                fusion_norm = scoring.normalize({
                    candidate.id: fused_before.get(candidate.id, 0.0)
                    for candidate in reranked
                })
                rerank_norm = scoring.normalize(rerank_raw)
                for candidate in reranked:
                    candidate.score = (
                        0.7 * fusion_norm.get(candidate.id, 0.0)
                        + 0.3 * rerank_norm.get(candidate.id, 0.0)
                    )
                reranked.sort(key=lambda candidate: (-candidate.score, candidate.id))
            final = reranked[:k]
            for candidate in final:
                detail = score_details[candidate.id]
                detail["rerank_score"] = rerank_raw.get(candidate.id)
                detail["calibrated_score"] = candidate.score
        else:
            final = pool[:k]

        if reinforce and not requested_historical:
            for c in final:
                self.store.reinforce(c.id, boost=scoring.INTERACTION_BOOST["recall"])

        # ``Candidate.score`` is deliberately query-relative: its retrieval arms are
        # min-max normalised before fusion. Publish a separate absolute signal from the
        # raw cosine already returned by the vector arm plus lexical Jaccard. Reusing
        # retrieval evidence avoids a second embedding batch on every ordinary recall.
        support = {
            candidate.id: _absolute_retrieval_support(
                query,
                candidate.record.content,
                title=candidate.record.title,
                semantic_cosine=vec.get(candidate.id, 0.0),
            )
            for candidate in final
        }
        chunks = [{
            "id": c.id, "title": c.record.title, "content": c.record.content,
            "scope": c.record.scope.value, "mtype": c.record.mtype.value,
            "repo_id": c.record.repo_id, "score": round(c.score, 4), "arm": c.arm,
            # ``score`` stays for compatibility.  ``relative_score`` names its actual
            # contract: compare it only among candidates from this one response.
            "relative_score": round(c.score, 4),
            "absolute_support": round(support[c.id], 4),
            "subject_key": c.record.subject_key,
            "claim_kind": c.record.claim_kind,
            "retention": round(scoring.retention(c.record.stability, c.record.last_access, now), 4),
            "provenance": c.record.provenance,
        } for c in final]
        context, packed_chunks, usage = self.context_packer.pack(query, final, budget)
        trace = None
        if diagnostics:
            trace = [
                {"id": candidate.id, **score_details[candidate.id]}
                for candidate in final
            ]
        return RecallResult(
            chunks=chunks,
            context=context,
            count=len(final),
            packed_chunks=packed_chunks,
            usage=usage,
            valid_at=flt.valid_at,
            known_at=flt.known_at,
            historical=requested_historical,
            retrieval_profile=selected_profile,
            candidate_depth_mode=requested_depth_mode,
            candidate_k_requested=requested_candidate_k,
            candidate_k_used=candidate_k,
            candidate_depth_reason=candidate_depth_reason,
            retrieval_trace=trace,
            token_counter=getattr(self.context_packer, "count_tokens", None),
            source_metadata={
                candidate.id: _source_safety_metadata(candidate.record)
                for candidate in final
                if candidate.record is not None
            },
        )

    # ── arms / helpers ────────────────────────────────────────────────────────
    def _code_arm(
        self,
        query: str,
        flt: SearchFilter,
        candidate_k: int,
        *,
        historical: Optional[bool] = None,
    ) -> dict[str, float]:
        """Bridge code-symbol matches to scoped memories with bounded work.

        The symbol graph remains optional: an unindexed repo simply contributes
        no candidates.  Query fan-out, matched symbols, graph edges, and linked
        memories are all capped so code recall cannot degrade into a repository
        scan.
        """
        if not flt.repo_id:
            return {}
        identifiers = []
        seen_identifiers = set()
        stop = {
            "about", "called", "class", "code", "does", "file", "from",
            "function", "into", "module", "that", "this", "what", "where",
            "which", "with",
        }
        for value in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", query):
            folded = value.casefold()
            if folded in stop or folded in seen_identifiers:
                continue
            seen_identifiers.add(folded)
            identifiers.append(value)
            if len(identifiers) >= 8:
                break
        if not identifiers:
            return {}

        symbols: dict[str, dict] = {}
        symbol_strength: dict[str, float] = {}
        per_term = max(2, min(12, candidate_k // max(1, len(identifiers))))
        for identifier in identifiers:
            matches = _call_temporal_store(
                self.store.search_symbols,
                flt,
                flt.repo_id,
                identifier,
                limit=per_term,
                requested_historical=historical,
            )
            for rank, symbol in enumerate(matches):
                symbol_id = symbol.get("id")
                if not symbol_id:
                    continue
                exact = identifier.casefold() in {
                    str(symbol.get("name") or "").casefold(),
                    str(symbol.get("fqname") or "").casefold(),
                }
                strength = (1.0 if exact else 0.75) / (rank + 1)
                symbols[symbol_id] = symbol
                symbol_strength[symbol_id] = max(
                    symbol_strength.get(symbol_id, 0.0), strength
                )
        if not symbols:
            return {}

        aliases: dict[str, str] = {}
        for symbol_id, symbol in symbols.items():
            for key in ("id", "name", "fqname"):
                value = str(symbol.get(key) or "")
                if value:
                    aliases[value] = symbol_id
        # Expand one stored code edge to capture callers/callees, bounded by a
        # multiple of candidate_k. Query only edges incident to matched aliases
        # before applying that cap, so later files cannot be hidden by a global prefix.
        edge_kwargs = {
            "limit": max(100, min(2000, candidate_k * 20)),
            "layers": flt.graph_layers,
        }
        # ``endpoints`` is a v2 Store optimization. Preserve compatibility with
        # external code stores that have not added the optional filter yet.
        try:
            edge_parameters = inspect.signature(self.store.list_code_edges).parameters.values()
            supports_endpoints = any(
                parameter.name == "endpoints"
                or parameter.kind == inspect.Parameter.VAR_KEYWORD
                for parameter in edge_parameters
            )
        except (TypeError, ValueError):
            supports_endpoints = False
        if supports_endpoints:
            edge_kwargs["endpoints"] = list(aliases)
        code_edges = _call_temporal_store(
            self.store.list_code_edges,
            flt,
            flt.repo_id,
            requested_historical=historical,
            **edge_kwargs,
        )
        related_names: dict[str, float] = {}
        for edge in code_edges:
            src, dst = str(edge.get("src") or ""), str(edge.get("dst") or "")
            if src in aliases:
                related_names[dst] = max(
                    related_names.get(dst, 0.0),
                    symbol_strength[aliases[src]] * 0.55,
                )
            if dst in aliases:
                related_names[src] = max(
                    related_names.get(src, 0.0),
                    symbol_strength[aliases[dst]] * 0.55,
                )
        if related_names:
            symbol_kwargs = {
                "limit": max(100, min(2000, candidate_k * 20)),
            }
            # Like code edges, direct symbol resolution is an optional Store
            # optimization.  When it is available, apply it before the cap so
            # a caller/callee in a later file is still eligible for recall.
            try:
                symbol_parameters = inspect.signature(self.store.list_symbols).parameters.values()
                supports_identifiers = any(
                    parameter.name == "identifiers"
                    or parameter.kind == inspect.Parameter.VAR_KEYWORD
                    for parameter in symbol_parameters
                )
            except (TypeError, ValueError):
                supports_identifiers = False
            if supports_identifiers:
                symbol_kwargs["identifiers"] = list(related_names)
            else:
                # External legacy stores cannot filter this lookup.  Do not
                # reintroduce the incorrect global prefix cap for them.
                symbol_kwargs["limit"] = None
            all_symbols = _call_temporal_store(
                self.store.list_symbols,
                flt,
                flt.repo_id,
                requested_historical=historical,
                **symbol_kwargs,
            )
            for symbol in all_symbols:
                matched_strength = max(
                    (
                        related_names.get(str(symbol.get(key) or ""), 0.0)
                        for key in ("id", "name", "fqname")
                    ),
                    default=0.0,
                )
                symbol_id = symbol.get("id")
                if matched_strength > 0.0 and symbol_id:
                    symbols[symbol_id] = symbol
                    symbol_strength[symbol_id] = max(
                        symbol_strength.get(symbol_id, 0.0), matched_strength
                    )

        selected_symbol_ids = sorted(
            symbols,
            key=lambda value: (-symbol_strength.get(value, 0.0), value),
        )[:max(10, min(100, candidate_k * 2))]
        rows_by_symbol = _call_temporal_store(
            self.store.memories_for_symbols,
            flt,
            flt.repo_id,
            selected_symbol_ids,
            limit=max(2, min(10, candidate_k)),
            requested_historical=historical,
        )
        out: dict[str, float] = {}
        for symbol_id in selected_symbol_ids:
            rows = rows_by_symbol.get(symbol_id, [])
            for rank, row in enumerate(rows):
                memory_id = row.get("id")
                if not memory_id:
                    continue
                confidence = max(0.0, min(1.0, float(row.get("confidence") or 0.0)))
                score = symbol_strength[symbol_id] * confidence / (rank + 1)
                out[memory_id] = max(out.get(memory_id, 0.0), score)
        return dict(
            sorted(out.items(), key=lambda item: (-item[1], item[0]))[:candidate_k]
        )

    def _graph_arm(
        self,
        query: str,
        flt: SearchFilter,
        now: float,
        *,
        candidate_k: int = 50,
    ) -> dict[str, float]:
        if flt.graph_layers is not None and not flt.graph_layers:
            return {}
        if self.graph_mode == "1hop":
            return self._graph_arm_1hop(query, flt, now, candidate_k=candidate_k)
        return self._graph_arm_ppr(query, flt, now, candidate_k=candidate_k)

    def _graph_arm_ppr(
        self,
        query: str,
        flt: SearchFilter,
        now: float,
        *,
        candidate_k: int = 50,
    ) -> dict[str, float]:
        """Personalized PageRank arm: build the scoped
        entity/memory graph — entity↔entity edges (bi-temporal), memory↔entity
        mentions, memory↔memory links — seed at the query's entities, and rank
        memories by walk probability. Multi-hop associations surface without
        expanding an explicit hop count; entity nodes are prefixed so names can
        never collide with memory ids."""
        entity_map = self._seed_entity_map(query, flt)
        patterns = {
            eid: (name.casefold(), _entity_pattern(name))
            for eid, name in entity_map.items()
            if name
        }
        query_folded = query.casefold()
        seeds = [
            eid
            for eid, (needle, pattern) in patterns.items()
            if needle in query_folded and pattern.search(query)
        ]
        if not seeds:
            return {}

        ent = "ent::{}".format
        adj: dict[str, list[tuple[str, float]]] = {}

        def connect(a: str, b: str, w: float) -> None:
            adj.setdefault(a, []).append((b, w))
            adj.setdefault(b, []).append((a, w))

        # Build a bounded edge set outward from the query entities.  A global
        # ULID-ordered cap would let old unrelated edges crowd out a new relation
        # required by this query before PPR sees it.
        edge_cap = 4000
        edges_by_id = {}
        frontier = set(seeds)
        expanded: set[str] = set()
        while frontier and len(edges_by_id) < edge_cap:
            batch = sorted(frontier - expanded)[:400]
            if not batch:
                break
            frontier.difference_update(batch)
            expanded.update(batch)
            next_frontier: set[str] = set()
            for edge in self.store.neighbors(
                    batch, at=now, layers=flt.graph_layers, flt=flt,
                    limit=edge_cap - len(edges_by_id)):
                if edge.id in edges_by_id:
                    continue
                edges_by_id[edge.id] = edge
                next_frontier.update((edge.src, edge.dst))
                if len(edges_by_id) >= edge_cap:
                    break
            frontier.update(next_frontier - expanded)
        for e in edges_by_id.values():
            connect(ent(e.src), ent(e.dst), max(float(e.weight or 1.0), 1e-6))

        # Query only the entity frontier before applying the incidence cap. A
        # global confidence/ID prefix can otherwise omit a memory attached to a
        # seeded or reached entity in a large scope.
        incidence_entity_ids = sorted({
            *seeds,
            *(endpoint for edge in edges_by_id.values() for endpoint in (edge.src, edge.dst)),
        })
        incidence = self.store.list_memory_entities(
            flt, entity_ids=incidence_entity_ids, limit=12_000,
        )
        # Links are graph evidence in their own right. Restricting their endpoints
        # to incidence rows silently drops a linked memory which has no entity
        # mention, even when its peer is reachable from a seeded entity. Use the
        # same bounded, scoped, bi-temporally visible memory universe as the other
        # retrieval arms so PPR can traverse that edge without widening scope. Keep
        # the incidence frontier as well when independent caps choose a different
        # subset of the scoped memory universe.
        incidence_memory_ids = {
            str(row.get("memory_id") or "")
            for row in incidence if row.get("memory_id")
        }
        frontier_links = self.store.links_touching(
            sorted(incidence_memory_ids),
            layers=flt.graph_layers,
            flt=flt,
            limit=20_000,
        )
        # Expand from the entity-incidence frontier before adding the bounded newest
        # memory window. An older unmentioned endpoint can then participate in PPR
        # through its visible link instead of being silently dropped by that window.
        memory_ids = sorted(incidence_memory_ids | {
            endpoint
            for link in frontier_links
            for endpoint in (link["a"], link["b"])
        } | {
            memory.id for memory in self.store.list_memories(flt, limit=12_000)
        })
        incidence_strength: dict[tuple[str, str], float] = {}
        for row in incidence:
            memory_id = str(row.get("memory_id") or "")
            entity_id = str(row.get("entity_id") or "")
            if memory_id and entity_id:
                key = (memory_id, entity_id)
                incidence_strength[key] = max(
                    incidence_strength.get(key, 0.0),
                    max(float(row.get("confidence") or 0.0), 1e-6),
                )
        for (memory_id, entity_id), confidence in incidence_strength.items():
            connect(memory_id, ent(entity_id), confidence)
        for link in self.store.links_among(
            memory_ids,
            layers=flt.graph_layers,
            flt=flt,
            limit=20_000,
        ):
            connect(link["a"], link["b"], 1.0)


        ranked = personalized_pagerank(adj, [ent(eid) for eid in seeds])
        memory_scores = [
            (nid, score) for nid, score in ranked.items()
            if not nid.startswith("ent::") and score > 0.0
        ]
        memory_scores.sort(key=lambda item: (-item[1], item[0]))
        return dict(memory_scores[:max(0, int(candidate_k))])

    def _graph_arm_1hop(
        self,
        query: str,
        flt: SearchFilter,
        now: float,
        *,
        candidate_k: int = 50,
    ) -> dict[str, float]:
        entity_map = self._seed_entity_map(query, flt)
        patterns = {
            eid: (name.casefold(), _entity_pattern(name))
            for eid, name in entity_map.items()
            if name
        }
        query_folded = query.casefold()
        seed_ids = [
            eid
            for eid, (needle, pattern) in patterns.items()
            if needle in query_folded and pattern.search(query)
        ]
        if not seed_ids:
            return {}
        related_ids = set(seed_ids)
        for edge in self.store.neighbors(
            seed_ids, at=now, layers=flt.graph_layers, flt=flt
        ):
            related_ids.add(edge.src)
            related_ids.add(edge.dst)
        rows = self.store.list_memory_entities(
            flt, entity_ids=sorted(related_ids), limit=12_000
        )
        out: dict[str, float] = {}
        if rows:
            for row in rows:
                memory_id = str(row.get("memory_id") or "")
                if memory_id:
                    out[memory_id] = (
                        out.get(memory_id, 0.0)
                        + max(0.0, float(row.get("confidence") or 0.0))
                    )
            return dict(sorted(
                out.items(), key=lambda item: (-item[1], item[0])
            )[:max(0, int(candidate_k))])

        return dict(sorted(
            out.items(), key=lambda item: (-item[1], item[0])
        )[:max(0, int(candidate_k))])

    def _seed_entity_map(
        self, query: str, flt: SearchFilter, *, limit: int = 2048,
    ) -> dict[str, str]:
        """Return a bounded, scoped set of entity names that may occur in ``query``."""
        terms = sorted({
            term.casefold() for term in re.findall(r"[\w@#.+-]+", query)
            if len(term) >= 2
        })[:16]
        if not terms:
            return {}
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
        clauses.append(
            "(" + " OR ".join("instr(lower(name), ?) > 0" for _ in terms) + ")"
        )
        params.extend(terms)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id LIMIT ?"
        params.append(max(0, int(limit)))
        return {
            r["id"]: r["name"]
            for r in self.store.conn.execute(sql, params).fetchall()
        }

    def _entity_map(self, flt: SearchFilter, *, limit: int = 2048) -> dict[str, str]:
        """Compatibility view of scoped entities without restoring unbounded recall scans.

        The retrieval pipeline uses :meth:`_seed_entity_map` so graph seeding remains
        query-directed. Older integrations and scope-invariant tests exercised this private
        helper directly, so retain its original semantics behind an explicit safety bound.
        """
        sql = "SELECT DISTINCT id, name FROM entities"
        clauses, params = [], []
        if flt.workspace_id:
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
        sql += " ORDER BY id LIMIT ?"
        params.append(max(0, int(limit)))
        return {
            row["id"]: row["name"]
            for row in self.store.conn.execute(sql, params).fetchall()
        }

    def _pack(self, cands: list[Candidate]) -> str:
        """Compatibility helper for callers that exercised the old private method."""
        context, _, _ = self.context_packer.pack("", cands, self.token_budget)
        return context


def _source_safety_metadata(record: MemoryRecord) -> dict:
    """Project only trust flags needed by grounded recall, never caller metadata."""
    metadata = record.metadata if isinstance(record.metadata, dict) else {}
    provenance = metadata.get("provenance")
    provenance = provenance if isinstance(provenance, dict) else {}
    quarantine = metadata.get("quarantine")
    quarantine = quarantine if isinstance(quarantine, dict) else {}
    out = {}
    trust = {}
    if provenance.get("trusted") is False:
        trust["trusted"] = False
    if provenance.get("quarantined") is True:
        trust["quarantined"] = True
    if trust:
        out["provenance"] = trust
    if str(quarantine.get("state", "")).casefold() == "quarantined":
        out["quarantine"] = {"state": "quarantined"}
    return out


def _absolute_retrieval_support(
    query: str,
    content: str,
    *,
    title: str = "",
    semantic_cosine: float,
) -> float:
    """Bounded, query-independent support from evidence retrieval already computed.

    Vector backends return raw cosine similarity. Lexical Jaccard supplies a useful
    absolute fallback when the vector arm is disabled or a lexical candidate fell
    outside the vector arm's top-k. Unlike fused rank, neither component is min-max
    normalised against the other candidates in this response.
    """
    semantic = max(0.0, min(1.0, float(semantic_cosine)))
    # FTS indexes title and content together, so its absolute evidence floor
    # must use the same text rather than rejecting a legitimate title-only hit.
    lexical = jaccard(tokenize(query), tokenize("\n".join((str(title or ""), content))))
    return max(semantic, lexical)


def _entity_pattern(name: str) -> re.Pattern[str]:
    """Match an entity as a complete token/phrase, not inside unrelated words."""
    return re.compile(r"(?<!\w)" + re.escape(name) + r"(?!\w)", re.IGNORECASE)


def _ranked(arm: dict[str, float], recs: dict) -> list[str]:
    # Tie-break on id: RRF depends on rank position, so equal arm scores must not order
    # differently between runs (they feed the final score).
    return [i for i, _ in sorted(arm.items(), key=lambda x: (-x[1], x[0])) if i in recs]


def _call_temporal_store(
    method,
    flt: SearchFilter,
    *args,
    requested_historical: Optional[bool] = None,
    **kwargs,
):
    """Call an optional code-store extension without masking implementation bugs.

    Older third-party stores may not expose the v5 ``flt`` keyword. Current reads can
    retain their legacy behavior, but historical reads must fail closed: retrying a
    method without the filter would silently substitute present-day code evidence.
    Signature inspection distinguishes an unsupported keyword from a genuine
    ``TypeError`` raised inside the implementation, which is allowed to propagate.
    """
    try:
        parameters = inspect.signature(method).parameters.values()
        supports_filter = any(
            parameter.name == "flt"
            or parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
    except (TypeError, ValueError):
        supports_filter = False
    if supports_filter:
        return method(*args, flt=flt, **kwargs)
    if flt.historical if requested_historical is None else requested_historical:
        return []
    return method(*args, **kwargs)
