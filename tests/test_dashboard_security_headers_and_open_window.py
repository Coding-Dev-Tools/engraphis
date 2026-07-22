"""Security headers and the loopback-or-token local dashboard boundary."""
import pytest

pytest.importorskip("fastapi", reason="full-stack extra not installed")
pytest.importorskip("httpx", reason="httpx not installed")

from fastapi.testclient import TestClient  # noqa: E402

from engraphis.config import settings  # noqa: E402


def _client(monkeypatch, tmp_path, *, api_token="", client_addr=("127.0.0.1", 50000)):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "security.db"))
    monkeypatch.setattr(settings, "embed_model", "")
    monkeypatch.setattr(settings, "embed_dim", 384)
    monkeypatch.setattr(settings, "allowed_workspaces", [])
    monkeypatch.setattr(settings, "api_token", api_token)
    from engraphis.dashboard_app import create_app
    return TestClient(create_app(), client=client_addr)


def test_remote_runtime_refuses_data_routes_without_token(monkeypatch, tmp_path):
    with _client(
        monkeypatch, tmp_path, client_addr=("203.0.113.9", 51234)
    ) as client:
        response = client.get("/api/memories")
        assert response.status_code == 403
        assert response.json()["auth"] == "local-token-required"


def test_loopback_zero_config_still_works(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        assert client.get("/api/memories").status_code == 200


def test_remote_api_token_is_required_and_accepted(monkeypatch, tmp_path):
    with _client(
        monkeypatch,
        tmp_path,
        api_token="deployment-token-with-enough-entropy",
        client_addr=("203.0.113.9", 51234),
    ) as client:
        assert client.get("/api/memories").status_code == 401
        allowed = client.get(
            "/api/memories",
            headers={"Authorization": "Bearer deployment-token-with-enough-entropy"},
        )
        assert allowed.status_code == 200


def test_public_metadata_does_not_expose_team_account_routes(monkeypatch, tmp_path):
    with _client(
        monkeypatch, tmp_path, client_addr=("203.0.113.9", 51234)
    ) as client:
        assert client.get("/api/health").status_code == 200
        state = client.get("/api/auth/state")
        assert state.status_code == 200
        assert state.json()["hosted_team"] is True
        assert client.post("/api/auth/setup", json={}).status_code == 403


def test_security_headers_cover_short_circuit_errors(monkeypatch, tmp_path):
    with _client(
        monkeypatch,
        tmp_path,
        api_token="deployment-token-with-enough-entropy",
        client_addr=("203.0.113.9", 51234),
    ) as client:
        response = client.get("/api/memories")
        assert response.status_code == 401
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert "default-src" in response.headers["content-security-policy"]


def test_cors_preflight_reaches_cors_before_auth(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "cors_origins", ["https://client.example"])
    with _client(
        monkeypatch,
        tmp_path,
        api_token="deployment-token-with-enough-entropy",
        client_addr=("203.0.113.9", 51234),
    ) as client:
        response = client.options(
            "/api/memories",
            headers={
                "Origin": "https://client.example",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "https://client.example"
