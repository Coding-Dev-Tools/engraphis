"""Empty audits retain the public cluster schema, radar rows and zero-score semantics."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest

from engraphis.core.spec_crawl import (
    AXIS_LABELS, DEFAULT_AXES, MAX_MEMORY_SIMILARITY_TOKENS, MAX_SPEC_WORDS, analyze_memory_nodes,
)


def assert_cluster_schema(report, reference):
    assert set(report) == set(reference)
    assert report["cluster_health"] == report["cluster_health_score"]
    assert report["radar"] == report["coverage"]
    assert tuple(report["radar"]) == DEFAULT_AXES
    assert report["axis_labels"] == {axis: AXIS_LABELS[axis] for axis in DEFAULT_AXES}
    assert set(report["score_breakdown"]) == set(reference["score_breakdown"])
    gap = reference["policy_gaps"][0]
    assert all(set(item) == set(gap) for item in report["policy_gaps"])
    assert all(set(item["remedy"]) == set(gap["remedy"]) for item in report["policy_gaps"])


def assert_empty_semantics(report):
    assert report["node_count"] == report["cluster_health_score"] == report["cluster_health"] == 0
    assert report["coverage"] == report["radar"] == {axis: 0.0 for axis in DEFAULT_AXES}
    assert all(value == 0 for value in report["score_breakdown"].values())
    for key in ("nodes", "links", "conflicts", "redundancies", "orphans", "flags", "remediation_plan", "trace"):
        assert report[key] == []
    assert [gap["axis"] for gap in report["policy_gaps"]] == list(DEFAULT_AXES)
    assert all(gap["severity"] == "high" for gap in report["policy_gaps"])
    assert all(gap["description"] == "No memories for " + gap["axis"] for gap in report["policy_gaps"])
    assert all(gap["remedy"]["recommended_axis"] == gap["axis"] for gap in report["policy_gaps"])
    assert report["similarity_token_limit"] == MAX_MEMORY_SIMILARITY_TOKENS
    assert report["similarity_sampled_node_count"] == report["word_analysis_truncated_node_count"] == 0
    assert report["word_limit"] == MAX_SPEC_WORDS
    assert report["analysis_truncated"] is False


@pytest.mark.parametrize("include_trace", [False, True])
@pytest.mark.parametrize("empty_kind", ["list", "tuple", "iterator"])
def test_empty_core_audit_has_the_same_schema_as_a_populated_audit(include_trace, empty_kind):
    empty = {"list": [], "tuple": (), "iterator": iter(())}[empty_kind]
    report = analyze_memory_nodes(empty, workspace="alpha", include_trace=include_trace)
    reference = analyze_memory_nodes([{"id": "reference", "content": "Local reference notes."}],
                                     workspace="alpha", include_trace=include_trace)
    assert_cluster_schema(report, reference)
    assert_empty_semantics(report)
    assert json.loads(json.dumps(report)) == report


@pytest.fixture
def service():
    pytest.importorskip("fastapi", reason="route schema checks require the server extra")
    from engraphis.service import MemoryService
    svc = MemoryService.create(":memory:", graph_extractor="none")
    svc.create_workspace("selected")
    svc.create_workspace("reference")
    svc.remember("Independent reference notes.", workspace="reference", trusted=True)
    try:
        yield svc
    finally:
        svc.close()


@pytest.mark.parametrize("include_trace", [False, True])
@pytest.mark.parametrize("selection", ["no_memories", "empty_selection", "filtered_type", "pending_only"])
def test_actual_api_handler_keeps_empty_aliases_and_scope_metadata(service, monkeypatch, include_trace, selection):
    from fastapi.encoders import jsonable_encoder
    from engraphis.routes import v2_api
    request = {"workspace": "selected", "include_trace": include_trace}
    if selection != "no_memories":
        options = {"source": "spec_crawl"} if selection == "pending_only" else {"trusted": True}
        service.remember("Selected reference notes.", workspace="selected", **options)
    if selection == "empty_selection":
        request["memory_ids"] = []
    if selection == "filtered_type":
        request["mtypes"] = ["procedural"]
    monkeypatch.setattr(v2_api, "service", lambda: service)
    before = service.store.conn.total_changes
    report = jsonable_encoder(v2_api.spec_crawl_memories(v2_api._SpecCrawlMemoriesReq(**request)))
    reference = jsonable_encoder(v2_api.spec_crawl_memories(
        v2_api._SpecCrawlMemoriesReq(workspace="reference", include_trace=include_trace)))
    assert service.store.conn.total_changes == before
    assert_cluster_schema(report, reference)
    assert_empty_semantics(report)
    assert report["workspace"] == "selected" and report["repo"] is None
    assert report["total_eligible_node_count"] == 0
    assert report["node_selection_truncated"] is report["truncated"] is False
    assert report["node_limit"] == 150
    assert report["count_semantics"] == (
        "requested_selection" if selection == "empty_selection" else "bounded_scope_sample")


@pytest.mark.parametrize("include_trace", [False, True])
@pytest.mark.parametrize("populated", [False, True])
def test_empty_cluster_http_serialization_matches_populated_contract(service, monkeypatch, include_trace, populated):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from engraphis.routes import v2_api
    if populated:
        service.remember("Local source reference notes.", workspace="selected", trusted=True)
    monkeypatch.setattr(v2_api, "service", lambda: service)
    app = FastAPI()
    app.include_router(v2_api.router)
    before = service.store.conn.total_changes
    with TestClient(app) as client:
        response = client.post("/api/spec/crawl/memories",
                               json={"workspace": "selected", "include_trace": include_trace})
        reference = client.post("/api/spec/crawl/memories",
                                json={"workspace": "reference", "include_trace": include_trace})
    assert response.status_code == reference.status_code == 200
    assert service.store.conn.total_changes == before
    report = response.json()
    assert_cluster_schema(report, reference.json())
    assert report["node_count"] == int(populated)
    if not populated:
        assert_empty_semantics(report)


@pytest.mark.parametrize("include_trace", [False, True])
@pytest.mark.parametrize("json_output", [False, True])
@pytest.mark.parametrize("populated", [False, True])
def test_actual_workspace_cli_prints_all_axes_and_preserves_json_schema(
    tmp_path, include_trace, json_output, populated,
):
    pytest.importorskip("fastapi", reason="workspace CLI requires the server extra")
    from engraphis.service import MemoryService
    database = tmp_path / "cluster.db"
    svc = MemoryService.create(str(database), graph_extractor="none")
    try:
        svc.create_workspace("selected")
        if populated:
            svc.remember("Local reference notes.", workspace="selected", trusted=True)
        expected = svc.spec_crawl_memories(workspace="selected", include_trace=include_trace)
    finally:
        svc.close()
    command = [sys.executable, "-m", "scripts.spec_crawl", "--workspace", "selected",
               "--db", str(database), "--min-score", "1"]
    if json_output:
        command.append("--json")
    if not include_trace:
        command.append("--no-trace")
    environment = dict(os.environ, ENGRAPHIS_STATE_DIR=str(tmp_path / ".engraphis"),
                       ENGRAPHIS_EMBED_MODEL="", ENGRAPHIS_RERANK_MODEL="",
                       ENGRAPHIS_ALLOWED_WORKSPACES="", ENGRAPHIS_SERVICE_MODE="customer")
    completed = subprocess.run(command, cwd=Path(__file__).resolve().parents[1],
                               capture_output=True, text=True, env=environment, timeout=20)
    assert completed.returncode == int(expected["cluster_health_score"] < 1), completed.stderr
    assert ("FAILURE: Cluster health 0" in completed.stderr) is (not populated)
    if json_output:
        actual = json.loads(completed.stdout)
        assert actual == expected
        assert tuple(actual["radar"]) == DEFAULT_AXES
    else:
        rows = re.findall(r"^  ([A-Z]+)\s+\[([#-]{10})\] ([0-9]+)%$", completed.stdout, re.M)
        assert [axis.lower() for axis, _bar, _percent in rows] == list(DEFAULT_AXES)
        assert [int(percent) for _axis, _bar, percent in rows] == [
            round(expected["coverage"][axis] * 100) for axis in DEFAULT_AXES]
        assert f"Cluster Health: {expected['cluster_health']}/100" in completed.stdout
