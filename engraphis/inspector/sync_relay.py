"""Gated cloud-sync relay — the server-side half of the managed Pro sync transport.

Stores opaque per-account sync bundles and serves them only to a device that presents a
valid, unexpired, non-revoked license whose plan includes ``sync``. The gate
(:func:`license_registry.verify_for_feature`) runs here, on vendor hardware, so a client
that has patched its local feature check still cannot push or pull: no valid key, no
bundles. Bundles are namespaced by an account id derived from the license, so one
customer's devices never see another customer's data.

Mounted OUTSIDE the ``/api/`` prefix so the dashboard's admin-token auth gate does not
apply — authentication here IS the license key, carried as ``Authorization: Bearer``.
The bundle bytes are opaque to this layer (the sync engine treats every pulled bundle as
untrusted anyway), so an end-to-end-encrypted client can push ciphertext unchanged.
"""
from __future__ import annotations

import base64
import re
import sqlite3
import time
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from engraphis.inspector import license_registry as reg
from engraphis.licensing import LicenseError

SYNC_FEATURE = "sync"
MAX_BUNDLE_BYTES = 256 * 1024 * 1024  # match FolderTransport's cap
MAX_WORKSPACE_BYTES = 350 * 1024 * 1024
MAX_BUNDLES_PER_WORKSPACE = 64
MAX_BUNDLE_NAME_CHARS = 200
MAX_WORKSPACE_ID_CHARS = 200

_BUNDLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS sync_bundles (
    account_id   TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    name         TEXT NOT NULL,
    data         BLOB NOT NULL,
    updated_at   REAL NOT NULL,
    PRIMARY KEY (account_id, workspace_id, name)
);
"""


def _conn(db_path: Optional[str] = None) -> sqlite3.Connection:
    conn = reg.connect(db_path)          # same relay DB as the registry
    conn.executescript(_BUNDLE_SCHEMA)
    return conn


def _safe_name(name: str) -> str:
    """Return a bounded portable bundle name, or ``""`` when invalid."""
    value = str(name or "").strip()
    if (
        len(value) > MAX_BUNDLE_NAME_CHARS
        or not value.endswith(".json")
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value) is None
    ):
        return ""
    return value


def _safe_workspace_id(workspace_id: str) -> str:
    value = str(workspace_id or "").strip()
    if (
        not value
        or len(value) > MAX_WORKSPACE_ID_CHARS
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        return ""
    return value


def _bearer_key(request: Request) -> str:
    return (request.headers.get("Authorization") or "").removeprefix("Bearer ").strip()


#: Header the client uses to identify its device so the relay can hold it to a seat.
MACHINE_ID_HEADER = "X-Engraphis-Machine-Id"


def _machine_id(request: Request) -> str:
    return (request.headers.get(MACHINE_ID_HEADER) or "").strip()


def _authorize(request: Request):
    """Verify the caller's license server-side. Returns (license, account_id).

    Raises :class:`LicenseError` (rendered as 402 by the app's exception handler) if the
    key is missing, malformed, expired, wrong-plan, or revoked.

    Team seat enforcement lives HERE, on vendor hardware — this is the only truly
    non-bypassable gate. A Team license is paid per seat and must not be shareable beyond
    its ``seats`` count, so every Team sync call must present the device's machine id and
    hold a live seat: the relay reclaims idle seats, then claims/refreshes this device's
    seat, refusing (402) once ``seats`` distinct devices are active at once. An idle device
    frees its seat automatically, so seats float without ever exceeding the cap. Pro (the
    individual multi-device tier) is intentionally not device-capped at the relay: its
    value is one person syncing their own machines, and ``account_id`` already isolates it
    from other customers."""
    lic = reg.verify_for_feature(_bearer_key(request), SYNC_FEATURE)
    if lic.plan == "team":
        mid = _machine_id(request)
        if not mid:
            raise LicenseError(
                "team sync requires a device id — this client must send the "
                "%s header (register a seat before syncing)" % MACHINE_ID_HEADER,
                feature=SYNC_FEATURE)
        conn = _conn()
        try:
            reg.claim_seat(conn, lic, mid)     # raises LicenseError (→ 402) if seat cap full
        finally:
            conn.close()
    return lic, reg.account_id_for(lic)


router = APIRouter(prefix="/relay/v1", tags=["sync-relay"])


@router.post("/{workspace_id}/bundles/{name}")
async def push_bundle(workspace_id: str, name: str, request: Request):
    """Store (overwrite) one full-state bundle for the caller's account+workspace."""
    _lic, account_id = _authorize(request)          # raises → 402 if unlicensed
    workspace = _safe_workspace_id(workspace_id)
    safe = _safe_name(name)
    if not workspace or not safe:
        return JSONResponse({"error": "invalid workspace or bundle name"}, status_code=400)
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > MAX_BUNDLE_BYTES:
            return JSONResponse({"error": "bundle too large"}, status_code=413)
        data.extend(chunk)
    payload = bytes(data)
    conn = _conn()
    try:
        usage = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(LENGTH(data)), 0) AS total, "
            "COALESCE(MAX(CASE WHEN name=? THEN LENGTH(data) ELSE 0 END), 0) AS replacing "
            "FROM sync_bundles WHERE account_id=? AND workspace_id=?",
            (safe, account_id, workspace),
        ).fetchone()
        replacing = int(usage["replacing"] or 0)
        if replacing == 0 and int(usage["n"] or 0) >= MAX_BUNDLES_PER_WORKSPACE:
            return JSONResponse(
                {"error": "workspace has too many device bundles"}, status_code=413
            )
        projected = int(usage["total"] or 0) - replacing + len(payload)
        if projected > MAX_WORKSPACE_BYTES:
            return JSONResponse(
                {"error": "workspace relay storage limit exceeded"}, status_code=413
            )
        conn.execute(
            "INSERT INTO sync_bundles (account_id, workspace_id, name, data, updated_at) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(account_id, workspace_id, name) DO UPDATE SET "
            "  data=excluded.data, updated_at=excluded.updated_at",
            (account_id, workspace, safe, payload, time.time()),
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "name": safe, "bytes": len(payload)}


@router.get("/{workspace_id}/bundles")
async def pull_bundles(workspace_id: str, request: Request):
    """Return every bundle for the caller's account+workspace (base64-encoded).

    Isolation is enforced by ``account_id`` in the WHERE clause: a caller only ever sees
    bundles pushed under their own license identity."""
    _lic, account_id = _authorize(request)
    workspace = _safe_workspace_id(workspace_id)
    if not workspace:
        return JSONResponse({"error": "invalid workspace"}, status_code=400)
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT name, data FROM sync_bundles WHERE account_id=? AND workspace_id=? "
            "ORDER BY name",
            (account_id, workspace),
        ).fetchall()
    finally:
        conn.close()
    return {"bundles": [
        {"name": r["name"], "data": base64.b64encode(r["data"]).decode("ascii")}
        for r in rows
    ]}


@router.get("/{workspace_id}/names")
async def list_names(workspace_id: str, request: Request):
    """Bundle names only (no payloads) for the caller's account+workspace."""
    _lic, account_id = _authorize(request)
    workspace = _safe_workspace_id(workspace_id)
    if not workspace:
        return JSONResponse({"error": "invalid workspace"}, status_code=400)
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT name FROM sync_bundles WHERE account_id=? AND workspace_id=? "
            "ORDER BY name",
            (account_id, workspace),
        ).fetchall()
    finally:
        conn.close()
    return {"names": [r["name"] for r in rows]}
