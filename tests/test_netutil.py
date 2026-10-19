"""netutil — URL/host helpers shared by every entrypoint (IPv6 + wildcard binds)."""
from types import SimpleNamespace

import pytest

from engraphis.netutil import (
    bracket_host, client_ip, connect_host, declared_host_names, display_base_url,
    is_local_request, is_rebinding_safe_host,
)


def test_bracket_host_ipv6_and_idempotent():
    assert bracket_host("::1") == "[::1]"
    assert bracket_host("[::1]") == "[::1]"          # idempotent
    assert bracket_host("2001:db8::7") == "[2001:db8::7]"


def test_bracket_host_passthrough():
    assert bracket_host("127.0.0.1") == "127.0.0.1"
    assert bracket_host("example.com") == "example.com"
    assert bracket_host("") == ""


def test_connect_host_maps_wildcards_to_loopback():
    for wildcard in ("", "0.0.0.0"):
        assert connect_host(wildcard) == "127.0.0.1"
    for wildcard in ("::", "[::]", "  ::  "):
        assert connect_host(wildcard) == "::1"
    assert connect_host("10.1.2.3") == "10.1.2.3"
    assert connect_host("::1") == "::1"              # loopback is connectable, not wildcard


def test_display_base_url_never_malformed_for_ipv6_bind():
    # The 2026-07-16 Railway fix binds ENGRAPHIS_HOST='::'; the naive f-string produced
    # the malformed http://:::8700. Wildcards map to loopback, IPv6 gets brackets.
    assert display_base_url("::", 8700) == "http://[::1]:8700"
    assert display_base_url("0.0.0.0", 8700) == "http://127.0.0.1:8700"
    assert display_base_url("::1", 8700) == "http://[::1]:8700"
    assert display_base_url("127.0.0.1", 9000) == "http://127.0.0.1:9000"
    assert display_base_url("example.com", 443, scheme="https") == "https://example.com:443"


def test_settings_base_url_uses_display_rules(monkeypatch):
    from engraphis.config import Settings
    monkeypatch.setenv("ENGRAPHIS_HOST", "::")
    monkeypatch.setenv("ENGRAPHIS_PORT", "8701")
    assert Settings().base_url == "http://[::1]:8701"


def _request(peer, forwarded=""):
    return SimpleNamespace(
        client=SimpleNamespace(host=peer),
        headers={"x-forwarded-for": forwarded} if forwarded else {},
    )


def test_client_ip_ignores_forwarding_headers_from_untrusted_peer(monkeypatch):
    monkeypatch.delenv("ENGRAPHIS_FORWARDED_ALLOW_IPS", raising=False)
    assert client_ip(_request("198.51.100.10", "203.0.113.99")) == "198.51.100.10"


def test_client_ip_uses_proxy_appended_rightmost_address(monkeypatch):
    monkeypatch.setenv("ENGRAPHIS_FORWARDED_ALLOW_IPS", "*")
    assert client_ip(_request(
        "127.0.0.1", "attacker-spoofed, 203.0.113.9"
    )) == "203.0.113.9"


@pytest.mark.parametrize("header", [
    "x-forwarded-for",
    "forwarded",
    "x-real-ip",
    "x-forwarded-host",
    "x-forwarded-proto",
    "x-forwarded-port",
    "x-forwarded-prefix",
])
def test_local_request_fails_closed_for_unknown_names_and_forwarding_headers(header):
    request = SimpleNamespace(
        scope={"client": ("not-an-ip", 1)}, headers={})
    assert not is_local_request(request)
    request.scope["client"] = ("127.0.0.1", 1)
    request.headers[header] = "https" if header == "x-forwarded-proto" else "forwarded"
    assert not is_local_request(request)


def test_local_request_accepts_loopback_and_testclient_fixture():
    for host in ("127.0.0.1", "::1", "::ffff:127.0.0.1", "testclient"):
        assert is_local_request(SimpleNamespace(
            scope={"client": (host, 1)}, headers={}))


def test_local_request_accepts_only_explicit_container_nat_peers(monkeypatch):
    request = SimpleNamespace(scope={"client": ("172.18.0.1", 1)}, headers={})
    monkeypatch.delenv("ENGRAPHIS_LOCAL_TRUSTED_PEERS", raising=False)
    assert not is_local_request(request)

    monkeypatch.setenv("ENGRAPHIS_LOCAL_TRUSTED_PEERS", "172.16.0.0/12")
    assert is_local_request(request)
    request.headers["x-forwarded-for"] = "127.0.0.1"
    assert not is_local_request(request)


def test_declared_host_names_accept_urls_names_and_lists():
    assert declared_host_names(
        "http://Engraphis.LAN:8700/", "desk-top, graph.internal:8720", "", " , ",
    ) == frozenset({"engraphis.lan", "desk-top", "graph.internal"})
    # Browsers send internationalised names as punycode.
    assert declared_host_names("bücher.example") == frozenset({"xn--bcher-kva.example"})
    # Userinfo, a bad port or no host at all declares nothing.
    assert declared_host_names(
        "http://user@evil.example", "host:notaport", "http://", "a..b",
    ) == frozenset()


def _host_request(host, server=("127.0.0.1", 8700)):
    return SimpleNamespace(headers={"host": host} if host is not None else {},
                           scope={"server": server})


@pytest.mark.parametrize("host", [
    "localhost", "localhost:8700", "LOCALHOST:8700", "127.0.0.1:8700", "[::1]:8700",
    "10.0.0.7", "engraphis.lan:8700",
])
def test_rebinding_safe_hosts(host):
    assert is_rebinding_safe_host(
        _host_request(host), declared_host_names("engraphis.lan"),
    )


@pytest.mark.parametrize("host", [
    None, "", "attacker.test", "attacker.test:8700", "127.0.0.1.attacker.test",
    "localhost.", "localhost..", "evil.localhost", "localhost.attacker.test",
    "attacker.test@127.0.0.1", "localhost:attacker.test", "local host", "[::1",
    "localhost/x", "localhost?x", "localhost#x", "localhost\\x",
])
def test_rebinding_prone_hosts(host):
    assert not is_rebinding_safe_host(_host_request(host), declared_host_names("engraphis.lan"))


def test_the_servers_own_reported_name_is_accepted():
    # In-process transports report the URL host they were given; a real server reports
    # its socket address, which the IP rule already accepts.
    assert is_rebinding_safe_host(_host_request("testserver", server=("testserver", 80)))
    assert not is_rebinding_safe_host(_host_request("testserver", server=None))
