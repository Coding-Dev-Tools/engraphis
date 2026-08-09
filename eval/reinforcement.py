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


def _gain_checks(gains: list[float], *, tolerance: float = 1e-12) -> dict[str, bool]:
    finite = all(math.isfinite(gain) for gain in gains)
    nonnegative = finite and all(gain >= -tolerance for gain in gains)
    nonincreasing = finite and all(
        later <= earlier + tolerance
        for earlier, later in zip(gains, gains[1:])
    )
    return {
        "finite": finite,
        "nonnegative": nonnegative,
        "nonincreasing": nonincreasing,
    }


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
    recall_gain_checks = _gain_checks(recall_gains)
    create_gain_checks = _gain_checks(create_gains)
    checks = {
        "finite": (
            math.isfinite(recall_stability)
            and math.isfinite(create_stability)
            and recall_gain_checks["finite"]
            and create_gain_checks["finite"]
        ),
        "recall_1000_under_5_days": recall_stability < 5.0,
        "create_1000_under_10_days": create_stability < 10.0,
        "within_policy_cap": max(recall_stability, create_stability) <= MAX_STABILITY_DAYS,
        "nonnegative_recall_gain": recall_gain_checks["nonnegative"],
        "nonnegative_create_gain": create_gain_checks["nonnegative"],
        "diminishing_recall_gain": recall_gain_checks["nonincreasing"],
        "diminishing_create_gain": create_gain_checks["nonincreasing"],
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
