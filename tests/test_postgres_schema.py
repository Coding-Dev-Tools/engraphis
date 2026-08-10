import json
import sys
import types

import pytest

from engraphis.backends import postgres_schema
from engraphis.core.interfaces import SchemaSnapshot, SearchFilter
import engraphis.service as service_module
from engraphis.service import MAX_CONTENT_CHARS, MemoryService


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


def test_postgres_introspection_rejects_missing_current_database_row(monkeypatch):
    connection = _Connection()
    connection.cursor_obj.fetchone = lambda: None
    monkeypatch.setattr(postgres_schema, '_connect', lambda _dsn: connection)

    with pytest.raises(postgres_schema.PostgresIntrospectionError, match='did not return a database name'):
        postgres_schema.PostgresSchemaIntrospector().inspect('postgresql://db.example/appdb')

    assert connection.closed is True


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
        "postgresql://localhost/appdb"
    )

    assert snapshot.metadata["database"] == "appdb"
    assert captured["connect_timeout"] == postgres_schema._MAX_CONNECT_TIMEOUT_SECONDS
    timeout_call = connection.cursor_obj.calls[0]
    assert "set_config('statement_timeout'" in timeout_call[0]
    assert timeout_call[1] == ("45000",)


def test_postgres_connect_pins_validated_address_without_losing_tls_host(monkeypatch):
    captured = {}
    resolutions = 0

    def resolve(*args, **kwargs):
        nonlocal resolutions
        resolutions += 1
        if resolutions > 1:
            return [
                (
                    postgres_schema.socket.AF_INET,
                    postgres_schema.socket.SOCK_STREAM,
                    postgres_schema.socket.IPPROTO_TCP,
                    "",
                    ("10.0.0.8", 5432),
                )
            ]
        return [
            (
                postgres_schema.socket.AF_INET,
                postgres_schema.socket.SOCK_STREAM,
                postgres_schema.socket.IPPROTO_TCP,
                "",
                ("93.184.216.34", 5432),
            )
        ]

    def connect(dsn, **kwargs):
        captured["dsn"] = dsn
        captured.update(kwargs)
        return _Connection()

    monkeypatch.setattr(postgres_schema.socket, "getaddrinfo", resolve)
    monkeypatch.setitem(sys.modules, "psycopg", types.SimpleNamespace(connect=connect))

    dsn = (
        "postgresql://db.example/appdb"
        "?host=10.0.0.8&hostaddr=10.0.0.8"
    )
    postgres_schema._connect(dsn)

    assert resolutions == 1
    assert captured["dsn"] == dsn
    assert captured["host"] == "db.example"
    assert captured["hostaddr"] == "93.184.216.34"


@pytest.mark.parametrize(
    "address",
    [
        "10.0.0.8",
        "100.64.0.1",
        "127.0.0.2",
        "169.254.1.1",
        "192.0.2.1",
        "224.0.0.1",
        "::",
        "fe80::1",
        "ff02::1",
    ],
)
def test_postgres_connect_rejects_non_global_addresses(monkeypatch, address):
    family = (
        postgres_schema.socket.AF_INET6
        if ":" in address
        else postgres_schema.socket.AF_INET
    )
    monkeypatch.setattr(
        postgres_schema.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (
                family,
                postgres_schema.socket.SOCK_STREAM,
                postgres_schema.socket.IPPROTO_TCP,
                "",
                (address, 5432),
            )
        ],
    )

    with pytest.raises(
        postgres_schema.PostgresIntrospectionError,
        match="global unicast",
    ):
        postgres_schema._connect("postgresql://db.example/appdb")


def test_postgres_connect_rejects_mixed_global_and_private_dns(monkeypatch):
    monkeypatch.setattr(
        postgres_schema.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (
                postgres_schema.socket.AF_INET,
                postgres_schema.socket.SOCK_STREAM,
                postgres_schema.socket.IPPROTO_TCP,
                "",
                ("93.184.216.34", 5432),
            ),
            (
                postgres_schema.socket.AF_INET,
                postgres_schema.socket.SOCK_STREAM,
                postgres_schema.socket.IPPROTO_TCP,
                "",
                ("10.0.0.8", 5432),
            ),
        ],
    )

    with pytest.raises(
        postgres_schema.PostgresIntrospectionError,
        match="global unicast",
    ):
        postgres_schema._connect("postgresql://db.example/appdb")


def test_postgres_connect_keeps_explicit_dsn_and_loopback_exceptions(monkeypatch):
    calls = []

    def connect(dsn, **kwargs):
        calls.append((dsn, kwargs))
        return _Connection()

    explicit = "postgresql://db.internal/appdb"
    monkeypatch.setenv("ENGRAPHIS_POSTGRES_DSN", explicit)
    monkeypatch.setitem(sys.modules, "psycopg", types.SimpleNamespace(connect=connect))

    postgres_schema._connect(explicit)
    monkeypatch.delenv("ENGRAPHIS_POSTGRES_DSN")
    postgres_schema._connect("postgresql://localhost/appdb")

    assert calls[0][1] == {"connect_timeout": 10}
    assert calls[1][1]["host"] == "localhost"
    assert calls[1][1]["hostaddr"] == "127.0.0.1"


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
    assert postgres_schema._catalog_id(
        "constraint", "public", "users", "shared_name",
    ) in ids
    assert postgres_schema._catalog_id(
        "constraint", "public", "orders", "shared_name",
    ) in ids
    assert {
        (
            postgres_schema._catalog_id("table", "public", "orders"),
            postgres_schema._catalog_id("table", "auth", "accounts"),
            "references",
        )
    } <= {
        (relation["source"], relation["target"], relation["relation"])
        for relation in snapshot.relations
    }

    constraint_query, params = connection.cursor_obj.calls[-1]
    assert "tc.constraint_schema=ccu.constraint_schema" in constraint_query
    assert "tc.table_schema=ccu.table_schema" not in constraint_query
    assert params[:2] == (["auth", "public"], ["auth", "public"])


def test_postgres_source_digest_excludes_credentials_and_connection_options():
    first = postgres_schema._source_digest(
        "postgresql://alice:first-password@db.example:5433/appdb?sslmode=require"
    )
    rotated = postgres_schema._source_digest(
        "postgresql://bob:second-password@db.example:5433/appdb?sslmode=disable"
    )
    other_database = postgres_schema._source_digest(
        "postgresql://alice:first-password@db.example:5433/other"
    )

    assert first == rotated
    assert first != other_database
    assert postgres_schema._source_digest(
        "host=db.example dbname=appdb user=alice password=first-password"
    ) == postgres_schema._source_digest(
        "host=db.example dbname=appdb user=bob password=second-password"
    )
    assert postgres_schema._source_digest(
        "host=db.example dbname=appdb user=alice password=first-password"
    ) != postgres_schema._source_digest(
        "host=other.example dbname=appdb user=alice password=first-password"
    )
    assert postgres_schema._source_digest(
        "host=db.example dbname=appdb user=alice password=first-password"
    ) != postgres_schema._source_digest(
        "host=db.example dbname=other user=alice password=first-password"
    )
    assert postgres_schema._source_digest(
        "host=db.example port=5432 dbname=appdb user=alice password=first-password"
    ) != postgres_schema._source_digest(
        "host=db.example port=5433 dbname=appdb user=alice password=first-password"
    )


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


def test_empty_postgres_chunk_result_returns_without_indexing(monkeypatch):
    snapshot = SchemaSnapshot(
        title="PostgreSQL schema: empty",
        text="x" * (MAX_CONTENT_CHARS + 1),
        metadata={"database": "empty", "source_digest": "digest"},
    )

    class _Introspector:
        def inspect(self, supplied, *, schemas=None):
            return snapshot

    class _EmptyExtractor:
        def extract(self, _text):
            return []

    monkeypatch.setattr(
        postgres_schema, "get_postgres_introspector", lambda: _Introspector()
    )
    monkeypatch.setattr(service_module, "ChunkingExtractor", _EmptyExtractor)
    service = MemoryService.create(":memory:")

    assert service.import_postgres_schema(
        "postgresql://local/empty", workspace="acme"
    ) == {"workspace": "acme", "stored": 0, "entities": 0, "relations": 0}


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



def test_dotted_postgres_identifiers_stay_distinct_through_service_ingestion(monkeypatch):
    connection = _Connection()

    def execute(query, params=()):
        normalized = " ".join(query.split())
        connection.cursor_obj.calls.append((normalized, tuple(params)))
        if "current_database()" in normalized:
            connection.cursor_obj.result = [("appdb",)]
        elif "information_schema.tables" in normalized:
            connection.cursor_obj.result = [
                ("a", "b.c", "BASE TABLE"),
                ("a.b", "c", "BASE TABLE"),
            ]
        elif "information_schema.columns" in normalized:
            connection.cursor_obj.result = [
                ("a", "b.c", "id", 1, "integer", "NO", None),
                ("a.b", "c", "id", 1, "integer", "NO", None),
            ]
        else:
            connection.cursor_obj.result = [
                ("PRIMARY KEY", "a", "b.c", "id",
                 "a", "b.c", "id", "pk.shared"),
                ("PRIMARY KEY", "a.b", "c", "id",
                 "a.b", "c", "id", "pk.shared"),
            ]

    connection.cursor_obj.execute = execute
    monkeypatch.setattr(postgres_schema, "_connect", lambda _dsn: connection)
    snapshot = postgres_schema.PostgresSchemaIntrospector().inspect(
        "postgresql://db.example/appdb",
        schemas=["a", "a.b"],
    )

    assert len({entity["id"] for entity in snapshot.entities}) == len(snapshot.entities)
    graph_entities = {
        kind: [entity for entity in snapshot.entities if entity["kind"] == kind]
        for kind in ("table", "column", "constraint")
    }
    for entities in graph_entities.values():
        assert len(entities) == 2
        assert len({entity["name"] for entity in entities}) == 2

    class _Introspector:
        def inspect(self, _dsn, *, schemas=None):
            return snapshot

    monkeypatch.setattr(
        postgres_schema,
        "get_postgres_introspector",
        lambda: _Introspector(),
    )
    service = MemoryService.create(":memory:", graph_extractor="none")
    result = service.import_postgres_schema(
        "postgresql://db.example/appdb",
        workspace="acme",
        schemas=["a", "a.b"],
    )
    stored_rows = service.store.conn.execute(
        "SELECT id, name, etype FROM entities"
    ).fetchall()
    for kind, entities in graph_entities.items():
        assert {
            row["name"] for row in stored_rows if row["etype"] == kind
        } == {
            entity["name"] for entity in entities
        }
    assert len(stored_rows) == len(snapshot.entities)
    actual_ids = {
        entity["id"]: next(
            row["id"]
            for row in stored_rows
            if row["name"] == entity["name"] and row["etype"] == entity["kind"]
        )
        for entity in snapshot.entities
    }
    stored_edges = {
        (row["src"], row["dst"], row["relation"])
        for row in service.store.conn.execute(
            "SELECT src, dst, relation FROM edges"
        ).fetchall()
    }
    assert {
        (
            actual_ids[relation["source"]],
            actual_ids[relation["target"]],
            relation["relation"],
        )
        for relation in snapshot.relations
    } <= stored_edges
    assert result["relations"] == len(snapshot.relations)


def test_postgres_inspection_runs_before_the_local_write_transaction(monkeypatch):
    snapshot = SchemaSnapshot(
        title="PostgreSQL schema: lock-safe",
        text="# PostgreSQL schema: lock-safe",
        metadata={"database": "lock-safe", "tables": 0, "source_digest": "digest"},
    )
    service = MemoryService.create(":memory:", graph_extractor="none")

    class _Introspector:
        def inspect(self, _dsn, *, schemas=None):
            assert not service.store.conn.transaction_owned_by_current_thread()
            return snapshot

    monkeypatch.setattr(
        postgres_schema,
        "get_postgres_introspector",
        lambda: _Introspector(),
    )
    service.import_postgres_schema(
        "postgresql://db.example/lock-safe",
        workspace="acme",
    )


def _write_state(service):
    tables = (
        "memories",
        "mem_vectors",
        "entities",
        "edges",
        "audit",
        "operation_receipts",
    )
    return {
        table: service.store.conn.execute(
            f"SELECT COUNT(*) AS count FROM {table}"
        ).fetchone()["count"]
        for table in tables
    }


def test_postgres_import_rolls_back_earlier_chunks_when_a_later_write_fails(
    monkeypatch,
):
    snapshot = SchemaSnapshot(
        title="PostgreSQL schema: atomic",
        text=" ".join(f"distinct_{index}" for index in range(20_000)),
        metadata={"database": "atomic", "tables": 2, "source_digest": "digest"},
    )

    class _Introspector:
        def inspect(self, _dsn, *, schemas=None):
            return snapshot

    monkeypatch.setattr(
        postgres_schema,
        "get_postgres_introspector",
        lambda: _Introspector(),
    )
    service = MemoryService.create(":memory:", graph_extractor="none")
    service.store.get_or_create_workspace("acme")
    before = _write_state(service)
    original_remember = service.remember
    calls = 0

    def fail_second_chunk(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected later-chunk failure")
        return original_remember(*args, **kwargs)

    monkeypatch.setattr(service, "remember", fail_second_chunk)
    with pytest.raises(RuntimeError, match="later-chunk failure"):
        service.import_postgres_schema(
            "postgresql://db.example/atomic",
            workspace="acme",
        )

    assert calls == 2
    assert _write_state(service) == before


@pytest.mark.parametrize("failure_site", ["entity", "edge"])
def test_postgres_import_rolls_back_all_local_writes_when_graph_projection_fails(
    monkeypatch,
    failure_site,
):
    snapshot = SchemaSnapshot(
        title="PostgreSQL schema: atomic",
        text="# PostgreSQL schema: atomic",
        entities=[
            {"id": "database:atomic", "name": "atomic", "kind": "database"},
            {"id": "table:atomic", "name": '"public"."items"', "kind": "table"},
            {"id": "column:atomic", "name": '"public"."items"."id"', "kind": "column"},
        ],
        relations=[
            {
                "source": "database:atomic",
                "target": "table:atomic",
                "relation": "contains",
            },
            {
                "source": "table:atomic",
                "target": "column:atomic",
                "relation": "contains",
            },
        ],
        metadata={"database": "atomic", "tables": 1, "source_digest": "digest"},
    )

    class _Introspector:
        def inspect(self, _dsn, *, schemas=None):
            return snapshot

    monkeypatch.setattr(
        postgres_schema,
        "get_postgres_introspector",
        lambda: _Introspector(),
    )
    service = MemoryService.create(":memory:", graph_extractor="none")
    service.store.get_or_create_workspace("acme")
    before = _write_state(service)

    if failure_site == "entity":
        original_upsert = service.store.upsert_entity
        entity_calls = 0

        def reject_second_entity(*args, **kwargs):
            nonlocal entity_calls
            entity_calls += 1
            if entity_calls == 2:
                raise RuntimeError("injected entity failure")
            return original_upsert(*args, **kwargs)

        monkeypatch.setattr(service.store, "upsert_entity", reject_second_entity)
    else:
        original_upsert = service.store.upsert_edge
        edge_calls = 0

        def reject_second_edge(*args, **kwargs):
            nonlocal edge_calls
            edge_calls += 1
            if edge_calls == 2:
                raise RuntimeError("injected edge failure")
            return original_upsert(*args, **kwargs)

        monkeypatch.setattr(service.store, "upsert_edge", reject_second_edge)

    with pytest.raises(RuntimeError, match=f"{failure_site} failure"):
        service.import_postgres_schema(
            "postgresql://db.example/atomic",
            workspace="acme",
        )

    assert _write_state(service) == before
