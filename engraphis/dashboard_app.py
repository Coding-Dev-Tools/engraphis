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
#
# The /api/license and /api/license/*-trial entries close a real deadlock: create_user()
# (called by /api/auth/setup) requires require_feature("team"), so a brand-new team-mode
# instance with zero users can't create its first admin without an active license — but
# before this fix, obtaining that license (reading /api/license, or starting a Pro/Team
# trial) itself required an authenticated team session, which is impossible with zero users.
# Every visitor hit a 401 the instant they touched Settings → License or clicked "Start
# trial," logged in or not. These three are safe to expose pre-login: GET /api/license is
# instance-level plan info (already fully public in single-user mode), and the trial
# endpoints are self-limited server-side regardless of who calls them (one trial per device
# via cloud_license, refused if a paid key is already active). Deliberately NOT adding
# /api/license/activate here: unlike the _PUBLIC entries below (whole path skips the auth
# gate, role check included), pasting an arbitrary key is a whole-team-affecting action and
# stays behind min_role()'s existing admin check — a fresh self-host bootstraps a purchased
# key via ENGRAPHIS_LICENSE_KEY/~/.engraphis/license.key (server-side config), not this
# endpoint, so making it public isn't needed to fix the deadlock and would let ANY visitor
# (any role, or no session) change the whole team's license.
_PUBLIC = {"/", "/api/health", "/api/ready", "/api/auth/state", "/api/auth/login",
           "/api/auth/setup", "/api/auth/logout", "/api/auth/forgot", "/api/auth/reset",
           "/api/license", "/api/license/trial", "/api/license/team-trial",
           "/webhooks/polar"}


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
        from engraphis.service import set_current_user
        # Clear any user bound to this context before we decide who (if anyone) is calling,
        # so a personal-folder check can never inherit a stale identity from a prior request
        # served on the same worker context. The team branch below rebinds the real user;
        # public paths, the bearer bypass, and single-user mode all leave it cleared, which
        # is exactly "no per-user restriction".
        set_current_user(None)
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
            # Bind the identity the service reads to enforce personal-folder ownership on
            # every workspace-scoped read/write (see MemoryService._authorize_workspace).
            set_current_user(user)
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

    _maybe_start_autosync()
    _maybe_start_dreaming()
    _maybe_start_license_revalidation()
    return app


#: Guard so repeated ``create_app()`` calls (or a re-import) never spawn a second loop.
_AUTOSYNC_STARTED = False
_DREAMING_STARTED = False
_REVALIDATE_STARTED = False


def _maybe_start_autosync() -> None:
    """Launch the background auto-sync loop once — unless disabled or under pytest.

    A single daemon thread polls the persisted auto-sync policy (:mod:`engraphis.autosync`)
    and runs a sync pass whenever the cadence is due. It is **opt-in** (the policy defaults
    to disabled, so nothing happens until the user flips the Settings toggle), it is
    licensed-gated inside :func:`autosync.run_once` (a lapsed plan / missing key just
    no-ops), and it is fully fault-isolated: every error is swallowed and retried next tick
    so the loop can never take the dashboard down. Skipped under pytest so the test suite
    never opens a network loop, and switch-offable with ``ENGRAPHIS_AUTOSYNC_LOOP=0``."""
    global _AUTOSYNC_STARTED
    if _AUTOSYNC_STARTED:
        return
    import sys
    if "pytest" in sys.modules or _os.environ.get("PYTEST_CURRENT_TEST"):
        return
    if _os.environ.get("ENGRAPHIS_AUTOSYNC_LOOP", "1").strip().lower() in (
            "0", "false", "no", "off"):
        return
    import threading
    import time

    def _loop() -> None:
        from engraphis import autosync
        from engraphis.routes import v2_api
        time.sleep(10)   # let startup settle before the first poll
        while True:
            try:
                if autosync.due(autosync.load_policy()):
                    autosync.run_once(v2_api.service())
            except Exception:  # noqa: BLE001 — the loop must outlive any single failure
                pass
            time.sleep(60)

    threading.Thread(target=_loop, name="engraphis-autosync", daemon=True).start()
    _AUTOSYNC_STARTED = True


def _maybe_start_dreaming() -> None:
    """Launch the background "dreaming" loop once — automated consolidation without cron.

    A single daemon thread polls the persisted maintenance policy (:mod:`engraphis.automation`)
    and runs a sweep whenever the cadence is due **or** the dreaming trigger fires (enough new
    episodic memories have accumulated and the store has gone quiet — ``automation.dream_due``).
    Same safety envelope as the auto-sync loop: **opt-in** (the policy defaults to disabled),
    **Pro-gated** (``run_maintenance`` funnels through ``require_feature('automation')`` and the
    loop checks ``has_feature`` first so the free tier no-ops cheaply), fully **fault-isolated**
    (every error swallowed, retried next tick), skipped under pytest, and switch-offable with
    ``ENGRAPHIS_DREAM_LOOP=0``. Polls every 5 minutes — consolidation is heavier than a sync."""
    global _DREAMING_STARTED
    if _DREAMING_STARTED:
        return
    import sys
    if "pytest" in sys.modules or _os.environ.get("PYTEST_CURRENT_TEST"):
        return
    if _os.environ.get("ENGRAPHIS_DREAM_LOOP", "1").strip().lower() in (
            "0", "false", "no", "off"):
        return
    import threading
    import time

    def _loop() -> None:
        from engraphis import automation, licensing
        from engraphis.routes import v2_api
        time.sleep(20)   # let startup settle (after the autosync poll)
        while True:
            try:
                if licensing.has_feature("automation"):
                    svc = v2_api.service()
                    if automation.dream_due(svc):
                        automation.run_maintenance(svc, dry_run=False)
            except Exception:  # noqa: BLE001 — the loop must outlive any single failure
                pass
            time.sleep(300)

    threading.Thread(target=_loop, name="engraphis-dreaming", daemon=True).start()
    _DREAMING_STARTED = True


def _maybe_start_license_revalidation() -> None:
    """Launch a background loop that periodically re-checks an active paid license
    against the vendor relay — unless disabled or under pytest.

    ``gate()`` only re-registers when the cached lease actually expires, which means a
    revoked/refunded key can keep working locally for up to the full lease TTL before
    the next natural gate check catches it. This loop closes that latency gap: it calls
    :func:`cloud_license.revalidate` on a cadence far shorter than the lease TTL, and
    ``revalidate`` itself deletes the cached lease the moment the server denies the key
    — so the very next feature check (``gate()``/``current_license(refresh=True)``)
    fails closed immediately instead of waiting out the lease. A no-op when there is no
    key configured or the key is a local-only construct (nothing to revalidate against).
    Same safety envelope as the other background loops: fully fault-isolated, skipped
    under pytest, switch-offable with ``ENGRAPHIS_REVALIDATE_LOOP=0``."""
    global _REVALIDATE_STARTED
    if _REVALIDATE_STARTED:
        return
    import sys
    if "pytest" in sys.modules or _os.environ.get("PYTEST_CURRENT_TEST"):
        return
    if _os.environ.get("ENGRAPHIS_REVALIDATE_LOOP", "1").strip().lower() in (
            "0", "false", "no", "off"):
        return
    import threading
    import time

    def _loop() -> None:
        from engraphis import cloud_license, licensing
        from engraphis.config import settings
        time.sleep(30)   # let startup settle (after the autosync/dreaming polls)
        while True:
            try:
                material = licensing._read_key_material()
                lic = licensing.current_license()
                if material and lic.is_paid:
                    base = (_os.environ.get("ENGRAPHIS_CLOUD_URL", "").strip()
                            or lic.cloud_url or (settings.relay_url or "").strip())
                    if base:
                        cloud_license.revalidate(lic, material, base_url=base)
            except Exception:  # noqa: BLE001 — the loop must outlive any single failure
                pass
            time.sleep(600)

    threading.Thread(target=_loop, name="engraphis-revalidate", daemon=True).start()
    _REVALIDATE_STARTED = True


#: Module-level ASGI app for ``uvicorn engraphis.dashboard_app:app`` (see
#: scripts/start_dashboard.py). Built once at import; the background loops inside
#: create_app() are pytest-guarded so importing this module under test is safe.
app = create_app()
