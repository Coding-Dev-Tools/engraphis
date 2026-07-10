"""Polar webhook fulfillment — receives ``order.paid`` events, generates signed
license keys with the vendor secret, and emails them to the buyer.

Environment
-----------
POLAR_WEBHOOK_SECRET         -- set in Polar dashboard > Settings > Webhooks
ENGRAPHIS_SMTP_HOST          -- your SMTP server (e.g. smtp.resend.com)
ENGRAPHIS_SMTP_PORT          -- default 587
ENGRAPHIS_SMTP_USER          -- SMTP username
ENGRAPHIS_SMTP_PASSWORD      -- SMTP password (or API key)
ENGRAPHIS_SMTP_FROM          -- sender address (default: keys@engraphis.com)
ENGRAPHIS_VENDOR_SIGNING_KEY -- vendor Ed25519 seed as 64-char hex (preferred), OR a
                                path to a file containing that hex. Falls back to
                                ENGRAPHIS_SIGNING_KEY, then .secrets/vendor_signing.key.
POLAR_ORGANIZATION_ID        -- (optional) verify the webhook org matches
"""
from __future__ import annotations

import email.message
import hashlib
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


def _hex_to_seed(value: str, *, source: str) -> bytes:
    try:
        raw = bytes.fromhex(value)
    except ValueError:
        raise RuntimeError(f"{source} is not valid hex")
    if len(raw) != 32:
        raise RuntimeError(f"{source} must be a 32-byte (64 hex char) Ed25519 seed")
    return raw


def _read_seed_file(path: Path) -> bytes:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        raise RuntimeError(
            f"Signing key not found at {path} — set ENGRAPHIS_VENDOR_SIGNING_KEY to the "
            f"vendor seed (64 hex chars) or run `python -m scripts.license_admin keygen`")
    return _hex_to_seed(text, source=str(path))


def _looks_like_hex_seed(value: str) -> bool:
    return len(value) == 64 and all(c in "0123456789abcdefABCDEF" for c in value)


def _load_signing_secret() -> bytes:
    """Resolve the vendor Ed25519 seed (32 bytes).

    Precedence: ``ENGRAPHIS_VENDOR_SIGNING_KEY`` then ``ENGRAPHIS_SIGNING_KEY``,
    each accepted as EITHER an inline 64-char hex seed (how Railway sets it) OR a
    path to a file containing that hex. Falls back to ``.secrets/vendor_signing.key``.
    Accepting the inline hex is what makes the deployed webhook actually load the
    key — the env is a value, not a file, in a container.
    """
    for env_name in ("ENGRAPHIS_VENDOR_SIGNING_KEY", "ENGRAPHIS_SIGNING_KEY"):
        val = os.environ.get(env_name, "").strip()
        if not val:
            continue
        if _looks_like_hex_seed(val):
            return _hex_to_seed(val, source=env_name)
        path = Path(val)
        if path.exists():
            return _read_seed_file(path)
        raise RuntimeError(
            f"{env_name} is neither a 64-char hex seed nor a path to a seed file")
    return _read_seed_file(Path(_DEFAULT_KEY_PATH))


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


_RESEND_API_URL = "https://api.resend.com/emails"


def _license_email_text(key: str, product_name: str) -> str:
    return f"""Thank you for purchasing Engraphis {product_name}!

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


def _resend_api_key() -> str:
    """Resend API key for HTTPS delivery, or "" if none.

    Prefers ENGRAPHIS_RESEND_API_KEY; otherwise reuses ENGRAPHIS_SMTP_PASSWORD
    when it is a Resend key (``re_...``) with a Resend SMTP host — so an existing
    Resend SMTP setup works over HTTPS with zero new config.
    """
    key = os.environ.get("ENGRAPHIS_RESEND_API_KEY", "").strip()
    if key:
        return key
    host = os.environ.get("ENGRAPHIS_SMTP_HOST", "").strip().lower()
    pw = os.environ.get("ENGRAPHIS_SMTP_PASSWORD", "").strip()
    if "resend.com" in host and pw.startswith("re_"):
        return pw
    return ""


def _send_via_resend_api(to: str, subject: str, text_body: str, from_addr: str,
                         api_key: str) -> None:
    """Send via Resend's HTTPS API (port 443). Works where outbound SMTP is
    blocked (Railway, Fly, many PaaS). Raises RuntimeError on any failure.

    Uses httpx (a declared dependency) rather than urllib, and sets an explicit
    User-Agent: ``api.resend.com`` is behind Cloudflare, which blocks the default
    ``Python-urllib`` client signature with a 403 "error code: 1010". httpx's
    mainstream TLS fingerprint plus a named UA passes that bot check.
    """
    import httpx

    payload = {"from": from_addr, "to": [to], "subject": subject, "text": text_body}
    headers = {
        "Authorization": "Bearer %s" % api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Engraphis/1.0 (+https://engraphis.com)",
    }
    try:
        resp = httpx.post(_RESEND_API_URL, json=payload, headers=headers, timeout=20.0)
    except httpx.HTTPError as exc:
        raise RuntimeError("Resend API unreachable: %s" % exc) from exc
    if resp.status_code not in (200, 201):
        raise RuntimeError(
            "Resend API error HTTP %s: %s" % (resp.status_code, resp.text[:200]))


def send_license_email(to: str, key: str, product_name: str = "Pro") -> None:
    """Deliver a license key to *to*.

    Prefers the Resend HTTPS API (``ENGRAPHIS_RESEND_API_KEY`` or the Resend key
    already in ``ENGRAPHIS_SMTP_PASSWORD``) because many hosts — Railway included —
    block outbound SMTP ports, which makes ``smtplib`` hang until timeout. Falls
    back to SMTP (``ENGRAPHIS_SMTP_*``). Raises RuntimeError if nothing is
    configured, and raises on delivery failure.
    """
    subject = "Your Engraphis %s License Key" % product_name
    text_body = _license_email_text(key, product_name)
    from_addr = os.environ.get("ENGRAPHIS_SMTP_FROM", "keys@engraphis.com").strip()

    api_key = _resend_api_key()
    if api_key:
        logger.info("sending license email to %s via Resend API", to)
        _send_via_resend_api(to, subject, text_body, from_addr, api_key)
        logger.info("license email delivered to %s (Resend API)", to)
        return

    smtp_host = os.environ.get("ENGRAPHIS_SMTP_HOST", "").strip()
    smtp_port = int(os.environ.get("ENGRAPHIS_SMTP_PORT", "587"))
    smtp_user = os.environ.get("ENGRAPHIS_SMTP_USER", "").strip()
    smtp_pass = os.environ.get("ENGRAPHIS_SMTP_PASSWORD", "").strip()
    if not smtp_host or not smtp_user or not smtp_pass:
        raise RuntimeError(
            "No email delivery configured — set ENGRAPHIS_RESEND_API_KEY (preferred) "
            "or ENGRAPHIS_SMTP_HOST/USER/PASSWORD"
        )

    msg = email.message.EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text_body)
    logger.info("sending license email to %s via %s:%d", to, smtp_host, smtp_port)
    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(smtp_user, smtp_pass)
        smtp.send_message(msg)
    logger.info("license email delivered to %s (SMTP)", to)


def _fallback_dir() -> Path:
    """Directory for the operator-only manual-fulfillment fallback file."""
    state = os.environ.get("ENGRAPHIS_WEBHOOK_STATE", "").strip()
    if state:
        return Path(state).expanduser().resolve().parent
    db = os.environ.get("ENGRAPHIS_DB_PATH", "").strip()
    if db and db != ":memory:":
        try:
            return Path(db).expanduser().resolve().parent
        except Exception:
            pass
    return Path.cwd()


def _persist_fallback_key(email_addr: str, key: str, product_name: str) -> Optional[Path]:
    """Write an undelivered key to a 0600 operator-only file (NEVER the app log).

    Returns the file path, or None if it couldn't be written. The raw key must not
    hit application logs — log aggregation is usually less protected than this file.
    """
    path = _fallback_dir() / "undelivered_license_keys.tsv"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Create with 0600 before writing any key material.
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write("%d\t%s\t%s\t%s\n" % (int(time.time()), email_addr, product_name, key))
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return path
    except OSError:
        return None


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
    except Exception as exc:  # noqa: BLE001 — any delivery failure must not lose a paid key
        # Delivery failed (misconfig, provider error, network). Do NOT log the raw
        # key (log aggregation is less protected than a local 0600 file). Persist
        # it for manual delivery and log only a fingerprint + the failure reason.
        # We still return the key so the webhook 202s (Polar won't retry-storm);
        # the operator delivers from the fallback file.
        fp = hashlib.sha256(key.encode("ascii")).hexdigest()[:12]
        saved = _persist_fallback_key(email_addr, key, product_name)
        if saved:
            logger.warning(
                "email delivery failed (%s) — key %s for %s saved to %s (deliver manually)",
                exc, fp, email_addr, saved)
        else:
            logger.error(
                "email delivery failed (%s) AND could not persist key %s for %s — "
                "reissue via `python -m scripts.license_admin issue`", exc, fp, email_addr)
    return key
