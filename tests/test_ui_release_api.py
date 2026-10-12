"""Release regressions for cheap bootstrap, graph serialization and bounded evidence."""
# ruff: noqa: E402 -- optional server stack guard precedes its imports
import pytest

pytest.importorskip("fastapi")

import fastapi.routing
from fastapi import FastAPI
from fastapi.testclient import TestClient

from engraphis.core.interfaces import Edge, MemoryRecord, Node, Scope
from engraphis.routes import v2_api
from engraphis import service as service_module
from engraphis.service import MemoryService


def test_graph_http_returns_exact_unicode_primitives_without_recursive_encoding(monkeypatch):
    service = MemoryService.create(":memory:", graph_extractor="none")
    scene = {
        "nodes": [{"id": "ent_1", "label": "工程 · café", "x": 0.125}],
        "edges": [], "meta": {"cache_hit": True, "nested": [None, False, 7]},
    }
    monkeypatch.setattr(v2_api, "service", lambda: service)
    monkeypatch.setattr(service, "graph_scene", lambda **_kwargs: scene)

    def unexpected_encoding(*_args, **_kwargs):
        raise AssertionError("cached scenes must bypass recursive response encoding")

    monkeypatch.setattr(fastapi.routing, "jsonable_encoder", unexpected_encoding)
    app = FastAPI()
    app.include_router(v2_api.router)
    try:
        response = TestClient(app).get("/api/graph/scene?workspace=release-ui")
        assert response.status_code == 200
        assert response.json() == scene
        assert response.headers["content-type"] == "application/json"
    finally:
        service.close()


def test_ledger_bootstrap_can_skip_unused_stats_and_default_still_includes_them(monkeypatch):
    service = MemoryService.create(":memory:", graph_extractor="none")
    service.store.get_or_create_workspace("release-ui")
    calls = []
    monkeypatch.setattr(v2_api, "service", lambda: service)
    monkeypatch.setattr(v2_api, "get_license", lambda: {"plan": "local"})
    monkeypatch.setattr(v2_api, "_update_snapshot", lambda: {})
    monkeypatch.setattr(service, "stats", lambda **kwargs: calls.append(kwargs) or {"memories": 0})
    app = FastAPI()
    app.include_router(v2_api.router)
    try:
        client = TestClient(app)
        cheap = client.get("/api/bootstrap?include_stats=false").json()
        assert cheap["workspaces"][0]["name"] == "release-ui"
        assert cheap["stats"] is None and calls == []
        assert client.get("/api/bootstrap").json()["stats"] == {"memories": 0}
        assert len(calls) == 1
    finally:
        service.close()


def test_duplicate_support_candidates_report_truncation_before_distinct_card_cap(monkeypatch):
    service = MemoryService.create(":memory:", graph_extractor="none")
    wid = service.store.get_or_create_workspace("release-ui")
    memory_id = service.store.add_memory(MemoryRecord(
        id="", content="Repeated evidence.", workspace_id=wid, scope=Scope.WORKSPACE,
    ))
    source = service.store.upsert_entity(Node(
        id="ent_source", name="Source", ntype="concept", workspace_id=wid,
    ))
    target = service.store.upsert_entity(Node(
        id="ent_target", name="Target", ntype="concept", workspace_id=wid,
    ))
    for index in range(4):
        service.store.upsert_edge(Edge(
            id=f"edg_{index}", src=source, dst=target, relation=f"relation_{index}",
            workspace_id=wid,
            provenance={"source": "structured_extractor", "memory_id": memory_id},
        ))
    monkeypatch.setattr(service_module, "GRAPH_ENTITY_EVIDENCE_CANDIDATE_LIMIT", 2)
    try:
        result = service.graph_entity_evidence(source, workspace="release-ui")
        assert result["totals"]["evidence"] == 1
        assert result["evidence"][0]["memory_id"] == memory_id
        assert result["truncation"]["evidence"] is True
    finally:
        service.close()
