"""Dashboard one-click cloud sync — the /api/sync/* endpoints behind the "Sync now" button.

Skips on the numpy-only gate (needs fastapi/httpx). Uses the deterministic embedder and a
fresh DB per test, mirroring tests/test_dashboard_v2.py. The relay is stubbed with a fake
transport so no network is touched; the real client↔server round-trip is covered by
tests/test_sync_relay.py.
"""
import time

import pytest

pytest.importorskip("fastapi", reason="full-stack extra not installed")
pytest.importorskip("httpx", reason="httpx not installed")

from fastapi.testclient import TestClient  # noqa: E402

from engraphis import licensing as lic  # noqa: E402
from engraphis.config import settings  # noqa: E402
from engraphis.licensing import compose_key, ed25519_public_key  # noqa: E402
from engraphis.service import MemoryService  # noqa: E402

_SECRET = bytes(range(32))


def _key(plan="team", seats=5):
    return compose_key({"v": 1, "plan": plan, "email": "w@x.co", "seats": seats,
                        "issued": int(time.time()),
                        "expires": int(time.time() + 365 * 86400)}, _SECRET)


def _client(monkeypatch, tmp_path, *, key=None):
    db = str(tmp_path / "dash.db")
    monkeypatch.setattr(settings, "db_path", db)
    monkeypatch.setattr(settings, "embed_model", "")
    monkeypatch.setenv("ENGRAPHIS_EMBED_MODEL", "")
    monkeypatch.setenv("ENGRAPHIS_TEAM_MODE", "")
    monkeypatch.setattr(lic, "_LICENSE_FILE", tmp_path / "license.key")
    if key:
        monkeypatch.setenv("ENGRAPHIS_LICENSE_KEY", key)
        monkeypatch.setenv("ENGRAPHIS_LICENSE_PUBKEY", ed25519_public_key(_SECRET).hex())
    else:
        monkeypatch.delenv("ENGRAPHIS_LICENSE_KEY", raising=False)
    lic.current_license(refresh=True)
    svc = MemoryService.create(db)
    svc.remember("Postgres 16 is the main database.", workspace="demo",
                 scope="workspace", title="DB choice")
    from engraphis.routes import v2_api
    v2_api.set_service(svc)
    v2_api._SYNC_STATE.clear()
    from engraphis.dashboard_app import create_app
    return TestClient(create_app())


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


def test_sync_status_locked_without_key(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as c:
        d = c.get("/api/sync/status").json()
        assert d["available"] is False        # no plan/key → button shows "sign in"
        assert d["has_key"] is False
        assert d["relay_url"].startswith("https://")   # defaults to the managed relay
        assert d["last"] is None


def test_sync_run_requires_license(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as c:
        assert c.post("/api/sync/run", json={}).status_code == 402


def test_sync_status_ready_with_key(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path, key=_key()) as c:
        d = c.get("/api/sync/status").json()
        assert d["available"] is True
        assert d["has_key"] is True


def test_sync_run_pushes_and_records_summary(monkeypatch, tmp_path):
    captured = {}

    def fake_get_transport(kind="folder", **kw):
        captured["kind"] = kind
        captured["kw"] = kw
        return _FakeTransport()

    monkeypatch.setattr("engraphis.backends.sync_folder.get_transport", fake_get_transport)
    with _client(monkeypatch, tmp_path, key=_key()) as c:
        r = c.post("/api/sync/run", json={})
        assert r.status_code == 200, r.text
        su = r.json()["summary"]
        assert su["workspaces"] >= 1
        assert su["exported"] >= 1                       # the seeded memory was pushed
        assert su["errors"] == []
        # the relay transport is namespaced by workspace NAME, at the managed relay
        assert captured["kind"] == "relay"
        assert captured["kw"]["workspace_id"] == "demo"
        assert captured["kw"]["base_url"].startswith("https://")
        # the button's status now reflects the last sync
        st = c.get("/api/sync/status").json()
        assert st["last"] and st["last"]["exported"] >= 1
