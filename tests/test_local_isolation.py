"""Deployment-mode contract: local installations do not leak hosted state."""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi", reason="full-stack extra not installed")

_HOSTED_ENV_VARS = (
    "ENGRAPHIS_CLOUD_CONTROL_URL",
    "ENGRAPHIS_CLOUD_COMPUTE_URL",
    "ENGRAPHIS_CLOUD_ORGANIZATION_ID",
    "ENGRAPHIS_CLOUD_REFRESH_CREDENTIAL",
    "ENGRAPHIS_CLOUD_ACCESS_TOKEN",
    "ENGRAPHIS_CONTROL_PLANE_URL",
    "ENGRAPHIS_HOSTED_MODE",
)


@pytest.fixture(autouse=True)
def _local_environment(monkeypatch):
    for name in _HOSTED_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_local_mode_when_no_hosted_env_vars():
    from engraphis.config import deployment_mode, is_local_mode

    assert is_local_mode()
    assert deployment_mode() == "local"


def test_local_invitations_never_contain_hosted_urls(monkeypatch, tmp_path):
    from engraphis.config import settings
    from engraphis.dashboard_app import create_app
    from starlette.testclient import TestClient

    monkeypatch.setattr(settings, "api_token", "")
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "local.db"))
    with TestClient(create_app()) as client:
        response = client.get("/api/auth/state")
        data = response.json()

    assert data["deployment_mode"] == "local"
    assert data["hosted_team"] is False
    assert data["local_invitations"] is True
    assert data["cloud_url"] == ""
    assert "engraphis.com" not in response.text


def test_device_connect_uses_the_shipped_endpoint_as_explicit_opt_in(monkeypatch):
    from engraphis import device_connect

    monkeypatch.setattr(device_connect, "default_control_url", lambda: "https://cloud.test")
    def fail_preflight(**_kwargs):
        raise device_connect.DeviceConnectError("preflight reached", status=400)

    monkeypatch.setattr(device_connect, "preflight", fail_preflight)
    with pytest.raises(device_connect.DeviceConnectError, match="preflight reached"):
        device_connect.connect("engr_ct_test_token_value_here_1234")
