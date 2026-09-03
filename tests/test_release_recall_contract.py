"""Release contract: recall fusion ranking + RecallCoreFix pinned names.

Fusion behavior is exercised end-to-end through ``RecallEngine`` with
``_FixedScoreIndex`` doubles (the same seam as the calibration regression in
``test_recall.py``) across the balanced/fast/graph named profiles. The second
half pins the exact contract names RecallCoreFix implements — these tests fail
before that slice lands and pass after.
"""
import inspect

import pytest

from engraphis.backends import DeterministicEmbedder, NumpyVectorIndex
from engraphis.backends.reranker import IdentityReranker
from engraphis.core import recall as recall_module
from engraphis.core.interfaces import MemoryRecord, SearchFilter
from engraphis.core.recall import CONSOLIDATION_BONUS, RecallEngine
from engraphis.core.retrieval_policy import ProfileConfig, profile_config
from engraphis.core.store import Store


class _SemanticTestEmbedder(DeterministicEmbedder):
    """Test double that opts into vector semantics without a model download."""

    supports_semantic_search = True
    embedding_mode = "semantic"


class _FixedScoreIndex:
    """Semantic-index double for calibration regressions."""

    def __init__(self, scores):
        self.scores = list(scores)

    def search(self, query, k, *, filter=None):
        return self.scores[:k]


def _add(store, emb, wid, text):
    return store.add_memory(MemoryRecord(
        id="", content=text, workspace_id=wid,
        embedding=emb.embed([text])[0],
        provenance={"source": "test", "trusted": True, "review_state": "approved"},
    ))


def _calibration_fixture():
    from engraphis.core.store import Store
    store = Store(":memory:")
    emb = _SemanticTestEmbedder(256)
    wid = store.get_or_create_workspace("w")
    weak_id = _add(store, emb, wid, "The parking garage closes at dusk.")
    lexical_id = _add(store, emb, wid, "PASETO is the approved token format.")
    eng = RecallEngine(
        store, emb, _FixedScoreIndex([(weak_id, 0.01)]), IdentityReranker(),
    )
    return eng, wid, weak_id, lexical_id


# ── pinned names (fail before RecallCoreFix lands) ───────────────────────────

def test_balanced_profile_calibrates_semantic_confidence_by_default():
    assert profile_config("balanced").semantic_confidence_calibration is True
    assert ProfileConfig("probe", True, True, False, False).semantic_confidence_calibration is True


def test_arm_candidate_k_default_is_200():
    assert recall_module.ARM_CANDIDATE_K_DEFAULT == 200
    assert profile_config("balanced").arm_candidate_k_default == 200
    assert ProfileConfig("probe", True, True, False, False).arm_candidate_k_default == 200


def test_profile_rerank_blend_and_consolidation_bonus():
    config = profile_config("balanced")
    assert tuple(config.rerank_blend) == (0.7, 0.3)
    assert config.consolidation_bonus == 0.05
    assert recall_module.SEMANTIC_CONFIDENCE_FLOOR == 0.0
    assert CONSOLIDATION_BONUS == 0.05


# ── fusion ranking through RecallEngine ──────────────────────────────────────

@pytest.mark.parametrize("profile", ["balanced", "fast", "graph"])
def test_weak_vector_singleton_loses_to_lexical_evidence(profile):
    """A lone vector hit with raw cosine 0.01 is weak evidence everywhere.

    Every named profile calibrates rank evidence with cosine confidence, so the
    record with exact lexical support wins in balanced, fast, and graph alike.
    """
    eng, wid, weak_id, lexical_id = _calibration_fixture()
    result = eng.recall(
        "PASETO", SearchFilter(workspace_id=wid), k=1, retrieval_profile=profile,
    )
    assert [chunk["id"] for chunk in result.chunks] == [lexical_id]
    assert [chunk["id"] for chunk in result.chunks] != [weak_id]


@pytest.mark.parametrize("profile", ["balanced", "fast", "graph"])
def test_vector_lexical_agreement_ranking_is_deterministic(profile):
    """When the vector double and lexical evidence agree, the winner is stable."""
    from engraphis.core.store import Store
    store = Store(":memory:")
    emb = _SemanticTestEmbedder(256)
    wid = store.get_or_create_workspace("w")
    winner = _add(store, emb, wid, "PASETO is the approved token format.")
    loser = _add(store, emb, wid, "The parking garage closes at dusk.")
    eng = RecallEngine(
        store, emb,
        _FixedScoreIndex([(winner, 0.9), (loser, 0.2)]),
        IdentityReranker(),
    )
    first = eng.recall(
        "PASETO", SearchFilter(workspace_id=wid), k=2, retrieval_profile=profile,
    )
    second = eng.recall(
        "PASETO", SearchFilter(workspace_id=wid), k=2, retrieval_profile=profile,
    )
    assert [chunk["id"] for chunk in first.chunks] == [winner, loser]
    assert [chunk["id"] for chunk in second.chunks] == [winner, loser]


def test_diagnostics_expose_candidate_depth_and_per_arm_counts():
    eng, wid, _weak_id, _lexical_id = _calibration_fixture()
    result = eng.recall(
        "PASETO", SearchFilter(workspace_id=wid), k=1,
        retrieval_profile="balanced", diagnostics=True,
    )
    assert isinstance(result.candidate_k_used, int)
    assert result.candidate_k_used >= 1
    details = result.planning_details
    assert isinstance(details, dict)
    assert details["candidate_k_used"] == result.candidate_k_used
    assert set(details["arm_counts"]) == {"vector", "lexical", "graph", "code"}
    assert all(isinstance(count, int) for count in details["arm_counts"].values())
    assert isinstance(details["rerank_changed"], bool)
    assert list(details["scoring"]["rerank_blend"]) == [0.7, 0.3]
    assert details["scoring"]["consolidation_bonus"] == 0.05


def test_graph_seed_fallback_entry_point():
    assert callable(RecallEngine._graph_seed_fallback)
    params = inspect.signature(RecallEngine._graph_seed_fallback).parameters
    assert list(params)[:3] == ["self", "query", "flt"]
    assert params["m"].default == 8
    assert params["prompt_only"].default is False
    assert params["m"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["prompt_only"].kind is inspect.Parameter.KEYWORD_ONLY


def test_ordinary_recall_keeps_requested_candidate_depth():
    store_emb_eng = _calibration_fixture()
    eng, wid = store_emb_eng[0], store_emb_eng[1]
    result = eng.recall("PASETO", SearchFilter(workspace_id=wid), k=1)
    assert result.candidate_k_requested == 50
    # Prompt-safe recall overfetches the first arm (50 + min(250, 150)) within
    # the default arm cap, and reports the actual page depth it searched.
    assert result.candidate_k_used == 200


def test_numpy_backed_recall_reports_vector_arm_evidence():
    store = Store(":memory:")
    emb = _SemanticTestEmbedder(256)
    eng = RecallEngine(store, emb, NumpyVectorIndex(store), IdentityReranker())
    wid = store.get_or_create_workspace("w")
    _add(store, emb, wid, "PASETO is the approved token format.")
    result = eng.recall(
        "PASETO", SearchFilter(workspace_id=wid), k=1, diagnostics=True,
    )
    assert result.count == 1
    assert result.retrieval_trace
    assert result.retrieval_trace[0]["raw"]["semantic"] is not None
