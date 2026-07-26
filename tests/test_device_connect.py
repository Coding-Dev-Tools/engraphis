"""Tests for the client half of the device-connect flow.

``cloud_session.save_bootstrap`` had no production caller: nothing in the shipped client
ever created ``~/.engraphis/cloud_session.json``, while the docs told customers to prefer
it.  These tests pin the command that closes that gap, and in particular that the connect
token -- a bearer credential -- never escapes the request body.
"""
from __future__ import annotations

import http.client
import json
import os
import urllib.error
import urllib.request
from io import BytesIO
from pathlib import Path

import pytest

from engraphis import cloud_session, device_connect
from engraphis.hosted_client import CloudUrlUnresolved
from engraphis.private_state import UnsafeStateFile
from scripts import connect as connect_cli

CONTROL_URL = "https://control.example.test"
COMPUTE_URL = "https://compute.example.test"
TOKEN = "engr_ct_R3plAceMeWithRealEntropy_0123456789"

#: Every key the server's ``DeviceRegistrationResponse`` carries, fed to the client
#: verbatim the way a real 200 would be.
REGISTRATION = {
    "organization_id": "org_alpha",
    "installation_id": "instl_alpha",
    "device_id": "devc_alpha",
    "member_id": "mem_alpha",
    "workspace_id": "ws_alpha",
    "access_token": "short-lived-access-token",
    "token_type": "Bearer",
    "expires_in_seconds": 900,
    "refresh_credential": "rotating-refresh-credential",
    "refresh_expires_at": "2026-08-21T00:00:00Z",
    "token_subject": "device",
    "entitlement_version": 7,
    "plan": "team",
    "cloud_access_active": True,
    # No ``export``: the signed compliance export was never implemented and is no longer
    # in either repo's plan->feature table, so a fixture claiming the cloud grants it
    # would re-establish exactly the drift that removal cleaned up.
    "cloud_features": ["analytics", "automation", "sync", "team"],
}


class _Response:
    """Minimal stand-in for the ``http.client`` response the opener yields."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def read(self, size: int = -1) -> bytes:
        return self._payload


class _Opener:
    """Fake of the pinned opener, recording exactly what was put on the wire."""

    def __init__(self, *, body=None, error=None) -> None:
        self._body = body
        self._error = error
        self.calls = []

    def open(self, request, timeout):
        self.calls.append({
            "url": request.full_url,
            "method": request.get_method(),
            "headers": dict(request.headers),
            "body": request.data,
            "timeout": timeout,
        })
        if self._error is not None:
            raise self._error
        return _Response(json.dumps(self._body).encode("utf-8"))


def _install_opener(monkeypatch, opener) -> None:
    # ``build_pinned_https_opener`` is a thin wrapper over ``build_opener``; patching the
    # stdlib factory keeps the module's own call site (and its redirect handler) intact.
    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: opener)


def _http_error(status: int, body: bytes = b'{"detail":"private"}'):
    return urllib.error.HTTPError(
        CONTROL_URL + device_connect.CONNECT_PATH, status, "denied", {}, BytesIO(body)
    )


@pytest.fixture(autouse=True)
def _isolated_client_state(monkeypatch, tmp_path):
    """A private state directory and a control plane that never needs DNS."""

    monkeypatch.setenv("ENGRAPHIS_STATE_DIR", str(tmp_path))
    for name in (
        "ENGRAPHIS_CLOUD_CONTROL_URL",
        "ENGRAPHIS_CLOUD_COMPUTE_URL",
        "ENGRAPHIS_CLOUD_ACCESS_TOKEN",
        "ENGRAPHIS_CLOUD_ORGANIZATION_ID",
        "ENGRAPHIS_CLOUD_REFRESH_CREDENTIAL",
        "ENGRAPHIS_CLOUD_TOKEN_SUBJECT",
    ):
        monkeypatch.delenv(name, raising=False)
    # ``validate_cloud_base_url`` resolves the hostname, so the offline gate cannot use
    # the real one against ``.test`` names. Normalization is what callers depend on.
    normalize = lambda value: str(value).rstrip("/")  # noqa: E731
    monkeypatch.setattr(device_connect, "validate_cloud_base_url", normalize)
    monkeypatch.setattr(cloud_session, "validate_cloud_base_url", normalize)
    return tmp_path


def _state_files(root: Path):
    return [path for path in Path(root).rglob("*") if path.is_file()]


# --------------------------------------------------------------------------- happy path


def test_connect_writes_a_session_the_rest_of_the_client_can_use(monkeypatch, tmp_path):
    """The whole point: after connect, ``cloud_session.configured()`` is true.

    Before this command shipped, the only writer of the session file was a function with
    zero production callers, so this assertion could not hold for any customer.
    """

    opener = _Opener(body=REGISTRATION)
    _install_opener(monkeypatch, opener)

    summary = device_connect.connect(
        TOKEN, control_url=CONTROL_URL, compute_url=COMPUTE_URL
    )

    assert cloud_session.configured() is True
    saved = json.loads((tmp_path / "cloud_session.json").read_text(encoding="utf-8"))
    assert saved["schema"] == "engraphis-cloud-session/v1"
    assert saved["organization_id"] == "org_alpha"
    assert saved["installation_id"] == "instl_alpha"
    assert saved["device_id"] == "devc_alpha"
    assert saved["member_id"] == "mem_alpha"
    assert saved["refresh_credential"] == "rotating-refresh-credential"
    assert saved["token_subject"] == "device"
    assert saved["control_url"] == CONTROL_URL
    assert saved["compute_url"] == COMPUTE_URL
    # The entitlement travelled with the registration, so the dashboard knows the plan on
    # its very first boot instead of showing a paying Team customer the free core.
    assert saved["plan"] == "team"
    assert saved["cloud_access_active"] is True
    assert "team" in saved["cloud_features"]
    assert summary["organization_id"] == "org_alpha"


def test_connect_posts_exactly_the_documented_request(monkeypatch):
    """The endpoint 422s on unknown fields, and takes no ``organization_id`` at all."""

    opener = _Opener(body=REGISTRATION)
    _install_opener(monkeypatch, opener)

    device_connect.connect(
        TOKEN,
        control_url=CONTROL_URL,
        compute_url=COMPUTE_URL,
        workspace_id="ws_alpha",
        installation_label="CI runner",
        device_name="build-box",
    )

    call = opener.calls[0]
    assert call["url"] == CONTROL_URL + "/v1/devices/connect"
    assert call["method"] == "POST"
    assert call["timeout"] == device_connect.DEFAULT_TIMEOUT_SECONDS
    body = json.loads(call["body"].decode("utf-8"))
    assert body["connect_token"] == TOKEN
    assert body["installation_client_id"].startswith("inst_")
    assert body["device_client_id"].startswith("dev_")
    assert body["workspace_id"] == "ws_alpha"
    assert body["installation_label"] == "CI runner"
    assert body["device_name"] == "build-box"
    # The token carries the organization; sending one would be rejected.
    assert "organization_id" not in body
    assert set(body) <= {
        "connect_token", "installation_client_id", "device_client_id",
        "installation_label", "device_name", "platform", "app_version", "workspace_id",
    }


def test_optional_fields_are_omitted_rather_than_sent_empty(monkeypatch):
    opener = _Opener(body=REGISTRATION)
    _install_opener(monkeypatch, opener)

    device_connect.connect(
        TOKEN, control_url=CONTROL_URL, compute_url=COMPUTE_URL, device_name=""
    )

    body = json.loads(opener.calls[0]["body"].decode("utf-8"))
    assert "workspace_id" not in body
    assert "installation_label" not in body
    assert "device_name" not in body


def test_client_ids_are_stable_across_connects(monkeypatch):
    """A reconnect must re-present the same installation, not mint a phantom device."""

    opener = _Opener(body=REGISTRATION)
    _install_opener(monkeypatch, opener)

    first = device_connect.client_identity()
    device_connect.connect(TOKEN, control_url=CONTROL_URL, compute_url=COMPUTE_URL)
    device_connect.connect(TOKEN, control_url=CONTROL_URL, compute_url=COMPUTE_URL)

    bodies = [json.loads(call["body"].decode("utf-8")) for call in opener.calls]
    assert {body["installation_client_id"] for body in bodies} == {first[0]}
    assert {body["device_client_id"] for body in bodies} == {first[1]}
    assert device_connect.client_identity() == first


# ------------------------------------------------------------------------ failure modes


def test_expired_or_consumed_token_is_actionable_and_writes_nothing(
    monkeypatch, tmp_path, capsys
):
    """401 is the control plane's single answer for expired / consumed / invalid.

    The copy therefore has to name all three and point at the one fix, and a refused
    connect must not leave a half-written session behind for the dashboard to load.
    """

    _install_opener(monkeypatch, _Opener(error=_http_error(401)))

    exit_code = connect_cli.main([
        "--token", TOKEN, "--control-url", CONTROL_URL, "--compute-url", COMPUTE_URL,
    ])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "expired" in captured.err
    assert "already used" in captured.err
    assert "account portal" in captured.err
    assert not (tmp_path / "cloud_session.json").exists()
    assert cloud_session.configured() is False


def test_lapsed_subscription_is_distinguishable_from_a_dead_token(monkeypatch, capsys):
    """402 must not be flattened into "your token is bad" -- the fix is billing."""

    _install_opener(monkeypatch, _Opener(error=_http_error(402)))

    exit_code = connect_cli.main([
        "--token", TOKEN, "--control-url", CONTROL_URL, "--compute-url", COMPUTE_URL,
    ])

    assert exit_code == 1
    message = capsys.readouterr().err
    assert "subscription is not active" in message
    assert "billing" in message
    assert "expired" not in message

    with pytest.raises(device_connect.DeviceConnectError) as caught:
        device_connect.connect(TOKEN, control_url=CONTROL_URL, compute_url=COMPUTE_URL)
    assert caught.value.status == 402


@pytest.mark.parametrize(
    "status,fragment",
    [
        (403, "organization owner"),
        (422, "pip install -U engraphis"),
        (429, "Too many connect attempts"),
        (503, "not accepting new device activations"),
        (500, "could not connect this device"),
    ],
)
def test_each_failure_status_gets_its_own_copy(monkeypatch, status, fragment):
    _install_opener(monkeypatch, _Opener(error=_http_error(status)))

    with pytest.raises(device_connect.DeviceConnectError) as caught:
        device_connect.connect(TOKEN, control_url=CONTROL_URL, compute_url=COMPUTE_URL)

    assert fragment in str(caught.value)
    assert caught.value.status == (status if status != 500 else 503)


def test_error_body_is_never_reflected_into_the_message(monkeypatch):
    """Provider bodies are untrusted and may quote internal hosts."""

    error = _http_error(503, b'{"detail":"upstream 10.0.0.7 activation freeze"}')
    _install_opener(monkeypatch, _Opener(error=error))

    with pytest.raises(device_connect.DeviceConnectError) as caught:
        device_connect.connect(TOKEN, control_url=CONTROL_URL, compute_url=COMPUTE_URL)

    assert "10.0.0.7" not in str(caught.value)


def test_http_error_body_is_drained_and_closed(monkeypatch):
    error = _http_error(429)
    closed = []
    original_close = error.close
    error.close = lambda: (closed.append(True), original_close())
    _install_opener(monkeypatch, _Opener(error=error))

    with pytest.raises(device_connect.DeviceConnectError):
        device_connect.connect(TOKEN, control_url=CONTROL_URL, compute_url=COMPUTE_URL)

    assert closed == [True]


def test_network_failure_reports_an_outage_without_leaking_the_detail(monkeypatch):
    _install_opener(
        monkeypatch, _Opener(error=urllib.error.URLError("proxy.internal refused"))
    )

    with pytest.raises(device_connect.DeviceConnectError) as caught:
        device_connect.connect(TOKEN, control_url=CONTROL_URL, compute_url=COMPUTE_URL)

    assert caught.value.status == 503
    assert "proxy.internal" not in str(caught.value)


def test_a_success_without_a_refresh_credential_writes_nothing(monkeypatch, tmp_path):
    body = dict(REGISTRATION)
    body.pop("refresh_credential")
    _install_opener(monkeypatch, _Opener(body=body))

    with pytest.raises(device_connect.DeviceConnectError):
        device_connect.connect(TOKEN, control_url=CONTROL_URL, compute_url=COMPUTE_URL)

    assert not (tmp_path / "cloud_session.json").exists()


@pytest.mark.parametrize("payload", [b"not json", b'"a string"', b"[]"])
def test_a_non_object_response_is_rejected(monkeypatch, payload):
    class _Raw(_Opener):
        def open(self, request, timeout):
            return _Response(payload)

    _install_opener(monkeypatch, _Raw())

    with pytest.raises(device_connect.DeviceConnectError, match="invalid connect"):
        device_connect.connect(TOKEN, control_url=CONTROL_URL, compute_url=COMPUTE_URL)


# ------------------------------------------- failures *after* the token has been spent
#
# Once ``opener.open`` has returned, the control plane has answered and the single-use
# connect token is gone.  Every fault from that point on has to arrive as a
# ``DeviceConnectError`` that says so -- a traceback here is the worst possible outcome,
# because the customer cannot tell whether to retry or fetch a new token.


class _TruncatedBody:
    """A 200 whose body stops mid-stream, the way a dropped chunked reply does."""

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def read(self, size: int = -1) -> bytes:
        # What ``HTTPResponse._read_chunked`` raises for a truncated body.  It subclasses
        # ``HTTPException``/``ValueError`` -- neither ``OSError`` nor ``URLError``.
        raise http.client.IncompleteRead(b'{"organization_id":"org_al')


@pytest.mark.parametrize("failure", [
    http.client.IncompleteRead(b'{"organization_id":"org_al'),
    http.client.LineTooLong("header line"),
    http.client.BadStatusLine("garbage"),
])
def test_a_truncated_reply_reports_the_token_state_not_a_traceback(
    monkeypatch, tmp_path, failure
):
    """``IncompleteRead`` is an ``HTTPException``, so the transport clause never saw it."""

    class _Broken(_Opener):
        def open(self, request, timeout):
            self.calls.append({"body": request.data})
            if isinstance(failure, http.client.IncompleteRead):
                return _TruncatedBody()
            raise failure

    opener = _Broken()
    _install_opener(monkeypatch, opener)

    with pytest.raises(device_connect.DeviceConnectError) as caught:
        device_connect.connect(TOKEN, control_url=CONTROL_URL, compute_url=COMPUTE_URL)

    message = str(caught.value)
    assert caught.value.status == 502
    # The customer must be told the token may be gone rather than told to just retry.
    assert "may already have been used" in message
    assert TOKEN not in message
    assert not (tmp_path / "cloud_session.json").exists()


def test_a_connection_reset_before_any_reply_still_says_retry(monkeypatch):
    """``RemoteDisconnected`` is *both* an ``OSError`` and an ``HTTPException``.

    Nothing was read, so the token is untouched and "try again" remains the right copy;
    it must not be swept into the token-may-be-spent message by the new clause.
    """

    _install_opener(
        monkeypatch, _Opener(error=http.client.RemoteDisconnected("closed early"))
    )

    with pytest.raises(device_connect.DeviceConnectError) as caught:
        device_connect.connect(TOKEN, control_url=CONTROL_URL, compute_url=COMPUTE_URL)

    assert caught.value.status == 503
    assert "temporarily unreachable" in str(caught.value)


@pytest.mark.parametrize("failure", [
    CloudUrlUnresolved("cloud service URL could not be resolved"),
    ValueError("cloud service URL must not target private/reserved IP ranges"),
])
def test_endpoint_validation_failing_after_redemption_is_not_a_traceback(
    monkeypatch, tmp_path, failure
):
    """``save_bootstrap`` re-resolves both endpoints *after* the POST spent the token.

    ``CloudUrlUnresolved`` is a ``ValueError``, so neither the ``CloudSessionError`` nor
    the ``OSError`` handler in ``connect()`` covers it: a resolver that dies mid-connect,
    or a host that starts resolving to a rejected address, escaped as a raw traceback.
    """

    opener = _Opener(body=REGISTRATION)
    _install_opener(monkeypatch, opener)

    def _rejects(value):
        raise failure

    # Only the save path: the pre-POST checks in ``connect()`` already passed.
    monkeypatch.setattr(cloud_session, "validate_cloud_base_url", _rejects)

    with pytest.raises(device_connect.DeviceConnectError) as caught:
        device_connect.connect(TOKEN, control_url=CONTROL_URL, compute_url=COMPUTE_URL)

    message = str(caught.value)
    assert caught.value.status == 409
    assert opener.calls, "the token was spent; the copy has to admit it"
    assert "has been used" in message
    assert TOKEN not in message
    assert not (tmp_path / "cloud_session.json").exists()


def test_the_cli_reports_a_post_redemption_fault_instead_of_a_traceback(
    monkeypatch, capsys
):
    _install_opener(monkeypatch, _Opener(body=REGISTRATION))

    def _rejects(value):
        raise CloudUrlUnresolved("cloud service URL could not be resolved")

    monkeypatch.setattr(cloud_session, "validate_cloud_base_url", _rejects)

    code = connect_cli.main([
        "--token", TOKEN, "--control-url", CONTROL_URL, "--compute-url", COMPUTE_URL,
    ])

    err = capsys.readouterr().err
    assert code == 1
    assert "Traceback" not in err
    assert "has been used" in err


# ------------------------------------------------------ pre-flight on session storage
#
# The POST is the point of no return: the control plane consumes the single-use connect
# token as it answers.  ``save_bootstrap`` runs afterwards, so before the pre-flight a
# state directory that had lost its permissions -- or a ``cloud_session.json`` replaced
# by a link -- spent the customer's token and then failed, leaving them with nothing to
# retry with.  Every test here asserts the same thing: no request was put on the wire.


def _probe_files(root: Path):
    """Temporary files the writability probe must never leave behind."""

    return sorted(path.name for path in Path(root).iterdir() if ".preflight." in path.name)


def test_an_unsafe_session_path_fails_before_the_token_is_spent(monkeypatch, tmp_path):
    """The reported regression: a session leaf that is not a plain private file.

    A directory stands in for the symlink/hard-link family because it trips exactly the
    same ``private_file_stat`` rejection on every platform, including a Windows runner
    without the privilege to create a symlink at all.
    """

    opener = _Opener(body=REGISTRATION)
    _install_opener(monkeypatch, opener)
    # The identity file already exists, which is precisely why its earlier success is no
    # evidence that the directory is still writable now.
    device_connect.client_identity()
    session_path = tmp_path / "cloud_session.json"
    session_path.mkdir()

    with pytest.raises(device_connect.DeviceConnectError) as caught:
        device_connect.connect(TOKEN, control_url=CONTROL_URL, compute_url=COMPUTE_URL)

    # The one assertion that matters: the token was never presented, so it is still live.
    assert opener.calls == []
    assert caught.value.status == 409
    # The customer has to be told which path to fix, by name.
    assert str(session_path) in str(caught.value)
    assert "has not been used" in str(caught.value)
    assert session_path.is_dir()
    assert _probe_files(tmp_path) == []


def test_an_unsafe_refresh_lock_also_fails_before_the_token_is_spent(monkeypatch, tmp_path):
    """``save_bootstrap`` takes the refresh lock first, so an unusable lock spends it too."""

    opener = _Opener(body=REGISTRATION)
    _install_opener(monkeypatch, opener)
    lock_path = tmp_path / ".cloud_session.refresh.lock"
    lock_path.mkdir()

    with pytest.raises(device_connect.DeviceConnectError) as caught:
        device_connect.connect(TOKEN, control_url=CONTROL_URL, compute_url=COMPUTE_URL)

    assert opener.calls == []
    assert str(lock_path) in str(caught.value)
    assert not (tmp_path / "cloud_session.json").exists()


def test_a_hard_linked_session_is_refused_before_the_token_is_spent(monkeypatch, tmp_path):
    """A second pathname to the session would keep resolving to the rotated credential."""

    opener = _Opener(body=REGISTRATION)
    _install_opener(monkeypatch, opener)
    session_path = tmp_path / "cloud_session.json"
    session_path.write_text("{}", encoding="utf-8")
    try:
        os.link(str(session_path), str(tmp_path / "session_alias.json"))
    except (OSError, AttributeError, NotImplementedError) as exc:  # pragma: no cover
        pytest.skip("hard links are unavailable on this filesystem: %s" % exc)

    with pytest.raises(device_connect.DeviceConnectError) as caught:
        device_connect.connect(TOKEN, control_url=CONTROL_URL, compute_url=COMPUTE_URL)

    assert opener.calls == []
    assert str(session_path) in str(caught.value)
    # Inspection only: a pre-existing file is never opened, truncated or replaced.
    assert session_path.read_text(encoding="utf-8") == "{}"


def test_an_uncreatable_state_directory_fails_before_the_token_is_spent(
    monkeypatch, tmp_path
):
    """``ENGRAPHIS_STATE_DIR`` under a regular file: the directory can never be made."""

    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("ENGRAPHIS_STATE_DIR", str(blocker / "state"))
    opener = _Opener(body=REGISTRATION)
    _install_opener(monkeypatch, opener)

    with pytest.raises(device_connect.DeviceConnectError) as caught:
        device_connect.connect(TOKEN, control_url=CONTROL_URL, compute_url=COMPUTE_URL)

    assert opener.calls == []
    assert str(blocker / "state") in str(caught.value)
    assert blocker.read_text(encoding="utf-8") == "not a directory"


def test_an_unwritable_state_directory_fails_before_the_token_is_spent(
    monkeypatch, tmp_path
):
    """A read-only mount, which is the failure the report describes.

    Simulated at the probe rather than with ``chmod``: POSIX modes are advisory for root
    and Windows has no equivalent, so a mode-based test would silently stop failing.  The
    probe is the only thing standing between the caller and ``atomic_private_text``.
    """

    opener = _Opener(body=REGISTRATION)
    _install_opener(monkeypatch, opener)
    device_connect.client_identity()

    def _read_only(*args, **kwargs):
        raise PermissionError(13, "read-only file system")

    monkeypatch.setattr(cloud_session.tempfile, "mkstemp", _read_only)

    with pytest.raises(device_connect.DeviceConnectError) as caught:
        device_connect.connect(TOKEN, control_url=CONTROL_URL, compute_url=COMPUTE_URL)

    assert opener.calls == []
    assert caught.value.status == 409
    assert str(tmp_path) in str(caught.value)
    assert "not writable" in str(caught.value)
    assert not (tmp_path / "cloud_session.json").exists()


def test_the_preflight_creates_nothing_and_leaves_nothing_behind(monkeypatch, tmp_path):
    """A pre-flight that wrote the session would defeat the "nothing on failure" rule."""

    session_path = cloud_session.preflight_save()

    assert session_path == tmp_path / "cloud_session.json"
    assert not session_path.exists()
    assert _probe_files(tmp_path) == []

    # Idempotent, and equally inert once a real session exists.
    _install_opener(monkeypatch, _Opener(body=REGISTRATION))
    device_connect.connect(TOKEN, control_url=CONTROL_URL, compute_url=COMPUTE_URL)
    saved = session_path.read_bytes()

    assert cloud_session.preflight_save() == session_path
    assert session_path.read_bytes() == saved
    assert _probe_files(tmp_path) == []


# ------------------------------------------------------------- pre-flight token hygiene


@pytest.mark.parametrize("token", [
    "",
    "   ",
    "engr_ct_",
    "engr_ct_short",
    "ct_missing_prefix_but_long_enough_to_pass",
    "engr_ct_has spaces in the middle",
    "engr_ct_" + "x" * 600,
    "engraphis connect --token engr_ct_pasted_whole_command",
])
def test_malformed_tokens_are_rejected_before_any_network_call(monkeypatch, token):
    """A bad paste must cost nothing: no request, no rate budget, no consumed token."""

    class _Poison:
        def open(self, request, timeout):
            raise AssertionError("a malformed token must not reach the network")

    _install_opener(monkeypatch, _Poison())

    with pytest.raises(device_connect.DeviceConnectError) as caught:
        device_connect.connect(token, control_url=CONTROL_URL)

    assert caught.value.status == 400


def test_a_rejected_token_is_not_quoted_back(monkeypatch):
    """Even an invalid token is a credential: it must stay out of the error text."""

    secret = "engr_ct_" + "s" * 4  # too short, but still secret-shaped
    with pytest.raises(device_connect.DeviceConnectError) as caught:
        device_connect.connect(secret, control_url=CONTROL_URL)

    assert secret not in str(caught.value)
    assert "ssss" not in str(caught.value)


def test_surrounding_whitespace_from_a_paste_is_tolerated(monkeypatch):
    opener = _Opener(body=REGISTRATION)
    _install_opener(monkeypatch, opener)

    device_connect.connect(
        "  %s\n" % TOKEN, control_url=CONTROL_URL, compute_url=COMPUTE_URL
    )

    body = json.loads(opener.calls[0]["body"].decode("utf-8"))
    assert body["connect_token"] == TOKEN


# ------------------------------------------------------------------ credential leakage


def test_the_token_never_reaches_stdout_stderr_or_disk(monkeypatch, tmp_path, capsys):
    """The one invariant that has to hold on the success path too.

    A token echoed into a terminal lands in scrollback, CI logs and screenshots; a token
    persisted next to the session outlives the single use it was minted for.
    """

    opener = _Opener(body=REGISTRATION)
    _install_opener(monkeypatch, opener)

    exit_code = connect_cli.main([
        "--token", TOKEN, "--control-url", CONTROL_URL, "--compute-url", COMPUTE_URL,
    ])
    assert exit_code == 0

    captured = capsys.readouterr()
    assert TOKEN not in captured.out
    assert TOKEN not in captured.err
    written = _state_files(tmp_path)
    assert written, "connect must actually write the session"
    for path in written:
        assert TOKEN.encode("utf-8") not in path.read_bytes(), "token leaked into %s" % path
    # It did go where it belongs.
    assert TOKEN.encode("utf-8") in opener.calls[0]["body"]


def test_the_session_secrets_are_not_printed(monkeypatch, capsys):
    _install_opener(monkeypatch, _Opener(body=REGISTRATION))

    connect_cli.main([
        "--token", TOKEN, "--control-url", CONTROL_URL, "--compute-url", COMPUTE_URL,
    ])

    captured = capsys.readouterr()
    assert REGISTRATION["refresh_credential"] not in captured.out
    assert REGISTRATION["access_token"] not in captured.out
    assert "org_alpha" in captured.out  # the useful, non-secret parts are shown


def test_the_json_summary_carries_no_secrets(monkeypatch, capsys):
    _install_opener(monkeypatch, _Opener(body=REGISTRATION))

    connect_cli.main([
        "--token", TOKEN, "--control-url", CONTROL_URL, "--compute-url", COMPUTE_URL,
        "--json",
    ])

    summary = json.loads(capsys.readouterr().out)
    assert "refresh_credential" not in summary
    assert "access_token" not in summary
    assert summary["plan"] == "team"
    assert summary["control_url"] == CONTROL_URL


def test_summarize_drops_every_secret_field():
    summary = device_connect.summarize(REGISTRATION)
    assert set(summary) & {"refresh_credential", "access_token"} == set()
    assert summary["organization_id"] == "org_alpha"


# ------------------------------------------------------------------------- CLI surface


def test_cli_reports_the_connection_and_the_next_step(monkeypatch, capsys):
    _install_opener(monkeypatch, _Opener(body=REGISTRATION))

    assert connect_cli.main([
        "--token", TOKEN, "--control-url", CONTROL_URL, "--compute-url", COMPUTE_URL,
    ]) == 0

    out = capsys.readouterr().out
    assert "Connected this device to Engraphis Cloud." in out
    assert "team" in out
    assert "cloud_session.json" in out
    assert "engraphis-dashboard" in out


def test_cli_says_so_when_compute_is_not_configured(monkeypatch, capsys):
    """Connecting against a custom control plane must not silently half-configure."""

    _install_opener(monkeypatch, _Opener(body=REGISTRATION))

    assert connect_cli.main(["--token", TOKEN, "--control-url", CONTROL_URL]) == 0

    out = capsys.readouterr().out
    assert "hosted compute is not configured" in out
    assert "ENGRAPHIS_CLOUD_COMPUTE_URL" in out
    assert cloud_session.configured(require_compute=False) is True


def test_compute_url_is_taken_from_the_environment(monkeypatch):
    monkeypatch.setenv("ENGRAPHIS_CLOUD_COMPUTE_URL", COMPUTE_URL)
    _install_opener(monkeypatch, _Opener(body=REGISTRATION))

    summary = device_connect.connect(TOKEN, control_url=CONTROL_URL)

    assert summary["compute_url"] == COMPUTE_URL
    assert cloud_session.configured() is True


def test_non_shipped_control_plane_uses_its_vetted_compute_url(monkeypatch, tmp_path):
    """The connect response, not a shipped-only fallback, configures custom planes."""

    server_compute = "https://assigned-compute.example.test"
    _install_opener(monkeypatch, _Opener(body=dict(REGISTRATION, compute_url=server_compute)))

    summary = device_connect.connect(TOKEN, control_url=CONTROL_URL)

    saved = json.loads((tmp_path / "cloud_session.json").read_text(encoding="utf-8"))
    assert summary["compute_url"] == server_compute
    assert saved["compute_url"] == server_compute
    assert saved["compute_url_source"] == "server"
    assert cloud_session.configured() is True


@pytest.mark.parametrize("source", ["cli", "environment"])
def test_explicit_compute_override_outranks_the_connect_response(monkeypatch, source):
    """A server cannot replace an operator-selected endpoint during device connect."""

    server_compute = "https://assigned-compute.example.test"
    kwargs = {}
    if source == "cli":
        kwargs["compute_url"] = COMPUTE_URL
    else:
        monkeypatch.setenv("ENGRAPHIS_CLOUD_COMPUTE_URL", COMPUTE_URL)
    _install_opener(monkeypatch, _Opener(body=dict(REGISTRATION, compute_url=server_compute)))

    summary = device_connect.connect(TOKEN, control_url=CONTROL_URL, **kwargs)
    assert summary["compute_url"] == COMPUTE_URL
    assert cloud_session._load()["compute_url_source"] == "explicit"


def test_empty_cli_compute_value_does_not_suppress_the_connect_response(monkeypatch):
    server_compute = "https://assigned-compute.example.test"
    _install_opener(monkeypatch, _Opener(body=dict(REGISTRATION, compute_url=server_compute)))

    assert device_connect.connect(
        TOKEN, control_url=CONTROL_URL, compute_url=""
    )["compute_url"] == server_compute


def test_control_url_defaults_to_the_shipped_manifest(monkeypatch):
    from engraphis.commercial import manifest

    assert device_connect.default_control_url() == manifest()["control_plane"]
    # And only the shipped control plane gets a guessed compute endpoint.
    assert device_connect.default_compute_url(manifest()["control_plane"]) == \
        device_connect.DEFAULT_COMPUTE_URL
    assert device_connect.default_compute_url(CONTROL_URL) == ""


def test_the_manifest_outranks_the_compute_constant(monkeypatch):
    """A published ``compute_plane`` wins, so the manifest stays the endpoint authority.

    The constant is only the fallback for today's manifest, which declares no
    ``compute_plane``; reading the key alone would resolve ``""`` and save a session
    ``configured()`` rejects.
    """

    from engraphis.commercial import manifest

    shipped = manifest()["control_plane"]
    monkeypatch.setattr(
        "engraphis.commercial.manifest",
        lambda: {"control_plane": shipped, "compute_plane": COMPUTE_URL},
    )

    assert device_connect.default_compute_url(shipped) == COMPUTE_URL
    # A self-hosted control plane still gets no guess from either source.
    assert device_connect.default_compute_url(CONTROL_URL) == ""


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf", "0", "-5"])
def test_a_non_finite_timeout_is_refused_before_any_request(monkeypatch, bad):
    """``argparse``'s ``type=float`` accepts ``nan``/``inf``; the socket layer does not.

    Those reach ``urllib``'s deadline arithmetic and raise ``ValueError``/``OverflowError``
    that ``post_connect`` does not catch, so the CLI printed a traceback instead of its
    structured error.  Nothing may be sent, either.
    """

    class _Exploding:
        @staticmethod
        def open(request, timeout=None):  # pragma: no cover - must never run
            raise AssertionError("a request was started with an unusable timeout")

    _install_opener(monkeypatch, _Exploding())

    with pytest.raises(device_connect.DeviceConnectError) as caught:
        device_connect.connect(TOKEN, control_url=CONTROL_URL, timeout=float(bad))

    assert caught.value.status == 400
    assert "timeout" in str(caught.value)


def test_the_cli_reports_a_bad_timeout_instead_of_a_traceback(monkeypatch, capsys):
    class _Exploding:
        @staticmethod
        def open(request, timeout=None):  # pragma: no cover - must never run
            raise AssertionError("a request was started with an unusable timeout")

    _install_opener(monkeypatch, _Exploding())

    code = connect_cli.main(
        ["--token", TOKEN, "--control-url", CONTROL_URL, "--timeout", "nan"]
    )

    assert code == 1
    err = capsys.readouterr().err
    assert "timeout" in err
    assert "Traceback" not in err


def test_a_storage_fault_racing_the_save_is_not_a_traceback(monkeypatch):
    """``UnsafeStateFile`` is an ``OSError``, not a ``CloudSessionError``.

    The pre-flight closes the common case, but the state directory can still change
    between the pre-flight and the write.  The token is spent by then, so the customer
    must be told that plainly rather than shown a stack trace.
    """

    _install_opener(monkeypatch, _Opener(body=REGISTRATION))

    def _boom(*args, **kwargs):
        raise UnsafeStateFile("cloud_session.json is not a plain private file")

    monkeypatch.setattr(cloud_session, "save_bootstrap", _boom)

    with pytest.raises(device_connect.DeviceConnectError) as caught:
        device_connect.connect(TOKEN, control_url=CONTROL_URL)

    message = str(caught.value)
    assert caught.value.status == 409
    # The token is gone; the copy must not promise it can be reused.
    assert "has been used" in message
    assert TOKEN not in message


def test_control_url_env_override_wins(monkeypatch):
    monkeypatch.setenv("ENGRAPHIS_CLOUD_CONTROL_URL", CONTROL_URL)
    assert device_connect.default_control_url() == CONTROL_URL


def test_an_unconfigured_control_plane_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(device_connect, "default_control_url", lambda: "")

    with pytest.raises(device_connect.DeviceConnectError) as caught:
        device_connect.connect(TOKEN)

    assert "ENGRAPHIS_CLOUD_CONTROL_URL" in str(caught.value)
    assert caught.value.status == 400


def test_token_dash_reads_stdin(monkeypatch, capsys):
    _install_opener(monkeypatch, _Opener(body=REGISTRATION))

    class _Stdin:
        @staticmethod
        def isatty() -> bool:
            return False

        @staticmethod
        def readline() -> str:
            return TOKEN + "\n"

    monkeypatch.setattr(connect_cli.sys, "stdin", _Stdin)

    assert connect_cli.main([
        "--token", "-", "--control-url", CONTROL_URL, "--compute-url", COMPUTE_URL,
    ]) == 0
    assert TOKEN not in capsys.readouterr().out


def test_token_dash_on_a_terminal_explains_itself_instead_of_hanging(monkeypatch, capsys):
    class _Tty:
        @staticmethod
        def isatty() -> bool:
            return True

    monkeypatch.setattr(connect_cli.sys, "stdin", _Tty)

    assert connect_cli.main(["--token", "-", "--control-url", CONTROL_URL]) == 1
    assert "reads the token from stdin" in capsys.readouterr().err


def test_connect_is_reachable_as_the_documented_engraphis_verb(monkeypatch, capsys):
    """The portal shows `engraphis connect --token ...`; that string has to run."""

    from scripts import entry

    _install_opener(monkeypatch, _Opener(body=REGISTRATION))
    assert entry.COMMANDS["connect"] == "scripts.connect:main"

    assert entry.main([
        "connect", "--token", TOKEN, "--control-url", CONTROL_URL,
        "--compute-url", COMPUTE_URL,
    ]) == 0
    assert "Connected this device to Engraphis Cloud." in capsys.readouterr().out


def test_the_dispatcher_restores_argv_and_rejects_unknown_verbs(capsys):
    from scripts import entry

    saved = list(entry.sys.argv)
    assert entry.main(["definitely-not-a-command"]) == 2
    assert entry.sys.argv == saved
    assert "unknown command" in capsys.readouterr().err


def test_every_dispatched_verb_resolves_to_a_real_callable():
    from importlib import import_module

    from scripts import entry

    for verb, target in entry.COMMANDS.items():
        module_name, _, attribute = target.partition(":")
        assert callable(getattr(import_module(module_name), attribute)), verb
