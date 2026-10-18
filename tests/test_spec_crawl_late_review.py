"""Coverage, bounded conflict reporting, and conservative directive identities."""
from __future__ import annotations

import copy
import json

import pytest

from engraphis.core import spec_crawl


def _ports(count, *, subject="service", prefix="port", repeated=False):
    return [
        {"id": f"{prefix}-{i}", "subject_key": subject, "claim_kind": "configuration",
         "content": f"port: {9000 if repeated else 9000 + i}", "valid_from": i + 1}
        for i in range(count)
    ]


def _pair(left, right):
    return [
        {"id": "old", "subject_key": "service", "claim_kind": "policy",
         "valid_from": 1, "content": left},
        {"id": "new", "subject_key": "service", "claim_kind": "policy",
         "valid_from": 2, "content": right},
    ]


@pytest.mark.parametrize("content", ["This note mentions policy.", "Policy guidelines.", "Must."])
def test_incidental_subthreshold_axis_never_removes_the_missing_rules_gap(content):
    report = spec_crawl.analyze_memory_nodes([{"id": "note", "content": content}])
    assert report["coverage"]["rules"] == 0
    assert "rules" not in report["nodes"][0]["covered_axes"]
    assert any(item["axis"] == "rules" for item in report["policy_gaps"])
    assert report["score_breakdown"]["coverage_points"] == 0


@pytest.mark.parametrize("title,content,axes", [
    ("Policy", "Raster dimensions.", {"rules"}),
    ("Entry", "# Rules\nRaster dimensions.", {"rules"}),
    ("Entry", "Never deploy production.", {"rules"}),
    ("Entry", "Raster dimensions.", {"context"}),
    ("Entry", "```text\n# Rules\nNever deploy production.\n```", set()),
])
def test_real_threshold_heading_directive_and_unclassified_controls(title, content, axes):
    report = spec_crawl.analyze_memory_nodes([{"id": "note", "title": title, "content": content}])
    assert set(report["nodes"][0]["covered_axes"]) == axes


@pytest.mark.parametrize("count", [0, 1, 5, 6, 10, 11, 16])
def test_exact_conflict_and_remedy_counts_at_independent_boundaries(count):
    report = spec_crawl.analyze_memory_nodes(_ports(count), include_trace=False)
    total = count * (count - 1) // 2
    assert len(report["conflicts"]) == min(total, 50)
    assert report["conflicts_total"] == total
    assert report["conflict_limit"] == 50
    assert report["conflicts_truncated"] is (total > 50)
    assert report["analysis_truncated"] is (total > 50)
    assert report["conflict_remediation_count"] == min(total, 10)
    assert report["conflict_remediation_limit"] == 10
    assert report["conflict_remediation_truncated"] is (total > 10)
    assert sum(item["action"] == "supersede" for item in report["remediation_plan"]) == min(total, 10)


def test_exactly_fifty_details_are_complete_while_ten_suggestions_are_partial():
    nodes = _ports(10, subject="first", prefix="first")
    nodes += _ports(5, subject="second", prefix="second", repeated=True)
    nodes += [{"id": "second-other", "subject_key": "second", "claim_kind": "configuration",
               "content": "port: 9999", "valid_from": 10}]
    report = spec_crawl.analyze_memory_nodes(nodes)
    assert len(report["conflicts"]) == report["conflicts_total"] == 50
    assert report["conflicts_truncated"] is report["analysis_truncated"] is False
    assert report["conflict_remediation_count"] == 10
    assert report["conflict_remediation_truncated"] is True


def test_total_deduplicates_scalar_keys_and_directives_and_bounds_remedy_work(monkeypatch):
    nodes = _ports(16)
    for i, node in enumerate(nodes):
        node["content"] += f"\ntimeout: {i + 1}\n" + (
            "Always deploy production." if i < 8 else "Never deploy production.")
    original = copy.deepcopy(nodes)
    real_remedy = spec_crawl._conflict_remedy
    calls = []

    def remedy(*args, **kwargs):
        calls.append((args[0]["id"], args[1]["id"]))
        return real_remedy(*args, **kwargs)

    monkeypatch.setattr(spec_crawl, "_conflict_remedy", remedy)
    report = spec_crawl.analyze_memory_nodes(nodes, include_trace=False)
    assert len(report["conflicts"]) == len(calls) == 50
    assert report["conflicts_total"] == 120
    assert report["redundancies"] == []
    assert nodes == original


def test_directive_only_conflicts_count_omitted_pairs():
    nodes = [dict(id=f"rule-{i}", subject_key="service", claim_kind="policy",
                  valid_from=i + 1, content=("Always" if i < 10 else "Never") +
                  " require authentication.") for i in range(22)]
    report = spec_crawl.analyze_memory_nodes(nodes, include_trace=False)
    assert len(report["conflicts"]) == 50
    assert report["conflicts_total"] == 120
    assert report["analysis_truncated"] is True


def test_maximum_node_selection_counts_pairs_without_unbounded_remedies():
    report = spec_crawl.analyze_memory_nodes(_ports(200), include_trace=False)
    assert report["node_count"] == 200
    assert report["conflicts_total"] == 19_900
    assert len(report["conflicts"]) == 50
    assert report["conflict_remediation_count"] == 10


@pytest.mark.parametrize("difference", ["subject_key", "claim_kind"])
def test_distinct_subject_or_claim_partitions_are_excluded_from_conflict_totals(difference):
    nodes = _ports(2)
    nodes[1][difference] = "unrelated"
    report = spec_crawl.analyze_memory_nodes(nodes)
    assert report["conflicts_total"] == 0
    assert not report["conflicts"]
    assert report["conflicts_truncated"] is report["analysis_truncated"] is False


@pytest.mark.parametrize("left,right", [
    ("deploy production", "deploy to production"),
    ("deploy to production", "deploy into production"),
    ("deploy the production", "deploy to production"),
    ("use the local database", "use local database"),
    ("deploy the artifact to the production from the registry",
     "deploy artifact into production from registry"),
    ("deploy to https://example.test/Finance", "deploy https://example.test/Finance"),
])
@pytest.mark.parametrize("reverse", [False, True])
def test_grammar_equivalent_actions_identify_the_same_dated_opposition(left, right, reverse):
    nodes = _pair("Always " + left + ".", "Never " + right + ".")
    if reverse:
        nodes.reverse()
    report = spec_crawl.analyze_memory_nodes(nodes)
    assert len(report["conflicts"]) == 1
    assert report["conflicts"][0]["remedy"]["retire_node"] == "old"
    assert report["conflicts"][0]["remedy"]["keep_node"] == "new"


@pytest.mark.parametrize("left,right", [
    ("deploy from production", "deploy to production"),
    ("deploy production via blue", "deploy production via green"),
    ("deploy production without approval", "deploy production with approval"),
    ("deploy to production", "deploy into staging"),
    ("deploy in production", "deploy to production"),
    ("deploy a to production", "deploy to production"),
    ("deploy production from a via blue", "deploy production from via blue"),
    ("use a as the fallback", "use as the fallback"),
    ("use a via the fallback", "use via fallback"),
    ("deploy to https://example.test/Finance", "deploy https://example.test/finance"),
    ("read /etc/Production", "read /etc/production"),
])
def test_grammar_normalization_preserves_distinct_objects_sources_methods_and_conditions(left, right):
    report = spec_crawl.analyze_memory_nodes(_pair("Always " + left, "Never " + right))
    assert not report["conflicts"]
    assert not any("retire_node" in item for item in report["remediation_plan"])


def test_general_deployment_policy_still_clarifies_a_normalized_scoped_exception():
    report = spec_crawl.analyze_memory_nodes(_pair("Always deploy.", "Never deploy to production."))
    assert len(report["conflicts"]) == 1
    assert report["conflicts"][0]["severity"] == "medium"
    assert report["conflicts"][0]["remedy"]["action"] == "clarify"
    assert "retire_node" not in report["conflicts"][0]["remedy"]


def test_normalized_long_directive_keeps_its_complete_scope_identity():
    target = "artifact " * 40
    report = spec_crawl.analyze_memory_nodes(_pair(
        "Always deploy the " + target + "to production.",
        "Never deploy " + target + "into production."))
    assert len(report["conflicts"]) == 1
    different = spec_crawl.analyze_memory_nodes(_pair(
        "Always deploy the " + target + "to production.",
        "Never deploy " + target + "into staging."))
    assert not different["conflicts"]
    assert all(len(key) < 125 for node in report["nodes"] for key in node["directives"])


@pytest.fixture
def capped_service():
    pytest.importorskip("fastapi")
    from engraphis.service import MemoryService
    svc = MemoryService.create(":memory:", graph_extractor="none")
    try:
        for i in range(16):
            svc.remember(f"port: {9000 + i}", workspace="selected", subject_key="service",
                         claim_kind="configuration", valid_from=i + 1,
                         resolve_conflicts=False, trusted=True)
        yield svc
    finally:
        svc.close()


@pytest.mark.parametrize("transport", ["service", "http", "mcp"])
def test_real_public_transports_preserve_conflict_limits_without_writing(capped_service, monkeypatch, transport):
    svc = capped_service
    before = svc.store.conn.total_changes
    if transport == "http":
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from engraphis.routes import v2_api
        monkeypatch.setattr(v2_api, "service", lambda: svc)
        app = FastAPI()
        app.include_router(v2_api.router)
        with TestClient(app) as client:
            response = client.post("/api/spec/crawl/memories",
                                   json={"workspace": "selected", "include_trace": False})
        assert response.status_code == 200, response.text
        report = response.json()
    elif transport == "mcp":
        pytest.importorskip("mcp")
        from engraphis import mcp_server
        monkeypatch.setattr(mcp_server, "service", lambda: svc)
        report = json.loads(mcp_server.engraphis_spec_crawl_memories(workspace="selected"))
    else:
        report = svc.spec_crawl_memories(workspace="selected", include_trace=False)
    assert svc.store.conn.total_changes == before
    assert report["node_count"] == report["total_eligible_node_count"] == 16
    assert report["node_selection_truncated"] is False
    assert len(report["conflicts"]) == 50
    assert report["conflicts_total"] == 120
    assert report["analysis_truncated"] is report["truncated"] is True
    assert report["conflict_remediation_count"] == 10
