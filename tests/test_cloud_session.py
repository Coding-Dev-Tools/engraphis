from __future__ import annotations

import pytest

from engraphis import cloud_session


def test_refresh_rotates_saved_credential_and_binds_client_workspace(monkeypatch) -> None:
    monkeypatch.delenv("ENGRAPHIS_CLOUD_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("ENGRAPHIS_CLOUD_REFRESH_CREDENTIAL", raising=False)
    saved = {
        "control_url": "https://control.example.test",
        "compute_url": "https://compute.example.test",
        "organization_id": "org_1",
        "refresh_credential": "old-refresh",
        "token_subject": "member",
    }
    writes = []
    requests = []
    monkeypatch.setattr(cloud_session, "_load", lambda: dict(saved))
    monkeypatch.setattr(cloud_session, "_save", writes.append)
    monkeypatch.setattr(
        cloud_session,
        "validate_cloud_base_url",
        lambda value: value.rstrip("/"),
    )

    def refresh(control_url, credential, workspace_id, token_subject):
        requests.append((control_url, credential, workspace_id, token_subject))
        return {
            "access_token": "short-lived-access",
            "organization_id": "org_1",
            "refresh_credential": "rotated-refresh",
            "refresh_expires_at": "2026-08-21T00:00:00Z",
            "token_subject": "member",
        }

    monkeypatch.setattr(cloud_session, "_post_refresh", refresh)
    result = cloud_session.access_for_workspace("ws_client_1")
    assert result == (
        "short-lived-access",
        "org_1",
        "https://compute.example.test",
    )
    assert requests == [(
        "https://control.example.test",
        "old-refresh",
        "ws_client_1",
        "member",
    )]
    assert writes[0]["refresh_credential"] == "rotated-refresh"


def test_direct_access_token_path_never_reads_refresh_state(monkeypatch) -> None:
    monkeypatch.setenv("ENGRAPHIS_CLOUD_ACCESS_TOKEN", "direct-token")
    monkeypatch.setenv("ENGRAPHIS_CLOUD_ORGANIZATION_ID", "org_direct")
    monkeypatch.setenv("ENGRAPHIS_CLOUD_COMPUTE_URL", "https://compute.example.test")
    monkeypatch.setattr(cloud_session, "validate_cloud_base_url", lambda value: value)
    monkeypatch.setattr(
        cloud_session,
        "_load",
        lambda: (_ for _ in ()).throw(AssertionError("refresh state must not be read")),
    )
    assert cloud_session.access_for_workspace("ws") == (
        "direct-token",
        "org_direct",
        "https://compute.example.test",
    )


def test_environment_refresh_honors_explicit_device_subject(monkeypatch) -> None:
    monkeypatch.setenv("ENGRAPHIS_CLOUD_REFRESH_CREDENTIAL", "env-refresh")
    monkeypatch.setenv("ENGRAPHIS_CLOUD_CONTROL_URL", "https://control.example.test")
    monkeypatch.setenv("ENGRAPHIS_CLOUD_TOKEN_SUBJECT", "device")
    monkeypatch.setattr(cloud_session, "_load", lambda: {})
    monkeypatch.setattr(
        cloud_session, "validate_cloud_base_url", lambda value: value.rstrip("/")
    )
    subjects = []

    def refresh(control_url, credential, workspace_id, token_subject):
        subjects.append(token_subject)
        return {
            "access_token": "device-access",
            "organization_id": "org_device",
            "refresh_credential": "rotated-but-env-owned",
            "token_subject": "device",
        }

    monkeypatch.setattr(cloud_session, "_post_refresh", refresh)
    assert cloud_session.access_for_workspace("ws", require_compute=False) == (
        "device-access",
        "org_device",
        "",
    )
    assert subjects == ["device"]


@pytest.mark.parametrize("subject", ["admin", "", "device member"])
def test_environment_refresh_rejects_invalid_subject(monkeypatch, subject) -> None:
    monkeypatch.setenv("ENGRAPHIS_CLOUD_REFRESH_CREDENTIAL", "env-refresh")
    monkeypatch.setenv("ENGRAPHIS_CLOUD_CONTROL_URL", "https://control.example.test")
    monkeypatch.setenv("ENGRAPHIS_CLOUD_TOKEN_SUBJECT", subject)
    monkeypatch.setattr(cloud_session, "_load", lambda: {})

    if subject == "":
        # Empty means the documented member default, not an invalid override.
        assert cloud_session.configured(require_compute=False) is True
    else:
        with pytest.raises(cloud_session.CloudSessionError, match="device.*member"):
            cloud_session.configured(require_compute=False)
