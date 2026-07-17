"""Retention / decay engine — Ebbinghaus forgetting curve + interaction-aware
reinforcement.

Formulas (from the Engraphis paper §3.2 and MemoryBank):
    R(t) = exp(-t / S)                    retention at time t
    S_new = S * (1 + α * log(1 + n))      stability grows with access count n
    S += boost(level)                     interaction signals boost stability
    surprise = 1 + |prediction_error|     novelty weight

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

_INTERACTION_BOOST = {
    "view": 0.05,
    "react": 0.20,
    "reply": 0.50,
    "create": 1.00,
    "engage": 0.30,
    "recall": 0.15,
    "read": 0.05,
}

_ALPHA = 0.3


def retention_score(mem: dict[str, Any], now: Optional[float] = None) -> float:
    """Ebbinghaus retention R = exp(-t/S) where t is days since last access."""
    now = now or now_ts()
    S = max(mem.get("stability", 1.0), 0.01)
    days = (now - mem.get("last_access", now)) / 86400.0
    return math.exp(-days / S)


def reinforce(mem_id: int, *, access_count_delta: int = 1) -> None:
    """Reinforce a memory on recall — increase stability via spacing effect."""
    conn = get_conn()
    row = conn.execute(
        "SELECT stability, access_count FROM memories WHERE id=?", (mem_id,)
    ).fetchone()
    if not row:
        return
    new_count = row["access_count"] + access_count_delta
    growth = 1.0 + _ALPHA * math.log(1 + new_count)
    new_stab = row["stability"] * growth
    conn.execute(
        "UPDATE memories SET stability=?, access_count=?, last_access=? WHERE id=?",
        (new_stab, new_count, now_ts(), mem_id),
    )
    conn.commit()


def apply_interaction_boost(mem_id: int, interaction_level: str) -> None:
    """Boost stability based on interaction signal (view/react/reply/create)."""
    boost = _INTERACTION_BOOST.get(interaction_level.lower(), 0.1)
    conn = get_conn()
    row = conn.execute(
        "SELECT stability FROM memories WHERE id=?", (mem_id,)
    ).fetchone()
    if not row:
        return
    new_stab = row["stability"] + boost
    conn.execute(
        "UPDATE memories SET stability=?, last_access=? WHERE id=?",
        (new_stab, now_ts(), mem_id),
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
    like = "%" + name + "%"
    rows = conn.execute(
        "SELECT id FROM memories WHERE namespace=? AND (title LIKE ? OR content LIKE ?) "
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
    """Conscious Recall score = retention × cosine_similarity × surprise."""
    r = retention_score(mem)
    import numpy as np
    sim = float(np.dot(query_vec, mem_vec)) if query_vec is not None else 0.0
    surprise = mem.get("surprise", 1.0)
    return r * sim * surprise
