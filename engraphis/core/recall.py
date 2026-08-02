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

import hashlib
import inspect
import json
import math
import queue
import re
import threading
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Optional

from engraphis.core import scoring
from engraphis.core.context import DeterministicContextPacker
from engraphis.core.graph_policy import UniformGraphTraversalPolicy
from engraphis.core.graphrank import personalized_pagerank
from engraphis.core.interfaces import (
    Candidate,
    ContextPacker,
    ContextUsage,
    GraphLayer,
    GraphTraversalPlan,
    GraphTraversalPolicy,
    CandidateDepthPolicy,
    MemoryType,
    MemoryRecord,
    PackedChunk,
    PlannedQuery,
    QueryPlanner,
    Reranker,
    RetrievalPlan,
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
from engraphis.core.query_planner import (
    DeterministicQueryPlanner,
    MAX_PLANNED_PRIORITY,
    MAX_PLANNED_QUERIES,
    PLANNING_MODES,
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
    context_revision: str = ""
    planning_mode: str = "off"
    planning_details: Optional[dict[str, Any]] = None
    graph_traversal_details: Optional[list[dict[str, Any]]] = None
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
                 candidate_depth_policy: Optional[CandidateDepthPolicy] = None,
                 graph_traversal_policy: Optional[GraphTraversalPolicy] = None,
                 query_planner: Optional[QueryPlanner] = None,
                 planner_timeout_s: float = 2.0) -> None:
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
        self.graph_traversal_policy = graph_traversal_policy or UniformGraphTraversalPolicy()
        self.query_planner = query_planner or DeterministicQueryPlanner()
        self.planner_timeout_s = max(0.0, float(planner_timeout_s))
        self._planner_slot = threading.BoundedSemaphore(1)
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
               planning: str = "off",
               mtype_limits: Optional[dict] = None,
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
        planning_mode = str(planning or "off").strip().casefold()
        if planning_mode not in PLANNING_MODES:
            choices = ", ".join(sorted(PLANNING_MODES))
            raise ValueError(f"planning must be one of: {choices}")
        caller_limits = _normalize_mtype_limits(mtype_limits)
        plan, planner_fallback = self._plan_queries(
            query,
            flt,
            selected_profile=selected_profile,
            planning_mode=planning_mode,
        )
        effective_limits = dict(plan.mtype_limits)
        effective_limits.update(caller_limits)
        planned_queries = list(plan.queries)

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
        run_configs = [
            config if index == 0 and arm_config is not None else profile_config(item.profile)
            for index, item in enumerate(planned_queries)
        ]
        embedded_texts = [
            item.text for item, run_config in zip(planned_queries, run_configs)
            if run_config.vector
        ]
        embedded = self.embedder.embed(embedded_texts) if embedded_texts else []
        embedded_iter = iter(embedded)
        query_vectors = [
            next(embedded_iter) if run_config.vector else None
            for run_config in run_configs
        ]

        while True:
            query_runs = []
            for item, run_config, qvec in zip(
                planned_queries, run_configs, query_vectors
            ):
                query_filter = _planned_filter(flt, item.mtypes)
                if query_filter is None:
                    query_runs.append({
                        "query": item,
                        "config": run_config,
                        "vector": {},
                        "lexical": {},
                        "graph": {},
                        "code": {},
                    })
                    continue
                vec = (
                    dict(self.index.search(qvec, arm_candidate_k, filter=query_filter))
                    if qvec is not None else {}
                )
                lex = (
                    dict(self.store.fts_search(
                        item.text, arm_candidate_k, filter=query_filter
                    ))
                    if run_config.lexical else {}
                )
                graph_plan, graph_policy_fallback = (
                    self._plan_graph_traversal(item.text, query_filter)
                    if run_config.graph else (None, "")
                )
                graph = (
                    self._graph_arm(
                        item.text,
                        query_filter,
                        now,
                        candidate_k=arm_candidate_k,
                        traversal_plan=graph_plan,
                        prompt_only=prompt_only,
                    )
                    if run_config.graph else {}
                )
                code = (
                    self._code_arm(
                        item.text,
                        query_filter,
                        arm_candidate_k,
                        historical=requested_historical,
                    )
                    if run_config.code else {}
                )
                query_runs.append({
                    "query": item,
                    "config": run_config,
                    "vector": vec,
                    "lexical": lex,
                    "graph": graph,
                    "code": code,
                    "graph_traversal_plan": graph_plan,
                    "graph_traversal_policy": getattr(
                        self.graph_traversal_policy,
                        "identity",
                        type(self.graph_traversal_policy).__name__,
                    ),
                    "graph_traversal_fallback": graph_policy_fallback,
                })

            # Sorted, not raw set order: a set of ids iterates in hash order, which varies
            # with PYTHONHASHSEED, so equal-scored results used to come back in a different
            # order in every process. One batched lookup replaces per-id lookups.
            candidate_ids = sorted({
                memory_id
                for run in query_runs
                for arm in ("vector", "lexical", "graph", "code")
                for memory_id in run[arm]
            })
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
                enabled and len(run[arm]) >= arm_candidate_k
                for run in query_runs
                for arm, enabled in (
                    ("vector", run["config"].vector),
                    ("lexical", run["config"].lexical),
                    ("graph", run["config"].graph),
                    ("code", run["config"].code),
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
                # This is the page depth actually used by the retrieval arms.  A
                # prompt-only recall may have widened it to find approved evidence.
                candidate_k_used=arm_candidate_k,
                candidate_depth_reason=candidate_depth_reason,
                retrieval_trace=[] if diagnostics else None,
                context_revision=_context_revision(usage, packed, context),
                planning_mode=planning_mode,
                planning_details=(
                    _planning_details(
                        plan,
                        query_runs,
                        recs,
                        effective_limits,
                        [],
                        planner_fallback,
                        getattr(self.query_planner, "identity", type(self.query_planner).__name__),
                        rerank_pool_size=0,
                        available_candidates=0,
                    ) if diagnostics else None
                ),
                graph_traversal_details=(
                    _graph_traversal_details(query_runs) if diagnostics else None
                ),
                token_counter=getattr(self.context_packer, "count_tokens", None),
            )

        arm_state, rrf = _fuse_query_runs(query_runs, recs)
        primary_vec = query_runs[0]["vector"]

        # ── six-term weighted score (+ small RRF nudge for cross-arm agreement) ──
        scored: list[Candidate] = []
        score_details: dict[str, dict[str, Any]] = {}
        for mid, rec in recs.items():
            w = self.weights.get(rec.mtype, scoring.Weights())
            adjusted_semantic = arm_state["adjusted"]["semantic"].get(mid, 0.0)
            adjusted_lexical = arm_state["adjusted"]["lexical"].get(mid, 0.0)
            adjusted_graph = arm_state["adjusted"]["graph"].get(mid, 0.0)
            adjusted_code = arm_state["adjusted"]["code"].get(mid, 0.0)
            semantic_score = max(adjusted_semantic, adjusted_code)
            base = scoring.score_memory(
                rec, now=now, weights=w,
                semantic=semantic_score, lexical=adjusted_lexical,
                graph=adjusted_graph, recency_tau_days=self.recency_tau_days,
            )
            arms = [
                name for name in ("semantic", "lexical", "graph", "code")
                if mid in arm_state["raw"][name]
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
                    "semantic": arm_state["raw"]["semantic"].get(mid),
                    "lexical": arm_state["raw"]["lexical"].get(mid),
                    "graph": arm_state["raw"]["graph"].get(mid),
                    "code": arm_state["raw"]["code"].get(mid),
                },
                "normalized": {
                    "semantic": arm_state["normalized"]["semantic"].get(mid, 0.0),
                    "lexical": arm_state["normalized"]["lexical"].get(mid, 0.0),
                    "graph": arm_state["normalized"]["graph"].get(mid, 0.0),
                    "code": arm_state["normalized"]["code"].get(mid, 0.0),
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
        # Type limits need candidates beyond the ordinary top-4k window, but sending
        # the complete multi-query union to a cross-encoder creates an avoidable
        # latency/cost hazard. Add the best pre-rerank candidates required to fill k
        # from every eligible memory type; with four types this remains <= 8k.
        pool = _type_aware_rerank_pool(scored, effective_limits, k=max(0, int(k)))
        rerank_k = len(pool) if effective_limits else k
        if self.reranker:
            fused_before = {candidate.id: candidate.score for candidate in pool}
            reranked = self.reranker.rerank(query, pool, rerank_k)
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
            ranked_final = reranked
            for candidate in ranked_final:
                detail = score_details[candidate.id]
                detail["rerank_score"] = rerank_raw.get(candidate.id)
                detail["calibrated_score"] = candidate.score
        else:
            ranked_final = pool

        final, type_limit_drops = _apply_mtype_limits(
            ranked_final, effective_limits, k=max(0, int(k))
        )

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
                semantic_cosine=primary_vec.get(candidate.id, 0.0),
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
            # Report the final, post-widening arm depth rather than the policy's
            # initial candidate depth.  This is diagnostic telemetry, not a limit.
            candidate_k_used=arm_candidate_k,
            candidate_depth_reason=candidate_depth_reason,
            retrieval_trace=trace,
            context_revision=_context_revision(usage, packed_chunks, context),
            planning_mode=planning_mode,
            planning_details=(
                _planning_details(
                    plan,
                    query_runs,
                    recs,
                    effective_limits,
                    type_limit_drops,
                    planner_fallback,
                    getattr(self.query_planner, "identity", type(self.query_planner).__name__),
                    rerank_pool_size=len(pool),
                    available_candidates=len(scored),
                ) if diagnostics else None
            ),
            graph_traversal_details=(
                _graph_traversal_details(query_runs) if diagnostics else None
            ),
            token_counter=getattr(self.context_packer, "count_tokens", None),
            source_metadata={
                candidate.id: _source_safety_metadata(candidate.record)
                for candidate in final
                if candidate.record is not None
            },
        )

    def _plan_queries(
        self,
        query: str,
        flt: SearchFilter,
        *,
        selected_profile: str,
        planning_mode: str,
    ) -> tuple[RetrievalPlan, str]:
        identity = RetrievalPlan((PlannedQuery(query, 1, selected_profile),))
        if planning_mode == "off":
            return identity, ""
        try:
            # Query planning is not a policy boundary. SearchFilter is mutable for
            # legacy compatibility, so never expose the live retrieval filter to an
            # injected planner. Clone its collection fields as well to prevent an
            # in-place list mutation from widening the real query.
            planner_filter = replace(
                flt,
                scopes=list(flt.scopes) if flt.scopes is not None else None,
                mtypes=list(flt.mtypes) if flt.mtypes is not None else None,
                graph_layers=(
                    list(flt.graph_layers) if flt.graph_layers is not None else None
                ),
            )
            proposed = self._run_planner(query, planner_filter)
            return _sanitize_plan(proposed, query, selected_profile), ""
        except Exception as exc:
            return identity, _planner_fallback_reason(exc)

    def _run_planner(self, query: str, planner_filter: SearchFilter) -> RetrievalPlan:
        """Enforce the planner deadline even for a non-cooperative injected backend.

        Python cannot safely kill an arbitrary running function. A single daemon
        worker therefore owns the planner slot; recall returns the identity route
        on deadline, and further calls fail open until the timed-out worker exits.
        This bounds caller latency and prevents an accumulation of stuck threads.
        """
        if self.planner_timeout_s <= 0 or not self._planner_slot.acquire(blocking=False):
            raise TimeoutError("planner deadline unavailable")
        outcome: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

        def invoke() -> None:
            try:
                outcome.put((True, self.query_planner.plan(
                    query,
                    filter=planner_filter,
                    timeout_s=self.planner_timeout_s,
                )))
            except Exception as exc:
                outcome.put((False, exc))
            finally:
                self._planner_slot.release()

        worker = threading.Thread(
            target=invoke,
            name="engraphis-query-planner",
            daemon=True,
        )
        worker.start()
        worker.join(self.planner_timeout_s)
        if worker.is_alive():
            raise TimeoutError("planner deadline exceeded")
        try:
            succeeded, value = outcome.get_nowait()
        except queue.Empty as exc:
            raise RuntimeError("planner terminated without a result") from exc
        if succeeded:
            return value
        raise value

    def _plan_graph_traversal(
        self,
        query: str,
        flt: SearchFilter,
    ) -> tuple[GraphTraversalPlan, str]:
        """Return an injected policy plan or fail closed to uniform traversal.

        Traversal policy is a soft ranking enhancement, never an availability or
        authorization boundary.  A broken optional policy therefore must not make
        local recall unavailable or change the established uniform PPR fallback.
        """
        try:
            # Policies may receive filter context to explain a plan, but they are
            # not an authority boundary. SearchFilter remains mutable for legacy
            # compatibility, so never expose the live retrieval filter to an
            # injected policy: a buggy/malicious implementation must not widen
            # scope, erase temporal anchors, or loosen graph-layer constraints.
            policy_filter = replace(
                flt,
                scopes=list(flt.scopes) if flt.scopes is not None else None,
                mtypes=list(flt.mtypes) if flt.mtypes is not None else None,
                graph_layers=(
                    list(flt.graph_layers) if flt.graph_layers is not None else None
                ),
            )
            proposed = self.graph_traversal_policy.plan(query, filter=policy_filter)
        except Exception:
            return GraphTraversalPlan(reason_codes=("policy_unavailable",)), "policy_unavailable"
        if not isinstance(proposed, GraphTraversalPlan):
            return GraphTraversalPlan(reason_codes=("invalid_policy_output",)), "invalid_policy_output"
        try:
            # Rebuild a base plan rather than invoking a subclass's method in
            # the hot path. This validates finite, unique weights and prevents
            # an injected subclass from changing multiplier semantics.
            plan = GraphTraversalPlan(
                intent=proposed.intent,
                layer_weights=proposed.layer_weights,
                reason_codes=proposed.reason_codes,
            )
        except Exception:
            return GraphTraversalPlan(reason_codes=("invalid_policy_output",)), "invalid_policy_output"
        return plan, ""

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
        traversal_plan: Optional[GraphTraversalPlan] = None,
        prompt_only: bool = False,
    ) -> dict[str, float]:
        if flt.graph_layers is not None and not flt.graph_layers:
            return {}
        if self.graph_mode == "1hop":
            return self._graph_arm_1hop(
                query, flt, now, candidate_k=candidate_k, prompt_only=prompt_only,
            )
        return self._graph_arm_ppr(
            query,
            flt,
            now,
            candidate_k=candidate_k,
            traversal_plan=traversal_plan,
            prompt_only=prompt_only,
        )

    def _prompt_eligible_memory_ids(self, memory_ids: set[str]) -> set[str]:
        """Return only approved, non-quarantined memory nodes for prompt PPR."""
        if not memory_ids:
            return set()
        records = self.store.get_memories(sorted(memory_ids))
        return {
            memory_id
            for memory_id, record in records.items()
            if prompt_eligible(record.provenance, record.metadata)
        }

    @staticmethod
    def _edge_source_memory_ids(edge) -> set[str]:
        provenance = edge.provenance if isinstance(edge.provenance, dict) else {}
        values = [provenance.get("memory_id")]
        many = provenance.get("memory_ids")
        if isinstance(many, (list, tuple, set)):
            values.extend(many)
        return {str(value) for value in values if value}

    def _prompt_eligible_edges(self, edges: list) -> list:
        """Keep direct edges and edges whose every memory support is prompt-eligible."""
        source_ids = (
            set().union(*(self._edge_source_memory_ids(edge) for edge in edges))
            if edges else set()
        )
        eligible_ids = self._prompt_eligible_memory_ids(source_ids)
        return [
            edge for edge in edges
            if not (sources := self._edge_source_memory_ids(edge))
            or sources <= eligible_ids
        ]

    def _graph_arm_ppr(
        self,
        query: str,
        flt: SearchFilter,
        now: float,
        *,
        candidate_k: int = 50,
        traversal_plan: Optional[GraphTraversalPlan] = None,
        prompt_only: bool = False,
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

        if not isinstance(traversal_plan, GraphTraversalPlan):
            traversal_plan, _ = self._plan_graph_traversal(query, flt)
        ent = "ent::{}".format
        adj: dict[str, list[tuple[str, float]]] = {}

        def connect(a: str, b: str, w: float, layer: GraphLayer) -> None:
            weighted = max(float(w or 1.0), 1e-6) * traversal_plan.multiplier(layer)
            adj.setdefault(a, []).append((b, weighted))
            adj.setdefault(b, []).append((a, weighted))

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
            edges = self.store.neighbors(
                batch, at=now, layers=flt.graph_layers, flt=flt,
                limit=edge_cap - len(edges_by_id),
            )
            if prompt_only:
                edges = self._prompt_eligible_edges(edges)
            for edge in edges:
                if edge.id in edges_by_id:
                    continue
                edges_by_id[edge.id] = edge
                next_frontier.update((edge.src, edge.dst))
                if len(edges_by_id) >= edge_cap:
                    break
            frontier.update(next_frontier - expanded)
        for e in edges_by_id.values():
            connect(
                ent(e.src),
                ent(e.dst),
                max(float(e.weight or 1.0), 1e-6),
                e.layer or GraphLayer.SEMANTIC,
            )

        # Query only the entity frontier before applying the incidence cap. A
        # global confidence/ID prefix can otherwise omit a memory attached to a
        # seeded or reached entity in a large scope.
        incidence_entity_ids = sorted({
            *seeds,
            *(endpoint for edge in edges_by_id.values() for endpoint in (edge.src, edge.dst)),
        })
        incidence = self.store.list_memory_entities(
            flt, entity_ids=incidence_entity_ids, limit=12_000, prompt_only=prompt_only,
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
        memory_ids = incidence_memory_ids | {
            endpoint
            for link in frontier_links
            for endpoint in (link["a"], link["b"])
        } | {
            memory.id for memory in self.store.list_memories(
                flt, limit=12_000, prompt_only=prompt_only,
            )
        }
        if prompt_only:
            memory_ids = self._prompt_eligible_memory_ids(memory_ids)
            incidence = [
                row for row in incidence
                if str(row.get("memory_id") or "") in memory_ids
            ]
            frontier_links = [
                link for link in frontier_links
                if link["a"] in memory_ids and link["b"] in memory_ids
            ]
        memory_ids = sorted(memory_ids)
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
            # Incidence is a structural memory↔entity bridge, not an inferred
            # entity relation.  Preferencing a causal/temporal relation must not
            # downweight the only path that reaches its supporting memory.
            adj.setdefault(memory_id, []).append((ent(entity_id), confidence))
            adj.setdefault(ent(entity_id), []).append((memory_id, confidence))
        for link in self.store.links_among(
            memory_ids,
            layers=flt.graph_layers,
            flt=flt,
            limit=20_000,
        ):
            connect(
                link["a"],
                link["b"],
                1.0,
                GraphLayer(str(link.get("layer") or GraphLayer.SEMANTIC.value)),
            )

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
        prompt_only: bool = False,
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
        edges = self.store.neighbors(
            seed_ids, at=now, layers=flt.graph_layers, flt=flt
        )
        if prompt_only:
            edges = self._prompt_eligible_edges(edges)
        for edge in edges:
            related_ids.add(edge.src)
            related_ids.add(edge.dst)
        rows = self.store.list_memory_entities(
            flt, entity_ids=sorted(related_ids), limit=12_000, prompt_only=prompt_only,
        )
        eligible_ids = (
            self._prompt_eligible_memory_ids({
                str(row.get("memory_id") or "")
                for row in rows if row.get("memory_id")
            })
            if prompt_only else None
        )
        out: dict[str, float] = {}
        if rows:
            for row in rows:
                memory_id = str(row.get("memory_id") or "")
                if memory_id and (eligible_ids is None or memory_id in eligible_ids):
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


def _sanitize_plan(
    proposed: RetrievalPlan,
    original_query: str,
    selected_profile: str,
) -> RetrievalPlan:
    """Validate an untrusted planner result and restore the mandatory identity route."""
    if not isinstance(proposed, RetrievalPlan):
        raise ValueError("planner must return RetrievalPlan")
    # The mandatory route must be the caller's exact query, matching planning-off
    # behavior. Use a whitespace-normalized key only for duplicate detection.
    original = str(original_query or "")
    queries = [PlannedQuery(original, 1, selected_profile)]
    seen = {" ".join(original.split()).casefold()}
    candidates = []
    for position, item in enumerate(proposed.queries):
        if not isinstance(item, PlannedQuery):
            raise ValueError("planner queries must be PlannedQuery values")
        text = " ".join(str(item.text or "").split())[:2048]
        if not text or text.casefold() in seen:
            continue
        if isinstance(item.priority, bool) or not isinstance(item.priority, int):
            raise ValueError("planned query priority must be a positive integer")
        priority = min(MAX_PLANNED_PRIORITY, max(2, item.priority))
        profile = str(item.profile or "balanced").strip().casefold()
        if profile not in {"balanced", "lexical", "graph", "code"}:
            raise ValueError("planned query profile is invalid")
        mtypes = tuple(dict.fromkeys(MemoryType(value) for value in item.mtypes))
        candidates.append((priority, position, PlannedQuery(text, priority, profile, mtypes)))
        seen.add(text.casefold())
    candidates.sort(key=lambda value: (value[0], value[1], value[2].text.casefold()))
    for _, _, item in candidates[: MAX_PLANNED_QUERIES - 1]:
        queries.append(item)
    reasons = tuple(
        str(reason).strip()[:80]
        for reason in proposed.reason_codes[:8]
        if str(reason).strip()
    )
    return RetrievalPlan(
        tuple(queries),
        _normalize_mtype_limits(proposed.mtype_limits),
        reasons,
    )


def _planner_fallback_reason(exc: Exception) -> str:
    """Map planner failures to stable diagnostics without reflecting provider data."""
    if isinstance(exc, TimeoutError):
        return "planner_timeout"
    if isinstance(exc, (TypeError, ValueError)):
        return "invalid_planner_output"
    return "planner_unavailable"


def _normalize_mtype_limits(values: Optional[dict]) -> dict[MemoryType, int]:
    if values is None:
        return {}
    if not isinstance(values, dict):
        raise ValueError("mtype_limits must be an object of memory type to maximum count")
    normalized = {}
    for raw_key, raw_limit in values.items():
        try:
            key = MemoryType(raw_key)
        except (TypeError, ValueError) as exc:
            choices = ", ".join(item.value for item in MemoryType)
            raise ValueError(f"mtype_limits keys must be one of: {choices}") from exc
        if isinstance(raw_limit, bool) or not isinstance(raw_limit, int):
            raise ValueError("mtype_limits values must be non-negative integers")
        limit = raw_limit
        if limit < 0:
            raise ValueError("mtype_limits values must be non-negative integers")
        normalized[key] = limit
    return normalized


def _planned_filter(
    flt: SearchFilter,
    mtypes: tuple[MemoryType, ...],
) -> Optional[SearchFilter]:
    if not mtypes:
        return flt
    allowed = set(mtypes)
    if flt.mtypes is not None:
        allowed &= {MemoryType(value) for value in flt.mtypes}
    if not allowed:
        return None
    ordered = [item for item in MemoryType if item in allowed]
    return replace(flt, mtypes=ordered)


def _fuse_query_runs(
    query_runs: list[dict[str, Any]],
    recs: dict[str, MemoryRecord],
) -> tuple[dict[str, dict[str, dict[str, float]]], dict[str, float]]:
    """Fuse query/arm rankings with priority-weighted RRF.

    Each arm is normalized within its own planned query before profile scaling.
    The best contribution per arm feeds the established six-term scorer; agreement
    across queries and arms is represented separately by weighted RRF.
    """
    names = {
        "vector": "semantic",
        "lexical": "lexical",
        "graph": "graph",
        "code": "code",
    }
    state = {
        category: {name: {} for name in names.values()}
        for category in ("raw", "normalized", "adjusted")
    }
    rrf: dict[str, float] = {}
    for run in query_runs:
        item = run["query"]
        config = run["config"]
        priority_weight = 1.0 / max(1, int(item.priority))
        for source_name, output_name in names.items():
            raw = {mid: score for mid, score in run[source_name].items() if mid in recs}
            normalized = scoring.normalize(raw)
            scale = getattr(config, f"{output_name}_scale")
            bonus = getattr(config, f"{output_name}_presence_bonus", 0.0)
            for mid, value in raw.items():
                state["raw"][output_name][mid] = max(
                    state["raw"][output_name].get(mid, float("-inf")),
                    float(value),
                )
                state["normalized"][output_name][mid] = max(
                    state["normalized"][output_name].get(mid, 0.0),
                    normalized.get(mid, 0.0),
                )
                adjusted = (normalized.get(mid, 0.0) * scale + bonus) * priority_weight
                state["adjusted"][output_name][mid] = max(
                    state["adjusted"][output_name].get(mid, 0.0),
                    adjusted,
                )
            for rank, mid in enumerate(_ranked(raw, recs)):
                rrf[mid] = rrf.get(mid, 0.0) + priority_weight / (60 + rank + 1)
    return state, rrf


def _apply_mtype_limits(
    candidates: list[Candidate],
    limits: dict[MemoryType, int],
    *,
    k: int,
) -> tuple[list[Candidate], list[dict[str, Any]]]:
    selected = []
    counts: dict[MemoryType, int] = {}
    drops = []
    for candidate in candidates:
        if len(selected) >= k:
            break
        if candidate.record is None:
            continue
        mtype = candidate.record.mtype
        limit = limits.get(mtype)
        if limit is not None and counts.get(mtype, 0) >= limit:
            drops.append({"id": candidate.id, "mtype": mtype.value, "limit": limit})
            continue
        selected.append(candidate)
        counts[mtype] = counts.get(mtype, 0) + 1
    return selected, drops


def _type_aware_rerank_pool(
    candidates: list[Candidate],
    limits: dict[MemoryType, int],
    *,
    k: int,
) -> list[Candidate]:
    """Return a bounded pool that can still fill every eligible memory-type slot."""
    if k <= 0:
        return []
    ordinary = list(candidates[: max(k * 4, k)])
    if not limits:
        return ordinary
    selected_ids = {candidate.id for candidate in ordinary}
    per_type: dict[MemoryType, int] = {}
    needed = {
        mtype: min(k, limits.get(mtype, k))
        for mtype in MemoryType
    }
    for candidate in ordinary:
        if candidate.record is not None:
            mtype = candidate.record.mtype
            per_type[mtype] = per_type.get(mtype, 0) + 1
    for candidate in candidates[len(ordinary):]:
        if candidate.record is None or candidate.id in selected_ids:
            continue
        mtype = candidate.record.mtype
        if per_type.get(mtype, 0) >= needed[mtype]:
            continue
        ordinary.append(candidate)
        selected_ids.add(candidate.id)
        per_type[mtype] = per_type.get(mtype, 0) + 1
        if all(per_type.get(value, 0) >= count for value, count in needed.items()):
            break
    return ordinary


def _context_revision(
    usage: ContextUsage,
    packed: list[PackedChunk],
    context: str,
) -> str:
    payload = {
        "token_counter": usage.token_counter,
        "packed": [[chunk.id, chunk.excerpt] for chunk in packed],
        # Headers (including titles) are part of the emitted prompt but not part
        # of PackedChunk.excerpt. Hash the exact prompt text as well so any host-
        # visible change necessarily produces a new revision.
        "context": context,
    }
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _planning_details(
    plan: RetrievalPlan,
    query_runs: list[dict[str, Any]],
    recs: dict[str, MemoryRecord],
    limits: dict[MemoryType, int],
    drops: list[dict[str, Any]],
    fallback: str,
    planner_identity: str,
    *,
    rerank_pool_size: int,
    available_candidates: int,
) -> dict[str, Any]:
    rankings = []
    for run in query_runs:
        item = run["query"]
        rankings.append({
            "text": item.text,
            "priority": item.priority,
            "profile": item.profile,
            "mtypes": [value.value for value in item.mtypes],
            "rankings": {
                name: _ranked(run[source], recs)
                for source, name in (
                    ("vector", "semantic"),
                    ("lexical", "lexical"),
                    ("graph", "graph"),
                    ("code", "code"),
                )
            },
        })
    return {
        "planner": str(planner_identity),
        "reason_codes": list(plan.reason_codes),
        "queries": rankings,
        "mtype_limits": {key.value: value for key, value in limits.items()},
        "type_limit_drops": drops,
        "fallback_reason": fallback or None,
        "rerank_pool": {
            "strategy": "type_aware_bounded" if limits else "top_4k",
            "size": rerank_pool_size,
            "available_candidates": available_candidates,
        },
    }


def _graph_traversal_details(query_runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Expose bounded graph-policy decisions only in diagnostic recall results."""
    details = []
    for run in query_runs:
        plan = run.get("graph_traversal_plan")
        if not isinstance(plan, GraphTraversalPlan):
            continue
        candidates = sorted(
            run["graph"].items(), key=lambda item: (-item[1], item[0])
        )[:50]
        details.append({
            "query": run["query"].text,
            "policy": str(run.get("graph_traversal_policy") or "unknown"),
            "plan": plan.as_dict(),
            "fallback_reason": run.get("graph_traversal_fallback") or None,
            "candidate_scores": [
                {"id": memory_id, "score": round(float(score), 8)}
                for memory_id, score in candidates
            ],
        })
    return details


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
    raw_semantic = float(semantic_cosine)
    semantic = max(0.0, min(1.0, raw_semantic)) if math.isfinite(raw_semantic) else 0.0
    # Titles improve candidate discovery, but are metadata rather than answer-bearing
    # evidence.  Keeping them out of the absolute gate aligns adaptive routing with
    # grounded recall and prevents a keyword-stuffed title from qualifying garbage.
    lexical = jaccard(tokenize(query), tokenize(content or ""))
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
