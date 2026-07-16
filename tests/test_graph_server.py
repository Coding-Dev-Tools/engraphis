"""Launcher-level auth gate for the optional read-only graph server.

The security promise (SECURITY.md, .env.example, README) is that non-loopback
serving refuses to start without a bearer token. `_loopback` is the predicate
that gate rests on, so its edge cases are load-bearing: an empty host string
makes the socket layer bind ALL interfaces (`socket.bind(("", port))` ==
0.0.0.0), and unparseable hostnames must fail closed.
"""
import pytest

from scripts.graph_server import _loopback, main


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost", "127.1.2.3"])
def test_loopback_hosts_are_recognized(host):
    assert _loopback(host) is True


@pytest.mark.parametrize("host", [
    "",            # empty string binds ALL interfaces — emphatically not loopback
    "0.0.0.0",
    "::",
    "192.168.1.10",
    "example.com",  # unresolvable here; fail closed
    "myhost.local",
])
def test_non_loopback_and_unparseable_hosts_fail_closed(host):
    assert _loopback(host) is False


@pytest.mark.parametrize("host", ["", "0.0.0.0"])
def test_main_refuses_tokenless_non_loopback_bind(host, monkeypatch):
    monkeypatch.delenv("ENGRAPHIS_GRAPH_TOKEN", raising=False)
    monkeypatch.delenv("ENGRAPHIS_API_TOKEN", raising=False)
    with pytest.raises(SystemExit) as exc:
        main(["--host", host, "--port", "8720"])
    assert exc.value.code == 2  # argparse .error()
