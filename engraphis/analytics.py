"""Workspace analytics — the Pro dashboard's data layer (docs/LAUNCH_PLAN.md §3.4).

House style (AGENTS.md §3): the math is a pure, tested function over plain rows
(:func:`analytics_from_rows`); SQL lives only in the thin :func:`compute_analytics`
wrapper. stdlib + core only — no new dependency, no LLM, no network.

Gating note: the *license check does not live here*. This module computes for whoever
calls it; the Inspector's HTTP layer enforces ``require_feature("analytics")``. Keeping
the gate at the edge means the core stays honestly open (Apache-2.0) and the paid
surface is exactly one, auditable place.
"""
from __future__ import annotations

import math
import time
from typing import Any, Iterable, Optional

from engraphis.core.scoring import retention

#: Retention level treated as "effectively forgotten" — matches the consolidation
#: sweep's ``archive_below`` default so the forecast predicts what the next sweep does.
FORGET_THRESHOLD = 0.05

_WEEK = 7 * 86400.0
_GROWTH_WEEKS = 12
_HIST_BUCKETS = 5


def _days_until_forgotten(stability: float, last_access: Optional[float],
                          now: float) -> float:
    """Days until Ebbinghaus retention exp(-Δt/S) crosses :data:`FORGET_THRESHOLD`.

    Solving exp(-dt/S) = T gives dt = S·ln(1/T); subtract days already elapsed.
    Returns +inf for pinned-style stability outliers only via natural math (S huge).
    """
    s = max(stability or 1.0, 1e-3)
    horizon_days = s * math.log(1.0 / FORGET_THRESHOLD)
    elapsed_days = max((now - (last_access if last_access is not None else now)) / 86400.0, 0.0)
    return horizon_days - elapsed_days


def analytics_from_rows(mem_rows: Iterable[dict], audit_counts: dict,
                        entity_rows: Iterable[dict], *, now: Optional[float] = None) -> dict:
    """Pure aggregation. ``mem_rows`` need: mtype, stability, last_access, ingested_at,
    importance, pinned, valid_to, expired_at. ``audit_counts`` maps action→count.
    ``entity_rows`` need: name, etype, n (mention/edge count)."""
    now = time.time() if now is None else now
    rows = list(mem_rows)
    live = [r for r in rows
            if r.get("expired_at") is None
            and (r.get("valid_to") is None or now < r["valid_to"])]

    # ── growth: weekly ingest counts, oldest→newest, exactly _GROWTH_WEEKS buckets ──
    growth = [0] * _GROWTH_WEEKS
    for r in rows:
        ts = r.get("ingested_at")
        if ts is None:
            continue
        idx = _GROWTH_WEEKS - 1 - int((now - ts) // _WEEK)
        if 0 <= idx < _GROWTH_WEEKS:
            growth[idx] += 1

    # ── retention histogram over live memories ──
    hist = [0] * _HIST_BUCKETS
    ret_sum = 0.0
    for r in live:
        ret = retention(r.get("stability") or 1.0, r.get("last_access"), now)
        ret_sum += ret
        hist[min(int(ret * _HIST_BUCKETS), _HIST_BUCKETS - 1)] += 1

    # ── decay forecast (pinned memories are exempt from decay archival) ──
    at_risk_7 = at_risk_30 = 0
    for r in live:
        if r.get("pinned"):
            continue
        days = _days_until_forgotten(r.get("stability") or 1.0, r.get("last_access"), now)
        if days <= 7:
            at_risk_7 += 1
        if days <= 30:
            at_risk_30 += 1

    by_type: dict = {}
    for r in live:
        by_type[r.get("mtype") or "?"] = by_type.get(r.get("mtype") or "?", 0) + 1

    total = len(rows)
    n_live = len(live)
    return {
        "generated_at": now,
        "totals": {
            "live": n_live,
            "all_rows": total,
            "superseded": total - n_live,
            "pinned": sum(1 for r in live if r.get("pinned")),
            "avg_retention": round(ret_sum / n_live, 4) if n_live else 0.0,
        },
        "growth_weekly": growth,
        "retention_histogram": {
            "buckets": ["0–20%", "20–40%", "40–60%", "60–80%", "80–100%"],
            "counts": hist,
        },
        "decay_forecast": {
            "threshold": FORGET_THRESHOLD,
            "at_risk_7d": at_risk_7,
            "at_risk_30d": at_risk_30,
        },
        "by_type": by_type,
        "resolver_mix": {k: int(v) for k, v in sorted(audit_counts.items())},
        "top_entities": [
            {"name": e["name"], "etype": e.get("etype") or "", "n": int(e.get("n") or 0)}
            for e in entity_rows
        ],
    }


def compute_analytics(store: Any, workspace_id: str, *, now: Optional[float] = None) -> dict:
    """SQL wrapper: fetch rows for one workspace, delegate to the pure function."""
    conn = store.conn
    mem_rows = [dict(r) for r in conn.execute(
        "SELECT mtype, stability, last_access, ingested_at, importance, pinned, "
        "valid_to, expired_at FROM memories WHERE workspace_id=?", (workspace_id,))]
    audit_counts = {r["action"]: r["n"] for r in conn.execute(
        "SELECT a.action, COUNT(*) AS n FROM audit a JOIN memories m ON m.id = a.target "
        "WHERE m.workspace_id=? GROUP BY a.action", (workspace_id,))}
    entity_rows = [dict(r) for r in conn.execute(
        "SELECT e.name, e.etype, COUNT(ed.id) AS n FROM entities e "
        "LEFT JOIN edges ed ON (ed.src = e.id OR ed.dst = e.id) "
        "WHERE e.workspace_id=? GROUP BY e.id, e.name, e.etype "
        "ORDER BY n DESC, e.created_at DESC LIMIT 8", (workspace_id,))]
    return analytics_from_rows(mem_rows, audit_counts, entity_rows, now=now)
