"""Recall scoring.

Pure, testable functions for the ordinary Engraphis recall score:

    score = w_r·retention + w_s·semantic + w_l·lexical + w_g·graph
          + w_i·importance − w_x·staleness

The proactive agenda additionally uses its own recency signal. Ordinary query recall
does not: retention already reflects time since reinforcement, and adding validity/
ingestion age would double-weight the age of an unreinforced record. Arm scores are
min-max normalized before fusion so no single arm dominates by raw scale. This is the
concrete fix for "similar ≠ important": semantic similarity is one term among five.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Optional

from engraphis.core.interfaces import MemoryRecord, MemoryType
from engraphis.core.retention_policy import (
    DEFAULT_STABILITY_DAYS as DEFAULT_STABILITY_DAYS,
    effective_stability,
)

# Interaction signals → stability boost (interaction-aware reinforcement).
INTERACTION_BOOST = {
    "view": 0.05, "read": 0.05, "recall": 0.15, "react": 0.20,
    "engage": 0.30, "reply": 0.50, "create": 1.00,
}

@dataclass(frozen=True)
class Weights:
    r: float = 1.0   # retention (Ebbinghaus)
    s: float = 1.0   # semantic similarity
    l: float = 0.5   # noqa: E741  (lexical weight w_l; single-letter to match the formula)
    g: float = 0.7   # graph proximity
    i: float = 0.6   # importance
    c: float = 0.3   # proactive-agenda recency (never ordinary query recall)
    x: float = 0.8   # staleness penalty (subtracted)


# Per-type weight profiles (§5.2 lifecycles → different retrieval emphasis).
DEFAULT_WEIGHTS: dict[MemoryType, Weights] = {
    MemoryType.WORKING:    Weights(r=0.6, s=1.0, l=0.6, g=0.4, i=0.3, c=1.0, x=0.5),
    MemoryType.EPISODIC:   Weights(r=0.9, s=1.0, l=0.6, g=0.7, i=0.6, c=0.6, x=0.8),
    MemoryType.SEMANTIC:   Weights(r=1.0, s=1.0, l=0.5, g=0.7, i=0.7, c=0.3, x=0.9),
    MemoryType.PROCEDURAL: Weights(r=1.0, s=0.9, l=0.5, g=0.8, i=0.9, c=0.2, x=0.7),
}


def weights_for(mtype: MemoryType) -> Weights:
    return DEFAULT_WEIGHTS.get(mtype, Weights())

def _finite_number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def _bounded(value: object, *, default: float = 0.0,
             lower: float = 0.0, upper: float = 1.0) -> float:
    number = _finite_number(value, default)
    return max(lower, min(upper, number))


def _confidence(value: object) -> float:
    if value is None:
        return 1.0
    return _bounded(value, default=1.0)


def retention(stability: float, last_access: Optional[float], now: float) -> float:
    """Ebbinghaus R(t) = exp(-Δt_days / S).

    ``stability=0`` is a v1-import compatibility sentinel for an unspecified
    value, so it deliberately means the v2 default of one day.  Negative and
    non-finite legacy values are treated the same way rather than producing an
    inverted or non-finite score.  None of these values requests hard deletion;
    forgetting only lowers priority.
    """
    S = effective_stability(stability)
    current = _finite_number(now, float("nan"))
    if not math.isfinite(current):
        return 0.0
    accessed = (
        current if last_access is None
        else _finite_number(last_access, current)
    )
    dt_days = max((current - accessed) / 86400.0, 0.0)
    return math.exp(-dt_days / S)


def recency(t_ref: Optional[float], now: float, tau_days: float = 30.0) -> float:
    """Exponential recency on world-time, for tie-breaking and temporal queries."""
    if t_ref is None:
        return 0.0
    current = _finite_number(now, float("nan"))
    reference = _finite_number(t_ref, float("nan"))
    if not math.isfinite(current) or not math.isfinite(reference):
        return 0.0
    tau = _finite_number(tau_days, 30.0)
    if tau <= 0:
        tau = 1e-6
    dt_days = max((current - reference) / 86400.0, 0.0)
    return math.exp(-dt_days / tau)


def staleness_penalty(valid_to: Optional[float], now: float,
                      ramp_days: float = 7.0) -> float:
    """1.0 once a fact is past its validity; ramps up in the ``ramp_days`` before."""
    if valid_to is None:
        return 0.0
    current = _finite_number(now, float("nan"))
    expiry = _finite_number(valid_to, float("nan"))
    if not math.isfinite(current) or not math.isfinite(expiry):
        return 0.0
    if current >= expiry:
        return 1.0
    ramp = _finite_number(ramp_days, 7.0)
    if ramp <= 0:
        return 0.0
    days_left = (expiry - current) / 86400.0
    if days_left >= ramp:
        return 0.0
    return max(0.0, min(1.0, 1.0 - (days_left / ramp)))


def normalize(scores: dict[str, float]) -> dict[str, float]:
    """Min-max normalize to [0, 1]; flat inputs map to 1.0.

    Retrieval adapters are external inputs in practice.  Non-finite values are
    treated as missing evidence instead of allowing NaN/Infinity to poison the
    fused ranking or its deterministic sort.  When every value is non-finite the
    arm contributes no evidence at all (empty result) rather than granting every
    key the maximum score.
    """
    if not scores:
        return {}
    finite: dict[str, float] = {}
    for key, value in scores.items():
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            continue  # unparseable evidence is missing evidence
        if math.isfinite(number):
            finite[key] = number
    if not finite:
        return {}
    lo, hi = min(finite.values()), max(finite.values())
    span = hi - lo
    if not math.isfinite(span):
        scale = max(abs(lo), abs(hi))
        if not math.isfinite(scale) or scale == 0.0:
            return {key: 1.0 for key in finite}
        scaled_lo = lo / scale
        scaled_hi = hi / scale
        span = scaled_hi - scaled_lo
        if not math.isfinite(span) or span < 1e-12:
            return {key: 1.0 for key in finite}
        return {
            key: max(0.0, min(1.0, (value / scale - scaled_lo) / span))
            for key, value in finite.items()
        }
    if span < 1e-12:
        return {key: 1.0 for key in finite}
    return {
        key: max(0.0, min(1.0, (value - lo) / span))
        for key, value in finite.items()
    }


def reciprocal_rank_fusion(rankings: list[list[str]], k: int = 60) -> dict[str, float]:
    """RRF across arms — rewards items ranked highly by multiple retrieval arms."""
    try:
        base = int(k)
    except (TypeError, ValueError):
        base = 60
    base = max(1, base)
    fused: dict[str, float] = {}
    for ranking in rankings or []:
        seen: set[str] = set()
        for mid in ranking or []:
            if not isinstance(mid, str) or not mid or mid in seen:
                continue
            seen.add(mid)
            rank = len(seen) - 1
            fused[mid] = fused.get(mid, 0.0) + 1.0 / (base + rank + 1)
    return fused


def score_memory(rec: MemoryRecord, *, now: float, weights: Weights,
                 semantic: float = 0.0, lexical: float = 0.0, graph: float = 0.0,
                 known_at: Optional[float] = None,
                 recency_tau_days: float = 30.0) -> float:
    """Score one ordinary query-recall candidate without age double-counting.

    Retention measures time since the candidate was last reinforced.  Recency is
    deliberately excluded here because it measures when the fact was valid or
    ingested; for an unreinforced record, using both makes age count twice.  The
    separate :func:`score_proactive` agenda retains its explicit recency signal.

    ``recency_tau_days`` is retained as an ignored compatibility parameter for
    callers that configured previous releases.

    When ``known_at`` predates a retroactive closure's system-time record, that
    closure cannot contribute a staleness penalty to the historical ranking.
    """
    w = weights
    r = retention(rec.stability, rec.last_access, now)
    known_valid_to = rec.valid_to
    if (
        known_at is not None
        and rec.valid_to_recorded_at is not None
        and known_at < rec.valid_to_recorded_at
    ):
        known_valid_to = None
    x = staleness_penalty(known_valid_to, now)
    confidence = _confidence(getattr(rec, "confidence", 1.0))
    importance = _bounded(getattr(rec, "importance", 0.0))
    return (
        _finite_number(getattr(w, "r", 0.0)) * r
        + _finite_number(getattr(w, "s", 0.0)) * _finite_number(semantic)
        + _finite_number(getattr(w, "l", 0.0)) * _finite_number(lexical)
        + _finite_number(getattr(w, "g", 0.0)) * _finite_number(graph)
        + _finite_number(getattr(w, "i", 0.0)) * importance * confidence
        - _finite_number(getattr(w, "x", 0.0)) * x
    )


def score_proactive(rec: MemoryRecord, *, now: float, weights: Optional[Weights] = None,
                    importance_retention_floor: Optional[float] = None) -> float:
    """Rank a queryless proactive agenda without turning decay into hard deletion.

    Importance is valuable while the record is retained, but it must not create an
    immortal second retention term.  ``importance_retention_floor`` remains accepted
    for call compatibility and deliberately no longer alters scoring.
    """
    w = weights or weights_for(rec.mtype)
    importance = _bounded(getattr(rec, "importance", 0.0))
    del importance_retention_floor
    r = retention(rec.stability, rec.last_access, now)
    rec_ref = rec.valid_from if rec.valid_from is not None else rec.ingested_at
    confidence = _confidence(getattr(rec, "confidence", 1.0))
    importance_signal = importance * r * confidence
    return (
        _finite_number(getattr(w, "i", 0.0)) * importance_signal
        + _finite_number(getattr(w, "c", 0.0)) * recency(rec_ref, now)
        + _finite_number(getattr(w, "r", 0.0)) * r
        - _finite_number(getattr(w, "x", 0.0)) * staleness_penalty(
            rec.valid_to, now
        )
    )
