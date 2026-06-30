"""Entity-relation graph store — backed by SQLite tables."""
from __future__ import annotations

import time
from typing import Any, Optional

from engraphis.stores import get_conn, now_ts


def upsert_entity(namespace: str, name: str, entity_type: Optional[str] = None) -> None:
    conn = get_conn()
    conn.execute(
        """INSERT INTO entities (namespace, name, entity_type, created_at)
           VALUES (?,?,?,?)
           ON CONFLICT(namespace, name) DO UPDATE SET entity_type=COALESCE(excluded.entity_type, entity_type)""",
        (namespace, name, entity_type, now_ts()),
    )
    conn.commit()


def upsert_edge(namespace: str, source: str, target: str, relation: str,
                weight: float = 1.0) -> None:
    conn = get_conn()
    now = now_ts()
    conn.execute(
        """INSERT INTO edges (namespace, source_entity, target_entity, relation,
                              weight, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(namespace, source_entity, target_entity, relation)
           DO UPDATE SET weight=weight+excluded.weight, updated_at=?""",
        (namespace, source, target, relation, weight, now, now, now),
    )
    conn.commit()


def get_entities(namespace: str, limit: int = 500) -> list[dict[str, Any]]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT name, entity_type, created_at FROM entities WHERE namespace=? LIMIT ?",
        (namespace, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def get_edges(namespace: str, limit: int = 1000) -> list[dict[str, Any]]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT source_entity, target_entity, relation, weight "
        "FROM edges WHERE namespace=? ORDER BY weight DESC LIMIT ?",
        (namespace, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def get_neighbors(namespace: str, entity_name: str, limit: int = 50) -> list[dict[str, Any]]:
    conn = get_conn()
    rows = conn.execute(
        """SELECT target_entity AS neighbor, relation, weight FROM edges
           WHERE namespace=? AND source_entity=?
           UNION
           SELECT source_entity AS neighbor, relation, weight FROM edges
           WHERE namespace=? AND target_entity=?
           ORDER BY weight DESC LIMIT ?""",
        (namespace, entity_name, namespace, entity_name, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def graph_snapshot(namespace: Optional[str] = None, limit: int = 200,
                   seed_limit: int = 10) -> dict[str, Any]:
    """Return a serializable snapshot of entities + edges for the admin route.
    Each entity includes a list of memory document_ids that mention it."""
    from engraphis.stores import get_conn
    ns_filter = namespace
    entities = get_entities(ns_filter, limit=limit) if ns_filter else _all_entities(limit)
    edges = get_edges(ns_filter, limit=limit * 2) if ns_filter else _all_edges(limit * 2)

    # Enrich entities with the documents that mention them
    import json as _json
    conn = get_conn()
    for ent in entities:
        ent_ns = ent.get("namespace") or namespace or ""
        # The events table stores document_id inside the payload JSON column
        rows = conn.execute(
            "SELECT payload FROM events WHERE namespace=? AND entity_name=? LIMIT 20",
            (ent_ns, ent["name"]),
        ).fetchall()
        doc_ids = []
        for r in rows:
            try:
                payload = _json.loads(r["payload"] or "{}")
                did = payload.get("document_id")
                if did and did not in doc_ids:
                    doc_ids.append(did)
            except Exception:
                pass
        ent["documents"] = doc_ids[:10]
        # Get a preview from the first document
        if ent["documents"]:
            doc_row = conn.execute(
                "SELECT title, content FROM memories WHERE namespace=? AND document_id=? LIMIT 1",
                (ent_ns, ent["documents"][0]),
            ).fetchone()
            if doc_row:
                ent["preview_title"] = doc_row["title"]
                ent["preview_content"] = doc_row["content"][:200]
            else:
                ent["preview_title"] = ""
                ent["preview_content"] = ""
        else:
            ent["preview_title"] = ""
            ent["preview_content"] = ""

    return {
        "entities": entities[:limit],
        "edges": edges[:limit],
        "entity_count": len(entities),
        "edge_count": len(edges),
        "seed_limit": seed_limit,
    }


def _all_entities(limit: int) -> list[dict[str, Any]]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT namespace, name, entity_type, created_at FROM entities LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def _all_edges(limit: int) -> list[dict[str, Any]]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT namespace, source_entity, target_entity, relation, weight "
        "FROM edges ORDER BY weight DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]
