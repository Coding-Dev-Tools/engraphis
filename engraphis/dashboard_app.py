"""Unified Engraphis WebUI — Memory Inspector + legacy dashboard on one port.

Serves the Memory Inspector (v2 product UI) at ``/`` and the legacy dashboard at
``/legacy``, both over the same :class:`MemoryService` instance and the same
``/api/*`` route set. Port :8710 redirects to :8700.
"""
from __future__ import annotations

import hmac
import json
import time
from pathlib import Path
from typing import Optional

import os as _os
_os.environ["ENGRAPHIS_EMBED_MODEL"] = (
    _os.environ.get("ENGRAPHIS_EMBED_MODEL", "").strip()
    or "sentence-transformers/all-MiniLM-L6-v2")

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from engraphis import __version__, licensing
from engraphis.analytics import compute_analytics, render_analytics_html
from engraphis.billing import router as billing_router
from engraphis.config import settings
from engraphis.inspector.auth import (
    SESSION_TTL_SECONDS, AuthError, AuthStore, min_role as _min_role, role_at_least,
)
from engraphis.licensing import LicenseError
from engraphis.logging_setup import configure_logging
from engraphis.routes import v2_api
from engraphis.service import MemoryService, ValidationError

import logging
configure_logging()
logger = logging.getLogger("engraphis")

_STATIC = Path(__file__).resolve().parent / "static"
_INDEX_DASHBOARD = _STATIC / "index.html"
_INDEX_INSPECTOR = Path(__file__).resolve().parent / "inspector" / "index.html"
_INSPECTOR_VENDOR = Path(__file__).resolve().parent / "inspector" / "vendor"

COOKIE_NAME = "engraphis_session"

_PUBLIC = {"/", "/legacy", "/api/health", "/api/ready",
           "/api/auth/state", "/api/auth/login", "/api/auth/setup",
           "/api/auth/logout", "/api/auth/users", "/api/auth/users/update",
           "/webhooks/polar"}


class _LoginBody(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=1_000)


class _SetupBody(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    name: str = Field(default="", max_length=120)
    password: str = Field(min_length=1, max_length=1_000)


class _UserCreateBody(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    name: str = Field(default="", max_length=120)
    password: str = Field(min_length=1, max_length=1_000)
    role: str = Field(default="member", max_length=20)


class _UserUpdateBody(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    role: Optional[str] = Field(default=None, max_length=20)
    disabled: Optional[bool] = None


class _ActivateBody(BaseModel):
    key: str = Field(min_length=1, max_length=10_000)


def _users_db_path(db_path: str) -> str:
    return ":memory:" if db_path == ":memory:" else db_path + ".users.db"


def create_app() -> FastAPI:
    app = FastAPI(title="Engraphis", docs_url="/api/docs", openapi_url="/api/openapi.json")

    # ── coreservice (lazy, shared by all routes) ──────────────────────────
    svc_inst = MemoryService.create(
        settings.db_path, embed_model=settings.embed_model,
        embed_dim=settings.embed_dim or 256,
        allowed_workspaces=settings.allowed_workspaces)
    v2_api.set_service(svc_inst)
    app.include_router(v2_api.router)

    try:
        import sys as _sys
        ed = svc_inst.engine.embedder
        ok = getattr(ed, "dim", 0) >= 384
        print("[engraphis] embedder: %s dim=%s %s" % (
            type(ed).__name__, getattr(ed, "dim", "?"),
            "(semantic search ready)" if ok else
            "(deterministic fallback)"), file=_sys.stderr)
    except Exception:
        pass

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins or ["http://127.0.0.1:8700",
                                                "http://localhost:8700"],
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
        allow_credentials=True,
    )

    # ── team auth state ────────────────────────────────────────────────────
    team_enabled = False
    team_auth_store = None
    try:
        from engraphis.routes import v2_team
        team_enabled, team_auth_store = v2_team.attach(app, svc_inst)
    except Exception:
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
        if team_enabled and licensing.has_feature("team"):
            auth_store = team_auth_store or getattr(app.state, "auth_store", None)
            if auth_store is None:
                auth_store = AuthStore(_users_db_path(settings.db_path))
                app.state.auth_store = auth_store
            user = auth_store.resolve_session(
                request.cookies.get(COOKIE_NAME, "")
                or request.cookies.get("engr_dash_session", ""))
            if user is None and _bearer_ok(request):
                user = {"id": "service-token", "email": "service-token", "role": "admin"}
            if user is None:
                return JSONResponse({"error": "authentication required", "auth": "team"},
                                    status_code=401)
            need = _min_role(request.method, path)
            if not role_at_least(user["role"], need):
                return JSONResponse({"error": "requires the %s role" % need},
                                    status_code=403)
            request.state.user = user
            return await call_next(request)
        if settings.api_token and not _bearer_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)

    # ── exception handlers ──────────────────────────────────────────────────
    @app.exception_handler(ValidationError)
    async def _validation(request: Request, exc: ValidationError):
        return JSONResponse({"error": str(exc)}, status_code=400)

    @app.exception_handler(LicenseError)
    async def _license(request: Request, exc: LicenseError):
        body = {"error": str(exc), "upgrade": True,
                "upgrade_url": licensing.upgrade_url(),
                "purchase_url": licensing.upgrade_url()}
        feature = getattr(exc, "feature", None)
        if feature:
            body["feature"] = feature
            body["tier_required"] = licensing.required_plan(feature)
        return JSONResponse(body, status_code=402)

    @app.exception_handler(AuthError)
    async def _autherr(request: Request, exc: AuthError):
        return JSONResponse({"error": str(exc)}, status_code=400)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        logger.exception("unhandled on %s %s", request.method, request.url.path)
        return JSONResponse({"error": "internal error -- see server logs"}, status_code=500)

    # ── pages ───────────────────────────────────────────────────────────────
    @app.get("/", include_in_schema=False)
    async def index():
        resp = FileResponse(_INDEX_INSPECTOR, media_type="text/html")
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return resp

    @app.get("/legacy", include_in_schema=False)
    async def legacy():
        resp = FileResponse(_INDEX_DASHBOARD, media_type="text/html")
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return resp

    # ── static / vendor assets ──────────────────────────────────────────────
    if _STATIC.is_dir():
        app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

    @app.get("/vendor/force-graph.min.js", include_in_schema=False)
    async def vendor_force_graph():
        return FileResponse(_INSPECTOR_VENDOR / "force-graph.min.js",
                            media_type="text/javascript",
                            headers={"Cache-Control": "public, max-age=604800"})

    # ── auth & licensing (team auth via v2_team, license via v2_api) ───────
    @app.get("/api/ready")
    async def ready():
        checks = {"db": False, "embedder": False}
        try:
            svc_inst.store.conn.execute("SELECT 1").fetchone()
            checks["db"] = True
            checks["embedder"] = getattr(svc_inst.engine, "embedder", None) is not None
        except Exception:
            pass
        is_ready = all(checks.values())
        return JSONResponse({"ready": is_ready, "checks": checks, "version": __version__},
                            status_code=200 if is_ready else 503)

    # ── Polar webhook ──────────────────────────────────────────────────────
    app.include_router(billing_router)

    for warning in licensing.production_warnings():
        import sys
        print("[engraphis] ship-safety: %s" % warning, file=sys.stderr)

    return app


app = create_app()
