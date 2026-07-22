"""Behavioral regression coverage for MCP idempotency annotations.

The hints describe the whole public tool, including optional branches and append-only
audit/receipt effects—not just whether the primary row reaches the same logical value.
"""
import asyncio
import json
import re

import pytest

pytest.importorskip("mcp", reason="optional 'mcp' extra not installed")

import engraphis.mcp_server as srv
from engraphis.core.interfaces import SchemaSnapshot
from engraphis.service import MemoryService


def _memory_server(monkeypatch):
    monkeypatch.setattr(srv, "_service", MemoryService.create(":memory:"))
    return srv


def _annotations(tool_name):
    tools = {tool.name: tool for tool in asyncio.run(srv.mcp.list_tools())}
    return tools[tool_name].annotations


def _database_dump(server):
    return tuple(server.service().store.conn.iterdump())


def _count(server, table, *, where="", params=()):
    query = f"SELECT COUNT(*) AS n FROM {table}"  # noqa: S608 - test-owned identifiers only
    if where:
        query += f" WHERE {where}"
    return server.service().store.conn.execute(query, params).fetchone()["n"]


def test_session_hints_cover_force_new_and_idempotent_end(monkeypatch):
    server = _memory_server(monkeypatch)

    first = json.loads(server.engraphis_start_session(
        workspace="acme", repo="api", agent="codex", goal="branch", force_new=True,
    ))
    second = json.loads(server.engraphis_start_session(
        workspace="acme", repo="api", agent="codex", goal="branch", force_new=True,
    ))
    assert first["session_id"] != second["session_id"]
    assert _count(server, "sessions") == 2
    start = _annotations("engraphis_start_session")
    assert start.readOnlyHint is False
    assert start.idempotentHint is False

    reusable = json.loads(server.engraphis_start_session(
        workspace="acme", repo="api", agent="codex", goal="retry-safe",
    ))
    after_start = _database_dump(server)
    retry = json.loads(server.engraphis_start_session(
        workspace="acme", repo="api", agent="codex", goal="retry-safe",
    ))
    assert retry["session_id"] == reusable["session_id"]
    assert retry["reused"] is True
    assert _database_dump(server) == after_start

    server.engraphis_end_session(
        session_id=reusable["session_id"], summary="done", outcome="shipped",
    )
    after_end = _database_dump(server)
    repeated = server.engraphis_end_session(
        session_id=reusable["session_id"], summary="done", outcome="shipped",
    )
    assert not repeated.startswith("Error:"), repeated
    assert _database_dump(server) == after_end
    end = _annotations("engraphis_end_session")
    assert end.readOnlyHint is False
    assert end.idempotentHint is True


def test_pin_and_forget_retries_append_audit_rows(monkeypatch):
    server = _memory_server(monkeypatch)
    stored = json.loads(server.engraphis_remember(
        content="Keep the deployment runbook.", workspace="acme",
    ))

    server.engraphis_pin(memory_id=stored["id"], workspace="acme", pinned=True)
    assert _count(
        server, "audit", where="target=? AND action='pin'", params=(stored["id"],),
    ) == 1
    server.engraphis_pin(memory_id=stored["id"], workspace="acme", pinned=True)
    assert _count(
        server, "audit", where="target=? AND action='pin'", params=(stored["id"],),
    ) == 2
    pin = _annotations("engraphis_pin")
    assert pin.readOnlyHint is False
    assert pin.idempotentHint is False

    server.engraphis_forget(memory_id=stored["id"], workspace="acme", reason="retired")
    assert _count(
        server, "audit", where="target=? AND action='invalidate'", params=(stored["id"],),
    ) == 1
    server.engraphis_forget(memory_id=stored["id"], workspace="acme", reason="retired")
    assert _count(
        server, "audit", where="target=? AND action='invalidate'", params=(stored["id"],),
    ) == 2
    forget = _annotations("engraphis_forget")
    assert forget.readOnlyHint is False
    assert forget.idempotentHint is False


def test_index_repo_receipt_makes_public_tool_non_idempotent(monkeypatch, tmp_path):
    server = _memory_server(monkeypatch)
    (tmp_path / "app.py").write_text("def hello():\n    return 'hi'\n", encoding="utf-8")
    kwargs = {"workspace": "acme", "repo": "api", "root_path": str(tmp_path)}

    first = json.loads(server.engraphis_index_repo(**kwargs))
    assert first["files_indexed"] == 1
    assert _count(server, "operation_receipts", where="operation='index_repo'") == 1
    second = json.loads(server.engraphis_index_repo(**kwargs))
    assert second["files_indexed"] == 0
    assert second["files_unchanged"] == 1
    assert _count(server, "operation_receipts", where="operation='index_repo'") == 2

    annotations = _annotations("engraphis_index_repo")
    assert annotations.readOnlyHint is False
    assert annotations.idempotentHint is False


def test_postgres_ingest_stores_a_new_snapshot_and_receipts_each_call(monkeypatch):
    from engraphis.backends import postgres_schema

    server = _memory_server(monkeypatch)
    snapshot = SchemaSnapshot(
        title="PostgreSQL schema: appdb",
        text="# PostgreSQL schema: appdb\n\n## public.users\n\n- `id`: integer",
        entities=[
            {"id": "database:appdb", "name": "appdb", "kind": "database"},
            {"id": "table:public.users", "name": "public.users", "kind": "table"},
        ],
        relations=[{
            "source": "database:appdb",
            "target": "table:public.users",
            "relation": "contains",
        }],
        metadata={"database": "appdb", "tables": 1, "source_digest": "digest"},
    )

    class _Introspector:
        def inspect(self, supplied, *, schemas=None):
            assert supplied == "postgresql://local/appdb"
            return snapshot

    monkeypatch.setattr(
        postgres_schema, "get_postgres_introspector", lambda: _Introspector(),
    )
    kwargs = {"dsn": "postgresql://local/appdb", "workspace": "acme"}
    first = json.loads(server.engraphis_ingest_postgres_schema(**kwargs))
    first_receipts = _count(server, "operation_receipts")
    second = json.loads(server.engraphis_ingest_postgres_schema(**kwargs))
    assert first["id"] != second["id"]
    assert _count(server, "memories") == 2
    assert _count(server, "operation_receipts") > first_receipts

    annotations = _annotations("engraphis_ingest_postgres_schema")
    assert annotations.readOnlyHint is False
    assert annotations.idempotentHint is False


def test_forced_update_check_rewrites_persistent_cache(monkeypatch, tmp_path):
    from engraphis import update_check

    cache = tmp_path / "update-check.json"
    clock = iter((1000.0, 1001.0, 1002.0, 1003.0))
    monkeypatch.setenv("ENGRAPHIS_UPDATE_CHECK", "1")
    monkeypatch.setenv("ENGRAPHIS_UPDATE_CACHE", str(cache))
    monkeypatch.setattr(update_check.time, "time", lambda: next(clock))
    monkeypatch.setattr(
        update_check,
        "_fetch",
        lambda url, timeout: {"version": "9.9.9", "url": "https://example.test/release"},
    )

    first = json.loads(srv.engraphis_check_update(force=True))
    first_cache = cache.read_text(encoding="utf-8")
    second = json.loads(srv.engraphis_check_update(force=True))
    second_cache = cache.read_text(encoding="utf-8")
    assert first["checked_at"] < second["checked_at"]
    assert first_cache != second_cache

    annotations = _annotations("engraphis_check_update")
    assert annotations.readOnlyHint is False
    assert annotations.idempotentHint is False
    assert annotations.openWorldHint is True


def test_consolidate_dry_run_is_pure_and_default_live_retry_is_stable(monkeypatch):
    server = _memory_server(monkeypatch)
    for run in (101, 202, 303):
        server.engraphis_remember(
            content=f"Build failed on the flaky network integration test in CI run {run}.",
            workspace="acme",
            repo="api",
            mtype="episodic",
            dedupe=False,
        )

    before_dry_run = _database_dump(server)
    dry_run = json.loads(server.engraphis_consolidate(
        workspace="acme", repo="api", dry_run=True,
    ))
    assert dry_run["digests_created"]
    assert _database_dump(server) == before_dry_run

    live = json.loads(server.engraphis_consolidate(
        workspace="acme", repo="api", dry_run=False,
    ))
    assert live["digests_created"]
    after_live = _database_dump(server)
    retry = json.loads(server.engraphis_consolidate(
        workspace="acme", repo="api", dry_run=False,
    ))
    assert retry["digests_created"] == []
    assert _database_dump(server) == after_live

    annotations = _annotations("engraphis_consolidate")
    assert annotations.readOnlyHint is False


def test_structured_consolidate_can_process_remaining_sources_on_retry(monkeypatch):
    from engraphis.llm import client as llm_client

    class _PartialStructuredLLM:
        def extract_json(self, prompt, schema):
            source_ids = re.findall(r"ID: (mem_[A-Z0-9]+)", prompt)
            return {
                "subject": "flaky network integration test",
                "facts": [{
                    "content": "The network integration test is repeatedly flaky.",
                    "title": "Recurring flaky integration test",
                    "confidence": 0.9,
                    "source_ids": source_ids[:2],
                }],
            }

        def close(self):
            return None

    monkeypatch.setattr(llm_client, "LLMClient", _PartialStructuredLLM)
    server = _memory_server(monkeypatch)
    for run in (101, 202, 303, 404, 505, 606):
        server.engraphis_remember(
            content=f"Build failed on the flaky network integration test in CI run {run}.",
            workspace="acme",
            repo="api",
            mtype="episodic",
            dedupe=False,
        )

    first = json.loads(server.engraphis_consolidate(
        workspace="acme", repo="api", dry_run=False, structured=True,
    ))
    assert first["digests_created"]
    after_first = _database_dump(server)
    second = json.loads(server.engraphis_consolidate(
        workspace="acme", repo="api", dry_run=False, structured=True,
    ))
    assert second["digests_created"]
    assert _database_dump(server) != after_first

    annotations = _annotations("engraphis_consolidate")
    assert annotations.readOnlyHint is False
    assert annotations.idempotentHint is False
