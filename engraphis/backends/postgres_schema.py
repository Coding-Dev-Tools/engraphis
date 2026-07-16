"""Optional PostgreSQL catalog introspection.

The DSN is used only to open the caller-requested connection. It is never persisted,
returned, logged, or embedded in memory; provenance contains a one-way digest instead.
"""
from __future__ import annotations

import hashlib
import os
from typing import Any, Optional

from engraphis.core.interfaces import SchemaSnapshot

_SYSTEM_SCHEMAS = {"pg_catalog", "information_schema"}
_MAX_ENTITIES = 20_000
_MAX_RELATIONS = 50_000
_DEFAULT_CONNECT_TIMEOUT_SECONDS = 10
_MAX_CONNECT_TIMEOUT_SECONDS = 120
_DEFAULT_STATEMENT_TIMEOUT_MS = 30_000
_MAX_STATEMENT_TIMEOUT_MS = 300_000


class PostgresIntrospectionError(ValueError):
    """Safe, actionable PostgreSQL inspection failure."""


def _bounded_env_int(name: str, default: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(1, min(maximum, value))


def _connect(dsn: str):
    timeout = _bounded_env_int(
        "ENGRAPHIS_POSTGRES_CONNECT_TIMEOUT",
        _DEFAULT_CONNECT_TIMEOUT_SECONDS,
        _MAX_CONNECT_TIMEOUT_SECONDS,
    )
    try:
        import psycopg
        return psycopg.connect(dsn, connect_timeout=timeout)
    except ImportError:
        try:
            import psycopg2
            return psycopg2.connect(dsn, connect_timeout=timeout)
        except ImportError as exc:
            raise PostgresIntrospectionError(
                "PostgreSQL introspection needs psycopg: "
                "pip install \"engraphis[postgres]\""
            ) from exc


def _rows(cursor, query: str, params: tuple = ()) -> list[tuple]:
    cursor.execute(query, params)
    return list(cursor.fetchall())


class PostgresSchemaIntrospector:
    def inspect(self, dsn: str, *, schemas: Optional[list[str]] = None) -> SchemaSnapshot:
        allow = {str(name).strip() for name in (schemas or []) if str(name).strip()}
        selected = sorted(allow)
        placeholders = ",".join("%s" for _ in selected)
        table_filter = (
            f" AND table_schema IN ({placeholders})" if selected else ""
        )
        constraint_filter = (
            f" AND tc.table_schema IN ({placeholders})" if selected else ""
        )
        conn = None
        try:
            conn = _connect(dsn)
            with conn:
                with conn.cursor() as cursor:
                    statement_timeout = _bounded_env_int(
                        "ENGRAPHIS_POSTGRES_STATEMENT_TIMEOUT_MS",
                        _DEFAULT_STATEMENT_TIMEOUT_MS,
                        _MAX_STATEMENT_TIMEOUT_MS,
                    )
                    cursor.execute(
                        "SELECT set_config('statement_timeout', %s, true)",
                        (str(statement_timeout),),
                    )
                    cursor.execute("SELECT current_database()")
                    database = str(cursor.fetchone()[0])
                    tables = _rows(cursor, f"""
                        SELECT table_schema, table_name, table_type
                        FROM information_schema.tables
                        WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
                        {table_filter}
                        ORDER BY table_schema, table_name
                        LIMIT %s
                    """, (*selected, _MAX_ENTITIES + 1))
                    columns = _rows(cursor, f"""
                        SELECT table_schema, table_name, column_name, ordinal_position,
                               data_type, is_nullable, column_default
                        FROM information_schema.columns
                        WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
                        {table_filter}
                        ORDER BY table_schema, table_name, ordinal_position
                        LIMIT %s
                    """, (*selected, _MAX_ENTITIES + 1))
                    constraints = _rows(cursor, f"""
                        SELECT tc.constraint_type, tc.table_schema, tc.table_name,
                               kcu.column_name, ccu.table_schema, ccu.table_name,
                               ccu.column_name, tc.constraint_name
                        FROM information_schema.table_constraints tc
                        LEFT JOIN information_schema.key_column_usage kcu
                          ON tc.constraint_catalog=kcu.constraint_catalog
                         AND tc.constraint_schema=kcu.constraint_schema
                         AND tc.constraint_name=kcu.constraint_name
                         AND tc.table_name=kcu.table_name
                        LEFT JOIN information_schema.constraint_column_usage ccu
                          ON tc.constraint_catalog=ccu.constraint_catalog
                         AND tc.constraint_schema=ccu.constraint_schema
                         AND tc.constraint_name=ccu.constraint_name
                        WHERE tc.table_schema NOT IN ('pg_catalog', 'information_schema')
                        {constraint_filter}
                        ORDER BY tc.table_schema, tc.table_name, tc.constraint_name
                        LIMIT %s
                    """, (*selected, _MAX_RELATIONS + 1))
        except PostgresIntrospectionError:
            raise
        except Exception as exc:
            raise PostgresIntrospectionError(
                f"PostgreSQL schema inspection failed ({type(exc).__name__}); "
                "verify the DSN, network access, and database permissions"
            ) from exc
        finally:
            try:
                conn.close()
            except Exception:
                pass

        def permitted(schema: Any) -> bool:
            value = str(schema or "")
            return value not in _SYSTEM_SCHEMAS and (not allow or value in allow)

        catalog_truncated = (
            len(tables) > _MAX_ENTITIES
            or len(columns) > _MAX_ENTITIES
            or len(constraints) > _MAX_RELATIONS
        )
        tables = [row for row in tables[:_MAX_ENTITIES] if permitted(row[0])]
        columns = [row for row in columns[:_MAX_ENTITIES] if permitted(row[0])]
        constraints = [
            row for row in constraints[:_MAX_RELATIONS] if permitted(row[1])
        ]

        entities: list[dict] = [{
            "id": f"database:{database}", "name": database, "kind": "database",
        }]
        relations: list[dict] = []
        schema_names = sorted({str(row[0]) for row in tables} | {str(row[0]) for row in columns})
        for schema in schema_names:
            sid = f"schema:{schema}"
            entities.append({"id": sid, "name": schema, "kind": "schema"})
            relations.append({
                "source": f"database:{database}", "target": sid, "relation": "contains",
            })

        table_ids = set()
        lines = [f"# PostgreSQL schema: {database}", ""]
        columns_by_table: dict[tuple[str, str], list[tuple]] = {}
        for row in columns:
            columns_by_table.setdefault((str(row[0]), str(row[1])), []).append(row)
        for schema, table, table_type in tables:
            schema, table = str(schema), str(table)
            tid = f"table:{schema}.{table}"
            table_ids.add(tid)
            entities.append({
                "id": tid, "name": f"{schema}.{table}", "kind": "view"
                if "VIEW" in str(table_type).upper() else "table",
            })
            relations.append({
                "source": f"schema:{schema}", "target": tid, "relation": "contains",
            })
            lines.extend([f"## {schema}.{table}", ""])
            for col in columns_by_table.get((schema, table), []):
                _, _, column, position, data_type, nullable, default = col
                cid = f"column:{schema}.{table}.{column}"
                entities.append({
                    "id": cid, "name": f"{schema}.{table}.{column}", "kind": "column",
                    "data_type": str(data_type), "nullable": str(nullable) == "YES",
                    "position": int(position),
                })
                relations.append({"source": tid, "target": cid, "relation": "contains"})
                suffix = " nullable" if str(nullable) == "YES" else " not null"
                default_text = f" default {default}" if default is not None else ""
                lines.append(f"- `{column}`: {data_type}{suffix}{default_text}")
            lines.append("")

        constraint_entities: set[str] = set()
        relation_keys = {
            (relation["source"], relation["target"], relation["relation"])
            for relation in relations
        }
        for constraint in constraints:
            ctype, schema, table, column, target_schema, target_table, target_column, name = (
                constraint
            )
            schema, table = str(schema), str(table)
            source_table = f"table:{schema}.{table}"
            if source_table not in table_ids:
                continue
            constraint_id = f"constraint:{schema}.{table}.{name}"
            if constraint_id not in constraint_entities:
                entities.append({
                    "id": constraint_id, "name": str(name), "kind": "constraint",
                    "constraint_type": str(ctype),
                })
                constraint_entities.add(constraint_id)
            constraint_key = (source_table, constraint_id, "has_constraint")
            if constraint_key not in relation_keys:
                relations.append({
                    "source": source_table,
                    "target": constraint_id,
                    "relation": "has_constraint",
                })
                relation_keys.add(constraint_key)
            if str(ctype).upper() == "FOREIGN KEY" and target_schema and target_table:
                target = f"table:{target_schema}.{target_table}"
                reference_key = (source_table, target, "references")
                if reference_key not in relation_keys:
                    relations.append({
                        "source": source_table,
                        "target": target,
                        "relation": "references",
                        "column": str(column or ""),
                        "target_column": str(target_column or ""),
                    })
                    relation_keys.add(reference_key)

        truncated = catalog_truncated
        if len(entities) > _MAX_ENTITIES:
            entities = entities[:_MAX_ENTITIES]
            truncated = True
        allowed_ids = {entity["id"] for entity in entities}
        relations = [
            relation for relation in relations
            if relation["source"] in allowed_ids and relation["target"] in allowed_ids
        ]
        if len(relations) > _MAX_RELATIONS:
            relations = relations[:_MAX_RELATIONS]
            truncated = True
        digest = hashlib.sha256(dsn.encode("utf-8")).hexdigest()[:24]
        return SchemaSnapshot(
            title=f"PostgreSQL schema: {database}",
            text="\n".join(lines).strip(),
            entities=entities,
            relations=relations,
            metadata={
                "database": database,
                "schemas": schema_names,
                "tables": len(tables),
                "columns": len(columns),
                "constraints": len(constraints),
                "source_digest": digest,
                "truncated": truncated,
            },
        )


def get_postgres_introspector():
    return PostgresSchemaIntrospector()
