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
import os
import sqlite3
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from engraphis.inspector import license_registry as reg
from engraphis.licensing import LicenseError

SYNC_FEATURE = "sync"
MAX_BUNDLE_BYTES = 256 * 1024 * 1024  # match FolderTransport's cap

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
    """Never let a bundle name escape its namespace (path traversal / separators)."""
    return os.path.basename(name or "").strip()


def _bearer_key(request: Request) -> str:
    return (request.headers.get("Authorization") or "").removeprefix("Bearer ").strip()


def _authorize(request: Request):
    """Verify the caller's license server-side. Returns (license, account_id).

    Raises :class:`LicenseError` (rendered as 402 by the app's exception handler) if the
    key is missing, malformed, expired, wrong-plan, or revoked."""
    lic = reg.verify_for_feature(_bearer_key(request), SYNC_FEATURE)
    return lic, reg.account_id_for(lic)


router = APIRouter(prefix="/relay/v1", tags=["sync-relay"])


@router.post("/{workspace_id}/bundles/{name}")
async def push_bundle(workspace_id: str, name: str, request: Request):
    """Store (overwrite) one full-state bundle for the caller's account+workspace."""
    _lic, account_id = _authorize(request)          # raises → 402 if unlicensed
    data = await request.body()
    if len(data) > MAX_BUNDLE_BYTES:
        return JSONResponse({"error": "bundle too large"}, status_code=413)
    safe = _safe_name(name)
    if not safe:
        return JSONResponse({"error": "invalid bundle name"}, status_code=400)
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO sync_bundles (account_id, workspace_id, name, data, updated_at) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(account_id, workspace_id, name) DO UPDATE SET "
            "  data=excluded.data, updated_at=excluded.updated_at",
            (account_id, workspace_id, safe, data, time.time()),
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "name": safe, "bytes": len(data)}


@router.get("/{workspace_id}/bundles")
async def pull_bundles(workspace_id: str, request: Request):
    """Return every bundle for the caller's account+workspace (base64-encoded).

    Isolation is enforced by ``account_id`` in the WHERE clause: a caller only ever sees
    bundles pushed under their own license identity."""
    _lic, account_id = _authorize(request)
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT name, data FROM sync_bundles WHERE account_id=? AND workspace_id=? "
            "ORDER BY name",
            (account_id, workspace_id),
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
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT name FROM sync_bundles WHERE account_id=? AND workspace_id=? "
            "ORDER BY name",
            (account_id, workspace_id),
        ).fetchall()
    finally:
        conn.close()
    return {"names": [r["name"] for r in rows]}
