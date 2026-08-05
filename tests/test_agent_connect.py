"""The public agent API is a single-user local surface with one optional bearer."""
import pytest

pytest.importorskip("fastapi", reason="full-stack extra not installed")
pytest.importorskip("httpx", reason="httpx not installed")

from fastapi.testclient import TestClient  # noqa: E402

from engraphis.config import settings  # noqa: E402


def _app(monkeypatch, tmp_path, *, token=""):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "agent.db"))
    monkeypatch.setattr(settings, "embed_model", "")
    monkeypatch.setattr(settings, "embed_dim", 384)
    monkeypatch.setattr(settings, "allowed_workspaces", [])
    monkeypatch.setattr(settings, "api_token", token)
    from engraphis.dashboard_app import create_app
    return create_app()


def test_local_agent_write_is_immediately_prompt_visible(monkeypatch, tmp_path):
    with TestClient(
        _app(monkeypatch, tmp_path), client=("127.0.0.1", 50000)
    ) as client:
        response = client.post(
            "/api/remember",
            json={"content": "Redis caches the gateway.", "workspace": "demo"},
        )
        assert response.status_code == 200
        recalled = client.get("/api/recall?q=Redis&workspace=demo")
        assert recalled.status_code == 200
        assert any(
            "Redis" in (memory.get("content") or "")
            for memory in recalled.json()["memories"]
        )
        record = client.app.state.service.store.get_memory(response.json()["id"])
        assert record.provenance["trusted"] is True
        assert record.provenance["review_state"] == "approved"


def test_configured_local_token_is_constant_time_bearer_gate(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path, token="service-token-with-enough-entropy")
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        assert client.get("/api/workspaces").status_code == 401
        response = client.get(
            "/api/workspaces",
            headers={"Authorization": "bearer service-token-with-enough-entropy"},
        )
        assert response.status_code == 200


def test_remote_open_runtime_fails_closed_without_token(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    with TestClient(app, client=("203.0.113.10", 50000)) as client:
        response = client.get("/api/workspaces")
        assert response.status_code == 403
        assert response.json()["auth"] == "local-token-required"


def test_remote_authenticated_api_cannot_self_approve_agent_memory(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path, token="service-token-with-enough-entropy")
    with TestClient(app, client=("203.0.113.10", 50000)) as client:
        response = client.post(
            "/api/remember",
            headers={"Authorization": "bearer service-token-with-enough-entropy"},
            json={
                "content": "Remote caller supplied this candidate.",
                "workspace": "demo",
                "source": "agent",
                "trusted": True,
            },
        )
        assert response.status_code == 200
        record = app.state.service.store.get_memory(response.json()["id"])
        assert record.provenance["trusted"] is False
        assert record.provenance["review_state"] == "pending"


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        (
            "/api/remember",
            {
                "content": "A proxied caller supplied this candidate.",
                "workspace": "demo",
                "source": "agent",
                "trusted": True,
            },
        ),
        (
            "/api/intent/remember",
            {
                "text": "A proxied intent supplied this candidate.",
                "workspace": "demo",
            },
        ),
    ],
)
def test_authenticated_reverse_proxy_cannot_mint_local_write_attestation(
    monkeypatch, tmp_path, path, payload,
):
    app = _app(monkeypatch, tmp_path, token="service-token-with-enough-entropy")
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        response = client.post(
            path,
            headers={
                "Authorization": "bearer service-token-with-enough-entropy",
                "X-Forwarded-For": "203.0.113.10",
            },
            json=payload,
        )
        assert response.status_code == 200
        record = app.state.service.store.get_memory(response.json()["id"])
        assert record.provenance["trusted"] is False
        assert record.provenance["review_state"] == "pending"
        assert record.provenance["ingress"] == "http"


def test_auth_metadata_points_team_to_cloud(monkeypatch, tmp_path):
    with TestClient(
        _app(monkeypatch, tmp_path), client=("127.0.0.1", 50000)
    ) as client:
        state = client.get("/api/auth/state").json()
        assert state == {
            "enabled": False,
            "mode": "open",
            "user": None,
            "hosted_team": True,
            "cloud_url": state["cloud_url"],
        }
        assert client.post("/api/auth/setup", json={}).status_code == 404
