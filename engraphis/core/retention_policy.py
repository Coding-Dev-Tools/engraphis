"""Bounded deterministic retention-state transitions."""
from __future__ import annotations

import math
import operator
from typing import Any


DEFAULT_STABILITY_DAYS = 1.0
MIN_STABILITY_DAYS = 0.05
MAX_STABILITY_DAYS = 100.0
MAX_ACCESS_COUNT = 1_000_000_000
DEFAULT_REINFORCEMENT_ALPHA = 0.3


def effective_stability(value: Any) -> float:
    """Return a finite stability value inside the supported policy domain."""
    try:
        stability = float(value)
    except (TypeError, ValueError, OverflowError):
        stability = DEFAULT_STABILITY_DAYS
    if not math.isfinite(stability) or stability <= 0:
        stability = DEFAULT_STABILITY_DAYS
    return min(MAX_STABILITY_DAYS, max(MIN_STABILITY_DAYS, stability))


def effective_access_count(value: Any) -> int:
    """Return a canonical reinforcement-event count for every write path."""
    if isinstance(value, bool):
        return 0
    try:
        count = operator.index(value)
    except TypeError:
        return 0
    return min(MAX_ACCESS_COUNT, max(0, count))


def _nonnegative_finite(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a non-negative finite number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a non-negative finite number") from exc
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{name} must be a non-negative finite number")
    return number


def reinforced_stability(
    stability: Any,
    access_count: Any,
    *,
    alpha: float = DEFAULT_REINFORCEMENT_ALPHA,
    boost: float = 0.0,
) -> tuple[float, int]:
    """Return bounded stability and the new reinforcement-event count.

    The nth event receives marginal-log credit. For stability of at least one day
    and a fixed interaction boost, cumulative growth is logarithmic until the cap:
    ``S_n = S_0 + (alpha + boost) * log(n + 1)``.
    """
    if isinstance(access_count, bool):
        raise ValueError("access_count must be a non-negative integer")
    try:
        count = operator.index(access_count)
    except TypeError as exc:
        raise ValueError("access_count must be a non-negative integer") from exc
    if count < 0:
        raise ValueError("access_count must be a non-negative integer")

    alpha_value = _nonnegative_finite(alpha, "alpha")
    boost_value = _nonnegative_finite(boost, "boost")
    current = effective_stability(stability)
    if count >= MAX_ACCESS_COUNT:
        return current, MAX_ACCESS_COUNT

    new_count = count + 1
    base_gain = alpha_value * min(current, DEFAULT_STABILITY_DAYS)
    marginal = math.log1p(1.0 / new_count)
    updated = current + (base_gain + boost_value) * marginal
    return min(MAX_STABILITY_DAYS, updated), new_count
