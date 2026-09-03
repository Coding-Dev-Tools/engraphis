"""Release-gate regression tests for profile-driven recall tuning.

Covers the recall-slice contract: calibration defaulting on (singleton-cosine
false 1.0), the 200 default arm-candidate depth, profile-owned rerank blend and
consolidation bonus, per-arm diagnostics, unconditional telemetry logging, and
the lexical graph-seed fallback. Only this file may assert the new behavior;
established baselines live with the other test slices.
"""
from __future__ import annotations

import logging

import pytest

from engraphis.backends import DeterministicEmbedder, NumpyVectorIndex
from engraphis.core.interfaces import MemoryRecord, Node, Scope, SearchFilter
from engraphis.core.recall import (
    ARM_CANDIDATE_K_DEFAULT,
    CONSOLIDATION_BONUS,
    SEMANTIC_CONFIDENCE_FLOOR,
    RecallEngine,
)
from engraphis.core.retrieval_policy import ProfileConfig, profile_config
from engraphis.core.store import Store


class _SemanticEmbedder(DeterministicEmbedder):
    """Test double that opts into vector semantics without a model download."""

    supports_semantic_search = True
    embedding_mode = "semantic"


class _FixedCosineIndex:
    """Vector-index double returning one controlled cosine per memory."""

    def __init__(self, pairs):
        self.pairs = list(pairs)

    def search(self, query, k, *, filter=None):
        return list(self.pairs[:max(0, int(k))])


class _RecordingIndex:
    """Vector-index double that records every arm depth it was queried with."""

    def __init__(self):
        self.requested: list[int] = []

    def search(self, query, k, *, filter=None):
        self.requested.append(int(k))
        return []


def _engine(**kwargs):
    store = Store(":memory:")
    embedder = _SemanticEmbedder(256)
    engine = RecallEngine(store, embedder, NumpyVectorIndex(store), **kwargs)
    workspace = store.get_or_create_workspace("release")
    return store, embedder, engine, workspace


def _add(store, embedder, workspace, text, **kwargs):
    # Direct Store fixtures model locally approved test data. Public ingress
    # coverage uses MemoryService and must remain pending until review.
    provenance = dict(kwargs.pop("provenance", {}) or {})
    provenance.setdefault("source", "test")
    provenance.setdefault("trusted", True)
    if provenance.get("trusted") is True:
        provenance.setdefault("review_state", "approved")
    return store.add_memory(MemoryRecord(
        id="",
        content=text,
        workspace_id=workspace,
        scope=Scope.REPO,
        embedding=embedder.embed([text])[0],
        provenance=provenance,
        **kwargs,
    ))


def test_balanced_profile_calibrates_semantic_confidence_by_default():
    assert profile_config("balanced").semantic_confidence_calibration is True
    assert SEMANTIC_CONFIDENCE_FLOOR == 0.0
    # The dataclass default itself is calibrated; only explicit opt-outs keep
    # the legacy rank-only semantic fusion.
    assert ProfileConfig("custom", True, True, True, False).semantic_confidence_calibration is True


def test_singleton_low_cosine_keeps_measured_support():
    store = Store(":memory:")
    embedder = _SemanticEmbedder(256)
    workspace = store.get_or_create_workspace("release")
    memory_id = _add(store, embedder, workspace, "The lighthouse keeper logged the tide.")
    engine = RecallEngine(store, embedder, _FixedCosineIndex([(memory_id, 0.02)]))
    flt = SearchFilter(workspace_id=workspace)

    result = engine.recall(
        "tide log", flt, k=1, candidate_k=10, diagnostics=True,
        arm_config=ProfileConfig("vector_only", True, False, False, False),
    )

    assert [item["id"] for item in result.chunks] == [memory_id]
    detail = result.retrieval_trace[0]
    # A singleton vector hit min-max normalizes to 1.0; calibration must scale
    # it back to the measured cosine instead of publishing a false 1.0.
    assert detail["normalized"]["semantic"] == pytest.approx(1.0)
    assert detail["profile_adjusted"]["semantic"] == pytest.approx(
        detail["normalized"]["semantic"] * 0.02
    )
    assert detail["profile_adjusted"]["semantic"] < 0.5


def test_explicit_opt_out_restores_rank_only_semantic():
    store = Store(":memory:")
    embedder = _SemanticEmbedder(256)
    workspace = store.get_or_create_workspace("release")
    memory_id = _add(store, embedder, workspace, "The lighthouse keeper logged the tide.")
    engine = RecallEngine(store, embedder, _FixedCosineIndex([(memory_id, 0.02)]))
    flt = SearchFilter(workspace_id=workspace)

    result = engine.recall(
        "tide log", flt, k=1, candidate_k=10, diagnostics=True,
        arm_config=ProfileConfig(
            "rank_only", True, False, False, False,
            semantic_confidence_calibration=False,
        ),
    )

    detail = result.retrieval_trace[0]
    assert detail["profile_adjusted"]["semantic"] == pytest.approx(
        detail["normalized"]["semantic"]
    )


def test_arm_candidate_k_default_contract():
    assert ARM_CANDIDATE_K_DEFAULT == 200
    assert profile_config("balanced").arm_candidate_k_default == 200


def test_default_prompt_recall_opens_200_page():
    store, embedder, _, workspace = _engine()
    index = _RecordingIndex()
    engine = RecallEngine(store, embedder, index)
    _add(store, embedder, workspace, "The nightly sync job failed twice.")
    _add(store, embedder, workspace, "The release checklist needs a sign-off.")
    flt = SearchFilter(workspace_id=workspace)

    result = engine.recall("nightly sync job", flt, k=2)

    assert index.requested[0] == 200
    assert result.candidate_k_used == 200
    assert result.candidate_k_requested == 50


def test_explicit_arm_cap_overrides_profile_default():
    store, embedder, _, workspace = _engine()
    index = _RecordingIndex()
    engine = RecallEngine(store, embedder, index, arm_candidate_k_cap=50)
    _add(store, embedder, workspace, "The nightly sync job failed twice.")
    flt = SearchFilter(workspace_id=workspace)

    result = engine.recall("nightly sync job", flt, k=1)

    assert index.requested[0] == 50
    assert result.candidate_k_used == 50
    assert result.count >= 1


def test_profile_blend_and_bonus_contract():
    balanced = profile_config("balanced")
    assert balanced.rerank_blend == (0.7, 0.3)
    assert balanced.consolidation_bonus == 0.05
    assert CONSOLIDATION_BONUS == 0.05


def test_consolidation_bonus_comes_from_profile():
    store, embedder, engine, workspace = _engine()
    _add(
        store, embedder, workspace, "Distilled rollout playbook.",
        provenance={"source": "consolidation", "trusted": True, "review_state": "approved"},
    )
    flt = SearchFilter(workspace_id=workspace)

    default = engine.recall("rollout playbook", flt, k=1, diagnostics=True)
    assert default.retrieval_trace[0]["consolidation_bonus"] == pytest.approx(0.05)

    unbonused = engine.recall(
        "rollout playbook", flt, k=1, diagnostics=True,
        arm_config=ProfileConfig(
            "custom", True, True, True, False, consolidation_bonus=0.0,
        ),
    )
    assert unbonused.retrieval_trace[0]["consolidation_bonus"] == pytest.approx(0.0)


def test_planning_details_reports_arm_counts_and_rerank_state():
    store, embedder, engine, workspace = _engine()
    _add(store, embedder, workspace, "The nightly sync job failed twice.")
    _add(store, embedder, workspace, "The release checklist needs a sign-off.")
    flt = SearchFilter(workspace_id=workspace)

    result = engine.recall("nightly sync job", flt, k=2, candidate_k=10, diagnostics=True)

    details = result.planning_details
    assert details["candidate_k_used"] == result.candidate_k_used
    assert set(details["arm_counts"]) == {"vector", "lexical", "graph", "code"}
    assert all(isinstance(count, int) and count >= 0 for count in details["arm_counts"].values())
    assert details["rerank_changed"] is False
    assert details["scoring"] == {
        "rerank_blend": [0.7, 0.3],
        "consolidation_bonus": 0.05,
    }
    assert isinstance(details["type_limit_drops"], list)


def test_telemetry_logged_without_diagnostics(caplog):
    store, embedder, engine, workspace = _engine()
    _add(store, embedder, workspace, "The nightly sync job failed twice.")
    flt = SearchFilter(workspace_id=workspace)

    with caplog.at_level(logging.INFO, logger="engraphis.core.recall"):
        result = engine.recall("nightly sync job", flt, k=1)

    assert result.planning_details is None
    assert result.retrieval_trace is None
    assert "candidate_k_used=" in caplog.text
    assert "rerank_changed=" in caplog.text
    assert "type_limit_drops=" in caplog.text


def test_graph_seed_fallback_projects_lexical_hits():
    """Opt-in fallback: when the query names no entity, entities mentioned in
    AND linked to lexical-hit memories seed the graph arm (paraphrase rescue)."""
    store, embedder, engine, workspace = _engine()
    memory_id = _add(store, embedder, workspace, "The nightly sync job failed twice.")
    entity_id = store.upsert_entity(Node(
        id="", name="sync-job", ntype="service", workspace_id=workspace,
    ))
    store.link_memory_entity(
        memory_id=memory_id, entity_id=entity_id, workspace_id=workspace,
        repo_id=None, source_kind="test", confidence=1.0,
    )
    flt = SearchFilter(workspace_id=workspace)
    query = "What failed during the nightly sync run?"

    assert engine._query_entity_seeds(query, flt) == []
    assert engine._graph_seed_fallback(query, flt) == [entity_id]
    # Default (opt-out): the 1-hop arm stays empty for the paraphrase...
    assert engine._graph_arm_1hop(query, flt, now=10**12, prompt_only=True) == {}
    # ...while opting in via the profile flag recovers the linked evidence.
    assert memory_id in engine._graph_arm_1hop(
        query, flt, now=10**12, prompt_only=True, seed_fallback=True,
    )


def test_graph_seed_fallback_rejects_topically_unrelated_entities():
    """A linked entity never mentioned by the lexical-hit memory content must
    not seed the graph arm — generic shared words are not topical evidence."""
    store, embedder, engine, workspace = _engine()
    memory_id = _add(store, embedder, workspace, "The nightly sync job failed twice.")
    unrelated = store.upsert_entity(Node(
        id="", name="AtlasCache", ntype="service", workspace_id=workspace,
    ))
    store.link_memory_entity(
        memory_id=memory_id, entity_id=unrelated, workspace_id=workspace,
        repo_id=None, source_kind="test", confidence=1.0,
    )
    flt = SearchFilter(workspace_id=workspace)
    query = "What failed during the nightly sync run?"

    assert engine._graph_seed_fallback(query, flt) == []


def test_graph_seed_fallback_is_opt_in_by_profile():
    from engraphis.core.retrieval_policy import ProfileConfig
    assert ProfileConfig("custom", True, True, True, False).graph_seed_fallback is False
    assert ProfileConfig(
        "custom-graph", True, True, True, False, graph_seed_fallback=True,
    ).graph_seed_fallback is True
