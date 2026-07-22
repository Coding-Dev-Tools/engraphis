"""Dashboard one-click cloud sync — the /api/sync/* endpoints behind the "Sync now" button.

Skips on the numpy-only gate (needs fastapi/httpx). Uses the deterministic embedder and a
fresh DB per test, mirroring tests/test_dashboard_v2.py. The relay is stubbed with a fake
transport so no network is touched; the real client↔server round-trip is covered by
tests/test_sync_relay.py.
"""
import pytest

pytest.importorskip("fastapi", reason="full-stack extra not installed")
pytest.importorskip("httpx", reason="httpx not installed")

from fastapi.testclient import TestClient  # noqa: E402

from engraphis.config import DEFAULT_RELAY_URL, settings  # noqa: E402
from engraphis.service import MemoryService  # noqa: E402


def _client(monkeypatch, tmp_path, *, cloud=False):
    db = str(tmp_path / "dash.db")
    monkeypatch.setattr(settings, "db_path", db)
    monkeypatch.setattr(settings, "embed_model", "")
    monkeypatch.setenv("ENGRAPHIS_EMBED_MODEL", "")
    monkeypatch.setenv("ENGRAPHIS_STATE_DIR", str(tmp_path / "state"))
    if cloud:
        monkeypatch.setenv("ENGRAPHIS_CLOUD_ACCESS_TOKEN", "cloud-access-token-" + "x" * 32)
        monkeypatch.setenv("ENGRAPHIS_CLOUD_ORGANIZATION_ID", "org_test")
    else:
        monkeypatch.delenv("ENGRAPHIS_CLOUD_ACCESS_TOKEN", raising=False)
        monkeypatch.delenv("ENGRAPHIS_CLOUD_ORGANIZATION_ID", raising=False)
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


def test_sync_status_migrates_retired_relay_url(monkeypatch, tmp_path):
    monkeypatch.setattr(
        settings, "relay_url", "https://engraphis-production.up.railway.app/")
    with _client(monkeypatch, tmp_path) as c:
        assert c.get("/api/sync/status").json()["relay_url"] == DEFAULT_RELAY_URL


def test_sync_run_requires_license(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as c:
        assert c.post("/api/sync/run", json={}).status_code == 402


def test_sync_status_ready_with_cloud_session(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path, cloud=True) as c:
        d = c.get("/api/sync/status").json()
        assert d["available"] is True
        assert d["has_key"] is False
        assert d["has_cloud_session"] is True


def test_sync_run_pushes_and_records_summary(monkeypatch, tmp_path):
    captured = {}

    def fake_get_transport(kind="folder", **kw):
        captured["kind"] = kind
        captured["kw"] = kw
        return _FakeTransport()

    monkeypatch.setattr("engraphis.backends.sync_folder.get_transport", fake_get_transport)
    with _client(monkeypatch, tmp_path, cloud=True) as c:
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


def test_sync_never_pushes_personal_folders(monkeypatch, tmp_path):
    """A personal folder is private to its owner and must never leave the device over the
    shared-account relay (teammates share the license key). _sync_all skips it; only shared
    folders are namespaced onto the relay."""
    synced = []

    def fake_get_transport(kind="folder", **kw):
        synced.append(kw.get("workspace_id"))
        return _FakeTransport()

    monkeypatch.setattr("engraphis.backends.sync_folder.get_transport", fake_get_transport)
    with _client(monkeypatch, tmp_path, cloud=True) as c:
        # seed a shared and a personal folder directly on the service the app uses
        from engraphis.routes import v2_api
        from engraphis.service import set_current_user
        svc = v2_api.service()
        svc.create_workspace("team-shared", visibility="shared", confirmed=True)
        set_current_user({"email": "owner@x.co", "role": "admin", "id": "o1"})
        try:
            svc.create_workspace("my-personal", visibility="personal")
        finally:
            set_current_user(None)
        assert c.post("/api/sync/run", json={}).status_code == 200
        assert "team-shared" in synced           # shared folders sync
        assert "demo" in synced                   # the seeded shared folder too
        assert "my-personal" not in synced        # ...personal folders never do


def test_sync_fails_closed_on_invalid_workspace_visibility(monkeypatch, tmp_path):
    synced = []

    def fake_get_transport(kind="folder", **kw):
        synced.append(kw.get("workspace_id"))
        return _FakeTransport()

    monkeypatch.setattr("engraphis.backends.sync_folder.get_transport", fake_get_transport)
    with _client(monkeypatch, tmp_path, cloud=True) as c:
        from engraphis.routes import v2_api
        svc = v2_api.service()
        svc.store.conn.execute(
            "UPDATE workspaces SET settings=? WHERE name='demo'",
            ('{"visibility":"corrupt-value"}',),
        )
        svc.store.conn.commit()

        response = c.post("/api/sync/run", json={})
        assert response.status_code == 200
        summary = response.json()["summary"]
        assert "demo" not in synced
        assert any(
            error["workspace"] == "demo" and "visibility is invalid" in error["error"]
            for error in summary["errors"]
        )
