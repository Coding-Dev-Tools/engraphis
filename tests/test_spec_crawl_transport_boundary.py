"""HTTP credentials never authorize operator-local specification files."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
from threading import Event

import pytest

pytest.importorskip("mcp")
pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient
from fastapi import BackgroundTasks

from engraphis import mcp_server
from engraphis.config import settings
from engraphis.routes import v2_api
from engraphis.service import ValidationError, current_user


@pytest.fixture
def mounted_dashboard(monkeypatch, tmp_path):
    from engraphis.dashboard_app import create_app

    monkeypatch.setattr(settings, "db_path", str(tmp_path / "dashboard.db"))
    monkeypatch.setattr(settings, "api_token", "synthetic-transport-token")
    monkeypatch.setattr(settings, "loop_interval", 0)
    monkeypatch.setattr(mcp_server, "_service", None)
    monkeypatch.setattr(v2_api, "_service", None)
    monkeypatch.setattr(mcp_server.mcp.settings, "json_response", True)
    allowed = tmp_path / "approved"
    allowed.mkdir()
    source = allowed / "policy.md"
    source.write_text("# Operator policy\nVerify 17 synthetic transport tests.\n", encoding="utf-8")
    monkeypatch.setenv("ENGRAPHIS_INDEX_ROOTS", str(allowed))
    app = create_app()
    assert app.state.mcp_over_http
    with TestClient(app, base_url="http://127.0.0.1:8700",
                    client=("203.0.113.9", 50000)) as client:
        yield client, v2_api.service(), source


def _rpc(client, name, arguments, *, authenticated=True):
    headers = {"Accept": "application/json, text/event-stream"}
    if authenticated:
        headers["Authorization"] = "Bearer synthetic-transport-token"
    response = client.post("/mcp/", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }, headers=headers)
    return response


def _capability(client, action):
    response = _rpc(client, "engraphis_discover_actions", {"task": action, "limit": 3})
    assert response.status_code == 200, response.text
    payload = json.loads(response.json()["result"]["content"][0]["text"])
    return next(row for row in payload["actions"] if row["canonical_action"] == action)


def _execute(client, action, arguments):
    capability = _capability(client, action)
    return _rpc(client, "engraphis_execute_read", {
        "capability_id": capability["capability_id"],
        "schema_digest": capability["schema_digest"], "arguments": arguments,
    })


def test_bearer_http_cannot_read_approved_local_source(mounted_dashboard, monkeypatch):
    client, service, source = mounted_dashboard
    assert current_user() is None
    assert service.allowed_workspaces is None
    # Direct local-owner use remains valid; this fixture is the only path read.
    assert service.spec_crawl(source_id=str(source), trace_claims=False)["word_count"] > 0
    reads = []
    from engraphis import service as service_module
    original_read = service_module._read_local_source

    def inspected(path, **kwargs):
        reads.append(str(path))
        assert path == source
        return original_read(path, **kwargs)

    monkeypatch.setattr(service_module, "_read_local_source", inspected)
    response = _execute(client, "spec_crawl", {"source_id": str(source), "trace_claims": False})
    assert response.status_code == 200
    assert reads == []
    assert "Verify 17 synthetic" not in response.text
    assert "error" in response.text.lower()
    assert current_user() is None


def test_http_text_and_procedural_crawls_remain_available(mounted_dashboard):
    client, service, source = mounted_dashboard
    service.remember("Verify 19 policy tests.", workspace="default", mtype="procedural",
                     trusted=True, resolve_conflicts=False)
    for arguments in [
        {"text": "# Rules\nVerify 23 examples.", "trace_claims": False},
        {"workspace": "default", "crawl_procedural": True, "trace_claims": False},
    ]:
        response = _execute(client, "spec_crawl", arguments)
        assert response.status_code == 200, response.text
        result = json.loads(response.json()["result"]["content"][0]["text"])
        assert result["result"]["word_count"] > 0
    assert service.spec_crawl(source_id=str(source), trace_claims=False)["word_count"] > 0


def test_http_rest_rejects_source_path_and_unauthenticated_mcp(mounted_dashboard):
    client, _, source = mounted_dashboard
    response = client.post("/api/spec/crawl", json={"source_id": str(source)}, headers={
        "Authorization": "Bearer synthetic-transport-token",
    })
    assert response.status_code == 422
    denied = _rpc(client, "engraphis_discover_actions", {"task": "audit prompt"}, authenticated=False)
    assert denied.status_code == 401


def test_nested_bindings_and_exception_restore_transport_without_granting_files(mounted_dashboard):
    from engraphis.service_context import (
        bind_service, http_transport_context, local_source_access_allowed,
    )

    _, service, source = mounted_dashboard
    principal = {"id": "reviewer", "email": "reviewer@example.test"}
    assert local_source_access_allowed()
    with pytest.raises(RuntimeError, match="synthetic failure"):
        with http_transport_context():
            with http_transport_context(), bind_service(service, principal=principal):
                assert not local_source_access_allowed()
                with pytest.raises(ValidationError, match="local operator"):
                    service.spec_crawl(source_id=str(source), trace_claims=False)
            assert not local_source_access_allowed()
            assert current_user() is None
            raise RuntimeError("synthetic failure")
    assert local_source_access_allowed()
    assert current_user() is None
    assert service.spec_crawl(source_id=str(source), trace_claims=False)["word_count"] > 0


def test_async_background_context_stays_denied_after_parent_returns(mounted_dashboard):
    from engraphis.service_context import http_transport_context, local_source_access_allowed

    _, service, source = mounted_dashboard

    async def scenario():
        proceed = asyncio.Event()

        async def background():
            await proceed.wait()
            assert not local_source_access_allowed()
            with pytest.raises(ValidationError, match="local operator"):
                await asyncio.to_thread(service.spec_crawl, source_id=str(source), trace_claims=False)

        with http_transport_context():
            task = asyncio.create_task(background())
        assert local_source_access_allowed()
        proceed.set()
        await task
        assert service.spec_crawl(source_id=str(source), trace_claims=False)["word_count"] > 0

    asyncio.run(scenario())


def test_concurrent_local_and_http_contexts_cannot_share_permission(mounted_dashboard):
    from engraphis.service_context import http_transport_context, local_source_access_allowed

    _, service, source = mounted_dashboard

    async def scenario():
        restricted = asyncio.Event()
        local_done = asyncio.Event()

        async def remote():
            with http_transport_context():
                restricted.set()
                await local_done.wait()
                assert not local_source_access_allowed()
                with pytest.raises(ValidationError, match="local operator"):
                    service.spec_crawl(source_id=str(source), trace_claims=False)

        async def local():
            await restricted.wait()
            assert local_source_access_allowed()
            assert service.spec_crawl(source_id=str(source), trace_claims=False)["word_count"] > 0
            local_done.set()

        await asyncio.gather(remote(), local())

    asyncio.run(scenario())


def test_mounted_http_and_direct_call_have_independent_simultaneous_contexts(
    mounted_dashboard, monkeypatch,
):
    from engraphis.service_context import local_source_access_allowed

    client, service, source = mounted_dashboard
    remote_entered, local_complete = Event(), Event()
    original = service.spec_crawl

    def synchronized(*args, **kwargs):
        if not local_source_access_allowed():
            remote_entered.set()
            assert local_complete.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "spec_crawl", synchronized)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_execute, client, "spec_crawl", {
            "source_id": str(source), "trace_claims": False,
        })
        try:
            assert remote_entered.wait(5)
            assert service.spec_crawl(source_id=str(source), trace_claims=False)["word_count"] > 0
        finally:
            local_complete.set()
        response = future.result(timeout=5)
        assert response.status_code == 200 and "error" in response.text.lower()


def test_http_background_callback_retains_file_restriction(mounted_dashboard):
    from engraphis.service_context import local_source_access_allowed

    client, service, source = mounted_dashboard
    observations = []

    def background():
        observations.append(local_source_access_allowed())
        with pytest.raises(ValidationError, match="local operator"):
            service.spec_crawl(source_id=str(source), trace_claims=False)

    @client.app.post("/api/transport-boundary-fixture")
    def schedule(background_tasks: BackgroundTasks):
        background_tasks.add_task(background)
        return {"scheduled": True}

    response = client.post("/api/transport-boundary-fixture", headers={
        "Authorization": "Bearer synthetic-transport-token",
    })
    assert response.status_code == 200
    assert observations == [False]


def test_stdio_process_retains_local_source_access(tmp_path):
    from scripts.smoke_installed_product import _Mcp

    source = tmp_path / "local-policy.md"
    source.write_text("# Local policy\nVerify 31 local tests.\n", encoding="utf-8")
    env = os.environ.copy()
    env.update({
        "ENGRAPHIS_DB_PATH": str(tmp_path / "stdio.db"), "ENGRAPHIS_INDEX_ROOTS": str(tmp_path),
        "ENGRAPHIS_EXTRACTOR": "none", "ENGRAPHIS_GRAPH_EXTRACTOR": "none",
        "ENGRAPHIS_VECTOR_BACKEND": "numpy", "ENGRAPHIS_MCP_WARMUP": "1",
    })
    root = Path(__file__).resolve().parents[1]
    with _Mcp([sys.executable, "-m", "engraphis.mcp_server"], env, root, 15) as client:
        discovered = client.request("tools/call", {
            "name": "engraphis_discover_actions", "arguments": {"task": "spec_crawl", "limit": 3},
        })
        actions = json.loads(discovered["content"][0]["text"])["actions"]
        capability = next(row for row in actions if row["canonical_action"] == "spec_crawl")
        result = client.request("tools/call", {
            "name": "engraphis_execute_read",
            "arguments": {
                "capability_id": capability["capability_id"],
                "schema_digest": capability["schema_digest"],
                "arguments": {"source_id": str(source), "trace_claims": False},
            },
        })
        assert not result.get("isError"), result
        report = json.loads(result["content"][0]["text"])["result"]
        assert report["word_count"] > 0
        assert report["source_label"] == source.name
