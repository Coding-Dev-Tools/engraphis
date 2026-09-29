"""Managed configuration requires the direct credential's own control origin."""
import io
import json
import socket
from types import SimpleNamespace

import pytest

from engraphis import cloud_session, hosted_client
from engraphis.backends import jev_transport as transport
from engraphis.backends.jev_decision import DecisionQuestion
from engraphis.config import Settings


@pytest.fixture
def direct_credentials(monkeypatch):
    monkeypatch.setenv("ENGRAPHIS_CLOUD_ACCESS_TOKEN", "synthetic-direct-access")
    monkeypatch.setenv("ENGRAPHIS_CLOUD_ORGANIZATION_ID", "org_direct")
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-personal-key")
    monkeypatch.delenv("ENGRAPHIS_CLOUD_CONTROL_URL", raising=False)
    monkeypatch.delenv("ENGRAPHIS_CLOUD_COMPUTE_URL", raising=False)
    saved_reads = []

    def saved():
        saved_reads.append(True)
        return {"control_url": "https://stale-saved.example.test",
                "refresh_credential": "synthetic-saved-refresh",
                "organization_id": "org_saved", "token_subject": "member"}

    def forbidden(*args, **kwargs):
        pytest.fail("direct configuration must not refresh, resolve, send, or select BYOK")

    monkeypatch.setattr(cloud_session, "_load", saved)
    monkeypatch.setattr(cloud_session, "_post_refresh", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(hosted_client, "build_pinned_https_opener", forbidden)
    monkeypatch.setattr(transport, "create_typesafe_decision_client", forbidden)
    return saved_reads


@pytest.mark.parametrize("control", [None, "", " \t\n"])
@pytest.mark.parametrize("mode", ["managed", "auto"])
@pytest.mark.parametrize("explicit", [False, True])
def test_direct_credentials_without_control_do_not_configure_managed_jev(
    direct_credentials, monkeypatch, control, mode, explicit,
):
    if control is not None:
        monkeypatch.setenv("ENGRAPHIS_CLOUD_CONTROL_URL", control)
    monkeypatch.setenv("ENGRAPHIS_DECISION_BACKEND", mode)

    assert transport.create_cloud_decision_client().is_configured is False
    settings = Settings(decision_backend=mode) if explicit else Settings()
    assert settings.has_decision_backend is False
    assert transport.select_decision_client(mode if explicit else None) == (None, "local_heuristic")
    # Org-scoped generic access remains usable; only the Jev route needs control.
    assert cloud_session.configured(require_compute=False) is True
    assert cloud_session.access_for_workspace(None, require_compute=False) == (
        "synthetic-direct-access", "org_direct", "",
    )
    assert direct_credentials == []


@pytest.mark.parametrize("control", [None, "", " \t\n"])
@pytest.mark.parametrize("mode", ["managed", "auto"])
def test_mcp_missing_control_falls_back_without_using_personal_key(
    direct_credentials, monkeypatch, control, mode,
):
    pytest.importorskip("mcp")
    from engraphis import mcp_server

    if control is not None:
        monkeypatch.setenv("ENGRAPHIS_CLOUD_CONTROL_URL", control)
    monkeypatch.setenv("ENGRAPHIS_DECISION_BACKEND", mode)
    result = json.loads(mcp_server.engraphis_decide(
        kind="custom", state="Synthetic evidence", allow_remote=True,
    ))
    assert result["decision_status"] == "local_fallback"
    assert result["fallback_reason"] == "backend_not_configured"
    assert result["selected"] is None and result["confidence"] is None
    assert direct_credentials == []


@pytest.mark.parametrize("mode", ["managed", "auto"])
@pytest.mark.parametrize("explicit", [False, True])
def test_direct_control_routes_exact_origin_and_token_without_saved_state_or_refresh(
    direct_credentials, monkeypatch, mode, explicit,
):
    control = "https://control.example.test:8443/base"
    monkeypatch.setenv("ENGRAPHIS_CLOUD_CONTROL_URL", control + "/")
    monkeypatch.setenv("ENGRAPHIS_DECISION_BACKEND", mode)
    settings = Settings(decision_backend=mode) if explicit else Settings()
    assert settings.has_decision_backend is True
    client, name = transport.select_decision_client(mode if explicit else None)
    assert isinstance(client, transport.EngraphisCloudDecisionClient)
    assert client.is_configured is True and name == "engraphis_cloud"
    assert direct_credentials == []
    requests, resolutions = [], []

    def resolve(host, port, *args, **kwargs):
        assert host == "control.example.test"
        resolutions.append((host, port))
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port or 0))]

    def open_request(request, timeout):
        requests.append(request)
        assert 0 < timeout <= client.timeout_s
        response = io.BytesIO(json.dumps({
            "model": transport.MODEL, "is_fallback": False, "decisions": {"q": {
                "type": "noul", "probability": 0.9, "confidence": 0.8,
                "confidence_source": "derived_decisiveness",
            }},
        }).encode())
        response.status = 200
        response.headers = {"Content-Type": "application/json"}
        return response

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(hosted_client, "build_pinned_https_opener",
                        lambda *handlers: SimpleNamespace(open=open_request))
    batch = client.evaluate("Synthetic evidence", [DecisionQuestion("q", "Assess", "noul")],
                            model=transport.MODEL, allow_remote=True)
    assert batch.get_noul("q").probability == 0.9
    assert len(requests) == 1 and resolutions
    assert requests[0].full_url == control + "/v1/jev/decide"
    assert requests[0].get_header("Authorization") == "Bearer synthetic-direct-access"
    assert json.loads(requests[0].data)["state"] == "Synthetic evidence"
    assert direct_credentials == []


@pytest.mark.parametrize("control", [None, "", " \t\n"])
def test_compute_only_direct_credentials_remain_usable(direct_credentials, monkeypatch, control):
    if control is not None:
        monkeypatch.setenv("ENGRAPHIS_CLOUD_CONTROL_URL", control)
    compute = "https://compute.example.test"
    monkeypatch.setenv("ENGRAPHIS_CLOUD_COMPUTE_URL", compute)
    assert cloud_session.configured() is True
    assert transport.create_cloud_decision_client().is_configured is False

    def resolve(host, port, *args, **kwargs):
        assert host == "compute.example.test"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port or 0))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    assert cloud_session.access_for_workspace(None) == (
        "synthetic-direct-access", "org_direct", compute,
    )
    assert direct_credentials == []


@pytest.mark.parametrize("failure", ["configured", "credential_bound_control_url"])
def test_managed_configuration_failure_is_closed_and_private(monkeypatch, caplog, failure):
    monkeypatch.setattr(cloud_session, "configured", lambda **kwargs: True)
    monkeypatch.setattr(cloud_session, "credential_bound_control_url",
                        lambda: "https://control.example.test")

    def unavailable(*args, **kwargs):
        raise OSError("synthetic private session path")

    monkeypatch.setattr(cloud_session, failure, unavailable)
    assert transport.create_cloud_decision_client().is_configured is False
    for mode in ("managed", "auto"):
        assert Settings(decision_backend=mode).has_decision_backend is False
        assert transport.select_decision_client(mode) == (None, "local_heuristic")
    assert caplog.text == ""


def test_unconfigured_session_does_not_inspect_an_origin(monkeypatch):
    monkeypatch.setattr(cloud_session, "configured", lambda **kwargs: False)

    def forbidden():
        pytest.fail("an unconfigured session must not inspect an origin")

    monkeypatch.setattr(cloud_session, "credential_bound_control_url", forbidden)
    assert transport.create_cloud_decision_client().is_configured is False
    assert Settings(decision_backend="managed").has_decision_backend is False
