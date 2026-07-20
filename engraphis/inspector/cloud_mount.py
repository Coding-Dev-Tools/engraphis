"""Shared mounting for the license-authenticated cloud endpoints.

Historically ``/license/v1`` (register / verify / **revoke**) and ``/relay/v1`` (the
gated Pro sync relay) were mounted *only* on the standalone Inspector app. When the
Inspector was retired, no shipped entrypoint served them — so key **revocation was
inoperable in production** and Pro sync had no backend. This module centralizes the
mount so ``engraphis.app`` (public server) and ``engraphis.dashboard_app`` (team
dashboard) expose identical behavior, with no drift.

These endpoints authenticate with a *license key* (Bearer) or the vendor admin token
(``ENGRAPHIS_VENDOR_ADMIN_TOKEN``; it never falls back to the instance API token) — so
callers must exempt :data:`CLOUD_PREFIXES` from any API-token middleware. They also raise
:class:`LicenseError`, which needs an app-level 402 handler;
:func:`mount_cloud_endpoints` installs one if absent.
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from engraphis import licensing
from engraphis.licensing import LicenseError

#: Path prefixes whose auth is the license key / dedicated vendor admin token, not the
#: instance API token. Any ENGRAPHIS_API_TOKEN gate must treat these as exempt (the
#: routers do their own, stronger, per-request authorization).
CLOUD_PREFIXES = ("/relay/", "/license/v1/")


def install_license_error_handler(app: FastAPI) -> None:
    """Register the single 402 handler for :class:`LicenseError` (idempotent).

    Mirrors the shape the Inspector/dashboard use so the client always gets a structured
    ``{error, upgrade, upgrade_url, feature?, tier_required?}`` payload instead of a bare
    500. Guarded by an app.state flag so double-mounting is a no-op."""
    if getattr(app.state, "_license_handler_installed", False):
        return

    @app.exception_handler(LicenseError)
    async def _license(request: Request, exc: LicenseError):  # noqa: ANN202
        if request.url.path.startswith(("/license/v1/", "/relay/v1/")):
            try:
                from engraphis.inspector import license_registry
                license_registry.record_control_plane_event("lease_rejected")
            except Exception:  # noqa: BLE001 - preserve the original safe 402 response
                pass
        body = {"error": str(exc), "upgrade": True,
                "upgrade_url": licensing.upgrade_url(),
                "purchase_url": licensing.upgrade_url()}  # legacy alias for older UIs
        feature = getattr(exc, "feature", None)
        if feature:
            body["feature"] = feature
            body["tier_required"] = licensing.required_plan(feature)
        return JSONResponse(body, status_code=402)

    app.state._license_handler_installed = True


def mount_cloud_endpoints(app: FastAPI, *, include_license: bool = True,
                          include_sync: bool = True) -> bool:
    """Mount ``/license/v1`` + ``/relay/v1`` and ensure a LicenseError→402 handler.

    Returns True if the routers were mounted. Import is done lazily and defensively so a
    minimal/core install that lacks the inspector subpackage still boots (the endpoints
    are simply absent, exactly as before)."""
    install_license_error_handler(app)
    try:
        if include_sync:
            from engraphis.inspector.sync_relay import router as sync_relay_router
        if include_license:
            from engraphis.inspector.license_cloud import router as license_cloud_router
    except Exception:  # noqa: BLE001 - cloud endpoints stay optional on minimal installs
        return False
    if include_sync:
        app.include_router(sync_relay_router)
    if include_license:
        app.include_router(license_cloud_router)
    app.state._cloud_endpoints_mounted = True
    return True
