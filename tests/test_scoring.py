from engraphis.core import scoring
from engraphis.core.interfaces import MemoryRecord, MemoryType


def test_retention_full_at_zero_and_decays():
    now = 1_000_000.0
    assert scoring.retention(2.0, now, now) == 1.0
    assert 0.0 < scoring.retention(2.0, now - 2 * 86400, now) < 1.0


def test_recency_bounds_and_monotonic():
    now = 1_000_000.0
    assert scoring.recency(None, now) == 0.0
    assert scoring.recency(now, now) == 1.0
    assert scoring.recency(now - 10 * 86400, now, 5) < scoring.recency(now - 86400, now, 5)


def test_staleness_penalty_ramp():
    now = 1000.0
    assert scoring.staleness_penalty(None, now) == 0.0
    assert scoring.staleness_penalty(now - 1, now) == 1.0
    assert scoring.staleness_penalty(now + 100 * 86400, now) == 0.0
    assert 0.0 < scoring.staleness_penalty(now + 3.5 * 86400, now, ramp_days=7.0) < 1.0


def test_normalize():
    out = scoring.normalize({"a": 0.0, "b": 10.0})
    assert out == {"a": 0.0, "b": 1.0}
    assert scoring.normalize({"a": 5.0, "b": 5.0}) == {"a": 1.0, "b": 1.0}


def test_rrf_rewards_multi_arm_agreement():
    fused = scoring.reciprocal_rank_fusion([["x", "y"], ["x", "z"]])
    assert fused["x"] > fused["y"] and fused["x"] > fused["z"]


def test_score_rewards_semantic_penalizes_stale():
    now = 1_000_000.0
    w = scoring.weights_for(MemoryType.SEMANTIC)
    rec = MemoryRecord(id="m", content="c", mtype=MemoryType.SEMANTIC,
                       last_access=now, ingested_at=now, valid_from=now)
    hi = scoring.score_memory(rec, now=now, weights=w, semantic=1.0)
    lo = scoring.score_memory(rec, now=now, weights=w, semantic=0.0)
    assert hi > lo
    stale = MemoryRecord(id="m2", content="c", mtype=MemoryType.SEMANTIC, last_access=now,
                         ingested_at=now, valid_from=now - 10 * 86400, valid_to=now - 86400)
    assert scoring.score_memory(stale, now=now, weights=w, semantic=1.0) < hi


def test_per_type_weight_profiles_differ():
    assert scoring.weights_for(MemoryType.WORKING).c > scoring.weights_for(MemoryType.SEMANTIC).c
    assert scoring.weights_for(MemoryType.PROCEDURAL).i > scoring.weights_for(MemoryType.WORKING).i
