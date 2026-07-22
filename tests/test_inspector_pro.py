"""Open-core boundary checks for the legacy Inspector compatibility API."""

import pytest

pytest.importorskip("fastapi", reason="full-stack extra not installed")
from fastapi.testclient import TestClient  # noqa: E402

from engraphis.config import settings  # noqa: E402
from engraphis.inspector.app import create_app  # noqa: E402
from engraphis.service import MemoryService  # noqa: E402


@pytest.fixture()
def make_client(monkeypatch):
    def _make(*, token: str = "", legacy_auth_store=None):
        monkeypatch.setattr(settings, "api_token", token)
        service = MemoryService.create(":memory:")
        stored = service.remember(
            "The rate limit is 500 rpm.", workspace="acme", repo="api"
        )
        app = create_app(service, legacy_auth_store)
        return app, TestClient(app), stored["id"]

    return _make


def test_inspector_is_single_user_open_or_bearer_only(make_client):
    app, client, _ = make_client(legacy_auth_store=object())
    state = client.get("/api/auth/state")
    assert state.status_code == 200
    assert state.json() == {
        "mode": "open",
        "enabled": False,
        "user": None,
        "local_multi_user": False,
        "team": {"available_locally": False, "mode": "hosted_cloud"},
    }
    assert not hasattr(app.state, "auth_store")
    assert client.get(
        "/api/recall", params={"q": "rate", "workspace": "acme"}
    ).status_code == 200


def test_optional_api_token_gates_local_data_but_not_health_or_state(make_client):
    _, client, _ = make_client(token="correct-token")

    assert client.get("/api/health").status_code == 200
    state = client.get("/api/auth/state")
    assert state.status_code == 200
    assert state.json()["mode"] == "token"
    assert state.json()["enabled"] is True

    params = {"q": "rate", "workspace": "acme"}
    assert client.get("/api/recall", params=params).status_code == 401
    assert client.get(
        "/api/recall",
        params=params,
        headers={"Authorization": "Bearer wrong-token"},
    ).status_code == 401
    assert client.get(
        "/api/recall",
        params=params,
        headers={"Authorization": "Bearer correct-token"},
    ).status_code == 200


@pytest.mark.parametrize(
    ("method", "path", "feature"),
    [
        ("post", "/api/auth/setup", "team"),
        ("post", "/api/auth/login", "team"),
        ("get", "/api/auth/users", "team"),
        ("get", "/api/license", "license"),
        ("post", "/api/license/activate", "license"),
        ("get", "/api/analytics?workspace=acme", "analytics"),
        ("get", "/api/analytics/portfolio", "analytics"),
        ("get", "/api/automation?workspace=acme", "automation"),
        ("post", "/api/automation/run", "automation"),
    ],
)
def test_hosted_features_have_no_local_authority(make_client, method, path, feature):
    _, client, _ = make_client()
    response = getattr(client, method)(path)
    assert response.status_code == 501
    assert response.json() == {
        "error": f"{feature} is available only through Engraphis Cloud",
        "feature": feature,
        "cloud_only": True,
    }


def test_local_owner_can_export_their_data_without_a_local_license_issuer(make_client):
    _, client, _ = make_client()
    response = client.get("/api/export", params={"workspace": "acme"})
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    payload = response.json()
    assert payload["format"] == "engraphis-export/1"
    assert payload["counts"]["memories"] == 1


def test_manual_consolidation_remains_local_but_automatic_policy_does_not(make_client):
    _, client, _ = make_client()
    manual = client.post(
        "/api/consolidate", json={"workspace": "acme", "dry_run": True}
    )
    assert manual.status_code == 200
    assert manual.json()["dry_run"] is True

    automatic = client.post("/api/automation/run")
    assert automatic.status_code == 501
    assert automatic.json()["cloud_only"] is True
