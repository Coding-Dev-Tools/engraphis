"""Unified local dashboard tests for the public open-core boundary."""
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="full-stack extra not installed")
pytest.importorskip("httpx", reason="httpx not installed")

from fastapi.testclient import TestClient  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from engraphis.config import settings  # noqa: E402
from engraphis.cloud_features import CloudFeatureError  # noqa: E402
from engraphis.routes import v2_api  # noqa: E402
from engraphis.service import MemoryService, ValidationError  # noqa: E402


def _client(monkeypatch, tmp_path):
    db_path = str(tmp_path / "dashboard.db")
    monkeypatch.setattr(settings, "db_path", db_path)
    monkeypatch.setattr(settings, "embed_model", "")
    monkeypatch.setattr(settings, "embed_dim", 384)
    monkeypatch.setattr(settings, "allowed_workspaces", [])
    monkeypatch.setattr(settings, "api_token", "")
    seeded = MemoryService.create(db_path)
    seeded.remember(
        "Postgres 16 is the main database.",
        workspace="demo",
        scope="workspace",
        title="Database",
    )
    seeded.remember(
        "A second workspace must stay isolated.",
        workspace="beta",
        scope="workspace",
        title="Isolation",
    )
    seeded.store.close()
    from engraphis.dashboard_app import create_app
    return TestClient(create_app(), client=("127.0.0.1", 50000))


def test_dashboard_serves_and_bootstraps_local_core(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert 'class="sidebar"' in page.text
        bootstrap = client.get("/api/bootstrap")
        assert bootstrap.status_code == 200
        assert bootstrap.json()["stats"]["memories"] >= 1


def test_team_account_routes_are_not_in_public_runtime(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        assert client.post("/api/auth/setup", json={}).status_code == 404
        assert client.get("/api/auth/users").status_code == 404
        state = client.get("/api/auth/state").json()
        assert state["enabled"] is False
        assert state["hosted_team"] is True


def test_local_agent_write_has_no_client_side_team_paywall(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        response = client.post(
            "/api/remember",
            json={"workspace": "demo", "content": "Queues use at-least-once delivery."},
        )
        assert response.status_code == 200


def test_manual_consolidation_stays_local_but_dreaming_is_cloud_only(
    monkeypatch, tmp_path
):
    with _client(monkeypatch, tmp_path) as client:
        manual = client.post(
            "/api/consolidate",
            json={"workspace": "demo", "dry_run": True, "infer": False},
        )
        assert manual.status_code == 200
        dream = client.post(
            "/api/consolidate",
            json={"workspace": "demo", "dry_run": True, "infer": True},
        )
        assert dream.status_code == 501
        assert dream.json()["detail"]["cloud_only"] is True


def test_analytics_route_delegates_to_managed_compute(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "engraphis.cloud_features.run_managed_job",
        lambda service, workspace, kind: {
            "result": {
                "kind": kind,
                "generation": 4,
                "totals": {"live": 1},
            }
        },
    )
    with _client(monkeypatch, tmp_path) as client:
        response = client.get("/api/analytics?workspace=demo")
        assert response.status_code == 200
        assert response.json()["kind"] == "analytics"
        assert response.json()["generation"] == 4


def test_unconnected_automation_returns_a_structured_auth_error(monkeypatch, tmp_path):
    for name in (
        "ENGRAPHIS_CLOUD_ACCESS_TOKEN",
        "ENGRAPHIS_CLOUD_ORGANIZATION_ID",
        "ENGRAPHIS_CLOUD_COMPUTE_URL",
        "ENGRAPHIS_CLOUD_REFRESH_CREDENTIAL",
        "ENGRAPHIS_CLOUD_CONTROL_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ENGRAPHIS_STATE_DIR", str(tmp_path / "unconnected-state"))

    with _client(monkeypatch, tmp_path) as client:
        response = client.get("/api/automation?workspace=demo")

    assert response.status_code == 401
    assert response.json()["detail"] == {
        "error": "managed cloud operation failed",
        "managed_cloud": True,
        "transient": False,
    }


def test_hosted_automation_accepts_the_cloud_policy_field(monkeypatch, tmp_path):
    saved = {}

    class _Cloud:
        def upload_snapshot(self, workspace_id, snapshot):
            return {"generation": snapshot["generation"]}

        def get_policy(self, workspace_id):
            return {"enabled": False, "cadence_minutes": 1440, "dream_enabled": False}

        def save_policy(self, workspace_id, policy):
            saved.update(policy)
            return {"version": 2}

    monkeypatch.setattr(
        "engraphis.cloud_features.build_managed_snapshot",
        lambda service, workspace: ("ws_cloud", {"generation": 1}),
    )
    monkeypatch.setattr(
        "engraphis.cloud_features.CloudFeatureClient.from_environment",
        lambda workspace_id=None: _Cloud(),
    )
    with _client(monkeypatch, tmp_path) as client:
        response = client.post(
            "/api/automation",
            json={"enabled": True, "dream_enabled": True, "cadence_hours": 12},
        )
        assert response.status_code == 200
        assert response.json()["dream_enabled"] is True
        assert saved["dream_enabled"] is True


def test_reading_or_disabling_automation_never_uploads_memory_content(
    monkeypatch, tmp_path
):
    saved = {}

    class _Cloud:
        def get_policy(self, workspace_id):
            return {"enabled": True, "cadence_minutes": 60, "dream_enabled": True}

        def list_jobs(self, workspace_id, *, limit=10):
            return {"jobs": []}

        def save_policy(self, workspace_id, policy):
            saved.update(policy)
            return {"version": 3}

    def _unexpected_upload(*args, **kwargs):
        raise AssertionError("policy inspection must not build or upload a snapshot")

    monkeypatch.setattr(
        "engraphis.cloud_features.build_managed_snapshot",
        _unexpected_upload,
    )
    monkeypatch.setattr(
        "engraphis.cloud_features.CloudFeatureClient.from_environment",
        lambda workspace_id=None: _Cloud(),
    )
    with _client(monkeypatch, tmp_path) as client:
        assert client.get("/api/automation").status_code == 200
        response = client.post("/api/automation", json={"enabled": False})
        assert response.status_code == 200
        assert saved["enabled"] is False


def test_automation_and_maintenance_use_the_selected_workspace(monkeypatch, tmp_path):
    policy_workspaces = []
    snapshot_workspaces = []
    maintenance_workspaces = []

    class _Cloud:
        def get_policy(self, workspace_id):
            policy_workspaces.append(workspace_id)
            return {"enabled": False, "cadence_minutes": 60, "dream_enabled": True}

        def list_jobs(self, workspace_id, *, limit=10):
            policy_workspaces.append(workspace_id)
            return {"jobs": []}

        def upload_snapshot(self, workspace_id, snapshot):
            snapshot_workspaces.append(workspace_id)
            return {"generation": snapshot["generation"]}

        def save_policy(self, workspace_id, policy):
            policy_workspaces.append(workspace_id)
            return {"version": 1}

    def snapshot(service, workspace):
        snapshot_workspaces.append(workspace)
        return service._lookup_workspace(workspace), {"generation": 1}

    def managed_job(service, workspace, kind):
        maintenance_workspaces.append((workspace, kind))
        return {"result": {"kind": kind}}

    monkeypatch.setattr("engraphis.cloud_features.build_managed_snapshot", snapshot)
    monkeypatch.setattr("engraphis.cloud_features.run_managed_job", managed_job)
    monkeypatch.setattr(
        "engraphis.cloud_features.CloudFeatureClient.from_environment",
        lambda workspace_id=None: _Cloud(),
    )
    with _client(monkeypatch, tmp_path) as client:
        beta_id = client.app.state.service._lookup_workspace("beta")
        demo_id = client.app.state.service._lookup_workspace("demo")
        assert client.get("/api/automation?workspace=beta").status_code == 200
        assert client.post(
            "/api/automation?workspace=beta", json={"enabled": True}
        ).status_code == 200
        assert client.post(
            "/api/maintenance/run?workspace=beta", json={"dry_run": True}
        ).status_code == 200

    assert beta_id in policy_workspaces
    assert demo_id not in policy_workspaces
    assert "beta" in snapshot_workspaces
    assert maintenance_workspaces == [("beta", "consolidate")]


def test_automation_workspace_query_unknown_is_not_replaced_by_legacy_default(
    monkeypatch, tmp_path
):
    with _client(monkeypatch, tmp_path) as client:
        for method, path, payload in (
            (client.get, "/api/automation?workspace=missing", None),
            (client.post, "/api/automation?workspace=missing", {"enabled": False}),
            (client.post, "/api/maintenance/run?workspace=missing", {"dry_run": True}),
        ):
            response = method(path, json=payload) if payload is not None else method(path)
            assert response.status_code == 404


def test_dashboard_automation_uses_active_workspace_and_discloses_upload_boundary():
    source = Path(__file__).parents[1] / "engraphis" / "static" / "dashboard.js"
    source = source.read_text(encoding="utf-8")
    assert "/automation?workspace=" in source
    assert "/maintenance/run?workspace=" in source
    assert "Preview snapshot" not in source
    assert "ENGRAPHIS_MANAGED_COMPUTE_CONSENT=1" in source
    assert "uploads the selected workspace’s normal and sensitive memory content" in source


def test_portfolio_and_report_analytics_are_hosted_only(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        assert client.get("/api/analytics/portfolio").status_code == 501
        assert client.get("/api/analytics/export?workspace=demo").status_code == 501


def test_raw_owner_export_is_free_but_signed_report_is_cloud_only(
    monkeypatch, tmp_path
):
    with _client(monkeypatch, tmp_path) as client:
        raw = client.get("/api/export?workspace=demo")
        assert raw.status_code == 200
        assert raw.json()["counts"]["memories"] >= 1
        signed = client.get("/api/export?workspace=demo&signed=true")
        assert signed.status_code == 501
        assert signed.json()["detail"]["cloud_only"] is True


def test_health_and_readiness_remain_public(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/ready").status_code == 200


def test_dashboard_exception_responses_do_not_echo_untrusted_exception_text():
    secret = "https://provider.example/?api_key=do-not-return-this"

    def fail_with(exc):
        raise exc

    with pytest.raises(HTTPException) as internal:
        v2_api._run(fail_with, RuntimeError(secret))
    assert internal.value.status_code == 500
    assert internal.value.detail == {"error": "internal server error"}
    assert secret not in repr(internal.value.detail)

    with pytest.raises(HTTPException) as validation:
        v2_api._run(fail_with, ValidationError(secret))
    assert validation.value.status_code == 400
    assert validation.value.detail == {"error": "invalid request"}
    assert secret not in repr(validation.value.detail)

    with pytest.raises(HTTPException) as downstream:
        v2_api._run(fail_with, HTTPException(status_code=418, detail={"error": secret}))
    assert downstream.value.status_code == 418
    assert downstream.value.detail == {"error": "request rejected"}
    assert secret not in repr(downstream.value.detail)

    with pytest.raises(HTTPException) as invalid_status:
        v2_api._run(fail_with, HTTPException(status_code=999, detail={"error": secret}))
    assert invalid_status.value.status_code == 500
    assert invalid_status.value.detail == {"error": "internal server error"}
    assert secret not in repr(invalid_status.value.detail)

    with pytest.raises(HTTPException) as mismatch:
        v2_api._run(fail_with, ValueError(f"{secret}: shapes 256 and 384 are not aligned"))
    assert mismatch.value.status_code == 409
    assert mismatch.value.detail["embedder"] is True
    assert secret not in repr(mismatch.value.detail)

    with pytest.raises(HTTPException) as ordinary_value_error:
        v2_api._run(fail_with, ValueError(secret))
    assert ordinary_value_error.value.status_code == 500
    assert ordinary_value_error.value.detail == {"error": "internal server error"}
    assert secret not in repr(ordinary_value_error.value.detail)

    with pytest.raises(HTTPException) as managed:
        v2_api._managed_call(fail_with, CloudFeatureError(secret, status=502))
    assert managed.value.status_code == 502
    assert managed.value.detail == {
        "error": "managed cloud operation failed", "managed_cloud": True,
        "transient": False,
    }
    assert secret not in repr(managed.value.detail)

    with pytest.raises(HTTPException) as consent:
        v2_api._managed_call(
            fail_with,
            CloudFeatureError(secret, status=409, code="consent_required"),
        )
    assert consent.value.status_code == 409
    assert consent.value.detail == {
        "error": "managed cloud operation failed",
        "managed_cloud": True,
        "transient": False,
        "code": "consent_required",
    }
    assert secret not in repr(consent.value.detail)
