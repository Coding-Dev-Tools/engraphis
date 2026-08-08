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


def test_dashboard_health_probe_accepts_a_local_health_payload_without_redirects(monkeypatch):
    requests = []
    handlers = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, limit):
            assert limit == 16 * 1024
            return b'{"status":"healthy"}'

    class Opener:
        def open(self, request, *, timeout):
            requests.append((request, timeout))
            return Response()

    def build_opener(*received):
        handlers.extend(received)
        return Opener()

    monkeypatch.setattr(start_dashboard.urllib.request, "build_opener", build_opener)

    assert start_dashboard._is_engraphis_dashboard("http://127.0.0.1:8700/") is True
    assert len(requests) == 1
    assert requests[0][0].full_url == "http://127.0.0.1:8700/api/health"
    assert requests[0][1] == 0.75
    assert len(handlers) == 1
    assert handlers[0].redirect_request(
        None, None, 302, "Found", {}, "http://example.test"
    ) is None


def test_dashboard_health_probe_refuses_redirect_without_a_second_request(monkeypatch):
    calls = []

    class Opener:
        def open(self, request, *, timeout):
            calls.append((request.full_url, timeout))
            raise start_dashboard.urllib.error.HTTPError(
                request.full_url, 302, "Found", {}, None,
            )

    monkeypatch.setattr(
        start_dashboard.urllib.request,
        "build_opener",
        lambda *_handlers: Opener(),
    )

    assert start_dashboard._is_engraphis_dashboard("http://127.0.0.1:8700") is False
    assert calls == [("http://127.0.0.1:8700/api/health", 0.75)]


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
    assert captured["access_log"] is False


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
    assert captured["access_log"] is False


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
    assert captured["access_log"] is False
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


def test_dashboard_streaming_body_limit_rejects_before_parsing_with_cors(
    monkeypatch, tmp_path,
):
    pytest.importorskip("fastapi")
    httpx = pytest.importorskip("httpx")
    import anyio
    from fastapi import FastAPI, Request
    from fastapi.middleware.cors import CORSMiddleware

    from engraphis.config import settings

    monkeypatch.setattr(settings, "db_path", str(tmp_path / "dashboard.db"))
    monkeypatch.setattr(settings, "embed_model", "")
    monkeypatch.setattr(settings, "vector_backend", "numpy")
    monkeypatch.setattr(settings, "loop_interval", 0)
    monkeypatch.setattr(settings, "loop_consolidate", 0)
    from engraphis.dashboard_app import _RequestBodyLimitMiddleware

    app = FastAPI()
    app.add_middleware(_RequestBodyLimitMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["https://dashboard.example"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    parsed = []
    @app.post("/api/auth/session")
    async def parse_body(request: Request):
        parsed.append(await request.body())
        return {"parsed": True}

    async def chunks():
        yield b"x" * 4096
        yield b"x" * 4097

    async def request():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            declared = await client.post(
                "/api/auth/session",
                content=b"{}",
                headers={
                    "Content-Length": str(8 * 1024 + 1),
                    "Content-Type": "application/json",
                    "Origin": "https://dashboard.example",
                },
            )
            streamed = await client.post(
                "/api/auth/session",
                content=chunks(),
                headers={
                    "Content-Type": "application/json",
                    "Origin": "https://dashboard.example",
                },
            )
            return declared, streamed

    declared, streamed = anyio.run(request)
    for response in (declared, streamed):
        assert response.status_code == 413
        assert response.json() == {
            "error": "request body too large",
            "max_bytes": 8 * 1024,
        }
        assert (
            response.headers["access-control-allow-origin"]
            == "https://dashboard.example"
        )
    assert parsed == []


def test_dashboard_lifespan_releases_its_service(monkeypatch):
    pytest.importorskip("fastapi")
    import anyio

    from engraphis import dashboard_app, update_check

    class _Store:
        prompt_eligibility_counts = None
        embedding_space_health = None
        active_embedding_space = None
        begin_embedding_rebuild = None
        finish_embedding_rebuild = None

    class _Service:
        allowed_workspaces = None
        store = _Store()
        engine = types.SimpleNamespace(
            embedder=types.SimpleNamespace(dim=16),
        )

        @staticmethod
        def stats():
            return {}

    service = _Service()
    released = []
    original_find_spec = dashboard_app.importlib.util.find_spec
    monkeypatch.setattr(
        dashboard_app.importlib.util,
        "find_spec",
        lambda name: None if name == "mcp" else original_find_spec(name),
    )
    monkeypatch.setattr(dashboard_app.MemoryService, "create", lambda *args, **kwargs: service)
    monkeypatch.setattr(dashboard_app.v2_api, "set_service", lambda bound: None)
    monkeypatch.setattr(dashboard_app.v2_api, "release_service", released.append)
    monkeypatch.setattr(update_check, "emit_startup_notice", lambda callback: None)
    monkeypatch.setattr(dashboard_app.settings, "loop_interval", 0)
    monkeypatch.setattr(dashboard_app.settings, "loop_consolidate", 0)
    app = dashboard_app.create_app()

    async def run_lifespan():
        async with app.router.lifespan_context(app):
            pass

    anyio.run(run_lifespan)
    assert released == [service]
