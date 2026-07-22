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
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from engraphis import __version__
from engraphis.inspector.auth import bearer_ok
from engraphis.inspector.cloud_mount import CLOUD_PREFIXES, mount_cloud_endpoints
from engraphis.config import settings
from engraphis.engines import reweight, thoughts as thoughts_engine
from engraphis.engines.embedder import warmup as _warmup_embedder
from engraphis.logging_setup import configure_logging
from engraphis.netutil import client_ip
from engraphis.routes.memory import router as memory_router
from engraphis.routes.vault import router as vault_router
from engraphis.stores import get_conn, init_db

logger = logging.getLogger("engraphis")


_background_task: Optional[asyncio.Task] = None
_STATIC_DIR = Path(__file__).resolve().parent / "static"
# Readiness cache: only a *successful* embedder init is cached, so a transient
# failure is re-checked on the next probe instead of wedging the pod NotReady.
_embedder_ok: bool = False


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


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""
    configure_logging()
    # Hosted JSON logging is credential-redacting. Keep this after the legacy logging
    # setup so it replaces that formatter, and pair it with the launcher's log_config=None
    # so Uvicorn cannot replace it again after app construction.
    from engraphis.observability import configure_structured_logging
    configure_structured_logging()

    # The legacy reference entrypoint is still packaged and callable, so it must honor
    # the same commercial role boundary as the primary dashboard entrypoint. Without
    # this dispatch a customer-mode process mounted the Polar webhook, signer-backed
    # issuance routes, and vendor-admin revocation surface merely because an operator
    # launched ``engraphis-server`` instead of ``engraphis-dashboard``.
    from engraphis.commercial import service_mode
    mode = service_mode()
    if mode == "vendor":
        from engraphis.vendor_app import create_app as create_vendor_app
        return create_vendor_app()

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

    # Optional bearer-token auth. Active only when ENGRAPHIS_API_TOKEN is set.
    # Health-type probes (liveness + readiness) stay unauthenticated by convention.
    # /webhooks/polar is server-to-server (Polar signs it with POLAR_WEBHOOK_SECRET,
    # verified in engraphis.billing) — it can't carry a bearer token, so it must be
    # exempt from ENGRAPHIS_API_TOKEN auth and from rate limiting.
    _PUBLIC_PREFIXES = ("/memory/health", "/api/health", "/api/ready",
                        "/openapi.json", "/static",
                        "/webhooks/polar")

    @app.middleware("http")
    async def _require_token(request: Request, call_next):
        token = settings.api_token
        if token and request.method != "OPTIONS" and request.url.path != "/" \
                and not request.url.path.startswith(_PUBLIC_PREFIXES) \
                and not request.url.path.startswith(CLOUD_PREFIXES):
            if not bearer_ok(request.headers.get("authorization"), token):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
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
            if request.method == "OPTIONS" or request.url.path.startswith(_PUBLIC_PREFIXES):
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
    # Purchase fulfillment (Polar order.paid → signed key → email). Shared with the
    # Inspector so it works regardless of which entrypoint is deployed.
    if settings.vendor_service:
        from engraphis.billing import router as billing_router
        app.include_router(billing_router)
    # Cloud license (register/verify/REVOKE) + gated Pro sync relay. Previously
    # mounted only on the retired Inspector, which made revocation inoperable in
    # production; now served by every shipped entrypoint. See inspector.cloud_mount.
    mount_cloud_endpoints(
        app, include_license=settings.vendor_service,
        include_sync=settings.customer_service)
    # Customer mode preserves the pre-split URL only as a bounded compatibility proxy;
    # it never mounts the local signer/control-plane implementation.
    from engraphis.inspector.license_compat_proxy import mount_license_compat_proxy
    mount_license_compat_proxy(app)

    # ── probes (unauthenticated; see _PUBLIC_PREFIXES) ──────────────────────────
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


app = create_app()
