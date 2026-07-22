"""Server-side registry of issued licenses + the authoritative license check.

This is the enforcement that runs on vendor-controlled hardware, which is the whole
point of server-side gating: a client can patch its local ``licensing.has_feature`` to
return ``True``, but it cannot make *this* code (running on the relay server) accept an
invalid, expired, or revoked key.

The check has four independent parts, each of which a client cannot fake:
  1. Signature — :func:`licensing.parse_key` verifies the ``ENGR1`` key against the
     pinned vendor public key. Only the holder of the vendor *private* seed (the server)
     can mint a key that verifies; a signature alone is not treated as issuance.
  2. Issuance — an active registry row must exist and its stored entitlement claims must
     match the signed payload exactly. A leaked signer cannot silently enroll new keys.
  3. Plan / expiry — the payload must grant the requested feature and not be expired.
  4. Revocation — a key we have explicitly revoked (refund, leak, abuse) is rejected
     even though its signature is still valid. This is what a signature alone can't do.

Storage is a single SQLite file (``ENGRAPHIS_RELAY_DB``, default ``~/.engraphis/relay.db``)
shared with the sync-relay bundle store.
"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import math
import os
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from engraphis.licensing import (
    License,
    LicenseError,
    ed25519_public_key,
    ed25519_sign,
    ed25519_verify,
    parse_key,
)


# A migration deadline is deliberately bounded. An accidentally distant timestamp must
# not turn the one-time compatibility path into a permanent alternate issuance channel.
LEGACY_MIGRATION_MAX_WINDOW_SECONDS = 30 * 86400

# Relay device tokens use their own domain and keypair. They are intentionally not lease
# tokens: a relay bearer and a local paid-feature lease have different audiences and
# revocation paths, so accepting one where the other belongs would be a confused-deputy
# bug. See :func:`verify_relay_device_token`.
RELAY_DEVICE_TOKEN_PREFIX = "ENGRDT1"
RELAY_DEVICE_TOKEN_TTL_DEFAULT = 3600
RELAY_DEVICE_TOKEN_TTL_MIN = 300
RELAY_DEVICE_TOKEN_TTL_MAX = 3600
RELAY_DEVICE_TOKEN_SCOPES = frozenset({"sync:read", "sync:write"})
RELAY_TOKEN_AUDIENCE_ENV = "ENGRAPHIS_RELAY_TOKEN_AUDIENCE"
RELAY_TOKEN_PREVIOUS_KEYS_ENV = "ENGRAPHIS_RELAY_TOKEN_PREVIOUS_KEYS"

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
    organization_id TEXT,          -- opaque relay tenant id; never derived from email
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
CREATE TABLE IF NOT EXISTS license_fulfillment_keys (
    retention_claim TEXT PRIMARY KEY,
    license_key TEXT NOT NULL,
    created_at REAL NOT NULL
);

"""


def _db_path(db_path: Optional[str] = None) -> str:
    return db_path or os.environ.get("ENGRAPHIS_RELAY_DB", "").strip() or _DEFAULT_DB


def _migration_organization_id(row: sqlite3.Row) -> str:
    """Deterministic, PII-free tenant id for a registry row from an older schema.

    Rows from one subscription (or, for one-off purchases, one order) retain a shared
    namespace. Rows with neither identifier are isolated by key fingerprint. New
    issuance uses randomness; determinism is confined to this one-time migration so it
    remains safe to retry after a partial rollout.
    """
    subscription_id = str(row["subscription_id"] or "").strip()
    order_id = str(row["order_id"] or "").strip()
    if subscription_id:
        basis = "subscription\0" + subscription_id
    elif order_id:
        basis = "order\0" + order_id
    else:
        basis = "key\0" + str(row["key_id"] or "")
    digest = hashlib.sha256(
        ("engraphis-organization-migration-v1\0" + basis).encode("utf-8")
    ).hexdigest()[:32]
    return "org_" + digest


def _legacy_account_id(row: sqlite3.Row) -> str:
    """Reproduce the retired email/key namespace only for one-time bundle migration."""
    email = str(row["email"] or "").strip().lower()
    basis = email or ("key:" + str(row["key_id"] or ""))
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def _valid_organization_id(value: object) -> bool:
    text = str(value or "")
    return len(text) == 36 and text.startswith("org_") and all(
        char in "0123456789abcdef" for char in text[4:]
    )


def _backfill_organization_ids(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT key_id, email, subscription_id, order_id FROM issued_licenses "
        "WHERE organization_id IS NULL OR organization_id='' ORDER BY key_id"
    ).fetchall()
    if not rows:
        return

    assignments = {row["key_id"]: _migration_organization_id(row) for row in rows}
    legacy_accounts = {row["key_id"]: _legacy_account_id(row) for row in rows}

    # Build connected components across both durable purchase identity and the retired
    # account id. If A/B shared a subscription while B/C had already collided by email,
    # all three must move together; resolving only one grouping would strand or leak part
    # of that component.
    parents = {row["key_id"]: row["key_id"] for row in rows}

    def _find(key_id: str) -> str:
        while parents[key_id] != key_id:
            parents[key_id] = parents[parents[key_id]]
            key_id = parents[key_id]
        return key_id

    def _union(left: str, right: str) -> None:
        left_root, right_root = _find(left), _find(right)
        if left_root != right_root:
            parents[max(left_root, right_root)] = min(left_root, right_root)

    seen_identity = {}
    for row in rows:
        key_id = row["key_id"]
        for identity in (
                ("candidate", assignments[key_id]),
                ("legacy", legacy_accounts[key_id])):
            previous = seen_identity.setdefault(identity, key_id)
            _union(previous, key_id)

    components = {}
    for row in rows:
        components.setdefault(_find(row["key_id"]), []).append(row["key_id"])
    for member_ids in components.values():
        candidate_ids = {assignments[key_id] for key_id in member_ids}
        if len(candidate_ids) <= 1:
            continue
        component_basis = "\0".join(sorted(
            candidate_ids | {legacy_accounts[key_id] for key_id in member_ids}
        ))
        common = "org_" + hashlib.sha256(
            ("engraphis-legacy-account-v1\0" + component_basis).encode("ascii")
        ).hexdigest()[:32]
        for key_id in member_ids:
            assignments[key_id] = common

    for row in rows:
        conn.execute(
            "UPDATE issued_licenses SET organization_id=? WHERE key_id=? "
            "AND (organization_id IS NULL OR organization_id='')",
            (assignments[row["key_id"]], row["key_id"]),
        )

    # The relay shares this database. Move existing bundle rows in the same transaction
    # so an upgrade cannot make customer data disappear behind the retired 16-char id.
    has_bundles = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sync_bundles'"
    ).fetchone()
    if has_bundles is None:
        return
    migrated_accounts = {}
    for row in rows:
        old_account_id = _legacy_account_id(row)
        new_account_id = assignments[row["key_id"]]
        previous = migrated_accounts.setdefault(old_account_id, new_account_id)
        if previous != new_account_id:
            raise sqlite3.IntegrityError(
                "legacy relay account maps to multiple organizations")
    for old_account_id, new_account_id in migrated_accounts.items():
        # Keep BLOBs inside SQLite. Fetching rows into Python here materialized an
        # account's entire (potentially multi-GB) sync history during startup. The
        # SELECT's final ``WHERE true`` also disambiguates SQLite's UPSERT parser.
        conn.execute(
            "INSERT INTO sync_bundles"
            "(account_id,workspace_id,name,data,updated_at) "
            "SELECT ?,workspace_id,name,data,updated_at FROM sync_bundles "
            "WHERE account_id=? AND true "
            "ON CONFLICT(account_id,workspace_id,name) DO UPDATE SET "
            "data=excluded.data,updated_at=excluded.updated_at "
            "WHERE excluded.updated_at>sync_bundles.updated_at",
            (new_account_id, old_account_id),
        )
        conn.execute(
            "DELETE FROM sync_bundles WHERE account_id=?", (old_account_id,))


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
    if "organization_id" not in columns:
        conn.execute("ALTER TABLE issued_licenses ADD COLUMN organization_id TEXT")
    _backfill_organization_ids(conn)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_issued_subscription "
        "ON issued_licenses(subscription_id)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_issued_order "
        "ON issued_licenses(order_id)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_issued_signer "
        "ON issued_licenses(signing_key_id)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_issued_organization "
        "ON issued_licenses(organization_id)")
    # The Python backfill above is DML, unlike the schema ALTERs. Commit it here so a
    # caller that opens the registry only to read cannot accidentally roll migration
    # state back when it closes the connection.
    conn.commit()
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


def _optional_number(value: object, *, label: str) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise LicenseError("license contains an invalid %s claim" % label) from None
    if not math.isfinite(number):
        raise LicenseError("license contains an invalid %s claim" % label)
    return number


def _license_claims(lic: License) -> dict:
    return {
        "email": str(lic.email or ""),
        "plan": str(lic.plan or ""),
        "seats": max(1, int(lic.seats or 1)),
        "issued": _optional_number(lic.issued, label="issued-at"),
        "expires": _optional_number(lic.expires, label="expiry"),
        "subscription_id": str(lic.subscription_id or ""),
        "order_id": str(lic.order_id or ""),
        "signing_key_id": str(lic.signing_key_id or ""),
    }


def _claim_value(column: str, value: object) -> object:
    if column in ("issued", "expires"):
        return _optional_number(value, label=column)
    if column == "seats":
        try:
            return max(1, int(value or 1))
        except (OverflowError, TypeError, ValueError):
            return 1
    return str(value or "")


def _claims_match(row: sqlite3.Row, lic: License, *, allow_missing: bool = False) -> bool:
    expected = _license_claims(lic)
    for column, signed_value in expected.items():
        stored = row[column]
        if allow_missing and stored is None:
            continue
        if _claim_value(column, stored) != signed_value:
            return False
    return True


def _organization_id_for_issue(
        conn: sqlite3.Connection, lic: License, preferred: Optional[str] = None) -> str:
    if preferred is not None:
        if not _valid_organization_id(preferred):
            raise ValueError("invalid organization id")
        return preferred

    # A renewal or signer-rotation replacement for the same vendor subscription/order
    # stays in the same tenant without trusting mutable contact email as identity.
    for column, value in (
            ("subscription_id", str(lic.subscription_id or "").strip()),
            ("order_id", str(lic.order_id or "").strip())):
        if not value:
            continue
        rows = conn.execute(
            "SELECT DISTINCT organization_id FROM issued_licenses "
            f"WHERE {column}=?",
            (value,),
        ).fetchall()
        if not rows:
            continue
        organizations = {str(row["organization_id"] or "") for row in rows}
        if len(organizations) != 1 or not all(
                _valid_organization_id(item) for item in organizations):
            raise LicenseError("license registry purchase identity is ambiguous")
        return next(iter(organizations))
    return "org_" + secrets.token_hex(16)


def _record_issued_on_connection(
        conn: sqlite3.Connection, lic: License, *,
        organization_id: Optional[str] = None) -> None:
    """Insert one verified issuance without weakening an existing registry record.

    Idempotent replays may fill columns absent from a pre-schema tombstone, but can never
    overwrite a non-null claim or reactivate a revoked row.
    """
    claims = _license_claims(lic)
    existing = conn.execute(
        "SELECT * FROM issued_licenses WHERE key_id=?", (lic.key_id,)
    ).fetchone()
    if existing is None:
        opaque_id = _organization_id_for_issue(conn, lic, organization_id)
        conn.execute(
            "INSERT INTO issued_licenses "
            "  (key_id, organization_id, email, plan, seats, issued, expires, "
            "   subscription_id, order_id, signing_key_id, status, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?, 'active', ?)",
            (lic.key_id, opaque_id, claims["email"], claims["plan"], claims["seats"],
             claims["issued"], claims["expires"], claims["subscription_id"],
             claims["order_id"], claims["signing_key_id"], time.time()),
        )
        return

    if not _claims_match(existing, lic, allow_missing=True):
        raise LicenseError("license registry claims do not match the signed license")
    stored_org = existing["organization_id"]
    if organization_id is not None and stored_org != organization_id:
        raise LicenseError("license registry organization does not match")
    if not _valid_organization_id(stored_org):
        raise LicenseError("license registry organization is invalid")

    missing = [column for column in claims if existing[column] is None]
    if missing:
        assignments = ", ".join(column + "=?" for column in missing)
        conn.execute(
            "UPDATE issued_licenses SET " + assignments + " WHERE key_id=?",
            tuple(claims[column] for column in missing) + (lic.key_id,),
        )


def _authoritative_row(conn: sqlite3.Connection, lic: License) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM issued_licenses WHERE key_id=?", (lic.key_id,)
    ).fetchone()


def account_id_for(lic: License, *, db_path: Optional[str] = None) -> str:
    """Return the durable opaque tenant id from authoritative issuance state.

    Contact email is intentionally absent from the derivation. A missing, revoked, or
    claim-mismatched row fails closed instead of silently creating a new namespace.
    """
    conn = connect(db_path)
    try:
        row = _authoritative_row(conn, lic)
    finally:
        conn.close()
    if row is None or row["status"] != "active":
        raise LicenseError("license is not active in the issuance registry")
    if not _claims_match(row, lic):
        raise LicenseError("license registry claims do not match the signed license")
    account_id = str(row["organization_id"] or "")
    if not _valid_organization_id(account_id):
        raise LicenseError("license registry organization is invalid")
    return account_id


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


def _clean_retention_claim(value: str) -> str:
    claim = str(value or "").strip()
    if (not claim or len(claim) > 256
            or any(ord(char) < 32 or ord(char) == 127 for char in claim)):
        raise ValueError("fulfillment retention claim is invalid")
    return claim


def record_fulfillment_key(retention_claim: str, key: str, *,
                           db_path: Optional[str] = None) -> str:
    """Atomically retain a recoverable key and make it usable in the registry.

    The outbox and registry share this SQLite database, but webhook claims live in a
    separate ledger. This journal closes the host-death window between registry insert
    and outbox enqueue: a retry gets the exact original key instead of minting another
    entitlement. The row is deleted only after the fulfillment claim is durable.
    """
    claim = _clean_retention_claim(retention_claim)
    candidate = parse_key(key)
    conn = connect(db_path)
    try:
        with conn:
            # Serialize the read-before-insert decision. A deferred transaction lets
            # two workers both observe "no journal row" and makes the loser fail its
            # unique insert after doing avoidable registry work. IMMEDIATE ensures the
            # waiter instead reads and returns the exact winner key.
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT license_key FROM license_fulfillment_keys "
                "WHERE retention_claim=?", (claim,)).fetchone()
            if existing is not None:
                # Validate stored recovery material before allowing it to cross the
                # provider boundary. Corruption fails closed instead of minting anew.
                recovered = str(existing["license_key"] or "")
                recovered_license = parse_key(recovered)
                _record_issued_on_connection(conn, recovered_license)
                if recovered_license.subscription_id:
                    conn.execute(
                        "UPDATE issued_licenses SET status='revoked', revoked_at=? "
                        "WHERE subscription_id=? AND key_id!=? AND status!='revoked'",
                        (time.time(), recovered_license.subscription_id,
                         recovered_license.key_id))
                return recovered
            _record_issued_on_connection(conn, candidate)
            if candidate.subscription_id:
                conn.execute(
                    "UPDATE issued_licenses SET status='revoked', revoked_at=? "
                    "WHERE subscription_id=? AND key_id!=? AND status!='revoked'",
                    (time.time(), candidate.subscription_id, candidate.key_id))
            conn.execute(
                "INSERT INTO license_fulfillment_keys("
                "retention_claim,license_key,created_at) VALUES (?,?,?)",
                (claim, key, time.time()))
        return key
    finally:
        conn.close()


def fulfillment_key(retention_claim: str, *, db_path: Optional[str] = None
                    ) -> Optional[str]:
    """Return crash-recovery key material for one unfinalized fulfillment."""
    claim = _clean_retention_claim(retention_claim)
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT license_key FROM license_fulfillment_keys WHERE retention_claim=?",
            (claim,)).fetchone()
        if row is None:
            return None
        value = str(row["license_key"] or "")
        parse_key(value)
        return value
    finally:
        conn.close()


def fulfillment_retention_claims(*, db_path: Optional[str] = None,
                                 after: str = "", limit: int = 1000) -> list[str]:
    """Page recovery claims for cross-database retention reconciliation."""
    page_size = max(1, min(1000, int(limit)))
    conn = connect(db_path)
    try:
        return [
            str(row["retention_claim"])
            for row in conn.execute(
                "SELECT retention_claim FROM license_fulfillment_keys "
                "WHERE retention_claim>? ORDER BY retention_claim LIMIT ?",
                (str(after or ""), page_size)).fetchall()
        ]
    finally:
        conn.close()


def redact_fulfillment_key(retention_claim: str, *,
                           db_path: Optional[str] = None) -> int:
    """Delete raw recovery material after its durable fulfillment commit."""
    claim = _clean_retention_claim(retention_claim)
    conn = connect(db_path)
    try:
        changed = conn.execute(
            "DELETE FROM license_fulfillment_keys WHERE retention_claim=?", (claim,))
        conn.commit()
        return int(changed.rowcount)
    finally:
        conn.close()


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
                "SELECT status, signing_key_id, organization_id, email, plan, seats, "
                "issued, expires, subscription_id, order_id "
                "FROM issued_licenses WHERE key_id=?",
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

            _record_issued_on_connection(
                conn, replacement, organization_id=source["organization_id"])
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
            "INSERT INTO issued_licenses"
            "(key_id, organization_id, status, created_at, revoked_at) "
            "VALUES (?, ?, 'revoked', ?, ?) "
            "ON CONFLICT(key_id) DO UPDATE SET "
            "status='revoked', revoked_at=excluded.revoked_at "
            "WHERE issued_licenses.status!='revoked'",
            (key_id, "org_" + secrets.token_hex(16), now, now),
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

    This remains a narrow status helper for admin/inventory call sites. Authorization
    uses :func:`verify_issued_license`, where an absent row fails closed."""
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT status FROM issued_licenses WHERE key_id=?", (key_id,)
        ).fetchone()
    finally:
        conn.close()
    return row is not None and row["status"] == "revoked"


def legacy_license_migration_active(*, now: Optional[float] = None) -> bool:
    """Whether the explicit, bounded pre-registry enrollment window is active.

    ``ENGRAPHIS_LEGACY_LICENSE_MIGRATION_UNTIL`` is an absolute Unix timestamp. It is
    disabled when absent, malformed, expired, or more than 30 days in the future. The
    last rule prevents a typo (or an effectively permanent date) from silently restoring
    signature-only issuance in a vendor deployment.
    """
    raw = os.environ.get("ENGRAPHIS_LEGACY_LICENSE_MIGRATION_UNTIL", "").strip()
    if not raw:
        return False
    try:
        deadline = float(raw)
        current = time.time() if now is None else float(now)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(deadline) or not math.isfinite(current):
        return False
    remaining = deadline - current
    return 0 < remaining <= LEGACY_MIGRATION_MAX_WINDOW_SECONDS


def _migrate_legacy_issuance(
        conn: sqlite3.Connection, lic: License, *, now: float) -> sqlite3.Row:
    """Atomically enroll/fill one signature-valid legacy key during the migration window."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = _authoritative_row(conn, lic)
        if row is not None and row["status"] != "active":
            raise LicenseError("this license has been revoked")
        if row is not None and not _claims_match(row, lic, allow_missing=True):
            raise LicenseError("license registry claims do not match the signed license")
        _record_issued_on_connection(conn, lic)
        conn.execute(
            "INSERT INTO control_plane_events(kind,occurred_at) VALUES (?,?)",
            ("legacy_license_migrated", now),
        )
        migrated = _authoritative_row(conn, lic)
        conn.execute("COMMIT")
        if migrated is None:
            raise LicenseError("license migration did not create an issuance record")
        return migrated
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def verify_issued_license(
        key: str, *, db_path: Optional[str] = None,
        now: Optional[float] = None) -> License:
    """Verify signed claims against one active authoritative issuance record.

    Signature validity is necessary but no longer sufficient: a leaked signing seed
    cannot mint a usable license unless the vendor registry also contains the exact
    signed entitlement. Legacy signature-only keys can be enrolled only while the
    explicit bounded migration deadline is active.
    """
    key = (key or "").strip()
    if not key:
        raise LicenseError("a license key is required")
    current = time.time() if now is None else float(now)
    lic = parse_key(key, now=current)
    conn = connect(db_path)
    try:
        row = _authoritative_row(conn, lic)
        complete_match = row is not None and _claims_match(row, lic)
        if (row is None or not complete_match) and legacy_license_migration_active(now=current):
            row = _migrate_legacy_issuance(conn, lic, now=current)
        if row is None:
            raise LicenseError("license was not issued by this service")
        if row["status"] != "active":
            raise LicenseError("this license has been revoked")
        if not _claims_match(row, lic):
            raise LicenseError("license registry claims do not match the signed license")
        organization_id = str(row["organization_id"] or "")
        if not _valid_organization_id(organization_id):
            raise LicenseError("license registry organization is invalid")
        return lic
    finally:
        conn.close()


def verify_for_feature(key: str, feature: str, *, db_path: Optional[str] = None,
                       now: Optional[float] = None) -> License:
    """THE server-side gate. Return the verified :class:`License` or raise LicenseError.

    Order: signature and expiry, matching active issuance row, then feature grant.
    The raised LicenseError carries ``feature`` for the HTTP 402 layer."""
    key = (key or "").strip()
    if not key:
        raise LicenseError("a license key is required for this feature", feature=feature)
    try:
        lic = verify_issued_license(key, db_path=db_path, now=now)
    except LicenseError as exc:
        raise LicenseError(str(exc), feature=feature) from None
    if not lic.has(feature):
        raise LicenseError(
            "this license's plan does not include '%s'" % feature, feature=feature)
    return lic


def canonical_relay_audience(value: object) -> str:
    """Return one canonical relay origin suitable for an exact token audience check."""
    raw = str(value or "").strip()
    if not raw or len(raw) > 2048:
        raise LicenseError("relay token audience is not configured")
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        raise LicenseError("relay token audience is invalid") from None
    scheme = parts.scheme.lower()
    hostname = (parts.hostname or "").strip().lower()
    if scheme not in ("http", "https") or not hostname:
        raise LicenseError("relay token audience must be an absolute HTTP(S) origin")
    if parts.username is not None or parts.password is not None \
            or parts.query or parts.fragment or parts.path not in ("", "/"):
        raise LicenseError("relay token audience must contain only an origin")
    if port is not None and port <= 0:
        raise LicenseError("relay token audience has an invalid port")
    if "\\" in parts.netloc or any(
            ord(char) < 33 or ord(char) == 127 for char in parts.netloc
    ) \
            or hostname.endswith(".") or "%" in hostname:
        raise LicenseError("relay token audience has an invalid host")
    try:
        canonical_host = hostname.encode("idna").decode("ascii")
    except UnicodeError:
        raise LicenseError("relay token audience has an invalid host") from None
    try:
        ip = ipaddress.ip_address(canonical_host)
    except ValueError:
        ip = None
    loopback = canonical_host == "localhost" or canonical_host.endswith(".localhost") \
        or (ip is not None and ip.is_loopback)
    if scheme != "https" and not loopback:
        raise LicenseError("relay token audience must use HTTPS unless it is loopback")
    if ip is not None and ip.version == 6:
        canonical_host = "[" + canonical_host + "]"
    default_port = 443 if scheme == "https" else 80
    port_suffix = "" if port is None or port == default_port else ":%d" % port
    return "%s://%s%s" % (scheme, canonical_host, port_suffix)


def relay_token_audience(value: Optional[str] = None) -> str:
    """Resolve the configured relay-token audience, failing closed when absent/invalid."""
    configured = os.environ.get(RELAY_TOKEN_AUDIENCE_ENV, "") if value is None else value
    return canonical_relay_audience(configured)


def relay_device_token_ttl_seconds() -> int:
    """Configured relay-bearer TTL, constrained to five minutes through one hour."""
    try:
        configured = int(os.environ.get(
            "ENGRAPHIS_RELAY_DEVICE_TOKEN_TTL_SECONDS",
            str(RELAY_DEVICE_TOKEN_TTL_DEFAULT),
        ))
    except (TypeError, ValueError):
        configured = RELAY_DEVICE_TOKEN_TTL_DEFAULT
    return min(RELAY_DEVICE_TOKEN_TTL_MAX, max(RELAY_DEVICE_TOKEN_TTL_MIN, configured))


def _relay_public_key(value: object) -> bytes:
    text = str(value or "").strip().lower()
    try:
        raw = bytes.fromhex(text)
    except ValueError:
        raise LicenseError("relay device-token public key is invalid") from None
    if len(raw) != 32:
        raise LicenseError("relay device-token public key must be 32 bytes")
    return raw


def relay_token_verifiers(*, now: Optional[float] = None) -> tuple[dict, ...]:
    """Return current and time-bounded previous relay-token verifier metadata.

    ``ENGRAPHIS_RELAY_TOKEN_PREVIOUS_KEYS`` is strict JSON:
    ``[{"public_key":"<64 hex>","issued_before":<epoch>,"not_after":<epoch>}]``.
    A previous key accepts only tokens issued strictly before its cutoff and expires
    entirely no later than one token TTL afterward. Expired metadata must be removed;
    the retired unbounded public-key list is rejected if present.
    """
    current_time = time.time() if now is None else float(now)
    if not math.isfinite(current_time):
        raise LicenseError("relay-token key metadata check time is invalid")
    current = _relay_public_key(os.environ.get("ENGRAPHIS_RELAY_TOKEN_PUBKEY", ""))
    if os.environ.get("ENGRAPHIS_RELAY_TOKEN_PREVIOUS_PUBKEYS", "").strip():
        raise LicenseError(
            "unbounded previous relay keys are unsupported; configure cutoff metadata")
    raw_previous = os.environ.get(RELAY_TOKEN_PREVIOUS_KEYS_ENV, "").strip()
    if len(raw_previous) > 16384:
        raise LicenseError("previous relay-token key metadata is too large")
    if not raw_previous:
        parsed = []
    else:
        try:
            parsed = json.loads(
                raw_previous,
                parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
            )
        except (ValueError, TypeError, RecursionError):
            raise LicenseError("previous relay-token key metadata is invalid JSON") from None
        if not isinstance(parsed, list):
            raise LicenseError("previous relay-token key metadata must be a JSON list")
    if len(parsed) > 3:
        raise LicenseError("too many previous relay-token keys are configured")

    verifiers = [{
        "public_key": current,
        "signing_key_id": current.hex()[:16],
        "issued_before": None,
        "not_after": None,
    }]
    seen = {current}
    required_fields = {"public_key", "issued_before", "not_after"}
    for item in parsed:
        if not isinstance(item, dict) or set(item) != required_fields:
            raise LicenseError("previous relay-token key metadata has invalid fields")
        public_key = _relay_public_key(item["public_key"])
        issued_before = item["issued_before"]
        not_after = item["not_after"]
        if isinstance(issued_before, bool) or not isinstance(issued_before, int) \
                or isinstance(not_after, bool) or not isinstance(not_after, int):
            raise LicenseError("previous relay-token key cutoffs must be integer epochs")
        if issued_before <= 0 or issued_before > current_time + 300 \
                or not_after <= issued_before \
                or not_after - issued_before > RELAY_DEVICE_TOKEN_TTL_MAX:
            raise LicenseError("previous relay-token key cutoff window is invalid")
        if not_after <= current_time:
            raise LicenseError(
                "previous relay-token key metadata has expired; remove the retired key")
        if public_key in seen:
            raise LicenseError("relay device-token public keys must be unique")
        seen.add(public_key)
        verifiers.append({
            "public_key": public_key,
            "signing_key_id": public_key.hex()[:16],
            "issued_before": issued_before,
            "not_after": not_after,
        })
    return tuple(verifiers)


def relay_token_public_keys() -> tuple[bytes, ...]:
    """Compatibility view of :func:`relay_token_verifiers`, current key first."""
    return tuple(item["public_key"] for item in relay_token_verifiers())


def _token_b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _token_b64decode(text: str) -> bytes:
    if not text or any(char not in (
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
    ) for char in text):
        raise ValueError("invalid base64url")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def compose_relay_device_token(
        lic: License, account_id: str, device_id: str, secret: bytes, *,
        scopes: Optional[list[str]] = None, now: Optional[float] = None,
        ttl_seconds: Optional[int] = None,
        audience: Optional[str] = None) -> tuple[str, dict]:
    """Sign a short-lived, scoped relay bearer with the dedicated token keypair."""
    if len(secret) != 32:
        raise ValueError("relay device-token signing key must be 32 bytes")
    if lic.plan != "pro":
        raise ValueError("relay device tokens are available only for Pro licenses")
    canonical_audience = relay_token_audience(audience)
    if not _valid_organization_id(account_id):
        raise ValueError("invalid relay account id")
    device_id = str(device_id or "").strip()
    if not device_id or len(device_id) > 200 or any(
            ord(char) < 32 or ord(char) == 127 for char in device_id):
        raise ValueError("invalid relay device id")
    requested = set(RELAY_DEVICE_TOKEN_SCOPES if scopes is None else scopes)
    if not requested or not requested.issubset(RELAY_DEVICE_TOKEN_SCOPES):
        raise ValueError("invalid relay device-token scopes")
    issued = time.time() if now is None else float(now)
    if not math.isfinite(issued):
        raise ValueError("invalid relay device-token issue time")
    ttl = relay_device_token_ttl_seconds() if ttl_seconds is None else int(ttl_seconds)
    ttl = min(RELAY_DEVICE_TOKEN_TTL_MAX, max(RELAY_DEVICE_TOKEN_TTL_MIN, ttl))
    signing_key_id = ed25519_public_key(secret).hex()[:16]
    expires = issued + ttl
    if lic.expires is not None:
        expires = min(expires, _optional_number(lic.expires, label="expiry") or expires)
    if expires <= issued:
        raise ValueError("cannot mint a relay token for an expired license")
    issued_epoch, expires_epoch = int(issued), int(expires)
    if expires_epoch <= issued_epoch:
        raise ValueError("license expires too soon to mint a relay device token")
    payload = {
        "v": 1,
        "typ": "relay_device",
        "aud": canonical_audience,
        "account_id": account_id,
        "key_id": lic.key_id,
        "device_id": device_id,
        "scopes": sorted(requested),
        "issued": issued_epoch,
        "expires": expires_epoch,
        "jti": secrets.token_urlsafe(18),
        "signing_key_id": signing_key_id,
    }
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = ed25519_sign(secret, body)
    token = "%s.%s.%s" % (
        RELAY_DEVICE_TOKEN_PREFIX,
        _token_b64encode(body),
        _token_b64encode(signature),
    )
    return token, payload


def verify_relay_device_token(
        token: str, required_scope: Optional[str] = None, *,
        db_path: Optional[str] = None, now: Optional[float] = None,
        check_registry: bool = True,
        expected_audience: Optional[str] = None) -> dict:
    """Verify a scoped relay bearer and its current registry entitlement.

    The token contains neither contact email nor raw license material. Vendor-control-
    plane callers keep ``check_registry=True`` for immediate revocation. The isolated
    managed-relay data plane must pass ``check_registry=False`` explicitly; signature,
    audience, one-hour maximum TTL, license-capped expiry, and scope still fail closed,
    while revocation converges when that short token expires.
    """
    raw_token = str(token or "").strip()
    if len(raw_token) > 8192:
        raise LicenseError("relay device token is too large")
    parts = raw_token.split(".")
    if len(parts) != 3 or parts[0] != RELAY_DEVICE_TOKEN_PREFIX:
        raise LicenseError("not an Engraphis relay device token")
    try:
        body = _token_b64decode(parts[1])
        signature = _token_b64decode(parts[2])
    except (ValueError, base64.binascii.Error):
        raise LicenseError("relay device token is not valid base64url") from None
    current = time.time() if now is None else float(now)
    if not math.isfinite(current):
        raise LicenseError("relay device token has invalid timing claims")
    verifier = next(
        (item for item in relay_token_verifiers(now=current)
         if ed25519_verify(item["public_key"], body, signature)),
        None,
    )
    if verifier is None:
        raise LicenseError("relay device-token signature is invalid")
    try:
        payload = json.loads(
            body.decode("utf-8"),
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise LicenseError("relay device-token payload is invalid") from None
    if not isinstance(payload, dict) or payload.get("v") != 1 \
            or payload.get("typ") != "relay_device":
        raise LicenseError("unsupported relay device-token payload")
    expected = relay_token_audience(expected_audience)
    signed_audience = payload.get("aud")
    if not isinstance(signed_audience, str):
        raise LicenseError("relay device token has no audience")
    canonical_signed_audience = canonical_relay_audience(signed_audience)
    if signed_audience != canonical_signed_audience \
            or canonical_signed_audience != expected:
        raise LicenseError("relay device token has the wrong audience")

    issued = _optional_number(payload.get("issued"), label="token issue time")
    expires = _optional_number(payload.get("expires"), label="token expiry")
    if issued is None or expires is None:
        raise LicenseError("relay device token has invalid timing claims")
    if current >= expires:
        raise LicenseError("relay device token has expired")
    if issued > current + 300 or expires <= issued \
            or expires - issued > RELAY_DEVICE_TOKEN_TTL_MAX:
        raise LicenseError("relay device token has invalid timing claims")
    if verifier["issued_before"] is not None:
        if issued >= verifier["issued_before"] or expires > verifier["not_after"] \
                or current >= verifier["not_after"]:
            raise LicenseError("relay device token was signed outside its rotation window")

    signing_key_id = str(payload.get("signing_key_id") or "").strip().lower()
    if signing_key_id != verifier["signing_key_id"]:
        raise LicenseError("relay device-token signer id does not match")
    account_id = str(payload.get("account_id") or "")
    key_id = str(payload.get("key_id") or "")
    device_id = str(payload.get("device_id") or "")
    jti = str(payload.get("jti") or "")
    if not _valid_organization_id(account_id):
        raise LicenseError("relay device token has an invalid account id")
    if len(key_id) != 12 or any(char not in "0123456789abcdef" for char in key_id):
        raise LicenseError("relay device token has an invalid key id")
    if not device_id or len(device_id) > 200 or any(
            ord(char) < 32 or ord(char) == 127 for char in device_id):
        raise LicenseError("relay device token has an invalid device id")
    if not jti or len(jti) > 128 or any(ord(char) < 33 or ord(char) == 127 for char in jti):
        raise LicenseError("relay device token has an invalid token id")
    scopes = payload.get("scopes")
    if not isinstance(scopes, list) or any(not isinstance(scope, str) for scope in scopes):
        raise LicenseError("relay device token has invalid scopes")
    scope_set = set(scopes)
    if len(scopes) != len(scope_set) or not scope_set \
            or not scope_set.issubset(RELAY_DEVICE_TOKEN_SCOPES):
        raise LicenseError("relay device token has invalid scopes")
    if required_scope is not None and required_scope not in scope_set:
        raise LicenseError("relay device token lacks the required scope")

    if check_registry:
        conn = connect(db_path)
        try:
            row = conn.execute(
                "SELECT status, organization_id, plan, expires FROM issued_licenses "
                "WHERE key_id=?",
                (key_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None or row["status"] != "active":
            raise LicenseError("relay device token is no longer entitled")
        if row["organization_id"] != account_id or row["plan"] != "pro":
            raise LicenseError("relay device token does not match its entitlement")
        registry_expiry = _optional_number(row["expires"], label="registry expiry")
        if registry_expiry is not None and current > registry_expiry:
            raise LicenseError("relay device token entitlement has expired")
    return payload


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
