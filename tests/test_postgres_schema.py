import json
import sys
import types

from engraphis.backends import postgres_schema
from engraphis.core.interfaces import SchemaSnapshot, SearchFilter
from engraphis.service import MemoryService


class _Cursor:
    def __init__(self):
        self.calls = []
        self.result = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, params=()):
        normalized = " ".join(query.split())
        self.calls.append((normalized, tuple(params)))
        if "current_database()" in normalized:
            self.result = [("appdb",)]
        elif "information_schema.tables" in normalized:
            self.result = [
                ("public", "users", "BASE TABLE"),
                ("public", "orders", "BASE TABLE"),
                ("auth", "accounts", "BASE TABLE"),
            ]
        elif "information_schema.columns" in normalized:
            self.result = [
                ("public", "users", "id", 1, "integer", "NO", None),
                ("public", "users", "tenant_id", 2, "integer", "NO", None),
                ("public", "orders", "account_id", 1, "integer", "NO", None),
                ("auth", "accounts", "id", 1, "integer", "NO", None),
            ]
        else:
            self.result = [
                ("PRIMARY KEY", "public", "users", "id",
                 "public", "users", "id", "shared_name"),
                ("PRIMARY KEY", "public", "orders", "account_id",
                 "public", "orders", "account_id", "shared_name"),
                ("FOREIGN KEY", "public", "orders", "account_id",
                 "auth", "accounts", "id", "orders_account_fk"),
            ]

    def fetchone(self):
        return self.result[0]

    def fetchall(self):
        return list(self.result)


class _Connection:
    def __init__(self):
        self.cursor_obj = _Cursor()
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self):
        return self.cursor_obj

    def close(self):
        self.closed = True


def test_postgres_connect_and_statement_timeouts_are_bounded(monkeypatch):
    captured = {}
    connection = _Connection()

    def connect(dsn, **kwargs):
        captured["dsn"] = dsn
        captured.update(kwargs)
        return connection

    monkeypatch.setitem(sys.modules, "psycopg", types.SimpleNamespace(connect=connect))
    monkeypatch.setenv("ENGRAPHIS_POSTGRES_CONNECT_TIMEOUT", "9999")
    monkeypatch.setenv("ENGRAPHIS_POSTGRES_STATEMENT_TIMEOUT_MS", "45000")

    snapshot = postgres_schema.PostgresSchemaIntrospector().inspect(
        "postgresql://local/appdb"
    )

    assert snapshot.metadata["database"] == "appdb"
    assert captured["connect_timeout"] == postgres_schema._MAX_CONNECT_TIMEOUT_SECONDS
    timeout_call = connection.cursor_obj.calls[0]
    assert "set_config('statement_timeout'" in timeout_call[0]
    assert timeout_call[1] == ("45000",)


def test_postgres_introspection_is_filtered_bounded_and_cross_schema_safe(monkeypatch):
    connection = _Connection()
    monkeypatch.setattr(postgres_schema, "_connect", lambda dsn: connection)
    dsn = "postgresql://user:secret@db.internal/appdb"
    snapshot = postgres_schema.PostgresSchemaIntrospector().inspect(
        dsn, schemas=["public", "auth"]
    )

    assert connection.closed is True
    assert dsn not in snapshot.text
    assert dsn not in json.dumps(snapshot.metadata)
    assert snapshot.metadata["source_digest"]
    ids = {entity["id"] for entity in snapshot.entities}
    assert "constraint:public.users.shared_name" in ids
    assert "constraint:public.orders.shared_name" in ids
    assert {
        ("table:public.orders", "table:auth.accounts", "references")
    } <= {
        (relation["source"], relation["target"], relation["relation"])
        for relation in snapshot.relations
    }

    constraint_query, params = connection.cursor_obj.calls[-1]
    assert "tc.constraint_schema=ccu.constraint_schema" in constraint_query
    assert "tc.table_schema=ccu.table_schema" not in constraint_query
    assert params[:2] == ("auth", "public")


def test_service_never_persists_postgres_dsn(monkeypatch):
    dsn = "postgresql://user:secret@db.internal/appdb"
    snapshot = SchemaSnapshot(
        title="PostgreSQL schema: appdb",
        text="# PostgreSQL schema: appdb\n\n## public.users\n\n- `id`: integer not null",
        entities=[
            {"id": "database:appdb", "name": "appdb", "kind": "database"},
            {"id": "table:public.users", "name": "public.users", "kind": "table"},
        ],
        relations=[{
            "source": "database:appdb",
            "target": "table:public.users",
            "relation": "contains",
        }],
        metadata={"database": "appdb", "tables": 1, "source_digest": "abc123"},
    )

    class _Introspector:
        def inspect(self, supplied, *, schemas=None):
            assert supplied == dsn
            return snapshot

    monkeypatch.setattr(
        postgres_schema, "get_postgres_introspector", lambda: _Introspector()
    )
    service = MemoryService.create(":memory:")
    result = service.import_postgres_schema(dsn, workspace="acme")
    wid = service.store.get_or_create_workspace("acme")
    memories = service.store.list_memories(
        SearchFilter(workspace_id=wid), include_invalid=True
    )
    audit = service.store.conn.execute("SELECT * FROM audit").fetchall()
    receipts = service.store.list_receipts(workspace_id=wid)
    serialized = json.dumps({
        "result": result,
        "memories": [memory.content for memory in memories],
        "metadata": [memory.metadata for memory in memories],
        "audit": [dict(row) for row in audit],
        "receipts": receipts,
    }, default=str)
    assert dsn not in serialized
    assert "secret" not in serialized


def test_large_postgres_snapshot_keeps_every_chunk_distinct(monkeypatch):
    snapshot = SchemaSnapshot(
        title="PostgreSQL schema: large",
        text="\n\n".join(
            f"## public.table_{index}\n\n- `id`: integer not null"
            for index in range(5_000)
        ),
        metadata={"database": "large", "tables": 5_000, "source_digest": "digest"},
    )

    class _Introspector:
        def inspect(self, supplied, *, schemas=None):
            return snapshot

    monkeypatch.setattr(
        postgres_schema, "get_postgres_introspector", lambda: _Introspector()
    )
    service = MemoryService.create(":memory:", graph_extractor="none")
    result = service.import_postgres_schema(
        "postgresql://local/large", workspace="acme"
    )

    assert len(result["memory_ids"]) > 1
    assert len(set(result["memory_ids"])) == len(result["memory_ids"])
