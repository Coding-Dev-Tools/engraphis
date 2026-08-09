"""Security headers and the loopback-or-token local dashboard boundary."""
import pytest

pytest.importorskip("fastapi", reason="full-stack extra not installed")
pytest.importorskip("httpx", reason="httpx not installed")

from fastapi.testclient import TestClient  # noqa: E402

from engraphis.config import settings  # noqa: E402


def _client(monkeypatch, tmp_path, *, api_token="", client_addr=("127.0.0.1", 50000),
            allowed_workspaces=None):
    monkeypatch.delenv("ENGRAPHIS_WORKSPACES", raising=False)
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "security.db"))
    monkeypatch.setattr(settings, "embed_model", "")
    monkeypatch.setattr(settings, "embed_dim", 384)
    monkeypatch.setattr(settings, "allowed_workspaces", allowed_workspaces or [])
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


def test_remote_dashboard_exchanges_token_for_an_httponly_session(monkeypatch, tmp_path):
    token = "deployment-token-with-enough-entropy"
    with _client(
        monkeypatch,
        tmp_path,
        api_token=token,
        client_addr=("203.0.113.9", 51234),
    ) as client:
        rejected = client.post("/api/auth/session", json={"token": "wrong"})
        assert rejected.status_code == 401
        assert "set-cookie" not in rejected.headers

        opened = client.post("/api/auth/session", json={"token": token})
        assert opened.status_code == 200
        cookie = opened.headers["set-cookie"]
        assert token not in cookie
        assert "HttpOnly" in cookie
        assert "SameSite=strict" in cookie

        # Cookie auth is reserved for the shipped same-origin browser bundle. The custom
        # header forces cross-origin scripts through CORS and blocks ordinary CSRF forms,
        # including the side-effectful first Automation GET.
        assert client.get("/api/memories").status_code == 403
        allowed = client.get(
            "/api/memories",
            headers={"X-Engraphis-Browser-Session": "1"},
        )
        assert allowed.status_code == 200


def test_browser_session_dies_when_the_deployment_token_changes(monkeypatch, tmp_path):
    token = "deployment-token-with-enough-entropy"
    with _client(
        monkeypatch,
        tmp_path,
        api_token=token,
        client_addr=("203.0.113.9", 51234),
    ) as client:
        assert client.post("/api/auth/session", json={"token": token}).status_code == 200
        monkeypatch.setattr(settings, "api_token", "rotated-deployment-token")
        response = client.get(
            "/api/memories",
            headers={"X-Engraphis-Browser-Session": "1"},
        )
        assert response.status_code == 401


def test_dashboard_review_approval_requires_browser_session_and_csrf(monkeypatch, tmp_path):
    token = "deployment-token-with-enough-entropy"
    with _client(monkeypatch, tmp_path, api_token=token) as client:
        service = client.app.state.service
        pending = service.remember(
            "The release switch is controlled by the operations owner.",
            workspace="review",
            source="web",
            trusted=True,
        )
        record = service.store.get_memory(pending["id"])
        assert record.provenance["review_state"] == "pending"

        # A bearer-only API caller cannot invoke the human approval ceremony.
        rejected = client.post(
            "/dashboard/review/approve",
            json={"memory_id": pending["id"], "reason": "checked"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert rejected.status_code == 401
        assert client.post(
            "/api/review/approve",
            json={"memory_id": pending["id"], "reason": "checked"},
            headers={"Authorization": f"Bearer {token}"},
        ).status_code == 404

        opened = client.post("/api/auth/session", json={"token": token})
        assert opened.status_code == 200
        csrf = opened.json()["review_csrf_token"]
        missing_csrf = client.post(
            "/dashboard/review/approve",
            json={"memory_id": pending["id"], "reason": "checked"},
            headers={"X-Engraphis-Browser-Session": "1"},
        )
        assert missing_csrf.status_code == 403

        approved = client.post(
            "/dashboard/review/approve",
            json={"memory_id": pending["id"], "reason": "checked against owner runbook"},
            headers={
                "X-Engraphis-Browser-Session": "1",
                "X-Engraphis-Review-CSRF": csrf,
            },
        )
        assert approved.status_code == 200
        successor = service.store.get_memory(approved.json()["id"])
        assert successor.provenance["review_state"] == "approved"
        assert successor.provenance["approved_from"] == pending["id"]
        # The original untrusted evidence remains pending instead of being relabeled.
        assert service.store.get_memory(pending["id"]).provenance["review_state"] == "pending"


def test_dashboard_review_approval_enforces_source_workspace_binding(monkeypatch, tmp_path):
    token = "deployment-token-with-enough-entropy"
    with _client(
        monkeypatch, tmp_path, api_token=token, allowed_workspaces=["allowed"],
    ) as client:
        service = client.app.state.service
        pending = service.remember(
            "The restricted release switch needs review.", workspace="allowed",
            source="web", trusted=True,
        )
        # Simulate a pre-existing foreign workspace, such as one that predates a later
        # ENGRAPHIS_WORKSPACES binding. Direct SQL is deliberate: public service writes
        # rightly reject this state, while the route must still fail closed if it exists.
        foreign_workspace = "ws_foreign"
        service.store.conn.execute(
            "INSERT INTO workspaces(id, name, created_at, settings) VALUES (?,?,?,?)",
            (foreign_workspace, "foreign", 0.0, "{}"),
        )
        service.store.conn.execute(
            "UPDATE memories SET workspace_id=? WHERE id=?",
            (foreign_workspace, pending["id"]),
        )
        service.store.conn.commit()

        csrf = client.post("/api/auth/session", json={"token": token}).json()["review_csrf_token"]
        rejected = client.post(
            "/dashboard/review/approve",
            json={"memory_id": pending["id"], "reason": "checked"},
            headers={
                "X-Engraphis-Browser-Session": "1",
                "X-Engraphis-Review-CSRF": csrf,
            },
        )

        assert rejected.status_code == 403
        assert service.store.conn.execute(
            "SELECT COUNT(*) FROM memories WHERE id<>?", (pending["id"],),
        ).fetchone()[0] == 0


def test_public_metadata_does_not_expose_team_account_routes(monkeypatch, tmp_path):
    with _client(
        monkeypatch, tmp_path, client_addr=("203.0.113.9", 51234)
    ) as client:
        assert client.get("/api/health").status_code == 200
        state = client.get("/api/auth/state")
        assert state.status_code == 200
        assert state.json()["enabled"] is False
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
