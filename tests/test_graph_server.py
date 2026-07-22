"""Launcher-level auth gate for the optional read-only graph server.

The security promise (SECURITY.md, .env.example, README) is that non-loopback
serving refuses to start without a bearer token. `_loopback` is the predicate
that gate rests on, so its edge cases are load-bearing: an empty host string
makes the socket layer bind ALL interfaces (`socket.bind(("", port))` ==
0.0.0.0), and unparseable hostnames must fail closed.
"""
import sys
import types

import pytest

from scripts.graph_server import _loopback, _port, main


@pytest.mark.parametrize("value", ["0", "65536", "70000", "abc", ""])
def test_invalid_port_is_rejected_before_server_or_model_imports(value, monkeypatch, capsys):
    monkeypatch.delitem(sys.modules, "engraphis.read_only_api", raising=False)

    with pytest.raises(SystemExit) as exc:
        main(["--port", value])

    assert exc.value.code == 2
    assert "engraphis.read_only_api" not in sys.modules
    stderr = capsys.readouterr().err
    assert "port must be" in stderr
    assert "Traceback" not in stderr
    assert "Loading weights" not in stderr


def test_invalid_port_from_environment_is_a_clean_argparse_error(monkeypatch, capsys):
    monkeypatch.setenv("ENGRAPHIS_GRAPH_PORT", "70000")
    monkeypatch.delitem(sys.modules, "engraphis.read_only_api", raising=False)

    with pytest.raises(SystemExit) as exc:
        main([])

    assert exc.value.code == 2
    assert "engraphis.read_only_api" not in sys.modules
    assert "port must be from 1 to 65535" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["1", "8720", "65535"])
def test_valid_port_range(value):
    assert _port(value) == int(value)

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

def test_main_blank_graph_token_falls_back_to_api_token(monkeypatch):
    captured = []
    uvicorn = types.ModuleType("uvicorn")
    uvicorn.run = lambda *_args, **_kwargs: None
    read_only_api = types.ModuleType("engraphis.read_only_api")

    def create_read_only_app(*, token):
        captured.append(token)
        return object()

    read_only_api.create_read_only_app = create_read_only_app
    monkeypatch.setitem(sys.modules, "uvicorn", uvicorn)
    monkeypatch.setitem(sys.modules, "engraphis.read_only_api", read_only_api)
    monkeypatch.setenv("ENGRAPHIS_GRAPH_TOKEN", "")
    monkeypatch.setenv("ENGRAPHIS_API_TOKEN", "api-token")

    assert main(["--host", "0.0.0.0", "--port", "8720"]) == 0
    assert captured == ["api-token"]
