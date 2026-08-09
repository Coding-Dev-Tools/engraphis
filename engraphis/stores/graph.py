"""Entity-relation graph store — backed by SQLite tables."""
from __future__ import annotations

from collections.abc import Iterable
import logging
from typing import Any, Optional

from engraphis.stores import get_conn, now_ts

logger = logging.getLogger("engraphis.stores.graph")


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


def _replace_support_rows(
    namespace: str,
    document_id: str,
    entities: list[tuple[str, str]],
    relations: list[tuple[str, str, str]],
    *,
    updated_at: float,
) -> None:
    conn = get_conn()
    conn.execute(
        """INSERT INTO graph_documents (namespace, document_id, updated_at)
           VALUES (?,?,?)
           ON CONFLICT(namespace, document_id)
           DO UPDATE SET updated_at=excluded.updated_at""",
        (namespace, document_id, updated_at),
    )
    conn.execute(
        "DELETE FROM document_edges WHERE namespace=? AND document_id=?",
        (namespace, document_id),
    )
    conn.execute(
        "DELETE FROM document_entities WHERE namespace=? AND document_id=?",
        (namespace, document_id),
    )
    conn.executemany(
        """INSERT INTO document_entities
           (namespace, document_id, entity_name, entity_type)
           VALUES (?,?,?,?)""",
        [
            (namespace, document_id, name, entity_type)
            for name, entity_type in entities
        ],
    )
    conn.executemany(
        """INSERT INTO document_edges
           (namespace, document_id, source_entity, target_entity, relation)
           VALUES (?,?,?,?,?)""",
        [
            (namespace, document_id, source, target, relation)
            for source, relation, target in relations
        ],
    )


def replace_document_evidence(
    namespace: str,
    document_id: str,
    entities: Iterable[tuple[str, str]],
    relations: Iterable[tuple[str, str, str]],
    *,
    updated_at: Optional[float] = None,
    commit: bool = True,
) -> None:
    """Replace one document's ``(source, relation, target)`` evidence and aggregates."""
    entity_rows = sorted(set(entities))
    relation_rows = sorted(set(relations))
    _replace_support_rows(
        namespace,
        document_id,
        entity_rows,
        relation_rows,
        updated_at=now_ts() if updated_at is None else updated_at,
    )
    rebuild_namespace(namespace, commit=commit)


def rebuild_namespace(namespace: str, *, commit: bool = True) -> None:
    """Derive aggregate entity and edge rows from live document evidence."""
    conn = get_conn()
    conn.execute("DELETE FROM edges WHERE namespace=?", (namespace,))
    conn.execute("DELETE FROM entities WHERE namespace=?", (namespace,))
    conn.execute(
        """INSERT INTO entities (namespace, name, entity_type, created_at)
           SELECT namespace, entity_name, MAX(entity_type), MIN(created_at)
           FROM (
               SELECT de.namespace, de.entity_name, de.entity_type,
                      gd.updated_at AS created_at
               FROM document_entities AS de
               JOIN graph_documents AS gd
                 ON gd.namespace=de.namespace AND gd.document_id=de.document_id
               WHERE de.namespace=?
               UNION ALL
               SELECT dx.namespace, dx.source_entity, NULL, gd.updated_at
               FROM document_edges AS dx
               JOIN graph_documents AS gd
                 ON gd.namespace=dx.namespace AND gd.document_id=dx.document_id
               WHERE dx.namespace=?
               UNION ALL
               SELECT dx.namespace, dx.target_entity, NULL, gd.updated_at
               FROM document_edges AS dx
               JOIN graph_documents AS gd
                 ON gd.namespace=dx.namespace AND gd.document_id=dx.document_id
               WHERE dx.namespace=?
           )
           GROUP BY namespace, entity_name""",
        (namespace, namespace, namespace),
    )
    conn.execute(
        """INSERT INTO edges
           (namespace, source_entity, target_entity, relation,
            weight, created_at, updated_at)
           SELECT dx.namespace, dx.source_entity, dx.target_entity, dx.relation,
                  COUNT(*) * 1.0, MIN(gd.updated_at), MAX(gd.updated_at)
           FROM document_edges AS dx
           JOIN graph_documents AS gd
             ON gd.namespace=dx.namespace AND gd.document_id=dx.document_id
           WHERE dx.namespace=?
           GROUP BY dx.namespace, dx.source_entity, dx.target_entity, dx.relation""",
        (namespace,),
    )
    if commit:
        conn.commit()


def remove_document_evidence(
    namespace: str,
    document_id: str,
    *,
    commit: bool = True,
) -> None:
    """Remove one document's support and refresh only its namespace."""
    conn = get_conn()
    conn.execute(
        "DELETE FROM graph_documents WHERE namespace=? AND document_id=?",
        (namespace, document_id),
    )
    rebuild_namespace(namespace, commit=commit)


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


def graph_snapshot(
    namespace: Optional[str] = None,
    limit: int = 200,
    seed_limit: int = 10,
) -> dict[str, Any]:
    """Return a deterministic graph page with explicit full-graph totals."""
    conn = get_conn()
    where = " WHERE namespace=?" if namespace is not None else ""
    params: tuple[Any, ...] = (namespace,) if namespace is not None else ()
    entity_total = conn.execute(
        f"SELECT COUNT(*) FROM entities{where}", params
    ).fetchone()[0]
    edge_total = conn.execute(
        f"SELECT COUNT(*) FROM edges{where}", params
    ).fetchone()[0]

    if namespace is None:
        edges = _all_edges(limit * 2)
        seed_entities = _all_entities(limit)
    else:
        edges = get_edges(namespace, limit=limit * 2)
        seed_entities = get_entities(namespace, limit=limit)

    wanted = {
        (entity.get("namespace") or namespace or "", entity["name"])
        for entity in seed_entities
    }
    for edge in edges:
        edge_namespace = edge.get("namespace") or namespace or ""
        wanted.add((edge_namespace, edge["source_entity"]))
        wanted.add((edge_namespace, edge["target_entity"]))

    entities_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for entity_namespace, entity_name in sorted(wanted):
        row = conn.execute(
            """SELECT namespace, name, entity_type, created_at
               FROM entities WHERE namespace=? AND name=?""",
            (entity_namespace, entity_name),
        ).fetchone()
        if row is not None:
            entities_by_key[(entity_namespace, entity_name)] = dict(row)

    entities = list(entities_by_key.values())
    document_cap = max(0, seed_limit)
    for entity in entities:
        entity_namespace = entity["namespace"]
        rows = conn.execute(
            """SELECT DISTINCT document_id FROM document_entities
               WHERE namespace=? AND entity_name=?
               ORDER BY document_id LIMIT ?""",
            (entity_namespace, entity["name"], document_cap),
        ).fetchall()
        entity["documents"] = [row["document_id"] for row in rows]
        if entity["documents"]:
            document = conn.execute(
                """SELECT title, content FROM memories
                   WHERE namespace=? AND document_id=?""",
                (entity_namespace, entity["documents"][0]),
            ).fetchone()
        else:
            document = None
        entity["preview_title"] = document["title"] if document else ""
        entity["preview_content"] = document["content"][:200] if document else ""

    return {
        "entities": entities,
        "edges": edges,
        "entity_count": entity_total,
        "edge_count": edge_total,
        "returned_entity_count": len(entities),
        "returned_edge_count": len(edges),
        "truncated": len(entities) < entity_total or len(edges) < edge_total,
        "seed_limit": seed_limit,
    }


def _all_entities(limit: int) -> list[dict[str, Any]]:
    conn = get_conn()
    rows = conn.execute(
        """SELECT namespace, name, entity_type, created_at FROM entities
           ORDER BY namespace, name LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def _all_edges(limit: int) -> list[dict[str, Any]]:
    conn = get_conn()
    rows = conn.execute(
        """SELECT namespace, source_entity, target_entity, relation, weight
           FROM edges
           ORDER BY weight DESC, namespace, source_entity, target_entity, relation
           LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]
