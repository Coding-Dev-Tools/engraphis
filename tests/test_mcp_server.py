"""Smoke test for the MCP binding. Skips cleanly when the optional 'mcp' package
is not installed, so the offline CI gate is unaffected."""
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp", reason="optional 'mcp' extra not installed")


ROOT = Path(__file__).resolve().parents[1]


def test_stdio_server_default_log_level_is_quiet():
    from engraphis.mcp_server import mcp
    assert mcp.settings.log_level == "WARNING"


def test_unexpected_tool_failure_does_not_leak_exception_text():
    from engraphis.mcp_server import _err
    output = _err(RuntimeError("token=SECRET C:/private/customer.db"))
    assert output.startswith("Error:")
    assert "SECRET" not in output and "private" not in output


def _module_with_memory_db(monkeypatch):
    import engraphis.mcp_server as srv
    from engraphis.service import MemoryService
    # Back the global service with an in-memory db so tests never touch real storage.
    monkeypatch.setattr(srv, "_service", MemoryService.create(":memory:"))
    return srv


def _recall_side_effect_snapshot(srv):
    """State covered by recall's reinforcement, receipt, and event side effects."""
    conn = srv.service().store.conn
    memories = conn.execute(
        "SELECT id, access_count, stability, last_access FROM memories ORDER BY id"
    ).fetchall()
    return {
        "memories": tuple(
            (row["id"], row["access_count"], row["stability"], row["last_access"])
            for row in memories
        ),
        "receipts": conn.execute(
            "SELECT COUNT(*) AS n FROM operation_receipts"
        ).fetchone()["n"],
        "events": conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"],
    }


_ALL_TOOLS = {
    "engraphis_remember", "engraphis_recall", "engraphis_why", "engraphis_timeline",
    "engraphis_recall_proactive", "engraphis_forget", "engraphis_pin", "engraphis_correct",
    "engraphis_promote", "engraphis_link", "engraphis_record_event", "engraphis_index_repo",
    "engraphis_search_code", "engraphis_code_path", "engraphis_code_impact",
    "engraphis_export_code_graph", "engraphis_start_session", "engraphis_end_session",
    "engraphis_stats", "engraphis_proactive_context", "engraphis_recall_grounded",
    "engraphis_answer", "engraphis_ingest", "engraphis_consolidate",
    "engraphis_ingest_postgres_schema",
    "engraphis_receipts", "engraphis_verify_receipts", "engraphis_export_receipts",
    "engraphis_check_update",
}


def test_server_identity_and_tools_registered():
    import asyncio

    import engraphis.mcp_server as srv
    assert srv.mcp.name == "engraphis_mcp"
    assert srv.mcp.instructions == srv._SESSION_PROTOCOL
    assert "engraphis_recall_proactive" in srv.mcp.instructions
    assert "operator-configured\nworkspace" in srv.mcp.instructions
    assert "engraphis_start_session" in srv.mcp.instructions
    assert "engraphis_end_session" in srv.mcp.instructions
    assert "open_threads=[]" in srv.mcp.instructions
    tools = {t.name: t for t in asyncio.run(srv.mcp.list_tools())}
    assert len(_ALL_TOOLS) == 29
    assert set(tools) == _ALL_TOOLS
    kilo = (ROOT / "docs" / "KILO_CODE_INTEGRATION.md").read_text(encoding="utf-8")
    full_surface = kilo.split("## 4. The 29 tools", 1)[1].split("\n---", 1)[0]
    assert set(re.findall(r"`(engraphis_[a-z_]+)`", full_surface)) == _ALL_TOOLS
    # Flat schema (not a nested "params" object) so agents can call fields directly.
    props = tools["engraphis_remember"].inputSchema.get("properties", {})
    assert "content" in props and "workspace" in props and "params" not in props


def test_mcp_server_module_entrypoint_runs_stdio_handshake():
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "entrypoint-test", "version": "1"},
        },
    }) + "\n"

    result = subprocess.run(
        [sys.executable, "-m", "engraphis.mcp_server"],
        cwd=ROOT,
        input=payload,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    response = json.loads(result.stdout)
    assert response["id"] == 1
    assert response["result"]["serverInfo"]["name"] == "engraphis_mcp"


@pytest.mark.parametrize(
    ("tool_name", "kwargs", "memory_changes", "receipt_changes"),
    [
        (
            "engraphis_recall",
            {"query": "Which tokens authenticate the API?", "workspace": "acme", "repo": "api"},
            True,
            True,
        ),
        (
            "engraphis_recall_grounded",
            {
                "query": "Which tokens authenticate the API?",
                "workspace": "acme",
                "repo": "api",
                "min_support": 0.0,
            },
            True,
            True,
        ),
        (
            "engraphis_answer",
            {
                "query": "Which tokens authenticate the API?",
                "workspace": "acme",
                "repo": "api",
                "min_support": 0.0,
            },
            True,
            True,
        ),
        (
            "engraphis_proactive_context",
            {
                "workspace": "acme",
                "repo": "api",
                "task": "Check which tokens authenticate the API",
            },
            False,
            True,
        ),
        (
            "engraphis_recall_proactive",
            {"workspace": "acme", "repo": "api"},
            False,
            False,
        ),
    ],
)
def test_retrieval_annotations_match_observed_state_mutation(
        monkeypatch, tool_name, kwargs, memory_changes, receipt_changes):
    """MCP hosts must not auto-approve stateful retrieval based on false hints."""
    import asyncio

    srv = _module_with_memory_db(monkeypatch)
    stored = json.loads(srv.engraphis_remember(
        content="The API uses PASETO tokens for authentication.",
        workspace="acme",
        repo="api",
        importance=0.9,
    ))
    assert stored["stored"] is True

    before = _recall_side_effect_snapshot(srv)
    result = getattr(srv, tool_name)(**kwargs)
    assert not result.startswith("Error:"), result
    json.loads(result)
    after = _recall_side_effect_snapshot(srv)
    observed_changes = {
        key: after[key] != before[key]
        for key in ("memories", "receipts", "events")
    }
    assert observed_changes == {
        "memories": memory_changes,
        "receipts": receipt_changes,
        "events": False,
    }
    observed_mutation = any(observed_changes.values())

    tools = {tool.name: tool for tool in asyncio.run(srv.mcp.list_tools())}
    annotations = tools[tool_name].annotations
    assert annotations.readOnlyHint is (not observed_mutation)
    assert annotations.idempotentHint is (not observed_mutation)


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


def test_remember_session_id_keeps_repo_default_scope(monkeypatch):
    srv = _module_with_memory_db(monkeypatch)
    session = json.loads(srv.engraphis_start_session(
        workspace="acme", repo="web", force_new=True
    ))

    stored = json.loads(srv.engraphis_remember(
        content="Durable repo fact learned during this session.",
        workspace="acme", repo="web", session_id=session["session_id"],
    ))

    assert stored["scope"] == "repo"


def test_grounded_recall_tool_returns_flat_answer_payload(monkeypatch):
    srv = _module_with_memory_db(monkeypatch)
    srv.engraphis_remember(
        content="The API uses PASETO tokens for authentication.", workspace="acme", repo="api")
    out = json.loads(srv.engraphis_recall_grounded(
        query="Which auth tokens does the API use?", workspace="acme", repo="api",
        min_support=0.0))
    assert out["query"] == "Which auth tokens does the API use?"
    assert out["grounded"] is True
    assert out["abstained"] is False
    assert "PASETO" in out["answer"]
    assert out["citations"]

    alias = json.loads(srv.engraphis_answer(
        query="Which auth tokens does the API use?", workspace="acme", repo="api",
        min_support=0.0))
    assert alias["grounded"] is True
    assert "PASETO" in alias["answer"]


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


def test_promote_tool_widens_scope(monkeypatch):
    srv = _module_with_memory_db(monkeypatch)
    source = json.loads(srv.engraphis_remember(
        content="All services use structured logs.", workspace="acme", repo="api"
    ))

    promoted = json.loads(srv.engraphis_promote(
        memory_id=source["id"], target_scope="workspace",
        workspace="acme", repo="api", reason="confirmed across repos",
    ))

    assert promoted["scope"] == "workspace"
    assert promoted["promoted_from"] == source["id"]
    assert srv.service().store.get_memory(source["id"]).valid_to is not None


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
                                         relation="related", reason="same subsystem"))
    assert link["linked"] is True
    assert link["reason"] == "same subsystem"
    assert srv.service().store.get_links(a["id"])[0]["reason"] == "same subsystem"

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

    path = json.loads(srv.engraphis_code_path(
        source="calc.py", target="add", workspace="acme", repo="sample",
    ))
    assert path["found"] is True
    impact = json.loads(srv.engraphis_code_impact(
        changed_files=["calc.py"], workspace="acme", repo="sample",
    ))
    assert impact["metrics"]["symbols_touched"] >= 1
    exported = json.loads(srv.engraphis_export_code_graph(
        workspace="acme", repo="sample",
    ))
    assert exported["graph"]["format"] == "engraphis-code-graph/1"
    assert "# Engraphis Code Graph Report" in exported["report_markdown"]


def test_receipt_tools(monkeypatch):
    srv = _module_with_memory_db(monkeypatch)
    srv.engraphis_remember(
        content="Receipts cover this write.", workspace="acme", scope="workspace"
    )
    listed = json.loads(srv.engraphis_receipts(workspace="acme"))
    assert listed["entries"][0]["operation"] == "remember"
    verified = json.loads(srv.engraphis_verify_receipts(workspace="acme"))
    assert verified["valid"] is True
    exported = json.loads(srv.engraphis_export_receipts(workspace="acme"))
    assert exported["verification"]["valid"] is True
