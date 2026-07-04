"""Analytics (Pro data layer) — pure-function math first, thin SQL wrapper second."""
import math

from engraphis.analytics import (
    FORGET_THRESHOLD, _days_until_forgotten, analytics_from_rows, compute_analytics,
)
from engraphis.service import MemoryService

NOW = 1_750_000_000.0


def _row(**kw):
    base = {"mtype": "semantic", "stability": 1.0, "last_access": NOW,
            "ingested_at": NOW, "importance": 0.0, "pinned": 0,
            "valid_to": None, "expired_at": None}
    base.update(kw)
    return base


def test_days_until_forgotten_matches_ebbinghaus_inverse():
    # exp(-dt/S) = T  →  dt = S·ln(1/T); with S=1 and fresh access ≈ 3.0 days.
    d = _days_until_forgotten(1.0, NOW, NOW)
    assert abs(d - math.log(1.0 / FORGET_THRESHOLD)) < 1e-9
    # Already-elapsed time is subtracted.
    d2 = _days_until_forgotten(1.0, NOW - 2 * 86400, NOW)
    assert abs((d - d2) - 2.0) < 1e-9


def test_analytics_from_rows_core_shape_and_counts():
    rows = [
        _row(stability=5.0),                                # ~15d horizon, live
        _row(stability=1.0, last_access=NOW - 2.5 * 86400), # forgotten in ~0.5d → at risk
        _row(stability=40.0),                               # safe for months
        _row(pinned=1, stability=0.01),                     # pinned → exempt from forecast
        _row(valid_to=NOW - 10, mtype="episodic"),          # superseded → not live
    ]
    out = analytics_from_rows(rows, {"invalidate": 2, "noop": 3},
                              [{"name": "Alice", "etype": "person", "n": 4}], now=NOW)
    assert out["totals"]["all_rows"] == 5
    assert out["totals"]["live"] == 4
    assert out["totals"]["superseded"] == 1
    assert out["totals"]["pinned"] == 1
    assert out["decay_forecast"]["at_risk_7d"] == 1        # only the fast-decaying one
    assert out["decay_forecast"]["at_risk_30d"] == 2       # + the ~15d-horizon one
    assert len(out["growth_weekly"]) == 12
    assert out["growth_weekly"][-1] == 5                   # all ingested "this week"
    assert sum(out["retention_histogram"]["counts"]) == 4  # live only
    assert out["by_type"] == {"semantic": 4}
    assert out["resolver_mix"] == {"invalidate": 2, "noop": 3}
    assert out["top_entities"][0]["name"] == "Alice"


def test_growth_buckets_place_old_memories_correctly():
    rows = [_row(ingested_at=NOW - 3 * 7 * 86400),         # 3 weeks ago
            _row(ingested_at=NOW - 100 * 7 * 86400)]       # off the chart → dropped
    out = analytics_from_rows(rows, {}, [], now=NOW)
    assert out["growth_weekly"][-4] == 1
    assert sum(out["growth_weekly"]) == 1


def test_compute_analytics_over_a_real_store():
    svc = MemoryService.create(":memory:")
    svc.remember("Deploy target is region iad.", workspace="acme", repo="infra")
    out2 = svc.remember("Deploy target is region fra as of March.",
                        workspace="acme", repo="infra")
    assert out2["op"] == "invalidate"                      # supersession happened
    wid = svc._lookup_workspace("acme")
    data = compute_analytics(svc.store, wid)
    assert data["totals"]["all_rows"] == 2
    assert data["totals"]["live"] == 1
    assert data["totals"]["superseded"] == 1
    assert data["resolver_mix"].get("invalidate", 0) >= 1
    assert data["totals"]["avg_retention"] > 0.9           # freshly written
