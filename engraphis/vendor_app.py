"""Minimal ASGI entrypoint for the isolated Engraphis commercial control plane."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from engraphis import __version__, http_security
from engraphis.commercial import (
    service_mode,
    vendor_admin_token_ready,
    vendor_readiness,
    vendor_serving_readiness,
)
from engraphis.inspector.auth import bearer_ok

logger = logging.getLogger(__name__)
EMAIL_WORKER_INTERVAL_SECONDS = 30


def _admin_ok(request: Request) -> bool:
    expected = os.environ.get("ENGRAPHIS_VENDOR_ADMIN_TOKEN", "").strip()
    return vendor_admin_token_ready() \
        and bearer_ok(request.headers.get("Authorization"), expected)


def _email_worker_ok(app: FastAPI) -> bool:
    task = getattr(app.state, "email_worker", None)
    return task is not None and not task.done()


def create_app() -> FastAPI:
    from engraphis.observability import configure_structured_logging
    configure_structured_logging()
    if service_mode() != "vendor":
        raise RuntimeError("the vendor app requires ENGRAPHIS_SERVICE_MODE=vendor")

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        from engraphis import email_outbox
        from engraphis.inspector.webhooks import _deliver_text_email

        stop = asyncio.Event()
        application.state.email_worker_stop = stop

        async def run():
            while not stop.is_set():
                try:
                    await asyncio.to_thread(
                        email_outbox.process_due, _deliver_text_email, limit=20)
                    application.state.email_worker_last_error = ""
                except Exception as exc:
                    application.state.email_worker_last_error = type(exc).__name__[:80]
                    application.state.email_worker_last_failure_at = time.time()
                    # Provider exceptions can embed request URLs, recipients, or response
                    # bodies. Keep only the class name at this log boundary.
                    logger.error(
                        "commercial email worker iteration failed (%s)",
                        type(exc).__name__)
                try:
                    await asyncio.wait_for(
                        stop.wait(), timeout=EMAIL_WORKER_INTERVAL_SECONDS)
                except asyncio.TimeoutError:
                    pass

        task = asyncio.create_task(run())
        application.state.email_worker = task
        try:
            yield
        finally:
            stop.set()
            await task

    app = FastAPI(title="Engraphis License Service", docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)

    from engraphis.billing import router as billing_router
    from engraphis.resend_events import router as resend_router
    from engraphis.inspector.cloud_mount import mount_cloud_endpoints
    app.include_router(billing_router)
    app.include_router(resend_router)
    mount_cloud_endpoints(app, include_license=True, include_sync=False)

    @app.get("/api/health")
    def health():
        return {"status": "ok", "service": "license"}

    @app.get("/api/ready")
    def ready():
        ok = bool(vendor_serving_readiness().get("ready")) and _email_worker_ok(app)
        return JSONResponse(
            {"ready": ok, "checks": {"control_plane": ok}, "version": __version__},
            status_code=200 if ok else 503)

    @app.get("/ops/ready")
    def operations_ready(request: Request):
        if not _admin_ok(request):
            return JSONResponse({"error": "vendor admin token required"}, status_code=401)
        checks = vendor_readiness()
        checks["email_worker"] = _email_worker_ok(app)
        checks["ready"] = bool(checks["ready"]) and checks["email_worker"]
        return JSONResponse(checks, status_code=200 if checks["ready"] else 503)

    @app.get("/ops/email")
    def email_operations(request: Request, limit: int = 100):
        if not _admin_ok(request):
            return JSONResponse({"error": "vendor admin token required"}, status_code=401)
        from engraphis.email_outbox import health, recent_redacted
        return {"health": health(), "messages": recent_redacted(limit)}

    @app.get("/ops/synthetic/trial")
    def synthetic_trial(request: Request):
        """Secret-free, non-mutating production check of the trial dependency chain."""
        if not _admin_ok(request):
            return JSONResponse({"error": "vendor admin token required"}, status_code=401)
        checks = vendor_readiness()
        trial_checks = {
            name: bool(checks.get(name)) for name in (
                "signer", "signer_release_ready", "registry", "email",
                "email_webhook", "email_outbox", "polar_backlog",
                "rejected_leases", "disk", "backup")
        }
        trial_checks["email_worker"] = _email_worker_ok(app)
        trial_checks["public_url"] = bool(
            os.environ.get("ENGRAPHIS_RELAY_PUBLIC_URL", "").strip())
        # Keep the public-rejection counter visible to monitoring, but do not make the
        # synthetic attacker-controlled.  ``vendor_readiness`` applies the same rule:
        # invalid lease attempts are an operator alert, not a serving dependency.
        trial_checks["ready"] = all(
            value for name, value in trial_checks.items()
            if name != "rejected_leases")
        return JSONResponse(
            trial_checks, status_code=200 if trial_checks["ready"] else 503)

    @app.post("/ops/email/retry")
    def retry_email_operations(request: Request):
        if not _admin_ok(request):
            return JSONResponse({"error": "vendor admin token required"}, status_code=401)
        from engraphis import email_outbox
        from engraphis.inspector.webhooks import _deliver_text_email
        requeued = email_outbox.requeue_failed(limit=100)
        result = email_outbox.process_due(_deliver_text_email, limit=100)
        result["requeued"] = requeued
        return result

    @app.post("/ops/backup")
    def run_backup(request: Request):
        if not _admin_ok(request):
            return JSONResponse({"error": "vendor admin token required"}, status_code=401)
        try:
            from engraphis.commercial import run_configured_backup
            result = run_configured_backup()
        except Exception as exc:  # noqa: BLE001 - never expose paths or provider detail
            logger.error("commercial backup failed (%s)", type(exc).__name__)
            return JSONResponse({"ok": False, "verified": False}, status_code=503)
        return JSONResponse(result, status_code=200 if result["verified"] else 503)

    http_security.install(app)
    return app
