"""Launcher configuration regressions."""

import argparse
import errno
import io
import json
import logging
import sys
import types

import pytest

from scripts import start_dashboard


def test_embed_model_uses_default_only_when_unset(monkeypatch):
    monkeypatch.delenv("ENGRAPHIS_EMBED_MODEL", raising=False)
    assert start_dashboard._embed_model_from_environment() == "sentence-transformers/all-MiniLM-L6-v2"


def test_embed_model_preserves_explicit_offline_opt_out(monkeypatch):
    monkeypatch.setenv("ENGRAPHIS_EMBED_MODEL", "")
    assert start_dashboard._embed_model_from_environment() == ""


@pytest.mark.parametrize("value", ["0", "-1", "65536", "not-a-number"])
def test_port_rejects_invalid_values(value):
    with pytest.raises(argparse.ArgumentTypeError):
        start_dashboard._port(value)


def test_port_accepts_boundaries():
    assert start_dashboard._port("1") == 1
    assert start_dashboard._port("65535") == 65535


@pytest.mark.parametrize("busy_errno", [errno.EADDRINUSE, errno.EACCES, 10013, 10048])
def test_port_probe_matches_uvicorn_reuseaddr_without_accepting_busy_port(
    monkeypatch, busy_errno,
):
    calls = []

    class Probe:
        def setsockopt(self, level, option, value):
            calls.append(("setsockopt", level, option, value))

        def bind(self, sockaddr):
            calls.append(("bind", sockaddr))
            raise OSError(busy_errno, "address already in use")

        def close(self):
            calls.append(("close",))

    monkeypatch.setattr(
        start_dashboard.socket, "getaddrinfo",
        lambda *_args, **_kwargs: [(
            start_dashboard.socket.AF_INET, start_dashboard.socket.SOCK_STREAM,
            0, "", ("127.0.0.1", 8700),
        )],
    )
    monkeypatch.setattr(start_dashboard.socket, "socket", lambda *_args: Probe())

    assert start_dashboard._port_is_available("127.0.0.1", 8700) is False
    assert calls == [
        ("setsockopt", start_dashboard.socket.SOL_SOCKET,
         start_dashboard.socket.SO_REUSEADDR, 1),
        ("bind", ("127.0.0.1", 8700)),
        ("close",),
    ]


def test_launcher_preserves_socket_peer_for_forwarded_header_validation(monkeypatch):
    uvicorn = pytest.importorskip("uvicorn")

    captured = {}
    monkeypatch.setattr(start_dashboard, "_port_is_available", lambda *_args: True)
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: captured.update(kwargs))
    fake = types.ModuleType("engraphis.dashboard_app")
    fake.app = object()
    monkeypatch.setitem(sys.modules, "engraphis.dashboard_app", fake)
    start_dashboard.main(["--no-open"])
    assert captured["proxy_headers"] is False
    assert "forwarded_allow_ips" not in captured


def test_reload_uses_an_asgi_import_string(monkeypatch):
    uvicorn = pytest.importorskip("uvicorn")

    captured = {}
    monkeypatch.setattr(start_dashboard, "_port_is_available", lambda *_args: True)
    monkeypatch.setattr(
        uvicorn, "run", lambda app, **kwargs: captured.update(app=app, **kwargs),
    )

    start_dashboard.main(["--no-open", "--reload"])

    assert captured["app"] == "engraphis.dashboard_app:app"
    assert captured["reload"] is True
    assert captured["proxy_headers"] is False


def test_json_launcher_preserves_redacted_uvicorn_access_formatter(monkeypatch):
    uvicorn = pytest.importorskip("uvicorn")
    stream = io.StringIO()
    root = logging.getLogger()
    handler = logging.StreamHandler(stream)
    monkeypatch.setattr(root, "handlers", [handler])
    monkeypatch.setattr(root, "level", logging.INFO)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        monkeypatch.setattr(logger, "handlers", [])
        monkeypatch.setattr(logger, "propagate", True)

    monkeypatch.setenv("ENGRAPHIS_JSON_LOGS", "1")
    captured = {}
    monkeypatch.setattr(start_dashboard, "_port_is_available", lambda *_args: True)
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: captured.update(kwargs))
    fake = types.ModuleType("engraphis.dashboard_app")
    fake.app = object()
    monkeypatch.setitem(sys.modules, "engraphis.dashboard_app", fake)

    start_dashboard.main(["--no-open"])

    assert captured["log_config"] is None
    # Exercise the same Config initialization uvicorn.run performs. A future launcher
    # change that restores Uvicorn's default dictConfig will replace our formatter here.
    uvicorn.Config(fake.app, log_config=captured["log_config"], log_level="info")
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d',
        "127.0.0.1:1234", "GET",
        "/?invite_token=invite-secret&key=provider-secret", "1.1", 200,
    )

    event = json.loads(stream.getvalue().splitlines()[-1])
    assert event["logger"] == "uvicorn.access"
    assert event["event"].count("[redacted]") == 2
    assert "invite-secret" not in stream.getvalue()
    assert "provider-secret" not in stream.getvalue()


def test_launcher_reuses_an_existing_dashboard_before_loading_the_model(monkeypatch, capsys):
    opened = []
    monkeypatch.setattr(start_dashboard, "_port_is_available", lambda *_args: False)
    monkeypatch.setattr(start_dashboard, "_is_engraphis_dashboard", lambda _url: True)
    monkeypatch.setattr(start_dashboard.webbrowser, "open", opened.append)

    start_dashboard.main(["--port", "8719"])

    assert opened == ["http://127.0.0.1:8719"]
    assert "already running at http://127.0.0.1:8719" in capsys.readouterr().out


def test_launcher_reports_a_non_engraphis_port_conflict(monkeypatch, capsys):
    monkeypatch.setattr(start_dashboard, "_port_is_available", lambda *_args: False)
    monkeypatch.setattr(start_dashboard, "_is_engraphis_dashboard", lambda _url: False)

    with pytest.raises(SystemExit) as exc:
        start_dashboard.main(["--no-open", "--port", "8719"])

    assert exc.value.code == 1
    error = capsys.readouterr().err
    assert "http://127.0.0.1:8719 is already in use" in error
    assert "--port" in error
