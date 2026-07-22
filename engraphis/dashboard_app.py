"""The restored v1 dashboard — served on the *v2* engine.

Same look-and-feel as the original dashboard (engraphis/static/index.html), but every
route reads/writes the v2 MemoryService where the real data lives. This keeps the v1
server (engraphis/app.py) untouched; run this with `python -m scripts.start_dashboard`.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from urllib.parse import urlsplit

import os as _os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from engraphis import licensing
from engraphis.config import settings
from engraphis.routes import v2_api
from engraphis.service import MemoryService

_STATIC = Path(__file__).resolve().parent / "static"
_INDEX = _STATIC / "index.html"

# The public package is a single-user local runtime. Hosted account, Team, trial, and
# recovery endpoints live in Engraphis Cloud; only the shell and health/auth metadata are
# reachable before the optional local API token gate.
_PUBLIC = {"/", "/api/health", "/api/ready", "/api/auth/state"}


def _embedder_status(embedder, configured_model: str) -> str:
    """Concise startup status without misdiagnosing an explicit offline selection."""
    from engraphis.backends.embedder_deterministic import DeterministicEmbedder

    if not isinstance(embedder, DeterministicEmbedder):
        return "semantic search ready"
    if not configured_model:
        return "deterministic offline mode selected"
    return "configured model unavailable; deterministic fallback active"


def _mcp_transport_security(mcp):
    """Keep the SDK's DNS-rebinding guard and add this deployment's public URL."""
    from mcp.server.transport_security import TransportSecuritySettings

    current = mcp.settings.transport_security
    allowed_hosts = set(current.allowed_hosts)
    allowed_origins = set(current.allowed_origins)
    dashboard_url = _os.environ.get("ENGRAPHIS_DASHBOARD_URL", "").strip()
    if dashboard_url:
        parsed = urlsplit(dashboard_url)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None):
            raise ValueError("ENGRAPHIS_DASHBOARD_URL must be an http(s) URL without userinfo")
        from engraphis.netutil import bracket_host
        host = bracket_host(parsed.hostname)
        if parsed.port is not None:
            host = "%s:%d" % (host, parsed.port)
        allowed_hosts.add(host)
        allowed_origins.add("%s://%s" % (parsed.scheme, host))
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=sorted(allowed_hosts),
        allowed_origins=sorted(allowed_origins),
    )


def create_app() -> FastAPI:
    from engraphis.observability import configure_structured_logging
    configure_structured_logging()
    # MCP-over-HTTP agent connect: build the streamable-http ASGI app up front so we can
    # give the dashboard a lifespan that initializes its session manager (a mounted
    # sub-app's own lifespan does NOT run in Starlette - only the root app's does -
    # which is why a naive app.mount('/mcp', mcp.streamable_http_app()) raises
    # 'Task group is not initialized'). The endpoint is built at '/' inside the sub-app
    # so mounting under /mcp lines up (Starlette strips the mount prefix).
    import importlib.util as _importlib_util
    import contextlib as _contextlib
    _mcp_asgi = None
    _mcp_mgr = None
    try:
        if _importlib_util.find_spec("mcp") is None:
            raise ImportError("the optional mcp package is not installed")
        import engraphis.mcp_server as _mcp_mod
        # The MCP session manager's run() is once-per-instance, but create_app() may be
        # called more than once in a process (tests, re-import). Reset the lazily-created
        # manager so each app gets a fresh, runnable one. No-op for the first call.
        try:
            _mcp_mod.mcp._session_manager = None
        except Exception:  # noqa: BLE001 - private attr; stay robust across mcp versions
            pass
        _prev_path = _mcp_mod.mcp.settings.streamable_http_path
        _prev_security = _mcp_mod.mcp.settings.transport_security
        try:
            _mcp_mod.mcp.settings.streamable_http_path = "/"
            _mcp_mod.mcp.settings.transport_security = _mcp_transport_security(_mcp_mod.mcp)
            _mcp_asgi = _mcp_mod.mcp.streamable_http_app()
        finally:
            # streamable_http_app() captures these settings in its session manager. Restore
            # the global FastMCP instance so importing the dashboard cannot alter the
            # standalone MCP server in the same process.
            _mcp_mod.mcp.settings.streamable_http_path = _prev_path
            _mcp_mod.mcp.settings.transport_security = _prev_security
        _mcp_mgr = _mcp_mod.mcp.session_manager
    except (Exception, SystemExit) as _exc:  # noqa: BLE001 - MCP mount stays optional
        import logging as _logging
        # A server-only install intentionally has no MCP SDK; that expected shape stays
        # silent. If an installed SDK fails to mount, retain a warning for operators.
        _level = _logging.INFO if importlib.util.find_spec("mcp") is None else _logging.WARNING
        _logging.getLogger("engraphis").log(
            _level, "MCP /mcp mount skipped (%s)", type(_exc).__name__
        )

    @_contextlib.asynccontextmanager
    async def _lifespan(app: FastAPI):
        try:  # one-line "update available" notice (background, fail-silent, opt-out)
            import logging as _logging

            from engraphis import update_check
            update_check.emit_startup_notice(_logging.getLogger("engraphis").info)
        except Exception:  # noqa: BLE001 - never block dashboard startup
            pass
        if _mcp_asgi is not None:
            async with _mcp_mgr.run():
                yield
        else:
            yield

    # FastAPI's interactive docs execute CDN-hosted JavaScript with same-origin
    # authority. Do not expose that supply-chain surface on an authenticated memory
    # dashboard; the machine-readable schema remains available behind the normal gate.
    app = FastAPI(title="Engraphis Dashboard", docs_url=None, redoc_url=None,
                  openapi_url="/api/openapi.json", lifespan=_lifespan)
    app.state.mcp_over_http = _mcp_asgi is not None

    # Honour the advertised allow-list on the actual GA dashboard entrypoint.  A
    # wildcard can never carry browser credentials.
    _cors_wildcard = "*" in settings.cors_origins
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=not _cors_wildcard,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(licensing.LicenseError)
    async def _license_error(request: Request, exc: licensing.LicenseError):
        feature = exc.feature or "team"
        body = {
            "error": str(exc),
            "upgrade": True,
            "feature": feature,
            "tier_required": licensing.required_plan(feature),
            "upgrade_url": licensing.upgrade_url(),
            "purchase_url": licensing.upgrade_url(),
        }
        return JSONResponse({**body, "detail": body}, status_code=402)
    svc = MemoryService.create(
        settings.db_path, embed_model=settings.embed_model,
        embed_dim=settings.embed_dim or 384,
        allowed_workspaces=settings.allowed_workspaces)
    app.state.service = svc
    try:
        import sys as _sys
        _ed = svc.engine.embedder
        print("[engraphis] embedder: %s dim=%s (%s)" % (
            type(_ed).__name__, getattr(_ed, "dim", "?"),
            _embedder_status(_ed, settings.embed_model)), file=_sys.stderr)
    except Exception:
        pass
    v2_api.set_service(svc)
    app.include_router(v2_api.router)

    app.state.auth_store = None
    app.state.team_enabled = False

    @app.get("/api/auth/state", include_in_schema=False)
    def local_auth_state():
        """Describe the local token gate without exposing hosted Team endpoints."""
        return {
            "enabled": False,
            "mode": "local-token" if settings.api_token else "open",
            "user": None,
            "hosted_team": True,
            "cloud_url": licensing.upgrade_url("team"),
        }

    from engraphis.local_auth import bearer_ok
    from engraphis.netutil import is_local_request

    @app.middleware("http")
    async def _auth_gate(request: Request, call_next):
        from engraphis.service import set_current_user

        # The open runtime has no hosted identity model. Clear any context inherited from
        # embedding applications and authorize the whole local instance as one principal.
        set_current_user(None)
        path = request.url.path
        if request.method == "OPTIONS":
            return await call_next(request)
        guarded = (
            path.startswith("/api/")
            or path == "/mcp"
            or path.startswith("/mcp/")
        )
        if not guarded or path in _PUBLIC:
            return await call_next(request)
        if (path == "/mcp" or path.startswith("/mcp/")) and not app.state.mcp_over_http:
            return JSONResponse({"error": "MCP-over-HTTP is unavailable"}, status_code=404)

        # A configured token protects every non-public API and MCP request. This is a
        # single deployment credential, not a user/seat/role authority.
        if settings.api_token:
            if not bearer_ok(request.headers.get("Authorization"), settings.api_token):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            return await call_next(request)

        # Zero-config access is intentionally loopback-only. Hosted Team deployments use
        # the private cloud service, never this local app's removed account database.
        if not is_local_request(request):
            return JSONResponse(
                {
                    "error": "remote access is disabled until ENGRAPHIS_API_TOKEN is set",
                    "auth": "local-token-required",
                },
                status_code=403,
            )
        return await call_next(request)

    if _STATIC.is_dir():
        app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        resp = FileResponse(_INDEX, media_type="text/html")
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return resp

    for warning in licensing.production_warnings():
        import sys
        print("[engraphis] ship-safety: %s" % warning, file=sys.stderr)

    # Share the dashboard's MemoryService with the MCP server (single writer, no second
    # SQLite connection) and mount the pre-built streamable-http app at /mcp. The session
    # manager is initialized in the app's lifespan (see _lifespan above).
    if _mcp_asgi is not None:
        _mcp_mod.set_service(svc)
        app.mount("/mcp", _mcp_asgi)
        app.state.mcp_over_http = True

    # Installed LAST so it is the OUTERMOST middleware (Starlette wraps in reverse
    # registration order): the headers must also land on the 401/403/402 responses the
    # auth gate returns short of call_next, not only on successful ones.
    from engraphis import http_security
    http_security.install(app)

    return app



#: Module-level ASGI app for ``uvicorn engraphis.dashboard_app:app`` (see
#: scripts/start_dashboard.py). Built once at import.
app = create_app()
