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
import math
import os
import sqlite3
import time
from pathlib import Path
from typing import Optional

from engraphis.licensing import License, LicenseError, parse_key

def _state_dir() -> Path:
    base = os.environ.get("ENGRAPHIS_STATE_DIR", "").strip()
    return Path(base) if base else (Path.home() / ".engraphis")


# Registry/relay DB default lives under the state dir so revocations persist on the same
# volume as the rest of the license state (ENGRAPHIS_RELAY_DB still overrides explicitly).
_DEFAULT_DB = str(_state_dir() / "relay.db")

_REG_SCHEMA = """
CREATE TABLE IF NOT EXISTS registrations (
    key_id     TEXT NOT NULL,
    machine_id TEXT NOT NULL,
    first_seen REAL NOT NULL,
    last_seen  REAL NOT NULL,
    PRIMARY KEY (key_id, machine_id)
);
"""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS issued_licenses (
    key_id     TEXT PRIMARY KEY,   -- licensing key_id (sha256(key)[:12]); never the key
    email      TEXT,
    plan       TEXT,
    seats      INTEGER,
    issued     REAL,
    expires    REAL,
    subscription_id TEXT,
    order_id   TEXT,
    signing_key_id TEXT,
    status     TEXT NOT NULL DEFAULT 'active',   -- 'active' | 'revoked'
    created_at REAL NOT NULL,
    revoked_at REAL
);
CREATE TABLE IF NOT EXISTS signer_rotation_reissues (
    source_key_id             TEXT NOT NULL,
    replacement_key_id        TEXT NOT NULL UNIQUE,
    source_signing_key_id     TEXT NOT NULL,
    replacement_signing_key_id TEXT NOT NULL,
    created_at                REAL NOT NULL,
    PRIMARY KEY (source_key_id, replacement_signing_key_id)
);
CREATE TABLE IF NOT EXISTS control_plane_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    occurred_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_control_plane_events_kind_time
    ON control_plane_events(kind, occurred_at);

"""


def _db_path(db_path: Optional[str] = None) -> str:
    return db_path or os.environ.get("ENGRAPHIS_RELAY_DB", "").strip() or _DEFAULT_DB


def connect(db_path: Optional[str] = None) -> sqlite3.Connection:
    """Open the relay DB (creating parent dir + schema). Callers close it."""
    path = _db_path(db_path)
    if path != ":memory:":
        database = Path(path).expanduser()
        database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # SQLite otherwise creates the PII-bearing registry using the process umask,
        # which can yield 0644 on ordinary hosts. Pre-create it owner-only before SQLite
        # opens it, and tighten an existing file after upgrades as defense in depth.
        descriptor = os.open(str(database), os.O_RDWR | os.O_CREAT, 0o600)
        os.close(descriptor)
        try:
            os.chmod(database, 0o600)
        except OSError:
            pass
        path = str(database)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    # Wait (up to 5s) for a competing writer's lock rather than failing with
    # "database is locked"; seat claims take a short IMMEDIATE write lock (see claim_seat).
    conn.execute("PRAGMA busy_timeout=5000")
    if path != ":memory:":
        # WAL: readers never block the writer (and vice-versa), so many team devices
        # hitting the relay at once — bundle push/pull plus seat claim/refresh — don't
        # serialize on a single database lock the way rollback-journal mode forces. It's a
        # persistent per-DB setting (harmless to re-assert each connect) and requires a
        # local filesystem, which Railway/Fly volumes are. NORMAL sync is the right
        # durability/throughput trade for a single-instance relay behind a volume.
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError as exc:
            # Concurrent first requests may race while one connection flips the
            # persistent journal mode. The winner establishes WAL; the others can safely
            # continue and will observe it on their next connection.
            if "locked" not in str(exc).lower():
                conn.close()
                raise
        conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(_SCHEMA)
    conn.executescript(_REG_SCHEMA)
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(issued_licenses)").fetchall()}
    if "subscription_id" not in columns:
        conn.execute("ALTER TABLE issued_licenses ADD COLUMN subscription_id TEXT")
    if "order_id" not in columns:
        conn.execute("ALTER TABLE issued_licenses ADD COLUMN order_id TEXT")
    if "signing_key_id" not in columns:
        # Pre-v1.0 rows cannot be backfilled from a public-key fingerprint because the
        # registry deliberately stores no raw license material. They remain NULL and are
        # reported as ``unknown`` by inventory(), which forces a conservative reissue.
        conn.execute("ALTER TABLE issued_licenses ADD COLUMN signing_key_id TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_issued_subscription "
        "ON issued_licenses(subscription_id)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_issued_order "
        "ON issued_licenses(order_id)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_issued_signer "
        "ON issued_licenses(signing_key_id)")
    return conn


def inventory(db_path: Optional[str] = None) -> dict:
    """Return PII-free counts for the pre-rotation issued-key audit."""
    conn = connect(db_path)
    try:
        statuses = {
            str(row["status"]): int(row["n"])
            for row in conn.execute(
                "SELECT status, COUNT(*) AS n FROM issued_licenses GROUP BY status")
        }
        plans = {
            str(row["plan"] or "unknown"): int(row["n"])
            for row in conn.execute(
                "SELECT plan, COUNT(*) AS n FROM issued_licenses GROUP BY plan")
        }
        active_key_ids = [
            str(row["key_id"])
            for row in conn.execute(
                "SELECT key_id FROM issued_licenses WHERE status='active' ORDER BY key_id")
        ]
        registrations = int(conn.execute(
            "SELECT COUNT(*) AS n FROM registrations").fetchone()["n"])
        signing_key_ids = {
            str(row["signing_key_id"] or "unknown"): int(row["n"])
            for row in conn.execute(
                "SELECT signing_key_id, COUNT(*) AS n FROM issued_licenses "
                "GROUP BY signing_key_id")
        }
        rotation_reissues = int(conn.execute(
            "SELECT COUNT(*) AS n FROM signer_rotation_reissues").fetchone()["n"])
    finally:
        conn.close()
    total = sum(statuses.values())
    return {
        "issued_total": total,
        "active": statuses.get("active", 0),
        "revoked": statuses.get("revoked", 0),
        "other_status": total - statuses.get("active", 0) - statuses.get("revoked", 0),
        "plans": plans,
        "active_key_ids": active_key_ids,
        "signing_key_ids": signing_key_ids,
        "registered_machines": registrations,
        "rotation_reissues": rotation_reissues,
        "rotation_requires_migration": statuses.get("active", 0) > 0,
    }


def account_id_for(lic: License) -> str:
    """Stable, non-PII namespace for a customer's bundles.

    Derived from the license email (all of a buyer's devices share one email, so they
    sync together); hashed so the sync store never holds a raw address. Falls back to
    the key fingerprint if a key somehow carries no email."""
    basis = (lic.email or "").strip().lower() or ("key:" + lic.key_id)
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def _record_issued_on_connection(conn: sqlite3.Connection, lic: License) -> None:
    """Insert or refresh one verified license without changing a revocation tombstone."""
    conn.execute(
        "INSERT INTO issued_licenses "
        "  (key_id, email, plan, seats, issued, expires, subscription_id, order_id, "
        "   signing_key_id, status, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?, 'active', ?) "
        "ON CONFLICT(key_id) DO UPDATE SET "
        "  email=excluded.email, plan=excluded.plan, seats=excluded.seats, "
        "  issued=excluded.issued, expires=excluded.expires, "
        "  subscription_id=excluded.subscription_id, order_id=excluded.order_id, "
        "  signing_key_id=excluded.signing_key_id",
        (lic.key_id, lic.email, lic.plan, lic.seats, lic.issued, lic.expires,
         lic.subscription_id or None, lic.order_id or None,
         lic.signing_key_id or None, time.time()),
    )


def record_issued(key: str, *, db_path: Optional[str] = None) -> str:
    """Record a freshly issued key in the registry (idempotent). Returns its key_id.

    Called from the fulfillment path (:func:`webhooks.issue_key`). Never raises on a
    duplicate, and never reactivates a revoked key.
    """
    lic = parse_key(key)
    conn = connect(db_path)
    try:
        _record_issued_on_connection(conn, lic)
        conn.commit()
    finally:
        conn.close()
    return lic.key_id


def signer_rotation_state(replacement_signing_key_id: str, *,
                          db_path: Optional[str] = None) -> dict:
    """Return registry state needed by the offline signer-reissue command.

    ``candidates`` contains customer PII and is vendor-admin data; unlike
    :func:`inventory`, this result must never be exposed through an HTTP endpoint.
    Completed audit rows let an interrupted command resume without duplicating keys.
    """
    target = (replacement_signing_key_id or "").strip().lower()
    if len(target) != 16 or any(char not in "0123456789abcdef" for char in target):
        raise ValueError("replacement signing-key id must be 16 lowercase hex characters")
    conn = connect(db_path)
    try:
        candidates = [
            dict(row) for row in conn.execute(
                "SELECT issued.* FROM issued_licenses AS issued "
                "WHERE issued.status='active' AND NOT EXISTS ("
                "  SELECT 1 FROM signer_rotation_reissues AS rotation "
                "  WHERE rotation.replacement_signing_key_id=? "
                "    AND (rotation.source_key_id=issued.key_id "
                "         OR rotation.replacement_key_id=issued.key_id)"
                ") ORDER BY issued.key_id",
                (target,),
            )
        ]
        completed = [
            dict(row) for row in conn.execute(
                "SELECT source_key_id, replacement_key_id, source_signing_key_id, "
                "replacement_signing_key_id, created_at "
                "FROM signer_rotation_reissues WHERE replacement_signing_key_id=? "
                "ORDER BY source_key_id",
                (target,),
            )
        ]
    finally:
        conn.close()
    return {"candidates": candidates, "completed": completed}


def record_signer_rotation(
        reissues: list[tuple[str, str, str]], *, replacement_signing_key_id: str,
        db_path: Optional[str] = None, now: Optional[float] = None) -> int:
    """Atomically record signer replacements while leaving every source key active.

    Each tuple is ``(source_key_id, source_signing_key_id, replacement_key)``.
    Source revocation is deliberately separate and grace-period gated.
    """
    target = (replacement_signing_key_id or "").strip().lower()
    if len(target) != 16 or any(char not in "0123456789abcdef" for char in target):
        raise ValueError("replacement signing-key id must be 16 lowercase hex characters")

    prepared = []
    source_ids = set()
    replacement_ids = set()
    for source_key_id, source_signing_key_id, replacement_key in reissues:
        source_id = (source_key_id or "").strip()
        source_signer = (source_signing_key_id or "").strip().lower()
        if not source_id or len(source_signer) != 16 \
                or any(char not in "0123456789abcdef" for char in source_signer):
            raise ValueError("source key id and 16-character hex signer id are required")
        replacement = parse_key(replacement_key, now=0)
        if replacement.signing_key_id != target:
            raise ValueError("replacement key was not signed by the requested signer")
        if source_id in source_ids or replacement.key_id in replacement_ids:
            raise ValueError("duplicate source or replacement key in rotation batch")
        if source_id == replacement.key_id:
            raise ValueError("source and replacement key ids must differ")
        source_ids.add(source_id)
        replacement_ids.add(replacement.key_id)
        prepared.append((source_id, source_signer, replacement_key, replacement))

    if not prepared:
        return 0

    created_at = time.time() if now is None else float(now)
    conn = connect(db_path)
    inserted = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        for source_id, source_signer, _replacement_key, replacement in prepared:
            existing = conn.execute(
                "SELECT replacement_key_id, source_signing_key_id "
                "FROM signer_rotation_reissues "
                "WHERE source_key_id=? AND replacement_signing_key_id=?",
                (source_id, target),
            ).fetchone()
            if existing is not None:
                if existing["replacement_key_id"] != replacement.key_id \
                        or existing["source_signing_key_id"] != source_signer:
                    raise ValueError("rotation audit conflicts with the requested replacement")
                continue

            source = conn.execute(
                "SELECT status, signing_key_id, email, plan, seats, issued, expires, "
                "subscription_id, order_id FROM issued_licenses WHERE key_id=?",
                (source_id,),
            ).fetchone()
            if source is None or source["status"] != "active":
                raise ValueError("every signer-rotation source must still be active")
            stored_signer = str(source["signing_key_id"] or "").strip().lower()
            if stored_signer and stored_signer != source_signer:
                raise ValueError("source signer does not match the registry")
            source_entitlement = (
                str(source["email"] or ""), str(source["plan"] or ""),
                max(1, int(source["seats"] or 1)), source["issued"], source["expires"],
                str(source["subscription_id"] or ""), str(source["order_id"] or ""),
            )
            replacement_entitlement = (
                replacement.email, replacement.plan, replacement.seats,
                replacement.issued, replacement.expires,
                replacement.subscription_id, replacement.order_id,
            )
            if source_entitlement != replacement_entitlement:
                raise ValueError("replacement key changes the source entitlement")

            _record_issued_on_connection(conn, replacement)
            replacement_row = conn.execute(
                "SELECT status FROM issued_licenses WHERE key_id=?",
                (replacement.key_id,),
            ).fetchone()
            if replacement_row is None or replacement_row["status"] != "active":
                raise ValueError("replacement key already has a revocation tombstone")
            conn.execute(
                "UPDATE issued_licenses SET signing_key_id=COALESCE(signing_key_id, ?) "
                "WHERE key_id=?",
                (source_signer, source_id),
            )
            conn.execute(
                "INSERT INTO signer_rotation_reissues "
                "(source_key_id, replacement_key_id, source_signing_key_id, "
                " replacement_signing_key_id, created_at) VALUES (?,?,?,?,?)",
                (source_id, replacement.key_id, source_signer, target, created_at),
            )
            inserted += 1
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return inserted


def retire_signer_rotation_sources(
        source_key_ids: list[str], *, replacement_signing_key_id: str,
        db_path: Optional[str] = None, grace_seconds: float = 30 * 86400,
        now: Optional[float] = None) -> int:
    """Revoke audited source keys only after their replacements and grace period exist."""
    target = (replacement_signing_key_id or "").strip().lower()
    if len(target) != 16 or any(char not in "0123456789abcdef" for char in target):
        raise ValueError("replacement signing-key id must be 16 lowercase hex characters")
    source_ids = sorted(set((key_id or "").strip() for key_id in source_key_ids))
    if not source_ids or any(not key_id for key_id in source_ids):
        raise ValueError("at least one non-empty source key id is required")

    timestamp = time.time() if now is None else float(now)
    minimum_age = max(30 * 86400, float(grace_seconds))
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        for source_id in source_ids:
            audit = conn.execute(
                "SELECT replacement_key_id, created_at FROM signer_rotation_reissues "
                "WHERE source_key_id=? AND replacement_signing_key_id=?",
                (source_id, target),
            ).fetchone()
            if audit is None:
                raise ValueError("source key has no audited signer replacement")
            if timestamp - float(audit["created_at"]) < minimum_age:
                raise ValueError("the 30-day signer-rotation grace period has not elapsed")
            replacement = conn.execute(
                "SELECT status FROM issued_licenses WHERE key_id=?",
                (audit["replacement_key_id"],),
            ).fetchone()
            if replacement is None or replacement["status"] != "active":
                raise ValueError("an active replacement is required before source retirement")

        placeholders = ",".join("?" for _ in source_ids)
        cursor = conn.execute(
            "UPDATE issued_licenses SET status='revoked', revoked_at=? "
            f"WHERE status='active' AND key_id IN ({placeholders})",
            (timestamp, *source_ids),
        )
        conn.execute("COMMIT")
        return cursor.rowcount
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def revoke(key_id: str, *, db_path: Optional[str] = None) -> bool:
    """Persist a revocation tombstone. Returns True when state changed.

    The tombstone also covers valid keys that were never recorded because an earlier
    best-effort registry write failed. A later :func:`record_issued` fills its metadata
    without reactivating it.
    """
    now = time.time()
    conn = connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO issued_licenses(key_id, status, created_at, revoked_at) "
            "VALUES (?, 'revoked', ?, ?) "
            "ON CONFLICT(key_id) DO UPDATE SET "
            "status='revoked', revoked_at=excluded.revoked_at "
            "WHERE issued_licenses.status!='revoked'",
            (key_id, now, now),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()



def revoke_superseded(subscription_id: str, keep_key_id: str, *,
                      db_path: Optional[str] = None) -> int:
    """Revoke older active keys after a replacement key is durably registered.

    Refuses to revoke anything unless ``keep_key_id`` is already recorded for the same
    subscription, so a failed best-effort write cannot strand a customer without a key.
    """
    subscription_id = (subscription_id or "").strip()[:128]
    keep_key_id = (keep_key_id or "").strip()
    if not subscription_id or not keep_key_id:
        return 0
    conn = connect(db_path)
    try:
        with conn:
            replacement = conn.execute(
                "SELECT 1 FROM issued_licenses "
                "WHERE key_id=? AND subscription_id=? AND status='active'",
                (keep_key_id, subscription_id),
            ).fetchone()
            if replacement is None:
                return 0
            cur = conn.execute(
                "UPDATE issued_licenses SET status='revoked', revoked_at=? "
                "WHERE subscription_id=? AND key_id!=? AND status!='revoked'",
                (time.time(), subscription_id, keep_key_id),
            )
        return cur.rowcount
    finally:
        conn.close()


def revoke_by_subscription(subscription_id: str, *,
                           db_path: Optional[str] = None) -> int:
    """Revoke EVERY active key issued for *subscription_id*. Returns the number changed.

    Used by the negative half of the billing lifecycle (refund / chargeback / hard
    cancellation): unlike :func:`revoke_superseded`, this keeps no key — the customer is
    no longer entitled to any. Keys are cloud-enforced (``enforce=cloud``), so the
    revocation takes effect at the next lease renewal (within one lease TTL, ~24h).
    Idempotent: a second call simply changes nothing.
    """
    subscription_id = (subscription_id or "").strip()[:128]
    if not subscription_id:
        return 0
    conn = connect(db_path)
    try:
        with conn:
            cur = conn.execute(
                "UPDATE issued_licenses SET status='revoked', revoked_at=? "
                "WHERE subscription_id=? AND status!='revoked'",
                (time.time(), subscription_id),
            )
        return cur.rowcount
    finally:
        conn.close()


def revoke_by_order(order_id: str, *,
                    db_path: Optional[str] = None) -> int:
    """Revoke every active key issued for a Polar order. Returns keys changed."""
    order_id = (order_id or "").strip()[:128]
    if not order_id:
        return 0
    conn = connect(db_path)
    try:
        with conn:
            cur = conn.execute(
                "UPDATE issued_licenses SET status='revoked', revoked_at=? "
                "WHERE order_id=? AND status!='revoked'",
                (time.time(), order_id),
            )
        return cur.rowcount
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


def record_control_plane_event(kind: str, *, db_path: Optional[str] = None,
                               now: Optional[float] = None) -> None:
    """Record a content-free counter used by readiness alerts."""
    clean = (kind or "").strip().lower()[:48]
    allowed = "abcdefghijklmnopqrstuvwxyz0123456789_.-"
    if not clean or any(char not in allowed for char in clean):
        raise ValueError("invalid control-plane event kind")
    now = time.time() if now is None else float(now)
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO control_plane_events(kind,occurred_at) VALUES (?,?)", (clean, now))
        conn.execute("DELETE FROM control_plane_events WHERE occurred_at<?", (now - 7 * 86400,))
        conn.commit()
    finally:
        conn.close()


def rejected_lease_health(*, db_path: Optional[str] = None,
                          now: Optional[float] = None) -> bool:
    """Fail readiness on a sustained recent lease/sync rejection rate."""
    now = time.time() if now is None else float(now)
    try:
        threshold = max(1, int(os.environ.get(
            "ENGRAPHIS_REJECTED_LEASE_ALERT_THRESHOLD", "50")))
        window = max(60, int(os.environ.get(
            "ENGRAPHIS_REJECTED_LEASE_ALERT_WINDOW_SECONDS", "3600")))
        conn = connect(db_path)
        try:
            count = int(conn.execute(
                "SELECT COUNT(*) FROM control_plane_events "
                "WHERE kind='lease_rejected' AND occurred_at>=?", (now - window,)
            ).fetchone()[0])
        finally:
            conn.close()
        return count < threshold
    except (OSError, TypeError, ValueError, sqlite3.Error):
        return False


# ── device registrations & seat accounting ─────────────────────────────────────────────
# A "seat" is one concurrently-active device. The per-license cap (``License.seats``) is
# enforced here, on vendor hardware, so it holds regardless of what a patched client does.
# Seats FLOAT: a device that stops checking in has its lease lapse, and its seat is then
# reclaimed automatically so the cap self-heals (no permanent lockout on a dead/retired
# machine). The concurrency guarantee ("no more than N live at once") is never weakened by
# reclamation because it is enforced at claim time; reclamation only frees provably-idle
# seats. This is the single source of truth for seat logic — the register endpoint and the
# sync relay both call it, so they can never drift.

LEASE_TTL_HOURS_DEFAULT = 24


def lease_ttl_seconds() -> int:
    """Lease validity window in seconds (``ENGRAPHIS_LEASE_TTL_HOURS``, default 24h).

    This IS the offline-grace window: online-only enforcement means a paying device keeps
    working without the server for at most one lease TTL, after which it must re-register
    (so a revoked key stops within ~24h). Floored at 5 minutes so a misconfiguration can
    never mint 0-second leases."""
    try:
        hours = float(os.environ.get("ENGRAPHIS_LEASE_TTL_HOURS", "").strip()
                      or LEASE_TTL_HOURS_DEFAULT)
    except (OverflowError, ValueError):
        hours = LEASE_TTL_HOURS_DEFAULT
    if not math.isfinite(hours):
        hours = LEASE_TTL_HOURS_DEFAULT
    return max(300, int(hours * 3600))


def seat_reclaim_seconds() -> int:
    """Idle window after which a device's seat is auto-reclaimed.

    A live device refreshes its registration at least once per lease TTL (the client only
    renews when its lease has lapsed, and the relay refreshes ``last_seen`` on every sync),
    so anything silent for *two* full TTLs has certainly lost its lease. The 2x multiplier
    is deliberately conservative: the concurrency cap is enforced instantly at claim time,
    so a longer reclaim window never permits over-subscription — it only guarantees we
    never reclaim a live, about-to-renew device. Tunable via ``ENGRAPHIS_SEAT_RECLAIM_MULT``
    (>= 1.0)."""
    try:
        mult = float(os.environ.get("ENGRAPHIS_SEAT_RECLAIM_MULT", "").strip() or 2.0)
    except (OverflowError, ValueError):
        mult = 2.0
    if not math.isfinite(mult):
        mult = 2.0
    mult = max(1.0, mult)
    return int(lease_ttl_seconds() * mult)


def _clean_machine_id(machine_id: str) -> str:
    """Normalize an untrusted, client-supplied machine id (bound length; strip).

    Honest limit (open-core): the id is a soft identifier, not an unforgeable
    attestation. Colluders who deliberately share ONE machine id occupy a single
    seat between them; this raises the bar against casual key-sharing (N distinct
    devices = N seats) without claiming to defeat a determined insider. The
    non-bypassable guarantee is the count: no more than ``seats`` distinct live
    ids can hold seats at once (enforced atomically in claim_seat)."""
    return (machine_id or "").strip()[:128]


def reclaim_stale_seats(conn: sqlite3.Connection, key_id: str, *,
                        older_than: Optional[float] = None,
                        now: Optional[float] = None) -> int:
    """Delete registrations whose lease has certainly lapsed (idle > ``older_than`` s).

    Returns the number of seats freed. Frees seats held by dead/retired devices so the
    per-key cap self-heals; a live device is never affected because it refreshes
    ``last_seen`` well within the window."""
    now = time.time() if now is None else now
    older_than = seat_reclaim_seconds() if older_than is None else older_than
    cur = conn.execute("DELETE FROM registrations WHERE key_id=? AND last_seen < ?",
                       (key_id, now - older_than))
    return cur.rowcount


def active_seat_count(conn: sqlite3.Connection, key_id: str) -> int:
    """Number of registered (seat-holding) devices for a key."""
    return int(conn.execute("SELECT COUNT(*) AS n FROM registrations WHERE key_id=?",
                            (key_id,)).fetchone()["n"])


def claim_seat(conn: sqlite3.Connection, lic: License, machine_id: str, *,
               now: Optional[float] = None, reclaim: bool = True) -> None:
    """Ensure ``machine_id`` holds a live seat under ``lic``, or raise ``LicenseError``.

    Idempotent for an already-registered device: it just refreshes ``last_seen`` — which is
    also the keep-alive that holds the seat while the device is active. Reclaims idle seats
    first so a dead device never permanently blocks a new one. Raises (rendered as a 402)
    when the license's seat cap is already full of *live* devices."""
    now = time.time() if now is None else now
    machine_id = _clean_machine_id(machine_id)
    if not machine_id:
        raise LicenseError("machine_id required")
    conn.executescript(_REG_SCHEMA)                      # DDL (own txn) before we take the lock
    seats = max(1, int(getattr(lic, "seats", 1) or 1))
    # The cap check and the insert MUST be atomic: without a held write lock, two concurrent
    # claims for two new devices could both read count < seats and both insert, overshooting
    # the cap (a TOCTOU race that would make a shared key exceed its paid seats). BEGIN
    # IMMEDIATE grabs the RESERVED write lock up front, so a competing claim blocks (up to
    # busy_timeout) until we commit and then observes our row. We drive transactions manually
    # here (isolation_level=None) and restore the connection's prior mode afterwards.
    prev_iso = conn.isolation_level
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if reclaim:
                reclaim_stale_seats(conn, lic.key_id, now=now)
            seen = conn.execute(
                "SELECT 1 FROM registrations WHERE key_id=? AND machine_id=?",
                (lic.key_id, machine_id)).fetchone()
            if seen is None:
                if active_seat_count(conn, lic.key_id) >= seats:
                    raise LicenseError(
                        "seat limit reached for this license (%d seat(s) in use). An idle "
                        "device frees its seat automatically; or deactivate one now." % seats)
                conn.execute(
                    "INSERT INTO registrations (key_id, machine_id, first_seen, last_seen) "
                    "VALUES (?,?,?,?)", (lic.key_id, machine_id, now, now))
            else:
                conn.execute(
                    "UPDATE registrations SET last_seen=? WHERE key_id=? AND machine_id=?",
                    (now, lic.key_id, machine_id))
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
    finally:
        conn.isolation_level = prev_iso


def release_seat(conn: sqlite3.Connection, key_id: str, machine_id: str) -> bool:
    """Free a seat by removing a device registration. Returns True if a row was removed."""
    cur = conn.execute("DELETE FROM registrations WHERE key_id=? AND machine_id=?",
                       (key_id, _clean_machine_id(machine_id)))
    conn.commit()
    return cur.rowcount > 0
