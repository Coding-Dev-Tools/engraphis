import math
import pytest

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


@pytest.mark.parametrize("bad", [
    float("nan"), float("inf"), float("-inf"), None, "bad", 10 ** 1000,
])
def test_normalize_ignores_nonfinite_and_malformed_evidence(bad):
    assert scoring.normalize({
        "low": 2.0,
        "bad": bad,
        "high": 6.0,
    }) == {"low": 0.0, "high": 1.0}
    assert scoring.normalize({"bad": bad}) == {}


def test_normalize_preserves_order_for_extreme_finite_range():
    out = scoring.normalize({"low": -1e308, "mid": 0.0, "high": 1e308})
    assert out["low"] == pytest.approx(0.0)
    assert out["mid"] == pytest.approx(0.5)
    assert out["high"] == pytest.approx(1.0)


def test_scoring_edge_inputs_stay_finite_and_bounded():
    now = 1_000_000.0
    # Non-finite values are missing evidence: they are dropped, not kept as 0.0.
    # A single surviving finite value is a flat input and maps to 1.0.
    normalized = scoring.normalize({"nan": float("nan"), "inf": float("inf"), "ok": 2.0})
    assert set(normalized) == {"ok"}
    assert all(math.isfinite(value) and 0.0 <= value <= 1.0 for value in normalized.values())
    # All-non-finite input contributes no evidence at all, never a max score.
    assert scoring.normalize({"nan": float("nan"), "inf": float("inf")}) == {}
    assert 0.0 <= scoring.retention("bad", "bad", now) <= 1.0
    assert 0.0 <= scoring.retention(1.0, now, float("nan")) <= 1.0
    assert 0.0 <= scoring.retention(10 ** 1000, now, now) <= 1.0
    assert 0.0 <= scoring.recency("bad", now, tau_days=0) <= 1.0
    assert 0.0 <= scoring.staleness_penalty(float("nan"), now) <= 1.0
    fused = scoring.reciprocal_rank_fusion([["a", "a", "", None], ["a"]], k=0)
    assert fused["a"] == pytest.approx(1.0)


def test_zero_confidence_dampens_importance_to_zero():
    now = 1_000_000.0
    record = MemoryRecord(
        id="zero", content="same", mtype=MemoryType.SEMANTIC,
        last_access=now, ingested_at=now, valid_from=now,
        importance=1.0, confidence=0.0,
    )
    certain = MemoryRecord(**{**record.__dict__, "id": "certain", "confidence": 1.0})
    weights = scoring.weights_for(MemoryType.SEMANTIC)
    gap = scoring.score_memory(certain, now=now, weights=weights) - (
        scoring.score_memory(record, now=now, weights=weights)
    )
    assert gap == pytest.approx(weights.i)


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


def test_ordinary_recall_does_not_double_weight_fact_age():
    """Validity/ingestion age is not a second decay curve in query recall."""
    now = 1_000_000.0
    w = scoring.weights_for(MemoryType.SEMANTIC)
    shared = dict(
        content="same evidence", mtype=MemoryType.SEMANTIC, last_access=now - 86_400,
        stability=4.0, importance=0.4,
    )
    new = MemoryRecord(id="new", ingested_at=now, valid_from=now, **shared)
    old = MemoryRecord(
        id="old", ingested_at=now - 365 * 86_400,
        valid_from=now - 365 * 86_400,
        **shared,
    )
    assert scoring.score_memory(new, now=now, weights=w, semantic=0.7) == (
        scoring.score_memory(old, now=now, weights=w, semantic=0.7)
    )


def test_per_type_weight_profiles_differ():
    assert scoring.weights_for(MemoryType.WORKING).c > scoring.weights_for(MemoryType.SEMANTIC).c
    assert scoring.weights_for(MemoryType.PROCEDURAL).i > scoring.weights_for(MemoryType.WORKING).i


def test_confidence_dampens_importance_in_score_memory():
    now = 1_000_000.0
    w = scoring.weights_for(MemoryType.SEMANTIC)
    shared = dict(
        content="same evidence", mtype=MemoryType.SEMANTIC,
        last_access=now, ingested_at=now, valid_from=now,
        importance=0.8, surprise=1.0,
    )
    certain = MemoryRecord(id="certain", confidence=1.0, **shared)
    unsure = MemoryRecord(id="unsure", confidence=0.5, **shared)
    assert scoring.score_memory(unsure, now=now, weights=w, semantic=1.0) < (
        scoring.score_memory(certain, now=now, weights=w, semantic=1.0)
    )
    # The default (1.0) is a no-op: identical to a record without the field set.
    plain = MemoryRecord(id="plain", **shared)
    assert scoring.score_memory(certain, now=now, weights=w, semantic=1.0) == (
        scoring.score_memory(plain, now=now, weights=w, semantic=1.0)
    )
    # The gap is exactly the dampened importance contribution: w.i * imp * (1 - conf).
    diff = (
        scoring.score_memory(certain, now=now, weights=w, semantic=1.0)
        - scoring.score_memory(unsure, now=now, weights=w, semantic=1.0)
    )
    assert diff == pytest.approx(w.i * 0.8 * 0.5)
    # Confidence outside [0, 1] is clamped, never amplified.
    wild = MemoryRecord(id="wild", confidence=9.0, **shared)
    assert scoring.score_memory(wild, now=now, weights=w, semantic=1.0) == (
        scoring.score_memory(certain, now=now, weights=w, semantic=1.0)
    )


def test_confidence_dampens_proactive_importance_signal():
    now = 1_000_000.0
    w = scoring.weights_for(MemoryType.SEMANTIC)
    shared = dict(
        content="same", mtype=MemoryType.SEMANTIC,
        last_access=now, ingested_at=now, valid_from=now,
        importance=0.9, stability=5.0,
    )
    certain = MemoryRecord(id="c", confidence=1.0, **shared)
    unsure = MemoryRecord(id="u", confidence=0.4, **shared)
    assert scoring.score_proactive(unsure, now=now, weights=w) < (
        scoring.score_proactive(certain, now=now, weights=w)
    )
    # Default 1.0 matches a legacy record that never set the field.
    plain = MemoryRecord(id="p", **shared)
    assert scoring.score_proactive(certain, now=now, weights=w) == (
        scoring.score_proactive(plain, now=now, weights=w)
    )
