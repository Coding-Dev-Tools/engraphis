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


def test_proactive_importance_decays_and_cannot_make_a_week_old_note_immortal():
    """Important but unreinforced notes yield to fresh evidence as retention decays."""
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

    assert [memory.id for memory in proactive["memories"]] != [old_important]


def test_decaying_importance_is_calibrated_by_the_checked_in_ranking_eval():
    report = run()

    result = report["decaying_importance"]
    assert result["importance_signal"] == "importance_times_retention"
    assert result["top_1_accuracy"] == 1.0
    assert result["minimum_expected_margin"] > 0.0
