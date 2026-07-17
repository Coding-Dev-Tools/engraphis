"""Regression test for the engraphis_start_session workspace-default contract.

Cron jobs (hourly-dev-engineer, ops-heartbeat, dashboard-refresh, vault-cron-health,
...) call engraphis_start_session WITHOUT the workspace argument. The MCP tool must
default workspace to 'default' rather than rejecting the call with
"workspace Field required" (the fleet-wide contract bug, 200+ occurrences in
gateway.log). This test locks that behavior and the fail-loud contract for empty/None.
"""
import json

import pytest

pytest.importorskip("mcp", reason="optional 'mcp' extra not installed")


def _module_with_memory_db(monkeypatch):
    import engraphis.mcp_server as srv
    from engraphis.service import MemoryService

    monkeypatch.setattr(srv, "_service", MemoryService.create(":memory:"))
    return srv


def test_start_session_omitted_workspace_defaults_to_default(monkeypatch):
    srv = _module_with_memory_db(monkeypatch)
    # No workspace argument at all -> must succeed and land in "default".
    out = json.loads(srv.engraphis_start_session())
    assert out["status"] == "active"
    assert out["workspace"] == "default"


def test_start_session_explicit_default_workspace(monkeypatch):
    srv = _module_with_memory_db(monkeypatch)
    out = json.loads(srv.engraphis_start_session(workspace="default"))
    assert out["status"] == "active"
    assert out["workspace"] == "default"


def test_start_session_explicit_named_workspace(monkeypatch):
    srv = _module_with_memory_db(monkeypatch)
    out = json.loads(srv.engraphis_start_session(workspace="acme", repo="web"))
    assert out["status"] == "active"
    assert out["workspace"] == "acme"


def test_start_session_explicit_none_workspace_rejected(monkeypatch):
    srv = _module_with_memory_db(monkeypatch)
    # Explicit None (vs omitted, which legitimately defaults to 'default') must
    # fail-loud, never silently coerce to 'default' or crash ungracefully.
    out = srv.engraphis_start_session(workspace=None)
    assert out.startswith("Error:")


def test_start_session_empty_workspace_rejected(monkeypatch):
    srv = _module_with_memory_db(monkeypatch)
    # Empty string violates min_length=1 -> service must report an error, not crash.
    out = srv.engraphis_start_session(workspace="")
    assert out.startswith("Error:")
