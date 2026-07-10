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
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    event_type = (event.get("type") or "").strip()
    if event_type != "order.paid":
        return JSONResponse({"status": "ignored", "type": event_type}, status_code=202)

    # Claim this delivery BEFORE fulfilling so a retry/crash can't mint a 2nd key.
    if not reserve_webhook(webhook_id):
        logger.info("polar webhook: duplicate delivery %s ignored", webhook_id)
        return JSONResponse({"status": "duplicate", "key_issued": False}, status_code=202)

    from engraphis.inspector.webhooks import handle_order_paid
    try:
        # handle_order_paid does blocking work (Ed25519 sign + SMTP); run it off the
        # event loop so a slow SMTP server can't stall other requests on this worker.
        key = await asyncio.to_thread(handle_order_paid, event.get("data") or event)
    except Exception as exc:  # noqa: BLE001 — surface a safe message, log full trace
        release_webhook(webhook_id)  # let Polar retry this event
        logger.exception("polar webhook: fulfillment failed")
        return JSONResponse({"error": "fulfillment failed: %s" % exc}, status_code=500)

    if not key:
        # No key minted (e.g. missing customer email) — release so a corrected
        # delivery isn't permanently suppressed; nothing was issued to duplicate.
        release_webhook(webhook_id)
        return JSONResponse({"status": "fulfilled", "key_issued": False}, status_code=202)

    logger.info("polar webhook: issued key for order %s", event.get("id", "?"))
    return JSONResponse({"status": "fulfilled", "key_issued": True}, status_code=202)
