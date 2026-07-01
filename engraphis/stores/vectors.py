"""Memory/document store — CRUD for the memories + chunks tables."""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional

import numpy as np

from engraphis.stores import blob_to_vector, get_conn, now_ts, vector_to_blob


def upsert_memory(
    *,
    namespace: str,
    document_id: str,
    title: str,
    content: str,
    metadata: Optional[dict] = None,
    source_type: Optional[str] = None,
    priority: Optional[str] = None,
    vector: Optional[np.ndarray] = None,
    created_at: Optional[float] = None,
    updated_at: Optional[float] = None,
    memory_type: str = "semantic",
) -> dict[str, Any]:
    """Insert or update a memory row. Returns the row as a dict."""
    conn = get_conn()
    ts = now_ts()
    created_at = created_at or ts
    updated_at = updated_at or ts
    meta_json = json.dumps(metadata or {}, ensure_ascii=False)
    vec_blob = vector_to_blob(vector) if vector is not None else None

    existing = conn.execute(
        "SELECT id, access_count, stability, surprise, last_access, memory_type "
        "FROM memories WHERE namespace=? AND document_id=?",
        (namespace, document_id),
    ).fetchone()

    if existing:
        # Preserve existing memory_type if not explicitly changed
        conn.execute(
            """UPDATE memories SET
                 title=?, content=?, metadata=?, source_type=?, priority=?,
                 vector=?, updated_at=?
               WHERE namespace=? AND document_id=?""",
            (title, content, meta_json, source_type, priority,
             vec_blob, updated_at, namespace, document_id),
        )
        row = get_memory(namespace, document_id)
    else:
        conn.execute(
            """INSERT INTO memories
                 (namespace, document_id, title, content, metadata, source_type,
                  priority, vector, created_at, updated_at, last_access,
                  access_count, stability, surprise, memory_type)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (namespace, document_id, title, content, meta_json, source_type,
             priority, vec_blob, created_at, updated_at, created_at,
             0, 1.0, 1.0, memory_type),
        )
        row = get_memory(namespace, document_id)

    conn.commit()
    return row


def get_memory(namespace: str, document_id: str) -> Optional[dict[str, Any]]:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM memories WHERE namespace=? AND document_id=?",
        (namespace, document_id),
    ).fetchone()
    return _row_to_mem(row) if row else None


def list_documents(
    namespace: Optional[str] = None,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
) -> list[dict[str, Any]]:
    conn = get_conn()
    sql = "SELECT * FROM memories"
    params: list[Any] = []
    if namespace:
        sql += " WHERE namespace=?"
        params.append(namespace)
    sql += " ORDER BY updated_at DESC"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    if offset:
        sql += " OFFSET ?"
        params.append(offset)
    rows = conn.execute(sql, params).fetchall()
    return [_row_to_mem(r) for r in rows]


def delete_memory_document(document_id: str, namespace: str) -> int:
    conn = get_conn()
    cur = conn.execute(
        "DELETE FROM memories WHERE namespace=? AND document_id=?",
        (namespace, document_id),
    )
    conn.commit()
    return cur.rowcount


def update_memory_content(
    namespace: str,
    document_id: str,
    *,
    title: Optional[str] = None,
    content: Optional[str] = None,
    metadata: Optional[dict] = None,
    vector: Optional[np.ndarray] = None,
    memory_type: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Update a memory's content/title/metadata/type and optionally re-embed."""
    conn = get_conn()
    sets = []
    params = []
    if title is not None:
        sets.append("title=?")
        params.append(title)
    if content is not None:
        sets.append("content=?")
        params.append(content)
    if metadata is not None:
        sets.append("metadata=?")
        params.append(json.dumps(metadata, ensure_ascii=False))
    if vector is not None:
        sets.append("vector=?")
        params.append(vector_to_blob(vector))
    if memory_type is not None:
        sets.append("memory_type=?")
        params.append(memory_type)
    if not sets:
        return get_memory(namespace, document_id)
    sets.append("updated_at=?")
    params.append(now_ts())
    params.extend([namespace, document_id])
    conn.execute(
        f"UPDATE memories SET {', '.join(sets)} WHERE namespace=? AND document_id=?",
        params,
    )
    conn.commit()
    return get_memory(namespace, document_id)


def move_memory(document_id: str, from_ns: str, to_ns: str) -> bool:
    """Move a memory from one namespace to another."""
    conn = get_conn()
    cur = conn.execute(
        "UPDATE memories SET namespace=?, updated_at=? WHERE namespace=? AND document_id=?",
        (to_ns, now_ts(), from_ns, document_id),
    )
    conn.commit()
    return cur.rowcount > 0


def bulk_delete(namespace: str, document_ids: list[str]) -> int:
    """Delete multiple memories by document_id within a namespace."""
    conn = get_conn()
    count = 0
    for doc_id in document_ids:
        cur = conn.execute(
            "DELETE FROM memories WHERE namespace=? AND document_id=?",
            (namespace, doc_id),
        )
        count += cur.rowcount
    conn.commit()
    return count


def delete_namespace(namespace: str) -> int:
    """Delete ALL memories, chunks, entities, edges, events, thoughts in a namespace."""
    conn = get_conn()
    count = 0
    for table in ("chunks", "edges", "entities", "events",
                  "interactions", "thoughts", "memories"):
        cur = conn.execute(f"DELETE FROM {table} WHERE namespace=?", (namespace,))
        if table == "memories":
            count = cur.rowcount
    conn.commit()
    return count


def all_vectors(namespace: Optional[str] = None) -> list[tuple[int, str, str, np.ndarray, dict]]:
    """Return (id, namespace, document_id, vector, mem_dict) for all memories
    that have a vector. Used by the recall engine for cosine search."""
    conn = get_conn()
    sql = "SELECT * FROM memories WHERE vector IS NOT NULL"
    params: list[Any] = []
    if namespace:
        sql += " AND namespace=?"
        params.append(namespace)
    rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        mem = _row_to_mem(r)
        vec = blob_to_vector(r["vector"])
        out.append((r["id"], mem["namespace"], mem["document_id"], vec, mem))
    return out


def touch_memory(mem_id: int, *, stability: Optional[float] = None,
                 surprise: Optional[float] = None) -> None:
    """Record an access (reinforcement). Called by the recall engine."""
    conn = get_conn()
    now = now_ts()
    if stability is not None and surprise is not None:
        conn.execute(
            "UPDATE memories SET last_access=?, access_count=access_count+1, "
            "stability=?, surprise=? WHERE id=?",
            (now, stability, surprise, mem_id),
        )
    else:
        conn.execute(
            "UPDATE memories SET last_access=?, access_count=access_count+1 WHERE id=?",
            (now, mem_id),
        )
    conn.commit()


def set_retention(mem_id: int, stability: float, surprise: float) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE memories SET stability=?, surprise=? WHERE id=?",
        (stability, surprise, mem_id),
    )
    conn.commit()


def apply_decay_to_all(namespace: Optional[str], halflife_days: float) -> int:
    """Ebbinghaus decay pass: reduce stability for memories not recently accessed.
    Returns the number of rows touched."""
    conn = get_conn()
    now = now_ts()
    rows = conn.execute(
        "SELECT id, stability, last_access, access_count FROM memories"
        + (" WHERE namespace=?" if namespace else ""),
        ([namespace] if namespace else []),
    ).fetchall()
    touched = 0
    for r in rows:
        days_since = (now - r["last_access"]) / 86400.0
        if days_since < 1e-6:
            continue
        decay_factor = 0.5 ** (days_since / max(halflife_days, 0.1))
        new_stab = max(r["stability"] * (0.5 + 0.5 * decay_factor), 0.01)
        if abs(new_stab - r["stability"]) > 1e-6:
            conn.execute(
                "UPDATE memories SET stability=? WHERE id=?",
                (new_stab, r["id"]),
            )
            touched += 1
    if touched:
        conn.commit()
    return touched


def _row_to_mem(row: sqlite3.Row) -> dict[str, Any]:
    if row is None:
        return None
    d = dict(row)
    d["metadata"] = json.loads(d.get("metadata") or "{}")
    d.pop("vector", None)
    return d
