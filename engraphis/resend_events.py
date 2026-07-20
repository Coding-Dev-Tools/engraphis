"""Verified Resend delivery-event webhook."""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import math
import os
import time
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from engraphis import email_outbox

router = APIRouter(prefix="/email/v1", tags=["email"])
MAX_BODY_BYTES = 256 * 1024


def _secret_bytes() -> bytes:
    raw = os.environ.get("RESEND_WEBHOOK_SECRET", "").strip()
    if not raw:
        return b""
    if not raw.startswith("whsec_"):
        return raw.encode("utf-8")
    try:
        encoded = raw[6:]
        return base64.b64decode(encoded + "=" * (-len(encoded) % 4), validate=True)
    except (binascii.Error, ValueError):
        return b""


def webhook_secret_ready() -> bool:
    """Return whether the configured signing secret is usable without exposing it."""
    return len(_secret_bytes()) >= 16


def verify_signature(body: bytes, event_id: str, timestamp: str,
    signature_header: str, *, now: Optional[float] = None) -> bool:
    secret = _secret_bytes()
    if len(secret) < 16 or not event_id or not timestamp or not signature_header:
        return False
    try:
        stamp = float(timestamp)
    except ValueError:
        return False
    if not math.isfinite(stamp):
        return False
    current_time = time.time() if now is None else now
    if not math.isfinite(current_time) or abs(current_time - stamp) > 300:
        return False
    signed = event_id.encode() + b"." + timestamp.encode() + b"." + body
    expected = base64.b64encode(hmac.new(secret, signed, hashlib.sha256).digest()).decode()
    for candidate in signature_header.split():
        if candidate.startswith("v1,") and hmac.compare_digest(candidate[3:], expected):
            return True
    return False


@router.post("/resend-events")
async def resend_event(request: Request):
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            declared_length = int(content_length)
            if declared_length < 0:
                return JSONResponse({"accepted": False}, status_code=400)
            if declared_length > MAX_BODY_BYTES:
                return JSONResponse({"accepted": False}, status_code=413)
        except ValueError:
            return JSONResponse({"accepted": False}, status_code=400)
    chunks = bytearray()
    async for chunk in request.stream():
        chunks.extend(chunk)
        if len(chunks) > MAX_BODY_BYTES:
            return JSONResponse({"accepted": False}, status_code=413)
    body = bytes(chunks)
    if len(body) > MAX_BODY_BYTES:
        return JSONResponse({"accepted": False}, status_code=413)
    event_id = request.headers.get("svix-id", "")
    stamp = request.headers.get("svix-timestamp", "")
    signature = request.headers.get("svix-signature", "")
    if len(event_id) > 255 or len(stamp) > 32 or len(signature) > 4096:
        return JSONResponse({"accepted": False}, status_code=400)
    if not verify_signature(body, event_id, stamp, signature):
        return JSONResponse({"accepted": False}, status_code=401)
    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        return JSONResponse({"accepted": False}, status_code=400)
    data = payload.get("data") if isinstance(payload, dict) else {}
    if not isinstance(data, dict):
        return JSONResponse({"accepted": False}, status_code=400)
    message_id = data.get("email_id") or data.get("id") or ""
    event_type = payload.get("type") if isinstance(payload, dict) else ""
    if not isinstance(message_id, str) or not isinstance(event_type, str):
        return JSONResponse({"accepted": False}, status_code=400)
    if not email_outbox.record_provider_event(event_id, message_id, event_type):
        return JSONResponse({"accepted": False}, status_code=400)
    return {"accepted": True}
