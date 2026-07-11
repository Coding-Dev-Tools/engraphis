"""Server-side registry of issued licenses + the authoritative license check.

This is the enforcement that runs on vendor-controlled hardware, which is the whole
point of server-side gating: a client can patch its local ``licensing.has_feature`` to
return ``True``, but it cannot make *this* code (running on the relay server) accept an
invalid, expired, or revoked key.

The check has three independent parts, each of which a client cannot fake:
  1. Signature — :func:`licensing.parse_key` verifies the ``ENGR1`` key against the
     pinned vendor public key. Only the holder of the vendor *private* seed (the server)
     can mint a key that verifies, so a valid signature is proof we issued it.
  2. Plan / expiry — the payload must grant the requested feature and not be expired.
  3. Revocation — a key we have explicitly revoked (refund, leak, abuse) is rejected
     even though its signature is still valid. This is what a signature alone can't do.

Storage is a single SQLite file (``ENGRAPHIS_RELAY_DB``, default ``~/.engraphis/relay.db``)
shared with the sync-relay bundle store.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from pathlib import Path
from typing import Optional

from engraphis.licensing import License, LicenseError, parse_key

_DEFAULT_DB = str(Path.home() / ".engraphis" / "relay.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS issued_licenses (
    key_id     TEXT PRIMARY KEY,   -- licensing key_id (sha256(key)[:12]); never the key
    email      TEXT,
    plan       TEXT,
    seats      INTEGER,
    issued     REAL,
    expires    REAL,
    status     TEXT NOT NULL DEFAULT 'active',   -- 'active' | 'revoked'
    created_at REAL NOT NULL,
    revoked_at REAL
);
"""


def _db_path(db_path: Optional[str] = None) -> str:
    return db_path or os.environ.get("ENGRAPHIS_RELAY_DB", "").strip() or _DEFAULT_DB


def connect(db_path: Optional[str] = None) -> sqlite3.Connection:
    """Open the relay DB (creating parent dir + schema). Callers close it."""
    path = _db_path(db_path)
    if path != ":memory:":
        Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def account_id_for(lic: License) -> str:
    """Stable, non-PII namespace for a customer's bundles.

    Derived from the license email (all of a buyer's devices share one email, so they
    sync together); hashed so the sync store never holds a raw address. Falls back to
    the key fingerprint if a key somehow carries no email."""
    basis = (lic.email or "").strip().lower() or ("key:" + lic.key_id)
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def record_issued(key: str, *, db_path: Optional[str] = None) -> str:
    """Record a freshly issued key in the registry (idempotent). Returns its key_id.

    Called from the fulfillment path (:func:`webhooks.issue_key`). Never raises on a
    duplicate — re-issuing the same key just refreshes the row."""
    lic = parse_key(key)
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO issued_licenses "
            "  (key_id, email, plan, seats, issued, expires, status, created_at) "
            "VALUES (?,?,?,?,?,?, 'active', ?) "
            "ON CONFLICT(key_id) DO UPDATE SET "
            "  email=excluded.email, plan=excluded.plan, seats=excluded.seats, "
            "  issued=excluded.issued, expires=excluded.expires",
            (lic.key_id, lic.email, lic.plan, lic.seats, lic.issued, lic.expires,
             time.time()),
        )
        conn.commit()
    finally:
        conn.close()
    return lic.key_id


def revoke(key_id: str, *, db_path: Optional[str] = None) -> bool:
    """Mark a key revoked. Returns True if a row was updated.

    A revoked key still has a valid signature but is refused by :func:`verify_for_feature`
    — use for refunds, leaked keys, or abuse."""
    conn = connect(db_path)
    try:
        cur = conn.execute(
            "UPDATE issued_licenses SET status='revoked', revoked_at=? "
            "WHERE key_id=? AND status!='revoked'",
            (time.time(), key_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def is_revoked(key_id: str, *, db_path: Optional[str] = None) -> bool:
    """True only if the key is present AND explicitly revoked.

    A key absent from the registry is NOT treated as revoked: only the vendor can mint a
    validly-signed key, so a good signature already proves issuance (keys sold before the
    registry existed, or trials, simply have no row). Revocation is an explicit overlay."""
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT status FROM issued_licenses WHERE key_id=?", (key_id,)
        ).fetchone()
    finally:
        conn.close()
    return row is not None and row["status"] == "revoked"


def verify_for_feature(key: str, feature: str, *, db_path: Optional[str] = None,
                       now: Optional[float] = None) -> License:
    """THE server-side gate. Return the verified :class:`License` or raise LicenseError.

    Order: signature + expiry (:func:`parse_key`) → plan grants ``feature`` → not revoked.
    The raised LicenseError carries ``feature`` so the HTTP layer renders a 402."""
    key = (key or "").strip()
    if not key:
        raise LicenseError("a license key is required for this feature", feature=feature)
    lic = parse_key(key, now=now)                      # signature + expiry + known plan
    if not lic.has(feature):
        raise LicenseError(
            "this license's plan does not include '%s'" % feature, feature=feature)
    if is_revoked(lic.key_id, db_path=db_path):
        raise LicenseError("this license has been revoked", feature=feature)
    return lic
