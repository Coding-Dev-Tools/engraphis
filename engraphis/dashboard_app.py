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

# Reachable without any session/token in every mode: the page shell, liveness, and
# auth endpoints needed while logged out. First-admin setup is handled separately below:
# it is safe from loopback, or remotely when the deployment bootstrap token authenticates it.
_PUBLIC = {"/", "/api/health", "/api/ready", "/api/auth/state", "/api/auth/login",
           "/api/auth/logout", "/api/auth/forgot", "/api/auth/reset",
           "/api/auth/invitations/accept", "/webhooks/polar"}

# A zero-user Team install must be able to inspect entitlement before its first admin
# exists. Trial creation is deliberately NOT public: on a hosted instance the deployment
# API token proves ownership, preventing a stranger from consuming the one-device trial.
# This route stops being public as soon as any user is created.
_TEAM_BOOTSTRAP_PUBLIC = {
    "/api/license",
    "/api/license/trials",
}


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
    from engraphis.commercial import service_mode
    mode = service_mode()
    if mode == "vendor":
        from engraphis.vendor_app import create_app as create_vendor_app
        return create_vendor_app()
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
    # The customer-side sync relay uses this same service to enforce the folder boundary
    # for scoped Team tokens. In particular, personal and unknown workspaces must never be
    # addressable through the account-wide relay namespace merely by guessing a name.
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

    # Polar billing webhook — self-hosted purchase fulfillment. Mounted here (as well as
    # on engraphis/app.py) so a single-binary dashboard deployment can fulfill licenses
    # after the standalone Inspector was retired. Route lives in engraphis.billing so all
    # entrypoints share identical signature-verification + idempotency.
    try:
        if not settings.vendor_service:
            raise ImportError("billing webhook disabled in customer service mode")
        from engraphis.billing import router as billing_router
        app.include_router(billing_router)
    except Exception:  # noqa: BLE001 - billing stays optional (e.g. minimal installs)
        pass

    # Cloud license (register/verify/REVOKE) + gated Pro sync relay — mounted on the
    # dashboard binary too, so a single-container team deployment can enforce
    # revocation and serve Pro sync. Endpoints live outside /api (license-key auth),
    # so the _auth_gate below (which only guards /api/*) leaves them alone.
    from engraphis.inspector.cloud_mount import mount_cloud_endpoints
    mount_cloud_endpoints(
        app, include_license=settings.vendor_service,
        include_sync=settings.customer_service)
    # Pre-split keys have team.engraphis.com/license/v1/* signed into them. Customer mode
    # keeps that public surface as a bounded 90-day proxy to license.engraphis.com;
    # combined development mode continues serving the local license router above.
    from engraphis.inspector.license_compat_proxy import mount_license_compat_proxy
    mount_license_compat_proxy(app)

    # Team auth plumbing is mounted whenever team mode is configured. The request gate
    # activates for a live Team license or an already-provisioned user database, so a
    # lapsed license never turns a private team instance into an open single-user app.
    team_enabled, auth_store, team_auth_broken = False, None, False
    try:
        from engraphis.routes import v2_team
        team_enabled, auth_store = v2_team.attach(app, svc)
    except Exception as exc:  # noqa: BLE001 - team stays optional on minimal installs
        # Swallowing this silently was an auth downgrade, not a graceful degradation:
        # with team_enabled False and auth_store None, _auth_gate below skips the ENTIRE
        # role layer AND personal-folder isolation (set_current_user is never called), and
        # the deployment quietly falls back to a single shared API token — viewers get
        # admin reach, personal folders become readable — with nothing in the log to say
        # so. Always leave a trace; and when the operator explicitly asked for team mode,
        # refuse every guarded route (see `team_auth_broken` in _auth_gate below) rather
        # than serving a provisioned team without role enforcement.
        # Imported here, not at module scope: the MCP-mount block above binds `_logging`
        # as a LOCAL of this function, so a module-level alias would be shadowed and
        # unbound on the (normal) path where that block never runs.
        import logging as _log
        _log.getLogger("engraphis").error(
            "team auth failed to mount — role enforcement and personal-folder isolation "
            "are NOT active; /api/* will answer 503 until this is repaired (%s)",
            type(exc).__name__,
        )
        team_auth_broken = settings.team_mode
    # Streamable HTTP sessions are process-local in the MCP SDK. Bind each one to the
    # authenticated user that initialized it so another valid member cannot replay a
    # stolen session id with their own bearer token.
    _mcp_session_users: dict[str, str] = {}

    def _api_bearer_ok(request: Request) -> bool:
        from engraphis.inspector.auth import bearer_ok
        return bearer_ok(request.headers.get("Authorization"), settings.api_token)

    def _bootstrap_bearer_ok(request: Request) -> bool:
        """Accept either bootstrap secret, but only for the explicit bootstrap paths.

        ``ENGRAPHIS_DEPLOYMENT_TOKEN`` proves ownership during hosted onboarding; it is
        not a second unrestricted service-account token.
        """
        from engraphis.inspector.auth import bearer_ok
        deployment = _os.environ.get("ENGRAPHIS_DEPLOYMENT_TOKEN", "").strip()
        return (_api_bearer_ok(request)
                or bearer_ok(request.headers.get("Authorization"), deployment))

    def _bearer_token(request: Request) -> str:
        header = request.headers.get("Authorization") or ""
        return header[7:].strip() if header[:7].lower() == "bearer " else ""

    from engraphis.netutil import is_local_request

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
        # Let CORSMiddleware answer browser preflights. It is registered inside this
        # function middleware, so authenticating first would turn valid preflights into
        # 401/403 responses before the CORS policy could evaluate them.
        if request.method == "OPTIONS":
            return await call_next(request)
        team_bootstrap_public = (
            (path in _TEAM_BOOTSTRAP_PUBLIC
             or (request.method == "GET" and path.startswith("/api/license/trials/")))
            and team_enabled
            and auth_store is not None
            and auth_store.count_users() == 0
        )
        setup_bootstrap_public = (
            path == "/api/auth/setup"
            and team_enabled
            and auth_store is not None
            and auth_store.count_users() == 0
            and (is_local_request(request)
                 or _bootstrap_bearer_ok(request))
        )
        # The OpenAPI schema publishes the full route map and therefore passes through
        # the same wall as every other /api path below.
        if (not path.startswith("/api/") and not (path == "/mcp" or path.startswith("/mcp/"))) \
                or path in _PUBLIC or team_bootstrap_public or setup_bootstrap_public:
            return await call_next(request)
        # Team mode was configured but its auth layer failed to mount (logged at mount
        # time). Continuing would silently fall through to the single shared API token
        # below: no roles, no personal-folder isolation. Refuse every guarded route.
        #
        # Deliberately enforced here rather than by re-raising at mount time: `app =
        # create_app()` runs at module scope and team_mode is ON by default, so raising
        # turns a transient users-db lock into a boot crash loop that cannot self-heal and
        # fails Railway's healthcheck. /api/health and /api/ready are exempted above, so
        # the container stays up and recovers on restart once the store is readable —
        # while everything that needs an identity fails closed until it is.
        if team_auth_broken:
            return JSONResponse(
                {"error": "team authentication is unavailable on this instance",
                 "auth": "team"}, status_code=503)
        # MCP-over-HTTP agent endpoint (/mcp) — Team-gated (402 without a Team license)
        # and authenticated with a per-user bearer token. Each MCP tool then enforces its
        # own viewer/member/admin role while reusing the dashboard's shared MemoryService.
        if path == "/mcp" or path.startswith("/mcp/"):

            if not (team_enabled and auth_store is not None
                    and licensing.has_feature("team")):
                return JSONResponse({"error": "a Team license is required to connect agents",
                                      "feature": "team", "auth": "team"}, status_code=402)
            supplied = _bearer_token(request)
            mu = auth_store.resolve_api_token(supplied) if supplied else None
            if mu is None:
                return JSONResponse({"error": "authentication required", "auth": "team"},
                                    status_code=401)
            if "agent" not in set(mu.get("token_scopes") or ()):
                return JSONResponse({"error": "token lacks agent scope"}, status_code=403)
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
            if not role_at_least(mu["role"], "member"):
                return JSONResponse({"error": "role member required", "auth": "team"},
                                    status_code=403)
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
        if settings.api_token and _api_bearer_ok(request):
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
            supplied = _bearer_token(request)
            user = auth_store.resolve_api_token(supplied) if supplied else None
            if user is not None and "agent" not in set(user.get("token_scopes") or ()):
                return JSONResponse({"error": "token lacks agent scope"}, status_code=403)
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
        if settings.api_token:
            if not _api_bearer_ok(request):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            return await call_next(request)

        # No auth wall of any kind is active: team mode has no users AND no paid license
        # yet, and no ENGRAPHIS_API_TOKEN is set. Locally that is the intended
        # zero-config experience. Reached over the network it is a hole — this is the
        # window between "Railway finishes the deploy" and "the operator creates the
        # first admin", during which the container is already bound publicly and
        # /api/* would otherwise answer anyone. That exposes routes that are admin-only
        # the moment the wall goes up: /api/resources/postgres (outbound connect to a
        # caller-supplied DSN), /api/code/index and /api/workspaces/import-folder
        # (server-local file reads), /api/workspaces/delete.
        #
        # Hosted first-admin setup requires ENGRAPHIS_DEPLOYMENT_TOKEN; otherwise any
        # remote caller could win a deployment race and make themselves the first admin.
        # The same ownership proof starts the deployment-bound trial from the public
        # onboarding screen, but it never grants access to ordinary data routes.
        if not is_local_request(request):
            return JSONResponse(
                {"error": "this instance has no authentication configured, so remote API "
                          "access is refused. For a hosted first boot, set "
                          "ENGRAPHIS_DEPLOYMENT_TOKEN and ENGRAPHIS_DASHBOARD_URL in the "
                          "deployment environment, then use the hosted setup screen to "
                          "activate a trial and create the first admin account.",
                 "auth": "unconfigured"},
                status_code=403)
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

    _maybe_start_autosync()
    _maybe_start_dreaming()
    _maybe_start_license_revalidation()
    _maybe_start_email_outbox()
    return app


#: Guard so repeated ``create_app()`` calls (or a re-import) never spawn a second loop.
_AUTOSYNC_STARTED = False
_DREAMING_STARTED = False
_REVALIDATE_STARTED = False
_EMAIL_OUTBOX_STARTED = False


def _process_due_email() -> dict:
    """Run one customer-local outbox pass with the configured mail provider."""
    from engraphis import email_outbox
    from engraphis.inspector.webhooks import _deliver_text_email
    return email_outbox.process_due(_deliver_text_email, limit=20)


def _maybe_start_email_outbox() -> None:
    """Retry locally configured invitation/reset email without blocking requests.

    Production Railway customers normally relay mail to the vendor control plane, whose
    ASGI lifespan owns its worker. A self-hosted customer may configure Resend/SMTP
    locally; immediate sends already persist failures in the durable outbox, so this loop
    makes the bounded retry policy effective there too.
    """
    global _EMAIL_OUTBOX_STARTED
    if _EMAIL_OUTBOX_STARTED:
        return
    import sys
    if "pytest" in sys.modules or _os.environ.get("PYTEST_CURRENT_TEST"):
        return
    if _os.environ.get("ENGRAPHIS_EMAIL_OUTBOX_LOOP", "1").strip().lower() in (
            "0", "false", "no", "off"):
        return
    from engraphis.inspector.webhooks import email_configured
    if not email_configured():
        return
    import logging
    import threading
    import time

    logger = logging.getLogger("engraphis.email_outbox")

    def _loop() -> None:
        time.sleep(10)
        while True:
            try:
                _process_due_email()
            except Exception as exc:  # noqa: BLE001 - retry loop must survive one bad iteration
                logger.error(
                    "customer email outbox iteration failed (%s)", type(exc).__name__
                )
            time.sleep(30)

    threading.Thread(target=_loop, name="engraphis-email-outbox", daemon=True).start()
    _EMAIL_OUTBOX_STARTED = True


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
