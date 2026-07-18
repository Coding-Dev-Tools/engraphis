"""Cloud license endpoints — registration (issues a signed lease), status, revocation.

Server-side counterpart to :mod:`engraphis.cloud_license`. Mounted OUTSIDE ``/api`` so a
client authenticates with its *license key*, not the dashboard admin token. Registration
verifies the key against the pinned vendor key + registry (signature, expiry, plan, not
revoked), enforces the per-key seat cap by counting distinct machine ids, records the
device, and returns a short-lived Ed25519-signed lease the client verifies offline.
Revocation and the other vendor-only admin routes require the dedicated vendor admin
token (``ENGRAPHIS_VENDOR_ADMIN_TOKEN``). There is no fallback to ``ENGRAPHIS_API_TOKEN``:
with the variable unset those routes fail closed — see :func:`_vendor_admin_token`.

Self-serve trial signup additionally requires ``ENGRAPHIS_RELAY_PUBLIC_URL``, because the
confirmation link is emailed and must never be built from the request's ``Host`` header —
see :func:`_relay_public_base`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from engraphis import cloud_license, netutil
from engraphis.inspector import license_registry as reg
from engraphis.inspector.auth import _EMAIL_RE, _hash_token, bearer_ok
from engraphis.inspector.webhooks import _load_signing_secret
from engraphis.licensing import LicenseError, PLAN_FEATURES, parse_key

logger = logging.getLogger("engraphis.license_cloud")

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


# License keys are currently far smaller than 8 KiB; 16 KiB leaves room for the other
# fields while bounding memory before JSON decoding. Checking Content-Length alone is not
# sufficient because a caller can stream a chunked request without that header.
MAX_JSON_BODY_BYTES = 16 * 1024


class _JsonBodyError(ValueError):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


async def _bounded_json_object(request: Request) -> dict:
    """Read one small JSON object without buffering an attacker-sized request."""
    declared = request.headers.get("Content-Length")
    if declared:
        try:
            declared_bytes = int(declared)
        except ValueError:
            raise _JsonBodyError("invalid content length") from None
        if declared_bytes < 0:
            raise _JsonBodyError("invalid content length")
        if declared_bytes > MAX_JSON_BODY_BYTES:
            raise _JsonBodyError("JSON body too large", 413)

    raw = bytearray()
    try:
        async for chunk in request.stream():
            if len(raw) + len(chunk) > MAX_JSON_BODY_BYTES:
                raise _JsonBodyError("JSON body too large", 413)
            raw.extend(chunk)
        body = json.loads(raw)
    except _JsonBodyError:
        raise
    except (UnicodeDecodeError, ValueError):
        raise _JsonBodyError("invalid JSON body") from None
    if not isinstance(body, dict):
        raise _JsonBodyError("JSON body must be an object")
    return body


def _json_error(exc: _JsonBodyError) -> JSONResponse:
    return JSONResponse({"error": str(exc)}, status_code=exc.status_code)


def _single_line(value: object, *, max_chars: int, required: bool = True) -> Optional[str]:
    """Return a stripped bounded string, or ``None`` when the field is invalid."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if required and not text:
        return None
    if len(text) > max_chars or any(
            ord(char) < 32 or ord(char) == 127 for char in text):
        return None
    return text


#: Per-IP burst cap on the unauthenticated, CPU-bound relay endpoints (``/register`` and
#: ``/team-invite``, which share one budget).
#: Ed25519 verification here is the pure-Python RFC-8032 reference implementation at
#: ~3 ms a call, and ``/register`` performs two (parse_key + record_issued) — roughly
#: 165 registrations/second/core. Without a cap, a few hundred requests per second of
#: well-formed-but-invalid keys saturates the worker and starves ``/api/health``, which
#: the platform reads as a failed healthcheck and restarts (Railway:
#: ``restartPolicyMaxRetries: 10``) — i.e. an unauthenticated remote DoS.
#:
#: In-process and best-effort by design: it is a burst damper in front of the expensive
#: crypto, not an accounting control (that is ``_bump_trial_rate``, which is durable and
#: transactional because it guards a one-per-device grant). A second worker gets its own
#: bucket; the DoS ceiling scales with workers, which is the intent.
def _nonnegative_env_int(name: str, default: int) -> int:
    """Read a non-negative integer without letting a typo break module import."""
    try:
        return max(0, int(os.environ.get(name, str(default)) or default))
    except (TypeError, ValueError):
        logger.warning("%s must be an integer; using %d", name, default)
        return default


REGISTER_RATE_PER_MINUTE = _nonnegative_env_int(
    "ENGRAPHIS_REGISTER_RATE_PER_MINUTE", 60)
_REGISTER_BUCKETS: "dict[str, tuple[float, float]]" = {}
_REGISTER_BUCKETS_MAX = 4096

# The relay reuses this token-bucket machinery but NOT the register budget. ``/register``
# is hit once per client per lease renewal; a single relay *sync round* legitimately makes
# ~1 + MAX_BUNDLES_PER_WORKSPACE (64) + 1 requests back to back, so charging it against the
# 60/min register bucket would 429 the tail of every large-workspace round — and 429 aborts
# the whole pull. Give the relay its own, sync-sized per-IP budget. Its purpose is narrow:
# bound how much ~3ms pure-Python Ed25519 verify work (each on a finite ASGI threadpool
# worker) an *invalid-key* flood from one address can buy before the key is even parsed.
# Default ~10 full rounds/min/IP — generous for real clients (incl. a modest NAT'd team),
# still a hard ceiling on a flood. Tunable; <= 0 disables.
RELAY_RATE_PER_MINUTE = _nonnegative_env_int(
    "ENGRAPHIS_RELAY_RATE_PER_MINUTE", 600)
_RELAY_BUCKETS: "dict[str, tuple[float, float]]" = {}
_RELAY_BUCKETS_MAX = 4096


def _register_rate_key(request: Request) -> str:
    """Best available caller identity; a header can never disable the limiter.

    Until an edge proxy is trusted explicitly, its direct address is the conservative
    shared bucket. Configure ENGRAPHIS_FORWARDED_ALLOW_IPS for per-client buckets.
    """
    return netutil.client_ip(request)


def _token_bucket_ok(buckets: "dict[str, tuple[float, float]]", ip: str,
                     rate_per_minute: int, max_buckets: int) -> bool:
    """Shared token-bucket core: ``rate_per_minute`` tokens refilling over 60s, keyed on
    *ip*. Returns False when the caller has spent its burst. Disabled (always True) when
    the limit is <= 0 or *ip* is empty.

    *buckets* is capped at *max_buckets* entries and evicts one oldest insertion when
    full: an attacker rotating source addresses must not be able to grow it without bound
    (that would be a memory-exhaustion DoS in the code meant to prevent a DoS). Dict
    insertion order makes this O(1); the worst case forgives one old caller's burst
    without resetting every active caller's budget."""
    if rate_per_minute <= 0 or not ip:
        return True
    now = time.monotonic()
    rate = rate_per_minute / 60.0
    tokens, last = buckets.get(ip, (float(rate_per_minute), now))
    tokens = min(float(rate_per_minute), tokens + (now - last) * rate)
    if tokens < 1.0:
        buckets[ip] = (tokens, now)
        return False
    if len(buckets) >= max_buckets and ip not in buckets:
        # Evict one oldest bucket instead of clearing EVERY caller's budget. Clearing
        # made a distributed source-address spray reset all active rate limits at once.
        buckets.pop(next(iter(buckets)), None)
    buckets[ip] = (tokens - 1.0, now)
    return True


def _register_rate_ok(ip: str) -> bool:
    """Per-IP budget for the unauthenticated ``/license/v1/*`` endpoints (register, trial,
    team-invite). See :func:`_token_bucket_ok`."""
    return _token_bucket_ok(_REGISTER_BUCKETS, ip, REGISTER_RATE_PER_MINUTE,
                            _REGISTER_BUCKETS_MAX)


def _relay_rate_ok(ip: str) -> bool:
    """Per-IP budget for the ``/relay/v1/*`` sync surface — separate bucket from
    :func:`_register_rate_ok`, sized for a full sync round (see ``RELAY_RATE_PER_MINUTE``)
    so legitimate large-workspace sync never trips it."""
    return _token_bucket_ok(_RELAY_BUCKETS, ip, RELAY_RATE_PER_MINUTE, _RELAY_BUCKETS_MAX)


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
        body = await _bounded_json_object(request)
    except _JsonBodyError as exc:
        return _json_error(exc)
    raw_key, raw_machine = body.get("key"), body.get("machine_id")
    if not isinstance(raw_key, str) or not isinstance(raw_machine, str):
        return JSONResponse({"error": "key and machine_id must be strings"}, status_code=400)
    if not raw_machine.strip():
        return JSONResponse({"error": "machine_id required"}, status_code=400)
    # Same bounds check the other routes use — one implementation, so the definition of
    # "bounded single-line" cannot drift between endpoints. `key` is required=False
    # deliberately: an empty key stays a 402 from the verifier (as it always has), not a
    # 400, so the response for a wrong key does not depend on whether it is blank.
    key = _single_line(raw_key, max_chars=8192, required=False)
    machine_id = _single_line(raw_machine, max_chars=200)
    if key is None:
        return JSONResponse({"error": "license key must be a bounded single-line value"},
                            status_code=400)
    if machine_id is None:
        return JSONResponse({"error": "machine_id must be a bounded single-line value"},
                            status_code=400)

    # Burst-cap BEFORE the ~3 ms signature verify below, so an invalid-key flood is
    # rejected for the price of a dict lookup instead of the price of Ed25519.
    if not _register_rate_ok(_register_rate_key(request)):
        return JSONResponse(
            {"error": "too many registration attempts — try again shortly"},
            status_code=429, headers={"Retry-After": "60"})

    # parse_key is pure-Python Ed25519 (~3 ms) and record_issued parses again. Run both
    # in a worker thread: on the single-worker deployments we ship, doing this inline
    # blocks the event loop for every other request including /api/health.
    lic = await asyncio.to_thread(parse_key, key)     # signature + expiry + plan → 402
    if await asyncio.to_thread(reg.is_revoked, lic.key_id):
        raise LicenseError("this license has been revoked")

    now = time.time()
    # Team is the only device-capped tier (it is seat-priced). Pro is intentionally NOT
    # device-capped here: its value is one person syncing their own machines, and
    # ``account_id`` isolation already separates customers. Seat-capping every plan would
    # make a Pro customer's second device fail registration → the client maps that 402 to
    # "revoked" and drops to the free tier, breaking the flagship multi-device Pro feature.
    # Mirrors sync_relay._authorize so the two enforcement points can't drift.
    if lic.plan == "team":
        def _claim():
            conn = _conn()
            try:
                # Claim (or refresh) this device's seat. Reclaims seats whose lease has
                # lapsed first, then enforces the per-license cap; raises LicenseError
                # (→ 402) if full.
                reg.claim_seat(conn, lic, machine_id, now=now)
            finally:
                conn.close()
        # Off-loop too: claim_seat takes BEGIN IMMEDIATE and can wait out the whole
        # busy_timeout (5s) under concurrent claims for the same key. Inline, that stalls
        # every other in-flight request; in a thread it stalls only this one. LicenseError
        # propagates out of to_thread unchanged, so the 402 mapping is unaffected.
        await asyncio.to_thread(_claim)

    try:                                              # ensure it's in the issued registry
        await asyncio.to_thread(reg.record_issued, key)   # re-parses the key — off-loop
    except Exception:
        pass

    ttl = reg.lease_ttl_seconds()
    payload = {"v": 1, "key_id": lic.key_id, "plan": lic.plan,
               "features": sorted(lic.features), "machine_id": machine_id,
               "issued": int(now), "expires": int(now + ttl)}
    signing_secret = await asyncio.to_thread(_load_signing_secret)
    lease = await asyncio.to_thread(cloud_license.compose_lease, payload, signing_secret)
    return {"lease": lease, "expires": payload["expires"], "plan": lic.plan}


@router.get("/verify/{key_id}")
async def verify(key_id: str, request: Request):
    """Public status probe for a key fingerprint (no key material needed)."""
    # Shares the /register + /team-invite burst budget. One indexed SELECT is far cheaper
    # than the Ed25519 verify that budget was sized for, and key_id is a SHA-256
    # fingerprint so there is nothing to enumerate — but "cheap" is not "free", and an
    # unmetered public endpoint on the same SQLite file as the relay is not worth keeping
    # as a deliberate exception. Sharing rather than adding a bucket is safe here because
    # no client polls this route (only scripts/smoke_cloud.py), so there is no legitimate
    # high-frequency caller to starve. The same budget covers both /start-trial/verify
    # handlers, which are the genuinely expensive unauthenticated routes here.
    if not _register_rate_ok(_register_rate_key(request)):
        return JSONResponse(
            {"error": "too many verification probes — try again shortly"},
            status_code=429, headers={"Retry-After": "60"})
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


_VENDOR_UNSET_WARNED = False


def _vendor_admin_token() -> str:
    """Token authorizing vendor-wide admin actions (revoke/enumerate ANY customer's
    license, free seats) on the shared relay.

    Deliberately a SEPARATE secret from the per-instance ``ENGRAPHIS_API_TOKEN``: that
    token is handed to scripts/agents as a generic service-account credential, and one
    leaked automation credential must not be able to revoke every customer's key.

    SECURITY (2026-07-18): this used to fall back to ``ENGRAPHIS_API_TOKEN`` with a logged
    warning, which meant the separation above existed on paper only — on a relay that set
    the common variable (the documented setup) any holder of the service-account token
    could revoke every customer's license. The fallback is gone: with
    ``ENGRAPHIS_VENDOR_ADMIN_TOKEN`` unset these routes fail CLOSED (``bearer_ok`` returns
    False for an empty expected token), which costs the operator vendor tooling until they
    set the variable but can never cost a customer their license."""
    global _VENDOR_UNSET_WARNED
    token = os.environ.get("ENGRAPHIS_VENDOR_ADMIN_TOKEN", "").strip()
    if not token and not _VENDOR_UNSET_WARNED:
        logger.warning(
            "ENGRAPHIS_VENDOR_ADMIN_TOKEN is not set — vendor admin routes "
            "(/license/v1 revoke/keys/deactivate) are DISABLED. Set that variable to a "
            "dedicated secret (not ENGRAPHIS_API_TOKEN) to re-enable them.")
        _VENDOR_UNSET_WARNED = True
    return token


def _admin_ok(request: Request) -> bool:
    return bearer_ok(request.headers.get("Authorization"), _vendor_admin_token())


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
        body = await _bounded_json_object(request)
    except _JsonBodyError as exc:
        return _json_error(exc)
    email = _single_line(body.get("email"), max_chars=384)
    if email is None or not _EMAIL_RE.match(email.lower()):
        return JSONResponse({"error": "valid email required"}, status_code=400)
    email = email.lower()
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
        body = await _bounded_json_object(request)
    except _JsonBodyError as exc:
        return _json_error(exc)
    key_id = _single_line(body.get("key_id"), max_chars=200)
    machine_id = _single_line(body.get("machine_id"), max_chars=200)
    if key_id is None or machine_id is None:
        return JSONResponse(
            {"error": "key_id and machine_id must be bounded single-line strings"},
            status_code=400,
        )
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
-- First-use pin of the dashboard URL a paid key is allowed to point its invites at.
-- A SEPARATE table from team_invite_sends on purpose: that one is a daily counter whose
-- old rows are pruned every send (`DELETE ... WHERE day < ?`), and a pin that evaporates
-- overnight is not a pin — an attacker would simply wait for the next UTC day.
CREATE TABLE IF NOT EXISTS team_invite_urls (
    key_id        TEXT PRIMARY KEY,
    dashboard_url TEXT NOT NULL,
    pinned_at     REAL NOT NULL
);
"""


def _invite_daily_cap() -> int:
    try:
        return max(1, int(os.environ.get("ENGRAPHIS_TEAM_INVITE_DAILY_CAP", "10")))
    except ValueError:
        return 10


def _today() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")


def _bump_invite_count(key_id: str, day: Optional[str] = None) -> bool:
    """Atomically bump today's send count for *key_id*. Returns True if the send is
    allowed (was under the cap before this call), False if already at/over it.

    Uses the same BEGIN IMMEDIATE pattern as :func:`license_registry.claim_seat` —
    the check-then-write must be one atomic step so two concurrent invites from the
    same key can't both slip through one under the cap."""
    conn = reg.connect()
    try:
        conn.executescript(_INVITE_SCHEMA)
        day = day or _today()
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
            # Only today's row is ever read — drop older days so this table cannot grow
            # without bound (same reasoning as _bump_trial_rate; free inside this lock).
            conn.execute("DELETE FROM team_invite_sends WHERE day < ?", (day,))
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


def _pin_invite_dashboard_url(key_id: str, url: str) -> bool:
    """Bind *key_id* to the first ``dashboard_url`` it ever sent an invite with.

    Returns True when *url* is the pinned one (or is the pin being established now),
    False when this key already pinned a DIFFERENT URL.

    Without this, ``dashboard_url`` is attacker-chosen free text inside an email that
    leaves the vendor's own domain, with the vendor's own From address and reputation —
    a credential-phishing amplifier wearing our brand. ``validate_cloud_base_url`` only
    proves the URL is well-formed HTTPS, never that the caller owns it. Pinning does not
    prove ownership either, but it collapses the abuse window to a single first send per
    key and makes any later attempt to re-aim a key's invites a hard failure.

    Same ``BEGIN IMMEDIATE`` check-then-write as :func:`_bump_invite_count`, for the same
    reason: two concurrent first sends must not each observe "no pin yet" and race in
    different URLs.
    """
    conn = reg.connect()
    try:
        conn.executescript(_INVITE_SCHEMA)
        prev_iso = conn.isolation_level
        conn.isolation_level = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT dashboard_url FROM team_invite_urls WHERE key_id=?",
                (key_id,)).fetchone()
            if row is not None:
                conn.execute("COMMIT")
                return str(row["dashboard_url"]) == url
            conn.execute(
                "INSERT INTO team_invite_urls(key_id, dashboard_url, pinned_at) "
                "VALUES (?,?,?)", (key_id, url, time.time()))
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


def _refund_invite_count(key_id: str, day: Optional[str] = None) -> None:
    """Undo a daily-cap reservation when the provider did not deliver."""
    conn = reg.connect()
    try:
        conn.executescript(_INVITE_SCHEMA)
        conn.execute(
            "UPDATE team_invite_sends SET count = MAX(0, count - 1) "
            "WHERE key_id=? AND day=?", (key_id, day or _today()))
        conn.commit()
    finally:
        conn.close()


@router.post("/team-invite")
async def team_invite(request: Request):
    """Send a team-invite notification through the vendor's own mail provider, on
    behalf of a self-hosted Team dashboard that has none configured. 402 if *key*
    doesn't verify or lacks the ``team`` feature (:func:`license_registry.
    verify_for_feature` — the same server-side gate every other licensed feature
    uses); 400 for a malformed recipient; 409 if a paid key tries to re-aim its invites
    at a different ``dashboard_url`` than the one it pinned on first use; 429 for a
    per-IP burst or past the per-key daily cap; 502 if the vendor's own mail provider
    rejects the send. Trial keys never choose the link (see below), and a ``viewer``
    invite never carries the license key."""
    try:
        body = await _bounded_json_object(request)
    except _JsonBodyError as exc:
        return _json_error(exc)
    key = _single_line(body.get("key"), max_chars=8192)
    to = _single_line(body.get("to"), max_chars=384)
    name = _single_line(body.get("name", ""), max_chars=120, required=False)
    role = _single_line(body.get("role", "member"), max_chars=32)
    invited_by = _single_line(
        body.get("invited_by", ""), max_chars=384, required=False)
    dashboard_url = _single_line(
        body.get("dashboard_url", ""), max_chars=2048, required=False)
    if None in (key, to, name, role, invited_by, dashboard_url):
        return JSONResponse(
            {"error": "invite fields must be bounded single-line strings"},
            status_code=400,
        )
    to = to.lower()
    invited_by = invited_by.lower()
    if role not in {"viewer", "member", "admin"}:
        return JSONResponse({"error": "invalid team role"}, status_code=400)
    if dashboard_url:
        try:
            dashboard_url = cloud_license.validate_cloud_base_url(dashboard_url)
        except ValueError:
            return JSONResponse({"error": "invalid dashboard URL"}, status_code=400)

    # Burst-cap before the verify below, for the same reason /register does: this is an
    # unauthenticated Ed25519 verify on a caller-supplied key. The bucket is deliberately
    # SHARED with /register rather than per-endpoint — one budget covers the whole
    # unauthenticated crypto surface, so alternating endpoints cannot buy double the
    # budget for the same work.
    if not _register_rate_ok(_register_rate_key(request)):
        return JSONResponse(
            {"error": "too many invite attempts — try again shortly"},
            status_code=429, headers={"Retry-After": "60"})

    # Signature verification is pure-Python CPU work; like /register, keep it off the
    # event loop so an invalid-key burst cannot stall the relay health endpoint.
    lic = await asyncio.to_thread(
        reg.verify_for_feature, key, "team")            # bad/expired/wrong-plan/revoked → 402
    if not _EMAIL_RE.match(to):
        return JSONResponse({"error": "invalid recipient email"}, status_code=400)
    if invited_by and not _EMAIL_RE.match(invited_by):
        return JSONResponse({"error": "invalid inviter email"}, status_code=400)

    # Who gets to choose the link inside a vendor-domain email. ``key`` is the ONLY
    # authentication here, and a free self-serve trial key satisfies ``team`` — so
    # unrestricted caller-supplied ``dashboard_url`` made this an open, branded phishing
    # relay for the price of one throwaway email address.
    if lic.is_trial:
        # Costs nothing to obtain, therefore gets no say: fall back to the relay's own
        # ENGRAPHIS_DASHBOARD_URL and then DEFAULT_TEAM_DASHBOARD_URL, both resolved
        # inside send_team_invite_email. A trial is a hosted-dashboard evaluation; a
        # self-hoster with their own dashboard URL is exactly who should be paying.
        dashboard_url = ""
    elif dashboard_url and not await asyncio.to_thread(
            _pin_invite_dashboard_url, lic.key_id, dashboard_url):
        return JSONResponse(
            {"error": "this license already sends invites for a different dashboard URL; "
                      "contact support to change it"},
            status_code=409)

    reservation_day = _today()
    if not await asyncio.to_thread(_bump_invite_count, lic.key_id, reservation_day):
        return JSONResponse(
            {"error": "daily invite-email limit reached for this license — try again "
                      "tomorrow, or configure your own ENGRAPHIS_RESEND_API_KEY/SMTP "
                      "to send directly instead of relaying"},
            status_code=429)

    from engraphis.inspector.webhooks import send_team_invite_email
    try:
        # Echo the just-verified Team key into the email so the relay-delivered invite
        # also carries Pro activation (the key is this account's shared team key — the
        # same one the caller already proved they hold). dashboard_url is forwarded from
        # the admin; when empty, send_team_invite_email falls back to the relay's own
        # ENGRAPHIS_DASHBOARD_URL and then the hosted DEFAULT_TEAM_DASHBOARD_URL, so a
        # relay-delivered invite always carries a clickable sign-in link.
        #
        # NOT for a viewer. Holding this key is write access to the team's relay
        # namespace — the relay authorizes on the key alone and cannot see dashboard
        # roles — so mailing it to a read-only account silently promotes them past every
        # dashboard check (see ``routes.v2_team._send_invite``). The withholding is
        # enforced HERE, server-side, because this is the code that composes the email:
        # a self-hosted dashboard relaying through us cannot opt out of it.
        echo_key = "" if role == "viewer" else key
        await asyncio.to_thread(send_team_invite_email, to, name, role,
                                invited_by=invited_by, key=echo_key,
                                dashboard_url=dashboard_url)
    except Exception as exc:  # noqa: BLE001 — surface a safe message, don't leak internals
        try:
            await asyncio.to_thread(_refund_invite_count, lic.key_id, reservation_day)
        except Exception as refund_exc:  # noqa: BLE001 - retain the safe provider response
            logger.error("invite quota refund failed (%s)", type(refund_exc).__name__)
        logger.error("team invite delivery failed (%s)", type(exc).__name__)
        return JSONResponse(
            {"error": "invite delivery failed; check the relay mail configuration and retry"},
            status_code=502)
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
# minting fresh machine ids).
#
# 2026-07-14: machine_id alone used to be enough to mint a key synchronously — delete
# ``~/.engraphis/machine_id`` and every device looks "new" to trial_grants, so anyone
# willing to run one `rm` got infinite free trials. Two independent hardenings now sit
# in front of a grant: (1) a real, controlled email address is required and the key is
# only minted after a one-time magic link sent to it is opened (below) — resetting
# machine_id no longer helps without also owning a fresh inbox; (2) POST /start-trial
# itself is IP rate-limited (``_bump_trial_rate``), since sending mail is a cost/abuse
# vector independent of whether a grant ever happens. Neither claims to stop a determined
# attacker with many mailboxes and many source IPs — same honesty-about-limits as
# machine_id — they raise the bar from "one shell command" to "sustained, resourced
# effort", and the resulting key's own short expiry plus the tight per-key invite cap
# above bound the cost of what gets through anyway.

_TRIAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS trial_grants (
    machine_id TEXT PRIMARY KEY,
    email      TEXT,
    plan       TEXT,
    issued_at  REAL NOT NULL
);
"""

_TRIAL_PENDING_SCHEMA = """
CREATE TABLE IF NOT EXISTS trial_pending (
    token_hash TEXT PRIMARY KEY,
    machine_id TEXT NOT NULL,
    email      TEXT NOT NULL,
    plan       TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS trial_pending_machine_idx ON trial_pending(machine_id);
-- The retention sweep in _reserve_trial filters on expires_at while holding the write
-- lock; without this it is a full scan of exactly the table the sweep exists to bound.
CREATE INDEX IF NOT EXISTS trial_pending_expires_idx ON trial_pending(expires_at);
"""

#: How long a magic link stays valid. Long enough to go check an inbox, short enough
#: that an unclicked link isn't a standing liability sitting in the DB.
_TRIAL_TOKEN_TTL_SECONDS = 1800

#: How long an ALREADY-EXPIRED pending row is kept before it is swept (see the sweep in
#: :func:`_reserve_trial`). Deleting expired rows the instant they lapse would bound the
#: table but silently break the diagnostic in :func:`verify_team_trial`: that function
#: distinguishes "this link has expired — request a new trial" (row still present, past
#: its TTL) from "this link is invalid or has already been used" (row gone). Sweeping
#: eagerly collapses the first message into the second, and does so NON-deterministically
#: — the message a user sees would depend on whether some unrelated device happened to
#: call /start-trial between the link lapsing and the user clicking it. A day of grace
#: keeps the honest message while still bounding growth to roughly one day of requests.
_TRIAL_PENDING_RETENTION_SECONDS = 86400

_TRIAL_RATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS trial_start_attempts (
    ip     TEXT NOT NULL,
    window TEXT NOT NULL,
    count  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (ip, window)
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


def _trial_rate_limit_per_hour() -> int:
    try:
        return max(1, int(os.environ.get("ENGRAPHIS_TRIAL_RATE_LIMIT_PER_HOUR", "5")))
    except ValueError:
        return 5


def _hour_bucket(now: Optional[float] = None) -> str:
    import datetime as _dt
    ts = now if now is not None else time.time()
    return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).strftime("%Y-%m-%d-%H")


def _client_ip(request: Request) -> str:
    """Thin alias for :func:`engraphis.netutil.client_ip` — the implementation moved
    there (2026-07-18) so the dashboard's login lockout and audit log can share the one
    correct rightmost-``X-Forwarded-For`` reading instead of trusting
    ``request.client.host``. Kept as a name so existing callers/tests here don't move."""
    return netutil.client_ip(request)


def _bump_trial_rate(ip: str) -> bool:
    """Atomically bump this hour's /start-trial request count for *ip*. Returns True
    if the request is allowed (was under the cap before this call), False if already
    at/over ``ENGRAPHIS_TRIAL_RATE_LIMIT_PER_HOUR``. Same BEGIN IMMEDIATE idiom as
    :func:`_bump_invite_count` — the check-then-write must be one atomic step so two
    concurrent requests from the same IP can't both slip through one under the cap."""
    conn = reg.connect()
    try:
        conn.executescript(_TRIAL_RATE_SCHEMA)
        window = _hour_bucket()
        cap = _trial_rate_limit_per_hour()
        prev_iso = conn.isolation_level
        conn.isolation_level = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT count FROM trial_start_attempts WHERE ip=? AND window=?",
                (ip, window)).fetchone()
            count = int(row["count"]) if row else 0
            if count >= cap:
                conn.execute("COMMIT")
                return False
            conn.execute(
                "INSERT INTO trial_start_attempts(ip, window, count) VALUES (?,?,1) "
                "ON CONFLICT(ip, window) DO UPDATE SET count = count + 1",
                (ip, window))
            # Drop windows that can never be consulted again (only the CURRENT hour is
            # ever read). Without this the table grows one row per (ip, hour) forever —
            # an attacker rotating source addresses turns the rate limiter itself into
            # unbounded disk growth on the same volume that holds relay.db. Done inside
            # the existing BEGIN IMMEDIATE so it costs no extra lock acquisition.
            conn.execute("DELETE FROM trial_start_attempts WHERE window < ?", (window,))
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
    return True


def _relay_public_base() -> str:
    """Base URL to build the emailed magic link against — ``ENGRAPHIS_RELAY_PUBLIC_URL``,
    or ``""`` if it is unset or unusable.

    SECURITY (2026-07-18): this deliberately takes NO ``Request``. It used to fall back to
    ``str(request.base_url)``, which is derived from the client-supplied ``Host`` header —
    and no entrypoint installs ``TrustedHostMiddleware``. That let an attacker POST
    ``/start-trial`` with ``Host: attacker.tld`` and a victim's address, so the victim
    received a genuine vendor email whose confirm link pointed at the attacker; replaying
    the captured token yielded a real signed key carrying the VICTIM's email — and since
    ``license_registry.account_id_for()`` namespaces the relay on ``sha256(email)``, that
    key landed inside the victim's sync-bundle tenant.

    The parameter is gone rather than merely unused so the Host header cannot be
    reintroduced here by a later well-meaning edit. Callers MUST treat ``""`` as "trial
    signup is not configured" and refuse to send mail (see :func:`start_team_trial`) —
    failing closed, exactly as ``routes.v2_team``'s password-reset link already does with
    ``ENGRAPHIS_DASHBOARD_URL``.

    The value is validated with the same rules the client applies to a cloud URL
    (HTTPS-only except loopback, no embedded credentials, no query/fragment), so a
    typo'd or hostile env value fails closed instead of shipping a bad link to a customer.
    """
    raw = os.environ.get("ENGRAPHIS_RELAY_PUBLIC_URL", "").strip().rstrip("/")
    if not raw:
        return ""
    try:
        return cloud_license.validate_cloud_base_url(raw).rstrip("/")
    except ValueError as exc:
        logger.error(
            "ENGRAPHIS_RELAY_PUBLIC_URL is set but unusable (%s) — trial signup is "
            "disabled until it is corrected", exc)
        return ""


def _trial_verify_success_html(key: str, plan: str, days: int) -> str:
    import html as _html
    label = _html.escape(plan.title())
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Your Engraphis {label} trial key</title></head>
<body style="font-family:system-ui,sans-serif;max-width:640px;margin:48px auto;padding:0 16px">
<h2>Your {label} trial is confirmed</h2>
<p>{days}-day trial key — paste this into your dashboard's Settings &rarr; License panel:</p>
<pre style="background:#f4f4f5;padding:14px;border-radius:8px;overflow-wrap:break-word;
white-space:pre-wrap;font-size:13px">{_html.escape(key)}</pre>
<ol>
<li>Open the Engraphis dashboard (default http://127.0.0.1:8700)</li>
<li>Go to Settings &rarr; License</li>
<li>Paste the key above and click Activate</li>
</ol>
<p><strong>Hosted deployment:</strong> add the complete key above as the private
<code>ENGRAPHIS_LICENSE_KEY</code> deployment variable and redeploy. Then open the
dashboard and create the first admin. Do not post the key in logs or support tickets.</p>
<p>You can close this page.</p>
</body></html>"""


def _trial_verify_error_html(message: str) -> str:
    import html as _html
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Engraphis trial</title></head>
<body style="font-family:system-ui,sans-serif;max-width:640px;margin:48px auto;padding:0 16px">
<h2>Couldn't confirm your trial</h2>
<p>{_html.escape(message)}</p>
</body></html>"""


def _trial_confirm_html(token: str, plan: str, email: str) -> str:
    """The interstitial a human sees before the trial is actually granted.

    Exists so that redemption needs a deliberate POST rather than the bare GET an email
    link-prescanner performs on the recipient's behalf — see :func:`confirm_team_trial`.
    The form posts back to this same URL with the token in the query string, so there is
    no request body to parse."""
    import html as _html
    label = _html.escape(plan.title())
    # A QUERY-ONLY relative reference, deliberately: it resolves against the current
    # document's own path, so the form posts back to wherever this page was actually
    # served from. A root-absolute "/license/v1/start-trial/verify?..." would silently
    # break every sub-path deployment — validate_cloud_base_url PRESERVES the path
    # component (it only rejects query/fragment), so ENGRAPHIS_RELAY_PUBLIC_URL=
    # https://example.com/relay is valid config; the emailed link would render fine and
    # then POST to a 404, stranding the customer with a token that expires in 30 minutes
    # and no diagnostic. Same breakage behind a root_path reverse proxy.
    # The token goes into an attribute, so escape it with quote=True (the default).
    action = "?token=%s" % _html.escape(token)
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>Confirm your Engraphis {label} trial</title></head>
<body style="font-family:system-ui,sans-serif;max-width:640px;margin:48px auto;padding:0 16px">
<h2>Confirm your {label} trial</h2>
<p>You're one click away. This will activate a trial for
<strong>{_html.escape(email)}</strong>.</p>
<form method="post" action="{action}">
<button type="submit" style="font:inherit;font-weight:600;padding:12px 20px;border:0;
border-radius:8px;background:#5941c2;color:#fff;cursor:pointer">
Activate my {label} trial</button>
</form>
<p style="color:#666;font-size:13px;margin-top:24px">This link can only be used once.
If you didn't request a trial, you can ignore this page — nothing happens until you
click the button.</p>
</body></html>"""


def _reserve_trial(mid: str, email: str, plan: str) -> Optional[str]:
    """Reserve the newest pending magic link without blocking the event loop."""
    conn = reg.connect()
    try:
        conn.executescript(_TRIAL_SCHEMA)
        _ensure_trial_plan_column(conn)
        conn.executescript(_TRIAL_PENDING_SCHEMA)
        now = time.time()
        token = secrets.token_urlsafe(32)
        prev_iso = conn.isolation_level
        conn.isolation_level = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT 1 FROM trial_grants WHERE machine_id=?", (mid,)).fetchone()
            if existing:
                conn.execute("COMMIT")
                return None
            # Drop pending links that lapsed more than a day ago. Without this, a row
            # whose magic link is NEVER opened (bounced mail, a scanner that never
            # follows, an attacker who never intended to redeem) is only ever cleared by
            # the same machine_id asking again — so an attacker rotating machine_id at the
            # /start-trial rate-limit ceiling grows this table without bound on the same
            # volume that holds relay.db. Same reasoning and same free-inside-the-lock
            # placement as the trial_start_attempts and team_invite_sends sweeps above.
            # The retention window is deliberate, NOT slack: see
            # _TRIAL_PENDING_RETENTION_SECONDS — sweeping at expiry would turn
            # verify_team_trial's "this link has expired" into "this link is invalid".
            conn.execute("DELETE FROM trial_pending WHERE expires_at < ?",
                         (now - _TRIAL_PENDING_RETENTION_SECONDS,))
            conn.execute("DELETE FROM trial_pending WHERE machine_id=?", (mid,))
            conn.execute(
                "INSERT INTO trial_pending(token_hash, machine_id, email, plan, "
                "created_at, expires_at) VALUES (?,?,?,?,?,?)",
                (_hash_token(token), mid, email, plan, now,
                 now + _TRIAL_TOKEN_TTL_SECONDS))
            conn.execute("COMMIT")
            return token
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


@router.post("/start-trial")
async def start_team_trial(request: Request):
    """Request a one-time, self-serve trial for *machine_id* + *email*.

    Does NOT issue a key synchronously (see the 2026-07-14 module comment above): it
    emails a one-time magic link to *email* and mints the real signed key only when
    that link is CONFIRMED. Opening it (``GET``) only renders :func:`confirm_team_trial`'s
    page; the key is minted by :func:`verify_team_trial`, the ``POST`` that page's button
    sends — so a mail link-prescanner cannot burn the grant on the recipient's behalf.
    ``plan`` selects the tier ("pro" or "team", default "team"). 429 if this source IP
    has requested too many trials recently (``ENGRAPHIS_TRIAL_RATE_LIMIT_PER_HOUR``,
    default 5/hour); 400 for a missing machine_id, an unknown plan, or a missing/
    malformed email; 409 if this device already holds a trial grant; 502 if the
    verification email could not be sent."""
    try:
        body = await _bounded_json_object(request)
    except _JsonBodyError as exc:
        return _json_error(exc)
    raw_mid, raw_email = body.get("machine_id"), body.get("email")
    raw_plan = body.get("plan", "team")
    if not isinstance(raw_mid, str) or not isinstance(raw_email, str) \
            or not isinstance(raw_plan, str):
        return JSONResponse(
            {"error": "machine_id, email, and plan must be strings"}, status_code=400)
    mid = raw_mid.strip()
    email = raw_email.strip().lower()
    plan = raw_plan.strip().lower()
    if not mid:
        return JSONResponse({"error": "machine_id required"}, status_code=400)
    if len(mid) > 128 or any(ord(char) < 32 or ord(char) == 127 for char in mid):
        return JSONResponse({"error": "machine_id must be a bounded single-line value"},
                            status_code=400)
    if plan not in PLAN_FEATURES:
        return JSONResponse({"error": "unknown plan '%s'" % plan}, status_code=400)
    if not email or not _EMAIL_RE.match(email):
        return JSONResponse(
            {"error": "a valid email address is required to start a trial"},
            status_code=400)

    # Fail closed BEFORE any state is written or rate budget is spent: without a
    # configured public base we would have to derive the emailed link from the Host
    # header, which is attacker-controlled (see _relay_public_base). A misconfigured
    # relay declines trials rather than mailing a link somebody else chose.
    public_base = _relay_public_base()
    if not public_base:
        logger.error("ENGRAPHIS_RELAY_PUBLIC_URL is not set — refusing to email a "
                     "trial link built from an untrusted Host header")
        return JSONResponse(
            {"error": "trial signup is not configured on this relay"}, status_code=503)

    if not await asyncio.to_thread(_bump_trial_rate, _client_ip(request)):
        return JSONResponse(
            {"error": "too many trial requests from this network — try again later"},
            status_code=429)

    token = await asyncio.to_thread(_reserve_trial, mid, email, plan)
    if token is None:
        return JSONResponse(
            {"error": "the free trial has already been used on this device"},
            status_code=409)

    verify_url = "%s/license/v1/start-trial/verify?token=%s" % (public_base, token)
    from engraphis.inspector.webhooks import send_trial_verification_email
    try:
        await asyncio.to_thread(
            send_trial_verification_email, email, verify_url, plan,
            minutes=_TRIAL_TOKEN_TTL_SECONDS // 60)
    except Exception as exc:  # noqa: BLE001 — surface a safe message, don't leak internals
        logger.error("trial verification delivery failed (%s)", type(exc).__name__)
        return JSONResponse(
            {"error": "trial email delivery failed; check the relay mail configuration "
                      "and retry"}, status_code=502)

    return {"pending": True,
            "message": "check %s for a link to confirm and activate your trial" % email,
            "expires_in": _TRIAL_TOKEN_TTL_SECONDS}


#: Applied to EVERY /start-trial/verify response, not just the success page: the request
#: URL itself carries the one-time token, so even an error page must stay out of shared
#: caches and out of the Referer of anything the reader clicks from there.
_TRIAL_PAGE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, private",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
}


def _trial_verify_rate_limited(request: Request) -> Optional[HTMLResponse]:
    """Shared burst gate for both /start-trial/verify handlers, or None when allowed.

    Both are unauthenticated and both hit SQLite before they can know whether the token
    is even real: the GET runs connect + two executescripts + a PRAGMA + a SELECT, and
    the POST additionally takes BEGIN IMMEDIATE — a RESERVED write lock on the same
    relay.db that carries seat claims and sync bundles — BEFORE it discovers the token is
    garbage. Flooding either one with junk tokens is therefore a bigger lever than the
    single indexed SELECT behind /verify/{key_id}. Both are sync defs, so each request
    also pins a threadpool worker. Answers HTML, not JSON: these routes are opened by a
    human in a browser."""
    if _register_rate_ok(_register_rate_key(request)):
        return None
    return HTMLResponse(
        _trial_verify_error_html("Too many attempts — please wait a minute and retry."),
        status_code=429, headers=dict(_TRIAL_PAGE_HEADERS, **{"Retry-After": "60"}))


@router.get("/start-trial/verify")
def confirm_team_trial(request: Request, token: str = ""):
    """Render the confirmation page for a magic-link token — WITHOUT redeeming it.

    Redemption lives in the POST companion below, and this split is load-bearing rather
    than cosmetic. Corporate mail gateways and antivirus link-prescanners (Outlook Safe
    Links, Proofpoint URL Defense, and friends) routinely GET every URL they find in an
    email body before the recipient ever sees it. When a bare GET redeemed the token, a
    prescanner silently burned the one-time grant, and the human who then clicked the
    link got "this link is invalid or has already been used" on a completely legitimate
    first attempt — and it hit hardest at exactly the corporate mail estates most likely
    to be buying Team.

    So: GET is safe and idempotent (it only reads), and the actual grant happens on a
    POST that a human has to click a button to send. Prescanners do not submit forms.
    The token still rides in the query string, which is what the form posts back to, so
    no request body parsing — and therefore no multipart dependency — is involved."""
    limited = _trial_verify_rate_limited(request)
    if limited is not None:
        return limited
    token = (token or "").strip()
    if not token:
        return HTMLResponse(_trial_verify_error_html("Missing token."),
                            status_code=400, headers=_TRIAL_PAGE_HEADERS)

    conn = reg.connect()
    try:
        conn.executescript(_TRIAL_SCHEMA)
        _ensure_trial_plan_column(conn)
        conn.executescript(_TRIAL_PENDING_SCHEMA)
        row = conn.execute(
            "SELECT machine_id, email, plan, expires_at FROM trial_pending "
            "WHERE token_hash=?", (_hash_token(token),)).fetchone()
    finally:
        conn.close()

    # Mirror the POST's diagnostics so a user learns the real problem before clicking,
    # not after. Read-only: a lapsed row is left for the retention sweep in
    # _reserve_trial, never deleted here (deleting on GET would hand a prescanner a way
    # to destroy the row it cannot redeem).
    if row is None:
        return HTMLResponse(
            _trial_verify_error_html("This link is invalid or has already been used."),
            status_code=400, headers=_TRIAL_PAGE_HEADERS)
    if row["expires_at"] < time.time():
        return HTMLResponse(
            _trial_verify_error_html(
                "This link has expired — request a new trial from the dashboard."),
            status_code=400, headers=_TRIAL_PAGE_HEADERS)

    return HTMLResponse(_trial_confirm_html(token, row["plan"], row["email"]),
                        headers=_TRIAL_PAGE_HEADERS)


@router.post("/start-trial/verify")
def verify_team_trial(request: Request, token: str = ""):
    """Redeem a magic-link token from :func:`start_team_trial` — mints and displays the
    real signed trial key. Answers a small HTML page, not JSON: this is meant to be
    reached by a human clicking the confirm button on the GET page above, who needs to
    read and copy a key, not parse a response body. One-time: the token is deleted on
    first use (success OR a stale/losing race), so replaying it never mints twice.

    Takes the token from the QUERY STRING, not a form body: the GET page posts back to
    its own URL, so there is nothing to parse and no python-multipart dependency (which
    has broken the [server] extra before)."""
    limited = _trial_verify_rate_limited(request)
    if limited is not None:
        return limited
    token = (token or "").strip()
    if not token:
        return HTMLResponse(_trial_verify_error_html("Missing token."),
                            status_code=400, headers=_TRIAL_PAGE_HEADERS)

    conn = reg.connect()
    try:
        conn.executescript(_TRIAL_SCHEMA)
        _ensure_trial_plan_column(conn)
        conn.executescript(_TRIAL_PENDING_SCHEMA)
        now = time.time()
        token_hash = _hash_token(token)
        prev_iso = conn.isolation_level
        conn.isolation_level = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT machine_id, email, plan, expires_at FROM trial_pending "
                "WHERE token_hash=?", (token_hash,)).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return HTMLResponse(
                    _trial_verify_error_html(
                        "This link is invalid or has already been used."),
                    status_code=400, headers=_TRIAL_PAGE_HEADERS)
            if row["expires_at"] < now:
                conn.execute("DELETE FROM trial_pending WHERE token_hash=?", (token_hash,))
                conn.execute("COMMIT")
                return HTMLResponse(
                    _trial_verify_error_html(
                        "This link has expired — request a new trial from the dashboard."),
                    status_code=400, headers=_TRIAL_PAGE_HEADERS)
            mid, email, plan = row["machine_id"], row["email"], row["plan"]
            existing = conn.execute(
                "SELECT 1 FROM trial_grants WHERE machine_id=?", (mid,)).fetchone()
            if existing:
                conn.execute(
                    "DELETE FROM trial_pending WHERE token_hash=?", (token_hash,))
                conn.execute("COMMIT")
                return HTMLResponse(
                    _trial_verify_error_html(
                        "The free trial has already been used on this device."),
                    status_code=409, headers=_TRIAL_PAGE_HEADERS)
            from engraphis.inspector.webhooks import issue_key
            from engraphis.licensing import TRIAL_DAYS
            seats = TEAM_TRIAL_SEATS if plan == "team" else 1
            key = issue_key(
                email, product_name=plan, seats=seats, days=TRIAL_DAYS,
                trial=True, record=False)
            conn.execute("DELETE FROM trial_pending WHERE token_hash=?", (token_hash,))
            conn.execute(
                "INSERT INTO trial_grants(machine_id, email, plan, issued_at) "
                "VALUES (?,?,?,?)", (mid, email, plan, now))
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

    try:  # best-effort registry write after the trial transaction releases its lock
        reg.record_issued(key)
    except Exception:
        pass
    # This body contains the full signed license key and the URL still carries the
    # one-time token, so keep both out of shared caches and out of Referer headers on
    # any link the reader clicks from here. Uses the shared constant rather than an
    # inline copy so the success page and the error pages can never drift apart.
    return HTMLResponse(_trial_verify_success_html(key, plan, TRIAL_DAYS),
                        headers=_TRIAL_PAGE_HEADERS)
