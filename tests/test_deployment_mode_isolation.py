"""Tests for deployment-mode detection and local/hosted isolation."""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi", reason="full-stack extra not installed")


@pytest.fixture(autouse=True)
def _clean_hosted_env(monkeypatch):
    """Remove all hosted-indicating env vars before each test."""
    hosted_vars = (
        "ENGRAPHIS_CLOUD_CONTROL_URL",
        "ENGRAPHIS_CLOUD_COMPUTE_URL",
        "ENGRAPHIS_CLOUD_ORGANIZATION_ID",
        "ENGRAPHIS_CLOUD_REFRESH_CREDENTIAL",
        "ENGRAPHIS_CLOUD_ACCESS_TOKEN",
        "ENGRAPHIS_CONTROL_PLANE_URL",
        "ENGRAPHIS_HOSTED_MODE",
    )
    for var in hosted_vars:
        monkeypatch.delenv(var, raising=False)
    yield


class TestDeploymentModeDetection:
    def test_local_mode_when_no_hosted_vars(self):
        from engraphis.config import deployment_mode, is_hosted_mode, is_local_mode

        assert deployment_mode() == "local"
        assert is_local_mode() is True
        assert is_hosted_mode() is False

    @pytest.mark.parametrize("name", [
        "ENGRAPHIS_CLOUD_CONTROL_URL",
        "ENGRAPHIS_CLOUD_COMPUTE_URL",
        "ENGRAPHIS_CLOUD_ORGANIZATION_ID",
        "ENGRAPHIS_CLOUD_REFRESH_CREDENTIAL",
        "ENGRAPHIS_CLOUD_ACCESS_TOKEN",
        "ENGRAPHIS_CONTROL_PLANE_URL",
    ])
    def test_hosted_mode_with_cloud_configuration(self, monkeypatch, name):
        monkeypatch.setenv(name, "configured")
        from engraphis.config import deployment_mode

        assert deployment_mode() == "hosted"

    def test_explicit_hosted_mode_override_true(self, monkeypatch):
        monkeypatch.setenv("ENGRAPHIS_HOSTED_MODE", "true")
        from engraphis.config import deployment_mode

        assert deployment_mode() == "hosted"

    def test_explicit_hosted_mode_override_false_forces_local(self, monkeypatch):
        monkeypatch.setenv("ENGRAPHIS_CLOUD_CONTROL_URL", "configured")
        monkeypatch.setenv("ENGRAPHIS_HOSTED_MODE", "false")
        from engraphis.config import deployment_mode

        assert deployment_mode() == "local"

    def test_blank_hosted_vars_are_local(self, monkeypatch):
        monkeypatch.setenv("ENGRAPHIS_CLOUD_CONTROL_URL", "")
        monkeypatch.setenv("ENGRAPHIS_CLOUD_COMPUTE_URL", "   ")
        from engraphis.config import deployment_mode

        assert deployment_mode() == "local"


class TestAuthStateEndpoint:
    def test_local_mode_auth_state_suppresses_cloud_url(self, monkeypatch, tmp_path):
        from fastapi.testclient import TestClient

        from engraphis.config import settings
        from engraphis.dashboard_app import create_app

        monkeypatch.setattr(settings, "api_token", "")
        monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
        with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
            data = client.get("/api/auth/state").json()

        assert data["deployment_mode"] == "local"
        assert data["cloud_url"] == ""
        assert data["hosted_team"] is False
        assert data["local_invitations"] is True


class TestDeviceConnectModeGuard:
    def test_local_mode_refuses_implicit_connect(self):
        from engraphis.device_connect import DeviceConnectError, connect

        with pytest.raises(DeviceConnectError, match="local mode"):
            connect("engr_ct_test_token_value_here_1234")

    def test_local_mode_allows_explicit_control_url(self, monkeypatch):
        from engraphis import device_connect

        def fail_preflight(**_kwargs):
            raise device_connect.DeviceConnectError("preflight reached", status=400)

        monkeypatch.setattr(device_connect, "preflight", fail_preflight)
        with pytest.raises(device_connect.DeviceConnectError) as exc_info:
            device_connect.connect(
                "engr_ct_test_token_value_here_1234",
                control_url="https://api.engraphis.com",
            )
        assert "local mode" not in str(exc_info.value).lower()
