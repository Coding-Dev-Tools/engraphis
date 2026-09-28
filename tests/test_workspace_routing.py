"""Project routing is durable, caller-owned, explicit, and checked before writes."""
import asyncio
import json

import pytest

from engraphis.service import MemoryService, ValidationError, set_current_user


@pytest.fixture
def svc():
    set_current_user(None)
    service = MemoryService.create(":memory:")
    yield service
    set_current_user(None)
    service.close()


def _user(name):
    set_current_user({"id": "usr_" + name, "email": name + "@example.test", "role": "member"})


def _snapshot(svc):
    return {
        table: [tuple(row) for row in svc.store.conn.execute("SELECT * FROM " + table)]
        for table in ("workspaces", "repos", "sessions", "memories", "operation_receipts")
    }


def _approved(svc, content, **kwargs):
    out = svc.remember(content, **kwargs)
    svc.engine.approve_for_prompt(out["id"], reviewer="test", reason="routing fixture")
    return out


def test_project_mapping_survives_service_reopen(tmp_path):
    path = str(tmp_path / "routing.db")
    first = MemoryService.create(path)
    first.create_workspace("client-acme")
    first.set_workspace_routing("client-acme", repo=" web ")
    first.close()
    second = MemoryService.create(path)
    try:
        assert second.get_workspace_routing("web") == {
            "repo": "web", "workspace": "client-acme", "configured": True, "source": "project",
        }
        started = second.start_session(repo="web", agent="another-client")
        assert started["workspace"] == "client-acme"
        assert started["workspace_source"] == "project"
        written = second.remember("Acme project fact.", repo="web")
        assert written["workspace"] == "client-acme"
        assert written["workspace_source"] == "project"
    finally:
        second.close()


def test_explicit_workspace_beats_mapping_and_default_is_a_real_choice(svc):
    svc.create_workspace("acme")
    svc.set_workspace_routing("acme", repo="web")
    started = svc.start_session("default", repo="web")
    assert (started["workspace"], started["workspace_source"]) == ("default", "explicit")
    written = svc.remember("A deliberate default fact.", workspace="default", repo="web")
    assert (written["workspace"], written["workspace_source"]) == ("default", "explicit")


def test_session_inheritance_beats_changed_mapping(svc):
    started = svc.start_session("acme", repo="web")
    svc.create_workspace("next-project")
    svc.set_workspace_routing("next-project", repo="web")
    written = svc.remember("Session's project fact.", session_id=started["session_id"])
    assert (written["workspace"], written["repo"], written["workspace_source"]) == (
        "acme", "web", "session",
    )
    assert written["scope"] == "repo"
    assert svc.store.get_memory(written["id"]).session_id == started["session_id"]


@pytest.mark.parametrize("kwargs", [
    {"workspace": "new-wrong-workspace"},
    {"repo": "new-wrong-repo"},
    {"workspace": "acme", "repo": "new-wrong-repo"},
])
def test_session_mismatches_create_no_rows(svc, kwargs):
    started = svc.start_session("acme", repo="web")
    before = _snapshot(svc)
    with pytest.raises(ValidationError, match="does not belong"):
        svc.remember("Must never be stored.", session_id=started["session_id"], **kwargs)
    assert _snapshot(svc) == before


def test_workspace_only_session_rejects_supplied_repo_without_creating_it(svc):
    started = svc.start_session("acme")
    before = _snapshot(svc)
    with pytest.raises(ValidationError, match="does not belong"):
        svc.remember("Wrong project.", session_id=started["session_id"], repo="web")
    assert _snapshot(svc) == before


def test_closed_or_unknown_session_never_falls_back(svc):
    started = svc.start_session("acme", repo="web")
    svc.end_session(started["session_id"])
    before = _snapshot(svc)
    for session_id in (started["session_id"], "ses_missing"):
        with pytest.raises(ValidationError):
            svc.remember("Must not fall back.", session_id=session_id)
    assert _snapshot(svc) == before


def test_mapping_removal_preserves_unrelated_settings(svc):
    svc.create_workspace("acme")
    wid = svc._lookup_workspace("acme")
    rid = svc.store.get_or_create_repo(wid, "web", settings={"custom": {"keep": True}})
    svc.set_workspace_routing("acme", repo="web")
    assert svc.set_workspace_routing("acme", repo="web", enabled=False) == {
        "repo": "web", "workspace": None, "configured": False, "source": "default",
    }
    settings = svc.store.conn.execute("SELECT settings FROM repos WHERE id=?", (rid,)).fetchone()
    assert json.loads(settings["settings"]) == {"custom": {"keep": True}}
    assert svc.start_session(repo="web")["workspace"] == "default"


def test_changed_mapping_is_audited_and_identical_retries_are_noops(svc):
    svc.create_workspace("acme")
    svc.set_workspace_routing("acme", repo="web")
    before = tuple(svc.store.conn.iterdump())
    svc.set_workspace_routing("acme", repo="web")
    assert tuple(svc.store.conn.iterdump()) == before
    svc.set_workspace_routing("acme", repo="web", enabled=False)
    before = tuple(svc.store.conn.iterdump())
    svc.set_workspace_routing("acme", repo="web", enabled=False)
    assert tuple(svc.store.conn.iterdump()) == before
    rows = list(svc.store.conn.execute("SELECT actor, detail FROM audit WHERE action='workspace_routing'"))
    assert [(row["actor"], row["detail"]) for row in rows] == [
        ("local", "project destination saved"), ("local", "project destination removed"),
    ]


def test_copy_export_and_sync_never_duplicate_personal_routing(svc):
    from engraphis.core.sync import SyncEngine

    svc.create_workspace("acme")
    svc.set_workspace_routing("acme", repo="web")
    _user("alice")
    svc.set_workspace_routing("acme", repo="web")
    set_current_user(None)
    svc.copy_workspace("acme", "copied")
    assert svc.get_workspace_routing("web")["workspace"] == "acme"
    copied_settings = svc.store.conn.execute(
        "SELECT settings FROM repos WHERE workspace_id=?", (svc._lookup_workspace("copied"),),
    ).fetchone()["settings"]
    assert "workspace_routing" not in json.loads(copied_settings)
    exported = svc.export_workspace(workspace="acme")
    assert all("workspace_routing" not in json.loads(repo["settings"]) for repo in exported["repos"])
    sync_bundle = SyncEngine(svc.store).export_bundle(svc._lookup_workspace("acme"))
    assert "workspace_routing" not in json.dumps(sync_bundle)
    assert "usr_alice" not in json.dumps(sync_bundle)


def test_whole_workspace_merge_keeps_each_principals_project_choice(svc):
    svc.create_workspace("source")
    svc.create_workspace("target")
    rid = svc.store.get_or_create_repo(svc._lookup_workspace("target"), "web", settings={"keep": 1})
    _user("alice")
    svc.set_workspace_routing("source", repo="web")
    _user("bob")
    svc.set_workspace_routing("target", repo="web")
    set_current_user(None)
    svc.merge_workspaces("source", "target")
    for principal in ("alice", "bob"):
        _user(principal)
        assert svc.get_workspace_routing("web")["workspace"] == "target"
    settings = json.loads(svc.store.conn.execute("SELECT settings FROM repos WHERE id=?", (rid,)).fetchone()[0])
    assert settings["keep"] == 1


def test_retarget_updates_only_callers_mapping(svc):
    svc.create_workspace("one")
    svc.create_workspace("two")
    _user("alice")
    svc.set_workspace_routing("one", repo="web")
    _user("bob")
    assert svc.get_workspace_routing("web")["configured"] is False
    svc.set_workspace_routing("one", repo="web")
    _user("alice")
    svc.set_workspace_routing("two", repo="web")
    assert svc.get_workspace_routing("web")["workspace"] == "two"
    _user("bob")
    assert svc.get_workspace_routing("web")["workspace"] == "one"
    set_current_user(None)
    assert svc.get_workspace_routing("web")["configured"] is False


def test_another_users_session_cannot_route_writes_or_reads(svc):
    svc.create_workspace("shared")
    _user("alice")
    started = svc.start_session("shared", repo="web")
    _user("bob")
    before = _snapshot(svc)
    with pytest.raises(ValidationError, match="another user"):
        svc.remember("Unauthorized session.", session_id=started["session_id"])
    with pytest.raises(ValidationError, match="another user"):
        svc.recall("private fact", session_id=started["session_id"])
    assert _snapshot(svc) == before


def test_inaccessible_saved_mapping_does_not_fall_back_or_get_overwritten(svc):
    svc.create_workspace("one")
    svc.create_workspace("two")
    svc.set_workspace_routing("one", repo="web")
    bound = MemoryService(svc.engine, allowed_workspaces=["two", "default"])
    before = _snapshot(svc)
    for operation in (
        lambda: bound.get_workspace_routing("web"),
        lambda: bound.start_session(repo="web"),
        lambda: bound.remember("Denied routing.", repo="web"),
        lambda: bound.set_workspace_routing("two", repo="web"),
    ):
        with pytest.raises(ValidationError):
            operation()
    assert _snapshot(svc) == before
    # A deliberate explicit workspace does not consult an irrelevant saved mapping.
    assert bound.start_session("two", repo="web")["workspace_source"] == "explicit"


def test_ambiguous_mapping_fails_until_explicitly_replaced(svc):
    for name in ("one", "two"):
        svc.create_workspace(name)
        svc.store.get_or_create_repo(svc._lookup_workspace(name), "web", settings={
            "workspace_routing": {"local": True},
        })
    before = _snapshot(svc)
    with pytest.raises(ValidationError, match="ambiguous"):
        svc.remember("Do not guess between projects.", repo="web")
    assert _snapshot(svc) == before
    assert svc.set_workspace_routing("two", repo="web")["workspace"] == "two"


@pytest.mark.parametrize("repo", ["", "   "])
def test_saving_empty_project_is_rejected_before_creating_rows(svc, repo):
    svc.create_workspace("acme")
    before = _snapshot(svc)
    with pytest.raises(ValidationError):
        svc.set_workspace_routing("acme", repo=repo)
    assert _snapshot(svc) == before


def test_recall_mapping_session_and_explicit_workspace_stay_isolated(svc):
    acme = _approved(svc, "The falcon build uses pnpm.", workspace="acme", repo="web")
    other = _approved(svc, "The falcon build uses npm.", workspace="other", repo="web")
    svc.set_workspace_routing("acme", repo="web")
    mapped = svc.recall("falcon build", repo="web")
    assert {item["id"] for item in mapped["memories"]} == {acme["id"]}
    explicit = svc.recall("falcon build", workspace="other", repo="web")
    assert {item["id"] for item in explicit["memories"]} == {other["id"]}
    session = svc.start_session("other", repo="web")
    inherited = svc.recall("falcon build", session_id=session["session_id"])
    assert {item["id"] for item in inherited["memories"]} == {other["id"]}
    grounded = svc.grounded_recall("falcon build", session_id=session["session_id"])
    assert {item["id"] for item in grounded["citations"]} <= {other["id"]}


def test_unmapped_repo_reads_default_but_no_context_local_read_remains_broad(svc):
    _approved(svc, "Falcon compiler policy.", workspace="other", repo="web")
    assert svc.recall("falcon", repo="web")["count"] == 0
    assert svc.recall("falcon")["count"] == 1
    assert svc.start_session()["workspace_source"] == "default"
    assert svc.remember("Context-free fallback.")["workspace"] == "default"


def test_http_routing_and_session_write_share_the_service_resolver(svc, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from engraphis.routes import v2_api

    monkeypatch.setattr(v2_api, "service", lambda: svc)
    app = FastAPI()
    app.include_router(v2_api.router)
    svc.create_workspace("acme")
    with TestClient(app) as client:
        assert client.get("/api/workspace-routing", params={"repo": "web"}).json() == {
            "repo": "web", "workspace": None, "configured": False, "source": "default",
        }
        saved = client.post("/api/workspace-routing", json={"workspace": "acme", "repo": "web"})
        assert saved.status_code == 200
        assert saved.json()["workspace"] == "acme"
        rejected = client.post("/api/workspace-routing", json={"workspace": "acme", "repo": ""})
        assert rejected.status_code == 422
        started = svc.start_session(repo="web")
        written = client.post("/api/remember", json={
            "content": "HTTP session inheritance.", "session_id": started["session_id"],
        })
        assert written.status_code == 200
        assert written.json()["workspace"] == "acme"
        assert written.json()["workspace_source"] == "session"


def test_smart_mcp_protocol_and_discovery_route_without_exposing_extra_tools(svc, monkeypatch):
    pytest.importorskip("mcp")
    from engraphis import mcp_server as server

    monkeypatch.setattr(server, "_service", svc)
    svc.create_workspace("acme")
    found = json.loads(server.engraphis_discover_actions("save project workspace routing"))
    action = found["actions"][0]
    saved = server.engraphis_execute_action(
        action["capability_id"], action["schema_digest"], {"workspace": "acme", "repo": "web"},
    )
    assert json.loads(saved)["result"]["workspace"] == "acme"
    response = asyncio.run(server.smart_mcp.call_tool("engraphis_session", {
        "repo": "web", "goal": "Build falcon", "token_budget": 64,
    }))
    blocks = response[0] if isinstance(response, tuple) else response
    started = json.loads(blocks[0].text)
    assert started["workspace"] == "acme"
    assert started["workspace_source"] == "project"
    written = json.loads(server.smart_remember("MCP session inheritance.", session_id=started["session_id"]))
    assert written["workspace"] == "acme"
    assert written["workspace_source"] == "session"
    listed = json.loads(server.engraphis_list_workspaces())
    assert [item["name"] for item in listed["workspaces"]] == ["acme"]


@pytest.mark.parametrize("keyword_fallback", [False, True])
def test_http_recall_context_respects_project_and_session_boundaries(svc, monkeypatch, keyword_fallback):
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from engraphis.routes import v2_api

    monkeypatch.setattr(v2_api, "service", lambda: svc)
    monkeypatch.setattr(v2_api, "_default_ws", lambda: "other")
    project = _approved(svc, "Falcon build project policy.", workspace="acme", repo="web")
    _approved(svc, "Falcon build unrelated project.", workspace="acme", repo="backend")
    legacy = _approved(svc, "Falcon build dashboard default.", workspace="other")
    session = svc.start_session("acme", repo="web")
    private = _approved(svc, "Falcon build session detail.", session_id=session["session_id"],
                        scope="session")
    other_session = svc.start_session("acme", repo="web", force_new=True)
    _approved(svc, "Falcon build another session.", session_id=other_session["session_id"],
              scope="session")
    svc.set_workspace_routing("acme", repo="web")
    if keyword_fallback:
        def mismatch(*args, **kwargs):
            raise ValueError("shapes not aligned")
        monkeypatch.setattr(svc, "recall", mismatch)
    app = FastAPI()
    app.include_router(v2_api.router)
    with TestClient(app) as client:
        mapped = client.get("/api/recall", params={"q": "Falcon", "repo": "web"})
        assert mapped.status_code == 200
        assert mapped.json()["workspace_source"] == "project"
        assert {item["id"] for item in mapped.json()["memories"]} == {project["id"]}
        inherited = client.get("/api/recall", params={"q": "Falcon", "session_id": session["session_id"]})
        assert inherited.status_code == 200
        assert inherited.json()["workspace_source"] == "session"
        assert {item["id"] for item in inherited.json()["memories"]} == {project["id"], private["id"]}
        context_free = client.get("/api/recall", params={"q": "Falcon"})
        assert {item["id"] for item in context_free.json()["memories"]} == {legacy["id"]}
        mismatch = client.get("/api/recall", params={
            "q": "Falcon", "workspace": "other", "session_id": session["session_id"],
        })
        assert mismatch.status_code == 400
        if not keyword_fallback:
            intent = client.post("/api/intent/recall", json={
                "query": "Falcon", "session_id": session["session_id"],
            })
            assert intent.status_code == 200
            assert intent.json()["workspace_source"] == "session"
            assert {item["id"] for item in intent.json()["memories"]} == {project["id"], private["id"]}


def test_intent_writes_and_grounded_answers_inherit_routing(svc, monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from engraphis.routes import v2_api

    monkeypatch.setattr(v2_api, "service", lambda: svc)
    monkeypatch.setattr(v2_api, "_default_ws", lambda: "other")
    fact = _approved(svc, "The falcon build uses pnpm.", workspace="acme", repo="web")
    _approved(svc, "The falcon build uses npm.", workspace="other", repo="web")
    svc.set_workspace_routing("acme", repo="web")
    session = svc.start_session(repo="web")["session_id"]
    app = FastAPI()
    app.include_router(v2_api.router)
    with TestClient(app) as client:
        saved = client.post("/api/intent/remember", json={"text": "Use staged deploys.", "session_id": session})
        assert saved.status_code == 200
        assert saved.json()["workspace"] == "acme" and saved.json()["repo"] == "web"
        for context in ({"repo": "web"}, {"session_id": session}):
            answer = client.post("/api/answer", json={"query": "The falcon build uses pnpm.", **context})
            assert answer.status_code == 200
            assert {item["id"] for item in answer.json()["citations"]} == {fact["id"]}
        for route, payload in (("/api/answer", {"query": "falcon"}),
                               ("/api/intent/remember", {"text": "Do not misroute."})):
            rejected = client.post(route, json={**payload, "workspace": "other", "session_id": session})
            assert rejected.status_code == 400


@pytest.mark.parametrize("with_session", [False, True])
def test_keyword_fallback_keeps_legacy_user_ancestors(svc, monkeypatch, with_session):
    pytest.importorskip("fastapi")
    from engraphis.routes import v2_api

    fact = _approved(svc, "Falcon legacy preference.", workspace="acme")
    svc.store.conn.execute("UPDATE memories SET scope='user' WHERE id=?", (fact["id"],))
    svc.store.conn.commit()
    session = svc.start_session("acme", repo="web")["session_id"] if with_session else None
    svc.store.get_or_create_repo(svc._lookup_workspace("acme"), "web")
    monkeypatch.setattr(v2_api, "service", lambda: svc)
    out = v2_api._keyword_search("acme", "Falcon", 8, repo="web", session_id=session)
    assert fact["id"] in {item["id"] for item in out}
