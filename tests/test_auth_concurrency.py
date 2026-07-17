from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

pytest.importorskip("fastapi")

from engraphis.inspector.auth import AuthError, AuthStore


PASSWORD = "StrongPassword1!"


def _allow_team(monkeypatch):
    monkeypatch.setattr("engraphis.licensing.require_feature", lambda *args, **kwargs: None)


def test_concurrent_user_creation_cannot_oversubscribe_seats(monkeypatch, tmp_path):
    _allow_team(monkeypatch)
    store = AuthStore(str(tmp_path / "users.db"), iterations=1)
    store.create_user("admin@example.com", "Admin", PASSWORD, "admin")
    barrier = threading.Barrier(8)

    def create(index):
        barrier.wait()
        try:
            store.create_user(
                f"user{index}@example.com", f"User {index}", PASSWORD, "member",
                seat_limit=3,
            )
            return "created"
        except AuthError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(create, range(8)))

    assert outcomes.count("created") == 2
    assert outcomes.count("rejected") == 6
    assert store.count_active_users() == 3


def test_concurrent_admin_demotions_cannot_remove_last_admin(monkeypatch, tmp_path):
    _allow_team(monkeypatch)
    store = AuthStore(str(tmp_path / "users.db"), iterations=1)
    first = store.create_user("first@example.com", "First", PASSWORD, "admin")
    second = store.create_user(
        "second@example.com", "Second", PASSWORD, "admin", seat_limit=2
    )
    barrier = threading.Barrier(2)

    def demote(user_id):
        barrier.wait()
        try:
            store.update_user(user_id, role="member")
            return "demoted"
        except AuthError:
            return "protected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(demote, (first["id"], second["id"])))

    assert sorted(outcomes) == ["demoted", "protected"]
    active_admins = [
        user for user in store.list_users()
        if user["role"] == "admin" and not user["disabled"]
    ]
    assert len(active_admins) == 1


def test_password_reset_token_is_consumed_once_under_concurrency(monkeypatch, tmp_path):
    _allow_team(monkeypatch)
    store = AuthStore(str(tmp_path / "users.db"), iterations=1)
    store.create_user("admin@example.com", "Admin", PASSWORD, "admin")
    reset = store.request_password_reset("admin@example.com")
    barrier = threading.Barrier(2)

    def consume(index):
        barrier.wait()
        try:
            store.reset_password(reset["token"], f"ReplacementPassword{index}!")
            return "reset"
        except AuthError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(consume, range(2)))

    assert sorted(outcomes) == ["rejected", "reset"]
    active_sessions = store.conn.execute(
        "SELECT COUNT(*) FROM auth_sessions"
    ).fetchone()[0]
    assert active_sessions == 1


def test_password_reset_revokes_long_lived_api_tokens(monkeypatch, tmp_path):
    _allow_team(monkeypatch)
    store = AuthStore(str(tmp_path / "users.db"), iterations=1)
    user = store.create_user("admin@example.com", "Admin", PASSWORD, "admin")
    api_token = store.create_api_token(user["id"], label="agent")["token"]
    reset = store.request_password_reset("admin@example.com")

    store.reset_password(reset["token"], "ReplacementPassword1!")

    assert store.resolve_api_token(api_token) is None


def test_delete_user_removes_auth_secrets_and_unicode_tokens_fail_normally(
        monkeypatch, tmp_path):
    _allow_team(monkeypatch)
    store = AuthStore(str(tmp_path / "users.db"), iterations=1)
    store.create_user("admin@example.com", "Admin", PASSWORD, "admin")
    member = store.create_user(
        "member@example.com", "Member", PASSWORD, "member", seat_limit=2
    )
    store.create_api_token(member["id"], label="agent")
    store.request_password_reset("member@example.com")
    store.create_session(member["id"])

    store.delete_user(member["id"])

    assert store.conn.execute(
        "SELECT COUNT(*) FROM auth_sessions WHERE user_id=?", (member["id"],)
    ).fetchone()[0] == 0
    assert store.conn.execute(
        "SELECT COUNT(*) FROM password_resets WHERE user_id=?", (member["id"],)
    ).fetchone()[0] == 0
    assert store.conn.execute(
        "SELECT COUNT(*) FROM api_tokens WHERE user_id=?", (member["id"],)
    ).fetchone()[0] == 0
    assert store.resolve_session("☃") is None
    assert store.resolve_api_token("☃") is None


def test_auth_resource_caps_bound_untrusted_growth(monkeypatch, tmp_path):
    _allow_team(monkeypatch)
    monkeypatch.setattr("engraphis.inspector.auth.MAX_THROTTLE_KEYS", 3)
    monkeypatch.setattr("engraphis.inspector.auth.MAX_SESSIONS_PER_USER", 2)
    monkeypatch.setattr("engraphis.inspector.auth.MAX_ACTIVE_API_TOKENS", 2)
    monkeypatch.setattr("engraphis.inspector.auth.MAX_REVOKED_API_TOKENS", 1)
    store = AuthStore(str(tmp_path / "users.db"), iterations=1)
    user = store.create_user("admin@example.com", "Admin", PASSWORD, "admin")

    for index in range(8):
        with pytest.raises(AuthError):
            store.login(f"unknown{index}@example.com", "wrong")
        store.request_password_reset(f"missing{index}@example.com")
    assert len(store._failures) <= 3
    assert len(store._reset_requests) <= 3

    sessions = [store.create_session(user["id"]) for _ in range(3)]
    assert store.resolve_session(sessions[0]) is None
    assert store.resolve_session(sessions[1]) is not None
    assert store.resolve_session(sessions[2]) is not None

    first = store.create_api_token(user["id"])
    second = store.create_api_token(user["id"])
    with pytest.raises(AuthError, match="token limit"):
        store.create_api_token(user["id"])
    assert store.revoke_api_token(user["id"], first["id"]) is True
    third = store.create_api_token(user["id"])
    assert store.revoke_api_token(user["id"], second["id"]) is True
    assert store.revoke_api_token(user["id"], third["id"]) is True
    revoked = store.conn.execute(
        "SELECT COUNT(*) FROM api_tokens WHERE user_id=? AND revoked=1",
        (user["id"],),
    ).fetchone()[0]
    assert revoked == 1


def test_password_reset_prunes_revoked_token_history(monkeypatch, tmp_path):
    _allow_team(monkeypatch)
    monkeypatch.setattr("engraphis.inspector.auth.MAX_REVOKED_API_TOKENS", 2)
    store = AuthStore(str(tmp_path / "users.db"), iterations=1)
    user = store.create_user("admin@example.com", "Admin", PASSWORD, "admin")
    for index in range(4):
        store.create_api_token(user["id"], label=str(index))
    reset = store.request_password_reset(user["email"])

    store.reset_password(reset["token"], "ReplacementPassword1!")

    revoked = store.conn.execute(
        "SELECT COUNT(*) FROM api_tokens WHERE user_id=? AND revoked=1",
        (user["id"],),
    ).fetchone()[0]
    assert revoked == 2


def test_disabling_user_permanently_revokes_api_tokens(monkeypatch, tmp_path):
    _allow_team(monkeypatch)
    store = AuthStore(str(tmp_path / "users.db"), iterations=1)
    store.create_user("admin@example.com", "Admin", PASSWORD, "admin")
    member = store.create_user(
        "member@example.com", "Member", PASSWORD, "member", seat_limit=2
    )
    token = store.create_api_token(member["id"])["token"]

    store.update_user(member["id"], disabled=True)
    store.update_user(member["id"], disabled=False, seat_limit=2)

    assert store.resolve_api_token(token) is None


def _park_first_hash(monkeypatch):
    """Make the FIRST PBKDF2 verification block until released, so a test can inject a
    concurrent disable / reset / lockout into the window where ``login`` runs the hash
    OFF the lock (between phase 1 and phase 3). Later verifications — e.g. the failed
    logins that drive a lockout — run normally. Returns ``(in_hash, release)`` events."""
    import engraphis.inspector.auth as authmod

    in_hash = threading.Event()
    release = threading.Event()
    real_verify = authmod._verify_password
    state = {"n": 0}

    def blocking_verify(password, encoded):
        n = state["n"]
        state["n"] += 1
        result = real_verify(password, encoded)
        if n == 0:
            in_hash.set()
            release.wait(5)
        return result

    monkeypatch.setattr(authmod, "_verify_password", blocking_verify)
    return in_hash, release


def test_login_denied_when_user_disabled_during_hash(monkeypatch, tmp_path):
    """A disable committed while a login is mid-hash must not be overwritten by a session
    minted from the stale phase-1 row."""
    _allow_team(monkeypatch)
    store = AuthStore(str(tmp_path / "users.db"), iterations=1)
    store.create_user("admin@example.com", "Admin", PASSWORD, "admin")
    member = store.create_user(
        "member@example.com", "Member", PASSWORD, "member", seat_limit=5)
    in_hash, release = _park_first_hash(monkeypatch)

    result = {}

    def do_login():
        try:
            result["user"] = store.login("member@example.com", PASSWORD)
        except AuthError as exc:
            result["error"] = str(exc)

    thread = threading.Thread(target=do_login)
    thread.start()
    assert in_hash.wait(5)                               # parked mid-hash (phase 2)
    store.update_user(member["id"], disabled=True)        # disable lands during the hash
    release.set()
    thread.join(5)

    assert "user" not in result                           # no session for a disabled user
    assert "error" in result
    assert store.conn.execute(
        "SELECT COUNT(*) FROM auth_sessions WHERE user_id=?", (member["id"],)
    ).fetchone()[0] == 0


def test_login_rejected_when_password_reset_during_hash(monkeypatch, tmp_path):
    """The OLD password, verified against the pre-reset hash off the lock, must not mint a
    session once a reset rotates ``pw_hash`` during the hash window."""
    _allow_team(monkeypatch)
    store = AuthStore(str(tmp_path / "users.db"), iterations=1)
    store.create_user("admin@example.com", "Admin", PASSWORD, "admin")
    store.create_user("member@example.com", "Member", PASSWORD, "member", seat_limit=5)
    in_hash, release = _park_first_hash(monkeypatch)

    result = {}

    def do_login():
        try:
            result["user"] = store.login("member@example.com", PASSWORD)   # the OLD password
        except AuthError as exc:
            result["error"] = str(exc)

    thread = threading.Thread(target=do_login)
    thread.start()
    assert in_hash.wait(5)
    reset = store.request_password_reset("member@example.com")
    store.reset_password(reset["token"], "BrandNewPassword9!")             # rotates pw_hash
    release.set()
    thread.join(5)

    assert "user" not in result        # the old password must not sign in after the reset
    assert "error" in result


def test_login_burst_cannot_beat_lockout_via_offlock_hash(monkeypatch, tmp_path):
    """Because PBKDF2 runs off the lock, a burst of attempts all clear the phase-1 throttle
    gate before any failure is recorded. Phase 3 must re-check the lockout so a correct
    guess inside the burst can't be converted into a session past the threshold."""
    _allow_team(monkeypatch)
    from engraphis.inspector.auth import LOCKOUT_FAILS
    store = AuthStore(str(tmp_path / "users.db"), iterations=1)
    store.create_user("admin@example.com", "Admin", PASSWORD, "admin")
    store.create_user("member@example.com", "Member", PASSWORD, "member", seat_limit=5)
    in_hash, release = _park_first_hash(monkeypatch)

    result = {}

    def do_login():
        try:
            result["user"] = store.login("member@example.com", PASSWORD)   # correct password
        except AuthError as exc:
            result["error"] = str(exc)

    thread = threading.Thread(target=do_login)
    thread.start()
    assert in_hash.wait(5)                       # correct login parked mid-hash, past phase 1
    for _ in range(LOCKOUT_FAILS):               # drive the account into lockout meanwhile
        with pytest.raises(AuthError):
            store.login("member@example.com", "WrongPassword0!")
    release.set()
    thread.join(5)

    assert "user" not in result                  # a hit can't be minted while locked out
    assert "too many failed attempts" in result.get("error", "")
    assert store.conn.execute(
        "SELECT COUNT(*) FROM auth_sessions WHERE user_id IN "
        "(SELECT id FROM users WHERE email='member@example.com')"
    ).fetchone()[0] == 0
