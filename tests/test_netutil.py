"""netutil — URL/host helpers shared by every entrypoint (IPv6 + wildcard binds)."""
from engraphis.netutil import bracket_host, connect_host, display_base_url


def test_bracket_host_ipv6_and_idempotent():
    assert bracket_host("::1") == "[::1]"
    assert bracket_host("[::1]") == "[::1]"          # idempotent
    assert bracket_host("2001:db8::7") == "[2001:db8::7]"


def test_bracket_host_passthrough():
    assert bracket_host("127.0.0.1") == "127.0.0.1"
    assert bracket_host("example.com") == "example.com"
    assert bracket_host("") == ""


def test_connect_host_maps_wildcards_to_loopback():
    for wildcard in ("", "0.0.0.0", "::", "[::]", "  ::  "):
        assert connect_host(wildcard) == "127.0.0.1"
    assert connect_host("10.1.2.3") == "10.1.2.3"
    assert connect_host("::1") == "::1"              # loopback is connectable, not wildcard


def test_display_base_url_never_malformed_for_ipv6_bind():
    # The 2026-07-16 Railway fix binds ENGRAPHIS_HOST='::'; the naive f-string produced
    # the malformed http://:::8700. Wildcards map to loopback, IPv6 gets brackets.
    assert display_base_url("::", 8700) == "http://127.0.0.1:8700"
    assert display_base_url("0.0.0.0", 8700) == "http://127.0.0.1:8700"
    assert display_base_url("::1", 8700) == "http://[::1]:8700"
    assert display_base_url("127.0.0.1", 9000) == "http://127.0.0.1:9000"
    assert display_base_url("example.com", 443, scheme="https") == "https://example.com:443"


def test_settings_base_url_uses_display_rules(monkeypatch):
    from engraphis.config import Settings
    monkeypatch.setenv("ENGRAPHIS_HOST", "::")
    monkeypatch.setenv("ENGRAPHIS_PORT", "8701")
    assert Settings().base_url == "http://127.0.0.1:8701"
