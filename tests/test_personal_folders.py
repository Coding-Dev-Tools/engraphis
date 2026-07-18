"""Personal vs shared workspace folders (team mode).

A folder created ``visibility='personal'`` is owned by — and visible/usable only to — the
creating dashboard user. Enforcement lives at MemoryService's single workspace-authorization
chokepoint (``_authorize_workspace`` via ``_clean_ws``), so *every* scoped read and write
inherits it, exactly like the existing ``ENGRAPHIS_WORKSPACES`` binding. Outside team mode
there is no current user, so nothing is restricted and single-tenant behaviour is unchanged.

These run offline on numpy alone (no fastapi/HTTP) by driving the current-user contextvar
directly — the same value the dashboard's team auth gate binds once per request. The full
HTTP path (real cookies, two users, one app) is covered in test_dashboard_v2.py.
"""
from unittest.mock import Mock

import pytest

from engraphis.service import MemoryService, ValidationError, set_current_user

ALICE = {"email": "alice@x.co", "name": "Alice", "role": "member", "id": "u_alice"}
BOB = {"email": "bob@x.co", "name": "Bob", "role": "admin", "id": "u_bob"}
CAROL = {"email": "carol@x.co", "name": "Carol", "role": "member", "id": "u_carol"}


@pytest.fixture(autouse=True)
def _clear_user():
    """The current-user contextvar is process-wide; never let one test's identity leak
    into the next (a leaked user would silently change what every later test can see)."""
    set_current_user(None)
    yield
    set_current_user(None)


def _svc():
    return MemoryService.create(":memory:")


def _names(svc):
    return sorted(w["name"] for w in svc.list_workspaces()["workspaces"])


# ── defaults / creation ───────────────────────────────────────────────────────
def test_personal_is_the_default_and_hidden_from_every_teammate():
    svc = _svc()
    set_current_user(ALICE)
    out = svc.create_workspace("team-proj", "private notes")
    assert out["visibility"] == "personal"
    set_current_user(BOB)  # a different teammate
    assert "team-proj" not in _names(svc)


def test_sharing_requires_confirmation_then_is_visible_to_every_teammate():
    svc = _svc()
    set_current_user(ALICE)
    with pytest.raises(ValidationError, match="explicit confirmation"):
        svc.create_workspace("team-proj", visibility="shared")
    out = svc.create_workspace("team-proj", visibility="shared", confirmed=True)
    assert out["visibility"] == "shared"
    assert svc.list_workspaces()["workspaces"][0]["can_change_access"] is True
    assert svc.set_workspace_visibility(
        "team-proj", "personal", confirmed=True)["visibility"] == "personal"
    assert svc.set_workspace_visibility(
        "team-proj", "shared", confirmed=True)["visibility"] == "shared"
    set_current_user(BOB)
    assert "team-proj" in _names(svc)


def test_original_sharer_or_admin_can_unshare_but_another_member_cannot():
    svc = _svc()
    set_current_user(ALICE)
    svc.create_workspace("alice-notes")
    shared = svc.set_workspace_visibility("alice-notes", "shared", confirmed=True)
    assert shared["visibility"] == "shared"

    listed = {w["name"]: w for w in svc.list_workspaces()["workspaces"]}
    assert listed["alice-notes"]["can_change_access"] is True

    set_current_user(CAROL)
    listed = {w["name"]: w for w in svc.list_workspaces()["workspaces"]}
    assert listed["alice-notes"]["can_change_access"] is False
    with pytest.raises(ValidationError, match="original sharer or an admin"):
        svc.set_workspace_visibility("alice-notes", "personal", confirmed=True)

    set_current_user(ALICE)
    private = svc.set_workspace_visibility("alice-notes", "personal", confirmed=True)
    assert private["visibility"] == "personal" and private["owner"] == ALICE["email"]

    # An admin can still govern a legacy shared folder that has no original sharer.
    set_current_user(None)
    svc.store.create_workspace("legacy-shared", settings=None)
    set_current_user(BOB)
    legacy_private = svc.set_workspace_visibility(
        "legacy-shared", "personal", confirmed=True)
    assert legacy_private["owner"] == BOB["email"]


def test_personal_is_owned_by_its_creator():
    svc = _svc()
    set_current_user(ALICE)
    out = svc.create_workspace("alice-scratch", "mine", visibility="personal")
    assert out["visibility"] == "personal"
    assert out["owner"] == ALICE["email"]
    listed = {w["name"]: w for w in svc.list_workspaces()["workspaces"]}
    assert listed["alice-scratch"]["visibility"] == "personal"
    assert listed["alice-scratch"]["owner"] == ALICE["email"]
    assert listed["alice-scratch"]["mine"] is True


def test_personal_without_a_current_user_degrades_to_shared():
    # No signed-in user to own it (single-tenant / MCP / CLI) — don't mint an orphan
    # folder nobody could ever reach; fall back to shared instead.
    svc = _svc()
    set_current_user(None)
    out = svc.create_workspace("nobody", visibility="personal")
    assert out["visibility"] == "shared" and out["owner"] == ""


# ── isolation: listing ────────────────────────────────────────────────────────
def test_personal_folder_is_hidden_from_other_users_even_admins():
    svc = _svc()
    set_current_user(ALICE)
    svc.create_workspace("alice-scratch", visibility="personal")
    svc.create_workspace("team-proj", visibility="shared", confirmed=True)
    assert _names(svc) == ["alice-scratch", "team-proj"]  # owner sees both
    set_current_user(BOB)  # admin — but personal means personal
    assert _names(svc) == ["team-proj"]  # alice-scratch is omitted entirely


# ── isolation: every scoped read/write is refused for a non-owner ─────────────
def test_non_owner_is_refused_read_and_write_access():
    svc = _svc()
    set_current_user(ALICE)
    svc.create_workspace("alice-scratch", visibility="personal")
    svc.remember("Alice's private note.", workspace="alice-scratch", scope="workspace")

    set_current_user(BOB)
    for call in (
        lambda: svc._clean_ws("alice-scratch"),
        lambda: svc.recall("note", workspace="alice-scratch"),
        lambda: svc.remember("intrusion", workspace="alice-scratch", scope="workspace"),
        lambda: svc.stats(workspace="alice-scratch"),
        lambda: svc.rename_workspace("alice-scratch", "stolen"),
        lambda: svc.delete_workspace("alice-scratch"),
    ):
        with pytest.raises(ValidationError):
            call()


def test_current_user_cannot_run_workspace_less_recall(monkeypatch):
    svc = _svc()
    engine_recall = Mock(side_effect=AssertionError("global recall executed"))
    monkeypatch.setattr(svc.engine.recall_engine, "recall", engine_recall)
    set_current_user(ALICE)

    with pytest.raises(ValidationError, match=r"^workspace is required on this instance$"):
        svc.recall("private notes")

    engine_recall.assert_not_called()


def test_current_user_cannot_run_workspace_less_grounded_recall(monkeypatch):
    svc = _svc()
    engine_recall = Mock(side_effect=AssertionError("global grounded recall executed"))
    monkeypatch.setattr(svc.engine, "grounded_recall", engine_recall)
    set_current_user(ALICE)

    with pytest.raises(ValidationError, match=r"^workspace is required on this instance$"):
        svc.grounded_recall("private notes")

    engine_recall.assert_not_called()


def test_owner_keeps_full_access_to_their_personal_folder():
    svc = _svc()
    set_current_user(ALICE)
    svc.create_workspace("alice-scratch", visibility="personal")
    mid = svc.remember("Kept.", workspace="alice-scratch", scope="workspace")["id"]
    assert svc._clean_ws("alice-scratch") == "alice-scratch"
    assert mid  # write succeeded for the owner


def test_shared_folder_stays_accessible_to_everyone():
    svc = _svc()
    set_current_user(ALICE)
    svc.create_workspace("team-proj", visibility="shared", confirmed=True)
    set_current_user(BOB)
    assert svc._clean_ws("team-proj") == "team-proj"  # not blocked for a teammate


# ── backward compatibility: no user context ⇒ no restriction ──────────────────
def test_no_user_context_sees_and_reaches_everything():
    # The MCP server, CLI, sync loop and migrations leave the contextvar unset. They must
    # keep full access — otherwise a personal folder would become unreadable to the very
    # jobs (export, sync, consolidation) that operate the whole database.
    svc = _svc()
    set_current_user(ALICE)
    svc.create_workspace("alice-scratch", visibility="personal")
    private = svc.remember(
        "Alice keeps private deployment notes here.",
        workspace="alice-scratch",
        scope="workspace",
    )
    set_current_user(None)
    assert "alice-scratch" in _names(svc)
    assert svc._clean_ws("alice-scratch") == "alice-scratch"
    global_recall = svc.recall("private deployment notes")
    assert any(m["id"] == private["id"] for m in global_recall["memories"])
    assert svc.grounded_recall("private deployment notes")["query"] == "private deployment notes"


def test_folders_created_before_the_feature_are_treated_as_shared():
    # A workspace with no visibility recorded in settings (every folder pre-dating this
    # feature) must read as shared, never accidentally locked to nobody.
    svc = _svc()
    set_current_user(None)
    svc.store.create_workspace("legacy", settings=None)  # stored with no visibility key
    set_current_user(BOB)
    listed = {w["name"]: w for w in svc.list_workspaces()["workspaces"]}
    assert listed["legacy"]["visibility"] == "shared"
    assert svc._clean_ws("legacy") == "legacy"


def test_share_and_unshare_require_confirmation_and_are_audited():
    svc = _svc()
    set_current_user(ALICE)
    svc.create_workspace("alice-scratch")
    with pytest.raises(ValidationError, match="explicit confirmation"):
        svc.set_workspace_visibility("alice-scratch", "shared")
    assert svc.set_workspace_visibility("alice-scratch", "shared", confirmed=True)["visibility"] == "shared"
    set_current_user(BOB)
    assert "alice-scratch" in _names(svc)
    result = svc.set_workspace_visibility("alice-scratch", "personal", confirmed=True)
    assert result["owner"] == BOB["email"]
    set_current_user(ALICE)
    assert "alice-scratch" not in _names(svc)
