import re

import pytest

pytest.importorskip("fastapi")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from engraphis.ai_context import build_proactive_context  # noqa: E402
from engraphis.core.interfaces import MemoryRecord, Scope  # noqa: E402
from engraphis.routes import v2_api  # noqa: E402
from engraphis.service import MemoryService, ValidationError  # noqa: E402


class _CitingLLM:
    def chat(self, messages, system=None, **kw):
        assert "SOURCES" in messages[0]["content"]
        return "- Use the SQLite storage convention [1].\n- Follow up on migration notes [1]."


class _UncitedLLM:
    def chat(self, messages, system=None, **kw):
        return "Use SQLite."

class _InvalidCitationLLM:
    def chat(self, messages, system=None, **kw):
        return "- Use the unrelated guidance [99]."



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

    invalid = build_proactive_context(task="implement persistence", memories=memories,
                                      last_session={}, llm=_InvalidCitationLLM(),
                                      synthesize=True)
    assert invalid["synthesized"] is False
    assert invalid["context_summary"] == uncited["context_summary"]




def test_service_proactive_context_is_deterministic_and_cited():
    svc = MemoryService.create(":memory:", embed_model="")
    pending = svc.remember("Engraphis stores local memories in SQLite.", workspace="acme",
                           scope="workspace", title="Storage backend", importance=0.8)
    svc.engine.approve_for_prompt(pending["id"], reviewer="test", reason="approved fixture")
    out = svc.proactive_context(workspace="acme", task="work on persistence", k=5)
    assert out["workspace"] == "acme"
    assert out["grounded"] is True
    assert out["synthesized"] is False
    assert "[1]" in out["context_summary"]
    assert out["citations"][0]["id"]
    assert any("Storage backend" in q or "persistence" in q for q in out["suggested_queries"])
    repeat = svc.proactive_context(workspace="acme", task="work on persistence", k=5)
    assert repeat["context_summary"] == out["context_summary"]
    assert [citation["id"] for citation in repeat["citations"]] == [
        citation["id"] for citation in out["citations"]
    ]
    assert repeat["suggested_queries"] == out["suggested_queries"]


def test_proactive_context_excludes_closed_and_cross_workspace_memories():
    svc = MemoryService.create(":memory:", embed_model="")
    historical = svc.remember(
        "The retired billing target was legacy.example.",
        workspace="acme", scope="workspace", title="Retired billing target", importance=0.9,
    )
    foreign = svc.remember(
        "The other workspace billing target is other.example.",
        workspace="other", scope="workspace", title="Foreign billing target", importance=0.9,
    )
    approved_historical = svc.engine.approve_for_prompt(
        historical["id"], reviewer="test", reason="approved fixture",
    )
    approved_foreign = svc.engine.approve_for_prompt(
        foreign["id"], reviewer="test", reason="approved fixture",
    )
    svc.store.close_validity(approved_historical["id"], reason="historical fixture")

    out = svc.proactive_context(workspace="acme", k=5)
    citation_ids = {citation["id"] for citation in out["citations"]}
    assert approved_historical["id"] not in citation_ids
    assert approved_foreign["id"] not in citation_ids




def test_service_proactive_context_logs_recall_failure_without_exception_text(
    monkeypatch, caplog
):
    svc = MemoryService.create(":memory:", embed_model="")
    svc.store.get_or_create_workspace("acme")

    def fail_recall(*args, **kwargs):
        raise RuntimeError("credential-like provider detail")

    monkeypatch.setattr(svc, "recall", fail_recall)
    with caplog.at_level("WARNING", logger="engraphis.service"):
        out = svc.proactive_context(workspace="acme", task="resume work", k=5)

    assert out["workspace"] == "acme"
    assert "RuntimeError" in caplog.text
    assert "acme" not in caplog.text
    assert "credential-like provider detail" not in caplog.text


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
    pending = svc.remember("Use PASETO for auth tokens.", workspace="acme",
                           scope="workspace", title="Auth convention", importance=0.9)
    svc.engine.approve_for_prompt(pending["id"], reviewer="test", reason="approved fixture")
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


def test_recall_proactive_honors_pinned_and_proactive_flags():
    svc = MemoryService.create(":memory:", embed_model="")
    wid = svc.store.get_or_create_workspace("acme")
    pinned = svc.remember("The deployment target is fly.io.", workspace="acme",
                          scope="workspace", importance=0.1)
    always = svc.remember("The auth convention is PASETO.", workspace="acme",
                          scope="workspace", importance=0.1,
                          metadata={"proactive": "always"})
    never = svc.remember("Internal debug log noise.", workspace="acme",
                         scope="workspace", importance=0.9,
                         metadata={"proactive": "never"})
    svc.engine.store.set_pinned(pinned["id"], True)
    approved = {}
    for mid in (pinned["id"], always["id"], never["id"]):
        res = svc.engine.approve_for_prompt(mid, reviewer="test", reason="approved fixture")
        approved[mid] = res["id"]

    out = svc.engine.recall_proactive(workspace_id=wid, k=10, prompt_only=True)
    ids = {m.id for m in out["memories"]}
    # Approval creates an approved successor (the pending original is not prompt-visible).
    # The successor inherits pinning/proactive flags, so it is the one recalled.
    assert approved[pinned["id"]] in ids       # pinned low-importance still surfaces
    assert approved[always["id"]] in ids       # proactive=always surfaces
    assert approved[never["id"]] not in ids    # proactive=never is excluded


def test_old_pinned_and_always_memories_are_not_lost_behind_proactive_scan_window():
    svc = MemoryService.create(":memory:", embed_model="")
    wid = svc.store.get_or_create_workspace("acme")
    rid = svc.store.get_or_create_repo(wid, "api")
    old_pinned = svc.store.add_memory(MemoryRecord(
        id="mem_old_pin", content="Old pinned context", workspace_id=wid,
        scope=Scope.WORKSPACE, pinned=True, ingested_at=1.0,
        provenance={"trusted": True, "review_state": "approved"},
    ))
    old_always = svc.store.add_memory(MemoryRecord(
        id="mem_old_always", content="Old always context", workspace_id=wid,
        repo_id=rid, scope=Scope.REPO, ingested_at=1.5,
        metadata={"proactive": "always"},
        provenance={"trusted": True, "review_state": "approved"},
    ))
    for index in range(501):
        svc.store.add_memory(MemoryRecord(
            id=f"mem_new_{index}", content=f"New context {index}",
            workspace_id=wid, repo_id=rid, scope=Scope.REPO,
            ingested_at=2.0 + index,
            provenance={"trusted": True, "review_state": "approved"},
        ))

    out = svc.engine.recall_proactive(
        workspace_id=wid, repo_id=rid, k=10, prompt_only=True,
    )
    ids = [memory.id for memory in out["memories"]]
    assert old_pinned in ids
    assert old_always in ids
    assert len(ids) == len(set(ids)) == 10
    assert ids == [
        memory.id for memory in svc.engine.recall_proactive(
            workspace_id=wid, repo_id=rid, k=10, prompt_only=True,
        )["memories"]
    ]

def test_compact_proactive_context_is_bounded_and_does_not_repeat_source_bodies():
    svc = MemoryService.create(":memory:", embed_model="")
    pending = svc.remember(
        "The authorization middleware uses PASETO tokens with a 15 minute lifetime.",
        workspace="acme", scope="workspace", title="Auth convention", importance=0.9,
    )
    svc.engine.approve_for_prompt(pending["id"], reviewer="test", reason="approved fixture")

    out = svc.proactive_context(
        workspace="acme", task="update authorization middleware", k=5,
        response_mode="compact", token_budget=32,
    )

    counter = svc.engine.recall_engine.context_packer.count_tokens
    assert set(out) == {"workspace", "repo", "context", "sources", "usage", "grounded", "reason"}
    assert counter(out["context"]) <= 32
    assert out["sources"][0]["id"].startswith("mem_")
    assert "content" not in out["sources"][0]
    assert "suggested_memories" not in out
    assert out["usage"]["budget_tokens"] == 32
    receipt = next(item for item in svc.receipt_log(workspace="acme")["entries"]
                   if item["operation"] == "proactive_context")
    assert receipt["metadata"]["response_mode"] == "compact"
    assert "PASETO" not in str(receipt)


@pytest.mark.parametrize("budget", range(8, 13))
def test_compact_proactive_context_never_emits_partial_citations(budget):
    svc = MemoryService.create(":memory:", embed_model="")
    pending = svc.remember(
        "The authorization middleware uses PASETO tokens with a 15 minute lifetime.",
        workspace="acme", scope="workspace", title="Auth convention", importance=0.9,
    )
    svc.engine.approve_for_prompt(pending["id"], reviewer="test", reason="approved fixture")

    out = svc.proactive_context(
        workspace="acme", task="update authorization middleware", k=5,
        response_mode="compact", token_budget=budget,
    )

    cited_numbers = {int(number) for number in re.findall(r"\[(\d+)\]", out["context"])}
    counter = svc.engine.recall_engine.context_packer.count_tokens
    assert counter(out["context"]) <= budget
    assert not re.search(r"\[(?:\d*)$", out["context"])
    assert {source["n"] for source in out["sources"]} == cited_numbers
    assert out["grounded"] is bool(cited_numbers)


def test_api_adaptive_context_routes_host_owned_history():
    svc = MemoryService.create(":memory:", embed_model="")
    svc.remember("The release manager approves deployment.", workspace="acme")
    v2_api.set_service(svc)
    app = FastAPI()
    app.include_router(v2_api.router)
    client = TestClient(app)

    response = client.post("/api/adaptive-context", json={
        "workspace": "acme",
        "query": "Who approves deployment?",
        "history": "The release manager approves deployment.",
        "max_context_tokens": 32,
    })

    assert response.status_code == 200
    body = response.json()
    assert body["context"] == "The release manager approves deployment."
    assert body["decision"]["mode"] == "history_bypass"
    assert body["sources"] == []
