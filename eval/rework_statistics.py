"""Small-sample guards around the existing deterministic bootstrap implementation."""
from __future__ import annotations

import math

from eval.benchmark import paired_bootstrap_ci


def blocked_mean_interval(values: list[float], *, unit: str, iterations: int = 2000,
                          seed: int = 20260905) -> dict:
    """Resample independent block means, never their correlated constituent rows."""
    if type(iterations) is not int or iterations < 1000 or type(seed) is not int:
        raise ValueError("bootstrap requires at least 1000 iterations and an integer seed")
    if any(type(value) not in {int, float} or not math.isfinite(value) for value in values):
        raise ValueError("bootstrap observations must be finite numbers")
    point = sum(values) / len(values) if values else None
    result = {"point": point, "low": None, "high": None, "confidence": 0.95,
              "units": len(values), "unit": unit, "iterations": iterations, "seed": seed,
              "method": "percentile bootstrap of independent block means",
              "degenerate": len(set(values)) < 2, "inferentially_usable": False}
    if len(values) >= 2:
        interval = paired_bootstrap_ci([(value, 0.0) for value in values],
                                       iterations=iterations, seed=seed)
        result.update({key: interval[key] for key in ("low", "high")})
        result["inferentially_usable"] = not result["degenerate"]
    return result
