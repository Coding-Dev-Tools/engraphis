"""Inspector HTTP layer — a thin FastAPI binding over :class:`MemoryService`.

Deliberately mirrors ``mcp_server.py``'s philosophy: no logic here, only transport.
All validation/authorization lives in the service (workspace binding included), so
the inspector inherits the same isolation guarantees as the MCP tools. Optional
bearer-token auth via ``ENGRAPHIS_API_TOKEN`` (same knob as the v1 server); CORS is
loopback-only by default. Responses are JSON; the single HTML page renders
everything client-side with ``textContent`` (no innerHTML on stored content — the
stored-XSS lesson from the v1 dashboard, applied from day one).

Commercial layer (docs/LAUNCH_PLAN.md §3): this file is also the *only* place the
Pro gates live — ``/api/analytics`` and ``/api/export`` call
``licensing.require_feature`` (→ HTTP 402 with an upgrade hint), and **team mode**
(``ENGRAPHIS_TEAM_MODE=1`` + a ``team`` license) switches ``/api/*`` from the
optional bearer token to real per-user sessions with server-side roles:
viewer (read) < member (+ governance) < admin (+ consolidate/users/license/export).
Free single-user behaviour is byte-for-byte unchanged when team mode is off.
"""
from __future__ import annotations

import hmac
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from engraphis import licensing
from engraphis.analytics import compute_analytics
from engraphis.config import settings
from engraphis.inspector.auth import SESSION_TTL_SECONDS, AuthError, AuthStore, role_at_least
from engraphis.licensing import LicenseError
from engraphis.service import MemoryService, ValidationError

_INDEX = Path(__file__).parent / "index.html"

COOKIE_NAME = "engraphis_session"

# Reachable without any auth in every mode: the page shell, liveness, and the auth
# bootstrap endpoints themselves (state/login/setup must work while logged out).
_PUBLIC = {"/", "/api/health", "/api/auth/state", "/api/auth/login", "/api/auth/setup"}


def _min_role(method: str, path: str) -> str:
    """Least role allowed to touch ``path`` in team mode. Server-side is the source of
    truth — the UI merely hides what this table already refuses."""
    if path.startswith("/api/auth/users") or path in (
            "/api/license/activate", "/api/export", "/api/consolidate"):
        return "admin"
    if method == "POST":            # pin / forget / correct — audited governance
        return "member"
    return "viewer"


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
        return bool(settings.team_mode) and licensing.has_feature("team")

    def _bearer_ok(request: Request) -> bool:
        token = settings.api_token
        if not token:
            return False
        supplied = (request.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        return bool(supplied) and hmac.compare_digest(supplied, token)

    @app.middleware("http")
    async def _auth_gate(request: Request, call_next):
        path = request.url.path
        if not path.startswith("/api/") or path in _PUBLIC:
            return await call_next(request)
        if team_active():
            user = auth().resolve_session(request.cookies.get(COOKIE_NAME, ""))
            if user is None and _bearer_ok(request):
                # Service-account escape hatch so existing scripts keep working.
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
        # Single-user modes: optional bearer token, exactly as before team mode existed.
        if settings.api_token and not _bearer_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)

    @app.exception_handler(ValidationError)
    async def _validation(request: Request, exc: ValidationError):
        return JSONResponse({"error": str(exc)}, status_code=400)

    @app.exception_handler(LicenseError)
    async def _license(request: Request, exc: LicenseError):
        return JSONResponse({"error": str(exc), "upgrade": True,
                             "purchase_url": licensing.PURCHASE_URL}, status_code=402)

    @app.exception_handler(AuthError)
    async def _autherr(request: Request, exc: AuthError):
        return JSONResponse({"error": str(exc)}, status_code=400)

    def _actor(request: Request) -> str:
        """Audit attribution: the signed-in user's email in team mode, else a stable
        surface tag. This is what makes the Team tier's audit trail answer *who*."""
        user = getattr(request.state, "user", None)
        return (user or {}).get("email") or "inspector"

    # ── page ────────────────────────────────────────────────────────────────
    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(_INDEX, media_type="text/html")

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
        return {
            "mode": mode,
            "setup_required": bool(team and auth().count_users() == 0),
            "user": user,
            "license": lic.to_public_dict(),
            "license_error": licensing.license_error(),
            # env asked for team mode but the license lacks it → UI shows the unlock path
            "team_locked": bool(settings.team_mode) and not licensing.has_feature("team"),
        }

    def _login_response(user: dict) -> JSONResponse:
        resp = JSONResponse({"user": {"email": user["email"], "name": user["name"],
                                      "role": user["role"]}})
        resp.set_cookie(COOKIE_NAME, user["token"], max_age=SESSION_TTL_SECONDS,
                        httponly=True, samesite="strict", path="/")
        return resp

    @app.post("/api/auth/setup")
    async def auth_setup(body: _SetupBody):
        if not team_active():
            raise LicenseError("team mode is not active on this instance")
        if auth().count_users() > 0:
            return JSONResponse({"error": "setup already completed"}, status_code=409)
        auth().create_user(body.email, body.name, body.password, "admin")
        user = auth().login(body.email, body.password)
        return _login_response(user)

    @app.post("/api/auth/login")
    async def auth_login(body: _LoginBody):
        if not team_active():
            return JSONResponse({"error": "team mode is not active"}, status_code=400)
        try:
            user = auth().login(body.email, body.password)
        except AuthError as exc:
            status = 429 if "too many" in str(exc) else 401
            return JSONResponse({"error": str(exc)}, status_code=status)
        return _login_response(user)

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
                                           disabled=body.disabled)}

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

    # ── Pro: analytics & compliance export (the 402 upgrade path) ───────────
    @app.get("/api/analytics")
    async def analytics(workspace: str):
        licensing.require_feature("analytics")
        wid, _ = svc()._require_scope(workspace, None)
        return compute_analytics(svc().store, wid)

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

    @app.post("/api/consolidate")
    async def consolidate(body: _ConsolidateBody):
        return svc().consolidate(workspace=body.workspace, repo=body.repo,
                                 dry_run=body.dry_run, min_cluster=body.min_cluster,
                                 archive_below=body.archive_below)

    return app
