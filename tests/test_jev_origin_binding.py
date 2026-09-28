"""Managed origin comparisons use real validation, bootstrap and saved rotations.

Credentials, DNS and HTTP responses are synthetic; private session discovery and
provider requests never occur. The session vault and URL validator remain real.
"""
import socket

import pytest

from engraphis import cloud_session, hosted_client
from engraphis.backends import jev_transport as transport
from engraphis.backends.jev_decision import DecisionQuestion


@pytest.fixture
def bootstrap(monkeypatch, tmp_path):
    monkeypatch.setenv("ENGRAPHIS_STATE_DIR", str(tmp_path / "synthetic-session"))
    for name in ("ENGRAPHIS_CLOUD_ACCESS_TOKEN", "ENGRAPHIS_CLOUD_COMPUTE_URL",
                 "ENGRAPHIS_CLOUD_ORGANIZATION_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ENGRAPHIS_CLOUD_TOKEN_SUBJECT", "member")
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port or 0)),
    ])

    def configure(raw_control, expected_control, *, source="environment"):
        monkeypatch.setenv("ENGRAPHIS_CLOUD_REFRESH_CREDENTIAL", "synthetic-bootstrap")
        monkeypatch.setenv("ENGRAPHIS_CLOUD_CONTROL_URL", raw_control)
        if source == "saved":
            cloud_session._save({
                "schema": "engraphis-cloud-session/v1", "control_url": raw_control,
                "refresh_credential": "synthetic-bootstrap",
                "organization_id": "org_synthetic", "token_subject": "member",
            })
            monkeypatch.setenv("ENGRAPHIS_CLOUD_CONTROL_URL", "https://unused.example.invalid")
        calls = {"refresh": [], "decision": []}

        def refresh(control, credential, workspace, subject, *, deadline):
            index = len(calls["refresh"]) + 1
            expected_credential = ("synthetic-bootstrap" if index == 1 else
                                   f"synthetic-rotated-{index - 1}")
            assert (control, credential, workspace, subject) == (
                expected_control, expected_credential, None, "member",
            )
            assert deadline > transport.time.monotonic()
            calls["refresh"].append((control, credential))
            return {"access_token": f"synthetic-access-{index}",
                    "refresh_credential": f"synthetic-rotated-{index}",
                    "organization_id": "org_synthetic", "token_subject": "member"}

        def post(url, token, payload, timeout_s, *, deadline):
            assert url == expected_control + "/v1/jev/decide"
            assert token == f"synthetic-access-{len(calls['refresh'])}"
            assert deadline > transport.time.monotonic()
            calls["decision"].append(url)
            return {"model": transport.MODEL, "is_fallback": False, "decisions": {"q": {
                "type": "noul", "probability": 0.9, "confidence": 0.8,
                "confidence_source": "derived_decisiveness",
            }}}

        monkeypatch.setattr(cloud_session, "_post_refresh", refresh)
        monkeypatch.setattr(transport, "_post_json", post)
        return transport.create_cloud_decision_client(timeout_s=5), calls

    return configure


def _evaluate(client):
    return client.evaluate("Synthetic evidence", [DecisionQuestion("q", "Assess", "noul")],
                           model=transport.MODEL, allow_remote=True)


@pytest.mark.parametrize("source", ["environment", "saved"])
@pytest.mark.parametrize("raw,canonical", [
    ("https://api.engraphis.com/", "https://api.engraphis.com"),
    ("HTTPS://api.engraphis.com/", "https://api.engraphis.com"),
    ("  https://api.engraphis.com///  ", "https://api.engraphis.com"),
    ("https://API.ENGRAPHIS.COM:443/control///", "https://API.ENGRAPHIS.COM:443/control"),
    ("http://127.0.0.1:8765/control/", "http://127.0.0.1:8765/control"),
])
def test_first_managed_decision_uses_canonical_bootstrap_and_reuses_rotation(
    bootstrap, monkeypatch, source, raw, canonical,
):
    client, calls = bootstrap(raw, canonical, source=source)
    assert client.is_configured
    assert _evaluate(client).get_noul("q").probability == 0.9
    saved = cloud_session._load()
    assert saved["control_url"] == canonical
    assert saved["refresh_credential"] == "synthetic-rotated-1"
    assert not cloud_session._refresh_is_unusable(saved, "synthetic-rotated-1")

    # Persisted family binding wins over an environment endpoint replacement.
    monkeypatch.setenv("ENGRAPHIS_CLOUD_CONTROL_URL", "https://unused.example.invalid")
    assert _evaluate(client).get_noul("q").probability == 0.9
    assert calls["refresh"] == [(canonical, "synthetic-bootstrap"),
                                (canonical, "synthetic-rotated-1")]
    assert calls["decision"] == [canonical + "/v1/jev/decide"] * 2
    assert cloud_session._load()["refresh_credential"] == "synthetic-rotated-2"


@pytest.mark.parametrize("changed", [
    "https://other.example.invalid/control", "https://api.engraphis.com:444/control",
    "https://api.engraphis.com/other",
])
def test_changed_host_port_or_base_path_blocks_decision_after_persisting_rotation(
    bootstrap, monkeypatch, caplog, changed,
):
    client, calls = bootstrap("https://api.engraphis.com/control/",
                              "https://api.engraphis.com/control")
    real_access = cloud_session.access_for_workspace

    def access(*args, **kwargs):
        result = real_access(*args, **kwargs)
        updated = cloud_session._load()
        updated["control_url"] = changed
        cloud_session._save(updated)
        return result

    monkeypatch.setattr(cloud_session, "access_for_workspace", access)
    with pytest.raises(transport.DecisionClientError) as caught:
        _evaluate(client)
    assert str(caught.value) == "session_changed"
    assert len(calls["refresh"]) == 1 and calls["decision"] == []
    saved = cloud_session._load()
    assert saved["refresh_credential"] == "synthetic-rotated-1"
    assert not cloud_session._refresh_is_unusable(saved, "synthetic-rotated-1")
    assert caplog.text == ""


@pytest.mark.parametrize("raw", [
    "http://api.engraphis.com/", "https://api.engraphis.com/control?query=private",
    "https://synthetic:private@api.engraphis.com/",
])
def test_invalid_origin_does_not_spend_bootstrap_or_echo_its_value(bootstrap, caplog, raw):
    client, calls = bootstrap(raw, "")
    with pytest.raises(transport.DecisionClientError) as caught:
        _evaluate(client)
    assert str(caught.value) == "remote_unavailable"
    assert calls == {"refresh": [], "decision": []}
    assert cloud_session._load() == {}
    assert caplog.text == ""


@pytest.mark.parametrize("phase", ["before", "after"])
def test_canonical_origin_validation_still_consumes_the_shared_deadline(
    bootstrap, monkeypatch, phase,
):
    client, calls = bootstrap("https://api.engraphis.com/", "https://api.engraphis.com")
    clock = [100.0]
    monkeypatch.setattr(transport.time, "monotonic", lambda: clock[0])
    real_validate = hosted_client.validate_cloud_base_url
    validations = []

    def validate(value):
        result = real_validate(value)
        validations.append(value)
        if len(validations) == (1 if phase == "before" else 2):
            clock[0] += 6
        return result

    monkeypatch.setattr(hosted_client, "validate_cloud_base_url", validate)
    with pytest.raises(transport.DecisionClientError, match="^remote_timeout$"):
        _evaluate(client)
    assert calls["decision"] == []
    if phase == "before":
        assert calls["refresh"] == [] and cloud_session._load() == {}
    else:
        assert len(calls["refresh"]) == 1
        saved = cloud_session._load()
        assert saved["refresh_credential"] == "synthetic-rotated-1"
        assert not cloud_session._refresh_is_unusable(saved, "synthetic-rotated-1")


@pytest.mark.parametrize("phase", ["before", "after"])
def test_expired_origin_read_never_begins_another_validation_phase(
    bootstrap, monkeypatch, phase,
):
    client, calls = bootstrap("https://api.engraphis.com/", "https://api.engraphis.com")
    clock = [100.0]
    monkeypatch.setattr(transport.time, "monotonic", lambda: clock[0])
    real_origin = cloud_session.credential_bound_control_url
    real_validate = hosted_client.validate_cloud_base_url
    origins, validations = [], []

    def origin():
        result = real_origin()
        origins.append(result)
        if len(origins) == (1 if phase == "before" else 2):
            clock[0] += 6
        return result

    def validate(value):
        assert clock[0] < 105, "expired filesystem read reached DNS validation"
        validations.append(value)
        return real_validate(value)

    monkeypatch.setattr(cloud_session, "credential_bound_control_url", origin)
    monkeypatch.setattr(hosted_client, "validate_cloud_base_url", validate)
    with pytest.raises(transport.DecisionClientError, match="^remote_timeout$"):
        _evaluate(client)
    assert calls["decision"] == []
    assert len(validations) == (0 if phase == "before" else 1)
    assert len(calls["refresh"]) == (0 if phase == "before" else 1)
    if phase == "after":
        saved = cloud_session._load()
        assert saved["refresh_credential"] == "synthetic-rotated-1"
        assert not cloud_session._refresh_is_unusable(saved, "synthetic-rotated-1")
