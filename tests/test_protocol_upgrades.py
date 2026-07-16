import json

from engraphis.core.interfaces import (
    Edge,
    GraphLayer,
    Node,
    SchemaSnapshot,
    SearchFilter,
)
from engraphis.core.store import Store
from engraphis.service import MemoryService


def test_explicit_graph_layer_survives_database_reopen(tmp_path):
    db = tmp_path / "memory.db"
    svc = MemoryService.create(str(db), graph_extractor="none")
    first = svc.remember("First fact", workspace="w", scope="workspace")
    second = svc.remember("Second fact", workspace="w", scope="workspace")
    svc.link(
        first["id"], second["id"], workspace="w",
        relation="related", layer="causal", reason="Second fact explains the first.",
    )
    wid, _ = svc._require_scope("w", None)
    e1 = svc.store.upsert_entity(Node(
        id="", name="A", ntype="concept", workspace_id=wid
    ))
    e2 = svc.store.upsert_entity(Node(
        id="", name="B", ntype="concept", workspace_id=wid
    ))
    svc.store.upsert_edge(Edge(
        id="", src=e1, dst=e2, relation="related",
        layer=GraphLayer.TEMPORAL, workspace_id=wid,
    ))
    svc.store.close()

    reopened = Store(str(db))
    persisted_link = reopened.get_links(first["id"])[0]
    assert persisted_link["layer"] == "causal"
    assert persisted_link["reason"] == "Second fact explains the first."
    assert reopened.edges_in_scope(
        SearchFilter(workspace_id=wid)
    )[0].layer == GraphLayer.TEMPORAL


def test_receipts_are_content_free_chained_and_tamper_evident():
    svc = MemoryService.create(":memory:", graph_extractor="none")
    secret = "launch code ORANGE-UNICORN-991"
    svc.remember(
        secret, workspace="w", scope="workspace",
        source="alice@example.test",
    )
    svc.recall("ORANGE-UNICORN", workspace="w", reinforce=False)
    exported = svc.export_receipts(workspace="w")
    encoded = json.dumps(exported)
    assert secret not in encoded
    assert "alice@example.test" not in encoded
    assert exported["verification"]["valid"] is True
    actor = svc.store.conn.execute(
        "SELECT actor FROM operation_receipts ORDER BY rowid LIMIT 1"
    ).fetchone()["actor"]
    assert actor != "alice@example.test" and len(actor) == 16

    svc.store.conn.execute(
        "UPDATE operation_receipts SET payload=payload || 'x' WHERE rowid=1"
    )
    svc.store.conn.commit()
    assert svc.verify_receipts(workspace="w")["valid"] is False


def test_host_retention_signal_is_bounded_and_persisted():
    svc = MemoryService.create(":memory:", graph_extractor="none")
    out = svc.remember(
        "Never expose production secrets.", workspace="w", scope="workspace",
        retention_class="critical", retention_reason="security policy",
    )
    record = svc.store.get_memory(out["id"])
    assert record.importance >= 0.9
    assert record.stability == 8.0
    signal = record.metadata["retention_supervision"]
    assert signal["label"] == "critical" and signal["source"] == "host"


def test_code_graph_links_memories_and_supports_unified_paths(tmp_path):
    (tmp_path / "deploy.py").write_text(
        "def deploy_release():\n    return True\n", encoding="utf-8"
    )
    svc = MemoryService.create(":memory:", graph_extractor="none")
    memory = svc.remember(
        "The deploy_release function must run after the approval gate.",
        workspace="w", repo="app",
    )
    report = svc.index_repo(
        workspace="w", repo="app", root_path=str(tmp_path),
    )
    assert report["code_memory_links"] >= 1

    search = svc.search_code(
        "deploy_release", workspace="w", repo="app",
    )
    assert search["symbols"][0]["linked_memories"][0]["id"] == memory["id"]
    path = svc.code_path(
        memory["id"], "deploy_release", workspace="w", repo="app",
    )
    assert path["found"] is True and path["hops"] == 1

    graph = svc.graph(workspace="w", repo="app", include_code=True)
    types = {node["etype"] for node in graph["nodes"]}
    assert "code_function" in types and "memory_semantic" in types
    assert any(edge["layer"] == "semantic" for edge in graph["edges"])


def test_intent_recall_locate_code_returns_memory_and_symbol_results(tmp_path):
    (tmp_path / "auth.py").write_text("def rotate_key():\n    pass\n", encoding="utf-8")
    svc = MemoryService.create(":memory:", graph_extractor="none")
    svc.index_repo(workspace="w", repo="app", root_path=str(tmp_path))
    svc.remember("rotate_key is used for key rotation.", workspace="w", repo="app")
    out = svc.intent_recall(
        "rotate_key", intent="locate code", workspace="w", repo="app",
    )
    assert out["operation"] == "recall"
    assert out["code"]["symbols"][0]["name"] == "rotate_key"
    assert out["memories"]


def test_postgres_import_never_persists_dsn(monkeypatch):
    dsn = "postgresql://alice:super-secret@example.test/db"
    snapshot = SchemaSnapshot(
        title="PostgreSQL schema: db",
        text="# PostgreSQL schema: db\n\n## public.users\n- `id`: bigint not null",
        entities=[
            {"id": "database:db", "name": "db", "kind": "database"},
            {"id": "table:public.users", "name": "public.users", "kind": "table"},
        ],
        relations=[
            {"source": "database:db", "target": "table:public.users",
             "relation": "contains"},
        ],
        metadata={"database": "db", "tables": 1, "source_digest": "abc"},
    )

    class FakeIntrospector:
        def inspect(self, value, *, schemas=None):
            assert value == dsn
            return snapshot

    monkeypatch.setattr(
        "engraphis.backends.postgres_schema.get_postgres_introspector",
        lambda: FakeIntrospector(),
    )
    svc = MemoryService.create(":memory:", graph_extractor="none")
    out = svc.import_postgres_schema(dsn, workspace="w", repo="app")
    assert out["entities"] == 2 and out["relations"] == 1
    dump = "\n".join(
        str(value)
        for row in svc.store.conn.iterdump()
        for value in [row]
    )
    assert dsn not in dump and "super-secret" not in dump
