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

    from engraphis.app import create_legacy_reference_app
    app = create_legacy_reference_app(legacy_db_path=tmp_path / "auth-v1.db")

    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            health = await c.get("/memory/health")
            memory_health = await c.get("/memory/health/stale")
            blocked = await c.post("/memory/query", json={"namespace": "x", "query": "y"})
        return health.status_code, memory_health.status_code, blocked.status_code

    health_status, memory_health_status, blocked_status = anyio.run(go)
    assert health_status == 200       # health is public
    assert memory_health_status == 401  # owner-data diagnostics are not probes
    assert blocked_status == 401      # protected route, no token -> blocked in middleware


def test_no_token_means_open_api(monkeypatch, tmp_path):
    """Zero-config stays zero-config *for a loopback caller* -- see the remote test below."""

    import anyio

    monkeypatch.setattr(settings, "api_token", "")          # auth disabled
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "open.db"))
    monkeypatch.setattr(settings, "loop_interval", 0)

    from engraphis.app import create_legacy_reference_app
    app = create_legacy_reference_app(legacy_db_path=tmp_path / "open-v1.db")

    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            health = await c.get("/memory/health")
            export = await c.get("/memory/export?workspace=default")
        return health.status_code, export.status_code

    health_status, export_status = anyio.run(go)
    assert health_status == 200
    assert export_status != 403   # ASGITransport presents a 127.0.0.1 peer


def test_a_remote_peer_is_refused_until_a_token_is_configured(monkeypatch, tmp_path):
    """Without this backstop a bind-all deployment published the whole memory API.

    ``docker-entrypoint.sh`` defaults ``ENGRAPHIS_HOST`` to ``::``, so ``docker run -p
    8700:8700 ... engraphis-server`` exposed ``/memory/export`` and ``/memory/admin/*``
    to every reachable peer with no credential at all. ``dashboard_app`` has always had
    this loopback fallback and ``scripts/graph_server.py`` refuses the equivalent start;
    the v1 surface was the one entrypoint with neither.
    """

    import anyio

    monkeypatch.setattr(settings, "api_token", "")          # zero-config
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "remote.db"))
    monkeypatch.setattr(settings, "loop_interval", 0)

    from engraphis.app import create_legacy_reference_app
    app = create_legacy_reference_app(legacy_db_path=tmp_path / "remote-v1.db")

    async def go():
        transport = httpx.ASGITransport(app=app, client=("203.0.113.77", 51234))
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            health = await c.get("/memory/health")
            export = await c.get("/memory/export?workspace=default")
            stats = await c.get("/memory/stats")
            delete = await c.post("/memory/admin/delete", json={})
        return (health.status_code, export.status_code,
                stats.status_code, delete.status_code)

    health_status, export_status, stats_status, delete_status = anyio.run(go)
    assert health_status == 200                    # liveness probes stay public
    assert export_status == 403
    assert stats_status == 403
    assert delete_status == 403


def test_a_configured_token_still_authorizes_a_remote_peer(monkeypatch, tmp_path):
    """The loopback backstop must not become a second, undocumented network policy."""

    import anyio

    monkeypatch.setattr(settings, "api_token", "tok-123")
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "remote-token.db"))
    monkeypatch.setattr(settings, "loop_interval", 0)

    from engraphis.app import create_legacy_reference_app
    app = create_legacy_reference_app(legacy_db_path=tmp_path / "remote-token-v1.db")

    async def go():
        transport = httpx.ASGITransport(app=app, client=("203.0.113.77", 51234))
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            authorized = await c.get(
                "/memory/stats", headers={"Authorization": "Bearer tok-123"}
            )
            anonymous = await c.get("/memory/stats")
        return authorized.status_code, anonymous.status_code

    authorized_status, anonymous_status = anyio.run(go)
    # The assertion is about the gate, not the handler: a correct bearer must reach the
    # route from a remote peer. (The route itself may then fail on this bare temp DB.)
    assert authorized_status not in (401, 403)
    assert anonymous_status == 401     # a token is configured, so 401 rather than 403
