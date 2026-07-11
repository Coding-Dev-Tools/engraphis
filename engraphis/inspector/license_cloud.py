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
import os
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
    try:
        hours = float(os.environ.get("ENGRAPHIS_LEASE_TTL_HOURS", "").strip()
                      or LEASE_TTL_HOURS_DEFAULT)
    except ValueError:
        hours = LEASE_TTL_HOURS_DEFAULT
    return max(300, int(hours * 3600))   # floor 5 min so a misconfig can't mint 0s leases


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
        seen = conn.execute(
            "SELECT 1 FROM registrations WHERE key_id=? AND machine_id=?",
            (lic.key_id, machine_id)).fetchone()
        if seen is None:
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM registrations WHERE key_id=?",
                (lic.key_id,)).fetchone()["n"]
            if count >= lic.seats:
                raise LicenseError(
                    "seat limit reached for this license (%d device(s)) — deactivate "
                    "another device first" % lic.seats)
            conn.execute(
                "INSERT INTO registrations (key_id, machine_id, first_seen, last_seen) "
                "VALUES (?,?,?,?)", (lic.key_id, machine_id, now, now))
        else:
            conn.execute(
                "UPDATE registrations SET last_seen=? WHERE key_id=? AND machine_id=?",
                (now, lic.key_id, machine_id))
        conn.commit()
    finally:
        conn.close()

    try:                                              # ensure it's in the issued registry
        reg.record_issued(key)
    except Exception:
        pass

    ttl = _lease_ttl_seconds()
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
