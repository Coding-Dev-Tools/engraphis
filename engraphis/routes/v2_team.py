"""Team mode (multi-user auth) for the v2 dashboard — reuses the Inspector's AuthStore.

Enabled only when ``ENGRAPHIS_TEAM_MODE`` is truthy; otherwise ``/api/auth/state``
reports ``{"enabled": false}`` and the dashboard stays single-user. Sessions are an
HttpOnly, SameSite=Strict cookie; roles (viewer/member/admin) are enforced server-side.
"""
from __future__ import annotations

import os
from typing import Optional

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel

from engraphis import licensing
from engraphis.config import settings
from engraphis.inspector.auth import AuthError, AuthStore, role_at_least

_COOKIE = "engr_dash_session"


class SetupReq(BaseModel):
    email: str
    name: str = ""
    password: str


class LoginReq(BaseModel):
    email: str
    password: str


class NewUserReq(BaseModel):
    email: str
    name: str = ""
    password: str
    role: str = "member"


class UpdUserReq(BaseModel):
    user_id: str
    role: Optional[str] = None
    disabled: Optional[bool] = None


def _enabled() -> bool:
    return os.environ.get("ENGRAPHIS_TEAM_MODE", "").lower() in {"1", "true", "yes", "on"}


def _users_db_path(db_path: str) -> str:
    """Users/sessions live in a *separate* SQLite file next to the memory DB — auth
    state is not memory state (see inspector/auth.py's module docstring): mixing the
    two would put password/session-token hashes inside the same file that
    ``/api/export`` and ordinary DB backups copy around."""
    return ":memory:" if db_path == ":memory:" else db_path + ".users.db"


def _set_cookie(response: Response, token: str, secure: bool = False) -> None:
    response.set_cookie(_COOKIE, token, httponly=True, samesite="strict",
                        max_age=7 * 86400, path="/", secure=secure)


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
    def setup(body: SetupReq, response: Response):
        if store.count_users() > 0:
            raise HTTPException(status_code=400, detail={"error": "team already set up"})
        try:
            store.create_user(body.email, body.name, body.password, "admin")
            u = store.login(body.email, body.password)
        except AuthError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)})
        _set_cookie(response, u.pop("token"))
        return {"user": u}

    @router.post("/login")
    def login(body: LoginReq, response: Response):
        try:
            u = store.login(body.email, body.password)
        except AuthError as exc:
            raise HTTPException(status_code=401, detail={"error": str(exc)})
        _set_cookie(response, u.pop("token"))
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
        _require(request, "admin")
        seats = licensing.current_license().seats
        try:
            u = store.create_user(body.email, body.name, body.password, body.role,
                                  seat_limit=seats)
        except AuthError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)})
        return {"user": u}

    @router.post("/users/update")
    def upd_user(body: UpdUserReq, request: Request):
        _require(request, "admin")
        try:
            u = store.update_user(body.user_id, role=body.role, disabled=body.disabled)
        except AuthError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)})
        return {"user": u}

    app.include_router(router)
    return True, store
