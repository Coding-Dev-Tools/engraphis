"""An empty eligible source is not evidence of a procedural policy."""
import hashlib
import json

import pytest

from engraphis.core.spec_crawl import DEFAULT_AXES, crawl_spec
from engraphis.service import MemoryService


@pytest.fixture(params=[("service", False), ("service", True), ("http", False),
                        ("http", True), ("mcp", False)])
def procedural_transport(request, monkeypatch):
    transport, include_trace = request.param
    if transport == "http":
        pytest.importorskip("fastapi", reason="HTTP transport needs the server extra")
        pytest.importorskip("httpx", reason="HTTP transport needs the test client")
    elif transport == "mcp":
        pytest.importorskip("mcp", reason="MCP transport needs the MCP extra")
    svc = MemoryService.create(":memory:")
    client = None
    if transport == "http":
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from engraphis.routes import v2_api

        monkeypatch.setattr(v2_api, "_service", svc)
        app = FastAPI()
        app.include_router(v2_api.router)
        client = TestClient(app)

        def audit(**values):
            response = client.post("/api/spec/crawl", json=values)
            assert response.status_code == 200, response.text
            return response.json()
    elif transport == "mcp":
        from engraphis import mcp_server

        monkeypatch.setattr(mcp_server, "service", lambda: svc)

        def audit(**values):
            values.pop("include_trace")  # MCP intentionally omits replay traces.
            return json.loads(mcp_server.engraphis_spec_crawl(**values))
    else:
        audit = svc.spec_crawl
    try:
        yield svc, audit, include_trace
    finally:
        if client is not None:
            client.close()
        svc.close()


@pytest.mark.parametrize("selection", ["empty", "semantic", "pending", "retired",
                                      "foreign_workspace", "foreign_repo", "foreign_session"])
def test_zero_eligible_procedural_sources_have_zero_evidence_and_complete_schema(
    procedural_transport, selection,
):
    svc, audit, include_trace = procedural_transport
    sid = svc.start_session("alpha", repo="app", agent="selected")["session_id"]
    kwargs = {"workspace": "alpha"}
    options = {"workspace": "alpha", "mtype": "procedural", "trusted": True}
    if selection == "semantic":
        options["mtype"] = "semantic"
    if selection == "pending":
        options["source"] = "spec_crawl"
    if selection == "foreign_workspace":
        options["workspace"] = "beta"
    if selection == "foreign_repo":
        options["repo"] = "other"
        kwargs["repo"] = "app"
    if selection == "foreign_session":
        other = svc.start_session("alpha", repo="app", agent="other")["session_id"]
        options.update(repo="app", session_id=other, scope="session")
        kwargs = {"session_id": sid}
    if selection != "empty":
        saved = svc.remember("# Rules\nVerify 12 tests before release.", **options)
        if selection == "retired":
            svc.retire(saved["id"], workspace="alpha")
    before = svc.store.conn.total_changes
    report = audit(**kwargs, crawl_procedural=True, trace_claims=True, include_trace=include_trace)
    assert svc.store.conn.total_changes == before
    assert report["procedural_memory_count"] == report["procedural_total_eligible_memory_count"] == 0
    assert report["score"] == report["word_count"] == 0
    assert report["coverage"] == {axis: 0.0 for axis in DEFAULT_AXES}
    assert all(value == 0 for value in report["score_breakdown"].values())
    assert report["content_sha256"] == hashlib.sha256(b"").hexdigest()
    assert report["source_label"] == "procedural:alpha"
    for key in ("sections", "flags", "claims", "procedural_sources"):
        assert report[key] == []
    assert all(value == 0 for key, value in report["counts"].items() if key != "kinds")
    assert all(value == 0 for value in report["counts"]["kinds"].values())
    assert report["procedural_source_truncated"] is report["truncated"] is False
    assert report["claim_tracing"] == {
        "requested": True, "enabled": False,
        "disabled_reason": "procedural_sources_are_not_independent_evidence",
    }
    reference = crawl_spec("# Rules\nVerify 12 tests.", include_trace=include_trace)
    extras = {"claim_tracing", "procedural_sources", "procedural_memory_count",
              "procedural_total_eligible_memory_count", "procedural_source_truncated"}
    assert set(report) - extras == set(reference)
    assert set(report["counts"]) == set(reference["counts"])
    assert set(report["counts"]["kinds"]) == set(reference["counts"]["kinds"])
    assert set(report["score_breakdown"]) == set(reference["score_breakdown"])
    assert ("trace" in report) is include_trace
    if include_trace:
        assert report["trace"] == []
        assert report["trace_sha256"] == hashlib.sha256(repr([]).encode()).hexdigest()


def test_real_procedural_policy_and_direct_text_keep_their_scoring(procedural_transport):
    svc, audit, include_trace = procedural_transport
    saved = svc.remember("# Rules\nVerify 12 tests before release.", workspace="alpha",
                         mtype="procedural", title="Rules", trusted=True)
    report = audit(workspace="alpha", crawl_procedural=True, include_trace=include_trace)
    assert report["procedural_memory_count"] == 1
    assert report["score"] > 0 and report["coverage"]["rules"] > 0
    assert report["procedural_sources"][0]["memory_id"] == saved["id"]
    direct = audit(text="# Rules\nVerify 12 tests.", workspace="alpha", crawl_procedural=True,
                   include_trace=include_trace, trace_claims=False)
    assert "procedural_memory_count" not in direct
    assert direct["score"] > 0 and direct["coverage"]["rules"] > 0
