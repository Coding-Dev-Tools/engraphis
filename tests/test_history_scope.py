"""Record history retains promoted ancestors without widening caller access."""
import time

import pytest

from engraphis.service import MemoryService, ValidationError, set_current_user


@pytest.fixture
def svc(tmp_path):
    service = MemoryService.create(
        str(tmp_path / "history.db"), embed_model="", vector_backend="numpy", extractor="none",
    )
    service.create_workspace("w", visibility="shared", confirmed=True)
    try:
        yield service
    finally:
        set_current_user(None)
        service.close()


def promoted_lineage(svc):
    pending = svc.remember(
        "Services preserve structured diagnostic events.", workspace="w", repo="api",
        source="web", trusted=False,
    )["id"]
    approved = svc.engine.approve_for_prompt(
        pending, reviewer="test-owner", reason="approved disposable fixture",
    )["id"]
    time.sleep(0.002)
    promoted = svc.promote(
        approved, "workspace", workspace="w", repo="api", reason="shared convention",
    )["id"]
    return [pending, approved, promoted]


@pytest.mark.parametrize("use_repo_id", [False, True])
def test_repo_history_pages_through_workspace_promotion(svc, use_repo_id):
    identities = promoted_lineage(svc)
    root = identities[1]
    repo = svc.store.get_memory(root).repo_id if use_repo_id else "api"
    assert svc.store.get_memory(identities[-1]).repo_id is None
    assert {row["id"] for row in svc.inspect(root, workspace="w", repo=repo)["chain"]} == set(identities)

    seen, cursor, anchors = [], "", None
    while True:
        page = svc.memory_history(root, workspace="w", repo=repo, limit=1, cursor=cursor)
        assert page["total_count"] == len(identities)
        assert page["count"] == len(page["versions"]) == 1
        if anchors is None:
            anchors = (page["valid_at"], page["known_at"])
        assert (page["valid_at"], page["known_at"]) == anchors
        seen.extend(row["id"] for row in page["versions"])
        svc.store.audit("test", "unrelated", "", "")
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert seen == identities
    earlier = svc.memory_history(
        root, workspace="w", repo=repo,
        known_at=svc.store.get_memory(root).ingested_at,
    )
    assert [row["id"] for row in earlier["versions"]] == identities[:-1]
    assert earlier["total_count"] == len(identities) - 1


def test_promoted_history_keeps_repository_workspace_and_session_boundaries(svc):
    identities = promoted_lineage(svc)
    root = identities[1]
    sibling = svc.remember(
        "Sibling repository lineage claim.", workspace="w", repo="web",
        metadata={"corrects": root}, resolve_conflicts=False,
    )["id"]
    foreign = svc.remember(
        "Foreign workspace lineage claim.", workspace="other",
        metadata={"corrects": root}, resolve_conflicts=False,
    )["id"]
    private = {}
    for user in ("alice", "bob"):
        set_current_user({"id": "usr_" + user, "email": user + "@example.test", "role": "member"})
        session = svc.start_session("w", repo="api", goal=user + " private history")
        private[user] = svc.remember(
            user + " private lineage claim.", workspace="w", repo="api",
            scope="session", session_id=session["session_id"],
            metadata={"corrects": root}, resolve_conflicts=False,
        )["id"]

    for user, other in (("alice", "bob"), ("bob", "alice")):
        set_current_user({"id": "usr_" + user, "email": user + "@example.test", "role": "member"})
        page = svc.memory_history(root, workspace="w", repo="api")
        returned = {row["id"] for row in page["versions"]}
        assert returned == set(identities) | {private[user]}
        assert not returned.intersection({sibling, foreign, private[other]})
        assert page["count"] == page["total_count"] == len(returned)
        assert page["next_cursor"] is None
        with pytest.raises(ValidationError, match="another user"):
            svc.memory_history(private[other], workspace="w", repo="api")
        with pytest.raises(ValidationError, match="does not belong"):
            svc.memory_history(root, workspace="w", repo="web")


@pytest.mark.parametrize("ancestor_scope", ["workspace", "user"])
def test_legacy_broader_scope_is_an_ancestor_even_with_a_stored_repo_id(svc, ancestor_scope):
    identities = promoted_lineage(svc)
    sibling = svc.remember("Sibling repository anchor.", workspace="w", repo="web")["id"]
    sibling_repo = svc.store.get_memory(sibling).repo_id
    # Imported legacy ancestors may retain an obsolete repo id; normal writes
    # reject this combination. Canonical ancestor visibility follows their scope.
    svc.store.conn.execute(
        "UPDATE memories SET scope=?, repo_id=? WHERE id=?",
        (ancestor_scope, sibling_repo, identities[-1]),
    )
    svc.store.conn.commit()
    page = svc.memory_history(identities[1], workspace="w", repo="api")
    assert [row["id"] for row in page["versions"]] == identities
    assert page["count"] == page["total_count"] == len(identities)


@pytest.mark.parametrize("root_index", [1, 2])
def test_rest_repo_history_returns_the_promoted_successor_on_its_next_page(svc, monkeypatch, root_index):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from engraphis.routes import v2_api

    identities = promoted_lineage(svc)
    monkeypatch.setattr(v2_api, "service", lambda: svc)
    app = FastAPI()
    app.include_router(v2_api.router)
    with TestClient(app) as client:
        url = f"/api/memory/{identities[root_index]}/history"
        params = {"workspace": "w", "repo": "api", "limit": 2}
        response = client.get(url, params=params)
        assert response.status_code == 200, response.text
        first = response.json()
        assert first["count"] == 2 and first["total_count"] == 3
        response = client.get(url, params={**params, "cursor": first["next_cursor"]})
        assert response.status_code == 200, response.text
        last = response.json()
        assert [row["id"] for row in first["versions"] + last["versions"]] == identities
        assert last["count"] == 1 and last["total_count"] == 3
        assert last["next_cursor"] is None
        assert last["versions"][0]["scope"] == "workspace"


@pytest.mark.parametrize("scope", ["workspace", "user"])
def test_project_history_can_open_broader_root_without_widening_governance(svc, scope):
    identities = promoted_lineage(svc)
    root = identities[-1]
    sibling = svc.remember(
        "Sibling repository lineage claim.", workspace="w", repo="web",
        metadata={"corrects": root}, resolve_conflicts=False,
    )["id"]
    foreign = svc.remember(
        "Foreign workspace lineage claim.", workspace="other",
        metadata={"corrects": root}, resolve_conflicts=False,
    )["id"]
    if scope == "user":
        # Imported broader roots may retain an obsolete repository id. Read
        # eligibility follows scope; governance still verifies exact ownership.
        svc.store.conn.execute(
            "UPDATE memories SET scope=?, repo_id=? WHERE id=?",
            (scope, svc.store.get_memory(sibling).repo_id, root),
        )
        svc.store.conn.commit()
    for repo in ("api", svc.store.get_memory(identities[0]).repo_id):
        first = svc.memory_history(root, workspace="w", repo=repo, limit=1)
        seen, page = [], first
        while True:
            seen.extend(row["id"] for row in page["versions"])
            assert page["total_count"] == len(identities)
            if not page["next_cursor"]:
                break
            page = svc.memory_history(
                root, workspace="w", repo=repo, limit=1, cursor=page["next_cursor"],
            )
        assert seen == identities
    assert {row["id"] for row in svc.memory_history(root, workspace="w")["versions"]} == set(identities) | {sibling}
    for rejected in (sibling, foreign):
        with pytest.raises(ValidationError, match="does not belong"):
            svc.memory_history(rejected, workspace="w", repo="api")
    with pytest.raises(ValidationError, match="does not belong"):
        svc.correct(root, "A project cannot edit a broader memory as its own.", workspace="w", repo="api")
