"""Minimal ASGI entrypoint for the isolated managed sync data plane.

The relay is intentionally not a dashboard deployment.  It exposes bundle transport,
liveness/readiness, and the time-bounded legacy license proxy only; mounting the memory
API, account setup, billing, or license issuance here would collapse trust domains and
let public traffic provision or interfere with the shared data plane.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from engraphis import __version__, http_security
from engraphis.commercial import managed_relay_verifier_readiness, service_mode


def create_app() -> FastAPI:
    """Build the public relay-only application and fail closed in any other mode."""
    from engraphis.observability import configure_structured_logging

    configure_structured_logging()
    if service_mode() != "relay":
        raise RuntimeError("the managed relay app requires ENGRAPHIS_SERVICE_MODE=relay")

    app = FastAPI(
        title="Engraphis Managed Relay",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    from engraphis.inspector.cloud_mount import mount_cloud_endpoints
    from engraphis.inspector.license_compat_proxy import mount_license_compat_proxy

    mount_cloud_endpoints(app, include_license=False, include_sync=True)
    mount_license_compat_proxy(app)

    @app.get("/api/health")
    def health():
        return {"status": "ok", "service": "relay"}

    @app.get("/api/ready")
    def ready():
        checks = managed_relay_verifier_readiness()
        ok = bool(checks.get("ready"))
        return JSONResponse(
            {"ready": ok, "checks": checks, "version": __version__},
            status_code=200 if ok else 503,
        )

    http_security.install(app)
    return app

