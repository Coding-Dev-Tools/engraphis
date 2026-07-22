"""Durable paid-entitlement lapse state and opaque deployment identity."""

import re

from engraphis.inspector.auth import (
    ENTITLEMENT_ACTIVE,
    ENTITLEMENT_RECOVERY,
    ENTITLEMENT_WRITE_GRACE,
    AuthStore,
    entitlement_grace_seconds,
)
from engraphis.licensing import License


PASSWORD = "Correct-horse-1"


def test_entitlement_grace_is_24_hours_by_default_and_cannot_be_extended(monkeypatch):
    monkeypatch.delenv("ENGRAPHIS_ENTITLEMENT_GRACE_HOURS", raising=False)
    assert entitlement_grace_seconds() == 24 * 3600

    monkeypatch.setenv("ENGRAPHIS_ENTITLEMENT_GRACE_HOURS", "999")
    assert entitlement_grace_seconds() == 24 * 3600

    monkeypatch.setenv("ENGRAPHIS_ENTITLEMENT_GRACE_HOURS", "6")
    assert entitlement_grace_seconds() == 6 * 3600


def test_entitlement_lapse_is_expiry_anchored_restart_safe_and_clock_monotonic(
        monkeypatch, tmp_path):
    monkeypatch.delenv("ENGRAPHIS_ENTITLEMENT_GRACE_HOURS", raising=False)
    path = str(tmp_path / "users.db")
    store = AuthStore(path, iterations=1)
    store.create_user("admin@example.com", "Admin", PASSWORD, "admin")
    paid = License(plan="team", expires=2_000, key_id="key-one")

    active = store.entitlement_access(paid, now=1_000)
    assert active["state"] == ENTITLEMENT_ACTIVE
    assert active["grace_until"] is None

    # The first denial is observed after signed expiry, so the 24-hour window starts at
    # that signed deadline, not at an arbitrarily late process restart.
    grace = store.entitlement_access(License.free(), now=2_500)
    assert grace["state"] == ENTITLEMENT_WRITE_GRACE
    assert grace["grace_started_at"] == 2_000
    assert grace["grace_until"] == 2_000 + 24 * 3600
    deadline = grace["grace_until"]
    store.conn.close()

    reopened = AuthStore(path, iterations=1)
    resumed = reopened.entitlement_access(License.free(), now=3_000)
    assert resumed["state"] == ENTITLEMENT_WRITE_GRACE
    assert resumed["grace_until"] == deadline

    recovery = reopened.entitlement_access(License.free(), now=deadline)
    assert recovery["state"] == ENTITLEMENT_RECOVERY
    assert recovery["workspace_writes_allowed"] is False
    # A wall-clock rollback after restart cannot reopen the consumed window.
    rolled_back = reopened.entitlement_access(License.free(), now=100)
    assert rolled_back["state"] == ENTITLEMENT_RECOVERY

    renewed = reopened.entitlement_access(
        License(plan="team", expires=deadline + 100_000, key_id="key-two"),
        now=deadline + 1,
    )
    assert renewed["state"] == ENTITLEMENT_ACTIVE
    assert renewed["grace_until"] is None
    reopened.conn.close()


def test_organization_id_is_opaque_and_stable_across_restart(tmp_path):
    path = str(tmp_path / "users.db")
    first = AuthStore(path, iterations=1)
    organization_id = first.organization_id()
    assert re.fullmatch(r"org_[0-9a-f]{32}", organization_id)
    first.conn.close()

    reopened = AuthStore(path, iterations=1)
    assert reopened.organization_id() == organization_id
    reopened.conn.close()
