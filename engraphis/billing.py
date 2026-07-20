"""Polar billing webhook — the single source of truth for purchase fulfillment.

Mounted by BOTH the public server (``engraphis/app.py``, the image Railway runs)
and the Inspector (``engraphis/inspector/app.py``), so a purchase auto-fulfills
no matter which entrypoint a deployment happens to run. Keeping the route in one
place is deliberate: the previous split (route only in the Inspector, but the
Inspector never deployed) is exactly why live purchases 404'd.

Flow: Polar POSTs an ``order.paid`` event → we verify the Standard-Webhooks HMAC
signature → mint a signed license key with the vendor seed → email it via SMTP.
The heavy lifting (key signing, SMTP) lives in ``engraphis.inspector.webhooks``;
this module is only transport + signature verification + idempotency.
"""
from __future__ import annotations

import base64
import binascii
import asyncio
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("engraphis.billing")

router = APIRouter()

# Standard-Webhooks replay tolerance (seconds). A captured delivery older/newer
# than this is rejected so one signed order.paid can't be replayed into a
# perpetual subscription.
_TIMESTAMP_TOLERANCE = 300

# A real Standard-Webhooks payload is a few KB. This route is deliberately exempt
# from auth + rate limiting (Polar can't present a bearer token), so cap the body
# to keep an unauthenticated caller from buffering arbitrary bytes into memory.
_MAX_BODY_BYTES = 65_536


def _decode_webhook_secret(secret: str) -> bytes:
    """Decode a Polar / Standard-Webhooks secret into raw HMAC key bytes.

    Polar issues secrets in the form ``whsec_<base64>``. The ``whsec_`` prefix is
    a label, NOT part of the key material, so it must be stripped before decoding
    — decoding the whole string yields the wrong key and every real delivery then
    fails the signature check. The base64 body may be unpadded, so pad it back.
    A bare base64 secret (no prefix) is accepted too, for tests and manual setups.
    """
    secret = (secret or "").strip()
    if secret.startswith("whsec_"):
        secret = secret[len("whsec_"):]
    pad = "=" * (-len(secret) % 4)
    return base64.b64decode(secret + pad, validate=True)


# ── idempotency ───────────────────────────────────────────────────────────────
# Polar re-delivers an event until it receives a 2xx. Without dedup, a retry (or a
# crash between minting a key and answering 2xx) would mint a *second* valid key
# for one paid order — and keys verify offline, so a stray key can't be revoked.
# We claim each Standard-Webhooks ``webhook-id`` (stable across retries of one
# event) with an ATOMIC ``INSERT`` into a small SQLite table BEFORE fulfilling, so
# the reservation is durable across workers/replicas and process restarts. On a
# fulfillment failure we release the claim so Polar's retry can try again. If no
# durable path is available we fall back to an in-process set (still correct for a
# single worker). A dedup-store error must never block a real purchase.
_mem_lock = threading.Lock()
_mem_seen: "set[str]" = set()


def _dedup_path() -> Optional[str]:
    override = os.environ.get("ENGRAPHIS_WEBHOOK_STATE", "").strip()
    if override:
        return override
    db = os.environ.get("ENGRAPHIS_DB_PATH", "").strip()
    if db and db != ":memory:":
        try:
            return str(Path(db).expanduser().resolve().parent / ".engraphis_webhooks.db")
        except Exception:
            return None
    return None


def _dedup_conn() -> Optional[sqlite3.Connection]:
    path = _dedup_path()
    if not path:
        return None
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS processed (webhook_id TEXT PRIMARY KEY, ts REAL)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS subscription_seats ("
            "subscription_id TEXT PRIMARY KEY, seats INTEGER NOT NULL, updated_at REAL)")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return conn
    except sqlite3.Error:
        return None


def reserve_webhook(webhook_id: str) -> bool:
    """Atomically claim *webhook_id*. Returns True if newly claimed (caller should
    fulfill), False if it was already processed (duplicate delivery)."""
    conn = _dedup_conn()
    if conn is None:
        with _mem_lock:
            if webhook_id in _mem_seen:
                return False
            _mem_seen.add(webhook_id)
            return True
    try:
        with conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO processed(webhook_id, ts) VALUES (?, ?)",
                (webhook_id, time.time()))
        return cur.rowcount == 1
    except sqlite3.Error:
        # Degraded: fall back to in-process guard rather than blocking the sale.
        with _mem_lock:
            if webhook_id in _mem_seen:
                return False
            _mem_seen.add(webhook_id)
            return True
    finally:
        conn.close()


def release_webhook(webhook_id: str) -> None:
    """Undo a reservation so a *failed* fulfillment can be retried by Polar."""
    with _mem_lock:
        _mem_seen.discard(webhook_id)
    conn = _dedup_conn()
    if conn is None:
        return
    try:
        with conn:
            conn.execute("DELETE FROM processed WHERE webhook_id = ?", (webhook_id,))
    except sqlite3.Error:
        pass
    finally:
        conn.close()


# ── seat-count baseline tracking (mid-cycle Team seat changes) ─────────────────
# ``subscription.updated`` fires for MANY unrelated transitions (cancel, uncancel,
# past_due, revoked...) as well as genuine seat-count changes, and it would also
# fire on the update immediately following a subscription's own creation. Without
# a durable "what did we last see" baseline per subscription, naively re-issuing a
# key on every subscription.updated would spam duplicate keys/emails. Requires a
# durable dedup path (ENGRAPHIS_WEBHOOK_STATE / ENGRAPHIS_DB_PATH); with no durable
# store configured this fails CLOSED (never re-issues) rather than open.
def get_known_seats(subscription_id: str) -> Optional[int]:
    """Last seat count recorded for *subscription_id*, or ``None`` if never seen."""
    conn = _dedup_conn()
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT seats FROM subscription_seats WHERE subscription_id = ?",
            (subscription_id,)).fetchone()
        return int(row[0]) if row else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def record_known_seats(subscription_id: str, seats: int) -> None:
    """Persist *seats* as the new baseline for *subscription_id*."""
    conn = _dedup_conn()
    if conn is None:
        return
    try:
        with conn:
            conn.execute(
                "INSERT INTO subscription_seats(subscription_id, seats, updated_at) "
                "VALUES (?, ?, ?) ON CONFLICT(subscription_id) DO UPDATE SET "
                "seats=excluded.seats, updated_at=excluded.updated_at",
                (subscription_id, seats, time.time()))
    except sqlite3.Error:
        pass
    finally:
        conn.close()

def _polar_subscription_id(data: dict, *, object_is_subscription: bool = False) -> str:
    """Extract a Polar subscription id from direct or nested event data."""
    from engraphis.inspector.webhooks import _extract_subscription_id
    sub_id = _extract_subscription_id(data, object_is_subscription=object_is_subscription)
    if sub_id:
        return sub_id
    order = data.get("order") or {}
    if isinstance(order, dict):
        return _extract_subscription_id(order)
    return ""


def _polar_order_id(data: dict) -> str:
    """Extract a Polar order id from direct or nested event data."""
    from engraphis.inspector.webhooks import _extract_order_id
    order_id = _extract_order_id(data)
    if order_id:
        return order_id
    order = data.get("order") or {}
    if isinstance(order, dict):
        return _extract_order_id(order)
    return ""


def _revoke_refunded_order(data: dict, webhook_id: str) -> JSONResponse:
    """Refunds return the money, so revoke the affected key(s) immediately."""
    subscription_id = _polar_subscription_id(data)
    order_id = _polar_order_id(data)
    if not subscription_id and not order_id:
        return JSONResponse({"status": "ignored", "reason": "missing refund target",
                             "type": "order.refunded"}, status_code=202)

    delivery_claim = "dlv:" + webhook_id
    if not reserve_webhook(delivery_claim):
        logger.info("polar webhook: duplicate refund delivery %s ignored", webhook_id)
        return JSONResponse({"status": "duplicate", "revoked": 0}, status_code=202)

    try:
        from engraphis.inspector.license_registry import (
            revoke_by_order, revoke_by_subscription)
        if subscription_id:
            revoked = revoke_by_subscription(subscription_id)
            target = {"subscription_id": subscription_id}
        else:
            revoked = revoke_by_order(order_id)
            target = {"order_id": order_id}
    except Exception:  # noqa: BLE001 — force Polar to retry if durable revoke failed
        release_webhook(delivery_claim)
        logger.exception("polar webhook: refund revocation failed")
        return JSONResponse({"error": "revocation failed"}, status_code=503)

    logger.warning("polar webhook: refund revoked %d license key(s) for %s",
                   revoked, target)
    return JSONResponse({"status": "revoked", "reason": "refund",
                         "revoked": revoked, **target}, status_code=202)


def _revoke_subscription_event(data: dict, webhook_id: str, *,
                               reason: str) -> JSONResponse:
    """Definitive subscription revocation: access should end now, not at expiry."""
    subscription_id = _polar_subscription_id(data, object_is_subscription=True)
    if not subscription_id:
        return JSONResponse({"status": "ignored", "reason": "missing subscription id",
                             "type": "subscription.revoked"}, status_code=202)

    delivery_claim = "dlv:" + webhook_id
    if not reserve_webhook(delivery_claim):
        logger.info("polar webhook: duplicate revocation delivery %s ignored", webhook_id)
        return JSONResponse({"status": "duplicate", "revoked": 0}, status_code=202)

    try:
        from engraphis.inspector.license_registry import revoke_by_subscription
        revoked = revoke_by_subscription(subscription_id)
    except Exception:  # noqa: BLE001 — force Polar to retry if durable revoke failed
        release_webhook(delivery_claim)
        logger.exception("polar webhook: subscription revocation failed")
        return JSONResponse({"error": "revocation failed"}, status_code=503)

    logger.warning("polar webhook: %s revoked %d license key(s) for subscription %s",
                   reason, revoked, subscription_id)
    return JSONResponse({"status": "revoked", "reason": reason, "revoked": revoked,
                         "subscription_id": subscription_id}, status_code=202)


@router.post("/webhooks/polar")
async def polar_webhook(request: Request):
    """Receive Polar ``order.paid`` events, issue a signed license key, and email
    it to the buyer. Signature is verified against ``POLAR_WEBHOOK_SECRET``.

    202 on success (and on ignored/duplicate events), 400 on unparsable input,
    403 on bad signature/timestamp, 500 on misconfiguration or fulfillment error.
    """
    secret = os.environ.get("POLAR_WEBHOOK_SECRET", "").strip()
    if not secret:
        return JSONResponse(
            {"error": "POLAR_WEBHOOK_SECRET not configured"}, status_code=500)

    try:
        content_length = int(request.headers.get("content-length") or 0)
    except ValueError:
        content_length = 0
    if content_length > _MAX_BODY_BYTES:
        return JSONResponse({"error": "payload too large"}, status_code=413)

    raw_body = await request.body()
    if len(raw_body) > _MAX_BODY_BYTES:
        return JSONResponse({"error": "payload too large"}, status_code=413)
    body_str = raw_body.decode("utf-8", errors="replace")

    webhook_id = request.headers.get("webhook-id", "")
    timestamp = request.headers.get("webhook-timestamp", "")
    signature_header = request.headers.get("webhook-signature", "")

    if not webhook_id or not timestamp or not signature_header:
        return JSONResponse({"error": "missing webhook headers"}, status_code=400)

    try:
        ts = float(timestamp)
    except ValueError:
        return JSONResponse({"error": "invalid webhook timestamp"}, status_code=400)
    if abs(time.time() - ts) > _TIMESTAMP_TOLERANCE:
        logger.warning("polar webhook: timestamp outside %ds tolerance", _TIMESTAMP_TOLERANCE)
        return JSONResponse({"error": "webhook timestamp outside tolerance"},
                            status_code=403)

    try:
        secret_bytes = _decode_webhook_secret(secret)
    except (binascii.Error, ValueError):
        return JSONResponse(
            {"error": "POLAR_WEBHOOK_SECRET is not valid base64"}, status_code=500)

    signed_content = f"{webhook_id}.{timestamp}.{body_str}".encode("utf-8")
    expected_digest = hmac.new(secret_bytes, signed_content, hashlib.sha256).digest()
    expected_b64 = base64.b64encode(expected_digest).decode("ascii")

    # webhook-signature is space-separated "v1,<b64>" pairs (key rotation). Accept
    # a match against ANY listed signature.
    presented = []
    for token in signature_header.split():
        parts = token.split(",", 1)
        presented.append(parts[1].strip() if len(parts) == 2 else parts[0].strip())
    if not presented:
        return JSONResponse({"error": "invalid signature format"}, status_code=403)
    if not any(hmac.compare_digest(expected_b64, p) for p in presented):
        logger.warning("polar webhook: invalid signature")
        return JSONResponse({"error": "invalid signature"}, status_code=403)

    try:
        event = json.loads(body_str)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    event_type = (event.get("type") or "").strip()
    data = event.get("data") or {}

    # Route by event type and derive a stable per-fulfillment key so we issue exactly
    # ONE key per order and ONE per trial, no matter which/how many events fire:
    #   order.paid           -> paid activation, trial conversion, and each renewal
    #                           (a fresh order.paid per cycle). Fulfillment "order:<id>".
    #   order.refunded       -> immediate revocation. Money returned means the key is
    #                           returned too.
    #   subscription.canceled -> no revocation. The customer paid for the current period;
    #                            the signed key expiry remains the entitlement boundary.
    #   subscription.revoked -> immediate revocation after the paid period actually ends
    #                           or on merchant/admin immediate revocation.
    #   subscription.created -> ONLY when the subscription is in a free trial, to grant
    #                           an immediate trial-length key. Fulfillment "trial:<sub id>".
    #   subscription.updated -> Team seat count changed mid-cycle (add/remove seats via
    #                           the Customer Portal). Only when status is active/trialing
    #                           AND the seat count actually differs from the last known
    #                           baseline for this subscription (see get_known_seats /
    #                           record_known_seats) — otherwise this event also fires for
    #                           cancel/uncancel/past_due and would spam a re-issue.
    # A non-trial subscription.created is a no-op: its paid key comes from order.paid, so
    # a canceled trial can never keep Pro — the short trial key just expires.
    if event_type == "order.refunded":
        return _revoke_refunded_order(data, webhook_id)
    if event_type in ("subscription.canceled", "subscription.cancelled"):
        return JSONResponse({"status": "ignored", "reason": "paid period honored",
                             "type": event_type}, status_code=202)
    if event_type == "subscription.revoked":
        return _revoke_subscription_event(data, webhook_id,
                                          reason="subscription_revoked")
    pending_seat_baseline = None  # (sub_id, seats) to persist ONLY after a successful re-issue
    if event_type == "order.paid":
        from engraphis.inspector.webhooks import handle_order_paid as _fulfill
        fulfillment_key = "order:" + str(data.get("id") or webhook_id)
    elif event_type == "subscription.created":
        if str(data.get("status", "")).strip().lower() != "trialing":
            return JSONResponse({"status": "ignored", "reason": "not a trial",
                                 "type": event_type}, status_code=202)
        from engraphis.inspector.webhooks import handle_subscription_created as _fulfill
        fulfillment_key = "trial:" + str(data.get("id") or webhook_id)
    elif event_type == "subscription.updated":
        status = str(data.get("status", "")).strip().lower()
        sub_id = str(data.get("id") or "")
        if status == "revoked":
            return _revoke_subscription_event(data, webhook_id,
                                              reason="subscription_revoked")
        if status not in ("active", "trialing") or not sub_id:
            return JSONResponse({"status": "ignored", "reason": "not an active/trialing "
                                 "subscription", "type": event_type}, status_code=202)
        from engraphis.inspector.webhooks import _extract_seats
        new_seats = _extract_seats(data)
        prior_seats = get_known_seats(sub_id)
        if prior_seats is None:
            # First sighting of this subscription: seed the baseline so the NEXT update can
            # detect a real change. Nothing to (re-)fulfill — the initial key came from
            # order.paid. Safe to persist immediately (no fulfillment depends on it).
            record_known_seats(sub_id, new_seats)
            return JSONResponse({"status": "ignored", "reason": "baseline recorded",
                                 "type": event_type}, status_code=202)
        if prior_seats == new_seats:
            return JSONResponse({"status": "ignored", "reason": "no seat-count change",
                                 "type": event_type}, status_code=202)
        # Real seat change. DEFER advancing the baseline until the re-issue actually
        # succeeds: recording it up-front meant a crash (or a failed/retried fulfillment)
        # left the baseline advanced while no new key went out, and Polar's retry then saw
        # prior == new and skipped the re-issue permanently. Persisted at the success path.
        pending_seat_baseline = (sub_id, new_seats)
        from engraphis.inspector.webhooks import handle_subscription_updated as _fulfill
        fulfillment_key = "seatsync:" + sub_id + ":" + str(new_seats)
    else:
        return JSONResponse({"status": "ignored", "type": event_type}, status_code=202)

    # Two-layer dedup: delivery-level (a retry of this exact webhook) and
    # fulfillment-level (one key per order/trial, even across different deliveries).
    if not reserve_webhook("dlv:" + webhook_id):
        logger.info("polar webhook: duplicate delivery %s ignored", webhook_id)
        return JSONResponse({"status": "duplicate", "key_issued": False}, status_code=202)
    if not reserve_webhook("ful:" + fulfillment_key):
        logger.info("polar webhook: %s already fulfilled — no second key", fulfillment_key)
        return JSONResponse({"status": "already_fulfilled", "key_issued": False},
                            status_code=202)

    try:
        # Blocking work (Ed25519 sign + email) runs off the event loop.
        key = await asyncio.to_thread(_fulfill, data)
    except Exception as exc:  # noqa: BLE001 — surface a safe message, log full trace
        release_webhook("dlv:" + webhook_id)
        release_webhook("ful:" + fulfillment_key)  # let Polar retry this event
        logger.exception("polar webhook: fulfillment failed")
        return JSONResponse({"error": "fulfillment failed: %s" % exc}, status_code=500)

    if not key:
        # Nothing issued (missing email) — release the claims so a corrected delivery
        # isn't permanently suppressed.
        release_webhook("dlv:" + webhook_id)
        release_webhook("ful:" + fulfillment_key)
        return JSONResponse({"status": "fulfilled", "key_issued": False}, status_code=202)

    if pending_seat_baseline is not None:
        # Key is out — now it's safe to advance the seat baseline. A crash before this
        # point simply leaves the old baseline, so Polar's retry re-detects the change.
        record_known_seats(*pending_seat_baseline)
    logger.info("polar webhook: issued key for %s", fulfillment_key)
    return JSONResponse({"status": "fulfilled", "key_issued": True}, status_code=202)
