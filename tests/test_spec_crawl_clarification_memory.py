"""Clarification persistence resolves the selected sentence without granting trust."""
from __future__ import annotations

import json

import pytest

from engraphis.core.poisoning import prompt_eligible
from engraphis.service import MemoryService


@pytest.fixture(params=["service", "http", "mcp"])
def clarification_transport(request, monkeypatch):
    if request.param == "http":
        pytest.importorskip("fastapi", reason="HTTP clarification transport needs the server extra")
        pytest.importorskip("httpx", reason="HTTP clarification transport needs the test client")
    elif request.param == "mcp":
        pytest.importorskip("mcp", reason="MCP clarification transport needs the optional MCP extra")
    svc = MemoryService.create(":memory:")
    client = None
    if request.param == "http":
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from engraphis.routes import v2_api

        monkeypatch.setattr(v2_api, "_service", svc)
        app = FastAPI()
        app.include_router(v2_api.router)
        client = TestClient(app)

        def answer(**values):
            response = client.post("/api/spec/crawl/answer", json=values)
            assert response.status_code == 200, response.text
            return response.json()
    elif request.param == "mcp":
        from engraphis import mcp_server

        monkeypatch.setattr(mcp_server, "service", lambda: svc)

        def answer(**values):
            return json.loads(mcp_server.engraphis_spec_crawl_answer(**values))
    else:
        answer = svc.spec_crawl_answer
    try:
        yield svc, answer
    finally:
        if client is not None:
            client.close()
        svc.close()


@pytest.mark.parametrize(("text", "answer", "flag_index", "sentence"), [
    ("# Rules\nVerify all tests.", "12", 0, "Verify 12 tests"),
    ("# Rules\nVerify all tests. Archive logs.", "12", 0, "Verify 12 tests"),
    ("# Rules\nVerify all tests. Verify all tests.", "12", 1, "Verify 12 tests"),
    ("# Rules\nVerify all tests.", "all 12", 0, "Verify all 12 tests"),
    ("# Rules\nVerify all tests using `all` as the fixture.", "12", 0,
     "Verify 12 tests using `all` as the fixture"),
    ("# Rules\nVerify all tests using `all`.", "12", 0,
     "Verify 12 tests using `all`"),
    ("# Rules\nVerify `first? every; path` all tests.", "12", 0,
     "Verify `first? every; path` 12 tests"),
    ("# Rules\n`all` tests must all pass.", "12", 0, "`all` tests must 12 pass"),
    ("# Rules\nVerify `line?\nall; second` all tests.", "12", 0,
     "Verify `line?\nall; second` 12 tests"),
    ("# Rules\nVerify " + "release evidence " * 15 + "for all tests.", "12", 0,
     "Verify " + "release evidence " * 15 + "for 12 tests"),
    ("# Rules\r\nRe\u0301view logs. Verify all tests.\r\nArchive results.", "12", 0,
     "Verify 12 tests"),
])
def test_saved_clarification_contains_the_full_resolved_selected_sentence(
    clarification_transport, text, answer, flag_index, sentence,
):
    svc, respond = clarification_transport
    sid = svc.start_session("alpha", repo="app", agent="codex")["session_id"]
    flags = svc.spec_crawl(text, session_id=sid, trace_claims=False)["flags"]
    vague = [flag for flag in flags if flag["kind"] == "vague"]
    result = respond(flag=vague[flag_index], answer=answer, spec_text=text,
                     session_id=sid, save_as_memory=True)
    record = svc.store.get_memory(result["memory_id"])
    assert record.content == sentence
    assert record.mtype.value == "procedural"
    assert record.scope.value == "session" and record.session_id == sid
    assert record.provenance["source"] == "spec_crawl"
    assert record.provenance["review_state"] == "pending"
    assert record.metadata["flag_id"] == vague[flag_index]["id"]
    assert not prompt_eligible(record.provenance, record.metadata)
    # Pending evidence cannot silently become a procedural instruction.
    assert svc.spec_crawl_memories(session_id=sid)["nodes"] == []
    approved = svc.engine.approve_for_prompt(
        record.id, reviewer="owner", reason="Verified the requested test count",
    )
    successor = svc.store.get_memory(approved["id"])
    assert successor.content == sentence
    assert successor.session_id == sid and successor.repo_id == record.repo_id
    assert prompt_eligible(successor.provenance, successor.metadata)
    procedural = svc.spec_crawl(crawl_procedural=True, session_id=sid, trace_claims=False)
    assert not [flag for flag in procedural["flags"] if flag["kind"] == "vague"]
    other = svc.start_session("alpha", repo="app", agent="other", force_new=True)["session_id"]
    assert approved["id"] not in {node["id"] for node in svc.spec_crawl_memories(session_id=other)["nodes"]}


def test_clarification_default_remains_read_only_and_unresolved_answers_are_not_erased(
    clarification_transport,
):
    svc, respond = clarification_transport
    text = "# Rules\nVerify all tests."
    flag = svc.spec_crawl(text, trace_claims=False)["flags"][0]
    before = svc.store.conn.total_changes
    result = respond(flag=flag, answer="12", spec_text=text)
    assert result["memory_id"] is None
    assert svc.store.conn.total_changes == before
    # A human can supply wording that still needs clarification. Persist that actual
    # wording, rather than deleting vague tokens or fabricating a completed decision.
    result = respond(flag=flag, answer="everything", spec_text=text, save_as_memory=True)
    record = svc.store.get_memory(result["memory_id"])
    assert record.content == "Verify everything tests"
    assert any(item["token"] == "everything" for item in result["new_report"]["flags"])
    assert not prompt_eligible(record.provenance, record.metadata)
