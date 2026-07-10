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
import math
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

    Match on substrings so "Engraphis Pro Monthly" and "Engraphis Pro Annual" both
    resolve to ``pro``. A paid order with an *unrecognized* product name still
    resolves to ``pro`` (never free) and logs loudly: a customer who paid must never
    be silently stiffed with a useless free-tier key. Correct Pro-vs-Team routing
    depends on the Polar product name containing "pro"/"team" — keep them named so.
    """
    name = (product_name or "").lower()
    if "team" in name:
        return "team"
    if "pro" in name:
        return "pro"
    logger.warning(
        "unrecognized paid product '%s' — defaulting to Pro so the buyer still gets a "
        "working key. Name your Polar products with 'Pro'/'Team' for correct tiering.",
        product_name)
    return "pro"


def _key_days(product_name: str, metadata: dict) -> int:
    """How long an auto-issued key stays valid.

    Precedence: explicit ``license_days`` metadata → annual detection (≈13 months so a
    late renewal never locks a paying customer out) → monthly default with a 5-day
    grace over the 30-day cycle (renewal fires a fresh ``order.paid`` each period)."""
    try:
        explicit = int(metadata.get("license_days") or 0)
    except (TypeError, ValueError):
        explicit = 0
    if explicit > 0:
        return explicit
    name = (product_name or "").lower()
    if "annual" in name or "year" in name or "yr" in name:
        return 395
    return 35


def _plan_label(product_name: str) -> str:
    """Clean tier label for customer-facing email copy ("Pro"/"Team")."""
    return _map_polar_product_to_plan(product_name).title()


def _extract_email(data: dict) -> Optional[str]:
    """Pull the buyer email from an order OR subscription payload, defensively."""
    cust = data.get("customer") or {}
    user = data.get("user") or {}
    return (cust.get("email") or data.get("customer_email")
            or data.get("email") or user.get("email"))


def _extract_product_name(data: dict) -> str:
    product = data.get("product") or {}
    return product.get("name") or data.get("product_name") or "Pro"


def _extract_seats(data: dict) -> int:
    """Seat count from an order or subscription payload (Team). Defaults to 1."""
    product = data.get("product") or {}
    meta = product.get("metadata") or data.get("metadata") or {}
    for candidate in (data.get("seats"), data.get("quantity"), meta.get("seats")):
        try:
            n = int(candidate)
            if n > 0:
                return n
        except (TypeError, ValueError):
            continue
    return 1


def _parse_ts(value) -> Optional[float]:
    """Coerce a Polar timestamp (ISO-8601 string or epoch number) to float epoch."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _trial_days(period_end, *, now: Optional[float] = None) -> int:
    """How long a trial key stays valid: through the trial's ``current_period_end``,
    rounded UP to whole days (so it always covers the full trial, never expiring a
    legit trial user early) but with NO extra grace — a canceled trial should lapse
    right at trial end so no one keeps Pro without paying. Falls back to
    ENGRAPHIS_TRIAL_DAYS (default 4, matching a 3-day trial) if the end is missing."""
    now = now if now is not None else time.time()
    end = _parse_ts(period_end)
    if end and end > now:
        return max(1, math.ceil((end - now) / 86400))
    try:
        return max(1, int(os.environ.get("ENGRAPHIS_TRIAL_DAYS", "4")))
    except ValueError:
        return 4


def issue_key(email_addr: str, product_name: str = "pro", seats: int = 1,
               days: Optional[int] = None, metadata: Optional[dict] = None) -> str:
    """Generate a signed ``ENGR1.xxx.yyy`` key for *email_addr*.

    Uses the pinned vendor signing key (``.secrets/vendor_signing.key`` or
    ``ENGRAPHIS_SIGNING_KEY`` env). ``product_name`` maps to a plan tier; ``days``
    (or product/metadata inference via :func:`_key_days`) sets validity.
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
        plan = "pro"
    if days is None:
        days = _key_days(product_name, metadata or {})

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


def _license_email_text(key: str, product_name: str, is_trial: bool = False) -> str:
    intro = (f"Your Engraphis {product_name} free trial has started!"
             if is_trial else f"Thank you for purchasing Engraphis {product_name}!")
    return f"""{intro}

Your license key:

    {key}

To activate:
    1. Open the Engraphis dashboard (engraphis-dashboard, http://127.0.0.1:8700)
    2. Go to Settings -> License
    3. Paste the key and click Activate

Or set the ENGRAPHIS_LICENSE_KEY environment variable, or save the key to
~/.engraphis/license.key.

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


def send_license_email(to: str, key: str, product_name: str = "Pro",
                       is_trial: bool = False) -> None:
    """Deliver a license key to *to*.

    Prefers the Resend HTTPS API (``ENGRAPHIS_RESEND_API_KEY`` or the Resend key
    already in ``ENGRAPHIS_SMTP_PASSWORD``) because many hosts — Railway included —
    block outbound SMTP ports, which makes ``smtplib`` hang until timeout. Falls
    back to SMTP (``ENGRAPHIS_SMTP_*``). Raises RuntimeError if nothing is
    configured, and raises on delivery failure. ``product_name`` should be a clean
    tier label ("Pro"/"Team"); ``is_trial`` selects trial-vs-purchase copy.
    """
    subject = ("Your Engraphis %s Trial License Key" if is_trial
               else "Your Engraphis %s License Key") % product_name
    text_body = _license_email_text(key, product_name, is_trial=is_trial)
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


def _issue_and_email(email_addr: str, product_name: str, seats: int,
                     days: Optional[int], *, is_trial: bool = False) -> str:
    """Mint a signed key and email it. On ANY delivery failure, persist the key to
    the 0600 fallback file (never the log) and still return it, so a paid or trial
    key is never lost and the webhook can 202 without a Polar retry-storm."""
    key = issue_key(email_addr, product_name=product_name, seats=seats, days=days)
    label = _plan_label(product_name)
    try:
        send_license_email(email_addr, key, product_name=label, is_trial=is_trial)
    except Exception as exc:  # noqa: BLE001 — a delivery failure must not lose a key
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


def handle_order_paid(payload: dict) -> Optional[str]:
    """Fulfill a Polar ``order.paid`` event — mint a period-bounded key and email it.

    Covers direct purchases, trial conversions, and each renewal (a fresh
    ``order.paid`` fires per cycle). Returns the key, or ``None`` if the payload has
    no customer email (logged; the route then leaves it for a corrected retry).
    """
    email_addr = _extract_email(payload)
    if not email_addr:
        logger.warning("order.paid missing customer email — cannot issue key")
        return None
    product = payload.get("product") or {}
    product_name = _extract_product_name(payload)
    seats = _extract_seats(payload)
    days = _key_days(product_name, product.get("metadata") or {})
    return _issue_and_email(email_addr, product_name, seats, days)


def handle_subscription_created(payload: dict) -> Optional[str]:
    """Fulfill the START of a subscription that is in a free TRIAL: mint a key that
    expires at the trial's end (+1 day grace) and email it immediately, so a trial
    customer has Pro during the trial.

    A non-trial subscription is a no-op here (returns ``None``) — its key is issued
    by the matching ``order.paid`` when payment is actually taken. That is what makes
    cancellation safe: a canceled trial never produces an ``order.paid``, so the only
    key that exists is the short trial key, which simply expires. Offline keys can't
    be revoked, so bounding their lifetime to the paid/trial period IS the revocation.
    """
    status = str(payload.get("status", "")).strip().lower()
    if status != "trialing":
        return None  # not a trial; order.paid handles paid activation & renewals
    email_addr = _extract_email(payload)
    if not email_addr:
        logger.warning("subscription.created (trial) missing customer email")
        return None
    product_name = _extract_product_name(payload)
    seats = _extract_seats(payload)
    days = _trial_days(payload.get("current_period_end"))
    logger.info("trial started for %s (%s) — issuing %d-day key",
                email_addr, product_name, days)
    return _issue_and_email(email_addr, product_name, seats, days, is_trial=True)
