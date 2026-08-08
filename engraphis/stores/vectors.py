"""Memory/document store — CRUD for the memories + chunks tables."""
from __future__ import annotations

import json
import math
import sqlite3
from typing import Any, Optional

import numpy as np

from engraphis.stores import blob_to_vector, get_conn, now_ts
from engraphis.core.retention_policy import (
    MIN_STABILITY_DAYS,
    effective_access_count,
    effective_stability,
)


def _vector_blob(vector: np.ndarray) -> bytes:
    """Validate and normalize one persisted embedding without device assumptions."""
    try:
        values = np.asarray(vector, dtype=np.float32)
    except (TypeError, ValueError) as exc:
        raise ValueError("vector must be a finite 1-D float32 array") from exc
    if values.ndim != 1 or values.shape[0] < 1:
        raise ValueError("vector must be a 1-D array with a positive dimension")
    if not np.isfinite(values).all():
        raise ValueError("vector must contain only finite values")
    with np.errstate(over="ignore", invalid="ignore"):
        norm = float(np.linalg.norm(values))
    if not np.isfinite(norm):
        raise ValueError("vector norm must be finite")
    if norm > 0:
        values = values / norm
    return values.tobytes()


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
    commit: bool = True,
) -> dict[str, Any]:
    """Insert or update a memory row. Returns the row as a dict."""
    conn = get_conn()
    ts = now_ts()
    created_at = ts if created_at is None else created_at
    updated_at = ts if updated_at is None else updated_at
    stamped_metadata = dict(metadata or {})
    if "provenance" not in stamped_metadata:
        stamped_metadata["provenance"] = {
            "source": "legacy_store",
            "trusted": True,
            "trust_origin": "legacy_store",
            "review_state": "approved",
        }
    meta_json = json.dumps(
        stamped_metadata, ensure_ascii=False, allow_nan=False
    )
    vec_blob = _vector_blob(vector) if vector is not None else None

    existing = conn.execute(
        "SELECT id, access_count, stability, surprise, last_access, memory_type "
        "FROM memories WHERE namespace=? AND document_id=?",
        (namespace, document_id),
    ).fetchone()

    if existing:
        # Preserve the existing memory_type when the caller did not explicitly
        # override it (i.e. passed the default "semantic"). This prevents silent
        # reversion of curated types (e.g. episodic) on re-ingest.
        effective_type = memory_type
        if memory_type == "semantic" and existing["memory_type"] != "semantic":
            effective_type = existing["memory_type"]
        conn.execute(
            """UPDATE memories SET
                 title=?, content=?, metadata=?, source_type=?, priority=?,
                 vector=?, updated_at=?, memory_type=?
               WHERE namespace=? AND document_id=?""",
            (title, content, meta_json, source_type, priority,
             vec_blob, updated_at, effective_type, namespace, document_id),
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

    if commit:
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
    if namespace is not None:
        sql += " WHERE namespace=?"
        params.append(namespace)
    sql += " ORDER BY updated_at DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    if offset is not None:
        if limit is None:
            sql += " LIMIT -1"
        sql += " OFFSET ?"
        params.append(offset)
    rows = conn.execute(sql, params).fetchall()
    return [_row_to_mem(r) for r in rows]


def find_document(document_id: str, namespace: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Fetch a memory by ``document_id``. With a namespace, scope to it; without one, return
    the most recently updated match across all namespaces (document_id is only unique
    per-namespace, so a bare lookup picks the newest rather than always missing)."""
    conn = get_conn()
    if namespace is not None:
        row = conn.execute(
            "SELECT * FROM memories WHERE namespace=? AND document_id=?",
            (namespace, document_id)).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM memories WHERE document_id=? ORDER BY updated_at DESC LIMIT 1",
            (document_id,)).fetchone()
    return _row_to_mem(row) if row else None


def delete_memory_document(document_id: str, namespace: str) -> int:
    """Delete one memory and rebuild its document-derived graph atomically."""
    conn = get_conn()
    from engraphis.stores import graph as graph_store

    try:
        cur = conn.execute(
            "DELETE FROM memories WHERE namespace=? AND document_id=?",
            (namespace, document_id),
        )
        if cur.rowcount:
            graph_store.rebuild_namespace(namespace, commit=False)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
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
    commit: bool = True,
) -> Optional[dict[str, Any]]:
    """Update a memory while preserving authority-bearing provenance metadata."""
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
        existing = get_memory(namespace, document_id)
        if existing is None:
            return None
        replacement = dict(metadata)
        existing_metadata = existing.get("metadata")
        if isinstance(existing_metadata, dict):
            for key in (
                "provenance", "trusted", "review_state", "quarantined", "quarantine",
            ):
                if key in existing_metadata:
                    replacement[key] = existing_metadata[key]
        sets.append("metadata=?")
        params.append(json.dumps(
            replacement, ensure_ascii=False, allow_nan=False
        ))
    if vector is not None:
        sets.append("vector=?")
        params.append(_vector_blob(vector))
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
    if commit:
        conn.commit()
    return get_memory(namespace, document_id)


def move_memory(document_id: str, from_ns: str, to_ns: str) -> bool:
    """Move one memory, its chunks, and document graph evidence atomically."""
    conn = get_conn()
    from engraphis.stores import graph as graph_store

    row = conn.execute(
        "SELECT id FROM memories WHERE namespace=? AND document_id=?",
        (from_ns, document_id),
    ).fetchone()
    if row is None:
        return False
    marker = conn.execute(
        """SELECT updated_at FROM graph_documents
           WHERE namespace=? AND document_id=?""",
        (from_ns, document_id),
    ).fetchone()
    entities = conn.execute(
        """SELECT entity_name, entity_type FROM document_entities
           WHERE namespace=? AND document_id=?""",
        (from_ns, document_id),
    ).fetchall()
    edges = conn.execute(
        """SELECT source_entity, target_entity, relation FROM document_edges
           WHERE namespace=? AND document_id=?""",
        (from_ns, document_id),
    ).fetchall()
    try:
        # Remove the source marker before changing the referenced memory key. This also
        # supports databases created before ON UPDATE CASCADE was added.
        conn.execute(
            "DELETE FROM graph_documents WHERE namespace=? AND document_id=?",
            (from_ns, document_id),
        )
        conn.execute(
            "UPDATE memories SET namespace=?, updated_at=? "
            "WHERE namespace=? AND document_id=?",
            (to_ns, now_ts(), from_ns, document_id),
        )
        conn.execute(
            "UPDATE chunks SET namespace=? WHERE memory_id=?",
            (to_ns, row["id"]),
        )
        graph_store.rebuild_namespace(from_ns, commit=False)
        if marker is not None:
            graph_store.replace_document_evidence(
                to_ns,
                document_id,
                [(item["entity_name"], item["entity_type"]) for item in entities],
                [
                    (
                        item["source_entity"],
                        item["relation"],
                        item["target_entity"],
                    )
                    for item in edges
                ],
                updated_at=marker["updated_at"],
                commit=False,
            )
        else:
            graph_store.rebuild_namespace(to_ns, commit=False)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return True


def bulk_delete(namespace: str, document_ids: list[str]) -> int:
    """Delete multiple memories and refresh graph evidence once."""
    conn = get_conn()
    from engraphis.stores import graph as graph_store

    count = 0
    try:
        for doc_id in document_ids:
            cur = conn.execute(
                "DELETE FROM memories WHERE namespace=? AND document_id=?",
                (namespace, doc_id),
            )
            count += cur.rowcount
        if count:
            graph_store.rebuild_namespace(namespace, commit=False)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return count


def delete_namespace(namespace: str, *, commit: bool = True) -> int:
    """Delete all legacy rows in one namespace."""
    conn = get_conn()
    count = 0
    for table in (
        "chunks", "edges", "entities", "events", "interactions", "thoughts", "memories",
    ):
        cur = conn.execute(f"DELETE FROM {table} WHERE namespace=?", (namespace,))
        if table == "memories":
            count = cur.rowcount
    if commit:
        conn.commit()
    return count


def all_vectors(namespace: Optional[str] = None) -> list[tuple[int, str, str, np.ndarray, dict]]:
    """Return (id, namespace, document_id, vector, mem_dict) for all memories
    that have a vector. Used by the recall engine for cosine search."""
    conn = get_conn()
    sql = "SELECT * FROM memories WHERE vector IS NOT NULL"
    params: list[Any] = []
    if namespace is not None:
        sql += " AND namespace=?"
        params.append(namespace)
    rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        mem = _row_to_mem(r)
        try:
            vec = blob_to_vector(r["vector"])
        except (TypeError, ValueError):
            # A truncated/legacy-corrupt BLOB must not take down the entire
            # recall pass; only return well-formed finite vectors.
            continue
        if vec.ndim != 1 or vec.shape[0] < 1 or not np.isfinite(vec).all():
            continue
        out.append((r["id"], mem["namespace"], mem["document_id"], vec, mem))
    return out


def touch_memory(
    mem_id: int,
    *,
    stability: Optional[float] = None,
    surprise: Optional[float] = None,
) -> None:
    """Record an access while keeping persisted retention state finite."""
    conn = get_conn()
    now = now_ts()
    if stability is not None and surprise is not None:
        try:
            finite_surprise = float(surprise)
        except (TypeError, ValueError, OverflowError):
            finite_surprise = 1.0
        if not math.isfinite(finite_surprise):
            finite_surprise = 1.0
        row = conn.execute(
            "SELECT access_count FROM memories WHERE id=?", (mem_id,)
        ).fetchone()
        count = (
            min(effective_access_count(row["access_count"]) + 1, 1_000_000_000)
            if row else 0
        )
        conn.execute(
            "UPDATE memories SET last_access=?, access_count=?, "
            "stability=?, surprise=? WHERE id=?",
            (now, count, effective_stability(stability), finite_surprise, mem_id),
        )
    else:
        conn.execute(
            "UPDATE memories SET last_access=?, "
            "access_count=MIN(access_count+1, 1000000000) WHERE id=?",
            (now, mem_id),
        )
    conn.commit()


def set_retention(mem_id: int, stability: float, surprise: float) -> None:
    """Persist only finite bounded retention state."""
    try:
        finite_surprise = float(surprise)
    except (TypeError, ValueError, OverflowError):
        finite_surprise = 1.0
    if not math.isfinite(finite_surprise):
        finite_surprise = 1.0
    conn = get_conn()
    conn.execute(
        "UPDATE memories SET stability=?, surprise=? WHERE id=?",
        (effective_stability(stability), finite_surprise, mem_id),
    )
    conn.commit()


def apply_decay_to_all(namespace: Optional[str], halflife_days: float) -> int:
    """Apply finite interval decay once and advance every processed anchor."""
    try:
        halflife = float(halflife_days)
    except (TypeError, ValueError, OverflowError):
        halflife = MIN_STABILITY_DAYS
    if not math.isfinite(halflife) or halflife <= 0:
        halflife = MIN_STABILITY_DAYS
    halflife = max(halflife, MIN_STABILITY_DAYS)

    conn = get_conn()
    now = now_ts()
    rows = conn.execute(
        "SELECT id, stability, last_access, last_decay FROM memories"
        + (" WHERE namespace=?" if namespace is not None else ""),
        ([namespace] if namespace is not None else []),
    ).fetchall()
    touched = 0
    for row in rows:
        stability = effective_stability(row["stability"])
        try:
            raw_stability = float(row["stability"])
        except (TypeError, ValueError, OverflowError):
            raw_stability = stability
        try:
            last_access = float(row["last_access"])
        except (TypeError, ValueError, OverflowError):
            last_access = now
        if not math.isfinite(last_access):
            last_access = now
        raw_anchor = row["last_decay"]
        try:
            anchor = (
                last_access if raw_anchor is None else float(raw_anchor)
            )
        except (TypeError, ValueError, OverflowError):
            anchor = last_access
        if not math.isfinite(anchor):
            anchor = last_access
        if last_access > anchor:
            conn.execute(
                "UPDATE memories SET stability=?, last_decay=? WHERE id=?",
                (stability, now, row["id"]),
            )
            continue
        delta_days = max(0.0, (now - anchor) / 86400.0)
        new_stability = effective_stability(
            stability * (0.5 ** (delta_days / halflife))
        )
        if (
            abs(new_stability - stability) > 1e-9
            or abs(stability - raw_stability) > 1e-9
        ):
            touched += 1
        conn.execute(
            "UPDATE memories SET stability=?, last_decay=? WHERE id=?",
            (new_stability, now, row["id"]),
        )
    conn.commit()
    return touched


def _row_to_mem(row: sqlite3.Row) -> dict[str, Any]:
    if row is None:
        return None
    d = dict(row)
    try:
        d["metadata"] = json.loads(d.get("metadata") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        d["metadata"] = {}
    d.pop("vector", None)
    return d
