"""Regression test for the optional bearer-token auth middleware on the REST API.

Skips when FastAPI/httpx aren't installed (the offline numpy-only CI gate), so it
never affects that gate. The token is read per-request from settings, so we can
flip it on with monkeypatch. The 401 path short-circuits in the middleware, so this
test needs no embedding model.
"""
import pytest

pytest.importorskip("fastapi", reason="full-stack extra not installed")
httpx = pytest.importorskip("httpx", reason="httpx not installed")

from engraphis.config import settings  # noqa: E402


def test_bearer_auth_blocks_unauthenticated_and_allows_health(monkeypatch, tmp_path):
    import anyio

    monkeypatch.setattr(settings, "api_token", "tok-123")
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "auth.db"))
    monkeypatch.setattr(settings, "loop_interval", 0)

    from engraphis.app import create_app
    app = create_app()

    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            health = await c.get("/memory/health")
            blocked = await c.post("/memory/query", json={"namespace": "x", "query": "y"})
        return health.status_code, blocked.status_code

    health_status, blocked_status = anyio.run(go)
    assert health_status == 200       # health is public
    assert blocked_status == 401      # protected route, no token -> blocked in middleware


def test_no_token_means_open_api(monkeypatch, tmp_path):
    import anyio

    monkeypatch.setattr(settings, "api_token", "")          # auth disabled
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "open.db"))
    monkeypatch.setattr(settings, "loop_interval", 0)

    from engraphis.app import create_app
    app = create_app()

    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return (await c.get("/memory/health")).status_code

    assert anyio.run(go) == 200
