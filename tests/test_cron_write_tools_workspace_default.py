"""Regression: every cron-auto-fired WRITE memory tool must default workspace to 'default'.

The 2026-07 fleet-wide outage ("workspace Field required", 200+ occurrences in
gateway.log) happened because ``engraphis_start_session`` hard-required ``workspace``
while the scheduled fleet (hourly-dev-engineer, ops-heartbeat, dashboard-refresh,
vault-cron-health, ...) calls these tools WITHOUT one. That single tool was fixed and
locked by ``test_start_session_workspace_default.py``.

But the *same class* of outage hits every auto-fired WRITE tool the fleet calls without a
workspace. ``engraphis_remember`` and ``engraphis_record_event`` are the actual memory
*write* path: if either regresses to a required ``workspace`` (e.g. someone drops the
``= "default"`` while adding a field, or re-tightens ``min_length=1`` into a required
positional), the fleet silently loses ALL memory writes again — a strictly worse failure
than the session tool, because the data never lands at all.

This test pins the (workspace defaults to 'default') contract for all three auto-fired
write/session tools at two levels:
  * signature level — asserts the ``workspace`` parameter still carries the ``"default"``
    default (this is the *exact* thing that broke: a missing default), and
  * behavioral level — omitting ``workspace`` succeeds and lands the write in 'default'.

NOTE: ``engraphis_recall`` is deliberately excluded. It is a stateful retrieval tool whose
``workspace`` defaults to ``None`` (search-broadly semantics), so omitting it returns an
empty/again result set rather than crashing with "workspace Field required". Its
reinforcement/receipt side effects do not make it one of the auto-fired write/session tools
whose default contract this test pins; its None default is intentional.
"""
import inspect
import json

import pytest

pytest.importorskip("mcp", reason="optional 'mcp' extra not installed")

# Tools the scheduled fleet fires WITHOUT a workspace arg on the write path.
CRON_WRITE_TOOLS = (
    "engraphis_start_session",
    "engraphis_remember",
    "engraphis_record_event",
)


def _module_with_memory_db(monkeypatch):
    import engraphis.mcp_server as srv
    from engraphis.service import MemoryService

    monkeypatch.setattr(srv, "_service", MemoryService.create(":memory:"))
    return srv


@pytest.mark.parametrize("tool_name", CRON_WRITE_TOOLS)
def test_cron_write_tool_workspace_defaults_to_default(monkeypatch, tool_name):
    """Signature guard: the exact regression (a removed/renamed default) fails right here."""
    srv = _module_with_memory_db(monkeypatch)
    param = inspect.signature(getattr(srv, tool_name)).parameters.get("workspace")
    assert param is not None, f"{tool_name} lost its 'workspace' parameter"
    assert param.default == "default", (
        f"{tool_name}.workspace default is {param.default!r}, not 'default' — cron calls "
        "that omit workspace will fail with 'workspace Field required' (fleet-wide "
        "memory-write outage)."
    )


def test_remember_omitted_workspace_stores_in_default(monkeypatch):
    srv = _module_with_memory_db(monkeypatch)
    out = json.loads(srv.engraphis_remember(
        content="cron write path must not require an explicit workspace"))
    assert out["stored"] is True
    assert out["workspace"] == "default"


def test_record_event_omitted_workspace_succeeds_in_default(monkeypatch):
    srv = _module_with_memory_db(monkeypatch)
    # The fleet always opens a session first, which provisions the 'default' workspace.
    started = json.loads(srv.engraphis_start_session())
    assert started["workspace"] == "default"

    omitted = json.loads(srv.engraphis_record_event(
        kind="ops_probe", content="hourly-dev-engineer regression event (omitted ws)"))
    # record_event returns {"id","kind"}; a required-workspace regression would instead
    # surface as an "Error: ... workspace Field required" string that fails json.loads or
    # carries no id.
    assert isinstance(omitted, dict) and omitted.get("id"), (
        f"record_event with omitted workspace did not return an id: {omitted!r}")
    assert omitted["kind"] == "ops_probe"

    # The omitted-workspace path must behave like passing workspace="default" explicitly
    # (parity check; combined with the signature guard above this pins "omitted -> default"
    # without depending on retrieval-ranking heuristics).
    explicit = json.loads(srv.engraphis_record_event(
        kind="ops_probe", content="hourly-dev-engineer regression event (explicit default)",
        workspace="default"))
    assert isinstance(explicit, dict) and explicit.get("id"), (
        f"record_event with workspace='default' did not return an id: {explicit!r}")
