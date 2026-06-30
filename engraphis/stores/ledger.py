"""Event ledger — append-only ordered state-transition log + interactions + thoughts + jobs."""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Optional

from engraphis.stores import get_conn, now_ts


# ── Events ───────────────────────────────────────────────────────────────────

def append_event(*, namespace: str, entity_name: str, event_type: str,
                 description: Optional[str] = None, payload: Optional[dict] = None,
                 timestamp: Optional[float] = None) -> int:
    conn = get_conn()
    cur = conn.execute(
        """INSERT INTO events (namespace, entity_name, event_type, description, payload, timestamp)
           VALUES (?,?,?,?,?,?)""",
        (namespace, entity_name, event_type, description,
         json.dumps(payload or {}, ensure_ascii=False), timestamp or now_ts()),
    )
    conn.commit()
    return cur.lastrowid


def get_events(namespace: str, entity_name: Optional[str] = None,
               limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    conn = get_conn()
    if entity_name:
        rows = conn.execute(
            "SELECT * FROM events WHERE namespace=? AND entity_name=? "
            "ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            (namespace, entity_name, limit, offset),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM events WHERE namespace=? ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            (namespace, limit, offset),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["payload"] = json.loads(d.get("payload") or "{}")
        out.append(d)
    return out


# ── Interactions ─────────────────────────────────────────────────────────────

def record_interaction(*, namespace: str, entity_name: str,
                       interaction_level: Optional[str] = None,
                       description: Optional[str] = None,
                       timestamp: Optional[float] = None) -> int:
    conn = get_conn()
    cur = conn.execute(
        """INSERT INTO interactions (namespace, entity_name, interaction_level, description, timestamp)
           VALUES (?,?,?,?,?)""",
        (namespace, entity_name, interaction_level, description, timestamp or now_ts()),
    )
    conn.commit()
    return cur.lastrowid


def get_interactions(namespace: str, limit: int = 100) -> list[dict[str, Any]]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM interactions WHERE namespace=? ORDER BY timestamp DESC LIMIT ?",
        (namespace, limit),
    ).fetchall()
    return [dict(r) for r in rows]


# ── Thoughts ─────────────────────────────────────────────────────────────────

def save_thought(*, namespace: str, content: str,
                 source_memory_ids: Optional[list[int]] = None) -> int:
    conn = get_conn()
    cur = conn.execute(
        """INSERT INTO thoughts (namespace, content, source_memory_ids, created_at)
           VALUES (?,?,?,?)""",
        (namespace, content,
         json.dumps(source_memory_ids or [], ensure_ascii=False), now_ts()),
    )
    conn.commit()
    return cur.lastrowid


def get_thoughts(namespace: str, limit: int = 50) -> list[dict[str, Any]]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM thoughts WHERE namespace=? ORDER BY created_at DESC LIMIT ?",
        (namespace, limit),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["source_memory_ids"] = json.loads(d.get("source_memory_ids") or "[]")
        out.append(d)
    return out


# ── Ingestion Jobs ───────────────────────────────────────────────────────────

def create_job(*, namespace: Optional[str], job_type: str,
               payload: Optional[dict] = None) -> dict[str, Any]:
    job_id = uuid.uuid4().hex[:16]
    conn = get_conn()
    now = now_ts()
    conn.execute(
        """INSERT INTO jobs (job_id, namespace, job_type, state, payload, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?)""",
        (job_id, namespace, job_type, "completed", json.dumps(payload or {}), now, now),
    )
    conn.commit()
    return get_job(job_id)


def get_job(job_id: str) -> Optional[dict[str, Any]]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["payload"] = json.loads(d.get("payload") or "{}")
    return d


def list_jobs(limit: int = 50) -> list[dict[str, Any]]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["payload"] = json.loads(d.get("payload") or "{}")
        out.append(d)
    return out
