import pytest

pytest.importorskip("fastapi")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from engraphis.ai_context import build_proactive_context  # noqa: E402
from engraphis.routes import v2_api  # noqa: E402
from engraphis.service import MemoryService, ValidationError  # noqa: E402


class _CitingLLM:
    def chat(self, messages, system=None, **kw):
        assert "SOURCES" in messages[0]["content"]
        return "- Use the SQLite storage convention [1].\n- Follow up on migration notes [1]."


class _UncitedLLM:
    def chat(self, messages, system=None, **kw):
        return "Use SQLite."


def test_ai_context_accepts_only_cited_llm_synthesis():
    memories = [{"id": "m1", "title": "Storage", "content": "Use SQLite for local storage."}]
    cited = build_proactive_context(task="implement persistence", memories=memories,
                                    last_session={}, llm=_CitingLLM(), synthesize=True)
    assert cited["synthesized"] is True
    assert "[1]" in cited["context_summary"]

    uncited = build_proactive_context(task="implement persistence", memories=memories,
                                      last_session={}, llm=_UncitedLLM(), synthesize=True)
    assert uncited["synthesized"] is False
    assert "[1]" in uncited["context_summary"]  # deterministic fallback preserves citations


def test_service_proactive_context_is_deterministic_and_cited():
    svc = MemoryService.create(":memory:", embed_model="")
    svc.remember("Engraphis stores local memories in SQLite.", workspace="acme",
                 scope="workspace", title="Storage backend", importance=0.8)
    out = svc.proactive_context(workspace="acme", task="work on persistence", k=5)
    assert out["workspace"] == "acme"
    assert out["grounded"] is True
    assert out["synthesized"] is False
    assert "[1]" in out["context_summary"]
    assert out["citations"][0]["id"]
    assert any("Storage backend" in q or "persistence" in q for q in out["suggested_queries"])


def test_ai_context_treats_string_open_threads_as_one_query():
    out = build_proactive_context(
        memories=[], last_session={"open_threads": "finish the migration"})
    assert out["suggested_queries"] == ["finish the migration"]
    assert "finish the migration" in out["context_summary"]


def test_service_proactive_context_bounds_agent_inputs():
    svc = MemoryService.create(":memory:", embed_model="")
    with pytest.raises(ValidationError, match="task exceeds"):
        svc.proactive_context(workspace="acme", task="x" * 10_001)
    with pytest.raises(ValidationError, match="agent_state exceeds"):
        svc.proactive_context(workspace="acme", agent_state="x" * 20_001)


def test_api_proactive_context_round_trip():
    svc = MemoryService.create(":memory:", embed_model="")
    svc.remember("Use PASETO for auth tokens.", workspace="acme",
                 scope="workspace", title="Auth convention", importance=0.9)
    v2_api.set_service(svc)
    app = FastAPI()
    app.include_router(v2_api.router)
    c = TestClient(app)

    r = c.post("/api/proactive-context", json={
        "workspace": "acme",
        "task": "change auth middleware",
        "k": 5,
    })
    assert r.status_code == 200
    data = r.json()
    assert data["workspace"] == "acme"
    assert data["grounded"] is True
    assert "context_summary" in data and "[1]" in data["context_summary"]
    assert data["citations"][0]["title"] == "Auth convention"
