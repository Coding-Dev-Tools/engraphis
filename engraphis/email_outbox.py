"""Durable transactional-email outbox shared by all commercial workflows.

Message bodies and recipients stay in the vendor database because they are required to
retry delivery.  Operations endpoints return only redacted metadata. Provider payloads
are reduced to stable event identifiers and states; raw webhook bodies are never stored.
"""
from __future__ import annotations

import hashlib
import math
import os
import secrets
import sqlite3
import time
from typing import Callable, Optional

from engraphis.inspector import license_registry

MAX_ATTEMPTS = 5
MAX_MANUAL_REQUEUES = 2
CLAIM_LEASE_SECONDS = 300
MAX_IDEMPOTENCY_KEY_CHARS = 256
MAX_KIND_CHARS = 48
MAX_RECIPIENT_CHARS = 384
MAX_SUBJECT_CHARS = 240
MAX_TEXT_BODY_BYTES = 256 * 1024

_SCHEMA = """
CREATE TABLE IF NOT EXISTS email_outbox (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT UNIQUE,
    kind TEXT NOT NULL,
    recipient TEXT NOT NULL,
    subject TEXT NOT NULL,
    text_body TEXT NOT NULL,
    reply_to TEXT,
    retention_claim TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    manual_requeues INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    next_attempt_at REAL NOT NULL,
    provider TEXT,
    provider_message_id TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    sent_at REAL
);
CREATE INDEX IF NOT EXISTS email_outbox_due_idx
    ON email_outbox(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS email_outbox_provider_idx
    ON email_outbox(provider_message_id);
CREATE TABLE IF NOT EXISTS email_delivery_events (
    provider_event_id TEXT PRIMARY KEY,
    provider_message_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    occurred_at REAL NOT NULL,
    recorded_at REAL NOT NULL
);
"""


def fulfillment_retention_claim(fulfillment_id: str) -> str:
    """Return the shared, bounded claim id used across the two commercial databases."""
    value = str(fulfillment_id or "")
    if not value:
        raise ValueError("fulfillment id is required")
    candidate = "ful:" + value
    if (len(candidate) <= MAX_IDEMPOTENCY_KEY_CHARS
            and not any(ord(char) < 32 or ord(char) == 127 for char in candidate)):
        return candidate
    return "ful:sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _connect() -> sqlite3.Connection:
    conn = license_registry.connect()
    conn.executescript(_SCHEMA)
    columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(email_outbox)").fetchall()
    }
    if "retention_claim" not in columns:
        conn.execute(
            "ALTER TABLE email_outbox ADD COLUMN "
            "retention_claim TEXT NOT NULL DEFAULT ''")
    if "manual_requeues" not in columns:
        conn.execute(
            "ALTER TABLE email_outbox ADD COLUMN "
            "manual_requeues INTEGER NOT NULL DEFAULT 0")
    # Purchase rows can be mapped to the durable Polar fulfillment claim without
    # recovering or exposing the key. This makes an interrupted pre-upgrade delivery
    # eligible for the same post-finalization cleanup as newly-enqueued messages.
    # Keep this backfill restart-idempotent. A host death can persist the ALTER before
    # this UPDATE; conditioning it on "column added in this process" would then strand
    # a recoverable pre-upgrade key without its fulfillment claim forever.
    legacy_purchase = conn.execute(
        "SELECT 1 FROM email_outbox WHERE retention_claim='' AND text_body<>'' "
        "AND idempotency_key LIKE 'purchase-license:%' LIMIT 1").fetchone()
    if legacy_purchase is not None:
        conn.execute(
            "UPDATE email_outbox SET retention_claim='ful:order:' || "
            "substr(idempotency_key, length('purchase-license:') + 1) "
            "WHERE retention_claim='' AND "
            "text_body<>'' AND idempotency_key LIKE 'purchase-license:%'")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS email_outbox_retention_idx "
        "ON email_outbox(retention_claim, status)")
    conn.commit()
    return conn


def _bounded_header(value: str, *, name: str, maximum: int,
                    required: bool = False) -> str:
    """Validate a value that may later become an email or provider header."""
    if not isinstance(value, str):
        raise ValueError("%s must be text" % name)
    cleaned = value.strip()
    if required and not cleaned:
        raise ValueError("%s is required" % name)
    if len(cleaned) > maximum:
        raise ValueError("%s is too long" % name)
    if any(ord(char) < 32 or ord(char) == 127 for char in cleaned):
        raise ValueError("%s contains control characters" % name)
    return cleaned


def enqueue(kind: str, recipient: str, subject: str, text_body: str, *,
            reply_to: Optional[str] = None, idempotency_key: str = "",
            retention_claim: str = "", max_attempts: int = MAX_ATTEMPTS) -> str:
    """Persist a message and return its stable id.

    Supplying an idempotency key makes repeated webhook/request delivery return the
    original message rather than enqueueing duplicates.
    """
    clean_kind = _bounded_header(
        kind or "transactional", name="kind", maximum=MAX_KIND_CHARS, required=True)
    clean_recipient = _bounded_header(
        recipient, name="recipient", maximum=MAX_RECIPIENT_CHARS, required=True).lower()
    clean_subject = _bounded_header(
        subject, name="subject", maximum=MAX_SUBJECT_CHARS, required=True)
    clean_reply_to = _bounded_header(
        reply_to or "", name="reply_to", maximum=MAX_RECIPIENT_CHARS) or None
    clean_idem = _bounded_header(
        idempotency_key or "", name="idempotency_key",
        maximum=MAX_IDEMPOTENCY_KEY_CHARS) or None
    clean_retention = _bounded_header(
        retention_claim or "", name="retention_claim",
        maximum=MAX_IDEMPOTENCY_KEY_CHARS)
    if not isinstance(text_body, str):
        raise ValueError("text_body must be text")
    if not text_body or len(text_body.encode("utf-8")) > MAX_TEXT_BODY_BYTES:
        raise ValueError("text_body is empty or too large")
    try:
        attempts_limit = max(1, min(10, int(max_attempts)))
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError("max_attempts must be a finite integer") from exc
    now = time.time()
    msg_id = "eml_" + secrets.token_hex(12)
    conn = _connect()
    try:
        if clean_idem:
            row = conn.execute(
                "SELECT id,kind,recipient,retention_claim FROM email_outbox "
                "WHERE idempotency_key=?",
                (clean_idem,)).fetchone()
            if row:
                return _reuse_idempotent_message(
                    conn, row, clean_kind, clean_recipient, clean_retention)
        try:
            conn.execute(
                "INSERT INTO email_outbox(id,idempotency_key,kind,recipient,subject,"
                "text_body,reply_to,retention_claim,max_attempts,next_attempt_at,"
                "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (msg_id, clean_idem, clean_kind, clean_recipient, clean_subject,
                 text_body, clean_reply_to, clean_retention, attempts_limit,
                 now, now, now))
            conn.commit()
            return msg_id
        except sqlite3.IntegrityError:
            if not clean_idem:
                raise
            # Another worker may have inserted this idempotency key after our
            # pre-check. End the failed INSERT transaction before reading the winner,
            # then apply the same compatibility and retention-claim checks as above.
            conn.rollback()
            row = conn.execute(
                "SELECT id,kind,recipient,retention_claim FROM email_outbox "
                "WHERE idempotency_key=?",
                (clean_idem,)).fetchone()
            if row:
                return _reuse_idempotent_message(
                    conn, row, clean_kind, clean_recipient, clean_retention)
            raise
    finally:
        conn.close()


def _reuse_idempotent_message(conn: sqlite3.Connection, row: sqlite3.Row,
                              kind: str, recipient: str,
                              retention_claim: str) -> str:
    """Return a compatible idempotent row, or fail closed on key collisions."""
    if str(row["kind"]) != kind or str(row["recipient"]).lower() != recipient:
        raise ValueError(
            "idempotency key is already bound to another message kind or recipient")
    existing_claim = str(row["retention_claim"] or "")
    if retention_claim and existing_claim and existing_claim != retention_claim:
        raise ValueError(
            "idempotency key is already bound to another retention claim")
    if retention_claim and not existing_claim:
        changed = conn.execute(
            "UPDATE email_outbox SET retention_claim=? "
            "WHERE id=? AND retention_claim=''",
            (retention_claim, row["id"]))
        conn.commit()
        if changed.rowcount != 1:
            # A concurrent compatible retry may have attached the same claim. Re-read
            # and verify; never silently bind one business operation to another claim.
            current = conn.execute(
                "SELECT retention_claim FROM email_outbox WHERE id=?",
                (row["id"],)).fetchone()
            if current is None or str(current["retention_claim"] or "") != retention_claim:
                raise ValueError(
                    "idempotency key is already bound to another retention claim")
    return str(row["id"])


def _claim(message_id: str) -> Optional[dict]:
    conn = _connect()
    previous = conn.isolation_level
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        now = time.time()
        row = conn.execute(
            "SELECT * FROM email_outbox WHERE id=?", (message_id,)).fetchone()
        claimable = row is not None and (
            (row["status"] in ("pending", "retry")
             and float(row["next_attempt_at"]) <= now)
            or (row["status"] == "sending" and float(row["next_attempt_at"]) <= now)
        )
        if not claimable:
            conn.execute("COMMIT")
            return None
        if int(row["attempts"]) >= int(row["max_attempts"]):
            if row["status"] == "sending":
                conn.execute(
                    "UPDATE email_outbox SET status='failed',last_error=?,updated_at=? "
                    "WHERE id=? AND status='sending'",
                    ("DeliveryLeaseExpired", now, message_id))
            conn.execute("COMMIT")
            return None
        attempts = int(row["attempts"]) + 1
        conn.execute(
            "UPDATE email_outbox SET status='sending',attempts=?,next_attempt_at=?,"
            "updated_at=? WHERE id=?",
            (attempts, now + CLAIM_LEASE_SECONDS, now, message_id))
        conn.execute("COMMIT")
        out = dict(row)
        out["attempts"] = attempts
        return out
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.isolation_level = previous
        conn.close()


def _provider_state(conn: sqlite3.Connection, provider_message_id: str) -> str:
    """Return the strongest recorded terminal state for a provider message."""
    if not provider_message_id:
        return "sent"
    states = {
        str(row[0]).strip().lower()
        for row in conn.execute(
            "SELECT event_type FROM email_delivery_events WHERE provider_message_id=?",
            (provider_message_id,)).fetchall()
    }
    if states & {"email.complained", "complained"}:
        return "complained"
    if states & {"email.bounced", "bounced"}:
        return "bounced"
    if states & {"email.delivered", "delivered"}:
        return "delivered"
    return "sent"


def _body_has_license_key(text_body: Optional[str]) -> bool:
    """True when the body carries a signed license key (ENGR1.<sig>.<payload>).

    Mirrors inspector/webhooks.py::_existing_license_delivery() extraction so a
    purchase/renewal outbox row stays the recoverable source of truth until the
    Polar fulfillment claim is finalized; only non-recoverable bodies are redacted
    after a successful send.
    """
    for entry in str(text_body or "").splitlines():
        candidate = entry.strip()
        if candidate.startswith("ENGR1.") and candidate.count(".") == 2:
            return True
    return False


def _retention_claim_fulfilled(claim_id: str) -> bool:
    if not claim_id:
        return False
    try:
        from engraphis.billing import webhook_claim_fulfilled
        return bool(webhook_claim_fulfilled(claim_id))
    except Exception:
        # The key is the crash-recovery source of truth. State-store uncertainty must
        # retain it, never guess that the independent fulfillment commit succeeded.
        return False


def redact_retention_claim(claim_id: str) -> int:
    """Clear terminal license bodies after their durable fulfillment claim commits."""
    clean_claim = _bounded_header(
        claim_id or "", name="retention_claim",
        maximum=MAX_IDEMPOTENCY_KEY_CHARS, required=True)
    conn = _connect()
    try:
        changed = conn.execute(
            "UPDATE email_outbox SET text_body='',reply_to=NULL,retention_claim='',"
            "updated_at=? WHERE retention_claim=? "
            "AND status IN ('sent','delivered','bounced','complained')",
            (time.time(), clean_claim))
        conn.commit()
        return int(changed.rowcount)
    finally:
        conn.close()


def redact_finalized_retention_claims() -> int:
    """Recover cleanup after a crash between fulfillment commit and body redaction."""
    conn = _connect()
    try:
        claims = {
            str(row[0]) for row in conn.execute(
                "SELECT DISTINCT retention_claim FROM email_outbox "
                "WHERE retention_claim<>'' AND text_body<>'' "
                "AND status IN ('sent','delivered','bounced','complained')"
            ).fetchall()
        }
    finally:
        conn.close()
    cleaned = 0
    cleaned_claims = set()
    cleanup_error = None

    def clean_claim(claim: str) -> None:
        nonlocal cleaned, cleanup_error
        if not _retention_claim_fulfilled(claim):
            return
        changed = 0
        try:
            changed += license_registry.redact_fulfillment_key(claim)
        except Exception as exc:  # retain readiness failure after trying both stores
            cleanup_error = cleanup_error or exc
        try:
            changed += redact_retention_claim(claim)
        except Exception as exc:
            cleanup_error = cleanup_error or exc
        if changed > 0 and claim not in cleaned_claims:
            cleaned_claims.add(claim)
            cleaned += 1

    for claim in claims:
        clean_claim(claim)

    # The registry journal is an independent plaintext recovery copy. A host can die
    # after outbox redaction but before journal deletion, so it must independently feed
    # reconciliation rather than being inferred from surviving outbox rows. Page by the
    # stable claim key: a fixed LIMIT would permanently starve newer finalized journals
    # behind a large set of legitimate, still-unfulfilled rows.
    cursor = ""
    while True:
        page = license_registry.fulfillment_retention_claims(after=cursor, limit=1000)
        if not page:
            break
        for claim in page:
            clean_claim(claim)
        cursor = page[-1]
        if len(page) < 1000:
            break
    if cleanup_error is not None:
        raise RuntimeError("could not reconcile finalized license retention") \
            from cleanup_error
    return cleaned


def deliver_now(message_id: str,
                deliverer: Callable[
                    [str, str, str, Optional[str], str], tuple[str, str]]) -> bool:
    """Attempt one claimed message and persist the outcome.

    ``deliverer`` receives recipient, subject, body, reply-to, and the stable outbox
    message id (for provider-side idempotency), then returns ``(provider,
    provider_message_id)``. Delivery exceptions are re-raised after the retry state is
    safely persisted so existing callers keep their fail/rollback policy.
    """
    message = _claim(message_id)
    if message is None:
        return False
    try:
        provider, provider_id = deliverer(
            message["recipient"], message["subject"], message["text_body"],
            message.get("reply_to"), message_id)
    except Exception as exc:
        attempts = int(message["attempts"])
        terminal = attempts >= int(message["max_attempts"])
        delay = min(3600, 60 * (2 ** max(0, attempts - 1)))
        conn = _connect()
        try:
            conn.execute(
                "UPDATE email_outbox SET status=?,next_attempt_at=?,last_error=?,"
                "updated_at=? WHERE id=? AND status='sending' AND attempts=?",
                ("failed" if terminal else "retry", time.time() + delay,
                 type(exc).__name__[:80], time.time(), message_id, attempts))
            conn.commit()
        finally:
            conn.close()
        raise
    conn = _connect()
    previous = conn.isolation_level
    conn.isolation_level = None
    try:
        provider_name = (provider or "unknown")[:40]
        provider_message_id = (provider_id or "")[:160]
        conn.execute("BEGIN IMMEDIATE")
        now = time.time()
        # Keep a signed key only while an explicit durable fulfillment claim still
        # needs it. Messages without a claim and messages whose claim already committed
        # are redacted in the same transaction as the terminal delivery state.
        retains_key = (
            _body_has_license_key(message["text_body"])
            and bool(message.get("retention_claim"))
            and not _retention_claim_fulfilled(str(message["retention_claim"]))
        )
        redact_sql = "" if retains_key else ",text_body='',reply_to=NULL,retention_claim=''"
        updated = conn.execute(
            "UPDATE email_outbox SET status='sent',provider=?,provider_message_id=?,"
            "last_error='',sent_at=?,updated_at=?" + redact_sql + " "
            "WHERE id=? AND status='sending' AND attempts=?",
            (provider_name, provider_message_id, now, now,
             message_id, int(message["attempts"])))
        if updated.rowcount == 1:
            state = _provider_state(conn, provider_message_id)
            if state != "sent":
                conn.execute(
                    "UPDATE email_outbox SET status=?,updated_at=? WHERE id=?",
                    (state, time.time(), message_id))
        conn.commit()
        was_updated = updated.rowcount == 1
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.isolation_level = previous
        conn.close()
    return was_updated


def process_due(deliverer: Callable[
                    [str, str, str, Optional[str], str], tuple[str, str]],
                *, limit: int = 20) -> dict:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id FROM email_outbox WHERE status IN ('pending','retry','sending') "
            "AND next_attempt_at<=? ORDER BY next_attempt_at LIMIT ?",
            (time.time(), max(1, min(100, int(limit))))).fetchall()
    finally:
        conn.close()
    sent = failed = 0
    for row in rows:
        try:
            sent += int(deliver_now(str(row["id"]), deliverer))
        except Exception:
            failed += 1
    return {"processed": len(rows), "sent": sent, "failed": failed}


def _selected_message_ids(message_ids, *, limit: int) -> list[str]:
    """Return bounded, de-duplicated outbox IDs for explicit operator actions."""
    selected = []
    seen = set()
    if isinstance(message_ids, str):
        message_ids = [message_ids]
    for value in message_ids:
        item = str(value or "").strip()
        if (
            item
            and item not in seen
            and item.startswith("eml_")
            and len(item) <= 64
            and all(
                char.isascii() and (char.isalnum() or char in "_-")
                for char in item
            )
        ):
            seen.add(item)
            selected.append(item)
        if len(selected) >= max(1, min(100, int(limit))):
            break
    return selected


def requeue_failed(message_ids, *, limit: int = 100) -> int:
    """Retry explicitly selected failures within a permanent manual-requeue cap."""
    selected = _selected_message_ids(message_ids, limit=limit)
    if not selected:
        return 0
    conn = _connect()
    previous = conn.isolation_level
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        now = time.time()
        requeued = 0
        for message_id in selected:
            changed = conn.execute(
                "UPDATE email_outbox SET status='retry',attempts=0,next_attempt_at=?,"
                "last_error='',manual_requeues=manual_requeues+1,updated_at=? "
                "WHERE id=? AND status='failed' AND manual_requeues<?",
                (now, now, message_id, MAX_MANUAL_REQUEUES))
            requeued += int(changed.rowcount)
        conn.execute("COMMIT")
        return requeued
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.isolation_level = previous
        conn.close()


def resolve_failed(message_ids, *, limit: int = 100) -> int:
    """Acknowledge manually reconciled failures and erase their recovery material.

    A paid-license row is eligible only after its cross-database fulfillment tombstone
    is durable. That preserves exact-key recovery if an operator tries to close a row
    while Polar can still retry the fulfillment. The caller is responsible for obtaining
    an explicit manual-delivery/reconciliation acknowledgement before invoking this.
    """
    selected = _selected_message_ids(message_ids, limit=limit)
    if not selected:
        return 0
    conn = _connect()
    previous = conn.isolation_level
    conn.isolation_level = None
    try:
        # Fulfilled is permanent, so it is safe to verify outside the relay-DB write
        # lock. Store uncertainty returns false and retains every recovery copy.
        rows = conn.execute(
            "SELECT id,retention_claim FROM email_outbox WHERE status='failed' AND id IN ("
            + ",".join("?" for _item in selected) + ")",
            selected).fetchall()
        eligible = {
            str(row["id"]): str(row["retention_claim"] or "")
            for row in rows
            if not row["retention_claim"]
            or _retention_claim_fulfilled(str(row["retention_claim"]))
        }
        if not eligible:
            return 0
        conn.execute("BEGIN IMMEDIATE")
        now = time.time()
        resolved = 0
        for message_id in selected:
            if message_id not in eligible:
                continue
            claim = eligible[message_id]
            changed = conn.execute(
                "UPDATE email_outbox SET status='resolved',recipient='',subject='',"
                "text_body='',reply_to=NULL,retention_claim='',last_error='',updated_at=? "
                "WHERE id=? AND status='failed' AND retention_claim=?",
                (now, message_id, claim))
            if changed.rowcount != 1:
                continue
            if claim:
                # The registry journal shares this database, so recovery-key deletion
                # and outbox redaction commit atomically.
                conn.execute(
                    "DELETE FROM license_fulfillment_keys WHERE retention_claim=?",
                    (claim,))
            resolved += 1
        conn.execute("COMMIT")
        return resolved
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.isolation_level = previous
        conn.close()


def record_provider_event(provider_event_id: str, provider_message_id: str,
                          event_type: str, *, occurred_at: Optional[float] = None) -> bool:
    """Idempotently reduce a verified provider event to an outbox state transition."""
    event_id = (provider_event_id or "").strip()
    message_id = (provider_message_id or "").strip()
    normalized = (event_type or "").strip().lower()
    if not event_id or len(event_id) > 255 \
            or not message_id or len(message_id) > 160 \
            or not normalized or len(normalized) > 80 \
            or any(ord(char) < 33 or ord(char) == 127
                   for value in (event_id, message_id, normalized) for char in value):
        return False
    mapping = {
        "email.delivered": "delivered",
        "email.bounced": "bounced",
        "email.complained": "complained",
        "delivered": "delivered",
        "bounced": "bounced",
        "complained": "complained",
    }
    state = mapping.get(normalized)
    conn = _connect()
    try:
        happened = time.time() if occurred_at is None else float(occurred_at)
        if not math.isfinite(happened):
            return False
        inserted = conn.execute(
            "INSERT OR IGNORE INTO email_delivery_events(provider_event_id,"
            "provider_message_id,event_type,occurred_at,recorded_at) VALUES (?,?,?,?,?)",
            (event_id, message_id, normalized, happened, time.time()))
        if inserted.rowcount == 0:
            # Svix/Resend is at-least-once.  The stable provider event ID is the
            # idempotency boundary: a replay must never reinterpret a different body and
            # mutate another message after the original event was already reduced.
            conn.commit()
            return True
        if state:
            protected = {
                "delivered": ("bounced", "complained"),
                "bounced": ("complained",),
                "complained": (),
            }[state]
            placeholders = ",".join("?" for _value in protected)
            suffix = (" AND status NOT IN (%s)" % placeholders) if protected else ""
            conn.execute(
                "UPDATE email_outbox SET status=?,updated_at=? "
                "WHERE provider_message_id=?" + suffix,
                (state, time.time(), message_id, *protected))
        conn.commit()
        return True
    finally:
        conn.close()


def health() -> dict:
    now = time.time()
    window_start = now - 86400
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS backlog, MIN(created_at) AS oldest FROM email_outbox "
            "WHERE status IN ('pending','retry','sending')").fetchone()
        terminal = int(conn.execute(
            "SELECT COUNT(*) FROM email_outbox WHERE status='failed'").fetchone()[0])
        bounced = int(conn.execute(
            "SELECT COUNT(*) FROM email_outbox WHERE status IN ('bounced','complained')"
        ).fetchone()[0])
        sent_recent = int(conn.execute(
            "SELECT COUNT(*) FROM email_outbox WHERE sent_at>=?", (window_start,)
        ).fetchone()[0])
        bounced_recent = int(conn.execute(
            "SELECT COUNT(DISTINCT o.id) FROM email_delivery_events e "
            "JOIN email_outbox o ON o.provider_message_id=e.provider_message_id "
            "WHERE e.occurred_at>=? AND o.sent_at>=? "
            "AND e.event_type IN ('email.bounced','email.complained',"
            "'bounced','complained')", (window_start, window_start)).fetchone()[0])
    finally:
        conn.close()
    backlog = int(row["backlog"] or 0)
    age = max(0, int(time.time() - row["oldest"])) if row["oldest"] else 0
    config_valid = True
    try:
        maximum = int(os.environ.get(
            "ENGRAPHIS_EMAIL_MAX_BACKLOG_AGE_SECONDS", "900"))
        maximum_bounce_rate = float(os.environ.get(
            "ENGRAPHIS_EMAIL_MAX_BOUNCE_RATE", "0.05"))
        minimum_sample = int(os.environ.get(
            "ENGRAPHIS_EMAIL_BOUNCE_MIN_SAMPLE", "20"))
        if maximum < 60 or minimum_sample < 1 or not (
                math.isfinite(maximum_bounce_rate)
                and 0.0 <= maximum_bounce_rate <= 1.0):
            raise ValueError("out-of-range email health configuration")
    except (OverflowError, ValueError):
        maximum, maximum_bounce_rate, minimum_sample = 900, 0.05, 20
        config_valid = False
    bounce_rate = bounced_recent / max(1, sent_recent)
    bounce_ok = sent_recent < minimum_sample or bounce_rate <= maximum_bounce_rate
    healthy = config_valid and (backlog == 0 or age <= maximum) \
        and terminal == 0 and bounce_ok
    return {"healthy": healthy, "backlog": backlog,
            "oldest_age_seconds": age, "failed": terminal,
            "bounced_or_complained": bounced, "sent_24h": sent_recent,
            "bounced_or_complained_24h": bounced_recent,
            "bounce_rate_24h": round(bounce_rate, 4),
            "configuration_valid": config_valid}


def recent_redacted(limit: int = 100) -> list[dict]:
    """Admin view with no recipient, subject, body, or provider error payload."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id,kind,status,attempts,max_attempts,provider,provider_message_id,"
            "created_at,updated_at,sent_at FROM email_outbox ORDER BY created_at DESC LIMIT ?",
            (max(1, min(500, int(limit))),)).fetchall()
    finally:
        conn.close()
    out = []
    for row in rows:
        item = dict(row)
        provider_id = item.pop("provider_message_id") or ""
        item["provider_message_fingerprint"] = (
            hashlib.sha256(provider_id.encode()).hexdigest()[:12] if provider_id else "")
        out.append(item)
    return out
