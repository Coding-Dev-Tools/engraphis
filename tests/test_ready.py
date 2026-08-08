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
    monkeypatch.setattr(settings, "embed_model", "")

    from engraphis import app as app_module
    from engraphis.stores import init_db

    monkeypatch.setattr(app_module, "_warmup_embedder", lambda: True)
    app = app_module.create_legacy_reference_app(
        legacy_db_path=tmp_path / "ready-v1.db"
    )
    init_db()
    return app


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


def test_api_ready_is_503_when_required_schema_is_missing(app):
    from engraphis.stores import get_conn

    conn = get_conn()
    conn.execute("ALTER TABLE memories RENAME TO memories_missing")
    conn.commit()

    response = _get(app, "/api/ready")

    assert response.status_code == 503
    assert response.json()["checks"]["db"] is False


def test_legacy_readiness_rechecks_exact_backend_after_failure(monkeypatch):
    from engraphis import app as app_module

    calls = []

    def warmup():
        calls.append("legacy")
        return len(calls) > 1

    monkeypatch.setattr(app_module, "_warmup_embedder", warmup)
    monkeypatch.setattr(app_module, "_embedder_ok", True)

    assert app_module._embedder_ready() is False
    assert app_module._embedder_ready() is True
    assert calls == ["legacy", "legacy"]


def test_background_maintenance_runs_off_the_event_loop(monkeypatch):
    import asyncio
    import contextlib
    import threading
    import time

    from engraphis import app as app_module

    started = threading.Event()
    release = threading.Event()

    def blocking_decay(*, namespace=None):
        del namespace
        started.set()
        release.wait(timeout=2)
        return 0

    def release_later():
        started.wait(timeout=1)
        time.sleep(0.5)
        release.set()

    monkeypatch.setattr(settings, "loop_interval", 0)
    monkeypatch.setattr(app_module.reweight, "decay_pass", blocking_decay)
    monkeypatch.setattr(
        app_module.thoughts_engine,
        "synthesize_thoughts",
        lambda **_kwargs: {"persisted": False},
    )
    helper = threading.Thread(target=release_later, daemon=True)
    helper.start()

    async def exercise():
        task = asyncio.create_task(
            app_module._consciousness_loop(enable_consolidation=False)
        )
        try:
            assert await asyncio.to_thread(started.wait, 1)
            # A direct call would pin the loop until ``release_later`` fires.
            assert not release.is_set()
        finally:
            release.set()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    asyncio.run(exercise())
    helper.join(timeout=1)


def test_probes_are_public_even_with_token(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "api_token", "tok-123")
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "tok.db"))
    monkeypatch.setattr(settings, "loop_interval", 0)
    monkeypatch.setattr(settings, "embed_model", "")

    from engraphis import app as app_module
    monkeypatch.setattr(app_module, "_warmup_embedder", lambda: True)
    app = app_module.create_legacy_reference_app(legacy_db_path=tmp_path / "tok-v1.db")
    assert _get(app, "/api/health").status_code == 200          # no 401
    assert _get(app, "/api/ready").status_code in (200, 503)    # no 401


def test_request_id_is_assigned_and_propagated(app):
    r = _get(app, "/memory/health")
    assert r.headers.get("x-request-id")                         # assigned when absent
    r = _get(app, "/memory/health", headers={"X-Request-ID": "req-42"})
    assert r.headers["x-request-id"] == "req-42"                 # propagated when supplied
