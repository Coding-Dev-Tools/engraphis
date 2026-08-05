"""Deterministic release gate for the reinforcement state transition."""
from __future__ import annotations

import json
import math

from engraphis.core import scoring
from engraphis.core.retention_policy import (
    MAX_STABILITY_DAYS,
    reinforced_stability,
)


def _trajectory(events: int, boost: float) -> tuple[float, list[float]]:
    stability, count = 1.0, 0
    gains = []
    for _ in range(events):
        updated, count = reinforced_stability(stability, count, boost=boost)
        gains.append(updated - stability)
        stability = updated
    return stability, gains


def run() -> dict:
    recall_stability, recall_gains = _trajectory(
        1000, scoring.INTERACTION_BOOST["recall"]
    )
    create_stability, create_gains = _trajectory(
        1000, scoring.INTERACTION_BOOST["create"]
    )
    now = 10_000_000.0
    retention_90d = scoring.retention(
        recall_stability, now - 90 * 86_400, now
    )
    checks = {
        "finite": math.isfinite(recall_stability) and math.isfinite(create_stability),
        "recall_1000_under_5_days": recall_stability < 5.0,
        "create_1000_under_10_days": create_stability < 10.0,
        "within_policy_cap": max(recall_stability, create_stability) <= MAX_STABILITY_DAYS,
        "diminishing_recall_gain": recall_gains[99] < recall_gains[0],
        "diminishing_create_gain": create_gains[99] < create_gains[0],
        "recall_burst_90d_retention_below_1e_6": retention_90d < 1e-6,
    }
    return {
        "schema": "engraphis-reinforcement-eval/v1",
        "recall_1000_stability_days": recall_stability,
        "create_1000_stability_days": create_stability,
        "recall_1000_retention_after_90d": retention_90d,
        "checks": checks,
        "passed": all(checks.values()),
    }


def main() -> None:
    report = run()
    print(json.dumps(report, indent=2, sort_keys=True))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
