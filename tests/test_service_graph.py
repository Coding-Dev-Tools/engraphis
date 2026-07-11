"""Tests for MemoryService.graph() — the Graph tab data source shared by the
v1-look dashboard and the Inspector UI (engraphis/graphdata.py).

Entity/edge extraction has its own coverage (test_graph_extractor.py); these
tests write directly to the entities/edges tables so they can pin exact
nodes/edges/stats without depending on the extractor's heuristics, and — the
main point of this file — lock in that graph() enforces the same
workspace-binding isolation boundary as every other read (service.py's
_clean_ws), which the dashboard-only implementation it replaced did not.
"""
from engraphis.backends.graph_extractor import get_graph_extractor
from engraphis.service import MemoryService, ValidationError


def _seed_entities(svc, workspace, rows, edges):
    wid = svc.store.get_or_create_workspace(workspace)
    conn = svc.store.conn
    for i, (name, etype) in enumerate(rows):
        conn.execute(
            "INSERT INTO entities(id, workspace_id, repo_id, name, etype, created_at) "
            "VALUES (?,?,?,?,?,0)", (f"ent{i}", wid, None, name, etype))
    for i, (src, dst, rel) in enumerate(edges):
        conn.execute(
            "INSERT INTO edges(id, workspace_id, repo_id, src, dst, relation) "
            "VALUES (?,?,?,?,?,?)", (f"edge{i}", wid, None, src, dst, rel))
    conn.commit()
    return wid


def test_graph_returns_seeded_nodes_and_edges():
    svc = MemoryService.create(":memory:")
    _seed_entities(svc, "acme",
                    [("Alice", "person_or_concept"), ("Acme Corp", "organization")],
                    [("Alice", "Acme Corp", "works_at")])
    g = svc.graph(workspace="acme")
    assert {n["id"] for n in g["nodes"]} == {"Alice", "Acme Corp"}
    assert g["edges"] == [{"from": "Alice", "to": "Acme Corp", "label": "works_at"}]
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
    assert {n["id"] for n in g["nodes"]} == {"Widget"}


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
    ids = {n["id"] for n in svc.graph(workspace="acme")["nodes"]}
    assert "Alice Johnson" in ids and "Acme Corp" in ids


def test_graph_lazy_backfills_preexisting_memories():
    """Memories written while extraction was OFF have no entities. When extraction
    is later enabled (an update), the first Graph-tab open backfills that
    workspace's graph from its existing memories — no manual migration."""
    svc = MemoryService.create(":memory:", graph_extractor="none")
    svc.remember("Alice Johnson works at Acme Corp.", workspace="acme",
                 scope="workspace")
    assert svc.graph(workspace="acme")["nodes"] == []      # extractor off -> no backfill

    svc.engine.graph_extractor = get_graph_extractor("regex")   # simulate the update
    ids = {n["id"] for n in svc.graph(workspace="acme")["nodes"]}
    assert "Alice Johnson" in ids and "Acme Corp" in ids


def test_graph_lazy_backfill_is_idempotent():
    """Re-opening the Graph tab must not duplicate entities."""
    svc = MemoryService.create(":memory:", graph_extractor="regex")
    svc.remember("Alice Johnson works at Acme Corp.", workspace="acme",
                 scope="workspace")
    first = svc.graph(workspace="acme")["stats"]["entities"]
    second = svc.graph(workspace="acme")["stats"]["entities"]
    assert first == second and first >= 2
