"""Closure observations must agree across every retrieval and graph surface."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading

import numpy as np
import pytest

from engraphis.backends.vector_numpy import NumpyVectorIndex
from engraphis.core import scoring
from engraphis.core.browsing import browse_memories
from engraphis.core.graph_scene import _complete_relations, build_canonical_graph
from engraphis.core.interfaces import Edge, MemoryRecord, Scope, SearchFilter
from engraphis.core.schema import SCHEMA_VERSION
from engraphis.core.store import Store, memory_matches_filter
from engraphis.service import MemoryService


ANCHORS = [(250.0, 450.0, True), (350.0, 450.0, False),
           (250.0, 550.0, False), (150.0, 550.0, True), (350.0, 350.0, True)]


def _seed(store):
    workspace = store.get_or_create_workspace("closure-workspace")
    memory = store.add_memory(MemoryRecord(
        id="mem_observed_closure", content="Aurora service depends on the cache.",
        workspace_id=workspace, scope=Scope.WORKSPACE,
        valid_from=100.0, ingested_at=100.0,
        provenance={"source": "local_cli", "trusted": True, "review_state": "approved"},
        embedding=(None if store.active_embedding_space() else
                   np.array([1.0, 0.0, 0.0], dtype=np.float32)),
    ))
    store.conn.executemany(
        "INSERT INTO entities(id,workspace_id,name,etype,created_at) "
        "VALUES (?,?,?,'concept',100)",
        [("ent_closure_alpha", workspace, "Aurora"),
         ("ent_closure_beta", workspace, "Cache")],
    )
    store.conn.commit()
    edge = store.upsert_edge(Edge(
        id="edg_closure_source", src="ent_closure_alpha", dst="ent_closure_beta",
        relation="depends_on", workspace_id=workspace,
        valid_from=100.0, ingested_at=100.0, provenance={"memory_id": memory},
    ))
    store.link_memory_entity(memory_id=memory, entity_id="ent_closure_alpha",
                             workspace_id=workspace, repo_id=None,
                             valid_from=100.0, ingested_at=100.0)
    return workspace, memory, edge


def _close_twice(store, memory, monkeypatch):
    monkeypatch.setattr("engraphis.core.store.now_ts", lambda: 400.0)
    store.close_validity(memory, at=300.0)
    monkeypatch.setattr("engraphis.core.store.now_ts", lambda: 500.0)
    store.close_validity(memory, at=200.0)


@pytest.fixture
def history(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "observations.db"))
    workspace, memory, edge = _seed(store)
    _close_twice(store, memory, monkeypatch)
    try:
        yield store, workspace, memory, edge
    finally:
        store.close()


@pytest.mark.parametrize("world,known,visible", ANCHORS)
def test_observations_agree_in_sql_records_and_browse(history, world, known, visible):
    store, workspace, memory, _ = history
    flt = SearchFilter(workspace_id=workspace, valid_at=world, known_at=known)
    assert (memory in store.list_memory_ids(flt)) is visible
    record = store.get_memory(memory)
    assert record is not None
    assert memory_matches_filter(record, flt) is visible
    assert {(end, learned) for end, learned in record.closure_observations} >= {
        (300.0, 400.0), (200.0, 500.0),
    }
    page = browse_memories(store, flt)
    assert (memory in {row["id"] for row in page["rows"]}) is visible
    assert page["total_count"] == int(visible)


@pytest.mark.parametrize("world,known,visible", ANCHORS)
@pytest.mark.parametrize("backend", ["numpy", "sqlite-vec"])
def test_vector_backends_use_all_observations(history, world, known, visible, backend):
    store, workspace, memory, _ = history
    if backend == "numpy":
        index = NumpyVectorIndex(store, dim=3)
    else:
        pytest.importorskip("sqlite_vec")
        from engraphis.backends.vector_sqlitevec import SqliteVecVectorIndex
        index = SqliteVecVectorIndex(store, dim=3)
        index.upsert([memory], np.array([[1.0, 0.0, 0.0]], dtype=np.float32))
    flt = SearchFilter(workspace_id=workspace, valid_at=world, known_at=known)
    found = index.search(np.array([1.0, 0.0, 0.0], dtype=np.float32), 5, filter=flt)
    assert (memory in {item[0] for item in found}) is visible


@pytest.mark.parametrize("world,known,visible", ANCHORS)
def test_graph_supports_incidence_edges_and_neighbors_use_source_observations(
    history, world, known, visible,
):
    store, workspace, memory, edge = history
    flt = SearchFilter(workspace_id=workspace, valid_at=world, known_at=known)
    assert bool(store.edge_supports_in_scope([edge], flt=flt)) is visible
    assert bool(store.edges_in_scope(flt)) is visible
    assert bool(store.neighbors(["ent_closure_alpha"], flt=flt)) is visible
    assert bool(store.neighbors(["ent_closure_alpha"], flt=flt, prompt_only=True)) is visible
    assert bool(store.list_memory_entities(flt, memory_ids=[memory])) is visible


def test_graph_explicit_world_override_still_uses_requested_knowledge(history):
    store, workspace, _, edge = history
    flt = SearchFilter(workspace_id=workspace, valid_at=150.0, known_at=550.0)
    assert store.edge_supports_in_scope([edge], flt=flt, at=250.0) == []
    assert store.edges_in_scope(flt, at=250.0) == []
    assert store.neighbors(["ent_closure_alpha"], flt=flt, at=250.0) == []


@pytest.mark.parametrize("world,known,visible", ANCHORS)
def test_complete_graph_scene_keeps_same_source_visibility(tmp_path, monkeypatch, world, known, visible):
    service = MemoryService.create(str(tmp_path / "scene.db"), graph_extractor="none")
    try:
        _, memory, edge = _seed(service.store)
        _close_twice(service.store, memory, monkeypatch)
        rows = service._graph_scene_rows(
            workspace="closure-workspace", valid_at=world, known_at=known,
            include_complete_rows=True,
        )
        assert (edge in {item["id"] for item in rows[3]}) is visible
        assert (memory in {item["memory_id"] for item in rows[4]}) is visible
        assert (memory in {item["id"] for item in rows[5]}) is visible
    finally:
        service.close()


@pytest.mark.parametrize("world,known", [(350.0, 450.0), (250.0, 550.0)])
def test_complete_history_marks_memory_ghost_from_earlier_observation(tmp_path, monkeypatch, world, known):
    service = MemoryService.create(str(tmp_path / "ghost.db"), graph_extractor="none")
    try:
        _, memory, edge = _seed(service.store)
        _close_twice(service.store, memory, monkeypatch)
        rows = service._graph_scene_rows(
            workspace="closure-workspace", valid_at=world, known_at=known,
            include_complete_rows=True, include_history=True,
        )
        memories = {item["id"]: item for item in rows[5]}
        assert memories[memory]["ghost"] is True
        assert next(item for item in rows[3] if item["id"] == edge)["ghost"] is True
        assert rows[4]
        assert all(item["memory_ghost"] for item in rows[4] if item["memory_id"] == memory)
    finally:
        service.close()


def test_filtered_normalized_evidence_never_becomes_a_supportless_relation():
    entities = [{"id": "alpha", "name": "Alpha"}, {"id": "beta", "name": "Beta"}]
    edge = {"id": "edge", "src": "alpha", "dst": "beta", "relation": "related",
            "_has_normalized_support": True, "provenance": {"memory_id": "hidden"}}
    graph = build_canonical_graph(entities, [edge], [], min_support=0)
    assert graph["edges"] == []
    relations, evidence = _complete_relations(
        graph, [edge], [], memory_ids=set(), include_weak_cooccurrence=True,
        layers=None, relations=None, min_support=0, min_confidence=0.0,
    )
    assert relations == evidence == []


def test_scoring_uses_the_earliest_endpoint_already_known(history):
    store, _, memory, _ = history
    record = store.get_memory(memory)
    weights = scoring.Weights(r=0, s=0, l=0, g=0, i=0, c=0, x=1)
    assert scoring.score_memory(record, now=350.0, known_at=350.0, weights=weights) == 0
    assert scoring.score_memory(record, now=350.0, known_at=450.0, weights=weights) == -1
    assert scoring.score_memory(record, now=250.0, known_at=450.0, weights=weights) == pytest.approx(
        -scoring.staleness_penalty(300.0, 250.0),
    )


def test_workspace_copy_preserves_all_observations_and_secure_erasure_removes_them(tmp_path, monkeypatch):
    service = MemoryService.create(str(tmp_path / "copy.db"), graph_extractor="none")
    try:
        _, memory, _ = _seed(service.store)
        _close_twice(service.store, memory, monkeypatch)
        copied = service.copy_workspace("closure-workspace", "closure-copy")
        target = service.store.list_memories(SearchFilter(workspace_id=copied["id"]), include_invalid=True)
        assert len(target) == 1
        assert set(target[0].closure_observations) >= {(300.0, 400.0), (200.0, 500.0)}
        assert not memory_matches_filter(target[0], SearchFilter(valid_at=350.0, known_at=450.0))
        service.engine.secure_erase(memory)
        assert service.store.conn.execute(
            "SELECT COUNT(*) FROM memory_closures WHERE memory_id=?", (memory,),
        ).fetchone()[0] == 0
        assert service.store.get_memory(target[0].id) is not None
    finally:
        service.close()


def test_v18_migration_backfills_existing_scalar_closure_idempotently(tmp_path):
    path = tmp_path / "legacy18.db"
    store = Store(str(path))
    workspace = store.get_or_create_workspace("legacy")
    memory = store.add_memory(MemoryRecord(
        id="mem_legacy_closed", content="A historical deployment fact.",
        workspace_id=workspace, scope=Scope.WORKSPACE, valid_from=100.0,
        valid_to=200.0, valid_to_recorded_at=300.0, ingested_at=100.0,
    ))
    store.close()
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE memory_closures")
        conn.execute("DELETE FROM schema_migrations WHERE version>18")
    for _ in range(2):
        reopened = Store(str(path))
        try:
            assert reopened.schema_version == SCHEMA_VERSION
            observations = reopened.conn.execute(
                "SELECT valid_to,recorded_at FROM memory_closures WHERE memory_id=?", (memory,),
            ).fetchall()
            assert [tuple(row) for row in observations] == [(200.0, 300.0)]
        finally:
            reopened.close()


def test_concurrent_closures_preserve_independent_observations(tmp_path, monkeypatch):
    path = tmp_path / "concurrent.db"
    first = Store(str(path))
    _, memory, _ = _seed(first)
    second = Store(str(path))
    barrier = threading.Barrier(2)
    monkeypatch.setattr("engraphis.core.store.now_ts", lambda: 500.0)

    def close(store, endpoint):
        barrier.wait(timeout=5)
        store.close_validity(memory, at=endpoint)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(close, first, 200.0), pool.submit(close, second, 300.0)]
            for future in futures:
                future.result(timeout=15)
        record = first.get_memory(memory)
        assert set(record.closure_observations) >= {(200.0, 500.0), (300.0, 500.0)}
        assert record.valid_to == 200.0
        assert first.conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        first.close()
        second.close()


@pytest.mark.parametrize("world,known,visible", ANCHORS)
def test_public_graph_suggestions_and_evidence_use_all_observations(tmp_path, monkeypatch, world, known, visible):
    service = MemoryService.create(str(tmp_path / "public-graph.db"), graph_extractor="none")
    try:
        _, memory, _ = _seed(service.store)
        _close_twice(service.store, memory, monkeypatch)
        suggestions = service.graph_suggest(
            workspace="closure-workspace", query="Aurora", valid_at=world, known_at=known,
        )
        assert (memory in {item["id"] for item in suggestions["groups"]["memories"]}) is visible
        evidence = service.graph_entity_evidence(
            "ent_closure_alpha", workspace="closure-workspace", valid_at=world, known_at=known,
        )
        assert (memory in {item["memory_id"] for item in evidence["evidence"]}) is visible
    finally:
        service.close()


@pytest.mark.parametrize("world,known,closed", [(250.0, 550.0, True), (350.0, 450.0, True),
                                               (250.0, 450.0, False), (350.0, 350.0, False)])
def test_public_graph_history_evidence_uses_source_closure_history(tmp_path, monkeypatch, world, known, closed):
    service = MemoryService.create(str(tmp_path / "public-history.db"), graph_extractor="none")
    try:
        _, memory, _ = _seed(service.store)
        _close_twice(service.store, memory, monkeypatch)
        evidence = service.graph_entity_evidence(
            "ent_closure_alpha", workspace="closure-workspace", valid_at=world, known_at=known,
            include_history=True,
        )
        assert (memory in {item["memory_id"] for item in evidence["evidence"]}) is closed
    finally:
        service.close()


@pytest.mark.parametrize("world,known,ending", [(250.0, 450.0, 300.0), (350.0, 450.0, 300.0),
                                               (250.0, 550.0, 200.0), (150.0, 550.0, 200.0),
                                               (350.0, 350.0, None)])
def test_classic_graph_time_payload_projects_the_known_source_ending(tmp_path, monkeypatch, world, known, ending):
    service = MemoryService.create(str(tmp_path / "classic-history.db"), graph_extractor="none")
    try:
        _, memory, edge = _seed(service.store)
        _close_twice(service.store, memory, monkeypatch)
        payload = service.graph(workspace="closure-workspace", valid_at=world, known_at=known, backfill=False)
        relation = next(item for item in payload["edges"] if item["id"] == edge)
        assert relation.get("valid_to") == ending
    finally:
        service.close()


def test_classic_graph_keeps_a_relation_live_when_an_independent_source_is_live(tmp_path, monkeypatch):
    service = MemoryService.create(str(tmp_path / "classic-multiple.db"), graph_extractor="none")
    try:
        workspace, memory, edge = _seed(service.store)
        live = service.store.add_memory(MemoryRecord(
            id="mem_live_independent", content="Independent source confirms the dependency.",
            workspace_id=workspace, scope=Scope.WORKSPACE, valid_from=100.0, ingested_at=100.0,
            provenance={"source":"local_cli", "trusted":True, "review_state":"approved"},
        ))
        service.store.add_edge_support(edge, {"memory_id":live})
        _close_twice(service.store, memory, monkeypatch)
        payload = service.graph(workspace="closure-workspace", valid_at=250.0, known_at=550.0, backfill=False)
        relation = next(item for item in payload["edges"] if item["id"] == edge)
        assert relation.get("valid_to") is None
    finally:
        service.close()
