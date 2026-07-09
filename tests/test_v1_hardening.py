"""v1 REST hardening: input size caps + control-char stripping (parity with the v2
service.py write-path guards, SECURITY.md) and optional per-IP rate limiting.

Skips when FastAPI/httpx aren't installed (the numpy-only core gate), like test_app_auth.
"""
import pytest

pytest.importorskip("fastapi", reason="full-stack extra not installed")
httpx = pytest.importorskip("httpx", reason="httpx not installed")

from engraphis.config import settings  # noqa: E402
from engraphis.models import MemoryItem, MAX_CONTENT_CHARS, MAX_NAME_CHARS  # noqa: E402


def test_model_strips_control_chars_and_caps_length():
    mi = MemoryItem(key="k", content="a\x00b\x1fc\x7f", namespace="ns")
    assert mi.content == "abc"  # control chars removed, ordinary text kept
    with pytest.raises(Exception):
        MemoryItem(key="k", content="x" * (MAX_CONTENT_CHARS + 1), namespace="ns")
    with pytest.raises(Exception):
        MemoryItem(key="x" * (MAX_NAME_CHARS + 1), content="c", namespace="ns")


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


def test_rest_insert_rejects_oversized_content(monkeypatch, tmp_path):
    import anyio
    monkeypatch.setattr(settings, "api_token", "")
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "h.db"))
    monkeypatch.setattr(settings, "loop_interval", 0)
    from engraphis.app import create_app
    app = create_app()

    async def go():
        async with _client(app) as c:
            r = await c.post("/memory/insert", json={
                "key": "k", "namespace": "ns", "content": "x" * (MAX_CONTENT_CHARS + 1)})
        return r.status_code

    assert anyio.run(go) == 422  # rejected at validation, before any ingest/embedder work


def test_rate_limit_returns_429(monkeypatch, tmp_path):
    import anyio
    monkeypatch.setattr(settings, "api_token", "")
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "r.db"))
    monkeypatch.setattr(settings, "loop_interval", 0)
    monkeypatch.setattr(settings, "rate_limit", 2)
    monkeypatch.setattr(settings, "rate_window", 60)
    from engraphis.app import create_app
    app = create_app()

    async def go():
        async with _client(app) as c:
            return [(await c.get("/memory/config")).status_code for _ in range(3)]

    codes = anyio.run(go)
    assert codes[:2] == [200, 200]
    assert codes[2] == 429  # third request in the window is throttled


def test_health_is_exempt_from_rate_limit(monkeypatch, tmp_path):
    import anyio
    monkeypatch.setattr(settings, "api_token", "")
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "r2.db"))
    monkeypatch.setattr(settings, "loop_interval", 0)
    monkeypatch.setattr(settings, "rate_limit", 1)
    monkeypatch.setattr(settings, "rate_window", 60)
    from engraphis.app import create_app
    app = create_app()

    async def go():
        async with _client(app) as c:
            return [(await c.get("/memory/health")).status_code for _ in range(3)]

    assert anyio.run(go) == [200, 200, 200]  # health/liveness never throttled
