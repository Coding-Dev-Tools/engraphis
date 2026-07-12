"""Automatic cloud sync — engraphis/autosync.py (policy + runner) and the /api/sync/auto
endpoints behind the dashboard's "Sync automatically" toggle.

The policy tests are pure (numpy-only gate, no extras). The runner test needs the v2
MemoryService (numpy) and stubs the relay with a fake transport, so no network is touched.
The endpoint tests additionally need fastapi/httpx and skip cleanly without them — mirroring
tests/test_sync_dashboard.py.
"""
import time

import pytest

from engraphis import autosync

try:  # the /api/sync/auto endpoint tests need the full stack; the rest do not.
    import fastapi as _fa  # noqa: F401
    import httpx as _hx  # noqa: F401
    _STACK = True
except Exception:  # noqa: BLE001
    _STACK = False

skip_no_stack = pytest.mark.skipif(not _STACK, reason="full-stack extra not installed")

_SECRET = bytes(range(32))


def _key(plan="team", seats=5):
    from engraphis.licensing import compose_key
    return compose_key({"v": 1, "plan": plan, "email": "w@x.co", "seats": seats,
                        "issued": int(time.time()),
                        "expires": int(time.time() + 365 * 86400)}, _SECRET)


class _FakeTransport:
    """A SyncTransport that records pushes and pulls nothing (a one-device round-trip)."""

    def __init__(self):
        self.pushed = []

    def push(self, name, data):
        self.pushed.append((name, data))

    def pull(self):
        return []

    def list_names(self):
        return [n for n, _ in self.pushed]


# ── pure policy (no extras) ───────────────────────────────────────────────────────────
def test_default_policy_is_off_15min():
    assert autosync.normalize_policy({}) == {"enabled": False, "cadence_minutes": 15}


def test_normalize_clamps_and_coerces():
    assert autosync.normalize_policy({"cadence_minutes": 0})["cadence_minutes"] == 15
    assert autosync.normalize_policy({"cadence_minutes": 1})["cadence_minutes"] == 5  # floor
    assert autosync.normalize_policy({"cadence_minutes": "x"})["cadence_minutes"] == 15
    assert autosync.normalize_policy({"enabled": 1})["enabled"] is True
    assert autosync.normalize_policy("garbage") == {"enabled": False, "cadence_minutes": 15}


def test_five_minute_floor_cannot_be_beaten():
    # No caller — slip of the finger or crafted request — can drive the relay faster than
    # once every 5 minutes; sub-5 values (and 0) clamp up.
    for v in (0, 1, 2, 4, -3):
        assert autosync.normalize_policy({"cadence_minutes": v})["cadence_minutes"] >= 5


def test_sync_auto_toggle_is_admin_only_but_members_still_write():
    from engraphis.inspector.auth import min_role
    # Changing team auto-sync is admin-only; reading it stays viewer-visible.
    assert min_role("POST", "/api/sync/auto") == "admin"
    assert min_role("GET", "/api/sync/auto") == "viewer"
    # Members keep "store + view": they can write memories; viewers only read.
    assert min_role("POST", "/api/correct") == "member"
    assert min_role("GET", "/api/memories") == "viewer"


def test_save_load_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("ENGRAPHIS_AUTOSYNC_STATE", str(tmp_path / "autosync.json"))
    autosync.save_policy({"enabled": True, "cadence_minutes": 30})
    p = autosync.load_policy()
    assert p["enabled"] is True and p["cadence_minutes"] == 30
    assert p["last_run"] is None and p["last_result"] is None


def test_due_respects_enabled_and_cadence():
    now = 1_000_000.0
    assert autosync.due({"enabled": False}, now=now) is False           # off
    assert autosync.due({"enabled": True}, now=now) is True             # never run
    pol = {"enabled": True, "cadence_minutes": 15, "last_run": now - 10 * 60}
    assert autosync.due(pol, now=now) is False                          # 10 min < 15
    pol["last_run"] = now - 20 * 60
    assert autosync.due(pol, now=now) is True                           # 20 min >= 15


# ── runner (numpy stack; relay stubbed) ───────────────────────────────────────────────
def test_run_once_skips_without_license(tmp_path, monkeypatch):
    from engraphis import licensing as lic
    monkeypatch.setenv("ENGRAPHIS_AUTOSYNC_STATE", str(tmp_path / "autosync.json"))
    monkeypatch.setattr(lic, "_LICENSE_FILE", tmp_path / "license.key")
    monkeypatch.delenv("ENGRAPHIS_LICENSE_KEY", raising=False)
    lic.current_license(refresh=True)
    assert autosync.run_once(None).get("skipped") == "unlicensed"


def test_run_once_runs_records_and_updates_button_state(tmp_path, monkeypatch):
    from engraphis import licensing as lic
    from engraphis.config import settings
    from engraphis.licensing import ed25519_public_key
    from engraphis.service import MemoryService

    monkeypatch.setenv("ENGRAPHIS_AUTOSYNC_STATE", str(tmp_path / "autosync.json"))
    monkeypatch.setattr(settings, "embed_model", "")
    monkeypatch.setenv("ENGRAPHIS_EMBED_MODEL", "")
    monkeypatch.setattr(lic, "_LICENSE_FILE", tmp_path / "license.key")
    monkeypatch.setenv("ENGRAPHIS_LICENSE_KEY", _key())
    monkeypatch.setenv("ENGRAPHIS_LICENSE_PUBKEY", ed25519_public_key(_SECRET).hex())
    # online-only enforcement now requires a live vendor lease for paid features; this test
    # exercises autosync mechanics, so grant the sync entitlement directly (as test_analytics
    # does) rather than standing up a fake relay.
    monkeypatch.setattr("engraphis.licensing.has_feature", lambda f: True)
    lic.current_license(refresh=True)

    db = str(tmp_path / "auto.db")
    monkeypatch.setattr(settings, "db_path", db)
    svc = MemoryService.create(db)
    svc.remember("Postgres 16 is the main database.", workspace="demo",
                 scope="workspace", title="DB choice")
    from engraphis.routes import v2_api
    v2_api.set_service(svc)
    v2_api._SYNC_STATE.clear()
    monkeypatch.setattr("engraphis.backends.sync_folder.get_transport",
                        lambda kind="folder", **kw: _FakeTransport())

    summ = autosync.run_once(svc, now=2_000_000.0)
    assert summ["workspaces"] >= 1 and summ["exported"] >= 1 and summ["errors"] == []
    p = autosync.load_policy()
    assert p["last_run"] == 2_000_000.0
    assert p["last_result"]["exported"] >= 1
    # the same "last synced" line the Sync-now button shows now reflects the auto run
    assert v2_api._SYNC_STATE["last"]["exported"] >= 1


# ── /api/sync/auto endpoints (full stack) ─────────────────────────────────────────────
def _client(monkeypatch, tmp_path, *, key=None):
    from fastapi.testclient import TestClient
    from engraphis import licensing as lic
    from engraphis.config import settings
    from engraphis.licensing import ed25519_public_key
    from engraphis.service import MemoryService

    db = str(tmp_path / "dash.db")
    monkeypatch.setattr(settings, "db_path", db)
    monkeypatch.setattr(settings, "embed_model", "")
    monkeypatch.setenv("ENGRAPHIS_EMBED_MODEL", "")
    monkeypatch.setenv("ENGRAPHIS_TEAM_MODE", "")
    monkeypatch.setenv("ENGRAPHIS_AUTOSYNC_STATE", str(tmp_path / "autosync.json"))
    monkeypatch.setattr(lic, "_LICENSE_FILE", tmp_path / "license.key")
    if key:
        monkeypatch.setenv("ENGRAPHIS_LICENSE_KEY", key)
        monkeypatch.setenv("ENGRAPHIS_LICENSE_PUBKEY", ed25519_public_key(_SECRET).hex())
    else:
        monkeypatch.delenv("ENGRAPHIS_LICENSE_KEY", raising=False)
    lic.current_license(refresh=True)
    svc = MemoryService.create(db)
    from engraphis.routes import v2_api
    v2_api.set_service(svc)
    v2_api._SYNC_STATE.clear()
    from engraphis.dashboard_app import create_app
    return TestClient(create_app())


@skip_no_stack
def test_sync_auto_get_defaults_off(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path, key=_key()) as c:
        d = c.get("/api/sync/auto").json()
        assert d["enabled"] is False and d["cadence_minutes"] == 15
        assert d["last_run"] is None


@skip_no_stack
def test_sync_auto_set_requires_license(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as c:
        assert c.post("/api/sync/auto", json={"enabled": True}).status_code == 402


@skip_no_stack
def test_sync_auto_set_persists_and_clamps(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path, key=_key()) as c:
        r = c.post("/api/sync/auto", json={"enabled": True, "cadence_minutes": 1})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["enabled"] is True and body["cadence_minutes"] == 5  # floored
        # a follow-up read reflects the persisted toggle
        assert c.get("/api/sync/auto").json()["enabled"] is True
