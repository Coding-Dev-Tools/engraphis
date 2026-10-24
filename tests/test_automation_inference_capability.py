"""Unsupported hosted inference cannot cause a snapshot or remote policy write."""
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi", reason="full-stack extra not installed")
pytest.importorskip("httpx", reason="full-stack extra not installed")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from engraphis import cloud_features  # noqa: E402
from engraphis.routes import v2_api  # noqa: E402


def _client():
    app = FastAPI()
    app.include_router(v2_api.router)
    return TestClient(app)


@pytest.mark.parametrize("body", [
    {"infer": True},
    {"infer": True, "enabled": True},
    {"infer": True, "enabled": False},
])
def test_inference_refused_before_local_or_hosted_work(monkeypatch, body):
    calls = []

    def forbidden(*args, **kwargs):
        calls.append("unexpected work")
        raise AssertionError("unsupported inference reached local or hosted work")

    monkeypatch.setattr(v2_api, "service", forbidden)
    monkeypatch.setattr(cloud_features.CloudFeatureClient, "from_environment", forbidden)
    monkeypatch.setattr(cloud_features, "build_managed_snapshot", forbidden)
    with _client() as client:
        response = client.post("/api/automation?workspace=chosen", json=body)

    assert response.status_code == 422
    assert response.json() == {"detail": {
        "error": "Hosted relationship inference is not available.",
        "code": "inference_unavailable",
    }}
    assert calls == []


@pytest.mark.parametrize("explicit_false", [False, True])
def test_supported_partial_save_never_inherits_legacy_inference(monkeypatch, explicit_false):
    saved = []
    bound_workspaces = []
    service = SimpleNamespace(
        _clean_ws=lambda value: value,
        _lookup_workspace=lambda value: "ws_" + value,
    )

    class Cloud:
        def get_policy(self, workspace_id):
            assert workspace_id == "ws_chosen"
            return {
                "enabled": False, "cadence_minutes": 60,
                "dream_enabled": True, "dream_min_new": 25,
                "dream_idle_minutes": 0, "infer": True,
            }

        def save_policy(self, workspace_id, policy):
            saved.append((workspace_id, policy))
            return {"version": 2}

        def upload_snapshot(self, *args, **kwargs):
            raise AssertionError("disabled supported save uploaded a snapshot")

    def from_environment(workspace_id=None):
        bound_workspaces.append(workspace_id)
        return Cloud()

    def forbidden_snapshot(*args, **kwargs):
        raise AssertionError("disabled supported save built a snapshot")

    monkeypatch.setattr(v2_api, "service", lambda: service)
    monkeypatch.setattr(
        cloud_features.CloudFeatureClient, "from_environment", from_environment,
    )
    monkeypatch.setattr(cloud_features, "build_managed_snapshot", forbidden_snapshot)
    body = {"enabled": False}
    if explicit_false:
        body["infer"] = False
    with _client() as client:
        response = client.post("/api/automation?workspace=chosen", json=body)

    assert response.status_code == 200
    assert response.json()["infer"] is False
    assert bound_workspaces == ["ws_chosen"]
    assert len(saved) == 1
    assert saved[0][0] == "ws_chosen"
    assert saved[0][1]["infer"] is False
    assert saved[0][1]["enabled"] is False
