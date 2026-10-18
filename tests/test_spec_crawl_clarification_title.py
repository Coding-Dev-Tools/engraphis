"""Approved clarifications do not recreate resolved ambiguity in generated titles."""
from __future__ import annotations

import pytest

from engraphis.core.poisoning import prompt_eligible
from engraphis.service import MemoryService


@pytest.fixture
def service():
    instance = MemoryService.create(":memory:", extractor="none", graph_extractor="none")
    try:
        yield instance
    finally:
        instance.close()


def _selection(service, scope):
    if scope == "workspace":
        return {"workspace": "selected"}
    if scope == "repo":
        wid = service.store.get_or_create_workspace("selected")
        service.store.get_or_create_repo(wid, "app")
        return {"workspace": "selected", "repo": "app"}
    session = service.start_session(
        "selected", repo="app" if scope == "repo_session" else None, agent="reviewer",
    )
    return {"session_id": session["session_id"]}


@pytest.mark.parametrize("scope", ["workspace", "repo", "repo_session", "workspace_session"])
@pytest.mark.parametrize("text,token,answer,resolved", [
    ("Verify all tests.", "all", "12", "Verify 12 tests"),
    ("Review every batch.", "every", "the release", "Review the release batch"),
    ("Verify everything.", "everything", "the release manifest", "Verify the release manifest"),
    ("Publish results soon.", "soon", "within 5 minutes", "Publish results within 5 minutes"),
    ("Respond quickly.", "quickly", "within 5 seconds", "Respond within 5 seconds"),
    ("Archive relevant logs.", "relevant", "the deployment", "Archive the deployment logs"),
])
def test_approved_resolved_sentence_has_no_generated_title_flags(
    service, scope, text, token, answer, resolved,
):
    selection = _selection(service, scope)
    specification = "# Rules\n" + text
    flag = next(flag for flag in service.spec_crawl(
        specification, trace_claims=False, **selection,
    )["flags"] if flag["kind"] == "vague" and flag["token"] == token)
    response = service.spec_crawl_answer(
        flag, answer, spec_text=specification, save_as_memory=True, **selection,
    )
    pending = service.store.get_memory(response["memory_id"])
    assert pending.content == resolved
    assert pending.mtype.value == "procedural"
    assert pending.provenance["source"] == "spec_crawl"
    assert pending.provenance["trusted"] is False
    assert pending.provenance["review_state"] == "pending"
    assert pending.metadata["flag_id"] == flag["id"]
    assert not prompt_eligible(pending.provenance, pending.metadata)
    assert service.spec_crawl_memories(**selection)["nodes"] == []

    approved = service.engine.approve_for_prompt(
        pending.id, reviewer="owner", reason="Verified the explicit release condition",
    )
    current = service.store.get_memory(approved["id"])
    assert current.content == resolved
    assert current.metadata["approved_from"] == pending.id
    assert current.provenance["approved_from"] == pending.id
    assert service.store.get_memory(current.metadata["approved_from"]).metadata["flag_id"] == flag["id"]
    assert current.title == pending.title
    assert (current.workspace_id, current.repo_id, current.session_id, current.scope) == (
        pending.workspace_id, pending.repo_id, pending.session_id, pending.scope,
    )
    assert prompt_eligible(current.provenance, current.metadata)
    report = service.spec_crawl_memories(**selection)
    assert {node["id"] for node in report["nodes"]} == {current.id}
    assert report["flags"] == []
    assert report["flags_total"] == report["nodes"][0]["flag_count"] == 0
    assert report["score_breakdown"]["quality_points"] == 30
    assert report["score_breakdown"]["vague_penalty"] == 0
    assert current.title == "Spec Clarification"
    assert pending.scope.value == ("session" if "session" in scope else scope)
    assert bool(pending.repo_id) is (scope in {"repo", "repo_session"})

    if "session_id" in selection:
        other = service.start_session(
            "selected", repo="app" if scope == "repo_session" else None,
            agent="other", force_new=True,
        )["session_id"]
        assert service.spec_crawl_memories(session_id=other)["nodes"] == []
        assert service.spec_crawl_memories(workspace="selected")["nodes"] == []
    elif scope == "repo":
        service.start_session("selected", repo="other", agent="other")
        assert service.spec_crawl_memories(workspace="selected", repo="other")["nodes"] == []
    service.start_session("elsewhere", repo="app", agent="other")
    assert service.spec_crawl_memories(workspace="elsewhere")["nodes"] == []


@pytest.mark.parametrize("scope", ["workspace", "repo", "repo_session", "workspace_session"])
def test_neutral_title_does_not_hide_an_unresolved_answer(service, scope):
    selection = _selection(service, scope)
    text = "# Rules\nVerify all tests."
    flag = next(flag for flag in service.spec_crawl(text, trace_claims=False, **selection)["flags"]
                if flag["token"] == "all")
    response = service.spec_crawl_answer(
        flag, "everything", spec_text=text, save_as_memory=True, **selection,
    )
    pending = service.store.get_memory(response["memory_id"])
    assert pending.content == "Verify everything tests"
    assert pending.metadata["flag_id"] == flag["id"]
    approved = service.engine.approve_for_prompt(
        pending.id, reviewer="owner", reason="Preserve the actual supplied wording",
    )
    report = service.spec_crawl_memories(**selection)
    assert report["nodes"][0]["id"] == approved["id"]
    assert [flag["token"] for flag in report["flags"] if flag.get("field") != "title"] == ["everything"]
    assert not [flag for flag in report["flags"] if flag.get("field") == "title"]
    assert report["score_breakdown"]["quality_points"] == 0
    assert report["score_breakdown"]["vague_penalty"] < 0


def test_default_clarification_is_read_only_and_user_title_ambiguity_remains_visible(service):
    specification = "# Rules\nVerify all tests."
    flag = service.spec_crawl(specification, trace_claims=False)["flags"][0]
    before = list(service.store.conn.iterdump())
    response = service.spec_crawl_answer(flag, "12", spec_text=specification)
    assert response["memory_id"] is None
    assert list(service.store.conn.iterdump()) == before
    memory = service.remember(
        "The release uses 12 tests.", title="Verify everything", workspace="selected",
        trusted=True, resolve_conflicts=False,
    )["id"]
    report = service.spec_crawl_memories(workspace="selected")
    assert [(flag["node_id"], flag["field"], flag["token"]) for flag in report["flags"]] == [
        (memory, "title", "everything"),
    ]
    assert report["score_breakdown"]["quality_points"] == 0
