"""Tests for MemoryService.graph() — the Graph tab data source shared by the
v1-look dashboard and the Inspector UI (engraphis/graphdata.py).

Entity/edge extraction has its own coverage (test_graph_extractor.py); these
tests write directly to the entities/edges tables so they can pin exact
nodes/edges/stats without depending on the extractor's heuristics, and — the
main point of this file — lock in that graph() enforces the same
workspace-binding isolation boundary as every other read (service.py's
_clean_ws), which the dashboard-only implementation it replaced did not.
"""
from engraphis.backends.extractor import StructuredLLMExtractor
from engraphis.backends.graph_extractor import get_graph_extractor
from engraphis.core.interfaces import Edge, MemoryRecord, MemoryType, Scope, SearchFilter
from engraphis.service import MemoryService, ValidationError


class _StructuredGraphLLM:
    def extract_json(self, prompt, schema):
        return {"facts": [{
            "content": "Engraphis stores memories in SQLite.",
            "title": "Storage backend",
            "entities": ["Engraphis", "SQLite"],
            "relations": [{"source": "engraphis", "relation": "stores_in", "target": "SQLite"}],
        }]}


def _seed_entities(svc, workspace, rows, edges):
    """``rows``: [(name, etype), ...]; ``edges``: [(src_name, dst_name, relation), ...]
    — authored by name for readability, but written to the DB the way the real
    extractor does (backends.graph_extractor.feed): edges.src/dst are entity **ids**
    (``ent0``, ``ent1``, ...), never the display name."""
    wid = svc.store.get_or_create_workspace(workspace)
    conn = svc.store.conn
    id_of = {}
    for i, (name, etype) in enumerate(rows):
        eid = f"ent{i}"
        id_of[name] = eid
        conn.execute(
            "INSERT INTO entities(id, workspace_id, repo_id, name, etype, created_at) "
            "VALUES (?,?,?,?,?,0)", (eid, wid, None, name, etype))
    for i, (src, dst, rel) in enumerate(edges):
        conn.execute(
            "INSERT INTO edges(id, workspace_id, repo_id, src, dst, relation) "
            "VALUES (?,?,?,?,?,?)", (f"edge{i}", wid, None, id_of[src], id_of[dst], rel))
    conn.commit()
    return wid, id_of


def test_graph_returns_seeded_nodes_and_edges():
    svc = MemoryService.create(":memory:")
    _wid, id_of = _seed_entities(
        svc, "acme",
        [("Alice", "person_or_concept"), ("Acme Corp", "organization")],
        [("Alice", "Acme Corp", "works_at")])
    g = svc.graph(workspace="acme")
    assert {n["id"] for n in g["nodes"]} == {id_of["Alice"], id_of["Acme Corp"]}
    assert {n["label"] for n in g["nodes"]} == {"Alice", "Acme Corp"}
    assert g["edges"] == [{"from": id_of["Alice"], "to": id_of["Acme Corp"], "label": "works_at"}]
    assert g["stats"] == {"entities": 2, "edges": 1, "connected": 2, "isolated": 0}


def test_graph_on_nonexistent_workspace_is_empty_not_an_error():
    svc = MemoryService.create(":memory:")
    g = svc.graph(workspace="never-created")
    assert g["nodes"] == [] and g["edges"] == []
    assert g["stats"]["entities"] == 0


def test_graph_rejects_unpermitted_workspace():
    """The isolation boundary this method must not skip: a bound instance refuses
    to read another tenant's graph even if the caller knows/guesses its name —
    the exact gap the old dashboard-only implementation (a raw sqlite connection
    straight to the DB file, no MemoryService involved) left open."""
    seed = MemoryService.create(":memory:")
    _seed_entities(seed, "beta", [("Secret Corp", "organization")], [])
    attacker = MemoryService.create(":memory:")
    attacker.engine = seed.engine
    attacker.store = seed.store  # share the underlying store, differ only in binding
    attacker.allowed_workspaces = frozenset(["alpha"])
    import pytest
    with pytest.raises(ValidationError):
        attacker.graph(workspace="beta")


def test_graph_allows_its_own_bound_workspace():
    svc = MemoryService.create(":memory:", allowed_workspaces=["alpha"])
    _seed_entities(svc, "alpha", [("Widget", "person_or_concept")], [])
    g = svc.graph(workspace="alpha")
    assert {n["label"] for n in g["nodes"]} == {"Widget"}


def test_create_defaults_graph_extractor_on():
    """The config default is "regex", so a plain create() wires an extractor —
    every front end populates the graph without opting in (the wiring gap that
    left settings.graph_extractor orphaned)."""
    svc = MemoryService.create(":memory:")
    assert svc.engine.graph_extractor is not None


def test_remember_populates_graph_when_extractor_wired():
    """Ingest through the wired extractor writes entities, so the Graph tab has
    nodes for freshly remembered content (new users, day one)."""
    svc = MemoryService.create(":memory:", graph_extractor="regex")
    svc.remember("Alice Johnson works at Acme Corp.", workspace="acme",
                 scope="workspace")
    nodes = svc.graph(workspace="acme")["nodes"]
    labels = {n["label"] for n in nodes}
    assert "Alice Johnson" in labels and "Acme Corp" in labels
    # node identity is the entity id (ent_<ulid>), not the extracted name —
    # regression guard for the 2026-07-11 id/name mixup bug
    assert all(n["id"] != n["label"] for n in nodes)


def test_structured_extractor_metadata_populates_graph_without_regex_extractor():
    """llm_structured emits validated entity/relation hints; those should feed the
    graph directly even when the regex text graph extractor is disabled."""
    svc = MemoryService.create(":memory:", graph_extractor="none")
    svc.engine.extractor = StructuredLLMExtractor(_StructuredGraphLLM())
    svc.ingest("raw transcript blob", workspace="acme", scope="workspace")

    g = svc.graph(workspace="acme")
    id_by_label = {n["label"]: n["id"] for n in g["nodes"]}
    assert {"Engraphis", "SQLite"} <= set(id_by_label)
    assert {"from": id_by_label["Engraphis"], "to": id_by_label["SQLite"],
            "label": "stores_in"} in g["edges"]


def test_graph_hides_edges_from_forgotten_memory():
    svc = MemoryService.create(":memory:", graph_extractor="regex")
    out = svc.remember("Alice Johnson works at Acme Corp.", workspace="acme",
                       scope="workspace")
    assert svc.graph(workspace="acme")["edges"]

    svc.forget(out["id"], workspace="acme")
    assert svc.graph(workspace="acme")["edges"] == []


def test_graph_lazy_backfills_structured_metadata_without_regex_extractor():
    svc = MemoryService.create(":memory:", graph_extractor="none")
    wid = svc.store.get_or_create_workspace("acme")
    svc.store.add_memory(MemoryRecord(
        id="", content="Engraphis stores memories in SQLite.",
        workspace_id=wid, scope=Scope.WORKSPACE, mtype=MemoryType.SEMANTIC,
        metadata={"entities": ["Engraphis", "SQLite"],
                  "relations": [{"source": "engraphis", "relation": "stores_in",
                                 "target": "SQLite"}]},
    ))
    g = svc.graph(workspace="acme")
    id_by_label = {n["label"]: n["id"] for n in g["nodes"]}
    assert {"Engraphis", "SQLite"} <= set(id_by_label)
    assert {"from": id_by_label["Engraphis"], "to": id_by_label["SQLite"],
            "label": "stores_in"} in g["edges"]


def test_graph_lazy_backfills_preexisting_memories():
    """Memories written while extraction was OFF have no entities. When extraction
    is later enabled (an update), the first Graph-tab open backfills that
    workspace's graph from its existing memories — no manual migration."""
    svc = MemoryService.create(":memory:", graph_extractor="none")
    svc.remember("Alice Johnson works at Acme Corp.", workspace="acme",
                 scope="workspace")
    assert svc.graph(workspace="acme")["nodes"] == []      # extractor off -> no backfill

    svc.engine.graph_extractor = get_graph_extractor("regex")   # simulate the update
    labels = {n["label"] for n in svc.graph(workspace="acme")["nodes"]}
    assert "Alice Johnson" in labels and "Acme Corp" in labels


def test_graph_lazy_backfill_is_idempotent():
    """Re-opening the Graph tab must not duplicate entities."""
    svc = MemoryService.create(":memory:", graph_extractor="regex")
    svc.remember("Alice Johnson works at Acme Corp.", workspace="acme",
                 scope="workspace")
    first = svc.graph(workspace="acme")["stats"]["entities"]
    second = svc.graph(workspace="acme")["stats"]["entities"]
    assert first == second and first >= 2


def test_graph_hides_edges_before_their_validity_window():
    svc = MemoryService.create(":memory:")
    _seed_entities(
        svc, "acme",
        [("Alice", "person"), ("Acme Corp", "organization")],
        [("Alice", "Acme Corp", "works_at")])
    svc.store.conn.execute(
        "UPDATE edges SET valid_from=? WHERE id='edge0'", (10**12,))
    svc.store.conn.commit()

    assert svc.graph(workspace="acme")["edges"] == []


def test_forgetting_one_support_keeps_a_multi_source_edge_live():
    svc = MemoryService.create(":memory:")
    wid, ids = _seed_entities(
        svc, "acme",
        [("Alice", "person"), ("Acme Corp", "organization")], [])
    first = svc.store.add_memory(MemoryRecord(
        id="", content="Alice works at Acme Corp.", workspace_id=wid,
        scope=Scope.WORKSPACE))
    second = svc.store.add_memory(MemoryRecord(
        id="", content="Acme Corp employs Alice.", workspace_id=wid,
        scope=Scope.WORKSPACE))
    edge_id = svc.store.upsert_edge(Edge(
        id="", src=ids["Alice"], dst=ids["Acme Corp"], relation="works_at",
        workspace_id=wid))
    svc.store.add_edge_support(edge_id, {"memory_id": first})
    svc.store.add_edge_support(edge_id, {"memory_id": second})

    svc.store.invalidate_edges_for_memory(first)

    edges = svc.store.edges_in_scope(SearchFilter(workspace_id=wid))
    assert [edge.id for edge in edges] == [edge_id]
    assert edges[0].provenance["memory_ids"] == [second]

    svc.store.invalidate_edges_for_memory(second)
    assert svc.store.edges_in_scope(SearchFilter(workspace_id=wid)) == []
