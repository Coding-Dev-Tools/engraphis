"""Session identity, lifecycle, and isolation regression tests.

Retries for the exact same task identity reuse one active session. Different goals, users,
agents, or repos never inherit each other's work. Starts and ends are atomic, an ended
handoff cannot be overwritten, and no write may target a closed session.
"""
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from engraphis.service import (
    MemoryService,
    ValidationError,
    current_user,
    set_current_user,
)


def _svc():
    return MemoryService.create(":memory:")


@pytest.fixture(autouse=True)
def _clear_current_user():
    set_current_user(None)
    yield
    set_current_user(None)


@pytest.mark.parametrize(
    ("principal", "message"),
    [
        ({"email": "alice@example.test", "role": "member"}, "principal id"),
        ({"id": "usr_alice", "role": "member"}, "principal email"),
        ({"id": "", "email": "alice@example.test"}, "principal id"),
        ({"id": "bad id", "email": "alice@example.test"}, "principal id is invalid"),
        ({"id": 7, "email": "alice@example.test"}, "principal id must be a string"),
        ({"id": "usr_alice", "email": "not-an-email"}, "principal email is invalid"),
        ({"id": "usr_alice", "email": ["alice@example.test"]},
         "principal email must be a string"),
        ({"id": "usr_alice\x00", "email": "alice@example.test"},
         "principal id contains control characters"),
        ([], "principal must be an object"),
    ],
)
def test_authenticated_principal_requires_stable_id_and_ownership_email(
        principal, message):
    set_current_user({
        "id": "usr_existing", "email": "existing@example.test", "role": "admin",
    })

    with pytest.raises(ValidationError, match=message):
        set_current_user(principal)

    # A failed rebind must not authorize the request as the previous principal.
    assert current_user() is None


def test_authenticated_principal_is_normalized_copied_and_member_safe():
    supplied = {
        "id": "  usr_alice  ", "email": "  Alice@Example.Test  ", "role": "owner",
    }
    set_current_user(supplied)
    supplied.update({"id": "usr_mutated", "email": "mutated@test", "role": "admin"})

    principal = current_user()
    assert principal == {
        "id": "usr_alice", "email": "alice@example.test", "role": "member",
    }
    principal["role"] = "admin"
    assert current_user()["role"] == "member"


@pytest.mark.parametrize(
    ("principal", "message"),
    [
        ({"email": "alice@example.test", "role": "member"}, "principal id"),
        ({"id": "usr_alice", "role": "member"}, "principal email"),
    ],
)
def test_malformed_authenticated_principal_creates_no_workspace_or_session(
        principal, message):
    svc = _svc()

    with pytest.raises(ValidationError, match=message):
        set_current_user(principal)

    assert svc.store.conn.execute("SELECT 1 FROM workspaces").fetchone() is None
    assert svc.store.conn.execute("SELECT 1 FROM sessions").fetchone() is None


def test_repeat_start_reuses_exact_active_task():
    svc = _svc()
    a = svc.start_session("w", repo="r", agent="claude-code", goal="first")
    b = svc.start_session("w", repo="r", agent="claude-code", goal="first")
    assert a["reused"] is False
    assert b["reused"] is True
    assert b["session_id"] == a["session_id"]
    assert b["goal"] == "first"


def test_different_goal_opens_a_distinct_session_without_force_new():
    svc = _svc()
    first = svc.start_session("w", repo="r", agent="codex", goal="LinkedIn launch")
    second = svc.start_session(
        "w", repo="r", agent="codex", goal="private-cloud extraction"
    )
    assert second["reused"] is False
    assert second["session_id"] != first["session_id"]
    assert second["goal"] == "private-cloud extraction"


def test_force_new_branches_a_fresh_session():
    svc = _svc()
    a = svc.start_session("w", repo="r", agent="claude-code", goal="same")
    b = svc.start_session(
        "w", repo="r", agent="claude-code", goal="same", force_new=True
    )
    assert b["reused"] is False
    assert b["session_id"] != a["session_id"]


def test_different_agent_is_a_different_session():
    svc = _svc()
    a = svc.start_session("w", repo="r", agent="claude-code")
    b = svc.start_session("w", repo="r", agent="cursor")
    assert b["reused"] is False
    assert b["session_id"] != a["session_id"]


def test_empty_agent_is_an_exact_identity_not_a_wildcard():
    svc = _svc()
    named = svc.start_session("w", repo="r", agent="cursor", goal="task")
    unnamed = svc.start_session("w", repo="r", agent="", goal="task")
    assert unnamed["reused"] is False
    assert unnamed["session_id"] != named["session_id"]


def test_different_repo_is_a_different_session():
    svc = _svc()
    a = svc.start_session("w", repo="backend", agent="claude-code")
    b = svc.start_session("w", repo="frontend", agent="claude-code")
    assert b["reused"] is False
    assert b["session_id"] != a["session_id"]


def test_ended_session_is_not_reused():
    svc = _svc()
    a = svc.start_session("w", repo="r", agent="claude-code", goal="task")
    svc.end_session(a["session_id"], summary="done", outcome="shipped",
                    open_threads=["follow up on X"])
    b = svc.start_session("w", repo="r", agent="claude-code", goal="task")
    assert b["reused"] is False
    assert b["session_id"] != a["session_id"]
    assert b["bootstrap"].get("outcome") == "shipped"
    assert "follow up on X" in b["bootstrap"].get("open_threads", [])


def test_concurrent_exact_starts_reuse_one_session(tmp_path):
    db = str(tmp_path / "sessions.db")
    first = MemoryService.create(db)
    first.remember("create the shared scope", workspace="w", repo="r")
    second = MemoryService.create(db)
    barrier = threading.Barrier(2)

    def start(svc):
        barrier.wait(timeout=10)
        return svc.start_session("w", repo="r", agent="codex", goal="same task")

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(start, (first, second)))
        assert len({item["session_id"] for item in results}) == 1
        assert sorted(item["reused"] for item in results) == [False, True]
    finally:
        second.store.close()
        first.store.close()


@pytest.mark.parametrize("operation", ["start", "end"])
def test_same_store_waiter_opens_and_settles_its_own_transaction(monkeypatch, operation):
    """A transaction owned by another thread must not look like our outer transaction."""
    svc = _svc()
    wid = svc.store.get_or_create_workspace("w")
    rid = svc.store.get_or_create_repo(wid, "r")
    sid = svc.store.start_session(wid, rid, agent="existing", goal="close me")
    main_thread = threading.get_ident()
    waiter_reached_lock = threading.Event()
    connection_type = type(svc.store.conn)
    original_acquire = connection_type._acquire

    def observed_acquire(connection):
        if connection is svc.store.conn and threading.get_ident() != main_thread:
            waiter_reached_lock.set()
        return original_acquire(connection)

    monkeypatch.setattr(connection_type, "_acquire", observed_acquire)

    def run_operation():
        if operation == "start":
            return svc.store.get_or_start_session(
                wid, rid, agent="waiter", goal="new task"
            )
        return svc.store.end_session(sid, summary="closed by waiter")

    svc.store.conn.execute("BEGIN IMMEDIATE")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run_operation)
        assert waiter_reached_lock.wait(timeout=10)
        assert not future.done()
        svc.store.conn.commit()
        result = future.result(timeout=10)

    assert svc.store.conn.in_transaction is False
    if operation == "start":
        assert result[1] is False
    else:
        assert result == "ended"


def test_team_users_get_distinct_owned_sessions_and_cannot_cross_access():
    svc = _svc()
    svc.remember("shared workspace seed", workspace="w", repo="r")
    try:
        set_current_user({"id": "usr_alice", "email": "alice@example.test", "role": "member"})
        alice = svc.start_session("w", repo="r", agent="codex", goal="same task")
        svc.remember(
            "Alice private working note", workspace="w", repo="r",
            session_id=alice["session_id"], scope="session",
        )

        set_current_user({"id": "usr_bob", "email": "bob@example.test", "role": "member"})
        bob = svc.start_session("w", repo="r", agent="codex", goal="same task")
        assert bob["reused"] is False
        assert bob["session_id"] != alice["session_id"]
        assert svc.store.get_session(alice["session_id"])["user_id"] == "usr_alice"
        assert svc.store.get_session(bob["session_id"])["user_id"] == "usr_bob"
        with pytest.raises(ValidationError, match="another user"):
            svc.recall(
                "private working", workspace="w", repo="r",
                session_id=alice["session_id"],
            )
        with pytest.raises(ValidationError, match="another user"):
            svc.grounded_recall(
                "private working", workspace="w", repo="r",
                session_id=alice["session_id"],
            )
        with pytest.raises(ValidationError, match="another user"):
            svc.remember(
                "Bob must not attach to Alice's session", workspace="w", repo="r",
                session_id=alice["session_id"],
            )
        with pytest.raises(ValidationError, match="another user"):
            svc.ingest(
                "Bob must not ingest into Alice's session", workspace="w", repo="r",
                session_id=alice["session_id"],
            )
        with pytest.raises(ValidationError, match="another user"):
            svc.record_event(
                "cross-user", "Bob must not record into Alice's session",
                workspace="w", repo="r", session_id=alice["session_id"],
            )
        with pytest.raises(ValidationError, match="another user"):
            svc.end_session(alice["session_id"], summary="hijacked")
    finally:
        set_current_user(None)


def test_stable_id_scopes_sessions_even_when_ownership_email_matches():
    svc = _svc()
    svc.remember("shared workspace seed", workspace="w", repo="r")
    shared_email = "shared-login@example.test"

    set_current_user({"id": "usr_alice", "email": shared_email, "role": "member"})
    alice = svc.start_session("w", repo="r", agent="codex", goal="same task")
    svc.end_session(alice["session_id"], summary="Alice private handoff")

    set_current_user({"id": "usr_bob", "email": shared_email, "role": "member"})
    bob = svc.start_session("w", repo="r", agent="codex", goal="same task")

    assert bob["reused"] is False
    assert bob["session_id"] != alice["session_id"]
    assert bob["bootstrap"] == {}
    assert svc.stats(workspace="w")["sessions"] == 1
    with pytest.raises(ValidationError, match="another user"):
        svc.end_session(alice["session_id"], summary="hijacked")


def test_authenticated_principal_cannot_claim_legacy_ownerless_session():
    svc = _svc()
    legacy = svc.start_session("w", repo="r", agent="codex", goal="same task")
    svc.remember(
        "LEGACY_OWNERLESS_PRIVATE", workspace="w", repo="r",
        session_id=legacy["session_id"], scope="session",
    )
    assert svc.store.get_session(legacy["session_id"])["user_id"] == ""

    set_current_user({
        "id": "usr_alice", "email": "alice@example.test", "role": "member",
    })
    owned = svc.start_session("w", repo="r", agent="codex", goal="same task")

    assert owned["reused"] is False
    assert owned["session_id"] != legacy["session_id"]
    with pytest.raises(ValidationError, match="no authenticated owner"):
        svc.recall(
            "LEGACY_OWNERLESS_PRIVATE", workspace="w", repo="r",
            session_id=legacy["session_id"],
        )
    with pytest.raises(ValidationError, match="no authenticated owner"):
        svc.end_session(legacy["session_id"], summary="claimed")
    exported = repr(svc.export_workspace(workspace="w", recovery=True))
    assert "LEGACY_OWNERLESS_PRIVATE" not in exported
    assert legacy["session_id"] not in exported
    assert svc.store.get_session(legacy["session_id"])["status"] == "active"


def test_handoffs_are_user_scoped_and_start_bootstrap_is_agent_scoped():
    svc = _svc()
    svc.remember("shared workspace seed", workspace="w", repo="r")
    try:
        set_current_user({"id": "usr_alice", "email": "alice@example.test", "role": "member"})
        alice = svc.start_session("w", repo="r", agent="codex", goal="alice task")
        svc.end_session(
            alice["session_id"], summary="Alice secret handoff", outcome="alice done",
            open_threads=["Alice private follow-up"],
        )

        set_current_user({"id": "usr_bob", "email": "bob@example.test", "role": "member"})
        bob = svc.start_session("w", repo="r", agent="codex", goal="bob task")
        assert bob["bootstrap"] == {}
        assert svc.recall_proactive(workspace="w", repo="r")["last_session"] == {}
        assert svc.proactive_context(workspace="w", repo="r")["last_session"] == {}
        svc.end_session(bob["session_id"], summary="Bob handoff", outcome="bob done")

        set_current_user({"id": "usr_alice", "email": "alice@example.test", "role": "member"})
        proactive = svc.recall_proactive(workspace="w", repo="r")
        assert proactive["last_session"]["summary"] == "Alice secret handoff"
        assert (
            svc.proactive_context(workspace="w", repo="r")["last_session"]["summary"]
            == "Alice secret handoff"
        )

        different_agent = svc.start_session(
            "w", repo="r", agent="cursor", goal="different client"
        )
        assert different_agent["bootstrap"] == {}
        same_agent = svc.start_session("w", repo="r", agent="codex", goal="next task")
        assert same_agent["bootstrap"]["summary"] == "Alice secret handoff"
        assert same_agent["bootstrap"]["open_threads"] == ["Alice private follow-up"]
    finally:
        set_current_user(None)


def test_bound_service_cannot_close_a_foreign_workspace_session():
    seed = _svc()
    foreign = seed.start_session("beta", repo="private", agent="codex", goal="secret")
    seed.remember("create allowed workspace", workspace="alpha")
    bound = MemoryService(seed.engine, allowed_workspaces=["alpha"])

    with pytest.raises(ValidationError, match="not permitted"):
        bound.end_session(foreign["session_id"], summary="foreign close")
    assert seed.store.get_session(foreign["session_id"])["status"] == "active"


def test_end_session_is_idempotent_but_conflicting_retry_cannot_overwrite():
    svc = _svc()
    started = svc.start_session("w", repo="r", agent="codex", goal="task")
    sid = started["session_id"]
    expected = {
        "summary": "completed safely",
        "outcome": "shipped",
        "open_threads": ["follow-up"],
    }
    first = svc.end_session(sid, **expected)
    ended_at = svc.store.get_session(sid)["ended_at"]
    second = svc.end_session(sid, **expected)
    assert second == first
    assert svc.store.get_session(sid)["ended_at"] == ended_at

    with pytest.raises(ValidationError, match="different handoff"):
        svc.end_session(
            sid, summary="unrelated overwrite", outcome="blocked", open_threads=["wrong"]
        )
    stored = svc.store.get_session(sid)
    assert stored["summary"] == expected["summary"]
    assert stored["outcome"] == expected["outcome"]
    assert stored["open_threads"] == expected["open_threads"]


def test_concurrent_conflicting_ends_preserve_exactly_one_handoff(tmp_path):
    db = str(tmp_path / "session-end.db")
    first = MemoryService.create(db)
    started = first.start_session("w", repo="r", agent="codex", goal="task")
    second = MemoryService.create(db)
    barrier = threading.Barrier(2)

    def close(svc, label):
        barrier.wait(timeout=10)
        try:
            svc.end_session(
                started["session_id"], summary=f"{label} summary",
                outcome=f"{label} outcome", open_threads=[f"{label} thread"],
            )
            return "ended"
        except ValidationError as exc:
            assert "different handoff" in str(exc)
            return "conflict"

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(close, (first, second), ("first", "second")))
        assert sorted(results) == ["conflict", "ended"]
        stored = first.store.get_session(started["session_id"])
        winner = stored["summary"].removesuffix(" summary")
        assert winner in {"first", "second"}
        assert stored["outcome"] == f"{winner} outcome"
        assert stored["open_threads"] == [f"{winner} thread"]
    finally:
        second.store.close()
        first.store.close()


def test_closed_session_rejects_every_write_path():
    svc = _svc()
    started = svc.start_session("w", repo="r", agent="codex", goal="task")
    sid = started["session_id"]
    svc.end_session(sid, summary="closed")

    calls = (
        lambda: svc.remember("late memory", workspace="w", repo="r", session_id=sid),
        lambda: svc.ingest("late ingest", workspace="w", repo="r", session_id=sid),
        lambda: svc.record_event("late", "late event", workspace="w", repo="r",
                                 session_id=sid),
    )
    for call in calls:
        with pytest.raises(ValidationError, match="not active"):
            call()


@pytest.mark.parametrize("operation", ["remember", "ingest"])
def test_session_close_linearizes_before_delayed_memory_write(monkeypatch, operation):
    """Closing after service preflight must still prevent the eventual engine write."""
    svc = _svc()
    started = svc.start_session("w", repo="r", agent="codex", goal="racing write")
    sid = started["session_id"]
    entered_embed = threading.Event()
    release_embed = threading.Event()
    original_embed = svc.engine.embedder.embed

    def delayed_embed(texts):
        entered_embed.set()
        assert release_embed.wait(timeout=10)
        return original_embed(texts)

    monkeypatch.setattr(svc.engine.embedder, "embed", delayed_embed)

    def write():
        method = getattr(svc, operation)
        return method(
            "must not survive closure", workspace="w", repo="r", session_id=sid
        )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(write)
        assert entered_embed.wait(timeout=10)
        svc.end_session(sid, summary="closed before write")
        release_embed.set()
        with pytest.raises(ValidationError, match="not active"):
            future.result(timeout=10)

    count = svc.store.conn.execute(
        "SELECT COUNT(*) AS n FROM memories WHERE session_id=?", (sid,)
    ).fetchone()["n"]
    assert count == 0


def test_session_close_linearizes_before_delayed_event_write(monkeypatch):
    svc = _svc()
    started = svc.start_session("w", repo="r", agent="codex", goal="racing event")
    sid = started["session_id"]
    entered_engine = threading.Event()
    release_engine = threading.Event()
    original_record_event = svc.engine.record_event

    def delayed_record_event(*args, **kwargs):
        entered_engine.set()
        assert release_engine.wait(timeout=10)
        return original_record_event(*args, **kwargs)

    monkeypatch.setattr(svc.engine, "record_event", delayed_record_event)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            svc.record_event, "race", "must not survive closure",
            workspace="w", repo="r", session_id=sid,
        )
        assert entered_engine.wait(timeout=10)
        svc.end_session(sid, summary="closed before event")
        release_engine.set()
        with pytest.raises(ValidationError, match="not active"):
            future.result(timeout=10)

    count = svc.store.conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE session_id=?", (sid,)
    ).fetchone()["n"]
    assert count == 0


def test_bare_id_governance_and_export_respect_session_owner():
    svc = _svc()
    svc.remember("shared record", workspace="w", repo="r")
    try:
        set_current_user({"id": "usr_alice", "email": "alice@test", "role": "member"})
        session = svc.start_session("w", repo="r", agent="codex", goal="private")
        private = svc.remember(
            "ALICE_BARE_ID_SECRET", workspace="w", repo="r",
            session_id=session["session_id"], scope="session",
        )
        alice_export = svc.export_workspace(workspace="w", recovery=True)
        assert "ALICE_BARE_ID_SECRET" in repr(alice_export)

        set_current_user({"id": "usr_bob", "email": "bob@test", "role": "member"})
        with pytest.raises(ValidationError, match="another user"):
            svc.inspect(private["id"], workspace="w", repo="r")
        with pytest.raises(ValidationError, match="another user"):
            svc.pin(private["id"], workspace="w", repo="r")
        with pytest.raises(ValidationError, match="another user"):
            svc.forget(private["id"], workspace="w", repo="r")
        bob_export = svc.export_workspace(workspace="w", recovery=True)
        assert "ALICE_BARE_ID_SECRET" not in repr(bob_export)
        assert session["session_id"] not in repr(bob_export)
    finally:
        set_current_user(None)


def test_shared_workspace_whole_scope_mutations_require_sharer_or_admin():
    svc = _svc()
    try:
        set_current_user({"id": "usr_alice", "email": "alice@test", "role": "member"})
        svc.create_workspace("shared", visibility="shared", confirmed=True)
        session = svc.start_session("shared", agent="codex", goal="private")
        svc.remember(
            "Alice session retention boundary", workspace="shared",
            session_id=session["session_id"], scope="session",
        )

        set_current_user({"id": "usr_bob", "email": "bob@test", "role": "member"})
        for operation in (
            lambda: svc.delete_workspace("shared"),
            lambda: svc.copy_workspace("shared", "stolen-copy"),
            lambda: svc.rename_workspace("shared", "renamed-by-bob"),
            lambda: svc.set_workspace_description("shared", "changed by Bob"),
        ):
            with pytest.raises(ValidationError, match="original sharer or an admin"):
                operation()

        set_current_user({"id": "usr_admin", "email": "admin@test", "role": "admin"})
        assert svc.copy_workspace("shared", "admin-copy")["memories_copied"] == 1
    finally:
        set_current_user(None)
