"""FastAPI app assembly — mounts all routes, serves dashboard, initializes DB,
starts background loop."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional, Union

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from engraphis import __version__
from engraphis.local_auth import bearer_ok
from engraphis.config import settings
from engraphis.engines import reweight, thoughts as thoughts_engine
from engraphis.engines.embedder import warmup as _warmup_embedder
from engraphis.logging_setup import configure_logging
from engraphis.netutil import client_ip
from engraphis.routes.memory import router as memory_router
from engraphis.routes.vault import VAULT_UPLOAD_REQUEST_BYTES, router as vault_router
from engraphis.stores import get_conn, init_db

logger = logging.getLogger("engraphis")


_background_task: Optional[asyncio.Task] = None
_STATIC_DIR = Path(__file__).resolve().parent / "static"
# Readiness cache: only a *successful* embedder init is cached, so a transient
# failure is re-checked on the next probe instead of wedging the pod NotReady.
_embedder_ok: bool = False
_UPLOAD_LIMIT_PATHS = frozenset({
    "/api/workspaces/import-files",
    "/memory/vaults/upload-folder",
    "/memory/vaults/upload-folder-smart",
})


class LegacyReferenceConfigurationError(RuntimeError):
    """The retired v1 server was not given a safely isolated database."""


def _canonical_db_path(value: Union[str, Path]) -> Path:
    """Return a comparison-safe database path without requiring it to exist."""
    return Path(value).expanduser().resolve(strict=False)


def _activate_legacy_reference_db(legacy_db_path: Union[str, Path]) -> str:
    """Point the v1-only store at an explicitly separate compatibility database.

    The legacy routes use the process-global v1 ``settings.db_path``.  They are safe
    only in their own process, and only after this guard has rejected the active v2
    database.  Dropping any thread-local v1 connection also prevents a prior test or
    embedder call from keeping the old database open after the switch.
    """
    if not str(legacy_db_path).strip():
        raise LegacyReferenceConfigurationError(
            "the v1 reference requires an explicit --legacy-db path"
        )
    legacy_path = _canonical_db_path(legacy_db_path)
    current_v2_path = _canonical_db_path(settings.db_path)
    if legacy_path == current_v2_path:
        raise LegacyReferenceConfigurationError(
            "the v1 reference database must differ from the current v2 database "
            "(%s)" % current_v2_path
        )

    # The v1 store is intentionally process-global.  This factory is therefore an
    # internal compatibility boundary, not a way to mount v1 beside v2 in one server.
    from engraphis import stores as legacy_stores

    connection = getattr(legacy_stores._local, "conn", None)
    if connection is not None:
        connection.close()
        del legacy_stores._local.conn
    settings.db_path = str(legacy_path)
    return settings.db_path


def create_legacy_reference_app(*, legacy_db_path: Union[str, Path]) -> FastAPI:
    """Build the internal v1 compatibility application on an isolated database.

    This is deliberately distinct from the public v2 server and dashboard launchers.
    Callers must supply the legacy database explicitly; using the configured v2
    database is rejected before any schema initialization can occur.
    """
    _activate_legacy_reference_db(legacy_db_path)
    return _build_legacy_reference_app()


class _RequestBodyTooLarge(Exception):
    """Internal signal used by the streaming ASGI request limiter."""


class _VaultUploadLimitMiddleware:
    """Reject oversized file imports before multipart parsing/spooling.

    ``Content-Length`` provides an immediate fast-fail. The receive wrapper is still
    required because clients can omit or lie about that header, including HTTP/1.1
    chunked uploads. A fronting proxy should configure an equal or lower request-body
    ceiling; this in-process guard remains the last line of defense for direct access.
    """

    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] != "http"
            or scope["method"] != "POST"
            or scope["path"].rstrip("/") not in _UPLOAD_LIMIT_PATHS
        ):
            await self.app(scope, receive, send)
            return

        raw_lengths = [
            value for name, value in scope.get("headers", [])
            if name.lower() == b"content-length"
        ]
        if len(raw_lengths) > 1:
            await JSONResponse(
                {"error": "invalid content-length"},
                status_code=400,
            )(scope, receive, send)
            return
        if raw_lengths:
            try:
                declared_length = int(raw_lengths[0])
            except (TypeError, ValueError):
                declared_length = -1
            if declared_length < 0:
                await JSONResponse(
                    {"error": "invalid content-length"},
                    status_code=400,
                )(scope, receive, send)
                return
            if declared_length > self.max_bytes:
                await self._too_large(scope, receive, send)
                return

        received = 0
        limit_exceeded = False

        async def limited_receive():
            nonlocal limit_exceeded, received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    limit_exceeded = True
                    raise _RequestBodyTooLarge
            return message

        async def guarded_send(message):
            # FastAPI converts arbitrary body-parser exceptions into a generic 400.
            # Suppress that replacement response once our receive wrapper has observed
            # the real cause; the middleware emits the canonical 413 below.
            if limit_exceeded:
                return
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except _RequestBodyTooLarge:
            pass
        if limit_exceeded:
            await self._too_large(scope, receive, send)

    async def _too_large(self, scope, receive, send):
        await JSONResponse(
            {
                "error": "request body too large",
                "max_bytes": self.max_bytes,
            },
            status_code=413,
        )(scope, receive, send)


def _embedder_ready() -> bool:
    global _embedder_ok
    try:
        from engraphis.backends.embedder_st import get_embedder
        emb = get_embedder(settings.embed_model or None, settings.embed_dim or 384)
        _embedder_ok = emb is not None and int(emb.dim) > 0
    except Exception as exc:  # pragma: no cover - defensive; get_embedder falls back itself
        # Provider/backend exceptions can contain credentialed URLs or local paths.
        # Readiness logs need the failure class, not the exception payload.
        logger.warning("Readiness: embedder init failed (%s)", type(exc).__name__)
        _embedder_ok = False
    return _embedder_ok


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Startup/shutdown for the app (replaces the deprecated @app.on_event hooks).

    Startup: initialize the DB (deferred to here so the CLI can set ENGRAPHIS_DB_PATH
    first), then start the background consolidation loop unless it's disabled. Shutdown:
    cancel and await the loop."""
    global _background_task
    init_db()
    # Warm the embedding model eagerly so the first recall call isn't paid
    # under request pressure (a cold load + concurrent call used to wedge
    # the forked PM2 worker and time out every recall).
    await asyncio.get_running_loop().run_in_executor(None, _warmup_embedder)
    if settings.loop_interval > 0:
        _background_task = asyncio.create_task(_consciousness_loop())
        logger.info("Background consciousness loop started (interval=%ds)", settings.loop_interval)
    else:
        logger.info("Background loop disabled (ENGRAPHIS_LOOP_INTERVAL=0)")
    try:  # one-line "update available" notice (background, fail-silent, opt-out)
        from engraphis import update_check
        update_check.emit_startup_notice(logger.info)
    except Exception:  # noqa: BLE001 - never block server startup
        pass
    try:
        yield
    finally:
        if _background_task:
            _background_task.cancel()
            try:
                await _background_task
            except asyncio.CancelledError:
                pass


def _build_legacy_reference_app() -> FastAPI:
    """Build the v1 compatibility/reference FastAPI application."""
    configure_logging()
    # Hosted JSON logging is credential-redacting. Keep this after the legacy logging
    # setup so it replaces that formatter, and pair it with the launcher's log_config=None
    # so Uvicorn cannot replace it again after app construction.
    from engraphis.observability import configure_structured_logging
    configure_structured_logging()

    app = FastAPI(
        title="Engraphis",
        description="Self-hosted AI memory engine for agents — Ebbinghaus decay, "
                    "interaction-aware recall, bi-temporal facts, and background "
                    "consolidation. Local-first; you bring the LLM.",
        version=__version__,
        lifespan=_lifespan,
        docs_url=None,
        redoc_url=None,
    )

    # Local-first CORS: loopback by default, override with ENGRAPHIS_CORS_ORIGINS.
    # Credentials are only allowed when the allow-list is explicit (never with "*").
    _wildcard = "*" in settings.cors_origins
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=not _wildcard,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(
        _VaultUploadLimitMiddleware,
        max_bytes=VAULT_UPLOAD_REQUEST_BYTES,
    )

    # Bearer-token auth when ENGRAPHIS_API_TOKEN is set; loopback-only otherwise.
    # Health-type probes (liveness + readiness) stay unauthenticated by convention.
    _PUBLIC_PROBES = frozenset({
        "/memory/health",
        "/api/health",
        "/api/ready",
        "/openapi.json",
    })

    def _public_path(path: str) -> bool:
        # ``/memory/health/*`` contains owner data such as titles and content previews;
        # only the exact liveness probe is public. Static files remain prefix-matched.
        return path in _PUBLIC_PROBES or path == "/static" or path.startswith("/static/")

    from engraphis.netutil import is_local_request

    @app.middleware("http")
    async def _require_token(request: Request, call_next):
        token = settings.api_token
        if (request.method == "OPTIONS" or request.url.path == "/"
                or _public_path(request.url.path)):
            return await call_next(request)
        if token:
            if not bearer_ok(request.headers.get("authorization"), token):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            return await call_next(request)

        # Zero-config access is loopback-only, matching dashboard_app's gate.  Without
        # this backstop a bind-all deployment (docker-entrypoint.sh defaults
        # ENGRAPHIS_HOST to "::") publishes /memory/export and /memory/admin/* to every
        # reachable peer.  scripts/graph_server.py already refuses the equivalent
        # non-loopback start; this applies the same rule to the v1 surface.
        if not is_local_request(request):
            return JSONResponse(
                {
                    "error": "remote access is disabled until ENGRAPHIS_API_TOKEN is set",
                    "auth": "local-token-required",
                },
                status_code=403,
            )
        return await call_next(request)

    # Optional in-process rate limiting (per-client-IP sliding window). Disabled unless
    # ENGRAPHIS_RATE_LIMIT > 0. In-memory/per-process — fine for one self-hosted instance;
    # front it with a reverse proxy for multi-process or distributed limits.
    if settings.rate_limit > 0:
        _hits: dict[str, deque] = defaultdict(deque)
        _PRUNE_EVERY = 60  # seconds between cleanup sweeps
        _last_prune = time.monotonic()

        @app.middleware("http")
        async def _rate_limit(request: Request, call_next):
            nonlocal _last_prune
            if request.method == "OPTIONS" or _public_path(request.url.path):
                return await call_next(request)
            client = client_ip(request)
            now = time.monotonic()
            # Periodically prune stale IP entries to prevent unbounded growth.
            if now - _last_prune > _PRUNE_EVERY:
                cutoff_all = now - settings.rate_window
                stale = [k for k, dq in _hits.items() if not dq or dq[-1] < cutoff_all]
                for k in stale:
                    del _hits[k]
                _last_prune = now
            dq = _hits[client]
            cutoff = now - settings.rate_window
            while dq and dq[0] <= cutoff:
                dq.popleft()
            if len(dq) >= settings.rate_limit:
                retry = int(dq[0] + settings.rate_window - now) + 1
                return JSONResponse({"error": "rate limit exceeded"}, status_code=429,
                                    headers={"Retry-After": str(retry)})
            dq.append(now)
            return await call_next(request)

    # Request-ID + access log. Defined last so it is the *outermost* middleware and
    # also covers requests short-circuited by auth/rate-limit above. An incoming
    # X-Request-ID is propagated (so a fronting proxy's id survives); otherwise one
    # is assigned. Echoed on the response for client-side correlation.
    @app.middleware("http")
    async def _request_log(request: Request, call_next):
        request_id = request.headers.get("x-request-id", "").strip() or uuid.uuid4().hex
        start = time.perf_counter()
        response = await call_next(request)
        duration_ms = round((time.perf_counter() - start) * 1000, 1)
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "%s %s -> %d (%.1fms)",
            request.method, request.url.path, response.status_code, duration_ms,
            extra={"request_id": request_id, "method": request.method,
                   "path": request.url.path, "status": response.status_code,
                   "duration_ms": duration_ms},
        )
        return response

    # Baseline security response headers, outermost of all (registered after the log
    # middleware, so it wraps it) — see engraphis.http_security.
    from engraphis import http_security
    http_security.install(app)

    # DB init + background loop lifecycle live in _lifespan (above); see FastAPI(lifespan=…).
    app.include_router(memory_router)
    app.include_router(vault_router)

    # ── probes (unauthenticated; see _PUBLIC_PROBES) ────────────────────────────
    @app.get("/api/health")
    async def api_health():
        """Liveness: the process is up and serving. No dependency checks."""
        return {"status": "ok", "timestamp": time.time(), "service": "engraphis"}

    @app.get("/api/ready")
    async def api_ready():
        """Readiness: DB answers a trivial SELECT and the embedder backend
        initializes. 503 until both hold, so orchestrators hold traffic."""
        checks = {"db": False, "embedder": False}
        try:
            get_conn().execute("SELECT 1").fetchone()
            checks["db"] = True
        except Exception as exc:
            logger.warning("Readiness: db check failed (%s)", type(exc).__name__)
        checks["embedder"] = _embedder_ready()
        ready = all(checks.values())
        return JSONResponse({"ready": ready, "checks": checks, "version": __version__},
                            status_code=200 if ready else 503)

    @app.get("/", response_class=HTMLResponse)
    async def dashboard():
        """Serve the visual dashboard."""
        index_path = _STATIC_DIR / "index.html"
        if index_path.exists():
            return HTMLResponse(index_path.read_text(encoding="utf-8"))
        return HTMLResponse("<h1>Dashboard not found</h1><p>Static files missing at: "
                            f"{_STATIC_DIR}</p>", status_code=404)

    if _STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    return app


async def _consciousness_loop() -> None:
    """Phase 2 + Phase 4 background cycle: decay → thought synthesis → reweight."""
    _consecutive_errors = 0
    while True:
        try:
            await asyncio.sleep(settings.loop_interval)
            touched = reweight.decay_pass(namespace=None)
            if touched:
                logger.info("Decay pass: %d memories reweighted", touched)
            result = thoughts_engine.synthesize_thoughts(
                namespace=None,
                max_chunks=settings.loop_top_k,
                persist=True,
            )
            if result.get("persisted"):
                # A synthesized thought is memory content. Never copy it into logs.
                logger.info(
                    "Thought synthesized and persisted (sources=%d)",
                    int(result.get("source_count") or 0),
                )
            _consecutive_errors = 0
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _consecutive_errors += 1
            backoff = min(60, settings.loop_interval * (2 ** _consecutive_errors))
            logger.error("Consciousness loop error (%s), backing off %ds",
                         type(exc).__name__, backoff)
            await asyncio.sleep(backoff)


def _create_retired_direct_app() -> FastAPI:
    """Retire the old ``uvicorn engraphis.app:app`` deployment target safely."""
    retired = FastAPI(
        title="Engraphis v1 reference retired",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @retired.api_route(
        "/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
        include_in_schema=False,
    )
    async def legacy_reference_retired(path: str):
        return JSONResponse(
            {
                "error": "legacy v1 reference application is retired",
                "detail": (
                    "Use engraphis-dashboard or engraphis-server for v2. "
                    "The internal v1 reference requires "
                    "python -m scripts.legacy_reference --legacy-db <separate-path>."
                ),
            },
            status_code=410,
        )

    return retired


# Keep the historical ASGI import target inert.  A direct ``engraphis.app:app`` launch
# must never initialize the v1 schema in the configured (normally v2) database.
app = _create_retired_direct_app()
