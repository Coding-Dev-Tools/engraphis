"""Workspace approval is explicit, durable and cannot come from legacy settings."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from engraphis.cloud_features import CloudFeatureError, build_managed_snapshot
from engraphis.service import MemoryService


@pytest.fixture
def _http_stack():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")


@pytest.fixture
def svc(tmp_path, monkeypatch):
    monkeypatch.delenv("ENGRAPHIS_MANAGED_COMPUTE_CONSENT", raising=False)
    service = MemoryService.create(str(tmp_path / "policy.db"))
    service.remember("A useful fact", workspace="a")
    service.remember("A different fact", workspace="b")
    yield service
    service.engine.close()


def test_legacy_state_requires_confirmation_and_preserves_data(svc, monkeypatch):
    monkeypatch.setenv("ENGRAPHIS_MANAGED_COMPUTE_CONSENT", "true")
    before = svc.store.conn.execute("SELECT count(*) FROM memories").fetchone()[0]
    policy = svc.managed_processing_policy("a")
    assert policy["confirmation_required"] and not policy["enabled"]
    with pytest.raises(CloudFeatureError, match="approval"):
        build_managed_snapshot(svc, "a", consent=True)
    assert svc.store.conn.execute("SELECT count(*) FROM memories").fetchone()[0] == before


def test_workspace_isolation_restart_and_optout(svc):
    with pytest.raises(ValueError, match="explicit"):
        svc.set_managed_processing_policy("a", enabled=True)
    svc.set_managed_processing_policy("a", enabled=True, confirmed=True, remote_revision=2)
    assert svc.managed_processing_policy("a")["enabled"]
    assert not svc.managed_processing_policy("b")["enabled"]
    other = MemoryService.create(svc.store.path)
    try:
        assert other.managed_processing_policy("a")["remote_revision"] == 2
        other.set_managed_processing_policy("a", enabled=False)
        assert not svc.managed_processing_policy("a")["enabled"]
        with pytest.raises(CloudFeatureError):
            build_managed_snapshot(svc, "a")
    finally:
        other.engine.close()


@pytest.mark.parametrize("revision", ["bad", True, -1, None])
def test_corrupt_state_fails_closed_and_can_be_reconfirmed(svc, revision):
    wid = svc._lookup_workspace("a")
    svc.store.conn.execute(
        "INSERT INTO sync_state(key,value,updated_at) VALUES (?,?,0)",
        ("managed_processing_policy:" + wid, json.dumps({
            "schema": "engraphis-managed-processing/v1", "revision": revision,
            "enabled": True, "confirmed": True,
        })),
    )
    svc.store.conn.commit()
    assert not svc.managed_processing_policy("a")["enabled"]
    assert svc.set_managed_processing_policy("a", enabled=True, confirmed=True)["enabled"]


@pytest.mark.usefixtures("_http_stack")
def test_http_acknowledgement_and_failed_optout(svc, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from engraphis.routes import v2_api
    from engraphis.cloud_features import CloudFeatureClient

    class Cloud:
        fail = False
        calls = []

        def get_processing_policy(self, wid):
            return {"enabled": False, "revision": 1}

        def set_processing_policy(self, wid, **kwargs):
            self.calls.append((wid, kwargs))
            if self.fail:
                raise CloudFeatureError("unavailable", status=503)
            return {"enabled": kwargs["enabled"], "revision": 2}

    cloud = Cloud()
    monkeypatch.setattr(CloudFeatureClient, "from_environment", lambda _: cloud)
    monkeypatch.setattr(v2_api, "service", lambda: svc)
    app = FastAPI()
    app.include_router(v2_api.router)
    with TestClient(app) as client:
        assert client.post("/api/managed-processing", json={
            "workspace": "a", "enabled": True,
        }).status_code == 400
        assert cloud.calls == []
        for payload in ({"enabled": "true"}, {"enabled": True, "confirmed": "true"}):
            assert client.post("/api/managed-processing", json={
                "workspace": "a", **payload,
            }).status_code == 422
        cloud.fail = True
        assert client.post("/api/managed-processing", json={
            "workspace": "a", "enabled": True, "confirmed": True,
        }).status_code == 503
        assert not svc.managed_processing_policy("a")["enabled"]
        cloud.fail = False
        enabled = client.post("/api/managed-processing", json={
            "workspace": "a", "enabled": True, "confirmed": True,
        }).json()
        assert enabled["enabled"] and enabled["remote_revision"] == 2
        cloud.fail = True
        disabled = client.post("/api/managed-processing", json={
            "workspace": "a", "enabled": False,
        }).json()
        assert not disabled["enabled"] and disabled["remote_sync_pending"]
        assert "may continue" in disabled["notice"]


@pytest.mark.usefixtures("_http_stack")
def test_delayed_enable_acknowledgement_cannot_overwrite_newer_optout(svc, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from engraphis.routes import v2_api
    from engraphis.cloud_features import CloudFeatureClient

    enable_started, release_enable = threading.Event(), threading.Event()

    class Cloud:
        def get_processing_policy(self, wid):
            return {"enabled": False, "revision": 1}

        def set_processing_policy(self, wid, **kwargs):
            if kwargs["enabled"]:
                enable_started.set()
                assert release_enable.wait(10)
                return {"enabled": True, "revision": 1}
            return {"enabled": False, "revision": 2}

    monkeypatch.setattr(CloudFeatureClient, "from_environment", lambda _: Cloud())
    monkeypatch.setattr(v2_api, "service", lambda: svc)
    app = FastAPI()
    app.include_router(v2_api.router)
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=1) as pool:
        enabling = pool.submit(client.post, "/api/managed-processing", json={
            "workspace": "a", "enabled": True, "confirmed": True,
        })
        try:
            assert enable_started.wait(10)
            disabled = client.post("/api/managed-processing", json={
                "workspace": "a", "enabled": False,
            })
            assert disabled.status_code == 200
            assert not disabled.json()["enabled"]
        finally:
            release_enable.set()
        rejected = enabling.result(timeout=10)
        assert rejected.status_code == 409
        assert rejected.json()["detail"]["code"] == "processing_policy_changed"
    policy = svc.managed_processing_policy("a")
    assert not policy["enabled"] and policy["remote_revision"] == 2
    with pytest.raises(CloudFeatureError, match="approval"):
        build_managed_snapshot(svc, "a")


@pytest.mark.parametrize("delay_at", ["get", "put"])
@pytest.mark.usefixtures("_http_stack")
def test_newer_optout_fences_delayed_cloud_enable(svc, monkeypatch, delay_at):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from engraphis.cloud_features import CloudFeatureClient
    from engraphis.routes import v2_api

    started, release = threading.Event(), threading.Event()

    class Cloud:
        revision = 1
        enabled = False
        gets = 0
        puts = []

        def get_processing_policy(self, wid):
            self.gets += 1
            if delay_at == "get" and self.gets == 1:
                started.set()
                assert release.wait(10)
            return {"enabled": self.enabled, "revision": self.revision}

        def set_processing_policy(self, wid, **kwargs):
            self.puts.append(kwargs)
            if delay_at == "put" and kwargs["enabled"]:
                started.set()
                assert release.wait(10)
            if kwargs["revision"] != self.revision:
                raise CloudFeatureError("stale revision", status=409)
            self.revision += 1
            self.enabled = kwargs["enabled"]
            return {"enabled": self.enabled, "revision": self.revision}

    cloud = Cloud()
    monkeypatch.setattr(CloudFeatureClient, "from_environment", lambda _: cloud)
    monkeypatch.setattr(v2_api, "service", lambda: svc)
    app = FastAPI()
    app.include_router(v2_api.router)
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=1) as pool:
        enabling = pool.submit(client.post, "/api/managed-processing", json={
            "workspace": "a", "enabled": True, "confirmed": True,
        })
        try:
            assert started.wait(10)
            disabled = client.post("/api/managed-processing", json={
                "workspace": "a", "enabled": False,
            })
            assert disabled.status_code == 200
            assert not disabled.json()["remote_sync_pending"]
        finally:
            release.set()
        assert enabling.result(timeout=10).status_code == 409
    assert not cloud.enabled and cloud.revision == 2
    assert not svc.managed_processing_policy("a")["enabled"]
    assert sum(call["enabled"] for call in cloud.puts) == (delay_at == "put")


@pytest.mark.usefixtures("_http_stack")
def test_optout_retries_remote_conflict_while_local_intent_is_current(svc, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from engraphis.cloud_features import CloudFeatureClient
    from engraphis.routes import v2_api

    enable_started, release_enable, enable_applied = (
        threading.Event(), threading.Event(), threading.Event()
    )

    class Cloud:
        revision = 1
        enabled = False
        off_revisions = []

        def get_processing_policy(self, wid):
            return {"enabled": self.enabled, "revision": self.revision}

        def set_processing_policy(self, wid, **kwargs):
            if kwargs["enabled"]:
                enable_started.set()
                assert release_enable.wait(10)
            else:
                self.off_revisions.append(kwargs["revision"])
                if len(self.off_revisions) == 1:
                    release_enable.set()
                    assert enable_applied.wait(10)
            if kwargs["revision"] != self.revision:
                raise CloudFeatureError("stale revision", status=409)
            self.revision += 1
            self.enabled = kwargs["enabled"]
            if self.enabled:
                enable_applied.set()
            return {"enabled": self.enabled, "revision": self.revision}

    cloud = Cloud()
    monkeypatch.setattr(CloudFeatureClient, "from_environment", lambda _: cloud)
    monkeypatch.setattr(v2_api, "service", lambda: svc)
    app = FastAPI()
    app.include_router(v2_api.router)
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=1) as pool:
        enabling = pool.submit(client.post, "/api/managed-processing", json={
            "workspace": "a", "enabled": True, "confirmed": True,
        })
        try:
            assert enable_started.wait(10)
            disabled = client.post("/api/managed-processing", json={
                "workspace": "a", "enabled": False,
            })
            assert disabled.status_code == 200
            assert not disabled.json()["remote_sync_pending"]
        finally:
            release_enable.set()
        assert enabling.result(timeout=10).status_code == 409
    assert cloud.off_revisions == [1, 2]
    assert not cloud.enabled and cloud.revision == 3
    assert svc.managed_processing_policy("a")["remote_revision"] == 3


@pytest.mark.usefixtures("_http_stack")
def test_optout_does_not_retry_after_newer_local_approval(svc, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from engraphis.cloud_features import CloudFeatureClient
    from engraphis.routes import v2_api

    class Cloud:
        calls = 0

        def get_processing_policy(self, wid):
            return {"enabled": False, "revision": 1}

        def set_processing_policy(self, wid, **kwargs):
            self.calls += 1
            svc.set_managed_processing_policy(
                "a", enabled=True, confirmed=True, remote_revision=2)
            raise CloudFeatureError("stale revision", status=409)

    cloud = Cloud()
    monkeypatch.setattr(CloudFeatureClient, "from_environment", lambda _: cloud)
    monkeypatch.setattr(v2_api, "service", lambda: svc)
    app = FastAPI()
    app.include_router(v2_api.router)
    with TestClient(app) as client:
        disabled = client.post("/api/managed-processing", json={
            "workspace": "a", "enabled": False,
        })
    assert disabled.status_code == 409 and cloud.calls == 1
    assert svc.managed_processing_policy("a")["enabled"]


def test_cloud_client_sends_required_revision(monkeypatch):
    from engraphis.cloud_features import CloudFeatureClient

    calls = []
    monkeypatch.setattr(CloudFeatureClient, "_request", lambda *args: calls.append(args[1:]) or {})
    cloud = CloudFeatureClient("https://compute.example", "org_test", "test-token")
    cloud.set_processing_policy("ws_test", enabled=True, confirmed=True, revision=7)
    assert calls == [("PUT", "/v1/organizations/org_test/workspaces/ws_test/processing-policy", {
        "enabled": True, "confirmed": True, "revision": 7,
    })]
    with pytest.raises(CloudFeatureError, match="revision"):
        cloud.set_processing_policy("ws_test", enabled=False, revision=True)
    assert len(calls) == 1


@pytest.mark.usefixtures("_http_stack")
def test_expired_cloud_session_stops_local_uploads_with_remote_pending(svc, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from engraphis.cloud_features import CloudFeatureClient
    from engraphis.routes import v2_api

    svc.set_managed_processing_policy("a", enabled=True, confirmed=True, remote_revision=2)

    def expired(_):
        raise CloudFeatureError("active cloud entitlement required", status=402)

    monkeypatch.setattr(CloudFeatureClient, "from_environment", expired)
    monkeypatch.setattr(v2_api, "service", lambda: svc)
    app = FastAPI()
    app.include_router(v2_api.router)
    with TestClient(app) as client:
        disabled = client.post("/api/managed-processing", json={
            "workspace": "a", "enabled": False,
        })
    assert disabled.status_code == 200
    assert not disabled.json()["enabled"] and disabled.json()["remote_sync_pending"]
    assert "may continue" in disabled.json()["notice"]
    with pytest.raises(CloudFeatureError, match="approval"):
        build_managed_snapshot(svc, "a")
