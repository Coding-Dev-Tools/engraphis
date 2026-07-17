"""The restored v1 dashboard — served on the *v2* engine.

Same look-and-feel as the original dashboard (engraphis/static/index.html), but every
route reads/writes the v2 MemoryService where the real data lives. This keeps the v1
server (engraphis/app.py) untouched; run this with `python -m scripts.start_dashboard`.
"""
from __future__ import annotations

import hmac
import importlib.util
from pathlib import Path
from urllib.parse import urlsplit

import os as _os

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from engraphis import licensing
from engraphis.config import settings
from engraphis.routes import v2_api
from engraphis.service import MemoryService

_STATIC = Path(__file__).resolve().parent / "static"
_INDEX = _STATIC / "index.html"

# Reachable without any session/token in every mode: the page shell, liveness, and
# auth bootstrap endpoints (state/login/setup must be reachable while logged out;
# setup still refuses to create the first admin until a paid license is active).
_PUBLIC = {"/", "/api/health", "/api/ready", "/api/auth/state", "/api/auth/login",
           "/api/auth/setup", "/api/auth/logout", "/api/auth/forgot", "/api/auth/reset",
           "/webhooks/polar"}

# A zero-user Team install must be able to inspect entitlement and start a trial before
# its first admin exists. These routes stop being public as soon as any user is created.
_TEAM_BOOTSTRAP_PUBLIC = {
    "/api/license", "/api/license/trial", "/api/license/team-trial",
}


def _mcp_transport_security(mcp):
    """Keep the SDK's DNS-rebinding guard and add this deployment's public URL."""
    from mcp.server.transport_security import TransportSecuritySettings

    current = mcp.settings.transport_security
    allowed_hosts = set(current.allowed_hosts)
    allowed_origins = set(current.allowed_origins)
    dashboard_url = _os.environ.get("ENGRAPHIS_DASHBOARD_URL", "").strip()
    if dashboard_url:
        parsed = urlsplit(dashboard_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username:
            raise ValueError("ENGRAPHIS_DASHBOARD_URL must be an http(s) URL without userinfo")
        hostname = parsed.hostname
        host = "[%s]" % hostname if ":" in hostname else hostname
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
    # MCP-over-HTTP agent connect: build the streamable-http ASGI app up front so we can
    # give the dashboard a lifespan that initializes its session manager (a mounted
    # sub-app's own lifespan does NOT run in Starlette - only the root app's does -
    # which is why a naive app.mount('/mcp', mcp.streamable_http_app()) raises
    # 'Task group is not initialized'). The endpoint is built at '/' inside the sub-app
    # so mounting under /mcp lines up (Starlette strips the mount prefix).
    import contextlib as _contextlib
    _mcp_asgi = None
    _mcp_mgr = None
    try:
        if importlib.util.find_spec("mcp") is None:
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
        import sys as _sys
        print("[engraphis] MCP /mcp mount skipped: %s" % _exc, file=_sys.stderr)

    @_contextlib.asynccontextmanager
    async def _lifespan(app: FastAPI):
        if _mcp_asgi is not None:
            async with _mcp_mgr.run():
                yield
        else:
            yield

    app = FastAPI(title="Engraphis Dashboard", docs_url="/api/docs",
                  openapi_url="/api/openapi.json", lifespan=_lifespan)
    app.state.mcp_over_http = False

    @app.exception_handler(licensing.LicenseError)
    async def _license_error(_request: Request, exc: licensing.LicenseError):
        feature = exc.feature or "team"
        body = {
            "error": str(exc),
            "feature": feature,
            "tier_required": licensing.required_plan(feature),
            "upgrade_url": licensing.upgrade_url(),
        }
        return JSONResponse({**body, "detail": body}, status_code=402)
    svc = MemoryService.create(
        settings.db_path, embed_model=settings.embed_model,
        embed_dim=settings.embed_dim or 384,
        allowed_workspaces=settings.allowed_workspaces)
    try:
        import sys as _sys
        from engraphis.backends.embedder_deterministic import DeterministicEmbedder
        _ed = svc.engine.embedder
        _ok = not isinstance(_ed, DeterministicEmbedder)
        print("[engraphis] embedder: %s dim=%s %s" % (
            type(_ed).__name__, getattr(_ed, "dim", "?"),
            "(semantic search ready)" if _ok else
            "(deterministic fallback - semantic Recall/Why/Timeline disabled; "
            "install sentence-transformers into THIS python)"), file=_sys.stderr)
    except Exception:
        pass
    v2_api.set_service(svc)
    app.include_router(v2_api.router)

    # Polar billing webhook — self-hosted purchase fulfillment. Mounted here (as well as
    # on engraphis/app.py) so a single-binary dashboard deployment can fulfill licenses
    # after the standalone Inspector was retired. Route lives in engraphis.billing so all
    # entrypoints share identical signature-verification + idempotency.
    try:
        from engraphis.billing import router as billing_router
        app.include_router(billing_router)
    except Exception:  # noqa: BLE001 - billing stays optional (e.g. minimal installs)
        pass

    # Cloud license (register/verify/REVOKE) + gated Pro sync relay — mounted on the
    # dashboard binary too, so a single-container team deployment can enforce
    # revocation and serve Pro sync. Endpoints live outside /api (license-key auth),
    # so the _auth_gate below (which only guards /api/*) leaves them alone.
    from engraphis.inspector.cloud_mount import mount_cloud_endpoints
    mount_cloud_endpoints(app)

    # Team auth plumbing is mounted whenever team mode is configured. The request gate
    # activates for a live Team license or an already-provisioned user database, so a
    # lapsed license never turns a private team instance into an open single-user app.
    team_enabled, auth_store = False, None
    try:
        from engraphis.routes import v2_team
        team_enabled, auth_store = v2_team.attach(app, svc)
    except Exception:  # noqa: BLE001 - team stays optional
        pass
    # Streamable HTTP sessions are process-local in the MCP SDK. Bind each one to the
    # authenticated user that initialized it so another valid member cannot replay a
    # stolen session id with their own bearer token.
    _mcp_session_users: dict[str, str] = {}

    def _bearer_ok(request: Request) -> bool:
        token = settings.api_token
        if not token:
            return False
        supplied = (request.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        return bool(supplied) and hmac.compare_digest(supplied, token)

    @app.middleware("http")
    async def _auth_gate(request: Request, call_next):
        from engraphis.service import set_current_user
        # Clear any user bound to this context before we decide who (if anyone) is calling,
        # so a personal-folder check can never inherit a stale identity from a prior request
        # served on the same worker context. The team branch below rebinds the real user;
        # public paths, the bearer bypass, and single-user mode all leave it cleared, which
        # is exactly "no per-user restriction".
        set_current_user(None)
        path = request.url.path
        team_bootstrap_public = (
            path in _TEAM_BOOTSTRAP_PUBLIC
            and team_enabled
            and auth_store is not None
            and auth_store.count_users() == 0
        )
        if (not path.startswith("/api/") and not (path == "/mcp" or path.startswith("/mcp/"))) \
                or path in _PUBLIC or team_bootstrap_public or path.startswith("/api/docs") \
                or path.startswith("/api/openapi"):
            return await call_next(request)
        # MCP-over-HTTP agent endpoint (/mcp) — Team-gated (402 without a Team license)
        # and authenticated with a per-user bearer token. Each MCP tool then enforces its
        # own viewer/member/admin role while reusing the dashboard's shared MemoryService.
        if path == "/mcp" or path.startswith("/mcp/"):
            if not (team_enabled and auth_store is not None
                    and licensing.has_feature("team")):
                return JSONResponse({"error": "a Team license is required to connect agents",
                                      "feature": "team", "auth": "team"}, status_code=402)
            supplied = (request.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
            mu = auth_store.resolve_api_token(supplied) if supplied else None
            if mu is None:
                return JSONResponse({"error": "authentication required", "auth": "team"},
                                    status_code=401)
            if not app.state.mcp_over_http:
                return JSONResponse({"error": "MCP-over-HTTP is unavailable"},
                                    status_code=404)
            session_id = (request.headers.get("Mcp-Session-Id") or "").strip()
            if session_id:
                owner_id = _mcp_session_users.get(session_id)
                if owner_id is None:
                    return JSONResponse({"error": "unknown MCP session"}, status_code=401)
                if owner_id != mu["id"]:
                    return JSONResponse({"error": "MCP session belongs to another user"},
                                        status_code=403)

            # Roles must be evaluated on every HTTP request, not captured when the MCP
            # session starts: a token owner's role or disabled state can change while the
            # session remains open. Reading the JSON body here is safe with Starlette's
            # BaseHTTPMiddleware request wrapper; call_next receives the cached body.
            if request.method == "POST":
                try:
                    payload = await request.json()
                except Exception:  # noqa: BLE001 - the MCP SDK returns its protocol error
                    payload = None
                messages = payload if isinstance(payload, list) else [payload]
                from engraphis.inspector.auth import role_at_least
                for message in messages:
                    if not isinstance(message, dict) or message.get("method") != "tools/call":
                        continue
                    params = message.get("params")
                    tool_name = params.get("name", "") if isinstance(params, dict) else ""
                    minimum = _mcp_mod.minimum_role(str(tool_name))
                    if not role_at_least(mu.get("role", ""), minimum):
                        return JSONResponse({"error": "requires the %s role" % minimum},
                                            status_code=403)
            request.state.user = mu
            set_current_user(mu)
            response = await call_next(request)
            response_session = (response.headers.get("Mcp-Session-Id") or "").strip()
            if response_session:
                existing = _mcp_session_users.setdefault(response_session, mu["id"])
                if existing != mu["id"]:  # pragma: no cover - defensive collision guard
                    return JSONResponse({"error": "MCP session collision"}, status_code=409)
            if request.method == "DELETE" and session_id and response.status_code < 400:
                _mcp_session_users.pop(session_id, None)
            return response
        # Service-account bearer token bypass — skips team auth entirely,
        # allowing CI/CD scripts and automation to use the same ENGRAPHIS_API_TOKEN
        # regardless of whether team mode is enabled.
        if settings.api_token and _bearer_ok(request):
            return await call_next(request)
        # A new, unlicensed instance with no users remains open for solo use. Once a paid
        # license (Pro or Team) activates the wall—or any users have been provisioned—the
        # wall stays up even if entitlement later lapses. Login remains public; paid writes
        # and seat growth keep their route-level license gates.
        team_auth_active = (team_enabled and auth_store is not None
                            and (auth_store.count_users() > 0
                                 or licensing.current_license().is_paid))
        if team_auth_active:
            from engraphis.inspector.auth import min_role, role_at_least
            from engraphis.routes.v2_team import _COOKIE
            # Agent connect: a per-user API bearer token (minted via POST /api/auth/token)
            # authenticates exactly like a cookie session, but for headless agents. Try it
            # first so an agent with no cookie is still bound to its member identity, then
            # fall back to the browser session cookie. Either way the resolved member is
            # bound via set_current_user so personal-folder ownership holds on every
            # workspace-scoped read/write.
            supplied = (request.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
            user = auth_store.resolve_api_token(supplied) if supplied else None
            if user is None:
                user = auth_store.resolve_session(request.cookies.get(_COOKIE, ""))
            if user is None:
                return JSONResponse({"error": "authentication required", "auth": "team"},
                                    status_code=401)
            need = min_role(request.method, path)
            if not role_at_least(user["role"], need):
                return JSONResponse({"error": "requires the %s role" % need},
                                    status_code=403)
            request.state.user = user
            # Bind the identity the service reads to enforce personal-folder ownership on
            # every workspace-scoped read/write (see MemoryService._authorize_workspace).
            set_current_user(user)
            return await call_next(request)
        # Single-user modes: optional bearer token, exactly as before team mode existed.
        if settings.api_token and not _bearer_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
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

    _maybe_start_autosync()
    _maybe_start_dreaming()
    _maybe_start_license_revalidation()
    return app


#: Guard so repeated ``create_app()`` calls (or a re-import) never spawn a second loop.
_AUTOSYNC_STARTED = False
_DREAMING_STARTED = False
_REVALIDATE_STARTED = False


def _maybe_start_autosync() -> None:
    """Launch the background auto-sync loop once — unless disabled or under pytest.

    A single daemon thread polls the persisted auto-sync policy (:mod:`engraphis.autosync`)
    and runs a sync pass whenever the cadence is due. It is **opt-in** (the policy defaults
    to disabled, so nothing happens until the user flips the Settings toggle), it is
    licensed-gated inside :func:`autosync.run_once` (a lapsed plan / missing key just
    no-ops), and it is fully fault-isolated: every error is swallowed and retried next tick
    so the loop can never take the dashboard down. Skipped under pytest so the test suite
    never opens a network loop, and switch-offable with ``ENGRAPHIS_AUTOSYNC_LOOP=0``."""
    global _AUTOSYNC_STARTED
    if _AUTOSYNC_STARTED:
        return
    import sys
    if "pytest" in sys.modules or _os.environ.get("PYTEST_CURRENT_TEST"):
        return
    if _os.environ.get("ENGRAPHIS_AUTOSYNC_LOOP", "1").strip().lower() in (
            "0", "false", "no", "off"):
        return
    import threading
    import time

    def _loop() -> None:
        from engraphis import autosync
        from engraphis.routes import v2_api
        time.sleep(10)   # let startup settle before the first poll
        while True:
            try:
                if autosync.due(autosync.load_policy()):
                    autosync.run_once(v2_api.service())
            except Exception:  # noqa: BLE001 — the loop must outlive any single failure
                pass
            time.sleep(60)

    threading.Thread(target=_loop, name="engraphis-autosync", daemon=True).start()
    _AUTOSYNC_STARTED = True


def _maybe_start_dreaming() -> None:
    """Launch the background "dreaming" loop once — automated consolidation without cron.

    A single daemon thread polls the persisted maintenance policy (:mod:`engraphis.automation`)
    and runs a sweep whenever the cadence is due **or** the dreaming trigger fires (enough new
    episodic memories have accumulated and the store has gone quiet — ``automation.dream_due``).
    Same safety envelope as the auto-sync loop: **opt-in** (the policy defaults to disabled),
    **Pro-gated** (``run_maintenance`` funnels through ``require_feature('automation')`` and the
    loop checks ``has_feature`` first so the free tier no-ops cheaply), fully **fault-isolated**
    (every error swallowed, retried next tick), skipped under pytest, and switch-offable with
    ``ENGRAPHIS_DREAM_LOOP=0``. Polls every 5 minutes — consolidation is heavier than a sync."""
    global _DREAMING_STARTED
    if _DREAMING_STARTED:
        return
    import sys
    if "pytest" in sys.modules or _os.environ.get("PYTEST_CURRENT_TEST"):
        return
    if _os.environ.get("ENGRAPHIS_DREAM_LOOP", "1").strip().lower() in (
            "0", "false", "no", "off"):
        return
    import threading
    import time

    def _loop() -> None:
        from engraphis import automation, licensing
        from engraphis.routes import v2_api
        time.sleep(20)   # let startup settle (after the autosync poll)
        while True:
            try:
                if licensing.has_feature("automation"):
                    svc = v2_api.service()
                    if automation.dream_due(svc):
                        automation.run_maintenance(svc, dry_run=False)
            except Exception:  # noqa: BLE001 — the loop must outlive any single failure
                pass
            time.sleep(300)

    threading.Thread(target=_loop, name="engraphis-dreaming", daemon=True).start()
    _DREAMING_STARTED = True


def _refresh_configured_license() -> None:
    """Refresh a configured key even when its cached fallback is currently free."""
    from engraphis import licensing
    if licensing._read_key_material():
        licensing.current_license(refresh=True)


def _maybe_start_license_revalidation() -> None:
    """Launch a background loop that periodically refreshes a configured license
    against the vendor relay — unless disabled or under pytest.

    ``gate()`` only re-registers when the cached lease actually expires, which means a
    revoked/refunded key can keep working locally for up to the full lease TTL before
    the next natural gate check catches it. Calling
    ``current_license(refresh=True)`` both propagates revocation and recovers a valid key
    after a transient service outage. A no-op when there is no configured key. Same
    safety envelope as the other background loops: fully fault-isolated, skipped under
    pytest, switch-offable with ``ENGRAPHIS_REVALIDATE_LOOP=0``."""
    global _REVALIDATE_STARTED
    if _REVALIDATE_STARTED:
        return
    import sys
    if "pytest" in sys.modules or _os.environ.get("PYTEST_CURRENT_TEST"):
        return
    if _os.environ.get("ENGRAPHIS_REVALIDATE_LOOP", "1").strip().lower() in (
            "0", "false", "no", "off"):
        return
    import threading
    import time

    def _loop() -> None:
        time.sleep(30)   # let startup settle (after the autosync/dreaming polls)
        while True:
            try:
                _refresh_configured_license()
            except Exception:  # noqa: BLE001 — the loop must outlive any single failure
                pass
            time.sleep(600)

    threading.Thread(target=_loop, name="engraphis-revalidate", daemon=True).start()
    _REVALIDATE_STARTED = True


#: Module-level ASGI app for ``uvicorn engraphis.dashboard_app:app`` (see
#: scripts/start_dashboard.py). Built once at import; the background loops inside
#: create_app() are pytest-guarded so importing this module under test is safe.
app = create_app()
