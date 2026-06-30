"""Recall scoring (MASTER_PLAN.md §7.1, §13.1).

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

from engraphis.core.interfaces import MemoryRecord, MemoryType

# Interaction signals → stability boost (neocortex-style reinforcement).
INTERACTION_BOOST = {
    "view": 0.05, "read": 0.05, "recall": 0.15, "react": 0.20,
    "engage": 0.30, "reply": 0.50, "create": 1.00,
}


@dataclass(frozen=True)
class Weights:
    r: float = 1.0   # retention (Ebbinghaus)
    s: float = 1.0   # semantic similarity
    l: float = 0.5   # lexical (BM25)
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


def retention(stability: float, last_access: float | None, now: float) -> float:
    """Ebbinghaus R(t) = exp(-Δt_days / S)."""
    S = max(stability or 1.0, 1e-3)
    dt_days = max((now - (last_access if last_access is not None else now)) / 86400.0, 0.0)
    return math.exp(-dt_days / S)


def recency(t_ref: float | None, now: float, tau_days: float = 30.0) -> float:
    """Exponential recency on world-time, for tie-breaking and temporal queries."""
    if t_ref is None:
        return 0.0
    dt_days = max((now - t_ref) / 86400.0, 0.0)
    return math.exp(-dt_days / max(tau_days, 1e-6))


def staleness_penalty(valid_to: float | None, now: float, ramp_days: float = 7.0) -> float:
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
