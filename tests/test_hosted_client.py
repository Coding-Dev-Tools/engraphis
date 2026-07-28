from __future__ import annotations

import io
import socket
import urllib.request

import pytest

from engraphis import hosted_client, licensing


def test_hosted_lifecycle_constants_keep_trial_and_grace_separate():
    assert hosted_client.TRIAL_DAYS == 3
    assert hosted_client.TRIAL_SECONDS == 259_200
    assert hosted_client.MAX_HOSTED_ACCOUNT_GRACE_SECONDS == 86_400
    assert hosted_client.MAX_LOCAL_WRITE_GRACE_SECONDS == 86_400


def test_upgrade_urls_are_hosted_metadata_only(monkeypatch):
    monkeypatch.delenv("ENGRAPHIS_UPGRADE_URL", raising=False)
    monkeypatch.delenv("ENGRAPHIS_PRO_UPGRADE_URL", raising=False)
    monkeypatch.delenv("ENGRAPHIS_TEAM_UPGRADE_URL", raising=False)
    monkeypatch.delenv("ENGRAPHIS_CLOUD_URL", raising=False)

    assert hosted_client.upgrade_url("pro") == (
        "https://api.engraphis.com/account?plan=pro&interval=monthly#billing"
    )
    assert hosted_client.upgrade_url("pro", "annual") == (
        "https://api.engraphis.com/account?plan=pro&interval=annual#billing"
    )
    assert hosted_client.upgrade_url("team") == (
        "https://api.engraphis.com/account?plan=team&interval=monthly#billing"
    )
    assert hosted_client.upgrade_url("team", "annual") == (
        "https://api.engraphis.com/account?plan=team&interval=annual#billing"
    )
    assert hosted_client.required_plan("sync") == "pro"
    assert hosted_client.required_plan("team") == "team"


@pytest.mark.parametrize("value", [
    "https://operator:secret@billing.example.test/account",
    "https://billing.example.test:not-a-port/account",
    "https://billing.example.test/\naccount",
])
def test_hosted_checkout_overrides_reject_userinfo_and_invalid_ports(monkeypatch, value):
    """Never render a plan CTA that embeds credentials or cannot be opened safely."""

    monkeypatch.setenv("ENGRAPHIS_PRO_UPGRADE_URL", value)
    monkeypatch.setenv("ENGRAPHIS_UPGRADE_URL", value)

    assert hosted_client.upgrade_url("pro") == (
        "https://api.engraphis.com/account?plan=pro&interval=monthly#billing"
    )
    assert hosted_client.account_url() == "https://api.engraphis.com/account"


def test_cloud_url_validation_requires_safe_remote_https(monkeypatch):
    monkeypatch.setattr(
        hosted_client.socket,
        "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("1.2.3.4", 0))],
    )
    assert hosted_client.validate_cloud_base_url("http://127.0.0.1:8700/") == (
        "http://127.0.0.1:8700"
    )
    assert hosted_client.validate_cloud_base_url("https://cloud.example/path/") == (
        "https://cloud.example/path"
    )

    for invalid in (
        "http://cloud.example",
        "https://user:secret@cloud.example",
        "https://cloud.example/path?secret=value",
    ):
        with pytest.raises(ValueError):
            hosted_client.validate_cloud_base_url(invalid)


def test_cloud_url_validation_rejects_unresolvable_hosts(monkeypatch):
    monkeypatch.setattr(
        hosted_client.socket,
        "getaddrinfo",
        lambda *a, **k: (_ for _ in ()).throw(hosted_client.socket.gaierror),
    )
    with pytest.raises(ValueError, match="could not be resolved"):
        hosted_client.validate_cloud_base_url("https://unresolvable.example/")


@pytest.mark.parametrize("address", ["10.0.0.1", "100.64.0.1", "169.254.169.254"])
def test_cloud_url_validation_rejects_every_non_global_address(address):
    with pytest.raises(ValueError, match="private/reserved"):
        hosted_client.validate_cloud_base_url("https://%s" % address)


def test_cloud_url_validation_does_not_treat_arbitrary_localhost_subdomains_as_loopback():
    with pytest.raises(ValueError, match="must use HTTPS"):
        hosted_client.validate_cloud_base_url("http://attacker.localhost")


def test_pinned_https_connection_uses_vetted_address_and_original_tls_name(monkeypatch):
    monkeypatch.setattr(
        hosted_client.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )
    connected = []
    wrapped = []

    class _Context:
        def wrap_socket(self, sock, *, server_hostname):
            wrapped.append(server_hostname)
            return sock

    connection = hosted_client.PinnedHTTPSConnection("cloud.example")
    connection._context = _Context()
    connection._create_connection = (
        lambda address, timeout, source: connected.append(address) or object()
    )

    connection.connect()

    assert connected == [("93.184.216.34", 443)]
    assert wrapped == ["cloud.example"]


def test_pinned_https_connection_falls_back_to_next_vetted_address(monkeypatch):
    monkeypatch.setattr(
        hosted_client.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:2800:220:1:248:1893:25c8:1946", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
        ],
    )
    connected = []

    class _Context:
        def wrap_socket(self, sock, *, server_hostname):
            return sock

    def connect(address, timeout, source):
        connected.append(address)
        if ":" in address[0]:
            raise OSError("IPv6 route is unavailable")
        return object()

    connection = hosted_client.PinnedHTTPSConnection("cloud.example")
    connection._context = _Context()
    connection._create_connection = connect

    connection.connect()

    assert connected == [
        ("2606:2800:220:1:248:1893:25c8:1946", 443),
        ("93.184.216.34", 443),
    ]


def test_pinned_https_connection_rejects_rebound_private_address(monkeypatch):
    monkeypatch.setattr(
        hosted_client.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
        ],
    )
    connection = hosted_client.PinnedHTTPSConnection("cloud.example")
    connection._create_connection = lambda *args: pytest.fail(
        "private rebound address reached the socket"
    )

    with pytest.raises(ValueError, match="private/reserved"):
        connection.connect()


def test_pinned_https_proxy_tunnel_uses_vetted_ip_and_original_tls_name(monkeypatch):
    monkeypatch.setattr(
        hosted_client.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )
    connection = hosted_client.PinnedHTTPSConnection("proxy.example")

    connection.set_tunnel("cloud.example", 443)

    assert connection._tunnel_host == "93.184.216.34"
    assert connection._tls_server_hostname == "cloud.example"


def test_pinned_https_proxy_tunnel_accepts_an_explicit_port_in_the_host(monkeypatch):
    monkeypatch.setattr(
        hosted_client.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 8443))
        ],
    )
    connection = hosted_client.PinnedHTTPSConnection("proxy.example")

    # urllib hands the raw netloc to set_tunnel, port included.
    connection.set_tunnel("cloud.example:8443")

    assert connection._tunnel_host == "93.184.216.34"
    assert connection._tunnel_port == 8443
    assert connection._tls_server_hostname == "cloud.example"


def test_pinned_https_proxy_tunnel_retries_every_vetted_address(monkeypatch):
    """A CONNECT that fails on the first address must fall through to the rest.

    Otherwise a dual-stack endpoint whose IPv6 address is unreachable from the proxy
    makes every hosted request fail, unlike the direct path which already retries.
    """

    monkeypatch.setattr(
        hosted_client.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:2800:220:1:248:1893:25c8:1946", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
        ],
    )
    tunnelled = []

    class _Sock:
        def close(self):
            pass

    class _Context:
        def wrap_socket(self, sock, *, server_hostname):
            return sock

    connection = hosted_client.PinnedHTTPSConnection("proxy.example", 3128)
    connection._context = _Context()
    connection._create_connection = lambda address, timeout, source: _Sock()

    def fake_tunnel():
        tunnelled.append((connection._tunnel_host, connection._tunnel_headers.get("Host")))
        if ":" in connection._tunnel_host:
            raise OSError("Tunnel connection failed: 502 Bad Gateway")

    connection._tunnel = fake_tunnel
    connection.set_tunnel("cloud.example", 443)
    # Python 3.12+ caches this authority at set_tunnel time; 3.11 does not. Set it
    # explicitly so the rebuild is asserted identically on every interpreter.
    connection._tunnel_headers["Host"] = "2606:2800:220:1:248:1893:25c8:1946:443"

    connection.connect()

    # The Host authority must follow the address actually being CONNECTed -- a strict
    # proxy rejects a retry whose Host still names the address that just failed.
    assert tunnelled == [
        ("[2606:2800:220:1:248:1893:25c8:1946]", "[2606:2800:220:1:248:1893:25c8:1946]:443"),
        ("93.184.216.34", "93.184.216.34:443"),
    ]
    assert connection._tunnel_host == "93.184.216.34"
    assert connection._tls_server_hostname == "cloud.example"


def test_pinned_https_proxy_tunnel_reports_the_last_failure_when_every_address_fails(
    monkeypatch,
):
    monkeypatch.setattr(
        hosted_client.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )

    class _Sock:
        def close(self):
            pass

    connection = hosted_client.PinnedHTTPSConnection("proxy.example", 3128)
    connection._create_connection = lambda address, timeout, source: _Sock()

    def fake_tunnel():
        raise OSError("Tunnel connection failed: 403 Forbidden")

    connection._tunnel = fake_tunnel
    connection.set_tunnel("cloud.example", 443)

    with pytest.raises(OSError, match="403 Forbidden"):
        connection.connect()


def test_pinned_https_proxy_tunnel_brackets_ipv6_on_the_connect_request_line(monkeypatch):
    """The CONNECT request target must be an unambiguous authority on every version.

    Python 3.9 and 3.10 serialize ``_tunnel_host`` verbatim, so a bare IPv6 literal
    would emit ``CONNECT 2606:...:443`` -- an ambiguous authority strict proxies reject.
    3.11+ bracket it themselves and leave an already-bracketed value alone.
    """

    v6 = "2606:2800:220:1:248:1893:25c8:1946"
    monkeypatch.setattr(
        hosted_client.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (v6, 443))],
    )
    sent = []

    class _Sock(io.BytesIO):
        def sendall(self, data):
            sent.append(data)

        def makefile(self, *args, **kwargs):
            return io.BytesIO(b"HTTP/1.1 200 Connection established\r\n\r\n")

        def close(self):
            pass

    class _Context:
        def wrap_socket(self, sock, *, server_hostname):
            return sock

    connection = hosted_client.PinnedHTTPSConnection("proxy.example", 3128)
    connection._context = _Context()
    connection._create_connection = lambda address, timeout, source: _Sock()
    connection.set_tunnel("cloud.example", 443)

    connection.connect()

    assert sent, "no CONNECT request was sent"
    request_line = sent[0].split(b"\r\n")[0]
    assert request_line.startswith(b"CONNECT [%s]:443 " % v6.encode()), request_line


def _record_connect_audits(monkeypatch):
    """Capture only ``http.client.connect`` audit events raised by the pinned client."""

    audited = []

    def _audit(name, *args):
        if name == "http.client.connect":
            audited.append(args)

    monkeypatch.setattr(hosted_client.sys, "audit", _audit)
    return audited


def test_pinned_connection_still_emits_the_standard_connect_audit_event(monkeypatch):
    """Overriding connect() must not hide hosted requests from audit hooks.

    ``HTTPConnection.connect`` raises ``http.client.connect`` before opening its socket,
    which is how a host process records or blocks outbound connections. This class
    bypasses that method, so it has to raise the event itself or every hosted request
    becomes invisible to those hooks.
    """

    monkeypatch.setattr(
        hosted_client.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )
    audited = _record_connect_audits(monkeypatch)

    class _Context:
        def wrap_socket(self, sock, *, server_hostname):
            return sock

    connection = hosted_client.PinnedHTTPSConnection("cloud.example", 443)
    connection._context = _Context()
    connection._create_connection = lambda address, timeout, source: object()

    connection.connect()

    # The vetted address actually dialled is reported, not the hostname, so a hook
    # deciding by address sees the destination the socket really opens.
    assert audited == [(connection, "93.184.216.34", 443)]


def test_pinned_proxy_tunnel_audits_each_proxy_dial(monkeypatch):
    monkeypatch.setattr(
        hosted_client.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:2800:220:1:248:1893:25c8:1946", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
        ],
    )

    class _Sock:
        def close(self):
            pass

    class _Context:
        def wrap_socket(self, sock, *, server_hostname):
            return sock

    connection = hosted_client.PinnedHTTPSConnection("proxy.example", 3128)
    connection._context = _Context()
    connection._create_connection = lambda address, timeout, source: _Sock()

    def fake_tunnel():
        if ":" in connection._tunnel_host:
            raise OSError("Tunnel connection failed: 502 Bad Gateway")

    connection._tunnel = fake_tunnel
    connection.set_tunnel("cloud.example", 443)
    audited = _record_connect_audits(monkeypatch)

    connection.connect()

    # One event per redial of the proxy, naming the proxy -- which is the socket that
    # actually gets opened on a tunnelled request.
    assert audited == [
        (connection, "proxy.example", 3128),
        (connection, "proxy.example", 3128),
    ]


def test_pinned_https_handler_forwards_only_supported_connection_arguments(monkeypatch):
    """Regression: 3.12 dropped ``HTTPSHandler._check_hostname`` and the matching kwarg.

    Reading it unconditionally raised ``AttributeError`` on 3.12+, breaking every hosted
    HTTPS request, and forwarding it would have raised ``TypeError`` inside
    ``http.client.HTTPSConnection``.  Nothing exercised ``https_open`` before this test.
    """

    handler = hosted_client.PinnedHTTPSHandler()
    captured = {}

    def _capture(http_class, req, **kwargs):
        captured["http_class"] = http_class
        captured["kwargs"] = kwargs
        return "response"

    monkeypatch.setattr(handler, "do_open", _capture)

    result = handler.https_open(urllib.request.Request("https://cloud.example/resource"))

    assert result == "response"
    assert captured["http_class"] is hosted_client.PinnedHTTPSConnection
    assert captured["kwargs"]["context"] is handler._context
    assert set(captured["kwargs"]) <= {"context", "check_hostname"}
    # The forwarded arguments must actually be accepted by the connection class on this
    # interpreter; constructing it does not open a socket.
    hosted_client.PinnedHTTPSConnection("cloud.example", **captured["kwargs"])


def test_licensing_facade_exposes_no_local_entitlement_engine():
    assert licensing.TRIAL_DAYS == 3
    assert licensing.production_warnings() == []
    for removed in (
        "activate",
        "compose_key",
        "current_license",
        "has_feature",
        "parse_key",
        "require_feature",
        "start_trial",
    ):
        assert not hasattr(licensing, removed)
