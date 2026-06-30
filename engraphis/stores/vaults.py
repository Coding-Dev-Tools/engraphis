"""Vault store — manages memory vaults (namespaces with metadata).

A vault is a named namespace with a description, color, memory type, and
active/inactive state. Only one vault can be active at a time — the active
vault is what new memories default to and what the dashboard focuses on.

Memory types (from cognitive science):
  - semantic    — facts, knowledge, preferences (default, slow decay)
  - episodic    — events, experiences, time-stamped (medium decay)
  - procedural  — how-to, workflows, step-by-step (slow decay, high retention)
  - working     — current task context (fast decay, short lifetime)
"""
from __future__ import annotations

import json
import time
from typing import Any, Optional

from engraphis.stores import get_conn, now_ts


def create_vault(*, namespace: str, name: str, description: str = "",
                 color: str = "#9d7cf6", memory_type: str = "semantic") -> dict[str, Any]:
    conn = get_conn()
    ts = now_ts()
    conn.execute(
        """INSERT INTO vaults (namespace, name, description, color, memory_type, is_active, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (namespace, name, description, color, memory_type, 0, ts, ts),
    )
    conn.commit()
    return get_vault(namespace)


def get_vault(namespace: str) -> Optional[dict[str, Any]]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM vaults WHERE namespace=?", (namespace,)).fetchone()
    return dict(row) if row else None


def list_vaults() -> list[dict[str, Any]]:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM vaults ORDER BY is_active DESC, name ASC").fetchall()
    vaults = [dict(r) for r in rows]
    # Attach memory count for each
    for v in vaults:
        count = conn.execute(
            "SELECT COUNT(*) as c FROM memories WHERE namespace=?", (v["namespace"],)
        ).fetchone()["c"]
        v["memory_count"] = count
        v["is_active"] = bool(v["is_active"])
    return vaults


def update_vault(namespace: str, *, name: Optional[str] = None,
                 description: Optional[str] = None, color: Optional[str] = None,
                 memory_type: Optional[str] = None) -> Optional[dict[str, Any]]:
    conn = get_conn()
    sets = []
    params = []
    if name is not None: sets.append("name=?"); params.append(name)
    if description is not None: sets.append("description=?"); params.append(description)
    if color is not None: sets.append("color=?"); params.append(color)
    if memory_type is not None: sets.append("memory_type=?"); params.append(memory_type)
    if not sets: return get_vault(namespace)
    sets.append("updated_at=?"); params.append(now_ts())
    params.append(namespace)
    conn.execute(f"UPDATE vaults SET {', '.join(sets)} WHERE namespace=?", params)
    conn.commit()
    return get_vault(namespace)


def set_active_vault(namespace: str) -> None:
    conn = get_conn()
    conn.execute("UPDATE vaults SET is_active=0")
    conn.execute("UPDATE vaults SET is_active=1 WHERE namespace=?", (namespace,))
    conn.commit()


def get_active_vault() -> Optional[dict[str, Any]]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM vaults WHERE is_active=1").fetchone()
    return dict(row) if row else None


def delete_vault(namespace: str, delete_memories: bool = True) -> dict[str, Any]:
    from engraphis.stores import vectors as mem_store
    conn = get_conn()
    deleted_memories = 0
    if delete_memories:
        deleted_memories = mem_store.delete_namespace(namespace)
    conn.execute("DELETE FROM vaults WHERE namespace=?", (namespace,))
    conn.commit()
    return {"namespace": namespace, "deleted_memories": deleted_memories}


def ensure_default_vault() -> None:
    """Create a default vault if none exist."""
    conn = get_conn()
    count = conn.execute("SELECT COUNT(*) as c FROM vaults").fetchone()["c"]
    if count == 0:
        create_vault(
            namespace="default",
            name="Default",
            description="General purpose memory vault",
            color="#9d7cf6",
            memory_type="semantic",
        )
        set_active_vault("default")
