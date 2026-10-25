"""Record history retains promoted ancestors without widening caller access."""
import time

import pytest

from engraphis.service import MemoryService, ValidationError, set_current_user


def test_why_and_timeline_find_old_facts_beyond_the_first_page():
    from engraphis.core.engine import MemoryEngine
    from engraphis.core.interfaces import MemoryRecord, Scope

    engine = MemoryEngine.create(":memory:", auto_evolve=False)
    try:
        workspace = engine.store.get_or_create_workspace("history")
        repo = engine.store.get_or_create_repo(workspace, "api")
        old = engine.store.add_memory(MemoryRecord(
            id="", content="Zephyr database migration chooses PostgreSQL.",
            workspace_id=workspace, repo_id=repo, scope=Scope.REPO,
            ingested_at=1.0, valid_from=1.0,
        ))
        for index in range(501):
            engine.store.add_memory(MemoryRecord(
                id="", content="Unrelated lighthouse observation %s." % index,
                workspace_id=workspace, repo_id=repo, scope=Scope.REPO,
                ingested_at=100.0 + index, valid_from=1.0,
            ))
        query = "Zephyr database migration"
        assert [row.id for row in engine.timeline(
            query, workspace_id=workspace, repo_id=repo,
        )] == [old]
        assert [row.id for row in engine.why(
            query, workspace_id=workspace, repo_id=repo,
        )["answer"]] == [old]
    finally:
        engine.close()


@pytest.mark.parametrize("helper", ["why", "timeline"])
def test_public_history_exhausts_keyset_pages_without_a_recency_cap(helper):
    from engraphis.core.engine import MemoryEngine
    from engraphis.core.interfaces import MemoryRecord, Scope

    engine = MemoryEngine.create(":memory:", auto_evolve=False)
    try:
        workspace = engine.store.get_or_create_workspace("history")
        for index in range(501):
            engine.store.add_memory(MemoryRecord(
                id="", content="Unrelated lighthouse observation %s." % index,
                workspace_id=workspace, scope=Scope.WORKSPACE,
                ingested_at=100.0 + index, valid_from=1.0,
                provenance={"trusted": True, "review_state": "approved"},
            ))
        # Insert last but backdate ingestion: this is beyond both the first ID page
        # and the recency window. Either accidental 500-record cap loses the result.
        old = engine.store.add_memory(MemoryRecord(
            id="", content="Zephyr database migration chooses PostgreSQL.",
            workspace_id=workspace, scope=Scope.WORKSPACE,
            ingested_at=1.0, valid_from=1.0,
            provenance={"trusted": True, "review_state": "approved"},
        ))
        service = MemoryService(engine)
        output = getattr(service, helper)("Zephyr database migration", workspace="history")
        records = output["answer" if helper == "why" else "history"]
        assert [row["id"] for row in records] == [old]
    finally:
        engine.close()


@pytest.mark.parametrize("helper", ["why", "timeline"])
def test_history_helpers_use_a_bounded_candidate_window(helper, monkeypatch):
    """History inspection must not hydrate every memory row on each call.

    The candidate window is the union of the indexed lexical arm and the semantic
    arm's ranked hits, so the previous unbounded keyset page loop is gone.
    """
    from engraphis.core import store as store_module
    from engraphis.core.engine import MemoryEngine
    from engraphis.core.interfaces import MemoryRecord, Scope

    engine = MemoryEngine.create(":memory:", auto_evolve=False)
    try:
        workspace = engine.store.get_or_create_workspace("history")
        for index in range(1200):
            engine.store.add_memory(MemoryRecord(
                id="", content="Unrelated lighthouse observation %s." % index,
                workspace_id=workspace, scope=Scope.WORKSPACE,
                ingested_at=100.0 + index, valid_from=1.0,
            ))
        seen_budgets = []
        original = engine.store.list_history_candidates

        def spy(*args, **kwargs):
            seen_budgets.append(kwargs.get("budget"))
            return original(*args, **kwargs)

        def forbidden(*args, **kwargs):
            raise AssertionError("history must not scan every memory row")

        monkeypatch.setattr(engine.store, "list_history_candidates", spy)
        monkeypatch.setattr(engine.store, "list_memories_page", forbidden)
        getattr(engine, helper)("lighthouse observation", workspace_id=workspace)
        assert seen_budgets
        assert set(seen_budgets) == {store_module.DEFAULT_HISTORY_CANDIDATE_BUDGET}
    finally:
        engine.close()


def test_why_finds_a_superseded_record_beyond_the_recency_window():
    """The lexical arm reaches a closed record that sits past the newest-N window."""
    from engraphis.core.engine import MemoryEngine
    from engraphis.core.interfaces import MemoryRecord, Scope

    engine = MemoryEngine.create(":memory:", auto_evolve=False)
    try:
        workspace = engine.store.get_or_create_workspace("history")
        superseded = engine.store.add_memory(MemoryRecord(
            id="", content="Zephyr database migration chooses PostgreSQL.",
            workspace_id=workspace, scope=Scope.WORKSPACE,
            ingested_at=1.0, valid_from=1.0, valid_to=2.0,
        ))
        for index in range(800):
            engine.store.add_memory(MemoryRecord(
                id="", content="Unrelated lighthouse observation %s." % index,
                workspace_id=workspace, scope=Scope.WORKSPACE,
                ingested_at=100.0 + index, valid_from=1.0,
            ))
        live = engine.store.add_memory(MemoryRecord(
            id="", content="Zephyr database migration chooses PostgreSQL.",
            workspace_id=workspace, scope=Scope.WORKSPACE,
            ingested_at=900.0, valid_from=2.0,
        ))
        output = engine.why("Zephyr database migration", workspace_id=workspace)
        assert [row.id for row in output["answer"]] == [live]
        assert superseded in {row.id for row in output["supersedes"]}
    finally:
        engine.close()


@pytest.mark.parametrize("helper", ["why", "timeline"])
def test_history_never_silently_empties_when_the_lexical_index_fails(
    helper, monkeypatch,
):
    """A broken lexical index degrades to one bounded page instead of no results."""
    import sqlite3

    from engraphis.core.engine import MemoryEngine
    from engraphis.core.interfaces import MemoryRecord, Scope

    engine = MemoryEngine.create(":memory:", auto_evolve=False)
    try:
        workspace = engine.store.get_or_create_workspace("history")
        record = engine.store.add_memory(MemoryRecord(
            id="", content="Zephyr database migration chooses PostgreSQL.",
            workspace_id=workspace, scope=Scope.WORKSPACE,
            ingested_at=1.0, valid_from=1.0,
        ))

        def broken(*args, **kwargs):
            raise sqlite3.OperationalError("fts index unavailable")

        monkeypatch.setattr(engine.store, "_lexical_candidate_ids", broken)
        output = getattr(engine, helper)("Zephyr database migration", workspace_id=workspace)
        rows = output["answer"] if helper == "why" else output
        assert [row.id for row in rows] == [record]
    finally:
        engine.close()


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
