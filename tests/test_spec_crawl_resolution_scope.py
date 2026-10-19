"""Read-visible ancestors do not grant broader mutation authority."""
from __future__ import annotations

import pytest

from engraphis.core.interfaces import Scope, SearchFilter
from engraphis.service import MemoryService, ValidationError


@pytest.fixture
def scoped_service(monkeypatch):
    service = MemoryService.create(":memory:", extractor="none", graph_extractor="none")
    monkeypatch.setattr(service.engine, "_evolve", lambda *args, **kwargs: None)
    wid = service.store.get_or_create_workspace("selected")
    repos = {name: service.store.get_or_create_repo(wid, name) for name in ("alpha", "beta")}
    sessions = {
        "alpha": service.store.start_session(wid, repos["alpha"]),
        "alpha_other": service.store.start_session(wid, repos["alpha"]),
        "beta": service.store.start_session(wid, repos["beta"]),
        "workspace": service.store.start_session(wid),
    }

    created = []

    def memory(scope, *, repo=None, session=None, timestamp=1000.0):
        created.append(scope)
        return service.engine.remember(
            f"Verify {29 + len(created)} scoped checks.", workspace_id=wid, repo_id=repos.get(repo),
            session_id=sessions.get(session), scope=Scope(scope),
            metadata={"provenance": {"trusted": True, "review_state": "approved"}},
            resolve_conflicts=False,
            valid_from=timestamp,
        )

    try:
        yield service, memory, sessions
    finally:
        service.close()


def _resolve(service, retained, retired, **scope):
    return service.spec_crawl_resolve(action="supersede", node_a=retained, node_b=retired,
                                      workspace="selected", confirmed=True, **scope)


@pytest.mark.parametrize("selected", ["workspace", "repo", "session"])
@pytest.mark.parametrize("broader", ["workspace", "repo"])
def test_narrow_policy_cannot_retire_read_visible_ancestor(scoped_service, selected, broader):
    service, memory, sessions = scoped_service
    if broader == "repo":
        retained = memory("session", repo="alpha", session="alpha", timestamp=2000)
        retired = memory("repo", repo="alpha")
        selector = {"session_id": sessions["alpha"]}
    else:
        retained = memory("repo", repo="alpha", timestamp=2000)
        retired = memory("workspace")
        selector = {"repo": "alpha"}
    if selected == "workspace":
        selector = {}
    elif selected == "session":
        selector = {"session_id": sessions["alpha"]}
    before = list(service.store.conn.iterdump())
    with pytest.raises(ValidationError):
        _resolve(service, retained, retired, **selector)
    assert list(service.store.conn.iterdump()) == before


@pytest.mark.parametrize("scope", ["workspace", "repo", "session"])
def test_same_scope_authorized_supersession(scoped_service, scope):
    service, memory, sessions = scoped_service
    details = {} if scope == "workspace" else {"repo": "alpha"}
    if scope == "session":
        details["session"] = "alpha"
    old = memory(scope, **details)
    current = memory(scope, timestamp=2000, **details)
    selector = {} if scope == "workspace" else {"repo": "alpha"}
    if scope == "session":
        selector["session_id"] = sessions["alpha"]
    result = _resolve(service, current, old, **selector)
    assert result["ok"]
    assert service.store.get_memory(old).valid_to == 2000
    assert service.store.get_memory(current).valid_to is None


@pytest.mark.parametrize("retained_scope,retired_scope", [
    ("workspace", "repo"), ("workspace", "session"), ("repo", "session"),
])
def test_broad_policy_can_supersede_selected_narrow_policy(scoped_service, retained_scope, retired_scope):
    service, memory, sessions = scoped_service
    retained = memory(retained_scope, repo="alpha" if retained_scope == "repo" else None,
                      timestamp=2000)
    retired = memory(retired_scope, repo="alpha",
                     session="alpha" if retired_scope == "session" else None)
    selector = {"repo": "alpha"}
    if retired_scope == "session":
        selector["session_id"] = sessions["alpha"]
    assert _resolve(service, retained, retired, **selector)["ok"]
    assert service.store.get_memory(retired).valid_to == 2000


@pytest.mark.parametrize("action", ["link", "supersede"])
def test_sibling_repositories_cannot_be_related_by_audit_remediation(scoped_service, action):
    service, memory, _ = scoped_service
    first = memory("repo", repo="alpha", timestamp=2000)
    second = memory("repo", repo="beta")
    before = list(service.store.conn.iterdump())
    with pytest.raises(ValidationError):
        service.spec_crawl_resolve(action=action, node_a=first, node_b=second,
                                  workspace="selected", confirmed=True)
    assert list(service.store.conn.iterdump()) == before


@pytest.mark.parametrize("selector", ["repo", "session"])
@pytest.mark.parametrize("action", ["link", "supersede"])
def test_narrow_request_cannot_mutate_only_broader_ancestors(scoped_service, selector, action):
    service, memory, sessions = scoped_service
    first, second = memory("workspace", timestamp=2000), memory("workspace")
    selected = {"repo": "alpha"} if selector == "repo" else {"session_id": sessions["alpha"]}
    before = list(service.store.conn.iterdump())
    with pytest.raises(ValidationError):
        service.spec_crawl_resolve(action=action, node_a=first, node_b=second,
                                  workspace="selected", confirmed=True, **selected)
    assert list(service.store.conn.iterdump()) == before


@pytest.mark.parametrize("action", ["link", "supersede"])
@pytest.mark.parametrize("selected", ["alpha", "alpha_other", "workspace"])
def test_sibling_session_endpoints_are_denied_without_changes(scoped_service, action, selected):
    service, memory, sessions = scoped_service
    first = memory("session", repo="alpha", session="alpha", timestamp=2000)
    second = memory("session", repo="alpha", session="alpha_other")
    before = list(service.store.conn.iterdump())
    with pytest.raises(ValidationError):
        service.spec_crawl_resolve(action=action, node_a=first, node_b=second,
                                  workspace="selected", session_id=sessions[selected], confirmed=True)
    assert list(service.store.conn.iterdump()) == before


@pytest.mark.parametrize("action", ["link", "supersede"])
def test_mismatched_session_repository_fails_closed(scoped_service, action):
    service, memory, sessions = scoped_service
    first = memory("session", repo="alpha", session="alpha", timestamp=2000)
    second = memory("session", repo="alpha", session="alpha")
    # A historical/malformed record must not acquire authority through a read filter.
    beta_id = service.store.get_or_create_repo(service.store.get_memory(first).workspace_id, "beta")
    service.store.conn.execute("UPDATE memories SET repo_id=? WHERE id=?", (beta_id, first))
    service.store.conn.commit()
    before = list(service.store.conn.iterdump())
    with pytest.raises(ValidationError, match="inconsistent session scope"):
        service.spec_crawl_resolve(action=action, node_a=first, node_b=second,
                                  workspace="selected", session_id=sessions["alpha"], confirmed=True)
    assert list(service.store.conn.iterdump()) == before


@pytest.mark.parametrize("retained_scope", ["workspace", "repo", "session"])
def test_compatible_links_preserve_both_endpoints(scoped_service, retained_scope):
    service, memory, sessions = scoped_service
    first = memory(retained_scope, repo="alpha" if retained_scope != "workspace" else None,
                   session="alpha" if retained_scope == "session" else None, timestamp=2000)
    second = memory("session", repo="alpha", session="alpha")
    result = service.spec_crawl_resolve(action="link", node_a=first, node_b=second,
                                      workspace="selected", session_id=sessions["alpha"])
    assert result["ok"]
    assert service.store.get_memory(first).valid_to is None
    assert service.store.get_memory(second).valid_to is None


def test_failure_after_validated_link_rolls_back_all_mutations(scoped_service, monkeypatch):
    service, memory, _ = scoped_service
    first, second = memory("repo", repo="alpha", timestamp=2000), memory("repo", repo="alpha")
    before = list(service.store.conn.iterdump())

    def failed_retirement(*args, **kwargs):
        assert service.store.conn.execute("SELECT COUNT(*) FROM mem_links").fetchone()[0] == 1
        raise ValidationError("synthetic retirement failure")

    monkeypatch.setattr(service, "retire", failed_retirement)
    with pytest.raises(ValidationError, match="synthetic retirement failure"):
        _resolve(service, first, second, repo="alpha")
    assert list(service.store.conn.iterdump()) == before


def test_cross_scope_supersession_preserves_temporal_history(scoped_service, monkeypatch):
    service, memory, sessions = scoped_service
    retained = memory("workspace", timestamp=2000)
    retired = memory("session", repo="alpha", session="alpha")
    originals = {mid: service.store.get_memory(mid) for mid in (retained, retired)}
    now = 30_000.0
    monkeypatch.setattr("engraphis.core.store.now_ts", lambda: now)
    monkeypatch.setattr("engraphis.core.engine.now_ts", lambda: now)
    # Make recording time controllable without rewriting the memory's world time.
    service.store.conn.execute("UPDATE memories SET ingested_at=10000 WHERE id IN (?,?)", (retained, retired))
    service.store.conn.commit()
    _resolve(service, retained, retired, session_id=sessions["alpha"])
    wid = service.store.get_memory(retained).workspace_id
    known = SearchFilter(workspace_id=wid, session_id=sessions["alpha"],
                         repo_id=service.store.get_memory(retired).repo_id,
                         valid_at=2500, known_at=now, include_ancestors=True)
    assert service.store.visible_memory_ids([retained, retired], known) == {retained}
    assert service.store.get_links(retained, flt=known)[0]["relation"] == "supersedes"
    known.known_at = now - 1
    assert service.store.visible_memory_ids([retained, retired], known) == {retained, retired}
    assert service.store.get_links(retained, flt=known) == []
    for mid, original in originals.items():
        actual = service.store.get_memory(mid)
        assert (actual.content, actual.metadata, actual.valid_from) == (
            original.content, original.metadata, original.valid_from,
        )
