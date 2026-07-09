"""Smoke test for the MCP binding. Skips cleanly when the optional 'mcp' package
is not installed, so the offline CI gate is unaffected."""
import json

import pytest

pytest.importorskip("mcp", reason="optional 'mcp' extra not installed")


def _module_with_memory_db(monkeypatch):
    import engraphis.mcp_server as srv
    from engraphis.service import MemoryService
    # Back the global service with an in-memory db so tests never touch real storage.
    monkeypatch.setattr(srv, "_service", MemoryService.create(":memory:"))
    return srv


_ALL_TOOLS = {
    "engraphis_remember", "engraphis_recall", "engraphis_why", "engraphis_timeline",
    "engraphis_recall_proactive", "engraphis_forget", "engraphis_pin", "engraphis_correct",
    "engraphis_link", "engraphis_record_event", "engraphis_index_repo",
    "engraphis_search_code", "engraphis_start_session", "engraphis_end_session",
    "engraphis_stats",
}


def test_server_identity_and_tools_registered():
    import asyncio

    import engraphis.mcp_server as srv
    assert srv.mcp.name == "engraphis_mcp"
    tools = {t.name: t for t in asyncio.run(srv.mcp.list_tools())}
    assert _ALL_TOOLS <= set(tools)
    # Flat schema (not a nested "params" object) so agents can call fields directly.
    props = tools["engraphis_remember"].inputSchema.get("properties", {})
    assert "content" in props and "workspace" in props and "params" not in props


def test_remember_and_recall_tool_callables(monkeypatch):
    srv = _module_with_memory_db(monkeypatch)
    stored = srv.engraphis_remember(
        content="We deploy via GitHub Actions on tag push.", workspace="acme", repo="infra")
    assert json.loads(stored)["stored"] is True

    recalled = srv.engraphis_recall(
        query="how do we deploy?", workspace="acme", repo="infra")
    rec = json.loads(recalled)
    assert rec["count"] >= 1
    assert "GitHub Actions" in rec["context"]


def test_remember_reports_resolution_op(monkeypatch):
    srv = _module_with_memory_db(monkeypatch)
    text = "We standardized on pnpm as the package manager for all frontend repos."
    first = json.loads(srv.engraphis_remember(content=text, workspace="acme", repo="web"))
    second = json.loads(srv.engraphis_remember(content=text, workspace="acme", repo="web"))
    assert first["op"] == "add"
    assert second["op"] == "noop"
    assert second["id"] == first["id"]


def test_tool_returns_actionable_error_on_bad_input(monkeypatch):
    srv = _module_with_memory_db(monkeypatch)
    out = srv.engraphis_remember(content="", workspace="acme")  # empty content -> service rejects
    assert out.startswith("Error:")


def test_why_and_timeline_tools(monkeypatch):
    srv = _module_with_memory_db(monkeypatch)
    srv.engraphis_remember(
        content="Until 2026-01 the rate limit was 100 requests per minute per API key.",
        workspace="acme", repo="web")
    srv.engraphis_remember(
        content="As of 2026-02 the rate limit was raised to 500 requests per minute per API key.",
        workspace="acme", repo="web")

    why = json.loads(srv.engraphis_why(query="what is the rate limit", workspace="acme", repo="web"))
    assert any("500" in m["content"] for m in why["answer"])
    assert any("100" in m["content"] for m in why["supersedes"])

    tl = json.loads(srv.engraphis_timeline(query="rate limit", workspace="acme", repo="web"))
    assert len(tl["history"]) == 2


def test_recall_proactive_tool(monkeypatch):
    srv = _module_with_memory_db(monkeypatch)
    srv.engraphis_remember(content="High importance convention.", workspace="acme", repo="web",
                           importance=0.9)
    started = json.loads(srv.engraphis_start_session(workspace="acme", repo="web"))
    assert started["bootstrap"] == {}
    srv.engraphis_end_session(session_id=started["session_id"], summary="mid-work",
                              open_threads=["thing left undone"])
    out = json.loads(srv.engraphis_recall_proactive(workspace="acme", repo="web"))
    assert out["memories"]
    assert out["last_session"]["open_threads"] == ["thing left undone"]

    # And the *next* start_session should bootstrap from that handoff.
    again = json.loads(srv.engraphis_start_session(workspace="acme", repo="web"))
    assert again["bootstrap"]["open_threads"] == ["thing left undone"]


def test_governance_tools_forget_pin_correct(monkeypatch):
    srv = _module_with_memory_db(monkeypatch)
    out = json.loads(srv.engraphis_remember(content="The API key header is X-Auth-Key.",
                                            workspace="acme"))
    pinned = json.loads(srv.engraphis_pin(memory_id=out["id"], workspace="acme"))
    assert pinned["pinned"] is True

    corrected = json.loads(srv.engraphis_correct(
        memory_id=out["id"], new_content="The API key header is X-Api-Key.",
        workspace="acme", reason="typo"))
    assert corrected["superseded"] == [out["id"]]

    forgotten = json.loads(srv.engraphis_forget(memory_id=corrected["id"], workspace="acme",
                                                reason="no longer needed"))
    assert forgotten["status"] == "forgotten"

    err = srv.engraphis_forget(memory_id="mem_does_not_exist", workspace="acme")
    assert err.startswith("Error:")


def test_governance_tools_reject_wrong_workspace(monkeypatch):
    srv = _module_with_memory_db(monkeypatch)
    out = json.loads(srv.engraphis_remember(content="Alpha's private fact.", workspace="alpha"))
    json.loads(srv.engraphis_remember(content="anchor", workspace="beta"))

    assert srv.engraphis_pin(memory_id=out["id"], workspace="beta").startswith("Error:")
    assert srv.engraphis_forget(memory_id=out["id"], workspace="beta").startswith("Error:")
    assert srv.engraphis_correct(memory_id=out["id"], new_content="tampered",
                                 workspace="beta").startswith("Error:")

    # untouched: still live under its real workspace
    r = json.loads(srv.engraphis_recall(query="private fact", workspace="alpha"))
    assert any(m["id"] == out["id"] for m in r["memories"])


def test_link_and_record_event_tools(monkeypatch):
    srv = _module_with_memory_db(monkeypatch)
    a = json.loads(srv.engraphis_remember(content="Memory A.", workspace="acme", repo="web"))
    b = json.loads(srv.engraphis_remember(content="Memory B.", workspace="acme", repo="web"))
    link = json.loads(srv.engraphis_link(a=a["id"], b=b["id"], workspace="acme", repo="web",
                                         relation="related"))
    assert link["linked"] is True

    ev = json.loads(srv.engraphis_record_event(
        kind="decision", content="Chose PASETO over JWT.", workspace="acme", repo="web"))
    assert ev["id"].startswith("evt_")


def test_link_tool_rejects_wrong_workspace(monkeypatch):
    srv = _module_with_memory_db(monkeypatch)
    a = json.loads(srv.engraphis_remember(content="Alpha's fact.", workspace="alpha"))
    b = json.loads(srv.engraphis_remember(content="Beta's fact.", workspace="beta"))
    err = srv.engraphis_link(a=a["id"], b=b["id"], workspace="alpha")
    assert err.startswith("Error:")


def test_index_repo_and_search_code_tools(monkeypatch, tmp_path):
    srv = _module_with_memory_db(monkeypatch)
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    report = json.loads(srv.engraphis_index_repo(
        workspace="acme", repo="sample", root_path=str(tmp_path)))
    assert report["files_indexed"] == 1

    out = json.loads(srv.engraphis_search_code(query="add", workspace="acme", repo="sample"))
    assert any(s["name"] == "add" for s in out["symbols"])
