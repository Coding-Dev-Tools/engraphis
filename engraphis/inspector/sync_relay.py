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

import asyncio
import os
import re
import sqlite3
import time
from typing import Optional, Union

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from engraphis import netutil
from engraphis.inspector import license_registry as reg
from engraphis.licensing import LicenseError

SYNC_FEATURE = "sync"
MAX_BUNDLE_BYTES = 64 * 1024 * 1024
MAX_WORKSPACE_BYTES = 256 * 1024 * 1024
MAX_BUNDLES_PER_WORKSPACE = 64
MAX_BUNDLE_NAME_CHARS = 200
MAX_WORKSPACE_ID_CHARS = 200


def _nonnegative_env_int(name: str, default: int) -> int:
    """Read a quota without letting an invalid deployment value break app startup."""
    try:
        return max(0, int(os.environ.get(name, str(default)) or default))
    except (TypeError, ValueError):
        return default

# Per-ACCOUNT ceilings (2026-07-18). The two per-workspace limits above are not a storage
# bound on their own: ``workspace_id`` is caller-supplied and only length/charset-checked,
# so a single account could mint unlimited distinct ids and store 256 MB under each. That
# matters more than ordinary quota-busting because the relay DB shares the /data volume
# with ``relay.db`` — the revocation registry and seat table — so one holder of a free
# 3-day trial key could fill the disk and take license verification down for EVERY
# customer (with Railway's restartPolicyMaxRetries: 10 turning that into a hard outage).
#
# Sized to be generous for real use and still bounded: 2 GB is 8 full workspaces at the
# existing per-workspace cap, and 64 workspaces is well past any plausible team.
MAX_ACCOUNT_BYTES = _nonnegative_env_int(
    "ENGRAPHIS_RELAY_MAX_ACCOUNT_BYTES", 2 * 1024 * 1024 * 1024)
MAX_WORKSPACES_PER_ACCOUNT = _nonnegative_env_int(
    "ENGRAPHIS_RELAY_MAX_WORKSPACES_PER_ACCOUNT", 64)
# Compatibility endpoint only. Current clients list names and fetch one raw bundle at a
# time, so this older base64 response is intentionally capped to prevent a single request
# from constructing a multi-hundred-megabyte JSON object in memory.
MAX_LEGACY_PULL_BYTES = 48 * 1024 * 1024

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
        or "/" in value
        or "\\" in value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        return ""
    return value


def _bearer_key(request: Request) -> str:
    scheme, separator, token = (request.headers.get("Authorization") or "").partition(" ")
    token = token.strip()
    if separator and scheme.lower() == "bearer" and len(token) <= 8192:
        return token
    return ""


#: Header the client uses to identify its device so the relay can hold it to a seat.
MACHINE_ID_HEADER = "X-Engraphis-Machine-Id"


def _machine_id(request: Request) -> str:
    value = (request.headers.get(MACHINE_ID_HEADER) or "").strip()
    if (
        not value
        or len(value) > 200
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        return ""
    return value


def _rate_limited(request: Request) -> bool:
    """True when this caller has spent its per-IP relay budget.

    Uses the relay's OWN token bucket (``license_cloud._relay_rate_ok``, keyed on
    :func:`netutil.client_ip`), NOT the ``/register`` bucket. A single legitimate sync
    round makes ~1 + up to ``MAX_BUNDLES_PER_WORKSPACE`` (64) + 1 requests back to back,
    so the 60/min register budget would 429 the tail of every large-workspace round — and
    a 429 aborts the whole pull, so the round would never converge. The relay budget is
    sized for that request profile (``RELAY_RATE_PER_MINUTE``) while still bounding how
    much pure-Python Ed25519 verify work an invalid-key flood from one address can buy.

    Fails OPEN. If the limiter cannot be imported or consulted at all — a minimal install
    without ``license_cloud``, say — the relay must keep serving paying customers.
    Silently losing a DoS guard is bad; turning a guard's own failure into a total sync
    outage is worse, and it would hand an attacker a much cheaper way to take the relay
    down than flooding it.
    """
    try:
        from engraphis.inspector import license_cloud
        return not license_cloud._relay_rate_ok(netutil.client_ip(request))
    except Exception:  # noqa: BLE001 — see "fails OPEN" above
        return False


def _authorize(request: Request):
    """Verify the caller's license server-side. Returns (license, account_id).

    Raises :class:`LicenseError` (rendered as 402 by the app's exception handler) if the
    key is missing, malformed, expired, wrong-plan, or revoked, and
    :class:`HTTPException` 429 when the caller outruns the relay burst budget above —
    checked FIRST, so an invalid-key flood is rejected before it can buy any signature
    verification. That ordering matters more here than on the license endpoints: several
    relay handlers are sync ``def``s, so each in-flight request also pins one of the
    ASGI threadpool's finite workers for the duration of the verify.

    Team seat enforcement lives HERE, on vendor hardware — this is the only truly
    non-bypassable gate. A Team license is paid per seat and must not be shareable beyond
    its ``seats`` count, so every Team sync call must present the device's machine id and
    hold a live seat: the relay reclaims idle seats, then claims/refreshes this device's
    seat, refusing (402) once ``seats`` distinct devices are active at once. An idle device
    frees its seat automatically, so seats float without ever exceeding the cap. Pro (the
    individual multi-device tier) is intentionally not device-capped at the relay: its
    value is one person syncing their own machines, and ``account_id`` already isolates it
    from other customers."""
    if _rate_limited(request):
        raise HTTPException(
            status_code=429,
            detail={"error": "too many sync attempts — try again shortly"},
            headers={"Retry-After": "60"})
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


def _store_bundle(account_id: str, workspace: str, name: str,
                  payload: Union[bytes, bytearray]) -> tuple[Optional[str], int]:
    """Atomically enforce per-workspace AND per-account quotas, then upsert one bundle.

    ``BEGIN IMMEDIATE`` closes the count/size TOCTOU race: concurrent first-time pushes
    cannot both observe spare quota and then oversubscribe the same account/workspace.
    Every check below runs inside that one transaction for the same reason — the
    account-wide totals are as racy as the per-workspace ones.
    Returns ``(error, status)`` where ``error is None`` means success.
    """
    conn = _conn()
    previous_isolation = conn.isolation_level
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        usage = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(LENGTH(data)), 0) AS total, "
            "COALESCE(MAX(CASE WHEN name=? THEN LENGTH(data) ELSE 0 END), 0) AS replacing, "
            "COALESCE(MAX(CASE WHEN name=? THEN 1 ELSE 0 END), 0) AS replacing_exists "
            "FROM sync_bundles WHERE account_id=? AND workspace_id=?",
            (name, name, account_id, workspace),
        ).fetchone()
        replacing = int(usage["replacing"] or 0)
        replacing_exists = bool(usage["replacing_exists"])
        if not replacing_exists and int(usage["n"] or 0) >= MAX_BUNDLES_PER_WORKSPACE:
            conn.execute("ROLLBACK")
            return "workspace has too many device bundles", 413
        projected = int(usage["total"] or 0) - replacing + len(payload)
        if projected > MAX_WORKSPACE_BYTES:
            conn.execute("ROLLBACK")
            return "workspace relay storage limit exceeded", 413

        # Account-wide ceilings. Without these the per-workspace caps bound nothing:
        # workspace_id is caller-supplied, so an account can create unlimited ids.
        account = conn.execute(
            "SELECT COALESCE(SUM(LENGTH(data)), 0) AS total, "
            "COUNT(DISTINCT workspace_id) AS workspaces "
            "FROM sync_bundles WHERE account_id=?",
            (account_id,),
        ).fetchone()
        # Workspace COUNT is checked before total bytes so that a push which trips both
        # reports the structural limit ("you have too many workspaces") rather than the
        # incidental one ("storage full") — the former tells the operator what to
        # actually change.
        if MAX_WORKSPACES_PER_ACCOUNT > 0 and int(usage["n"] or 0) == 0:
            # Only a push that creates a NEW workspace can grow the workspace count;
            # writing another bundle into an existing one must never be refused here.
            if int(account["workspaces"] or 0) >= MAX_WORKSPACES_PER_ACCOUNT:
                conn.execute("ROLLBACK")
                return "account has too many synced workspaces", 413
        if MAX_ACCOUNT_BYTES > 0:
            # Subtract the row being replaced so re-pushing an existing bundle at the
            # ceiling still succeeds — otherwise an account that legitimately reached the
            # cap could never sync again, only delete.
            account_projected = (
                int(account["total"] or 0) - replacing + len(payload))
            if account_projected > MAX_ACCOUNT_BYTES:
                conn.execute("ROLLBACK")
                return "account relay storage limit exceeded", 413
        conn.execute(
            "INSERT INTO sync_bundles (account_id, workspace_id, name, data, updated_at) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(account_id, workspace_id, name) DO UPDATE SET "
            "  data=excluded.data, updated_at=excluded.updated_at",
            (account_id, workspace, name, payload, time.time()),
        )
        conn.execute("COMMIT")
        return None, 200
    except BaseException:
        if conn.in_transaction:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        raise
    finally:
        conn.isolation_level = previous_isolation
        conn.close()


@router.post("/{workspace_id}/bundles/{name}")
async def push_bundle(workspace_id: str, name: str, request: Request):
    """Store (overwrite) one full-state bundle for the caller's account+workspace."""
    # Signature verification and Team seat claiming are CPU/SQLite work; keep both off
    # the event loop so a slow claim cannot stall health checks or unrelated clients.
    _lic, account_id = await asyncio.to_thread(
        _authorize, request)                         # raises → 402 if unlicensed
    workspace = _safe_workspace_id(workspace_id)
    safe = _safe_name(name)
    if not workspace or not safe:
        return JSONResponse({"error": "invalid workspace or bundle name"}, status_code=400)
    declared = request.headers.get("Content-Length")
    if declared:
        try:
            declared_bytes = int(declared)
            if declared_bytes < 0:
                return JSONResponse({"error": "invalid content length"}, status_code=400)
            if declared_bytes > MAX_BUNDLE_BYTES:
                return JSONResponse({"error": "bundle too large"}, status_code=413)
        except ValueError:
            return JSONResponse({"error": "invalid content length"}, status_code=400)
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > MAX_BUNDLE_BYTES:
            return JSONResponse({"error": "bundle too large"}, status_code=413)
        data.extend(chunk)
    error, status = await asyncio.to_thread(
        _store_bundle, account_id, workspace, safe, data)
    if error:
        return JSONResponse({"error": error}, status_code=status)
    return {"ok": True, "name": safe, "bytes": len(data)}


@router.delete("/{workspace_id}/bundles/{name}")
async def delete_bundle(workspace_id: str, name: str, request: Request):
    """Delete one bundle, freeing its bytes (and its workspace, if it was the last one).

    Added 2026-07-18 alongside the per-account quotas. Without a delete route those caps
    are a one-way door: an account that reaches ``MAX_WORKSPACES_PER_ACCOUNT`` or
    ``MAX_ACCOUNT_BYTES`` could only ever overwrite existing bundles with smaller ones,
    and would otherwise need vendor DB surgery to sync again. A quota the customer cannot
    remediate is an outage, not a limit.

    Scoped to the caller's own ``account_id`` like every other bundle query, so this
    cannot touch another customer's data. Idempotent: deleting an absent bundle is 200
    with ``deleted: false``, so a retried client request is never an error.
    """
    _lic, account_id = await asyncio.to_thread(
        _authorize, request)                         # raises -> 402 if unlicensed
    workspace = _safe_workspace_id(workspace_id)
    safe = _safe_name(name)
    if not workspace or not safe:
        return JSONResponse({"error": "invalid workspace or bundle name"}, status_code=400)
    deleted = await asyncio.to_thread(
        _delete_bundle, account_id, workspace, safe)
    return {"ok": True, "name": safe, "deleted": deleted}


def _delete_bundle(account_id: str, workspace: str, name: str) -> bool:
    """Perform the blocking SQLite delete outside the ASGI event loop."""
    conn = _conn()
    try:
        cursor = conn.execute(
            "DELETE FROM sync_bundles "
            "WHERE account_id=? AND workspace_id=? AND name=?",
            (account_id, workspace, name))
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


@router.get("/{workspace_id}/bundles/{name}")
def pull_bundle(workspace_id: str, name: str, request: Request):
    """Return one opaque bundle as raw bytes.

    Current clients use this endpoint after ``/names`` so server and client memory stay
    bounded by one bundle instead of base64-materializing every device snapshot at once.
    """
    _lic, account_id = _authorize(request)
    workspace = _safe_workspace_id(workspace_id)
    safe = _safe_name(name)
    if not workspace or not safe:
        return JSONResponse({"error": "invalid workspace or bundle name"}, status_code=400)
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT data FROM sync_bundles "
            "WHERE account_id=? AND workspace_id=? AND name=?",
            (account_id, workspace, safe),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return JSONResponse({"error": "bundle not found"}, status_code=404)
    return Response(
        content=row["data"],
        media_type="application/octet-stream",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/{workspace_id}/bundles")
def pull_bundles(workspace_id: str, request: Request):
    """Legacy bulk response for older clients.

    The current client uses ``/names`` plus one raw ``/bundles/{name}`` fetch at a time.
    Keep this compatibility path bounded so it cannot construct an enormous base64 JSON
    response. Isolation is still enforced by ``account_id`` in the WHERE clause.
    """
    import base64

    _lic, account_id = _authorize(request)
    workspace = _safe_workspace_id(workspace_id)
    if not workspace:
        return JSONResponse({"error": "invalid workspace"}, status_code=400)
    conn = _conn()
    try:
        conn.execute("BEGIN")
        total = conn.execute(
            "SELECT COALESCE(SUM(LENGTH(data)), 0) AS total FROM sync_bundles "
            "WHERE account_id=? AND workspace_id=?",
            (account_id, workspace),
        ).fetchone()
        if int(total["total"] or 0) > MAX_LEGACY_PULL_BYTES:
            return JSONResponse(
                {"error": "bulk bundle response exceeds compatibility limit; "
                          "use /names and fetch bundles individually"},
                status_code=413,
            )
        rows = conn.execute(
            "SELECT name, data FROM sync_bundles "
            "WHERE account_id=? AND workspace_id=? ORDER BY name",
            (account_id, workspace),
        ).fetchall()
    finally:
        conn.close()
    return {"bundles": [
        {"name": r["name"], "data": base64.b64encode(r["data"]).decode("ascii")}
        for r in rows
    ]}


@router.get("/{workspace_id}/names")
def list_names(workspace_id: str, request: Request):
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
