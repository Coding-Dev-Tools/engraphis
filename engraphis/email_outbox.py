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
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
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


def _connect() -> sqlite3.Connection:
    conn = license_registry.connect()
    conn.executescript(_SCHEMA)
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
            max_attempts: int = MAX_ATTEMPTS) -> str:
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
                "SELECT id FROM email_outbox WHERE idempotency_key=?",
                (clean_idem,)).fetchone()
            if row:
                return str(row["id"])
        try:
            conn.execute(
                "INSERT INTO email_outbox(id,idempotency_key,kind,recipient,subject,"
                "text_body,reply_to,max_attempts,next_attempt_at,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (msg_id, clean_idem, clean_kind, clean_recipient, clean_subject,
                 text_body, clean_reply_to, attempts_limit, now, now, now))
            conn.commit()
            return msg_id
        except sqlite3.IntegrityError:
            if not clean_idem:
                raise
            row = conn.execute(
                "SELECT id FROM email_outbox WHERE idempotency_key=?",
                (clean_idem,)).fetchone()
            if row:
                return str(row["id"])
            raise
    finally:
        conn.close()


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
        updated = conn.execute(
            "UPDATE email_outbox SET status='sent',provider=?,provider_message_id=?,"
            "last_error='',sent_at=?,updated_at=? "
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


def requeue_failed(*, limit: int = 100) -> int:
    """Make terminal failures explicitly retryable after an operator requests it."""
    conn = _connect()
    previous = conn.isolation_level
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT id FROM email_outbox WHERE status='failed' "
            "ORDER BY updated_at LIMIT ?", (max(1, min(500, int(limit))),)).fetchall()
        now = time.time()
        for row in rows:
            conn.execute(
                "UPDATE email_outbox SET status='retry',attempts=0,next_attempt_at=?,"
                "last_error='',updated_at=? WHERE id=? AND status='failed'",
                (now, now, str(row["id"])))
        conn.execute("COMMIT")
        return len(rows)
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
