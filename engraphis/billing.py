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
_RESERVATION_TTL_SECONDS = 300

class WebhookStateError(RuntimeError):
    """The configured durable webhook state store could not complete an operation."""



def _dedup_path() -> Optional[str]:
    override = os.environ.get("ENGRAPHIS_WEBHOOK_STATE", "").strip()
    if override:
        return override
    db = os.environ.get("ENGRAPHIS_DB_PATH", "").strip()
    if db and db != ":memory:":
        try:
            return str(Path(db).expanduser().resolve().parent / ".engraphis_webhooks.db")
        except (OSError, RuntimeError) as exc:
            raise WebhookStateError("could not resolve durable webhook state path") from exc
    return None


def _dedup_conn() -> Optional[sqlite3.Connection]:
    path = _dedup_path()
    if not path:
        return None
    conn = None
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS processed ("
            "webhook_id TEXT PRIMARY KEY, ts REAL, "
            "state TEXT NOT NULL DEFAULT 'fulfilled')")
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(processed)").fetchall()}
        if "state" not in columns:
            conn.execute(
                "ALTER TABLE processed ADD COLUMN "
                "state TEXT NOT NULL DEFAULT 'fulfilled'")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS subscription_seats ("
            "subscription_id TEXT PRIMARY KEY, seats INTEGER NOT NULL, updated_at REAL)")
        seat_columns = {
            row[1] for row in conn.execute(
                "PRAGMA table_info(subscription_seats)").fetchall()}
        if "event_ts" not in seat_columns:
            # The source event's own modification time (Polar ``modified_at``), so an
            # out-of-order redelivery of an OLDER subscription.updated can't regress a
            # newer seat count. Nullable: payloads without a timestamp fall back to the
            # seat-count-diff logic exactly as before.
            conn.execute(
                "ALTER TABLE subscription_seats ADD COLUMN event_ts REAL")
        conn.commit()
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return conn
    except (OSError, sqlite3.Error) as exc:
        if conn is not None:
            conn.close()
        raise WebhookStateError("durable webhook state store unavailable") from exc


def claim_webhook(webhook_id: str) -> str:
    """Atomically determine this delivery's state and claim it if free.

    Returns one of three states so the caller can answer correctly instead of
    conflating "already done" with "still running":
      ``"claimed"``    — we now own a fresh (or reclaimed-after-TTL) processing slot;
                         proceed with fulfillment.
      ``"in_flight"``  — another attempt holds an UNFINISHED claim younger than
                         :data:`_RESERVATION_TTL_SECONDS`. The caller must return a
                         RETRYABLE (non-2xx) response so Polar retries later: answering
                         2xx here would cancel Polar's retries, and if the in-flight
                         attempt crashed before minting the key the purchase would be
                         lost forever (no future delivery to reclaim the slot at TTL).
      ``"fulfilled"``  — a prior attempt completed; a true duplicate — answer 2xx.

    Completed claims remain permanent. In-memory state is used only when no durable
    store is configured; a configured store failure is retryable and raises.
    """
    conn = _dedup_conn()
    if conn is None:
        # Single-worker fallback: state dies with the process, so a post-crash retry
        # simply re-claims. A present entry means this process already handled (or is
        # handling) it and WILL complete, so "fulfilled" (→ 2xx duplicate) is correct.
        with _mem_lock:
            if webhook_id in _mem_seen:
                return "fulfilled"
            _mem_seen.add(webhook_id)
            return "claimed"
    try:
        now = time.time()
        with conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO processed(webhook_id, ts, state) "
                "VALUES (?, ?, 'processing')", (webhook_id, now))
            if cur.rowcount == 1:
                return "claimed"
            cur = conn.execute(
                "UPDATE processed SET ts=? WHERE webhook_id=? "
                "AND state='processing' AND ts<=?",
                (now, webhook_id, now - _RESERVATION_TTL_SECONDS))
            if cur.rowcount == 1:
                return "claimed"
            row = conn.execute(
                "SELECT state FROM processed WHERE webhook_id=?",
                (webhook_id,)).fetchone()
        return "in_flight" if (row is not None and row[0] == "processing") else "fulfilled"
    except sqlite3.Error as exc:
        raise WebhookStateError("could not reserve durable webhook claim") from exc
    finally:
        conn.close()


def reserve_webhook(webhook_id: str) -> bool:
    """Back-compat bool wrapper over :func:`claim_webhook`.

    True only when this call took ownership of a fresh or reclaimed processing slot
    (i.e. the caller should proceed to fulfill). False for both an already-fulfilled
    claim and an in-flight one — callers that must distinguish those use
    :func:`claim_webhook` directly.
    """
    return claim_webhook(webhook_id) == "claimed"


def complete_webhook(webhook_id: str) -> None:
    """Mark a claimed webhook or fulfillment as durably complete."""
    conn = _dedup_conn()
    if conn is None:
        return
    try:
        with conn:
            cur = conn.execute(
                "UPDATE processed SET state='fulfilled', ts=? "
                "WHERE webhook_id=? AND state='processing'",
                (time.time(), webhook_id))
            if cur.rowcount != 1:
                raise WebhookStateError("webhook claim was not pending")
    except sqlite3.Error as exc:
        raise WebhookStateError("could not complete durable webhook claim") from exc
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
    except sqlite3.Error as exc:
        raise WebhookStateError("could not release durable webhook claim") from exc
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
    baseline = get_seat_baseline(subscription_id)
    return baseline[0] if baseline is not None else None


def get_seat_baseline(subscription_id: str) -> Optional[tuple[int, Optional[float]]]:
    """Last ``(seats, event_ts)`` recorded for *subscription_id*, or ``None``.

    ``event_ts`` is the source event's own modification time (may be ``None`` for
    baselines recorded from payloads that carried no timestamp)."""
    conn = _dedup_conn()
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT seats, event_ts FROM subscription_seats WHERE subscription_id = ?",
            (subscription_id,)).fetchone()
        if not row:
            return None
        return int(row[0]), (float(row[1]) if row[1] is not None else None)
    except sqlite3.Error as exc:
        raise WebhookStateError("could not read durable seat baseline") from exc
    finally:
        conn.close()


def record_known_seats(subscription_id: str, seats: int,
                       event_ts: Optional[float] = None) -> bool:
    """Persist *seats* (and the event's *event_ts*) as the new baseline. Return false
    without a durable store."""
    conn = _dedup_conn()
    if conn is None:
        return False
    try:
        with conn:
            conn.execute(
                "INSERT INTO subscription_seats(subscription_id, seats, updated_at, "
                "event_ts) VALUES (?, ?, ?, ?) ON CONFLICT(subscription_id) DO UPDATE SET "
                "seats=excluded.seats, updated_at=excluded.updated_at, "
                "event_ts=excluded.event_ts",
                (subscription_id, seats, time.time(), event_ts))
        return True
    except sqlite3.Error as exc:
        raise WebhookStateError("could not persist durable seat baseline") from exc
    finally:
        conn.close()

def _finalize_webhook(delivery_id: str, fulfillment_id: str,
                      seat_baseline: Optional[tuple] = None) -> None:
    """Persist the seat baseline and complete both claims in one transaction.

    ``seat_baseline`` is ``(subscription_id, seats)`` or ``(subscription_id, seats,
    event_ts)``; ``event_ts`` (the source event's modification time) defaults to ``None``.
    """
    conn = _dedup_conn()
    if conn is None:
        return
    try:
        with conn:
            now = time.time()
            if seat_baseline is not None:
                sub_id, seats = seat_baseline[0], seat_baseline[1]
                event_ts = seat_baseline[2] if len(seat_baseline) > 2 else None
                conn.execute(
                    "INSERT INTO subscription_seats(subscription_id, seats, updated_at, "
                    "event_ts) VALUES (?, ?, ?, ?) ON CONFLICT(subscription_id) DO UPDATE "
                    "SET seats=excluded.seats, updated_at=excluded.updated_at, "
                    "event_ts=excluded.event_ts",
                    (sub_id, seats, now, event_ts))
            for claim_id in (fulfillment_id, delivery_id):
                cur = conn.execute(
                    "UPDATE processed SET state='fulfilled', ts=? "
                    "WHERE webhook_id=? AND state='processing'",
                    (now, claim_id))
                if cur.rowcount != 1:
                    raise WebhookStateError("webhook claim was not pending")
    except sqlite3.Error as exc:
        raise WebhookStateError("could not atomically finalize webhook") from exc
    finally:
        conn.close()

def _release_claims(*claim_ids: str) -> None:
    """Best-effort rollback used only while returning a retryable failure."""
    for claim_id in claim_ids:
        try:
            release_webhook(claim_id)
        except WebhookStateError:
            logger.exception("polar webhook: could not release claim %s", claim_id)


def _subscription_id(data: dict) -> str:
    raw = data.get("subscription_id")
    if not raw:
        subscription = data.get("subscription")
        raw = subscription.get("id") if isinstance(subscription, dict) else subscription
    return str(raw or "").strip()[:128]

def _order_id(data: dict) -> str:
    from engraphis.inspector.webhooks import _extract_order_id
    order_id = _extract_order_id(data)
    if order_id:
        return order_id
    order = data.get("order") or {}
    if isinstance(order, dict):
        return _extract_order_id(order)
    return ""


def _event_modified_at(data: dict) -> Optional[float]:
    """Epoch of the event object's own last-modification time, if the payload carries
    one (Polar sends ``modified_at`` on Subscription objects). Used to reject an
    out-of-order redelivery of an older subscription.updated. Returns ``None`` when the
    payload has no usable timestamp, in which case ordering can't be established and the
    caller falls back to seat-count comparison."""
    from engraphis.inspector.webhooks import _parse_ts
    return _parse_ts(data.get("modified_at") or data.get("updated_at"))


def _event_organization_id(event: dict, data: dict) -> str:
    """Best-effort extraction of the Polar organization id from an event, checked in the
    locations Polar populates across order/subscription payloads."""
    product = data.get("product") or {}
    subscription = data.get("subscription") or {}
    for candidate in (
        data.get("organization_id"),
        event.get("organization_id"),
        product.get("organization_id"),
        subscription.get("organization_id"),
    ):
        if candidate:
            return str(candidate).strip()
    return ""


def _organization_mismatch(event: dict, data: dict) -> bool:
    """True only when ``POLAR_ORGANIZATION_ID`` is configured AND the event carries a
    DIFFERENT organization id. The check is documented (webhooks.py env docs) but was
    never enforced. Fails closed on a concrete mismatch; when the env var is unset, or
    the payload carries no org id to compare, it does not block fulfillment (so a
    payload-shape assumption can never strand a real purchase)."""
    expected = os.environ.get("POLAR_ORGANIZATION_ID", "").strip()
    if not expected:
        return False
    found = _event_organization_id(event, data)
    if not found:
        logger.warning("polar webhook: POLAR_ORGANIZATION_ID set but event carries no "
                       "organization id to verify — proceeding")
        return False
    return not hmac.compare_digest(found, expected)


# Events that END entitlement IMMEDIATELY -> revoke every key for the subscription (keys
# are cloud-enforced, so revocation takes effect at the next lease renewal, ~24h).
#
# A plain cancel-at-period-end (``subscription.canceled``) is deliberately NOT here: the
# customer paid for the current period and keeps their plan until it ends — their
# period-bounded key simply expires (Polar later fires ``subscription.revoked`` when access
# actually ends). Only a REFUND (or an explicit access revocation) removes access now:
#   order.refunded       -> the money was returned, so pull the license immediately.
#   subscription.revoked -> Polar has revoked access (refund-driven, admin action, or the
#                           end of a cancel-at-period-end period).
_REVOKING_EVENTS = frozenset({
    "order.refunded",
    "subscription.revoked",
})


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

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > _MAX_BODY_BYTES:
            return JSONResponse({"error": "payload too large"}, status_code=413)
        body.extend(chunk)
    raw_body = bytes(body)
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

    signed_content = f"{webhook_id}.{timestamp}.".encode("utf-8") + raw_body
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

    # Reject a signed event from a DIFFERENT Polar organization when POLAR_ORGANIZATION_ID
    # is configured (the documented-but-previously-unenforced control). No-op when unset.
    if _organization_mismatch(event, data):
        logger.warning("polar webhook: organization id mismatch — rejecting")
        return JSONResponse({"error": "organization mismatch"}, status_code=403)

    # Negative lifecycle. Refunds revoke immediately; ordinary cancel-at-period-end
    # intentionally does NOT revoke because the customer keeps the paid period.
    if event_type in ("subscription.canceled", "subscription.cancelled"):
        return JSONResponse({"status": "ignored", "reason": "paid period honored",
                             "type": event_type}, status_code=202)
    if event_type in _REVOKING_EVENTS:
        sub_id = _subscription_id(data)
        if not sub_id and event_type.startswith("subscription."):
            sub_id = str(data.get("id") or "").strip()[:128]
        order_id = _order_id(data)
        if not order_id and event_type.startswith("order."):
            order_id = str(data.get("id") or "").strip()[:128]
        if not sub_id and not order_id:
            return JSONResponse({"status": "ignored", "reason": "missing revoke target",
                                 "type": event_type}, status_code=202)
        try:
            from engraphis.inspector import license_registry as _reg
            if sub_id:
                revoked = await asyncio.to_thread(_reg.revoke_by_subscription, sub_id)
                target = {"subscription_id": sub_id}
            else:
                revoked = await asyncio.to_thread(_reg.revoke_by_order, order_id)
                target = {"order_id": order_id}
        except Exception:  # noqa: BLE001 — retryable: let Polar redeliver the revoke
            logger.exception("polar webhook: revocation failed for %s", sub_id or order_id)
            return JSONResponse({"error": "revocation failed"}, status_code=503)
        reason = "refund" if event_type == "order.refunded" else "subscription_revoked"
        logger.info("polar webhook: %s revoked %d key(s) for %s",
                    event_type, revoked, target)
        return JSONResponse({"status": "revoked", "reason": reason, "revoked": revoked,
                             "keys_revoked": revoked, **target}, status_code=202)

    # Route by event type and derive a stable per-fulfillment key so we issue exactly
    # ONE key per order and ONE per trial, no matter which/how many events fire:
    #   order.paid           -> paid activation, trial conversion, and each renewal
    #                           (a fresh order.paid per cycle). Fulfillment "order:<id>".
    #   subscription.created -> ONLY when the subscription is in a free trial, to grant
    #                           an immediate trial-length key. Fulfillment "trial:<sub id>".
    #   subscription.updated -> Team seat count changed mid-cycle (add/remove seats via
    #                           the Customer Portal). Only when status is active AND the
    #                           seat count actually differs from the last known baseline
    #                           for this subscription (see get_known_seats /
    #                           record_known_seats) — trialing updates wait for payment,
    #                           and unrelated updates cannot spam replacement keys.
    # A non-trial subscription.created is a no-op: its paid key comes from order.paid, so
    # a canceled trial can never keep Pro — the short trial key just expires.
    pending_seat_baseline = None  # (sub_id, seats, event_ts); persisted after key issuance
    if event_type == "order.paid":
        from engraphis.inspector.webhooks import (
            _extract_seats, handle_order_paid as _fulfill)
        fulfillment_key = "order:" + str(data.get("id") or webhook_id)
        sub_id = _subscription_id(data)
        if sub_id:
            # event_ts stays None: an Order object's modified_at is a different clock from
            # the Subscription's, so it must not seed the seat-ordering anchor (which is
            # compared only against subscription.updated modified_at values).
            pending_seat_baseline = (sub_id, _extract_seats(data), None)
    elif event_type == "subscription.created":
        if str(data.get("status", "")).strip().lower() != "trialing":
            return JSONResponse({"status": "ignored", "reason": "not a trial",
                                 "type": event_type}, status_code=202)
        from engraphis.inspector.webhooks import (
            _extract_seats, handle_subscription_created as _fulfill)
        sub_id = str(data.get("id") or webhook_id)
        fulfillment_key = "trial:" + sub_id
        pending_seat_baseline = (sub_id, _extract_seats(data), _event_modified_at(data))
    elif event_type == "subscription.updated":
        status = str(data.get("status", "")).strip().lower()
        sub_id = str(data.get("id") or "").strip()[:128]
        if status == "revoked":
            try:
                from engraphis.inspector import license_registry as _reg
                revoked = await asyncio.to_thread(_reg.revoke_by_subscription, sub_id)
            except Exception:  # noqa: BLE001 — retryable: let Polar redeliver the revoke
                logger.exception("polar webhook: revocation failed for %s", sub_id)
                return JSONResponse({"error": "revocation failed"}, status_code=503)
            return JSONResponse({"status": "revoked", "reason": "subscription_revoked",
                                 "revoked": revoked, "subscription_id": sub_id},
                                status_code=202)
        if status != "active" or not sub_id:
            return JSONResponse({"status": "ignored", "reason": "not an active "
                                 "subscription", "type": event_type}, status_code=202)
        from engraphis.inspector.webhooks import _extract_seats
        new_seats = _extract_seats(data)
        event_ts = _event_modified_at(data)
        try:
            prior = get_seat_baseline(sub_id)
        except WebhookStateError:
            logger.exception("polar webhook: could not read seat baseline")
            return JSONResponse({"error": "webhook state unavailable"}, status_code=503)
        if prior is None:
            # First sighting seeds the baseline; the initial paid key came from order.paid.
            try:
                persisted = record_known_seats(sub_id, new_seats, event_ts)
            except WebhookStateError:
                logger.exception("polar webhook: could not seed seat baseline")
                return JSONResponse({"error": "webhook state unavailable"}, status_code=503)
            reason = "baseline recorded" if persisted else "durable baseline unavailable"
            return JSONResponse({"status": "ignored", "reason": reason,
                                 "type": event_type}, status_code=202)
        prior_seats, prior_ts = prior
        # Out-of-order guard: if this delivery is OLDER than the last one we acted on for
        # this subscription, ignore it — a delayed redelivery of a stale seat count must
        # not regress a newer one (and revoke the correct key). Only applies when both
        # timestamps are known; without them we fall back to seat-count comparison.
        if event_ts is not None and prior_ts is not None and event_ts <= prior_ts:
            return JSONResponse({"status": "ignored", "reason": "out-of-order update",
                                 "type": event_type}, status_code=202)
        if prior_seats == new_seats:
            # No seat change. Keep the ordering anchor current (so a later, genuinely
            # older delivery is still recognized as stale) but do not re-issue.
            if event_ts is not None and (prior_ts is None or event_ts > prior_ts):
                try:
                    record_known_seats(sub_id, new_seats, event_ts)
                except WebhookStateError:
                    logger.exception("polar webhook: could not advance seat anchor")
                    return JSONResponse({"error": "webhook state unavailable"},
                                        status_code=503)
            return JSONResponse({"status": "ignored", "reason": "no seat-count change",
                                 "type": event_type}, status_code=202)
        pending_seat_baseline = (sub_id, new_seats, event_ts)
        from engraphis.inspector.webhooks import handle_subscription_updated as _fulfill
        # webhook-id is covered by the signature and stable across retries of one
        # delivery. Versioning by it permits legitimate A -> B -> A seat cycles while
        # retaining idempotency for a retried logical update.
        fulfillment_key = "seatsync:" + sub_id + ":" + webhook_id
    else:
        return JSONResponse({"status": "ignored", "type": event_type}, status_code=202)

    # Two-layer dedup: delivery-level (a retry of this exact webhook) and
    # fulfillment-level (one key per order/trial/update version). Each claim is
    # tri-state so an in-flight attempt is answered RETRYABLE (503) rather than a 2xx
    # "duplicate": a 2xx cancels Polar's retries, and if the in-flight attempt crashed
    # before minting the key the purchase would be lost with no future delivery to
    # reclaim the slot at the TTL. Only a genuinely COMPLETED claim is a duplicate.
    delivery_claim = "dlv:" + webhook_id
    fulfillment_claim = "ful:" + fulfillment_key
    delivery_reserved = False
    try:
        delivery_state = claim_webhook(delivery_claim)
        if delivery_state == "fulfilled":
            logger.info("polar webhook: duplicate delivery %s ignored", webhook_id)
            return JSONResponse(
                {"status": "duplicate", "key_issued": False}, status_code=202)
        if delivery_state == "in_flight":
            logger.info("polar webhook: delivery %s already in flight — retry later",
                        webhook_id)
            return JSONResponse({"status": "processing", "key_issued": False},
                                status_code=503)
        delivery_reserved = True
        fulfillment_state = claim_webhook(fulfillment_claim)
        if fulfillment_state == "fulfilled":
            complete_webhook(delivery_claim)
            logger.info("polar webhook: %s already fulfilled — no second key", fulfillment_key)
            return JSONResponse({"status": "already_fulfilled", "key_issued": False},
                                status_code=202)
        if fulfillment_state == "in_flight":
            # A concurrent delivery for the same order/trial/version is minting the key.
            # Release our delivery claim and have Polar retry — by then it's fulfilled.
            _release_claims(delivery_claim)
            logger.info("polar webhook: %s fulfillment in flight — retry later",
                        fulfillment_key)
            return JSONResponse({"status": "processing", "key_issued": False},
                                status_code=503)
    except WebhookStateError:
        if delivery_reserved:
            _release_claims(delivery_claim)
        logger.exception("polar webhook: durable reservation failed")
        return JSONResponse({"error": "webhook state unavailable"}, status_code=503)

    try:
        # Blocking work (Ed25519 sign + email) runs off the event loop.
        key = await asyncio.to_thread(_fulfill, data)
    except Exception as exc:  # noqa: BLE001 - external-provider boundary
        _release_claims(delivery_claim, fulfillment_claim)
        logger.error("polar webhook: fulfillment failed (%s)", type(exc).__name__)
        return JSONResponse({"error": "license fulfillment failed; retry delivery"},
                            status_code=500)

    if not key:
        # Nothing issued (missing email) — release the claims so a corrected delivery
        # isn't permanently suppressed.
        _release_claims(delivery_claim, fulfillment_claim)
        return JSONResponse({"status": "fulfilled", "key_issued": False}, status_code=202)

    try:
        # Baseline advancement and both durable completion markers share one commit.
        _finalize_webhook(delivery_claim, fulfillment_claim, pending_seat_baseline)
    except WebhookStateError:
        _release_claims(delivery_claim, fulfillment_claim)
        logger.exception("polar webhook: durable finalization failed")
        return JSONResponse({"error": "webhook state unavailable"}, status_code=503)
    logger.info("polar webhook: issued key for %s", fulfillment_key)
    return JSONResponse({"status": "fulfilled", "key_issued": True}, status_code=202)
