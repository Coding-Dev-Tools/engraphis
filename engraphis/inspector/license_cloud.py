"""Cloud license endpoints — registration (issues a signed lease), status, revocation.

Server-side counterpart to :mod:`engraphis.cloud_license`. Mounted OUTSIDE ``/api`` so a
client authenticates with its *license key*, not the dashboard admin token. Registration
verifies the key against the pinned vendor key + registry (signature, expiry, plan, not
revoked), enforces the per-key seat cap by counting distinct machine ids, records the
device, and returns a short-lived Ed25519-signed lease the client verifies offline.
Revocation requires the vendor admin token (``ENGRAPHIS_API_TOKEN``).
"""
from __future__ import annotations

import hmac
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from engraphis import cloud_license
from engraphis.config import settings
from engraphis.inspector import license_registry as reg
from engraphis.inspector.webhooks import _load_signing_secret
from engraphis.licensing import LicenseError, parse_key

LEASE_TTL_HOURS_DEFAULT = 72

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
