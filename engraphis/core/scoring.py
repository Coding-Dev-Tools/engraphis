"""Recall scoring.

Pure, testable functions for the six-term Engraphis recall score:

    score = w_r·retention + w_s·semantic + w_l·lexical + w_g·graph
          + w_i·importance + w_c·recency − w_x·staleness

Weights are per memory type (a procedural memory weights importance/graph higher;
a working memory weights recency higher), and arm scores are min-max normalized
before fusion so no single arm dominates by raw scale. This is the concrete fix
for "similar ≠ important": semantic similarity is one term among six.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from engraphis.core.interfaces import MemoryRecord, MemoryType

# Interaction signals → stability boost (interaction-aware reinforcement).
INTERACTION_BOOST = {
    "view": 0.05, "read": 0.05, "recall": 0.15, "react": 0.20,
    "engage": 0.30, "reply": 0.50, "create": 1.00,
}

# ``0`` can occur as an "unspecified" value in legacy or synchronized data.  v2
# treats it as the normal default rather than silently turning an otherwise ordinary
# memory into a near-instantly forgotten one.  New v2 writes are validated positive.
DEFAULT_STABILITY_DAYS = 1.0

# Proactive recall is an agenda, not an answer-ranking path.  A memory the caller
# deliberately marked important remains eligible for that agenda even after its raw
# Ebbinghaus score has decayed.  This floor affects only the queryless ranking; it
# never mutates stability or changes normal query recall.
PROACTIVE_IMPORTANCE_RETENTION_FLOOR = 0.80


@dataclass(frozen=True)
class Weights:
    r: float = 1.0   # retention (Ebbinghaus)
    s: float = 1.0   # semantic similarity
    l: float = 0.5   # noqa: E741  (lexical weight w_l; single-letter to match the formula)
    g: float = 0.7   # graph proximity
    i: float = 0.6   # importance
    c: float = 0.3   # recency
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


def retention(stability: float, last_access: Optional[float], now: float) -> float:
    """Ebbinghaus R(t) = exp(-Δt_days / S).

    ``stability=0`` is a v1-import compatibility sentinel for an unspecified
    value, so it deliberately means the v2 default of one day.  It is *not* a
    request to hard-forget the record; forgetting only lowers priority.
    """
    try:
        supplied = float(stability)
    except (TypeError, ValueError):
        supplied = DEFAULT_STABILITY_DAYS
    S = supplied if math.isfinite(supplied) and supplied > 0 else DEFAULT_STABILITY_DAYS
    dt_days = max((now - (last_access if last_access is not None else now)) / 86400.0, 0.0)
    return math.exp(-dt_days / S)


def recency(t_ref: Optional[float], now: float, tau_days: float = 30.0) -> float:
    """Exponential recency on world-time, for tie-breaking and temporal queries."""
    if t_ref is None:
        return 0.0
    dt_days = max((now - t_ref) / 86400.0, 0.0)
    return math.exp(-dt_days / max(tau_days, 1e-6))


def staleness_penalty(valid_to: Optional[float], now: float,
                      ramp_days: float = 7.0) -> float:
    """1.0 once a fact is past its validity; ramps up in the ``ramp_days`` before."""
    if valid_to is None:
        return 0.0
    if now >= valid_to:
        return 1.0
    days_left = (valid_to - now) / 86400.0
    if days_left >= ramp_days:
        return 0.0
    return 1.0 - (days_left / ramp_days)


def normalize(scores: dict[str, float]) -> dict[str, float]:
    """Min-max normalize to [0, 1]; flat inputs map to 1.0."""
    if not scores:
        return {}
    vals = list(scores.values())
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-12:
        return {k: 1.0 for k in scores}
    return {k: (v - lo) / (hi - lo) for k, v in scores.items()}


def reciprocal_rank_fusion(rankings: list[list[str]], k: int = 60) -> dict[str, float]:
    """RRF across arms — rewards items ranked highly by multiple retrieval arms."""
    fused: dict[str, float] = {}
    for ranking in rankings:
        for rank, mid in enumerate(ranking):
            fused[mid] = fused.get(mid, 0.0) + 1.0 / (k + rank + 1)
    return fused


def score_memory(rec: MemoryRecord, *, now: float, weights: Weights,
                 semantic: float = 0.0, lexical: float = 0.0, graph: float = 0.0,
                 recency_tau_days: float = 30.0) -> float:
    """The six-term recall score for a single candidate."""
    w = weights
    r = retention(rec.stability, rec.last_access, now)
    rec_ref = rec.valid_from if rec.valid_from is not None else rec.ingested_at
    c = recency(rec_ref, now, recency_tau_days)
    x = staleness_penalty(rec.valid_to, now)
    return (w.r * r + w.s * semantic + w.l * lexical + w.g * graph
            + w.i * (rec.importance or 0.0) + w.c * c - w.x * x)


def score_proactive(rec: MemoryRecord, *, now: float, weights: Optional[Weights] = None,
                    importance_retention_floor: Optional[float] = None) -> float:
    """Rank a queryless proactive agenda without turning decay into hard deletion.

    The raw retention curve still governs ordinary memories.  Explicitly important
    records receive a bounded eligibility floor, so a useful week-old policy is not
    displaced solely by a newly written zero-importance scratch note.
    """
    w = weights or weights_for(rec.mtype)
    importance = min(max(float(rec.importance or 0.0), 0.0), 1.0)
    floor = PROACTIVE_IMPORTANCE_RETENTION_FLOOR
    if importance_retention_floor is not None:
        floor = min(max(float(importance_retention_floor), 0.0), 1.0)
    r = max(
        retention(rec.stability, rec.last_access, now),
        importance * floor,
    )
    rec_ref = rec.valid_from if rec.valid_from is not None else rec.ingested_at
    return w.i * importance + w.c * recency(rec_ref, now) + w.r * r
