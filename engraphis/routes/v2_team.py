"""Team mode (multi-user auth) for the v2 dashboard — reuses the Inspector's AuthStore.

Team plumbing is ON by default; set ``ENGRAPHIS_TEAM_MODE=0`` (or false/no/off) to
disable it. The login wall activates with a live paid entitlement (Pro or Team) and
remains active once users exist, even if that entitlement later lapses. Sessions use
an HttpOnly, SameSite=Strict cookie; roles (viewer/member/admin) are enforced
server-side.

A Pro license bootstraps a single-admin instance (cloud dashboard + sync relay, no
member seats). A Team license adds multi-user seats, roles, and agent-connect write.
Adding users beyond the first admin still requires Team (see ``AuthStore.create_user``).
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field

from engraphis import licensing
from engraphis.config import resolve_license_server_url, settings
from engraphis.netutil import client_ip, is_local_request
from engraphis.inspector.auth import (
    API_TOKEN_SCOPES, PBKDF2_ITERATIONS, SESSION_TTL_SECONDS, AccountLockedError,
    AuthError, AuthStore, role_at_least)

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


def _dashboard_base_url(request: Request) -> str:
    """Return a canonical origin for credential-bearing email links.

    A remote request's ``Host`` header is attacker-controlled, so it must never become
    the destination of a password-reset or invitation email. Hosted deployments must set
    ``ENGRAPHIS_DASHBOARD_URL``; only a genuinely local, unproxied request may fall back
    to its request origin for the zero-config loopback experience.
    """
    configured = os.environ.get("ENGRAPHIS_DASHBOARD_URL", "").strip()
    if configured:
        from engraphis.cloud_license import validate_cloud_base_url
        try:
            return validate_cloud_base_url(configured).rstrip("/")
        except ValueError as exc:
            raise ValueError("ENGRAPHIS_DASHBOARD_URL is invalid") from exc
    if not is_local_request(request):
        raise ValueError("ENGRAPHIS_DASHBOARD_URL is required for remote email links")

    from urllib.parse import urlsplit, urlunsplit
    parts = urlsplit(str(request.base_url).strip())
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise ValueError("request origin is not an absolute HTTP URL")
    try:
        parts.port
    except ValueError:
        raise ValueError("request origin has an invalid port") from None
    if parts.username is not None or parts.password is not None \
            or "\\" in parts.netloc or any(char.isspace() for char in parts.netloc):
        raise ValueError("request origin contains an invalid authority")
    return urlunsplit((parts.scheme.lower(), parts.netloc,
                       parts.path.rstrip("/"), "", ""))


def _send_invite(invitation: dict, admin: dict, request: Request) -> tuple:
    """Deliver a one-time invitation URL without exposing the Team license key."""
    if os.environ.get("ENGRAPHIS_TEAM_INVITES", "1").strip().lower() in (
            "0", "false", "no", "off"):
        return False, "team invite delivery is disabled"

    try:
        dashboard_url = _dashboard_base_url(request)
    except ValueError as exc:
        logger.error("team invite URL is unavailable (%s)", type(exc).__name__)
        return False, "a trusted dashboard URL is not configured"
    from urllib.parse import quote
    # Keep the secret in the URL fragment: browsers never send fragments in HTTP
    # requests, so Uvicorn/proxy access logs cannot capture the one-time credential.
    invite_url = dashboard_url + "/#invite_token=" + quote(invitation["token"], safe="")

    from engraphis.inspector import webhooks
    if webhooks.email_configured():
        try:
            webhooks.send_team_invite_email(
                invitation["email"], invitation["name"], invitation["role"],
                invited_by=admin["email"], invite_url=invite_url)
            return True, ""
        except Exception as exc:  # noqa: BLE001 - audited by the caller
            logger.error("local team invite delivery failed (%s)", type(exc).__name__)
            return False, "local email provider rejected delivery"

    from engraphis.licensing import _read_key_material
    key = _read_key_material()
    if not key:
        return False, ("no local email delivery configured and no active Team license "
                       "available for vendor-relayed delivery")
    from engraphis import cloud_license
    return cloud_license.send_team_invite(
        resolve_license_server_url(licensing.current_license().cloud_url), key,
        invitation["email"], invitation["name"], invitation["role"], admin["email"],
        invite_url=invite_url)


class SetupReq(BaseModel):
    email: str = Field(..., min_length=5, max_length=254)
    name: str = Field(default="", max_length=120)
    password: str = Field(..., min_length=10, max_length=128)


class LoginReq(BaseModel):
    email: str = Field(..., min_length=5, max_length=254)
    password: str = Field(..., min_length=1, max_length=128)


class ForgotReq(BaseModel):
    email: str = Field(..., min_length=1, max_length=254)


class ResetReq(BaseModel):
    token: str = Field(..., min_length=10, max_length=256)
    password: str = Field(..., min_length=10, max_length=128)


class NewUserReq(BaseModel):
    email: str = Field(..., min_length=5, max_length=254)
    name: str = Field(default="", max_length=120)
    password: Optional[str] = Field(default=None, min_length=10, max_length=128)
    role: str = Field(default="member", pattern=r'^(viewer|member|admin)$')


class InvitationReq(BaseModel):
    email: str = Field(..., min_length=5, max_length=254)
    name: str = Field(default="", max_length=120)
    role: str = Field(default="member", pattern=r'^(viewer|member|admin)$')


class InvitationAcceptReq(BaseModel):
    token: str = Field(..., min_length=10, max_length=256)
    password: str = Field(..., min_length=10, max_length=128)


class UpdUserReq(BaseModel):
    user_id: str
    role: Optional[str] = Field(default=None, pattern=r'^(viewer|member|admin)$')
    disabled: Optional[bool] = None


class DelUserReq(BaseModel):
    user_id: str


class TokenReq(BaseModel):
    label: str = Field(default="", max_length=120,
                       description="A memorable name for this agent token (e.g. 'claude-code-laptop').")
    scopes: Optional[list[str]] = Field(default=None, max_length=len(API_TOKEN_SCOPES))


def _enabled() -> bool:
    return os.environ.get("ENGRAPHIS_TEAM_MODE", "1").strip().lower() not in (
        "0", "false", "no", "off")


def _users_db_path(db_path: str) -> str:
    """Users/sessions live in a *separate* SQLite file next to the memory DB — auth
    state is not memory state (see inspector/auth.py's module docstring): mixing the
    two would put password/session-token hashes inside the same file that
    ``/api/export`` and ordinary DB backups copy around."""
    return ":memory:" if db_path == ":memory:" else db_path + ".users.db"


def _auth_iterations() -> int:
    """PBKDF2 cost for dashboard team auth.

    Production always uses the compiled-in security cost. Tests may opt into a lower
    cost with ``ENGRAPHIS_TEST_AUTH_ITERATIONS`` so full-stack dashboard coverage does
    not spend minutes hashing throwaway passwords. The override is deliberately ignored
    outside pytest to avoid accidental weak production configuration.
    """
    if os.environ.get("PYTEST_CURRENT_TEST"):
        raw = os.environ.get("ENGRAPHIS_TEST_AUTH_ITERATIONS", "").strip()
        if raw:
            try:
                return max(1, int(raw))
            except ValueError:
                pass
    return PBKDF2_ITERATIONS


def _cookie_secure(request: Request) -> bool:
    """Whether the session cookie should carry the Secure flag.

    Direct HTTPS is authoritative. Forwarded protocol headers are honored only when the
    direct peer is listed in ``ENGRAPHIS_FORWARDED_ALLOW_IPS``; see
    :func:`engraphis.http_security.wants_https`."""
    from engraphis.http_security import wants_https
    return wants_https(request)


def _set_cookie(response: Response, token: str, *, secure: bool = False) -> None:
    """Set the dashboard session cookie. Secure flag comes from :func:`_cookie_secure`
    (HTTPS, including via a TLS-terminating proxy), so the cookie is never sent cleartext.
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

    # Team mode is ON by default. A new unlicensed install remains open for solo use,
    # but setup requires a paid license (Pro or Team). A Pro license bootstraps a
    # single-admin cloud instance (dashboard + sync relay, no member seats); a Team
    # license adds multi-user seats, roles, and agent-connect write. Once users exist, the
    # dashboard keeps the login wall active even if the license lapses so private data
    # never becomes public. Paid operations and seat growth continue to enforce the
    # live entitlement; adding seats beyond the first admin still requires Team.
    store = AuthStore(_users_db_path(settings.db_path), iterations=_auth_iterations())
    app.state.auth_store = store

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
        users = store.count_users()
        entitlement = licensing.current_license()
        team_licensed = entitlement.has("team")
        paid = entitlement.is_paid
        return {"enabled": bool(paid or users),
                "needs_setup": bool(paid and users == 0),
                "licensed": team_licensed,
                "team_locked": bool(users and not paid),
                "user": _user(request)}

    @router.post("/setup")
    def setup(body: SetupReq, request: Request, response: Response):
        if not licensing.current_license().is_paid:
            raise HTTPException(status_code=402, detail={
                "error": "Setup requires an active Pro or Team license",
                "tier_required": "pro",
                "upgrade_url": licensing.upgrade_url(),
            })
        if store.count_users() > 0:
            raise HTTPException(status_code=400, detail={"error": "team already set up"})
        try:
            # require_empty closes the TOCTOU: the zero-user check and the INSERT are atomic
            # inside create_user's write transaction, so concurrent setups can't both create
            # an admin (the router check above is just a fast-path).
            admin = store.create_user(body.email, body.name, body.password, "admin",
                                      require_empty=True)
            store.record_event("team.setup", actor_id=admin["id"], actor_email=admin["email"],
                               detail="initial admin created")
            u = store.login(body.email, body.password,
                            ip=client_ip(request))
        except AuthError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)})
        _set_cookie(response, u.pop("token"), secure=_cookie_secure(request))
        return {"user": u}

    @router.post("/login")
    def login(body: LoginReq, request: Request, response: Response):
        ip = client_ip(request)
        try:
            u = store.login(body.email, body.password, ip=ip)
        except AccountLockedError as exc:
            # Typed lockout → 429 with Retry-After, so clients back off instead of
            # hammering a locked account (and the mapping can't rot with the wording).
            raise HTTPException(status_code=429, detail={"error": str(exc)},
                                headers={"Retry-After": str(exc.retry_after)})
        except AuthError as exc:
            raise HTTPException(status_code=401, detail={"error": str(exc)})
        _set_cookie(response, u.pop("token"), secure=_cookie_secure(request))
        return {"user": u}

    @router.post("/forgot")
    def forgot(body: ForgotReq, request: Request):
        """Request a password-reset link. Always answers ``{"ok": true}`` — the
        response is identical whether or not the email matches an account, the
        account is disabled, or the per-email throttle kicked in, so a client
        can't enumerate registered users by watching for a different reply.

        If (and only if) a matching, enabled account exists and the throttle
        allows it, a single-use reset link is emailed. Delivery is best-effort:
        a failure is logged server-side and never surfaced to the caller, for the
        same anti-enumeration reason (see send_password_reset_email's docstring).
        """
        try:
            base = _dashboard_base_url(request)
        except ValueError as exc:
            # Do not issue (and thereby invalidate) a reset token that cannot be sent.
            # The public response remains identical to the unknown-user path.
            logger.warning("password reset URL is unavailable (%s)", type(exc).__name__)
            return {"ok": True}
        try:
            info = store.request_password_reset(body.email)
        except AuthError:
            info = None
        if info:
            try:
                reset_url = base + "/#reset_token=" + info["token"]
                from engraphis.inspector import webhooks
                if webhooks.email_configured():
                    webhooks.send_password_reset_email(
                        info["email"], info["name"], reset_url)
                else:
                    from engraphis import cloud_license
                    from engraphis.licensing import _read_key_material
                    key = _read_key_material()
                    if not key:
                        raise RuntimeError("no reset-email delivery credential")
                    queued, reason = cloud_license.send_password_reset(
                        resolve_license_server_url(licensing.current_license().cloud_url),
                        key, info["email"], info["name"], reset_url,
                    )
                    if not queued:
                        raise RuntimeError(reason or "reset-email relay rejected delivery")
            except Exception as exc:  # noqa: BLE001 — must never change the response
                # Recipient and reset token are deliberately absent from logs.
                logger.warning("password reset delivery failed (%s)",
                               type(exc).__name__)
        return {"ok": True}

    @router.post("/reset")
    def reset(body: ResetReq, request: Request, response: Response):
        """Consume a password-reset token issued by ``/forgot`` and sign the user
        back in with a fresh session (old sessions are revoked — see
        AuthStore.reset_password)."""
        try:
            u = store.reset_password(body.token, body.password)
        except AuthError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)})
        _set_cookie(response, u.pop("token"), secure=_cookie_secure(request))
        return {"user": u}

    @router.post("/logout")
    def logout(request: Request, response: Response):
        tok = request.cookies.get(_COOKIE)
        if tok:
            store.revoke_session(tok)
        response.delete_cookie(_COOKIE, path="/")
        return {"ok": True}

    def _create_and_send_invitation(body, request: Request) -> dict:
        admin = _require(request, "admin")
        try:
            invitation = store.create_invitation(
                body.email, body.name, body.role, created_by=admin["id"],
                seat_limit=licensing.current_license().seats)
        except AuthError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)})
        sent, reason = _send_invite(invitation, admin, request)
        store.set_invitation_delivery(
            invitation["id"], "sent" if sent else "failed", reason)
        ip = client_ip(request)
        store.record_event(
            "user.invited", actor_id=admin["id"], actor_email=admin["email"],
            target=invitation["email"], detail="role=%s" % invitation["role"], ip=ip)
        if not sent:
            store.record_event(
                "user.invite_email_failed", actor_id=admin["id"],
                actor_email=admin["email"], target=invitation["email"],
                detail=reason[:200], ip=ip)
        public = dict(invitation)
        public.pop("token", None)
        public["delivery_state"] = "sent" if sent else "failed"
        public["last_delivery_error"] = "" if sent else reason[:200]
        return {"invitation": public, "invited": sent}

    @router.post("/invitations/accept")
    def accept_invitation(body: InvitationAcceptReq, request: Request, response: Response):
        try:
            # Re-read the entitlement at acceptance time: an invitation may have been
            # issued under a larger Team plan and must not oversubscribe a downgrade.
            accepted = store.accept_invitation(
                body.token, body.password,
                seat_limit=licensing.current_license().seats,
            )
            store.record_event(
                "user.invitation_accepted", actor_id=accepted["id"],
                actor_email=accepted["email"], target=accepted["email"],
                detail="role=%s" % accepted["role"], ip=client_ip(request))
            # The one-time invitation token has already authenticated this recipient.
            # Mint the session directly instead of feeding the new password through the
            # public login throttle: an attacker must not be able to consume a valid
            # invitation and then strand its recipient behind an IP lockout.
            session_token = store.create_session(accepted["id"])
        except AuthError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)})
        _set_cookie(response, session_token, secure=_cookie_secure(request))
        return {"user": accepted}

    @router.post("/invitations")
    def create_invitation(body: InvitationReq, request: Request):
        return _create_and_send_invitation(body, request)

    @router.get("/invitations")
    def invitations(request: Request):
        _require(request, "admin")
        return {"invitations": store.list_invitations()}

    @router.post("/invitations/{invite_id}/resend")
    def resend_invitation(invite_id: str, request: Request):
        admin = _require(request, "admin")
        try:
            invitation = store.resend_invitation(invite_id)
        except AuthError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)})
        sent, reason = _send_invite(invitation, admin, request)
        store.set_invitation_delivery(invite_id, "sent" if sent else "failed", reason)
        store.record_event(
            "user.invitation_resent", actor_id=admin["id"], actor_email=admin["email"],
            target=invitation["email"], detail="sent=%s" % sent, ip=client_ip(request))
        return {"ok": sent, "delivery_state": "sent" if sent else "failed",
                "error": "" if sent else reason}

    @router.delete("/invitations/{invite_id}")
    def revoke_invitation(invite_id: str, request: Request):
        admin = _require(request, "admin")
        if not store.revoke_invitation(invite_id):
            raise HTTPException(status_code=404, detail={"error": "invitation not found"})
        store.record_event(
            "user.invitation_revoked", actor_id=admin["id"],
            actor_email=admin["email"], target=invite_id, ip=client_ip(request))
        return {"ok": True}

    @router.get("/users")
    def users(request: Request):
        # "admin", not "member": auth.min_role() maps every /api/auth/users* path to admin
        # and runs FIRST in dashboard_app's _auth_gate middleware, so a member is already
        # refused upstream and a laxer check here is dead code that only misleads. Keeping
        # the two in agreement means min_role() stays the single source of truth and this
        # route can't quietly become the weaker one if the router is ever mounted without
        # that middleware.
        _require(request, "admin")
        return {"users": store.list_users()}

    @router.post("/users")
    def add_user(body: NewUserReq, request: Request):
        # v1.0 compatibility alias. Password-bearing account creation is intentionally
        # rejected; callers must send only email/name/role and let the recipient choose
        # the password through the one-time invitation.
        if body.password is not None:
            raise HTTPException(status_code=400, detail={
                "error": "temporary passwords are no longer accepted; create an invitation"
            })
        return _create_and_send_invitation(body, request)

    @router.post("/users/update")
    def upd_user(body: UpdUserReq, request: Request):
        admin = _require(request, "admin")
        before = store.get_user(body.user_id)
        try:
            u = store.update_user(body.user_id, role=body.role, disabled=body.disabled,
                                  seat_limit=licensing.current_license().seats)
        except AuthError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)})
        ip = client_ip(request)
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

    @router.post("/users/delete")
    def del_user(body: DelUserReq, request: Request):
        """Permanently remove a member — frees their seat and their email address so
        the same address can be re-invited (e.g. after a typo'd/bounced invite email).
        See AuthStore.delete_user for why this is a hard delete rather than disable."""
        admin = _require(request, "admin")
        try:
            deleted = store.delete_user(body.user_id)
        except AuthError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)})
        ip = client_ip(request)
        store.record_event("user.deleted", actor_id=admin["id"], actor_email=admin["email"],
                           target=deleted["email"], ip=ip)
        return {"ok": True}

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
            iso = _dt.datetime.fromtimestamp(
                e["ts"], tz=_dt.timezone.utc).isoformat()
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

    # ── per-user API tokens (agent connect) ─────────────────────────────────────
    # A signed-in member mints a long-lived bearer token here (from the dashboard UI)
    # and pastes it into their agent's config. The token authenticates the agent to
    # /api/* exactly like a cookie session would (see dashboard_app._auth_gate), bound
    # to that member for personal-folder authz and role enforcement. The Team-license
    # gate itself lives on the agent endpoints (e.g. POST /api/remember -> _paid('team')).

    @router.post("/token")
    def create_token(body: TokenReq, request: Request):
        u = _require(request, "viewer")
        allowed = {"agent", "sync:read"}
        if role_at_least(u["role"], "member"):
            allowed.add("sync:write")
        requested = set(allowed if body.scopes is None else body.scopes)
        if not requested:
            raise HTTPException(status_code=400, detail={
                "error": "at least one token scope is required"})
        if not requested.issubset(allowed):
            raise HTTPException(status_code=403, detail={"error": "scope not allowed for role"})
        try:
            row = store.create_api_token(u["id"], label=body.label, scopes=requested)
        except AuthError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)})
        ip = client_ip(request)
        store.record_event("api_token.created", actor_id=u["id"], actor_email=u["email"],
                           detail=row["label"] or "(unlabelled)", ip=ip)
        # ``token`` is returned ONCE; list_api_tokens below never includes it.
        return row

    @router.get("/tokens")
    def list_tokens(request: Request):
        u = _require(request, "viewer")
        return {"tokens": store.list_api_tokens(u["id"])}

    @router.delete("/token/{token_id}")
    def revoke_token(token_id: str, request: Request):
        u = _require(request, "viewer")
        ok = store.revoke_api_token(u["id"], token_id)
        if not ok:
            raise HTTPException(status_code=404, detail={"error": "token not found"})
        ip = client_ip(request)
        store.record_event("api_token.revoked", actor_id=u["id"], actor_email=u["email"],
                           target=token_id, ip=ip)
        return {"ok": True}

    @router.get("/connect-info")
    def connect_info(request: Request):
        """Who am I + the base URL/config an agent should use. Works with either a
        cookie session (browser) or a per-user bearer token (agent verifying itself)."""
        u = getattr(request.state, "user", None) or _user(request)
        if not u:
            raise HTTPException(status_code=401, detail={"error": "authentication required"})
        base = os.environ.get("ENGRAPHIS_DASHBOARD_URL", "").strip().rstrip("/")
        if not base:
            base = str(request.base_url).rstrip("/")
        # Importability is not availability: the MCP package can be installed while app
        # construction still refuses the mount (for example, an invalid public URL).
        mcp_on = bool(getattr(request.app.state, "mcp_over_http", False))
        return {
            "user": {"id": u["id"], "email": u["email"], "name": u.get("name", ""),
                     "role": u["role"]},
            "api_base": base + "/api",
            "dashboard_url": base,
            # A ready-to-paste snippet for an HTTP-capable agent/MCP-shell:
            "snippet": (
                f"ENGRAPHIS_API_URL={base}/api\n"
                f"Authorization: Bearer <your-token>\n"
                f"POST {base}/api/remember   {{\"content\": \"...\", \"workspace\": \"default\"}}\n"
                f"GET  {base}/api/recall?q=...&workspace=default"),
            "mcp_over_http": mcp_on,
            "mcp_url": (base + "/mcp") if mcp_on else None,
        }

    app.include_router(router)
    return True, store
