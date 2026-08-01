"""Regression coverage for the canonical v2 queryless recall policy."""

from engraphis.core import scoring
from engraphis.core.engine import MemoryEngine
from engraphis.core.interfaces import MemoryRecord, MemoryType, Scope
from engraphis.core.store import now_ts
from eval.proactive_ranking import run


def test_zero_stability_is_the_v2_legacy_default_not_a_fast_decay_sentinel():
    """v1 imports with ``stability=0`` retain v2's documented default semantics."""
    now = 1_000_000.0
    last_access = now - 7 * 86400.0

    assert scoring.retention(0.0, last_access, now) == scoring.retention(
        scoring.DEFAULT_STABILITY_DAYS, last_access, now
    )


def test_proactive_keeps_a_week_old_important_memory_ahead_of_fresh_scratch():
    """Decay remains a priority signal without starving the proactive agenda."""
    engine = MemoryEngine.create(":memory:")
    workspace_id = engine.store.get_or_create_workspace("acme")
    now = now_ts()
    old_important = engine.store.add_memory(MemoryRecord(
        id="", content="Production deploys require an approval.",
        workspace_id=workspace_id, scope=Scope.WORKSPACE,
        mtype=MemoryType.SEMANTIC, importance=0.9, stability=1.0,
        ingested_at=now - 7 * 86400.0, last_access=now - 7 * 86400.0,
    ))
    engine.store.add_memory(MemoryRecord(
        id="", content="Temporary scratch note.", workspace_id=workspace_id,
        scope=Scope.WORKSPACE, mtype=MemoryType.SEMANTIC,
        importance=0.0, stability=1.0, ingested_at=now, last_access=now,
    ))

    proactive = engine.recall_proactive(workspace_id=workspace_id, k=1)

    assert [memory.id for memory in proactive["memories"]] == [old_important]


def test_importance_floor_is_calibrated_by_the_checked_in_ranking_eval():
    report = run()

    assert report["no_floor"]["top_1_accuracy"] == 0.2
    assert report["prior_floor"]["top_1_accuracy"] == 0.4
    assert report["calibrated_floor"]["top_1_accuracy"] == 1.0
    assert report["calibrated_floor"]["minimum_expected_margin"] > 0.0
