"""Probes + request-id middleware on the REST API (/api/health, /api/ready).

Skips when FastAPI/httpx aren't installed (the offline numpy-only CI gate). The
embedder check uses the offline deterministic fallback, so no model downloads.
"""
import pytest

pytest.importorskip("fastapi", reason="full-stack extra not installed")
httpx = pytest.importorskip("httpx", reason="httpx not installed")

from engraphis import __version__  # noqa: E402
from engraphis.config import settings  # noqa: E402


def _get(app, path, headers=None):
    import anyio

    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await c.get(path, headers=headers or {})

    return anyio.run(go)


@pytest.fixture()
def app(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "api_token", "")
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "ready.db"))
    monkeypatch.setattr(settings, "loop_interval", 0)
    monkeypatch.setattr(settings, "embed_model", "")   # deterministic offline embedder

    from engraphis.app import create_app
    return create_app()


def test_api_ready_reports_checks_and_version(app):
    r = _get(app, "/api/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["ready"] is True
    assert body["checks"] == {"db": True, "embedder": True}
    assert body["version"] == __version__
    assert _get(app, "/api/health").status_code == 200   # liveness alias stays trivial


def test_api_ready_is_503_when_db_check_fails(app, monkeypatch):
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr("engraphis.app.get_conn", boom)
    r = _get(app, "/api/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["ready"] is False
    assert body["checks"]["db"] is False


def test_probes_are_public_even_with_token(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "api_token", "tok-123")
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "tok.db"))
    monkeypatch.setattr(settings, "loop_interval", 0)
    monkeypatch.setattr(settings, "embed_model", "")

    from engraphis.app import create_app
    app = create_app()
    assert _get(app, "/api/health").status_code == 200          # no 401
    assert _get(app, "/api/ready").status_code in (200, 503)    # no 401


def test_request_id_is_assigned_and_propagated(app):
    r = _get(app, "/memory/health")
    assert r.headers.get("x-request-id")                         # assigned when absent
    r = _get(app, "/memory/health", headers={"X-Request-ID": "req-42"})
    assert r.headers["x-request-id"] == "req-42"                 # propagated when supplied
