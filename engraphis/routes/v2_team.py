"""Team mode (multi-user auth) for the v2 dashboard — reuses the Inspector's AuthStore.

Enabled only when ``ENGRAPHIS_TEAM_MODE`` is truthy; otherwise ``/api/auth/state``
reports ``{"enabled": false}`` and the dashboard stays single-user. Sessions are an
HttpOnly, SameSite=Strict cookie; roles (viewer/member/admin) are enforced server-side.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field

from engraphis import licensing
from engraphis.config import settings
from engraphis.inspector.auth import (
    SESSION_TTL_SECONDS, AuthError, AuthStore, role_at_least)

logger = logging.getLogger("engraphis.team")

_COOKIE = "engr_dash_session"


def _csv_cell(value) -> str:
    """Neutralize spreadsheet formula injection (CWE-1236) in exported CSV. A cell that
    begins with =, +, -, @ (or a control char) is executed as a formula by Excel/Sheets;
    an unauthenticated failed-login attempt can seed such a value into actor_email, so we
    defuse it by prefixing a single quote. Applied to every free-text/attacker-influenced
    field in the export."""
    s = "" if value is None else str(value)
    if s and s[0] in ("=", "+", "-", "@", "\t", "\r", "\n"):
        s = "'" + s
    return s


class SetupReq(BaseModel):
    email: str = Field(..., min_length=5, max_length=254)
    name: str = Field(default="", max_length=120)
    password: str = Field(..., min_length=10, max_length=128)


class LoginReq(BaseModel):
    email: str = Field(..., min_length=5, max_length=254)
    password: str = Field(..., min_length=1, max_length=128)


class NewUserReq(BaseModel):
    email: str = Field(..., min_length=5, max_length=254)
    name: str = Field(default="", max_length=120)
    password: str = Field(..., min_length=10, max_length=128)
    role: str = Field(default="member", pattern=r'^(viewer|member|admin)$')


class UpdUserReq(BaseModel):
    user_id: str
    role: Optional[str] = Field(default=None, pattern=r'^(viewer|member|admin)$')
    disabled: Optional[bool] = None


def _enabled() -> bool:
    return os.environ.get("ENGRAPHIS_TEAM_MODE", "").lower() in {"1", "true", "yes", "on"}


def _users_db_path(db_path: str) -> str:
    """Users/sessions live in a *separate* SQLite file next to the memory DB — auth
    state is not memory state (see inspector/auth.py's module docstring): mixing the
    two would put password/session-token hashes inside the same file that
    ``/api/export`` and ordinary DB backups copy around."""
    return ":memory:" if db_path == ":memory:" else db_path + ".users.db"


def _set_cookie(response: Response, token: str, *, secure: bool = False) -> None:
    """Set the dashboard session cookie. Secure flag mirrors the Inspector pattern:
    True when the request arrived over HTTPS, so the cookie is never sent cleartext.
    TTL matches the server-side session expiry (SESSION_TTL_SECONDS) — the browser
    drops the cookie exactly when the server stops honouring it."""
    response.set_cookie(_COOKIE, token, httponly=True, samesite="strict",
                        max_age=SESSION_TTL_SECONDS, path="/", secure=secure)


def attach(app: FastAPI, service):
    """Mount /api/auth/* onto the dashboard app. Safe to call unconditionally.

    Returns ``(enabled, store)`` so the caller can wire a request-level auth gate
    over the rest of ``/api/*`` (this module only mounts the auth sub-routes;
    enforcing that a session exists on every other route is the caller's job)."""
    router = APIRouter(prefix="/api/auth", tags=["team"])

    if not _enabled():
        @router.get("/state")
        def state_off():
            return {"enabled": False, "needs_setup": False, "user": None}
        app.include_router(router)
        return False, None

    store = AuthStore(_users_db_path(settings.db_path))

    def _user(request: Request) -> Optional[dict]:
        tok = request.cookies.get(_COOKIE)
        return store.resolve_session(tok) if tok else None

    def _require(request: Request, minimum: str = "viewer") -> dict:
        u = _user(request)
        if not u:
            raise HTTPException(status_code=401, detail={"error": "authentication required"})
        if not role_at_least(u["role"], minimum):
            raise HTTPException(status_code=403, detail={"error": "insufficient role"})
        return u

    @router.get("/state")
    def state(request: Request):
        return {"enabled": True, "needs_setup": store.count_users() == 0,
                "user": _user(request)}

    @router.post("/setup")
    def setup(body: SetupReq, request: Request, response: Response):
        if store.count_users() > 0:
            raise HTTPException(status_code=400, detail={"error": "team already set up"})
        try:
            admin = store.create_user(body.email, body.name, body.password, "admin")
            store.record_event("team.setup", actor_id=admin["id"], actor_email=admin["email"],
                               detail="initial admin created")
            u = store.login(body.email, body.password,
                            ip=request.client.host if request.client else None)
        except AuthError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)})
        _set_cookie(response, u.pop("token"), secure=request.url.scheme == "https")
        return {"user": u}

    @router.post("/login")
    def login(body: LoginReq, request: Request, response: Response):
        ip = request.client.host if request.client else None
        try:
            u = store.login(body.email, body.password, ip=ip)
        except AuthError as exc:
            raise HTTPException(status_code=401, detail={"error": str(exc)})
        _set_cookie(response, u.pop("token"), secure=request.url.scheme == "https")
        return {"user": u}

    @router.post("/logout")
    def logout(request: Request, response: Response):
        tok = request.cookies.get(_COOKIE)
        if tok:
            store.revoke_session(tok)
        response.delete_cookie(_COOKIE, path="/")
        return {"ok": True}

    @router.get("/users")
    def users(request: Request):
        _require(request, "member")
        return {"users": store.list_users()}

    @router.post("/users")
    def add_user(body: NewUserReq, request: Request):
        admin = _require(request, "admin")
        seats = licensing.current_license().seats
        try:
            u = store.create_user(body.email, body.name, body.password, body.role,
                                  seat_limit=seats)
        except AuthError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)})
        ip = request.client.host if request.client else None
        store.record_event("user.created", actor_id=admin["id"], actor_email=admin["email"],
                           target=u["email"], detail="role=%s" % body.role, ip=ip)
        # Best-effort invite notification — must never block account creation, and
        # never carries the password (see send_team_invite_email's docstring). If
        # email delivery isn't configured or fails, the admin still has to share
        # the password out-of-band, so the account remains fully usable either way.
        invited = True
        try:
            from engraphis.inspector.webhooks import send_team_invite_email
            send_team_invite_email(u["email"], u["name"], u["role"])
        except Exception as exc:  # noqa: BLE001 — delivery failure must not fail the request
            invited = False
            logger.warning("team invite email to %s failed: %s", u["email"], exc)
            store.record_event("user.invite_email_failed", actor_id=admin["id"],
                               actor_email=admin["email"], target=u["email"],
                               detail=str(exc)[:200], ip=ip)
        return {"user": u, "invited": invited}

    @router.post("/users/update")
    def upd_user(body: UpdUserReq, request: Request):
        admin = _require(request, "admin")
        before = store.get_user(body.user_id)
        try:
            u = store.update_user(body.user_id, role=body.role, disabled=body.disabled,
                                  seat_limit=licensing.current_license().seats)
        except AuthError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)})
        ip = request.client.host if request.client else None
        tgt = u["email"] if u else body.user_id
        if body.role is not None and (not before or before["role"] != body.role):
            store.record_event("user.role_changed", actor_id=admin["id"],
                               actor_email=admin["email"], target=tgt,
                               detail="role=%s" % body.role, ip=ip)
        if body.disabled is not None and (not before or bool(before["disabled"]) != bool(body.disabled)):
            store.record_event("user.disabled" if body.disabled else "user.enabled",
                               actor_id=admin["id"], actor_email=admin["email"],
                               target=tgt, ip=ip)
        return {"user": u}

    @router.get("/audit")
    def audit(request: Request, limit: int = 100, action: Optional[str] = None,
              actor_id: Optional[str] = None, since: Optional[float] = None):
        """Admin-only team audit log: logins, user CRUD, role changes, seat events."""
        _require(request, "admin")
        return {"events": store.list_events(limit=limit, action=action,
                                            actor_id=actor_id, since=since),
                "total": store.count_events()}

    @router.get("/audit/export")
    def audit_export(request: Request, limit: int = 1000, since: Optional[float] = None):
        """Admin-only CSV export of the audit log for compliance/retention."""
        _require(request, "admin")
        import csv
        import datetime as _dt
        import io
        rows = store.list_events(limit=limit, since=since)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["ts", "iso_utc", "actor_id", "actor_email", "action",
                    "target", "detail", "ip"])
        for e in rows:
            iso = _dt.datetime.utcfromtimestamp(e["ts"]).replace(
                tzinfo=_dt.timezone.utc).isoformat()
            w.writerow([e["ts"], iso, _csv_cell(e.get("actor_id")),
                        _csv_cell(e.get("actor_email")), _csv_cell(e["action"]),
                        _csv_cell(e.get("target")), _csv_cell(e.get("detail")),
                        _csv_cell(e.get("ip"))])
        return Response(content=buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition":
                                 "attachment; filename=engraphis_team_audit.csv"})

    @router.get("/overview")
    def overview(request: Request):
        """Admin-only team overview: seat usage, members with last-active, activity mix."""
        _require(request, "admin")
        seats = int(licensing.current_license().seats)
        users = store.list_users()
        active = sum(1 for u in users if not u["disabled"])
        la = store.last_active()
        members = [{**u, "last_active": la.get(u["id"])} for u in users]
        return {"seats": {"used": active, "limit": seats,
                          "available": max(0, seats - active)},
                "members": members,
                "activity": store.action_counts(),
                "events_total": store.count_events()}

    app.include_router(router)
    return True, store
