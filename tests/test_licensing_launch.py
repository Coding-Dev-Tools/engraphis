"""Launch guards for the paid-customer path: entitlement copy, secrets, and hangs.

Every test here pins a behaviour a paying Pro/Team customer depends on at launch:

* a lapsed or revoked subscription must be told how to fix billing, not shown an outage;
* an offline or flaky control plane must degrade to a retryable error, never a traceback
  and never a permanent "your configuration is invalid";
* a bearer token must not be renderable from a client object; and
* one caller-supplied timeout must bound the whole dial, not each resolved address.
"""
from __future__ import annotations

import io
import json
import socket
import urllib.error
import http.client

import pytest

from engraphis import cloud_features, cloud_session, hosted_client, licensing
from engraphis.cloud_features import CloudFeatureClient, CloudFeatureError
from engraphis.cloud_session import CloudSessionError

# ── the private control plane's vocabulary, mirrored from (read-only) ──────────
# the hosted entitlement contract:20-28 and security.py:37-38.
# A mismatch here means a purchased feature silently never activates on the client.
SERVER_PLANS = {"free", "pro", "team"}
SERVER_PAID_PLANS = {"pro", "team"}
SERVER_HOSTED_ENTITLEMENTS = {
    "free": set(),
    "pro": {"analytics", "automation", "export", "sync"},
    "team": {"analytics", "automation", "export", "sync", "team"},
}
SERVER_TRIAL_DURATION_SECONDS = 259_200
SERVER_WORKSPACE_WRITE_GRACE_MAX_SECONDS = 86_400


@pytest.fixture()
def _upgrade_url(monkeypatch):
    monkeypatch.setenv("ENGRAPHIS_UPGRADE_URL", "https://account.example.test/billing")
    monkeypatch.delenv("ENGRAPHIS_PRO_UPGRADE_URL", raising=False)
    monkeypatch.delenv("ENGRAPHIS_TEAM_UPGRADE_URL", raising=False)
    return "https://account.example.test/billing"


def _http_error(status: int, body: bytes = b"{}") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://control.example.test/v1/tokens/refresh",
        status,
        "failure",
        {},
        io.BytesIO(body),
    )


def _opener_raising(error):
    class _Opener:
        def open(self, request, timeout=None):
            raise error

    return lambda *handlers: _Opener()


# ── (c) credentials must never be renderable ──────────────────────────────────
def test_cloud_client_repr_never_renders_the_bearer_token() -> None:
    """A dataclass __repr__ prints every field, so the token would reach any log."""

    client = CloudFeatureClient(
        "https://compute.example.test", "org_1", "eyJ-live-bearer-token"
    )

    assert "eyJ-live-bearer-token" not in repr(client)
    assert "eyJ-live-bearer-token" not in str(client)
    assert "eyJ-live-bearer-token" not in "%r" % (client,)
    # The token is still carried; only its rendering is suppressed.
    assert client.access_token == "eyJ-live-bearer-token"


def test_cloud_client_repr_keeps_the_non_secret_fields() -> None:
    client = CloudFeatureClient("https://compute.example.test", "org_1", "token")

    assert "compute.example.test" in repr(client)
    assert "org_1" in repr(client)


# ── (b) a flaky control plane must not produce a traceback ────────────────────
def test_refresh_survives_an_error_body_that_fails_to_read(monkeypatch) -> None:
    """Draining an HTTPError body can itself time out.

    A sibling ``except`` clause does not cover an exception raised inside the HTTPError
    handler, so an unguarded ``exc.read()`` escaped as an unhandled traceback and became
    an opaque dashboard 500 exactly when the cloud was flaky.
    """

    class _Unreadable(io.BytesIO):
        def read(self, *args, **kwargs):
            raise TimeoutError("the read timed out")

        def close(self):
            if self.closed:
                return
            # Model a reset after the descriptor was released.  Leaving BytesIO open
            # would make its finalizer repeat the synthetic failure as an unraisable
            # warning after the behaviour under test has already handled it.
            super().close()
            raise OSError("the socket was already reset")

    error = urllib.error.HTTPError(
        "https://control.example.test/v1/tokens/refresh", 500, "failure", {}, _Unreadable()
    )
    monkeypatch.setattr(
        cloud_session, "build_pinned_https_opener", _opener_raising(error)
    )

    with pytest.raises(CloudSessionError) as caught:
        cloud_session._post_refresh("https://control.example.test", "r", "ws", "member")

    assert caught.value.status == 503


@pytest.mark.parametrize("status", [400, 404, 409, 418, 500, 502, 503])
def test_every_refresh_status_degrades_to_a_structured_error(monkeypatch, status) -> None:
    monkeypatch.setattr(
        cloud_session, "build_pinned_https_opener", _opener_raising(_http_error(status))
    )

    with pytest.raises(CloudSessionError) as caught:
        cloud_session._post_refresh("https://control.example.test", "r", "ws", "member")

    assert 400 <= caught.value.status <= 599


def test_malformed_refresh_json_is_a_structured_error(monkeypatch) -> None:
    class _Response:
        def read(self, size):
            return b"{not json"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _Opener:
        def open(self, request, timeout=None):
            return _Response()

    monkeypatch.setattr(
        cloud_session, "build_pinned_https_opener", lambda *handlers: _Opener()
    )

    with pytest.raises(CloudSessionError, match="invalid session response"):
        cloud_session._post_refresh("https://control.example.test", "r", "ws", "member")


@pytest.mark.parametrize(
    "error",
    [
        urllib.error.URLError(ConnectionRefusedError("connection refused")),
        urllib.error.URLError(socket.gaierror("name resolution failed")),
        urllib.error.URLError(TimeoutError("timed out before send")),
    ],
)
def test_transport_failures_report_a_retryable_outage(monkeypatch, error) -> None:
    monkeypatch.setattr(
        cloud_session, "build_pinned_https_opener", _opener_raising(error)
    )

    with pytest.raises(CloudSessionError, match="temporarily unreachable"):
        cloud_session._post_refresh("https://control.example.test", "r", "ws", "member")


@pytest.mark.parametrize("error", [
    TimeoutError("timed out waiting for status"),
    http.client.RemoteDisconnected("closed waiting for status"),
    ConnectionResetError("reset waiting for status"),
    OSError("TLS connection failed while reading status"),
])
def test_post_send_refresh_transport_failures_require_reconnect(monkeypatch, error) -> None:
    """Unwrapped getresponse failures are ambiguous after the refresh POST was written."""

    monkeypatch.setattr(
        cloud_session, "build_pinned_https_opener", _opener_raising(error)
    )

    with pytest.raises(CloudSessionError, match="Connect this installation again") as caught:
        cloud_session._post_refresh("https://control.example.test", "r", "ws", "member")

    assert caught.value.status == 409


@pytest.mark.parametrize("error", [
    http.client.BadStatusLine("garbled status"),
    http.client.LineTooLong("status line too long"),
])
def test_malformed_refresh_status_after_post_requires_reconnect(monkeypatch, error) -> None:
    """The POST may have spent the credential before ``getresponse`` rejects its status."""

    monkeypatch.setattr(
        cloud_session, "build_pinned_https_opener", _opener_raising(error)
    )

    with pytest.raises(CloudSessionError, match="Connect this installation again") as caught:
        cloud_session._post_refresh("https://control.example.test", "r", "ws", "member")

    assert caught.value.status == 409


# ── (a)/(6) billing and authorization copy must be actionable ─────────────────
def test_lapsed_subscription_is_not_reported_as_an_outage(monkeypatch, _upgrade_url):
    """402 is the control plane's "no active paid entitlement".

    Reporting it with the generic 503 outage copy made a past_due customer retry forever
    instead of being sent to the one page that restores their features.
    """

    monkeypatch.setattr(
        cloud_session, "build_pinned_https_opener", _opener_raising(_http_error(402))
    )

    with pytest.raises(CloudSessionError) as caught:
        cloud_session._post_refresh("https://control.example.test", "r", "ws", "member")

    assert caught.value.status == 402
    assert _upgrade_url in str(caught.value)
    assert "billing" in str(caught.value).lower()


@pytest.mark.parametrize("status", [401, 403])
def test_revoked_session_still_asks_for_a_clean_reconnect(monkeypatch, status) -> None:
    monkeypatch.setattr(
        cloud_session, "build_pinned_https_opener", _opener_raising(_http_error(status))
    )

    with pytest.raises(CloudSessionError, match="connect again") as caught:
        cloud_session._post_refresh("https://control.example.test", "r", "ws", "member")

    assert caught.value.status == status


def test_rate_limited_refresh_keeps_its_own_status(monkeypatch) -> None:
    monkeypatch.setattr(
        cloud_session, "build_pinned_https_opener", _opener_raising(_http_error(429))
    )

    with pytest.raises(CloudSessionError) as caught:
        cloud_session._post_refresh("https://control.example.test", "r", "ws", "member")

    assert caught.value.status == 429
    assert "Try again shortly" in str(caught.value)


def test_unregistered_installation_is_a_reconnect_not_an_outage(monkeypatch) -> None:
    """404 means the org/entitlement row is gone; retrying forever cannot fix that."""

    monkeypatch.setattr(
        cloud_session, "build_pinned_https_opener", _opener_raising(_http_error(404))
    )

    with pytest.raises(CloudSessionError, match="connect again") as caught:
        cloud_session._post_refresh("https://control.example.test", "r", "ws", "member")

    assert caught.value.status == 409


def test_feature_gate_402_points_at_billing(_upgrade_url) -> None:
    message, transient = cloud_features._public_http_error(402)

    assert _upgrade_url in message
    assert transient is False


def test_untrusted_provider_bodies_are_still_never_reflected(monkeypatch) -> None:
    secret = "provider-secret https://internal.service/trace"
    error = urllib.error.HTTPError(
        "https://compute.example.test/private",
        402,
        "failure",
        {},
        io.BytesIO(json.dumps({"detail": secret}).encode("utf-8")),
    )

    class _Opener:
        def open(self, request, timeout):
            raise error

    monkeypatch.setattr(
        cloud_features.urllib.request, "build_opener", lambda *handlers: _Opener()
    )
    client = CloudFeatureClient("https://compute.example.test", "org_1", "token")

    with pytest.raises(CloudFeatureError) as caught:
        client._request("GET", "/private")

    assert secret not in str(caught.value)
    assert caught.value.status == 402


# ── (b) offline must not read as "your configuration is invalid" ──────────────
def _configure_cloud(monkeypatch) -> None:
    monkeypatch.setenv("ENGRAPHIS_CLOUD_CONTROL_URL", "https://control.example.test")
    monkeypatch.setenv("ENGRAPHIS_CLOUD_COMPUTE_URL", "https://compute.example.test")
    monkeypatch.setenv("ENGRAPHIS_CLOUD_REFRESH_CREDENTIAL", "saved-refresh")


def test_offline_customer_gets_a_retryable_outage_not_a_config_error(monkeypatch):
    """A broken resolver and a bad URL both raised ValueError from URL validation.

    The caller turned that into a permanent, non-retryable 409 "configuration is
    invalid", which is wrong and unactionable for a paying customer on a plane.
    """

    _configure_cloud(monkeypatch)

    def _unresolvable(*args, **kwargs):
        raise socket.gaierror("Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", _unresolvable)

    with pytest.raises(CloudFeatureError) as caught:
        CloudFeatureClient.from_environment("ws_1")

    assert caught.value.status == 503
    assert caught.value.transient is True
    assert "configuration is invalid" not in str(caught.value)


def test_unresolvable_host_stays_a_value_error_for_direct_callers(monkeypatch) -> None:
    """The new error narrows the type without breaking ``except ValueError`` callers."""

    def _unresolvable(*args, **kwargs):
        raise socket.gaierror("Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", _unresolvable)

    assert issubclass(hosted_client.CloudUrlUnresolved, ValueError)
    with pytest.raises(ValueError, match="could not be resolved"):
        hosted_client.validate_cloud_base_url("https://cloud.example.test")


def test_a_genuinely_invalid_url_is_still_a_permanent_config_error(monkeypatch) -> None:
    monkeypatch.setenv("ENGRAPHIS_CLOUD_CONTROL_URL", "ftp://control.example.test")
    monkeypatch.setenv("ENGRAPHIS_CLOUD_COMPUTE_URL", "https://compute.example.test")
    monkeypatch.setenv("ENGRAPHIS_CLOUD_REFRESH_CREDENTIAL", "saved-refresh")

    with pytest.raises(CloudFeatureError) as caught:
        CloudFeatureClient.from_environment("ws_1")

    assert caught.value.status == 409


@pytest.mark.parametrize(
    ("status", "transient"),
    [(401, False), (402, False), (409, False), (429, True), (503, True)],
)
def test_session_failures_carry_a_usable_retry_signal(monkeypatch, status, transient):
    def _fail(*args, **kwargs):
        raise CloudSessionError("private local state detail", status=status)

    monkeypatch.setattr(cloud_features, "access_for_workspace", _fail)

    with pytest.raises(CloudFeatureError) as caught:
        CloudFeatureClient.from_environment("ws_1")

    assert caught.value.status == status
    assert caught.value.transient is transient
    # Session text can quote local state paths and must not cross this boundary.
    assert "private local state detail" not in str(caught.value)


def test_unconnected_installation_is_told_to_connect(monkeypatch) -> None:
    def _fail(*args, **kwargs):
        raise CloudSessionError("connect first", status=401)

    monkeypatch.setattr(cloud_features, "access_for_workspace", _fail)

    with pytest.raises(CloudFeatureError, match="Connect this installation"):
        CloudFeatureClient.from_environment("ws_1")


# ── (b) one timeout budget must bound the whole dial ──────────────────────────
def test_one_timeout_budget_is_shared_across_every_resolved_address(monkeypatch):
    """Handing each vetted address the full timeout multiplied the stated budget.

    ``cloud_session`` dials while holding an exclusive cross-process refresh lock, so a
    multi-homed endpoint that blackholes traffic stalled every worker for N x timeout.
    """

    addresses = ["93.184.216.34", "93.184.216.35", "93.184.216.36", "93.184.216.37"]
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in addresses
        ],
    )

    class _Clock:
        now = 0.0

        def monotonic(self):
            return self.now

    clock = _Clock()
    monkeypatch.setattr(hosted_client, "time", clock)

    handed = []

    def _connect(address, timeout, source):
        handed.append(timeout)
        clock.now += timeout  # a blackholed address burns its whole allowance
        raise OSError("connection timed out")

    connection = hosted_client.PinnedHTTPSConnection("cloud.example", timeout=10.0)
    connection._create_connection = _connect

    with pytest.raises(OSError):
        connection.connect()

    assert handed, "the dial must attempt at least one vetted address"
    assert sum(handed) <= 10.0 + hosted_client.MIN_ATTEMPT_TIMEOUT_SECONDS
    assert len(handed) < len(addresses)


def test_a_fast_failure_still_tries_every_vetted_address(monkeypatch) -> None:
    """Bounding the budget must not cost the existing dual-stack failover."""

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:2800:220:1::1", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
        ],
    )
    tried = []

    class _Context:
        def wrap_socket(self, sock, *, server_hostname):
            return sock

    def _connect(address, timeout, source):
        tried.append(address[0])
        if ":" in address[0]:
            raise OSError("IPv6 route is unavailable")
        return object()

    connection = hosted_client.PinnedHTTPSConnection("cloud.example", timeout=10.0)
    connection._context = _Context()
    connection._create_connection = _connect

    connection.connect()

    assert tried == ["2606:2800:220:1::1", "93.184.216.34"]


def test_pinned_connection_never_inherits_a_blocking_default() -> None:
    """urllib supplies ``_GLOBAL_DEFAULT_TIMEOUT`` when a caller omits ``timeout=``."""

    default = hosted_client.DEFAULT_CONNECT_TIMEOUT_SECONDS

    assert hosted_client.PinnedHTTPSConnection("cloud.example").timeout == default
    assert hosted_client.PinnedHTTPSConnection(
        "cloud.example", timeout=None
    ).timeout == default
    assert hosted_client.PinnedHTTPSConnection(
        "cloud.example", timeout=socket._GLOBAL_DEFAULT_TIMEOUT
    ).timeout == default
    # An explicit budget is still honoured exactly.
    assert hosted_client.PinnedHTTPSConnection(
        "cloud.example", timeout=7.5
    ).timeout == 7.5


# ── (d) plan drift against the private control plane ──────────────────────────
def test_client_plan_names_match_the_control_plane() -> None:
    from engraphis.commercial import expected_checkout_targets, manifest

    assert set(manifest()["plans"]) == SERVER_PLANS
    assert {plan for plan, _ in expected_checkout_targets()} == SERVER_PAID_PLANS


def test_every_server_feature_key_advertises_the_plan_that_grants_it() -> None:
    """A feature the server grants only to Team must not advertise Pro, and vice versa."""

    for feature in sorted(SERVER_HOSTED_ENTITLEMENTS["team"]):
        granted_by_pro = feature in SERVER_HOSTED_ENTITLEMENTS["pro"]
        expected = "pro" if granted_by_pro else "team"
        assert licensing.required_plan(feature) == expected, feature
        assert licensing.required_plan(feature) in SERVER_PAID_PLANS


def test_plan_lookups_are_case_and_whitespace_insensitive(_upgrade_url, monkeypatch):
    """The control plane emits lowercase plans; a caller must not be able to drift."""

    monkeypatch.setenv("ENGRAPHIS_TEAM_UPGRADE_URL", "https://account.example.test/team")

    for spelling in ("team", "Team", "TEAM", " team "):
        assert licensing.required_plan(spelling) == "team"
        assert licensing.upgrade_url(spelling) == "https://account.example.test/team"
    for spelling in ("sync", "Sync", " SYNC "):
        assert licensing.required_plan(spelling) == "pro"


def test_trial_and_grace_windows_match_the_control_plane() -> None:
    assert licensing.TRIAL_SECONDS == SERVER_TRIAL_DURATION_SECONDS
    assert (
        licensing.MAX_LOCAL_WRITE_GRACE_SECONDS
        == SERVER_WORKSPACE_WRITE_GRACE_MAX_SECONDS
    )


def test_unknown_features_never_advertise_a_plan_the_server_cannot_sell() -> None:
    for feature in ("", "   ", "not-a-real-feature", None):
        assert licensing.required_plan(feature) in SERVER_PAID_PLANS
