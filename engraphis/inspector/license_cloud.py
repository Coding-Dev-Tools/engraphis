"""Cloud license endpoints — registration (issues a signed lease), status, revocation.

Server-side counterpart to :mod:`engraphis.cloud_license`. Mounted OUTSIDE ``/api`` so a
client authenticates with its *license key*, not the dashboard admin token. Registration
verifies the key against the pinned vendor key + registry (signature, expiry, plan, not
revoked), enforces the per-key seat cap by counting distinct machine ids, records the
device, and returns a short-lived Ed25519-signed lease the client verifies offline.
Revocation requires the vendor admin token (``ENGRAPHIS_API_TOKEN``).
"""
from __future__ import annotations

import asyncio
import hmac
import os
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from engraphis import cloud_license
from engraphis.config import settings
from engraphis.inspector import license_registry as reg
from engraphis.inspector.auth import _EMAIL_RE
from engraphis.inspector.webhooks import _load_signing_secret
from engraphis.licensing import LicenseError, PLAN_FEATURES, parse_key

LEASE_TTL_HOURS_DEFAULT = 24

#: Seats granted by the self-serve free Team trial (:func:`start_team_trial` below).
#: Fixed at 5 UNCONDITIONALLY — the trial request carries no seat count (see the
#: endpoint's docstring: only machine_id/email/plan are accepted), so there is
#: nothing a caller could pass to change this. A Team trial exists to show a whole
#: team the product, so it is always 5 seats for the full TRIAL_DAYS window, same
#: for every device/email, no plan-based or env-based override.
TEAM_TRIAL_SEATS = 5

_REG_SCHEMA = """
CREATE TABLE IF NOT EXISTS registrations (
    key_id     TEXT NOT NULL,
    machine_id TEXT NOT NULL,
    first_seen REAL NOT NULL,
    last_seen  REAL NOT NULL,
    PRIMARY KEY (key_id, machine_id)
);
"""


def _conn():
    conn = reg.connect()                 # shared relay DB (ENGRAPHIS_RELAY_DB)
    conn.executescript(_REG_SCHEMA)
    return conn


def _lease_ttl_seconds() -> int:
    """Deprecated shim — the lease TTL now lives in license_registry (single source of
    truth shared with seat reclamation). Kept so any external caller keeps working."""
    return reg.lease_ttl_seconds()   # floor 5 min so a misconfig can't mint 0s leases


router = APIRouter(prefix="/license/v1", tags=["license-cloud"])


@router.post("/register")
async def register(request: Request):
    """Register a device for a key and return a signed lease. 402 if the key is bad/
    expired/revoked; 402 (seat message) if the per-key device cap is reached."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    key = (body.get("key") or "").strip()
    machine_id = (body.get("machine_id") or "").strip()
    if not machine_id:
        return JSONResponse({"error": "machine_id required"}, status_code=400)

    lic = parse_key(key)                              # signature + expiry + plan → 402
    if reg.is_revoked(lic.key_id):
        raise LicenseError("this license has been revoked")

    now = time.time()
    conn = _conn()
    try:
        # Claim (or refresh) this device's seat. Reclaims seats whose lease has lapsed
        # first, then enforces the per-license cap; raises LicenseError (→ 402) if full.
        reg.claim_seat(conn, lic, machine_id, now=now)
    finally:
        conn.close()

    try:                                              # ensure it's in the issued registry
        reg.record_issued(key)
    except Exception:
        pass

    ttl = reg.lease_ttl_seconds()
    payload = {"v": 1, "key_id": lic.key_id, "plan": lic.plan,
               "features": sorted(lic.features), "machine_id": machine_id,
               "issued": int(now), "expires": int(now + ttl)}
    lease = cloud_license.compose_lease(payload, _load_signing_secret())
    return {"lease": lease, "expires": payload["expires"], "plan": lic.plan}


@router.get("/verify/{key_id}")
async def verify(key_id: str):
    """Public status probe for a key fingerprint (no key material needed)."""
    conn = reg.connect()
    try:
        row = conn.execute(
            "SELECT status, plan, expires FROM issued_licenses WHERE key_id=?",
            (key_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return {"key_id": key_id, "known": False, "valid": False}
    valid = row["status"] != "revoked" and (
        row["expires"] is None or time.time() <= row["expires"])
    return {"key_id": key_id, "known": True, "status": row["status"],
            "plan": row["plan"], "expires": row["expires"], "valid": bool(valid)}


def _admin_ok(request: Request) -> bool:
    token = settings.api_token
    supplied = (request.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
    return bool(token) and bool(supplied) and hmac.compare_digest(supplied, token)


@router.post("/revoke/{key_id}")
async def revoke(key_id: str, request: Request):
    """Vendor-only: kill a key. Its devices lose access at the next lease renewal."""
    if not _admin_ok(request):
        return JSONResponse({"error": "vendor admin token required"}, status_code=401)
    changed = reg.revoke(key_id)
    return {"key_id": key_id, "revoked": True, "changed": changed}


@router.get("/keys")
async def keys_by_email(request: Request, email: str = ""):
    """Vendor-only: look up a customer's keys by email, with plan/status/seat usage.

    Bridges the support flow: you know the buyer's email, not their key_id fingerprint."""
    if not _admin_ok(request):
        return JSONResponse({"error": "vendor admin token required"}, status_code=401)
    email = (email or "").strip().lower()
    if not email:
        return JSONResponse({"error": "email query param required"}, status_code=400)
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT key_id, plan, seats, status, expires FROM issued_licenses "
            "WHERE lower(email)=? ORDER BY created_at DESC", (email,)).fetchall()
        out = []
        for r in rows:
            used = conn.execute("SELECT COUNT(*) AS n FROM registrations WHERE key_id=?",
                                (r["key_id"],)).fetchone()["n"]
            out.append({"key_id": r["key_id"], "plan": r["plan"], "seats": r["seats"],
                        "status": r["status"], "devices_used": used, "expires": r["expires"]})
    finally:
        conn.close()
    return {"email": email, "keys": out}


@router.post("/revoke-by-email")
async def revoke_by_email(request: Request):
    """Vendor-only: revoke every key issued to an email (refund / chargeback / abuse)."""
    if not _admin_ok(request):
        return JSONResponse({"error": "vendor admin token required"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    email = (body.get("email") or "").strip().lower()
    if not email:
        return JSONResponse({"error": "email required"}, status_code=400)
    conn = reg.connect()
    try:
        rows = conn.execute(
            "SELECT key_id FROM issued_licenses WHERE lower(email)=?", (email,)).fetchall()
    finally:
        conn.close()
    revoked = [r["key_id"] for r in rows if reg.revoke(r["key_id"])]
    return {"email": email, "revoked": revoked, "count": len(revoked)}


@router.get("/keys/{key_id}/devices")
async def key_devices(key_id: str, request: Request):
    """Vendor-only: list a key's registered devices (spot seat-sharing / abuse)."""
    if not _admin_ok(request):
        return JSONResponse({"error": "vendor admin token required"}, status_code=401)
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT machine_id, first_seen, last_seen FROM registrations WHERE key_id=? "
            "ORDER BY last_seen DESC", (key_id,)).fetchall()
    finally:
        conn.close()
    return {"key_id": key_id, "devices": [
        {"machine_id": r["machine_id"], "first_seen": r["first_seen"],
         "last_seen": r["last_seen"]} for r in rows]}


@router.post("/deactivate")
async def deactivate_device(request: Request):
    """Vendor-only: free a seat by removing a device registration.

    Without this, a legit device swap (new laptop) permanently burns a seat, because
    registrations only grow and the cap is by distinct machine. Frees the slot so the
    replacement can register."""
    if not _admin_ok(request):
        return JSONResponse({"error": "vendor admin token required"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    key_id = (body.get("key_id") or "").strip()
    machine_id = (body.get("machine_id") or "").strip()
    if not key_id or not machine_id:
        return JSONResponse({"error": "key_id and machine_id required"}, status_code=400)
    conn = _conn()
    try:
        freed = reg.release_seat(conn, key_id, machine_id)
    finally:
        conn.close()
    return {"key_id": key_id, "machine_id": machine_id, "deactivated": freed}


# ── team-invite relay ───────────────────────────────────────────────────────────
# Lets a self-hosted Team dashboard with NO email delivery of its own (no local
# ENGRAPHIS_RESEND_API_KEY/SMTP_*) still get a working "Add member" invite email,
# by sending it through the VENDOR's mail provider instead — the same account that
# already emails every license key. The license key IS the authentication here
# (there is no admin token / customer identity to check, same as /register): a
# caller must present a signed key that verifies AND currently carries the "team"
# feature. Paid keys AND self-serve trial keys (see /license/v1/start-trial below —
# trial users need this to actually work, or they never see enough value to
# subscribe) both qualify, so the per-key daily cap is kept tight (default 10) to
# bound cost/abuse from a trial key, which costs nothing to obtain.

_INVITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS team_invite_sends (
    key_id TEXT NOT NULL,
    day    TEXT NOT NULL,
    count  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (key_id, day)
);
"""


def _invite_daily_cap() -> int:
    try:
        return max(1, int(os.environ.get("ENGRAPHIS_TEAM_INVITE_DAILY_CAP", "10")))
    except ValueError:
        return 10


def _today() -> str:
    import datetime as _dt
    return _dt.datetime.utcnow().strftime("%Y-%m-%d")


def _bump_invite_count(key_id: str) -> bool:
    """Atomically bump today's send count for *key_id*. Returns True if the send is
    allowed (was under the cap before this call), False if already at/over it.

    Uses the same BEGIN IMMEDIATE pattern as :func:`license_registry.claim_seat` —
    the check-then-write must be one atomic step so two concurrent invites from the
    same key can't both slip through one under the cap."""
    conn = reg.connect()
    try:
        conn.executescript(_INVITE_SCHEMA)
        day = _today()
        cap = _invite_daily_cap()
        prev_iso = conn.isolation_level
        conn.isolation_level = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT count FROM team_invite_sends WHERE key_id=? AND day=?",
                (key_id, day)).fetchone()
            count = int(row["count"]) if row else 0
            if count >= cap:
                conn.execute("COMMIT")
                return False
            conn.execute(
                "INSERT INTO team_invite_sends(key_id, day, count) VALUES (?,?,1) "
                "ON CONFLICT(key_id, day) DO UPDATE SET count = count + 1",
                (key_id, day))
            conn.execute("COMMIT")
            return True
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.isolation_level = prev_iso
    finally:
        conn.close()


@router.post("/team-invite")
async def team_invite(request: Request):
    """Send a team-invite notification through the vendor's own mail provider, on
    behalf of a self-hosted Team dashboard that has none configured. 402 if *key*
    doesn't verify or lacks the ``team`` feature (:func:`license_registry.
    verify_for_feature` — the same server-side gate every other licensed feature
    uses); 400 for a malformed recipient; 429 past the per-key daily cap; 502 if
    the vendor's own mail provider rejects the send."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    key = (body.get("key") or "").strip()
    to = (body.get("to") or "").strip().lower()
    name = (body.get("name") or "").strip()[:120]
    role = (body.get("role") or "member").strip()[:32]
    invited_by = (body.get("invited_by") or "").strip().lower()

    lic = reg.verify_for_feature(key, "team")          # bad/expired/wrong-plan/revoked → 402
    if not _EMAIL_RE.match(to):
        return JSONResponse({"error": "invalid recipient email"}, status_code=400)
    if invited_by and not _EMAIL_RE.match(invited_by):
        invited_by = ""                                 # ignore a malformed value, don't fail

    if not _bump_invite_count(lic.key_id):
        return JSONResponse(
            {"error": "daily invite-email limit reached for this license — try again "
                      "tomorrow, or configure your own ENGRAPHIS_RESEND_API_KEY/SMTP "
                      "to send directly instead of relaying"},
            status_code=429)

    from engraphis.inspector.webhooks import send_team_invite_email
    try:
        await asyncio.to_thread(send_team_invite_email, to, name, role, invited_by=invited_by)
    except Exception as exc:  # noqa: BLE001 — surface a safe message, don't leak internals
        return JSONResponse({"error": "delivery failed: %s" % exc}, status_code=502)
    return {"sent": True}


# ── self-serve Team trial: a REAL signed key, no purchase, no Polar checkout ───────────
# The local, fully-offline free trial (``licensing.start_trial``) only ever grants Pro —
# it is a client-only construct (HMAC-signed against the local machine, no server-issued
# key), which is exactly why it can never be used against ``team_invite`` above: that
# endpoint's whole security model is "only a genuinely vendor-signed key gets through",
# and an offline client-only claim can't prove that to a server that never saw it.
# Trial users still need team-invite (and the rest of Team mode) to actually work during
# the trial, or the trial doesn't demonstrate the product's value and they never convert
# to a paid seat. This endpoint reconciles both: it mints a REAL signed ``team`` key
# (reusing the exact same signer as a purchase) for a device's one-time trial, so
# everything downstream — team_invite, /license/v1/register, team-mode dashboard
# login — treats it exactly like a paid key would, just short-lived. One grant per
# machine_id ever (soft identifier — see ``_clean_machine_id``'s docstring in
# license_registry.py for the same honesty-about-limits as seat accounting: this raises
# the bar against casual reset-by-wipe, it does not claim to defeat a scripted attacker
# minting fresh machine ids). The resulting key's own short expiry plus the tight
# per-key invite cap above bound the cost of that residual gap.

_TRIAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS trial_grants (
    machine_id TEXT PRIMARY KEY,
    email      TEXT,
    plan       TEXT,
    issued_at  REAL NOT NULL
);
"""


def _ensure_trial_plan_column(conn) -> None:
    """Add trial_grants.plan to a pre-existing DB (older schema had none). Idempotent."""
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(trial_grants)").fetchall()}
        if "plan" not in cols:
            conn.execute("ALTER TABLE trial_grants ADD COLUMN plan TEXT")
            conn.commit()
    except Exception:
        pass


@router.post("/start-trial")
async def start_team_trial(request: Request):
    """Issue a one-time, self-serve, real signed trial key for *machine_id*.

    ``plan`` selects the tier ("pro" or "team", default "team"). The key is a genuine
    short-lived vendor-signed key carrying ``trial: 1``, so every downstream server-side
    gate (/register, team_invite) treats it exactly like a purchase. One trial per device
    ever, any plan: 409 if this device already claimed one; 400 for a missing machine_id
    or an unknown plan."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    mid = (body.get("machine_id") or "").strip()[:128]
    email = (body.get("email") or "").strip().lower()
    plan = (body.get("plan") or "team").strip().lower()
    if not mid:
        return JSONResponse({"error": "machine_id required"}, status_code=400)
    if plan not in PLAN_FEATURES:
        return JSONResponse({"error": "unknown plan '%s'" % plan}, status_code=400)
    if email and not _EMAIL_RE.match(email):
        email = ""

    conn = reg.connect()
    try:
        conn.executescript(_TRIAL_SCHEMA)
        _ensure_trial_plan_column(conn)
        prev_iso = conn.isolation_level
        conn.isolation_level = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT 1 FROM trial_grants WHERE machine_id=?", (mid,)).fetchone()
            if existing:
                conn.execute("COMMIT")
                return JSONResponse(
                    {"error": "the free trial has already been used on this device"},
                    status_code=409)
            conn.execute(
                "INSERT INTO trial_grants(machine_id, email, plan, issued_at) "
                "VALUES (?,?,?,?)", (mid, email or None, plan, time.time()))
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.isolation_level = prev_iso
    finally:
        conn.close()

    from engraphis.inspector.webhooks import issue_key
    from engraphis.licensing import TRIAL_DAYS
    # Team trials are always TEAM_TRIAL_SEATS (5) for TRIAL_DAYS (3) — unconditionally,
    # not derived from any request input (there is none to derive from; see the
    # constant's docstring above). Pro trials stay single-seat.
    seats = TEAM_TRIAL_SEATS if plan == "team" else 1
    key = issue_key(email or "trial@engraphis.local", product_name=plan, seats=seats,
                    days=TRIAL_DAYS, trial=True)
    return {"key": key, "days": TRIAL_DAYS, "plan": plan}
