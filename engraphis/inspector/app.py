"""Inspector HTTP layer — a thin FastAPI binding over :class:`MemoryService`.

Deliberately mirrors ``mcp_server.py``'s philosophy: no logic here, only transport.
All validation/authorization lives in the service (workspace binding included), so
the inspector inherits the same isolation guarantees as the MCP tools. Optional
bearer-token auth via ``ENGRAPHIS_API_TOKEN`` (same knob as the v1 server); CORS is
loopback-only by default. Responses are JSON: the standalone HTML UI was retired
2026-07-10 (folded into the unified dashboard on :8700), so this layer no longer
serves an HTML page — it is an internal JSON API surface, exercised by the tests and
reused by billing/webhooks and the dashboard's shared auth.

Commercial layer: this file is also the *only* place the
Pro gates live — ``/api/analytics`` and ``/api/export`` call
``licensing.require_feature`` (→ HTTP 402 with an upgrade hint), and **team mode**
(``ENGRAPHIS_TEAM_MODE=1`` + a ``team`` license) switches ``/api/*`` from the
optional bearer token to real per-user sessions with server-side roles:
viewer (read) < member (+ governance) < admin (+ consolidate/users/license/export).
Free single-user behaviour is byte-for-byte unchanged when team mode is off.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from engraphis import __version__, http_security, licensing
from engraphis.analytics import compute_analytics, render_analytics_html
from engraphis.billing import router as billing_router
from engraphis.inspector.sync_relay import router as sync_relay_router
from engraphis.inspector.license_cloud import router as license_cloud_router
from engraphis.config import settings
from engraphis.inspector.auth import (
    SESSION_TTL_SECONDS, AccountLockedError, AuthError, AuthStore,
    SetupAlreadyCompletedError, bearer_ok, min_role as _min_role, role_at_least,
)
from engraphis.licensing import LicenseError
from engraphis.logging_setup import configure_logging
from engraphis.netutil import client_ip, is_local_request
from engraphis.service import MemoryService, ValidationError

logger = logging.getLogger("engraphis")

COOKIE_NAME = "engraphis_session"

# Reachable without any auth in every mode: the page shell, liveness/readiness, and
# the auth bootstrap endpoints themselves (state/login/setup must work while logged out).
_PUBLIC = {"/", "/api/health", "/api/ready", "/api/auth/state", "/api/auth/login",
           "/api/auth/setup", "/api/auth/invitations/accept", "/webhooks/polar"}


# _min_role is now engraphis.inspector.auth.min_role (imported above as _min_role) —
# shared with dashboard_app.py so the policy can't drift between the two apps.


class _CorrectBody(BaseModel):
    memory_id: str = Field(min_length=1, max_length=200)
    new_content: str = Field(min_length=1, max_length=100_000)
    workspace: str = Field(min_length=1, max_length=200)
    repo: Optional[str] = Field(default=None, max_length=200)
    reason: str = Field(default="", max_length=1_000)


class _GovernBody(BaseModel):
    memory_id: str = Field(min_length=1, max_length=200)
    workspace: str = Field(min_length=1, max_length=200)
    repo: Optional[str] = Field(default=None, max_length=200)
    reason: str = Field(default="", max_length=1_000)
    pinned: bool = True


class _PromoteBody(BaseModel):
    memory_id: str = Field(min_length=1, max_length=200)
    target_scope: str
    workspace: str = Field(min_length=1, max_length=200)
    repo: Optional[str] = Field(default=None, max_length=200)
    reason: str = Field(default="", max_length=1_000)


class _ConsolidateBody(BaseModel):
    workspace: str = Field(min_length=1, max_length=200)
    repo: Optional[str] = Field(default=None, max_length=200)
    dry_run: bool = True
    min_cluster: int = Field(default=3, ge=2, le=20)
    archive_below: float = Field(default=0.05, ge=0.0, le=0.5)


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


def create_app(service: Optional[MemoryService] = None,
               auth_store: Optional[AuthStore] = None) -> FastAPI:
    configure_logging()
    from engraphis.commercial import service_mode
    mode = service_mode()
    if mode == "vendor":
        from engraphis.vendor_app import create_app as create_vendor_app
        return create_vendor_app()
    app = FastAPI(title="Engraphis Memory Inspector", docs_url=None, redoc_url=None)
    app.state.service = service
    app.state.auth_store = auth_store

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins or ["http://127.0.0.1:8710",
                                                "http://localhost:8710"],
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
        allow_credentials=True,
    )

    def svc() -> MemoryService:
        if app.state.service is None:
            app.state.service = MemoryService.create(
                settings.db_path,
                embed_model=settings.embed_model or None,
                allowed_workspaces=settings.allowed_workspaces,
                extractor=settings.extractor,
            )
        return app.state.service

    def auth() -> AuthStore:
        if app.state.auth_store is None:
            app.state.auth_store = AuthStore(_users_db_path(settings.db_path))
        return app.state.auth_store

    def team_active() -> bool:
        # A live Team entitlement enables first-time setup. Once any users exist, the
        # authentication wall is permanent even if the entitlement lapses; otherwise an
        # expired key would silently turn a private Inspector into an open API. Feature
        # routes still enforce the current license independently.
        return bool(settings.team_mode) and (
            auth().count_users() > 0 or licensing.has_feature("team"))

    def _bearer_ok(request: Request) -> bool:
        return bearer_ok(request.headers.get("Authorization"), settings.api_token)

    def _bearer_token(request: Request) -> str:
        header = request.headers.get("Authorization") or ""
        return header[7:].strip() if header[:7].lower() == "bearer " else ""

    @app.middleware("http")
    async def _auth_gate(request: Request, call_next):
        from engraphis.service import set_current_user

        # Clear any identity inherited by this request context before deciding who is
        # calling. Public, anonymous, and single-user requests must never retain the
        # Team user bound while handling an earlier request.
        set_current_user(None)
        path = request.url.path
        if not path.startswith("/api/") or path in _PUBLIC:
            return await call_next(request)
        if team_active():
            # Per-user API tokens authenticate headless clients exactly like browser
            # sessions. Resolve them first, then fall back to the session cookie.
            supplied = _bearer_token(request)
            user = auth().resolve_api_token(supplied) if supplied else None
            if user is not None and "agent" not in set(user.get("token_scopes") or ()):
                return JSONResponse({"error": "token lacks agent scope"}, status_code=403)
            if user is None:
                user = auth().resolve_session(request.cookies.get(COOKIE_NAME, ""))
            if user is None and _bearer_ok(request):
                # Keep the deployment token as an admin service account, but bind a
                # synthetic identity so personal-folder ownership still fails closed.
                user = {"id": "service-token", "email": "service-token", "role": "admin"}
                request.state.user = user
                set_current_user(user)
                return await call_next(request)
            if user is None:
                return JSONResponse({"error": "authentication required", "auth": "team"},
                                    status_code=401)
            need = _min_role(request.method, path)
            if not role_at_least(user["role"], need):
                return JSONResponse({"error": "requires the %s role" % need},
                                    status_code=403)
            request.state.user = user
            set_current_user(user)
            return await call_next(request)
        # Single-user modes: optional bearer token, exactly as before team mode existed.
        if settings.api_token and not _bearer_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)

    @app.exception_handler(ValidationError)
    async def _validation(request: Request, exc: ValidationError):
        return JSONResponse({"error": str(exc)}, status_code=400)

    @app.exception_handler(LicenseError)
    async def _license(request: Request, exc: LicenseError):
        """Every 402 in the product goes through here — the one place upgrade UX
        is shaped. Feature gates (require_feature) carry the feature name, so the
        client gets a structured payload instead of a bare error string."""
        body = {"error": str(exc), "upgrade": True,
                "upgrade_url": licensing.upgrade_url(),
                # legacy alias kept for older UI builds / scripts
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
        """Last-resort catch-all: without this, an unhandled exception falls through
        to Starlette's default handler, which returns a bare text/plain "Internal
        Server Error" body. The frontend's api() helper does res.json() on every
        response, so that plaintext body fails to parse and surfaces as the opaque
        "Error: bad response" -- hiding the real cause from both the user and
        whoever's debugging it. Log the exception type server-side without copying
        potentially sensitive exception text, and return a sanitized JSON message
        client-side so the failure remains visible and structured."""
        path_ref = hashlib.sha256(
            request.url.path.encode("utf-8", "replace")).hexdigest()[:12]
        logger.error("unhandled exception on %s path_ref=%s (%s)", request.method,
                     path_ref, type(exc).__name__)
        return JSONResponse({"error": "internal error -- see server logs"}, status_code=500)

    def _actor(request: Request) -> str:
        """Audit attribution: the signed-in user's email in team mode, else a stable
        surface tag. This is what makes the Team tier's audit trail answer *who*."""
        user = getattr(request.state, "user", None)
        return (user or {}).get("email") or "inspector"

    # ── page ────────────────────────────────────────────────────────────────
    # GET "/" is intentionally unrouted — the standalone HTML UI was retired 2026-07-10
    # (folded into the :8700 dashboard). This layer serves only JSON /api/* endpoints,
    # so no separately maintained SPA can drift or reintroduce a stored-XSS surface.

    # The /vendor/force-graph.min.js route existed only for the retired UI's Graph
    # tab, so it is gone too. (The dashboard serves its own libs from /static/vendor/.)

    # ── auth & licensing ────────────────────────────────────────────────────
    @app.get("/api/auth/state")
    async def auth_state(request: Request):
        team = team_active()
        mode = "team" if team else ("token" if settings.api_token else "open")
        user = None
        if team:
            user = auth().resolve_session(request.cookies.get(COOKIE_NAME, ""))
            if user:
                user = {"email": user["email"], "name": user["name"],
                        "role": user["role"]}
        lic = licensing.current_license()
        body = {
            "mode": mode,
            "setup_required": bool(team and auth().count_users() == 0),
            "user": user,
            "license": lic.to_public_dict(),
            "license_error": licensing.license_error(),
            # env asked for team mode but the license lacks it → UI shows the unlock path
            "team_locked": bool(settings.team_mode) and not licensing.has_feature("team"),
        }
        # Never let a browser cache the boot-state probe — a stale "mode:team"
        # here would make the fixed UI re-render the old login overlay.
        return JSONResponse(body, headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache", "Expires": "0",
        })

    def _login_response(user: dict, request: Request) -> JSONResponse:
        resp = JSONResponse({"user": {"email": user["email"], "name": user["name"],
                                      "role": user["role"]}})
        resp.set_cookie(COOKIE_NAME, user["token"], max_age=SESSION_TTL_SECONDS,
                        httponly=True, samesite="strict", path="/",
                        secure=http_security.wants_https(request))
        return resp

    @app.post("/api/auth/setup")
    async def auth_setup(body: _SetupBody, request: Request):
        # create_user/login run PBKDF2 (600k iterations, CPU-bound) — off the event loop
        # via to_thread, or every concurrent request stalls for the hash duration.
        if not team_active():
            raise LicenseError("team mode is not active on this instance")
        if auth().count_users() > 0:
            return JSONResponse({"error": "setup already completed"}, status_code=409)
        if not is_local_request(request) and not bearer_ok(
                request.headers.get("Authorization"), settings.api_token):
            return JSONResponse(
                {"error": "deployment API token required for remote setup"},
                status_code=401,
            )
        try:
            await asyncio.to_thread(
                auth().create_user, body.email, body.name, body.password, "admin",
                require_empty=True,
            )
        except SetupAlreadyCompletedError:
            return JSONResponse({"error": "setup already completed"}, status_code=409)
        user = await asyncio.to_thread(auth().login, body.email, body.password)
        return _login_response(user, request)

    @app.post("/api/auth/login")
    async def auth_login(body: _LoginBody, request: Request):
        if not team_active():
            return JSONResponse({"error": "team mode is not active"}, status_code=400)
        ip = client_ip(request)
        try:
            # PBKDF2 is CPU-bound; to_thread keeps the event loop free (see auth_setup).
            user = await asyncio.to_thread(
                auth().login, body.email, body.password, ip=ip)
        except AccountLockedError as exc:
            # Report the actual remaining window; never let HTTP metadata drift from
            # throttle configuration or elapsed time.
            return JSONResponse({"error": str(exc)}, status_code=429,
                                headers={"Retry-After": str(exc.retry_after)})
        except AuthError as exc:
            return JSONResponse({"error": str(exc)}, status_code=401)
        return _login_response(user, request)

    @app.post("/api/auth/logout")
    async def auth_logout(request: Request):
        token = request.cookies.get(COOKIE_NAME, "")
        if token:
            auth().revoke_session(token)
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE_NAME, path="/")
        return resp

    @app.get("/api/auth/users")
    async def users_list():
        return {"users": auth().list_users(),
                "seats": licensing.current_license().seats,
                "active": auth().count_active_users()}

    @app.post("/api/auth/users")
    async def users_create(body: _UserCreateBody):
        user = auth().create_user(body.email, body.name, body.password, body.role,
                                  seat_limit=licensing.current_license().seats)
        return {"user": user}

    @app.post("/api/auth/users/update")
    async def users_update(body: _UserUpdateBody):
        return {"user": auth().update_user(body.user_id, role=body.role,
                                           disabled=body.disabled,
                                           seat_limit=licensing.current_license().seats)}

    @app.get("/api/license")
    async def license_state():
        return {"license": licensing.current_license().to_public_dict(),
                "license_error": licensing.license_error()}

    @app.post("/api/license/activate")
    async def license_activate(body: _ActivateBody):
        lic = licensing.activate(body.key)   # LicenseError → 402 with the reason
        return {"license": lic.to_public_dict(), "activated": True}

    # ── read ────────────────────────────────────────────────────────────────
    @app.get("/api/health")
    async def health():
        return {"status": "ok", "service": "engraphis-inspector"}

    @app.get("/api/ready")
    async def ready():
        """Readiness (health is liveness-only): the service builds — which
        initializes the embedder backend — and the DB answers a trivial SELECT.
        503 until both hold."""
        checks = {"db": False, "embedder": False}
        try:
            s = svc()
            s.store.conn.execute("SELECT 1").fetchone()
            checks["db"] = True
            checks["embedder"] = getattr(s.engine, "embedder", None) is not None
        except Exception:
            pass
        is_ready = all(checks.values())
        return JSONResponse({"ready": is_ready, "checks": checks, "version": __version__},
                            status_code=200 if is_ready else 503)

    @app.get("/api/workspaces")
    async def workspaces():
        return svc().list_workspaces()

    @app.get("/api/stats")
    async def stats(workspace: Optional[str] = None):
        return svc().stats(workspace=workspace)

    @app.get("/api/recall")
    async def recall(q: str, workspace: str, repo: Optional[str] = None, k: int = 12):
        return svc().recall(q, workspace=workspace, repo=repo, k=k, reinforce=False)

    @app.get("/api/why")
    async def why(q: str, workspace: str, repo: Optional[str] = None, k: int = 5):
        return svc().why(q, workspace=workspace, repo=repo, k=k)

    @app.get("/api/timeline")
    async def timeline(q: str, workspace: str, repo: Optional[str] = None, limit: int = 20):
        return svc().timeline(q, workspace=workspace, repo=repo, limit=limit)

    @app.get("/api/proactive")
    async def proactive(workspace: str, repo: Optional[str] = None, k: int = 10):
        return svc().recall_proactive(workspace=workspace, repo=repo, k=k)

    @app.get("/api/memory/{memory_id}")
    async def memory(memory_id: str, workspace: str, repo: Optional[str] = None):
        return svc().inspect(memory_id, workspace=workspace, repo=repo)

    @app.get("/api/audit")
    async def audit_log(workspace: str, limit: int = 100):
        return svc().audit_log(workspace=workspace, limit=limit)

    @app.get("/api/receipts")
    async def receipts(workspace: str, limit: int = 100):
        return svc().receipt_log(workspace=workspace, limit=limit)

    @app.get("/api/receipts/verify")
    async def receipts_verify(workspace: str):
        return svc().verify_receipts(workspace=workspace)

    @app.get("/api/graph")
    async def graph(workspace: str, limit: int = 2000,
                    layers: Optional[str] = None, include_code: bool = False,
                    repo: Optional[str] = None):
        """Entity-relation network for the Graph tab -- same
        :meth:`MemoryService.graph` the v1-look dashboard's ``/api/graph`` calls
        (engraphis/graphdata.py), so both UIs render identical graphs and share
        the same workspace-binding isolation guard."""
        selected = None if layers is None else [
            x.strip() for x in layers.split(",") if x.strip()
        ]
        return svc().graph(
            workspace=workspace, limit=limit, layers=selected,
            include_code=include_code, repo=repo, backfill=False,
        )

    # ── Pro: analytics & compliance export (the 402 upgrade path) ───────────
    @app.get("/api/analytics")
    async def analytics(workspace: str):
        licensing.require_feature("analytics")
        wid, _ = svc()._require_scope(workspace, None)
        return compute_analytics(svc().store, wid)

    @app.get("/api/analytics/export")
    async def analytics_export(workspace: str):
        """Self-contained HTML analytics report (inline CSS, no CDN) — same Pro gate
        as the analytics dashboard it renders; a shareable artifact is the point."""
        licensing.require_feature("analytics")
        wid, _ = svc()._require_scope(workspace, None)
        page = render_analytics_html(compute_analytics(svc().store, wid),
                                     workspace=workspace, version=__version__)
        fname = "engraphis-analytics-%s-%s.html" % (
            workspace.replace("/", "_"), time.strftime("%Y%m%d"))
        return HTMLResponse(page, headers={
            "Content-Disposition": 'attachment; filename="%s"' % fname})

    @app.get("/api/export")
    async def export(workspace: str):
        licensing.require_feature("export")
        data = svc().export_workspace(workspace=workspace)
        fname = "engraphis-export-%s-%s.json" % (
            workspace.replace("/", "_"), time.strftime("%Y%m%d"))
        return JSONResponse(data, headers={
            "Content-Disposition": 'attachment; filename="%s"' % fname})

    # ── governance (audited; never a hard delete) ───────────────────────────
    @app.post("/api/pin")
    async def pin(body: _GovernBody, request: Request):
        return svc().pin(body.memory_id, workspace=body.workspace, repo=body.repo,
                         pinned=body.pinned, actor=_actor(request))

    @app.post("/api/forget")
    async def forget(body: _GovernBody, request: Request):
        return svc().forget(body.memory_id, workspace=body.workspace, repo=body.repo,
                            reason=body.reason, actor=_actor(request))

    @app.post("/api/correct")
    async def correct(body: _CorrectBody, request: Request):
        return svc().correct(body.memory_id, body.new_content, workspace=body.workspace,
                             repo=body.repo, reason=body.reason, actor=_actor(request))

    @app.post("/api/promote")
    async def promote(body: _PromoteBody, request: Request):
        return svc().promote(
            body.memory_id, body.target_scope, workspace=body.workspace,
            repo=body.repo, reason=body.reason, actor=_actor(request),
        )

    @app.post("/api/consolidate")
    async def consolidate(body: _ConsolidateBody):
        return svc().consolidate(workspace=body.workspace, repo=body.repo,
                                 dry_run=body.dry_run, min_cluster=body.min_cluster,
                                 archive_below=body.archive_below)

    # ── Polar webhook: auto-fulfill license keys on purchase ────────────────
    # Route lives in engraphis.billing so the public server (engraphis/app.py) and
    # this Inspector serve identical fulfillment logic — no drift between the two.
    if settings.vendor_service:
        app.include_router(billing_router)
        app.include_router(license_cloud_router)
    if settings.customer_service:
        app.include_router(sync_relay_router)
    from engraphis.inspector.license_compat_proxy import mount_license_compat_proxy
    mount_license_compat_proxy(app)

    # Baseline security response headers — see engraphis.http_security. Registered last
    # so it wraps the auth middleware above and also covers its 401/402 short-circuits.
    http_security.install(app)

    return app
