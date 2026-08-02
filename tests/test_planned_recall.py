"""Contracts for opt-in multi-query planning, type caps, and context revisions."""
from __future__ import annotations

import time

import numpy as np
import pytest

from engraphis.backends import DeterministicEmbedder, NumpyVectorIndex
from engraphis.backends.query_planner import LLMQueryPlanner
from engraphis.backends.reranker import IdentityReranker
from engraphis.core import grounded
from engraphis.core.interfaces import (
    MemoryRecord,
    MemoryType,
    PlannedQuery,
    RetrievalPlan,
    Scope,
    SearchFilter,
)
from engraphis.core.query_planner import DeterministicQueryPlanner
from engraphis.core.recall import RecallEngine
from engraphis.core.store import Store


class _StaticPlanner:
    identity = "tests.static-planner.v1"

    def __init__(self, result):
        self.result = result
        self.calls = 0

    def plan(self, query, *, filter=None, timeout_s=None):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class _MappedEmbedder:
    dim = 2

    def __init__(self, vectors):
        self.vectors = vectors

    def embed(self, texts, *, kind="text"):
        del kind
        return np.asarray([self.vectors[text] for text in texts], dtype=np.float32)


class _StructuredPlannerLLM:
    def __init__(self):
        self.timeout = None

    def extract_json(self, prompt, schema, **kwargs):
        assert "Keep the original query first" in prompt
        assert schema["properties"]["queries"]["maxItems"] == 3
        self.timeout = kwargs.get("timeout")
        return {
            "queries": [
                {"text": "original", "priority": 1, "profile": "balanced"},
                {"text": "RELEASE_GATE", "priority": 2, "profile": "lexical",
                 "mtypes": ["procedural"]},
            ],
            "mtype_limits": {"working": 1},
            "reason_codes": ["exact_identifier"],
        }


class _MutatingPlanner:
    identity = "tests.mutating-planner.v1"

    def __init__(self, other_repo):
        self.other_repo = other_repo

    def plan(self, query, *, filter=None, timeout_s=None):
        del timeout_s
        filter.repo_id = self.other_repo
        filter.valid_at = None
        filter.known_at = None
        if filter.mtypes is not None:
            filter.mtypes.clear()
        return RetrievalPlan((PlannedQuery("private override", 2, "lexical"),))


class _BlockingPlanner:
    identity = "tests.blocking-planner.v1"

    def plan(self, query, *, filter=None, timeout_s=None):
        del query, filter, timeout_s
        time.sleep(0.25)
        return RetrievalPlan(())


class _CountingReranker:
    def __init__(self):
        self.max_seen = 0

    def rerank(self, query, candidates, top_k):
        del query
        self.max_seen = max(self.max_seen, len(candidates))
        return list(candidates[:top_k])


def _engine(planner=None):
    store = Store(":memory:")
    embedder = DeterministicEmbedder(256)
    engine = RecallEngine(
        store,
        embedder,
        NumpyVectorIndex(store),
        IdentityReranker(),
        query_planner=planner,
    )
    workspace = store.get_or_create_workspace("planned")
    repo = store.get_or_create_repo(workspace, "recall")
    return store, embedder, engine, workspace, repo


def _add(store, embedder, workspace, repo, content, **kwargs):
    # These are direct Store fixtures for retrieval/planning behavior. Mark them
    # as already reviewed so the test exercises routing rather than the separate
    # public-ingress review gate.
    provenance = dict(kwargs.pop("provenance", {}) or {})
    provenance.setdefault("source", "test")
    provenance.setdefault("trusted", True)
    if provenance.get("trusted") is True:
        provenance.setdefault("review_state", "approved")
    return store.add_memory(MemoryRecord(
        id="",
        content=content,
        workspace_id=workspace,
        repo_id=repo,
        scope=Scope.REPO,
        embedding=embedder.embed([content])[0],
        provenance=provenance,
        **kwargs,
    ))


def test_deterministic_planner_keeps_original_and_bounds_additional_queries():
    query = 'Why does "DEPLOY_TOKEN" depend on ReleaseGate in this session?'
    plan = DeterministicQueryPlanner().plan(query)

    assert plan.queries[0].text == query
    assert plan.queries[0].priority == 1
    assert 1 < len(plan.queries) <= 3
    assert len({item.text.casefold() for item in plan.queries}) == len(plan.queries)
    assert plan.queries[1].profile == "lexical"
    assert "exact_term" in plan.reason_codes
    assert "relationship_intent" in plan.reason_codes


def test_planning_off_never_invokes_injected_planner_and_preserves_results():
    planner = _StaticPlanner(TimeoutError("should not run"))
    store, embedder, engine, workspace, repo = _engine(planner)
    memory_id = _add(store, embedder, workspace, repo, "Deployments use ReleaseGate.")
    flt = SearchFilter(workspace_id=workspace, repo_id=repo)

    implicit = engine.recall("How do deployments work?", flt, k=1)
    explicit = engine.recall("How do deployments work?", flt, k=1, planning="off")

    assert planner.calls == 0
    assert [item["id"] for item in implicit.chunks] == [memory_id]
    assert explicit.context == implicit.context
    assert explicit.context_revision == implicit.context_revision


def test_auto_plan_is_sanitized_to_original_plus_two_unique_queries():
    query = "Where is the deployment path?"
    planner = _StaticPlanner(RetrievalPlan((
        PlannedQuery("not the original", 9, "code"),
        PlannedQuery("ReleaseGate", 2, "lexical"),
        PlannedQuery("releasegate", 3, "graph"),
        PlannedQuery("extra ignored", 4, "balanced"),
    )))
    store, embedder, engine, workspace, repo = _engine(planner)
    _add(store, embedder, workspace, repo, "ReleaseGate controls the deployment path.")

    result = engine.recall(
        query,
        SearchFilter(workspace_id=workspace, repo_id=repo),
        planning="auto",
        diagnostics=True,
    )

    details = result.planning_details
    assert planner.calls == 1
    assert result.planning_mode == "auto"
    assert [item["text"] for item in details["queries"]] == [
        query,
        "ReleaseGate",
        "extra ignored",
    ]
    assert details["queries"][0]["priority"] == 1


def test_auto_plan_preserves_the_exact_original_query_route():
    query = "  Where is\nReleaseGate configured?  "
    planner = _StaticPlanner(RetrievalPlan((
        PlannedQuery("Where is ReleaseGate configured?", 2, "lexical"),
    )))
    store, embedder, engine, workspace, repo = _engine(planner)
    _add(store, embedder, workspace, repo, "ReleaseGate is configured in release.toml.")

    result = engine.recall(
        query,
        SearchFilter(workspace_id=workspace, repo_id=repo),
        planning="auto",
        diagnostics=True,
    )

    assert result.planning_details["queries"][0]["text"] == query
    assert len(result.planning_details["queries"]) == 1


def test_planner_failure_falls_back_to_identity_without_failing_recall():
    planner = _StaticPlanner(TimeoutError("planner deadline exceeded"))
    store, embedder, engine, workspace, repo = _engine(planner)
    memory_id = _add(store, embedder, workspace, repo, "The release manager approves deploys.")

    result = engine.recall(
        "Who approves deploys?",
        SearchFilter(workspace_id=workspace, repo_id=repo),
        planning="auto",
        diagnostics=True,
    )

    assert [item["id"] for item in result.chunks] == [memory_id]
    assert len(result.planning_details["queries"]) == 1
    assert result.planning_details["fallback_reason"] == "planner_timeout"


def test_planner_fallback_diagnostics_do_not_reflect_exception_details():
    secret = "provider-api-key-must-not-escape"
    planner = _StaticPlanner(RuntimeError(secret))
    store, embedder, engine, workspace, repo = _engine(planner)
    _add(store, embedder, workspace, repo, "The release manager approves deploys.")

    result = engine.recall(
        "Who approves deploys?",
        SearchFilter(workspace_id=workspace, repo_id=repo),
        planning="auto",
        diagnostics=True,
    )

    assert result.planning_details["fallback_reason"] == "planner_unavailable"
    assert secret not in repr(result.planning_details)


def test_non_cooperative_planner_is_timeboxed_at_the_recall_boundary():
    store, embedder, engine, workspace, repo = _engine(_BlockingPlanner())
    engine.planner_timeout_s = 0.02
    memory_id = _add(store, embedder, workspace, repo, "Release policy evidence.")

    started = time.perf_counter()
    result = engine.recall(
        "release policy",
        SearchFilter(workspace_id=workspace, repo_id=repo),
        planning="auto",
        diagnostics=True,
    )
    elapsed = time.perf_counter() - started

    assert elapsed < 0.15
    assert [item["id"] for item in result.chunks] == [memory_id]
    assert result.planning_details["fallback_reason"] == "planner_timeout"


@pytest.mark.parametrize("priority", [2.9, "2", True])
def test_invalid_planner_priority_falls_back_to_identity(priority):
    planner = _StaticPlanner(RetrievalPlan((
        PlannedQuery("ReleaseGate", priority, "lexical"),
    )))
    store, embedder, engine, workspace, repo = _engine(planner)
    _add(store, embedder, workspace, repo, "ReleaseGate controls deployments.")

    result = engine.recall(
        "deployment control",
        SearchFilter(workspace_id=workspace, repo_id=repo),
        planning="auto",
        diagnostics=True,
    )

    assert len(result.planning_details["queries"]) == 1
    assert result.planning_details["fallback_reason"] == "invalid_planner_output"


def test_sanitized_planner_priorities_remain_weighted_not_positional():
    planner = _StaticPlanner(RetrievalPlan((
        PlannedQuery("ReleaseGate", 2, "lexical"),
        PlannedQuery("deployment archive", 100, "balanced"),
    )))
    store, embedder, engine, workspace, repo = _engine(planner)
    _add(store, embedder, workspace, repo, "ReleaseGate controls deployments.")

    result = engine.recall(
        "deployment control",
        SearchFilter(workspace_id=workspace, repo_id=repo),
        planning="auto",
        diagnostics=True,
    )

    assert [item["priority"] for item in result.planning_details["queries"]] == [1, 2, 100]


def test_optional_llm_planner_is_injected_and_receives_deadline():
    llm = _StructuredPlannerLLM()
    plan = LLMQueryPlanner(llm).plan("original", timeout_s=0.75)

    assert llm.timeout == 0.75
    assert plan.queries[1].text == "RELEASE_GATE"
    assert plan.queries[1].mtypes == (MemoryType.PROCEDURAL,)
    assert plan.mtype_limits == {MemoryType.WORKING: 1}


def test_planner_details_are_emitted_only_with_diagnostics():
    store, embedder, engine, workspace, repo = _engine()
    _add(store, embedder, workspace, repo, "ReleaseGate controls deployments.")

    result = engine.recall(
        "Why does ReleaseGate control deployments?",
        SearchFilter(workspace_id=workspace, repo_id=repo),
        planning="auto",
    )

    assert result.planning_details is None
    assert result.retrieval_trace is None


def test_mtype_limits_are_maxima_applied_after_reranking():
    store, embedder, engine, workspace, repo = _engine()
    semantic_ids = [
        _add(
            store,
            embedder,
            workspace,
            repo,
            f"Deployment policy fact {index}.",
            mtype=MemoryType.SEMANTIC,
        )
        for index in range(3)
    ]
    procedural_id = _add(
        store,
        embedder,
        workspace,
        repo,
        "Deployment procedure steps for the release manager.",
        mtype=MemoryType.PROCEDURAL,
    )

    result = engine.recall(
        "deployment policy procedure",
        SearchFilter(workspace_id=workspace, repo_id=repo),
        k=4,
        mtype_limits={"semantic": 1},
        diagnostics=True,
    )

    returned = [item["id"] for item in result.chunks]
    assert procedural_id in returned
    assert len(set(returned) & set(semantic_ids)) == 1
    assert result.count == 2
    assert result.planning_details["type_limit_drops"]


def test_type_limits_can_intentionally_return_fewer_than_k_or_nothing():
    store, embedder, engine, workspace, repo = _engine()
    _add(
        store,
        embedder,
        workspace,
        repo,
        "Deployment is approved by ReleaseGate.",
        mtype=MemoryType.SEMANTIC,
    )

    result = engine.recall(
        "deployment ReleaseGate",
        SearchFilter(workspace_id=workspace, repo_id=repo),
        k=5,
        mtype_limits={MemoryType.SEMANTIC: 0},
    )

    assert result.count == 0
    assert result.context == ""


def test_type_limits_consider_candidates_beyond_the_default_rerank_pool():
    """A cap must not turn an available eligible result into an empty recall."""
    store, embedder, engine, workspace, repo = _engine()
    for index in range(5):
        _add(
            store,
            embedder,
            workspace,
            repo,
            f"Release policy {index}",
            mtype=MemoryType.SEMANTIC,
            importance=1.0,
        )
    procedural_id = _add(
        store,
        embedder,
        workspace,
        repo,
        "Release policy procedure",
        mtype=MemoryType.PROCEDURAL,
        importance=0.0,
    )

    result = engine.recall(
        "Release policy",
        SearchFilter(workspace_id=workspace, repo_id=repo),
        k=1,
        mtype_limits={MemoryType.SEMANTIC: 0},
    )

    assert [item["id"] for item in result.chunks] == [procedural_id]


def test_type_aware_rerank_pool_is_bounded_while_filling_eligible_types():
    store, embedder, engine, workspace, repo = _engine()
    reranker = _CountingReranker()
    engine.reranker = reranker
    for index in range(80):
        _add(
            store,
            embedder,
            workspace,
            repo,
            f"Release policy evidence {index}",
            mtype=MemoryType.SEMANTIC if index < 70 else MemoryType.PROCEDURAL,
            importance=1.0 if index < 70 else 0.0,
        )

    result = engine.recall(
        "Release policy evidence",
        SearchFilter(workspace_id=workspace, repo_id=repo),
        k=5,
        candidate_k=80,
        mtype_limits={MemoryType.SEMANTIC: 1},
        diagnostics=True,
    )

    assert sum(item["mtype"] == "semantic" for item in result.chunks) == 1
    assert any(item["mtype"] == "procedural" for item in result.chunks)
    assert reranker.max_seen <= 8 * 5
    assert result.planning_details["rerank_pool"]["size"] == reranker.max_seen


def test_context_revision_is_stable_and_changes_with_packed_excerpt():
    store, embedder, engine, workspace, repo = _engine()
    _add(
        store,
        embedder,
        workspace,
        repo,
        "ReleaseGate deployment evidence " + "with additional detail " * 30,
    )
    flt = SearchFilter(workspace_id=workspace, repo_id=repo)

    first = engine.recall("ReleaseGate deployment evidence", flt, token_budget=128)
    second = engine.recall("ReleaseGate deployment evidence", flt, token_budget=128)
    smaller = engine.recall("ReleaseGate deployment evidence", flt, token_budget=24)

    assert len(first.context_revision) == 64
    assert first.context_revision == second.context_revision
    assert smaller.context_revision != first.context_revision


def test_context_revision_changes_when_an_emitted_title_changes():
    store, embedder, engine, workspace, repo = _engine()
    memory_id = _add(
        store,
        embedder,
        workspace,
        repo,
        "ReleaseGate deployment evidence.",
        title="Original release title",
    )
    flt = SearchFilter(workspace_id=workspace, repo_id=repo)

    first = engine.recall("ReleaseGate deployment evidence", flt)
    store.conn.execute(
        "UPDATE memories SET title=? WHERE id=?",
        ("Updated release title", memory_id),
    )
    store.conn.commit()
    second = engine.recall("ReleaseGate deployment evidence", flt)

    assert first.context != second.context
    assert first.context_revision != second.context_revision


def test_planned_queries_cannot_escape_scope_or_prompt_trust_filters():
    planner = _StaticPlanner(RetrievalPlan((
        PlannedQuery("private override", 2, "lexical"),
    )))
    store, embedder, engine, workspace, repo = _engine(planner)
    other_repo = store.get_or_create_repo(workspace, "other")
    trusted_id = _add(store, embedder, workspace, repo, "Trusted deployment evidence.")
    _add(
        store,
        embedder,
        workspace,
        repo,
        "private override",
        provenance={"source": "import", "trusted": False},
    )
    _add(store, embedder, workspace, other_repo, "private override")

    result = engine.recall(
        "deployment evidence",
        SearchFilter(workspace_id=workspace, repo_id=repo),
        planning="auto",
        prompt_only=True,
        k=5,
    )

    assert [item["id"] for item in result.chunks] == [trusted_id]


def test_injected_planner_cannot_mutate_the_live_search_filter():
    store, embedder, engine, workspace, repo = _engine()
    other_repo = store.get_or_create_repo(workspace, "other")
    engine.query_planner = _MutatingPlanner(other_repo)
    trusted_id = _add(
        store,
        embedder,
        workspace,
        repo,
        "Trusted deployment evidence.",
        mtype=MemoryType.SEMANTIC,
        valid_from=100.0,
    )
    _add(
        store,
        embedder,
        workspace,
        other_repo,
        "private override",
        mtype=MemoryType.PROCEDURAL,
        valid_from=300.0,
    )
    known_at = time.time() + 1.0
    flt = SearchFilter(
        workspace_id=workspace,
        repo_id=repo,
        mtypes=[MemoryType.SEMANTIC],
        valid_at=200.0,
        known_at=known_at,
    )

    result = engine.recall("deployment evidence", flt, planning="auto", k=5)

    assert [item["id"] for item in result.chunks] == [trusted_id]
    assert flt.repo_id == repo
    assert flt.mtypes == [MemoryType.SEMANTIC]
    assert flt.valid_at == 200.0
    assert flt.known_at == known_at


def test_planned_queries_preserve_bitemporal_visibility():
    planner = _StaticPlanner(RetrievalPlan((
        PlannedQuery("production endpoint beta", 2, "lexical"),
    )))
    store, embedder, engine, workspace, repo = _engine(planner)
    old_id = _add(
        store,
        embedder,
        workspace,
        repo,
        "The production endpoint was alpha.",
        valid_from=100.0,
    )
    new_id = _add(
        store,
        embedder,
        workspace,
        repo,
        "The production endpoint was beta.",
        valid_from=200.0,
    )
    store.close_validity(old_id, at=200.0)
    store.conn.execute(
        "UPDATE memories SET ingested_at=100, valid_to_recorded_at=300 WHERE id=?",
        (old_id,),
    )
    store.conn.execute(
        "UPDATE memories SET ingested_at=300 WHERE id=?",
        (new_id,),
    )
    store.conn.commit()

    believed_before_correction = engine.recall(
        "What was the production endpoint?",
        SearchFilter(
            workspace_id=workspace,
            repo_id=repo,
            valid_at=250.0,
            known_at=250.0,
        ),
        planning="auto",
    )
    corrected_view = engine.recall(
        "What was the production endpoint?",
        SearchFilter(
            workspace_id=workspace,
            repo_id=repo,
            valid_at=250.0,
            known_at=350.0,
        ),
        planning="auto",
    )

    assert [item["id"] for item in believed_before_correction.chunks] == [old_id]
    assert [item["id"] for item in corrected_view.chunks] == [new_id]


def test_grounded_support_remains_anchored_to_original_query():
    planner = _StaticPlanner(RetrievalPlan((
        PlannedQuery("platypus habitat", 2, "lexical"),
    )))
    store, embedder, engine, workspace, repo = _engine(planner)
    _add(store, embedder, workspace, repo, "A platypus lives near freshwater rivers.")

    recalled = engine.recall(
        "What is the database backup schedule?",
        SearchFilter(workspace_id=workspace, repo_id=repo),
        planning="auto",
        k=3,
    )
    answer = grounded.build_grounded_answer(
        "What is the database backup schedule?",
        recalled,
        embedder,
    )

    assert recalled.count == 1
    assert answer.abstained is True
    assert answer.grounded is False


def test_planner_only_vector_candidate_gets_original_query_support():
    planner = _StaticPlanner(RetrievalPlan((
        PlannedQuery("target", 2, "lexical"),
    )))
    store = Store(":memory:")
    embedder = _MappedEmbedder({
        "original": [1.0, 0.0],
        "target": [0.95, 0.05],
        "distractor": [1.0, 0.0],
    })
    engine = RecallEngine(
        store, embedder, NumpyVectorIndex(store), IdentityReranker(), query_planner=planner,
    )
    workspace = store.get_or_create_workspace("planned")
    repo = store.get_or_create_repo(workspace, "recall")
    target_id = _add(store, embedder, workspace, repo, "target")
    _add(store, embedder, workspace, repo, "distractor")

    result = engine.recall(
        "original", SearchFilter(workspace_id=workspace, repo_id=repo),
        planning="auto", candidate_k=1, k=2,
    )

    target = next(chunk for chunk in result.chunks if chunk["id"] == target_id)
    assert target["absolute_support"] > 0.9


@pytest.mark.parametrize("value", [True, -1, 1.5, "2"])
def test_mtype_limits_reject_non_integer_or_negative_values(value):
    _, _, engine, _, _ = _engine()
    with pytest.raises(ValueError, match="non-negative integers"):
        engine.recall("query", mtype_limits={"semantic": value})
