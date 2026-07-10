"""Polar webhook fulfillment — receives ``order.paid`` events, generates signed
license keys with the vendor secret, and emails them to the buyer.

Environment
-----------
POLAR_WEBHOOK_SECRET     -- set in Polar dashboard > Settings > Webhooks
ENGRAPHIS_SMTP_HOST      -- your SMTP server (e.g. smtp.resend.com)
ENGRAPHIS_SMTP_PORT      -- default 587
ENGRAPHIS_SMTP_USER      -- SMTP username
ENGRAPHIS_SMTP_PASSWORD  -- SMTP password (or API key)
ENGRAPHIS_SMTP_FROM      -- sender address (default: keys@engraphis.dev)
ENGRAPHIS_SIGNING_KEY    -- path to vendor_signing.key (default: .secrets/vendor_signing.key)
POLAR_ORGANIZATION_ID    -- (optional) verify the webhook org matches
"""
from __future__ import annotations

import email.message
import json
import logging
import os
import smtplib
import time
from pathlib import Path
from typing import Optional

from engraphis.licensing import (
    PLAN_FEATURES,
    compose_key,
    ed25519_public_key,
    _DEV_VENDOR_PUBKEY_HEX,
)

logger = logging.getLogger("engraphis.webhooks")

_DEFAULT_KEY_PATH = Path(__file__).resolve().parent.parent.parent / ".secrets" / "vendor_signing.key"


def _load_signing_secret() -> bytes:
    path = Path(os.environ.get("ENGRAPHIS_SIGNING_KEY", str(_DEFAULT_KEY_PATH)))
    try:
        raw = bytes.fromhex(path.read_text(encoding="utf-8").strip())
    except OSError:
        raise RuntimeError(f"Signing key not found at {path} — run `python -m scripts.license_admin keygen`")
    except ValueError:
        raise RuntimeError(f"{path} is not valid hex")
    if len(raw) != 32:
        raise RuntimeError(f"{path} must contain a 32-byte hex seed")
    return raw


def _map_polar_product_to_plan(product_name: str) -> str:
    """Map Polar product name to an Engraphis plan tier.

    Match on substrings so "Engraphis Pro Monthly" and "Engraphis Pro Annual"
    both resolve to ``pro``. Fall back to ``pro`` for any non-``team`` match.
    """
    name = (product_name or "").lower()
    if "team" in name:
        return "team"
    if "pro" in name:
        return "pro"
    # Unknown product → free tier with a warning; user gets a key but can
    # still activate it (it just won't unlock features).
    logger.warning("unknown product '%s' — issuing free-tier key", product_name)
    return "free"


def issue_key(email_addr: str, product_name: str = "pro", seats: int = 1,
               days: int = 30) -> str:
    """Generate a signed ``ENGR1.xxx.yyy`` key for *email_addr*.

    Uses the pinned vendor signing key (``.secrets/vendor_signing.key`` or
    ``ENGRAPHIS_SIGNING_KEY`` env). ``product_name`` maps to a plan tier.
    """
    secret = _load_signing_secret()
    pub = ed25519_public_key(secret).hex()
    if pub == _DEV_VENDOR_PUBKEY_HEX:
        logger.warning(
            "signing with DEV keypair — keys are forgeable. Rotate with "
            "`python -m scripts.license_admin keygen --force` before real sales."
        )

    plan = _map_polar_product_to_plan(product_name)
    if plan not in PLAN_FEATURES:
        plan = "free"

    now = time.time()
    payload = {
        "v": 1,
        "plan": plan,
        "email": email_addr,
        "seats": max(1, int(seats)),
        "issued": int(now),
        "expires": int(now + days * 86400),
    }
    key = compose_key(payload, secret)
    logger.info("issued %s key for %s (expires in %d days)", plan, email_addr, days)
    return key


def send_license_email(to: str, key: str, product_name: str = "Pro") -> None:
    """Email a license key to *to* via SMTP (ENGRAPHIS_SMTP_* env vars).

    Raises RuntimeError if any SMTP configuration is missing.
    """
    smtp_host = os.environ.get("ENGRAPHIS_SMTP_HOST", "").strip()
    smtp_port = int(os.environ.get("ENGRAPHIS_SMTP_PORT", "587"))
    smtp_user = os.environ.get("ENGRAPHIS_SMTP_USER", "").strip()
    smtp_pass = os.environ.get("ENGRAPHIS_SMTP_PASSWORD", "").strip()
    smtp_from = os.environ.get("ENGRAPHIS_SMTP_FROM", "keys@engraphis.dev").strip()

    if not smtp_host or not smtp_user or not smtp_pass:
        raise RuntimeError(
            "SMTP not configured — set ENGRAPHIS_SMTP_HOST, ENGRAPHIS_SMTP_USER, "
            "and ENGRAPHIS_SMTP_PASSWORD"
        )

    msg = email.message.EmailMessage()
    msg["From"] = smtp_from
    msg["To"] = to
    msg["Subject"] = f"Your Engraphis {product_name} License Key"
    msg.set_content(
        f"""Thank you for purchasing Engraphis {product_name}!

Your license key:

    {key}

To activate:
    1. Open the Memory Inspector (engraphis-inspector)
    2. Click the free badge in the header
    3. Paste the key and click Activate

Or set the ENGRAPHIS_LICENSE_KEY environment variable.

Your key is verified offline — no phone-home. Keep it safe.

— The Engraphis team
"""
    )

    logger.info("sending license email to %s via %s:%d", to, smtp_host, smtp_port)
    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(smtp_user, smtp_pass)
        smtp.send_message(msg)
    logger.info("license email delivered to %s", to)


def handle_order_paid(payload: dict) -> Optional[str]:
    """Process a Polar ``order.paid`` webhook payload.

    Returns the issued key on success, ``None`` if the payload is missing
    required fields (logged as warning). Raises on signing or email failure.
    """
    email_addr = (
        (payload.get("customer") or {}).get("email")
        or payload.get("customer_email")
        or payload.get("email")
    )
    if not email_addr:
        logger.warning("order.paid missing customer email — cannot issue key")
        return None

    product = payload.get("product") or {}
    product_name = product.get("name", "Pro")
    # seats default to 1; could be custom metadata on the product
    seats = int(product.get("metadata", {}).get("seats", 1) or 1)

    key = issue_key(email_addr, product_name=product_name, seats=seats)
    try:
        send_license_email(email_addr, key, product_name=product_name)
    except RuntimeError:
        # SMTP not configured — log the key so the operator can deliver manually
        logger.warning(
            "SMTP unavailable — key for %s: %s (deliver manually)",
            email_addr, key,
        )
    return key
