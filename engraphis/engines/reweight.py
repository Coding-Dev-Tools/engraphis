"""Retention / decay engine — Ebbinghaus forgetting curve + interaction-aware
reinforcement.

Formulas (shared with the v2 retention policy):
    R(t) = exp(-t / S)                              retention at time t
    ΔS = (α * min(S, 1) + boost) * ln(1 + 1 / n)   nth-event reinforcement
    S_new = min(100, S + ΔS)                        bounded stability
    surprise = 1 + |prediction_error|               novelty weight

The decay pass reduces S for memories not recently accessed (subconscious
forgetting). The reinforcement pass increases S when a memory is recalled or
interacted with (consolidation).
"""
from __future__ import annotations

import math
from typing import Any, Optional

from engraphis.config import settings
from engraphis.stores import get_conn, now_ts
from engraphis.stores import vectors as mem_store
from engraphis.core.store import _escape_like
from engraphis.core.retention_policy import (
    DEFAULT_REINFORCEMENT_ALPHA,
    MAX_ACCESS_COUNT,
    effective_access_count,
    effective_stability,
    reinforced_stability,
)

_INTERACTION_BOOST = {
    "view": 0.05,
    "react": 0.20,
    "reply": 0.50,
    "create": 1.00,
    "engage": 0.30,
    "recall": 0.15,
    "read": 0.05,
}

_ALPHA = DEFAULT_REINFORCEMENT_ALPHA


def retention_score(mem: dict[str, Any], now: Optional[float] = None) -> float:
    """Return a finite Ebbinghaus score in the invariant range ``[0, 1]``."""
    reference = now_ts() if now is None else now
    try:
        reference = float(reference)
    except (TypeError, ValueError, OverflowError):
        reference = now_ts()
    if not math.isfinite(reference):
        reference = now_ts()
    try:
        last_access = float(mem.get("last_access", reference))
    except (TypeError, ValueError, OverflowError):
        last_access = reference
    if not math.isfinite(last_access):
        last_access = reference
    days = max(0.0, (reference - last_access) / 86400.0)
    score = math.exp(-days / effective_stability(mem.get("stability", 1.0)))
    return min(1.0, max(0.0, score))


def reinforce(mem_id: int, *, access_count_delta: int = 1) -> None:
    """Apply one or more bounded marginal-log reinforcement events."""
    if (
        isinstance(access_count_delta, bool)
        or not isinstance(access_count_delta, int)
        or not 0 <= access_count_delta <= 10_000
    ):
        raise ValueError("access_count_delta must be an integer from 0 to 10000")
    if access_count_delta == 0:
        return
    conn = get_conn()
    row = conn.execute(
        "SELECT stability, access_count FROM memories WHERE id=?", (mem_id,)
    ).fetchone()
    if not row:
        return
    stability = effective_stability(row["stability"])
    count = effective_access_count(row["access_count"])
    for _ in range(min(access_count_delta, MAX_ACCESS_COUNT - min(count, MAX_ACCESS_COUNT))):
        stability, count = reinforced_stability(
            stability,
            count,
            alpha=_ALPHA,
        )
    conn.execute(
        "UPDATE memories SET stability=?, access_count=?, last_access=? WHERE id=?",
        (stability, count, now_ts(), mem_id),
    )
    conn.commit()


def apply_interaction_boost(mem_id: int, interaction_level: str) -> None:
    """Apply one bounded interaction reinforcement event."""
    boost = _INTERACTION_BOOST.get(interaction_level.lower(), 0.1)
    conn = get_conn()
    row = conn.execute(
        "SELECT stability, access_count FROM memories WHERE id=?", (mem_id,)
    ).fetchone()
    if not row:
        return
    stability, count = reinforced_stability(
        row["stability"],
        effective_access_count(row["access_count"]),
        alpha=_ALPHA,
        boost=boost,
    )
    conn.execute(
        "UPDATE memories SET stability=?, access_count=?, last_access=? WHERE id=?",
        (stability, count, now_ts(), mem_id),
    )
    conn.commit()


def boost_entity_memories(namespace: str, entity_name: str,
                          interaction_level: str) -> int:
    """Apply an interaction boost to memories that mention *entity_name*.

    This is what makes a recorded interaction actually reinforce memory (interaction-aware
    reinforcement); previously ``apply_interaction_boost`` had no caller, so interactions
    were logged but never affected retention. Matching is a bounded name-substring lookup
    (the entity name is a BOUND parameter — no SQL injection). Returns how many memories
    were reinforced."""
    name = (entity_name or "").strip()
    if not name:
        return 0
    conn = get_conn()
    like = "%" + _escape_like(name) + "%"
    rows = conn.execute(
        "SELECT id FROM memories WHERE namespace=? AND (title LIKE ? ESCAPE '\\' OR content LIKE ? ESCAPE '\\') "
        "LIMIT 100",
        (namespace, like, like),
    ).fetchall()
    for r in rows:
        apply_interaction_boost(r["id"], interaction_level)
    return len(rows)


def decay_pass(namespace: Optional[str] = None) -> int:
    """Background decay: reduce stability for stale memories. Returns rows touched."""
    return mem_store.apply_decay_to_all(namespace, settings.decay_halflife_days)


def score_memory(mem: dict[str, Any], query_vec, mem_vec) -> float:
    """Return a finite legacy retention × cosine × surprise score."""
    import numpy as np

    retention = retention_score(mem)
    try:
        semantic = (
            float(np.dot(query_vec, mem_vec)) if query_vec is not None else 0.0
        )
    except (TypeError, ValueError, OverflowError):
        semantic = 0.0
    if not math.isfinite(semantic):
        semantic = 0.0
    try:
        surprise = float(mem.get("surprise", 1.0))
    except (TypeError, ValueError, OverflowError):
        surprise = 1.0
    if not math.isfinite(surprise):
        surprise = 1.0
    score = retention * semantic * surprise
    return score if math.isfinite(score) else 0.0
