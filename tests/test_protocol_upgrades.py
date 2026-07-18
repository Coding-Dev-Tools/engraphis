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


def test_link_infers_layer_for_response_receipt_and_persistence():
    svc = MemoryService.create(":memory:", graph_extractor="none")
    first = svc.remember("First fact", workspace="w", scope="workspace")
    second = svc.remember("Second fact", workspace="w", scope="workspace")
    third = svc.remember("Third fact", workspace="w", scope="workspace")

    inferred = svc.link(
        first["id"], second["id"], workspace="w", relation="causes",
    )
    assert inferred["layer"] == "causal"
    assert inferred["receipt"]["metadata"]["layer"] == "causal"

    explicit = svc.link(
        first["id"], third["id"], workspace="w",
        relation="causes", layer="temporal",
    )
    assert explicit["layer"] == "temporal"
    assert explicit["receipt"]["metadata"]["layer"] == "temporal"

    links = svc.store.get_links(first["id"])
    assert {(link["b"], link["layer"]) for link in links
            if link["relation"] == "causes"} == {
        (second["id"], "causal"),
        (third["id"], "temporal"),
    }


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


def test_unified_graph_filters_each_code_edge_by_persisted_layer():
    svc = MemoryService.create(":memory:", graph_extractor="none")
    svc.remember("Repo memory", workspace="w", repo="app")
    _, rid = svc._require_scope("w", "app")
    for name in ("source", "causal_target", "temporal_target", "entity_target"):
        svc.store.upsert_symbol(
            repo_id=rid, kind="function", name=name, fqname=name,
            file=f"{name}.py", span="1:1",
        )
    svc.store.add_code_edge(
        repo_id=rid, src="source", dst="causal_target",
        relation="causes", layer=GraphLayer.CAUSAL,
    )
    svc.store.add_code_edge(
        repo_id=rid, src="source", dst="temporal_target",
        relation="follows", layer=GraphLayer.TEMPORAL,
    )
    svc.store.add_code_edge(
        repo_id=rid, src="source", dst="entity_target",
        relation="calls", layer=GraphLayer.ENTITY,
    )

    graph = svc.graph(
        workspace="w", repo="app", include_code=True, layers=["causal"],
    )

    assert {edge["layer"] for edge in graph["edges"]} == {"causal"}
    assert {edge["label"] for edge in graph["edges"]} == {"causes"}
    assert {"app:source", "app:causal_target"} <= {
        node["label"] for node in graph["nodes"]
    }

    assert svc.graph(
        workspace="w", repo="app", include_code=True, layers=[]
    )["edges"] == []
    assert svc.graph(
        workspace="w", repo="app", include_code=True
    )["edges"]


def test_workspace_graph_filters_layers_before_edge_cap():
    svc = MemoryService.create(":memory:", graph_extractor="none")
    wid = svc.store.get_or_create_workspace("w")
    source = svc.store.upsert_entity(Node(
        id="", name="source", ntype="concept", workspace_id=wid
    ))
    target = svc.store.upsert_entity(Node(
        id="", name="target", ntype="concept", workspace_id=wid
    ))
    # graph(limit=2) has an edge cap of 2,000. Put more than that many
    # nonmatching rows ahead of the requested causal edge.
    for index in range(2001):
        svc.store.upsert_edge(Edge(
            id=f"a-nonmatching-{index:04d}",
            src=source,
            dst=target,
            relation="related",
            layer=GraphLayer.SEMANTIC,
            workspace_id=wid,
        ))
    svc.store.upsert_edge(Edge(
        id="z-matching",
        src=source,
        dst=target,
        relation="causes",
        layer=GraphLayer.CAUSAL,
        workspace_id=wid,
    ))

    graph = svc.graph(
        workspace="w", limit=2, layers=["causal"], backfill=False
    )

    assert [(edge["label"], edge["layer"]) for edge in graph["edges"]] == [
        ("causes", "causal")
    ]


def test_code_graph_filters_layers_before_edge_cap():
    svc = MemoryService.create(":memory:", graph_extractor="none")
    svc.remember("Repo memory", workspace="w", repo="app")
    _, rid = svc._require_scope("w", "app")
    for name in ("source", "target"):
        svc.store.upsert_symbol(
            repo_id=rid,
            kind="function",
            name=name,
            fqname=name,
            file=f"{name}.py",
            span="1:1",
        )
    # graph(limit=2) has an edge cap of 2,000. File ordering puts more than
    # that many nonmatching rows ahead of the requested semantic edge.
    for index in range(2001):
        svc.store.add_code_edge(
            repo_id=rid,
            src="source",
            dst="target",
            relation="calls",
            layer=GraphLayer.ENTITY,
            file="a.py",
            line=index,
            commit=False,
        )
    svc.store.add_code_edge(
        repo_id=rid,
        src="source",
        dst="target",
        relation="explains",
        layer=GraphLayer.SEMANTIC,
        file="z.py",
        line=1,
    )

    graph = svc.graph(
        workspace="w",
        repo="app",
        include_code=True,
        limit=2,
        layers=["semantic"],
    )

    assert [(edge["label"], edge["layer"]) for edge in graph["edges"]] == [
        ("explains", "semantic")
    ]


def test_repo_code_reads_exclude_session_scoped_linked_memories():
    svc = MemoryService.create(":memory:", graph_extractor="none")
    repo_memory = svc.remember(
        "Repository guidance for deploy_release.",
        workspace="w", repo="app", scope="repo",
    )
    session = svc.start_session("w", repo="app")
    session_memory = svc.remember(
        "Temporary session note for deploy_release.",
        workspace="w", repo="app", session_id=session["session_id"],
        scope="session",
    )
    _, rid = svc._require_scope("w", "app")
    symbol_id = svc.store.upsert_symbol(
        repo_id=rid, kind="function", name="deploy_release",
        fqname="deploy_release", file="deploy.py", span="1:1",
    )
    svc.store.link_memory_symbol(
        repo_id=rid, symbol_id=symbol_id, memory_id=repo_memory["id"],
    )
    svc.store.link_memory_symbol(
        repo_id=rid, symbol_id=symbol_id, memory_id=session_memory["id"],
    )

    search = svc.search_code("deploy_release", workspace="w", repo="app")
    assert {
        memory["id"] for memory in search["symbols"][0]["linked_memories"]
    } == {repo_memory["id"]}

    graph = svc.graph(
        workspace="w", repo="app", include_code=True, layers=["semantic"],
    )
    node_ids = {node["id"] for node in graph["nodes"]}
    assert repo_memory["id"] in node_ids
    assert session_memory["id"] not in node_ids


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
