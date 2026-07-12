"""The restored v1 dashboard — served on the *v2* engine.

Same look-and-feel as the original dashboard (engraphis/static/index.html), but every
route reads/writes the v2 MemoryService where the real data lives. This keeps the v1
server (engraphis/app.py) untouched; run this with `python -m scripts.start_dashboard`.
"""
from __future__ import annotations

import hmac
from pathlib import Path

import os as _os
_os.environ["ENGRAPHIS_EMBED_MODEL"] = (
    _os.environ.get("ENGRAPHIS_EMBED_MODEL", "").strip()
    or "sentence-transformers/all-MiniLM-L6-v2")

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
# the auth bootstrap endpoints themselves (state/login/setup must work while logged
# out) — same shape as engraphis/inspector/app.py's _PUBLIC set.
_PUBLIC = {"/", "/api/health", "/api/ready", "/api/auth/state", "/api/auth/login",
           "/api/auth/setup", "/api/auth/logout", "/webhooks/polar"}


def create_app() -> FastAPI:
    app = FastAPI(title="Engraphis Dashboard", docs_url="/api/docs", openapi_url="/api/openapi.json")
    svc = MemoryService.create(
        settings.db_path, embed_model=settings.embed_model,
        embed_dim=settings.embed_dim or 256,
        allowed_workspaces=settings.allowed_workspaces)
    try:
        import sys as _sys
        _ed = svc.engine.embedder
        _ok = getattr(_ed, "dim", 0) >= 384
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

    # Team mode (multi-user auth) — optional; attached only when the module is present
    # and a valid Team license is active, so single-user setups are unaffected.
    # ``attach`` mounts /api/auth/* AND tells us whether real per-user sessions are
    # active, so the gate below can require one for every other /api/* route —
    # without this, team mode would only protect the user-management endpoints and
    # leave recall/governance/export open to anyone who can reach the port.
    team_enabled, auth_store = False, None
    try:
        from engraphis.routes import v2_team
        team_enabled, auth_store = v2_team.attach(app, svc)
    except Exception:  # noqa: BLE001 - team stays optional
        pass

    def _bearer_ok(request: Request) -> bool:
        token = settings.api_token
        if not token:
            return False
        supplied = (request.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        return bool(supplied) and hmac.compare_digest(supplied, token)

    @app.middleware("http")
    async def _auth_gate(request: Request, call_next):
        path = request.url.path
        if not path.startswith("/api/") or path in _PUBLIC or path.startswith("/api/docs") \
                or path.startswith("/api/openapi"):
            return await call_next(request)
        # Service-account bearer token bypass — skips team auth entirely,
        # allowing CI/CD scripts and automation to use the same ENGRAPHIS_API_TOKEN
        # regardless of whether team mode is enabled.
        if settings.api_token and _bearer_ok(request):
            return await call_next(request)
        if team_enabled and auth_store is not None:
            from engraphis.inspector.auth import min_role, role_at_least
            from engraphis.routes.v2_team import _COOKIE
            user = auth_store.resolve_session(request.cookies.get(_COOKIE, ""))
            if user is None:
                return JSONResponse({"error": "authentication required", "auth": "team"},
                                    status_code=401)
            need = min_role(request.method, path)
            if not role_at_least(user["role"], need):
                return JSONResponse({"error": "requires the %s role" % need},
                                    status_code=403)
            request.state.user = user
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

    return app


app = create_app()
