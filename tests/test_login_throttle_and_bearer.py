"""Typed lockout errors, the per-IP login throttle, and the shared bearer helper."""
import pytest

from engraphis.inspector.auth import (
    IP_LOCKOUT_FAILS, LOCKOUT_FAILS, AccountLockedError, AuthError, AuthStore, bearer_ok,
)

PASSWORD = "StrongPassword1!"


def _store(tmp_path) -> AuthStore:
    store = AuthStore(str(tmp_path / "users.db"), iterations=1)
    store.create_user("admin@example.com", "Admin", PASSWORD, "admin")
    return store


# ── typed lockout ───────────────────────────────────────────────────────────────


def test_per_email_lockout_raises_typed_error(tmp_path):
    store = _store(tmp_path)
    for _ in range(LOCKOUT_FAILS):
        with pytest.raises(AuthError):
            store.login("admin@example.com", "wrong-password")
    with pytest.raises(AccountLockedError):
        store.login("admin@example.com", PASSWORD)     # even the RIGHT password waits


def test_account_locked_is_an_auth_error(tmp_path):
    # Existing broad handlers (`except AuthError`) must keep catching lockouts.
    assert issubclass(AccountLockedError, AuthError)


# ── per-IP throttle (cross-email credential stuffing) ──────────────────────────


def test_ip_lockout_engages_across_distinct_emails(tmp_path):
    """A stuffing sweep tries each address once, so the per-email throttle never fires;
    the per-IP window must catch it instead."""
    store = _store(tmp_path)
    ip = "203.0.113.9"
    for i in range(IP_LOCKOUT_FAILS):
        with pytest.raises(AuthError):
            store.login(f"victim{i}@example.com", "wrong-password", ip=ip)
    with pytest.raises(AccountLockedError):
        store.login("admin@example.com", PASSWORD, ip=ip)
    # A DIFFERENT source is unaffected (no collateral lockout of the whole userbase).
    user = store.login("admin@example.com", PASSWORD, ip="198.51.100.7")
    assert user["email"] == "admin@example.com" and user["token"]


def test_successful_logins_never_accrue_ip_failures(tmp_path):
    store = _store(tmp_path)
    ip = "203.0.113.10"
    for _ in range(IP_LOCKOUT_FAILS + 5):
        assert store.login("admin@example.com", PASSWORD, ip=ip)["token"]


def test_ip_is_optional(tmp_path):
    store = _store(tmp_path)
    assert store.login("admin@example.com", PASSWORD)["token"]   # ip=None still works


# ── shared bearer helper ────────────────────────────────────────────────────────


def test_bearer_ok_happy_path_and_case_insensitive_scheme():
    assert bearer_ok("Bearer sekrit", "sekrit")
    assert bearer_ok("bearer sekrit", "sekrit")        # RFC 7235: scheme is case-insensitive
    assert bearer_ok("BEARER sekrit", "sekrit")


def test_bearer_ok_refuses_bad_or_empty_input():
    assert not bearer_ok("Bearer wrong", "sekrit")
    assert not bearer_ok("Bearer ", "sekrit")
    assert not bearer_ok("", "sekrit")
    assert not bearer_ok(None, "sekrit")
    assert not bearer_ok("sekrit", "sekrit")           # missing scheme
    assert not bearer_ok("Bearer sekrit", "")          # no configured token = closed
    assert not bearer_ok("Bearer sékrit", "sekrit")    # non-ASCII must not raise
