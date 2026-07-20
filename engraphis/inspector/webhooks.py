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
import hmac
import logging
import math
import os
import smtplib
import stat
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


def _log_ref(value: object) -> str:
    """Return a non-reversible correlation id for customer/provider values."""
    return hashlib.sha256(str(value or "").encode("utf-8", "replace")).hexdigest()[:12]

# Canonical public links used in outbound customer-facing email footers. Centralized
# as constants so the URLs can't drift per-email — a previous invite shipped a
# wrong repo URL (github.com/engraphis/engraphis) that 404'd for paying customers.
# Override either per-deployment via env if a mirror/fork is the canonical surface.
SITE_URL = os.environ.get("ENGRAPHIS_SITE_URL", "https://engraphis.com/").strip()
REPO_URL = os.environ.get("ENGRAPHIS_REPO_URL",
                          "https://github.com/Coding-Dev-Tools/engraphis").strip()

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
        resolved = path.expanduser().resolve(strict=True)
        info = resolved.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size <= 0 or info.st_size > 1024:
            raise RuntimeError("signing key file must be a small regular file")
        if os.name != "nt" and stat.S_IMODE(info.st_mode) & 0o077:
            raise RuntimeError("signing key file must be owner-only (chmod 600)")
        text = resolved.read_text(encoding="utf-8").strip()
    except RuntimeError:
        raise
    except (OSError, UnicodeError):
        raise RuntimeError(
            "Signing key file is unavailable — set ENGRAPHIS_VENDOR_SIGNING_KEY to the "
            "vendor seed (64 hex chars) or run `python -m scripts.license_admin keygen`"
        ) from None
    return _hex_to_seed(text, source="signing key file")


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
        path = Path(val).expanduser()
        if path.exists():
            return _read_seed_file(path)
        raise RuntimeError(
            f"{env_name} is neither a 64-char hex seed nor a path to a seed file")
    return _read_seed_file(Path(_DEFAULT_KEY_PATH))


def _product_plan_overrides() -> dict:
    """Operator-configured exact product-name → plan map (``ENGRAPHIS_POLAR_PRODUCT_MAP``).

    JSON object of ``{"<product name>": "pro"|"team", ...}`` matched case-insensitively
    and exactly, letting an operator pin tiering precisely instead of relying on the
    substring heuristic — e.g. a product literally named "Engraphis Enterprise" that
    should map to ``team``. Consulted before the built-in substring rules. Malformed
    JSON or unknown plan values are ignored (the substring fallback still applies), so a
    bad env var can never stiff a paying customer.
    """
    raw = os.environ.get("ENGRAPHIS_POLAR_PRODUCT_MAP", "").strip()
    if not raw:
        return {}
    try:
        import json
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("ENGRAPHIS_POLAR_PRODUCT_MAP is not valid JSON — ignoring")
        return {}
    if not isinstance(parsed, dict):
        return {}
    out = {}
    for name, plan in parsed.items():
        plan = str(plan or "").strip().lower()
        if plan in PLAN_FEATURES:
            out[str(name).strip().lower()] = plan
    return out


def _map_polar_product_to_plan(product_name: str, product_id: str = "") -> str:
    """Map Polar product name to an Engraphis plan tier.

    An operator-configured exact-name override (``ENGRAPHIS_POLAR_PRODUCT_MAP``) wins so
    tiering can be pinned to real business data rather than inferred. Otherwise match on
    substrings so "Engraphis Pro Monthly" and "Engraphis Pro Annual" both resolve to
    ``pro``. A paid order with an *unrecognized* product name still resolves to ``pro``
    (never free) and logs loudly: a customer who paid must never be silently stiffed with
    a useless free-tier key. Correct Pro-vs-Team routing depends on the Polar product name
    containing "pro"/"team" (or an explicit override) — keep them named so.
    """
    from engraphis.commercial import product_for_id, service_mode
    configured = product_for_id(product_id)
    if configured:
        return configured["plan"]
    if service_mode() == "vendor":
        raise RuntimeError("unrecognized Polar product id")
    name = (product_name or "").lower()
    override = _product_plan_overrides().get(name.strip())
    if override:
        return override
    if "team" in name:
        return "team"
    if "pro" in name:
        return "pro"
    logger.warning(
        "unrecognized paid product ref=%s — defaulting to Pro so the buyer still gets a "
        "working key. Name your Polar products with 'Pro'/'Team' for correct tiering.",
        _log_ref(product_name))
    return "pro"


def _key_days(product_name: str, metadata: dict, product_id: str = "") -> int:
    """How long an auto-issued key stays valid.

    Exact configured product id wins in the vendor service. Development compatibility
    then uses explicit ``license_days`` metadata, name-based annual detection, or the
    35-day monthly default."""
    # In the isolated control plane, the exact manifest-backed product id is the
    # authority. Editable Polar metadata must not turn a recognized monthly product into
    # an arbitrarily long-lived entitlement. Combined-mode compatibility still honors
    # the historical metadata override when there is no configured exact product id.
    from engraphis.commercial import product_for_id
    configured = product_for_id(product_id)
    if configured:
        return 395 if configured["interval"] == "annual" else 35
    try:
        explicit = int(metadata.get("license_days") or 0)
    except (TypeError, ValueError):
        explicit = 0
    if explicit > 0:
        return explicit
    # Compatibility inference only: the production vendor path returned above.
    name = (product_name or "").lower()
    if "annual" in name or "year" in name or "yr" in name:
        return 395
    return 35


# Grace added over a subscription's current_period_end when re-issuing a key mid-cycle,
# so a slightly-late renewal webhook never briefly locks out a paying customer. Kept
# small (matches the 5-day grace baked into the 35-day monthly _key_days window) — the
# whole point of bounding to the period end is that a mid-cycle change must NOT hand out
# a fresh full 35/395-day window that outlives the paid period.
_KEY_PERIOD_GRACE_DAYS = 5


def _subscription_key_days(payload: dict, product_name: str, metadata: dict,
                           product_id: str = "", *, now: Optional[float] = None) -> int:
    """Validity (in days) for a key re-issued from a Subscription object mid-cycle.

    Bounds the key to the subscription's ``current_period_end`` (+ a small fixed grace)
    rather than a fresh full ``_key_days`` window measured from now. Without this, a
    late-cycle seat change would mint a key valid a whole extra billing cycle past the
    paid period (≈12 months for annual) — and since cancellation is enforced by letting
    the period-bounded key expire, that overrun is unpaid access. Falls back to
    :func:`_key_days` only when ``current_period_end`` is absent from the payload.
    """
    now = now if now is not None else time.time()
    end = _parse_ts(payload.get("current_period_end"))
    if end and end > now:
        return max(1, math.ceil((end - now) / 86400) + _KEY_PERIOD_GRACE_DAYS)
    return _key_days(product_name, metadata, product_id)


def _plan_label(product_name: str, product_id: str = "") -> str:
    """Clean tier label for customer-facing email copy ("Pro"/"Team")."""
    return _map_polar_product_to_plan(product_name, product_id).title()


def _extract_email(data: dict) -> Optional[str]:
    """Pull the buyer email from an order OR subscription payload, defensively."""
    cust = data.get("customer") or {}
    user = data.get("user") or {}
    if not isinstance(cust, dict):
        cust = {}
    if not isinstance(user, dict):
        user = {}
    candidate = (cust.get("email") or data.get("customer_email")
                 or data.get("email") or user.get("email"))
    if not isinstance(candidate, str):
        return None
    normalized = candidate.strip().lower()
    from engraphis.inspector.auth import _EMAIL_RE
    return normalized if _EMAIL_RE.match(normalized) else None

def _extract_subscription_id(data: dict, *, object_is_subscription: bool = False) -> str:
    """Normalized Polar subscription id carried into the signed license payload."""
    raw = data.get("subscription_id")
    if not raw:
        subscription = data.get("subscription")
        raw = subscription.get("id") if isinstance(subscription, dict) else subscription
    if not raw and object_is_subscription:
        raw = data.get("id")
    return str(raw or "").strip()[:128]

def _extract_order_id(data: dict) -> str:
    """Normalized Polar order id from an order-shaped payload."""
    return str(data.get("id") or data.get("order_id") or "").strip()[:128]

def _extract_subscription_id(data: dict, *, object_is_subscription: bool = False) -> str:
    """Normalized Polar subscription id from order, subscription, or nested payload."""
    raw = data.get("subscription_id")
    if not raw:
        subscription = data.get("subscription")
        raw = subscription.get("id") if isinstance(subscription, dict) else subscription
    if not raw and object_is_subscription:
        raw = data.get("id")
    return str(raw or "").strip()[:128]


def _extract_order_id(data: dict) -> str:
    """Normalized Polar order id from an order-shaped payload."""
    return str(data.get("id") or data.get("order_id") or "").strip()[:128]


def _extract_product_name(data: dict) -> str:
    product = data.get("product")
    if not isinstance(product, dict):
        product = {}
    raw = product.get("name") or data.get("product_name") or "Pro"
    # Keep provider-controlled labels out of email/TSV control sequences while retaining
    # ordinary Unicode product names.
    clean = "".join(ch for ch in str(raw).strip() if ch.isprintable())[:80]
    return clean or "Pro"


def _extract_seats(data: dict) -> int:
    """Seat count from an order or subscription payload (Team). Defaults to 1.

    Polar's native seat-based pricing (what "Engraphis Team" actually uses — see
    the dashboard's "Seat Pricing" price type) puts the seat count buyers chose at
    checkout in a ``seats`` field, but WHERE that field lives depends on the
    payload shape:
      - ``subscription.created``/``subscription.updated`` payloads ARE a
        Subscription object, so ``seats`` is top-level.
      - ``order.paid``/``order.created`` payloads are an Order object. Order's
        own top-level ``seats`` is populated ONLY for seat-based *one-time*
        orders (per Polar's schema) — for our recurring Team subscription it is
        null, and the real count lives nested at ``order.subscription.seats``.
    Checking only the top-level field (the old behavior) meant every recurring
    Team order.paid fell through to product metadata (unset) and silently
    defaulted every buyer to 1 seat regardless of how many they paid for.
    ``quantity`` and ``metadata.seats`` are kept as defensive fallbacks for
    payload shapes Polar doesn't currently send for this product.
    """
    product = data.get("product")
    if not isinstance(product, dict):
        product = {}
    meta = product.get("metadata") or data.get("metadata") or {}
    if not isinstance(meta, dict):
        meta = {}
    subscription = data.get("subscription") or {}
    if not isinstance(subscription, dict):
        subscription = {}
    for candidate in (
        data.get("seats"),
        subscription.get("seats"),
        data.get("quantity"),
        meta.get("seats"),
    ):
        try:
            n = int(candidate)
            if n > 0:
                return n
        except (OverflowError, TypeError, ValueError):
            continue
    return 1


def _parse_ts(value) -> Optional[float]:
    """Coerce a Polar timestamp (ISO-8601 string or epoch number) to float epoch."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    try:
        from datetime import datetime
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        return parsed if math.isfinite(parsed) else None
    except (OverflowError, OSError, ValueError, TypeError):
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
              days: Optional[int] = None, metadata: Optional[dict] = None,
              *, trial: bool = False, record: bool = True,
              subscription_id: str = "", order_id: str = "",
              product_id: str = "") -> str:
    """Generate a signed ``ENGR1.xxx.yyy`` key for *email_addr*.

    Uses the pinned vendor signing key (``.secrets/vendor_signing.key`` or
    ``ENGRAPHIS_SIGNING_KEY`` env). ``product_name`` maps to a plan tier; ``days``
    (or product/metadata inference via :func:`_key_days`) sets validity. Polar ids
    are signed into auto-issued keys so refund webhooks can revoke exactly the
    affected order/subscription without touching unrelated purchases.
    """
    secret = _load_signing_secret()
    pub = ed25519_public_key(secret).hex()
    if pub == _DEV_VENDOR_PUBKEY_HEX:
        logger.warning(
            "signing with DEV keypair — keys are forgeable. Generate a new signer with "
            "`python -m scripts.license_admin keygen --key-file <new-secure-path>` "
            "before real sales."
        )

    plan = _map_polar_product_to_plan(product_name, product_id)
    if plan not in PLAN_FEATURES:
        plan = "pro"
    if days is None:
        days = _key_days(product_name, metadata or {}, product_id)

    subscription_id = str(subscription_id or "").strip()[:128]
    order_id = str(order_id or "").strip()[:128]
    now = time.time()
    payload = {
        "v": 1,
        "plan": plan,
        "email": email_addr,
        "seats": max(1, int(seats)),
        "issued": int(now),
        "expires": int(now + days * 86400),
        "signing_key_id": pub[:16],
    }
    if subscription_id:
        payload["subscription_id"] = subscription_id
    if order_id:
        payload["order_id"] = order_id
    if trial:
        payload["trial"] = 1               # signed trial marker -> License.is_trial (UI)
    # Server-side enforcement (online-only): every minted key carries a signed
    # ``enforce: "cloud"`` claim plus the license-server URL, so the client requires a live
    # lease (register/renew against that URL) and the key is useless offline or after
    # revocation. The URL is ENGRAPHIS_KEY_CLOUD_URL if set, else the isolated license
    # service — so keys are cloud-enforced by DEFAULT now, not opt-in. The
    # claim rides inside the Ed25519-signed payload, so customers cannot strip it.
    from engraphis.config import (
        DEFAULT_LICENSE_SERVER_URL,
        canonicalize_license_server_url,
    )
    enforce_url = canonicalize_license_server_url(
        os.environ.get("ENGRAPHIS_KEY_CLOUD_URL", "") or DEFAULT_LICENSE_SERVER_URL)
    if enforce_url:
        payload["enforce"] = "cloud"
        payload["cloud_url"] = enforce_url
    key = compose_key(payload, secret)
    logger.info(
        "issued %s key for customer_ref=%s (expires in %d days)",
        plan, _log_ref(email_addr), days)
    if record:
        try:
            from engraphis.inspector.license_registry import (
                record_issued, revoke_superseded)
            key_id = record_issued(key)
            if subscription_id:
                revoke_superseded(subscription_id, key_id)
        except Exception as exc:
            from engraphis.commercial import service_mode
            if service_mode() == "vendor":
                # A cloud-enforced paid key that is absent from the registry cannot
                # activate. Fail before enqueueing its email so Polar retries instead of
                # recording a purchase-without-delivery.
                raise RuntimeError("license registry unavailable") from exc
            logger.warning("could not record/reconcile issued key in registry (%s)",
                           type(exc).__name__)
    return key


_RESEND_API_URL = "https://api.resend.com/emails"


def _license_email_text(key: str, product_name: str, is_trial: bool = False) -> str:
    intro = (f"Your Engraphis {product_name} free trial has started!"
             if is_trial else f"Thank you for purchasing Engraphis {product_name}!")
    verification_note = (
        "Your key activates against our license server automatically on first use and "
        "re-checks periodically — keep this device online to use paid features.")
    return f"""{intro}

Your license key:

    {key}

To activate:
    1. Open the Engraphis dashboard (engraphis-dashboard, http://127.0.0.1:8700)
    2. Go to Settings -> License
    3. Paste the key and click Activate

Or set the ENGRAPHIS_LICENSE_KEY environment variable, or save the key to
~/.engraphis/license.key.

{verification_note} Keep it safe.

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
                         api_key: str, *, reply_to: Optional[str] = None,
                         idempotency_key: str = "") -> str:
    """Send via Resend's HTTPS API (port 443). Works where outbound SMTP is
    blocked (Railway, Fly, many PaaS). Raises RuntimeError on any failure.

    Uses httpx (a declared dependency) rather than urllib, and sets an explicit
    User-Agent: ``api.resend.com`` is behind Cloudflare, which blocks the default
    ``Python-urllib`` client signature with a 403 "error code: 1010". httpx's
    mainstream TLS fingerprint plus a named UA passes that bot check.
    """
    import httpx

    payload = {"from": from_addr, "to": [to], "subject": subject, "text": text_body}
    if reply_to:
        payload["reply_to"] = reply_to
    headers = {
        "Authorization": "Bearer %s" % api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Engraphis/1.0 (+https://engraphis.com)",
    }
    provider_key = str(idempotency_key or "").strip()
    if provider_key:
        # Resend retains POST /emails idempotency keys for 24 hours. The durable
        # outbox's stable message id closes the crash window where Resend accepted a
        # send but this process died before recording the provider response.
        headers["Idempotency-Key"] = provider_key[:256]
    try:
        resp = httpx.post(_RESEND_API_URL, json=payload, headers=headers, timeout=20.0)
    except httpx.HTTPError:
        # httpx exceptions retain the request URL and can include provider details.
        # Keep the delivery boundary useful without reflecting that data outward.
        raise RuntimeError("Resend API is unreachable") from None
    if resp.status_code not in (200, 201):
        # The provider body is untrusted and can echo addresses, message content, or
        # credentials. Status is sufficient for retry/operations diagnostics.
        raise RuntimeError("Resend API rejected the request (HTTP %s)" % resp.status_code)
    try:
        response_body = resp.json()
        provider_id = response_body.get("id") if isinstance(response_body, dict) else None
    except (ValueError, TypeError, AttributeError):
        provider_id = None
    if not isinstance(provider_id, str) or not provider_id \
            or len(provider_id) > 160 \
            or any(ord(char) < 33 or ord(char) == 127 for char in provider_id):
        # A successful send without its stable provider id cannot be correlated with
        # delivery/bounce events. Keep the outbox retryable; its idempotency key makes a
        # retry safe even when the provider accepted the first request.
        raise RuntimeError("Resend API response omitted its message id")
    return provider_id


def email_configured() -> bool:
    """True if THIS process has its own outbound email delivery set up (Resend or
    SMTP). Callers use this to decide *before* attempting a send whether to use
    local delivery or fall back to something else (e.g. the vendor control plane's
    ``/license/v1/team-invite`` for a self-hosted dashboard with no mail account
    of its own) — cheaper and clearer than attempting-and-catching."""
    if _resend_api_key():
        return True
    return bool(os.environ.get("ENGRAPHIS_SMTP_HOST", "").strip()
                and os.environ.get("ENGRAPHIS_SMTP_USER", "").strip()
                and os.environ.get("ENGRAPHIS_SMTP_PASSWORD", "").strip())


def _deliver_text_email(to: str, subject: str, text_body: str,
                        reply_to: Optional[str] = None,
                        idempotency_key: str = "") -> tuple[str, str]:
    """Low-level provider call used only by the durable outbox worker."""
    from_addr = os.environ.get("ENGRAPHIS_SMTP_FROM", "keys@engraphis.com").strip()

    api_key = _resend_api_key()
    if api_key:
        logger.info("sending transactional email via Resend API")
        provider_id = _send_via_resend_api(
            to, subject, text_body, from_addr, api_key, reply_to=reply_to,
            idempotency_key=idempotency_key) or ""
        logger.info("transactional email accepted by Resend API")
        return "resend", provider_id

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
    if reply_to:
        msg["Reply-To"] = reply_to
    if idempotency_key:
        # Resend documents this equivalent header for its SMTP transport. Other SMTP
        # providers safely preserve or ignore the non-secret extension header.
        msg["Resend-Idempotency-Key"] = str(idempotency_key)[:256]
    msg.set_content(text_body)
    logger.info("sending transactional email via configured SMTP provider")
    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(smtp_user, smtp_pass)
        smtp.send_message(msg)
    logger.info("transactional email accepted by SMTP provider")
    return "smtp", ""


def _send_text_email(to: str, subject: str, text_body: str, *,
                     reply_to: Optional[str] = None, kind: str = "transactional",
                     idempotency_key: str = "") -> str:
    """Persist and immediately attempt a plain-text transactional email.

    Prefers the Resend HTTPS API (``ENGRAPHIS_RESEND_API_KEY`` or the Resend key
    already in ``ENGRAPHIS_SMTP_PASSWORD``) because many hosts — Railway included —
    block outbound SMTP ports, which makes ``smtplib`` hang until timeout. Falls
    back to SMTP (``ENGRAPHIS_SMTP_*``). Raises RuntimeError if nothing is
    configured, and raises on delivery failure — callers decide how to log/fall
    back (see :func:`_issue_and_email`'s 0600 fallback file for the license-key
    case; a team invite has no secret to lose, so it just logs and moves on).
    ``reply_to``, when given, routes replies to a human (e.g. the admin who sent
    a team invite) instead of the shared sending address.
    """
    from engraphis import email_outbox
    message_id = email_outbox.enqueue(
        kind, to, subject, text_body, reply_to=reply_to,
        idempotency_key=idempotency_key)
    email_outbox.deliver_now(message_id, _deliver_text_email)
    return message_id


def send_license_email(to: str, key: str, product_name: str = "Pro",
                       is_trial: bool = False, *, idempotency_key: str = "") -> None:
    """Deliver a license key to *to*.

    Raises RuntimeError if nothing is configured, and raises on delivery
    failure — see :func:`_send_text_email`. ``product_name`` should be a clean
    tier label ("Pro"/"Team"); ``is_trial`` selects trial-vs-purchase copy.
    """
    subject = ("Your Engraphis %s Trial License Key" if is_trial
               else "Your Engraphis %s License Key") % product_name
    text_body = _license_email_text(key, product_name, is_trial=is_trial)
    _send_text_email(to, subject, text_body,
                     kind="trial_license" if is_trial else "purchase_license",
                     idempotency_key=idempotency_key)


def _password_reset_email_text(name: str, reset_url: str) -> str:
    greeting = "Hi %s," % name if name else "Hi,"
    return f"""{greeting}

Someone requested a password reset for your Engraphis dashboard account. If this
was you, choose a new password here — this link works once and expires in 30
minutes:

    {reset_url}

If you didn't request this, you can safely ignore this email: your password has
not been changed.

— The Engraphis team
"""


def send_password_reset_email(to: str, name: str, reset_url: str) -> None:
    """Deliver a one-time password-reset link to *to* (``/api/auth/forgot``).

    Raises on delivery failure (see :func:`_send_text_email`); the caller
    (``routes.v2_team.forgot``) treats this as best-effort and must never let a
    delivery failure change the HTTP response — the "forgot password" endpoint
    always answers identically regardless of outcome, so a failed send can't be
    used to fingerprint which addresses have accounts.
    """
    subject = "Reset your Engraphis dashboard password"
    text_body = _password_reset_email_text(name, reset_url)
    _send_text_email(to, subject, text_body, kind="password_reset")


def queue_password_reset_email(to: str, name: str, reset_url: str, *,
                               idempotency_key: str) -> str:
    """Durably queue a vendor-relayed password-reset email without sending inline.

    The control-plane endpoint uses this path so a temporary provider outage leaves a
    recoverable pending operation. The outbox worker performs the bounded retries; the
    caller receives neither the reset URL nor a provider message identifier.
    """
    from engraphis import email_outbox
    return email_outbox.enqueue(
        "password_reset", to, "Reset your Engraphis dashboard password",
        _password_reset_email_text(name, reset_url),
        idempotency_key=idempotency_key,
    )


def _trial_verify_email_text(verify_url: str, plan: str, minutes: int) -> str:
    label = plan.title()
    return f"""Someone requested a free {label} trial for Engraphis using this email address.

Confirm it's you and get your trial key here — this link works once and expires in
{minutes} minutes:

    {verify_url}

Opening it shows a confirmation page with an "Activate my {label} trial" button.
Clicking that button mints your trial key and shows it, with instructions to activate
it in your dashboard (Settings -> License -> paste key -> Activate).

If you didn't request this, you can safely ignore this email: no trial has been
issued, and none will be unless that button is clicked. Simply opening the link — or
a mail scanner opening it for you — grants nothing.

— The Engraphis team
"""


def send_trial_verification_email(to: str, verify_url: str, plan: str = "team", *,
                                   minutes: int = 30) -> None:
    """Deliver a one-time magic link that mints a self-serve trial key on confirmation.

    Part of the 2026-07-14 trial-abuse hardening: ``inspector.license_cloud``'s
    ``POST /license/v1/start-trial`` no longer issues a key synchronously from a bare
    machine_id (trivially reset by deleting one local file — see that module's
    comment); it emails this link instead. Opening the link (``GET .../start-trial/
    verify``) only renders a confirm page — the key is minted by the ``POST`` that
    page's button sends, so a mail link-prescanner GETting the URL on the recipient's
    behalf cannot burn the one-time grant. Raises on delivery failure (see
    :func:`_send_text_email`) — unlike the password-reset send above, the caller DOES
    let a failure change the HTTP response (502): trial start is opt-in self-serve
    (no account to enumerate), and silently swallowing the failure would strand the
    requester with a pending token they can never redeem.
    """
    subject = "Confirm your Engraphis %s trial" % plan.title()
    text_body = _trial_verify_email_text(verify_url, plan, minutes)
    _send_text_email(to, subject, text_body, kind="trial_confirmation")


def send_trial_claim_email(to: str, verify_url: str, plan: str = "team", *,
                           minutes: int = 30, idempotency_key: str = "") -> None:
    """Send scanner-safe confirmation for a deployment-bound trial claim."""
    label = plan.title()
    subject = "Confirm your Engraphis %s trial" % label
    text_body = f"""Someone requested a free {label} trial for an Engraphis deployment.

Review and confirm the request here. The link expires in {minutes} minutes:

    {verify_url}

Opening the link only displays a confirmation page. The trial activates only after you
press the confirmation button. The signed license key is delivered directly to the
requesting deployment and is never displayed in your browser or sent by email.

If you did not request this trial, ignore this message.

— The Engraphis team
"""
    _send_text_email(
        to, subject, text_body, kind="trial_confirmation",
        idempotency_key=idempotency_key)


# The canonical hosted team dashboard (the Railway deployment members sign in
# to). Used as the default ``dashboard_url`` in team-invite emails when neither
# the caller nor ``ENGRAPHIS_DASHBOARD_URL`` supplies one, so an invite always
# carries a clickable "sign in" link instead of "ask your admin". A self-hoster
# running their own dashboard overrides this by setting ``ENGRAPHIS_DASHBOARD_URL``
# (or by passing ``dashboard_url`` explicitly), which both take precedence.
DEFAULT_TEAM_DASHBOARD_URL = "https://team.engraphis.com/"


def _invitation_email_text(name: str, role: str, invite_url: str,
                           invited_by: str = "") -> str:
    greeting = "Hi %s," % name if name else "Hi,"
    inviter = ("%s invited you" % invited_by if invited_by
               else "You have been invited")
    return """%s

%s to join an Engraphis team as a %s.

Choose your password and accept the invitation here. This one-time link expires in
72 hours and is replaced if your administrator resends the invitation:

    %s

The invitation does not contain a temporary password or the team's account-wide
license key. After signing in, create your own scoped, revocable device token from
Settings -> Connect an agent if you want to use a local client.

If you did not expect this invitation, ignore this message.

— The Engraphis team

Learn more: %s
Source & self-hosting: %s
""" % (greeting, inviter, role, invite_url, SITE_URL, REPO_URL)


def send_team_invite_email(to: str, name: str, role: str, *, invited_by: str = "",
                           invite_url: str = "", key: str = "",
                           dashboard_url: Optional[str] = None,
                           idempotency_key: str = "") -> None:
    """Send a one-time invitation URL. The deprecated ``key`` argument is ignored."""
    from engraphis.inspector.auth import _EMAIL_RE
    subject = "Accept your Engraphis team invitation"
    if not invite_url:
        base = (dashboard_url or os.environ.get("ENGRAPHIS_DASHBOARD_URL", "")
                or DEFAULT_TEAM_DASHBOARD_URL).strip().rstrip("/")
        invite_url = base
    text_body = _invitation_email_text(name, role, invite_url, invited_by=invited_by)
    reply_to = invited_by if invited_by and _EMAIL_RE.match(invited_by) else None
    _send_text_email(
        to, subject, text_body, reply_to=reply_to, kind="invitation",
        idempotency_key=idempotency_key)


def queue_team_invite_email(to: str, name: str, role: str, *, invited_by: str = "",
                            invite_url: str = "", dashboard_url: Optional[str] = None,
                            idempotency_key: str) -> str:
    """Durably queue a vendor-relayed invitation without a provider call inline."""
    from engraphis import email_outbox
    from engraphis.inspector.auth import _EMAIL_RE

    subject = "Accept your Engraphis team invitation"
    if not invite_url:
        base = (dashboard_url or os.environ.get("ENGRAPHIS_DASHBOARD_URL", "")
                or DEFAULT_TEAM_DASHBOARD_URL).strip().rstrip("/")
        invite_url = base
    reply_to = invited_by if invited_by and _EMAIL_RE.match(invited_by) else None
    return email_outbox.enqueue(
        "invitation", to, subject,
        _invitation_email_text(name, role, invite_url, invited_by=invited_by),
        reply_to=reply_to, idempotency_key=idempotency_key,
    )


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
    from engraphis.commercial import service_mode
    if service_mode() == "vendor":
        # Match billing._dedup_path's vendor default so paid-key recovery, the Polar
        # ledger, and commercial_backup all resolve the same managed directory.
        relay = os.environ.get("ENGRAPHIS_RELAY_DB", "").strip()
        if relay and relay != ":memory:":
            return Path(relay).expanduser().resolve().parent
        state_dir = os.environ.get("ENGRAPHIS_STATE_DIR", "").strip()
        return (Path(state_dir).expanduser().resolve() if state_dir
                else (Path.home() / ".engraphis").resolve())
    return Path.cwd()


UNDELIVERED_LICENSE_KEYS_NAME = "undelivered_license_keys.tsv"


def _persist_fallback_key(email_addr: str, key: str, product_name: str) -> Optional[Path]:
    """Write an undelivered key to a 0600 operator-only file (NEVER the app log).

    Returns the file path, or None if it couldn't be written. The raw key must not
    hit application logs — log aggregation is usually less protected than this file.
    """
    path = _fallback_dir() / UNDELIVERED_LICENSE_KEYS_NAME
    try:
        safe_key = str(key)
        if not safe_key or len(safe_key) > 8192 \
                or any(ord(char) < 33 or ord(char) == 127 for char in safe_key):
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            return None
        # Create with 0600 before writing any key material.
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(str(path), flags, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            info = os.fstat(fh.fileno())
            linked = os.lstat(path)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 \
                    or not stat.S_ISREG(linked.st_mode) \
                    or not os.path.samestat(info, linked):
                return None
            # Both fields originated outside the process. Keep one record per line even
            # if a future caller bypasses the normal email/product validation helpers.
            safe_email = " ".join(str(email_addr).split())[:320]
            safe_product = " ".join(str(product_name).split())[:240]
            fh.write("%d\t%s\t%s\t%s\n" % (
                int(time.time()), safe_email, safe_product, safe_key))
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return path
    except OSError:
        return None


def manual_fulfillment_clear() -> bool:
    """Return whether no operator-only undelivered-key record awaits reconciliation."""
    path = _fallback_dir() / UNDELIVERED_LICENSE_KEYS_NAME
    try:
        if path.is_symlink():
            return False
        if not path.exists():
            return True
        # Operators remove or encrypted-archive the reconciled file. Treat even an empty
        # leftover as unresolved so backup cannot later fail its non-empty private-file
        # invariant while operational readiness incorrectly reports green.
        return False
    except OSError:
        return False


def _existing_license_delivery(idempotency_key: str) -> Optional[str]:
    """Return the key already held by a durable purchase-email operation.

    This closes the retry window after a key/email was persisted but before the Polar
    delivery claims were finalized. The outbox body is the recoverable source of truth;
    a retry reuses that same signed key instead of minting and potentially emailing a
    second entitlement for one order.
    """
    if not idempotency_key:
        return None
    from engraphis import email_outbox
    conn = email_outbox._connect()
    try:
        row = conn.execute(
            "SELECT text_body FROM email_outbox WHERE idempotency_key=?",
            (idempotency_key,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    for line in str(row["text_body"] or "").splitlines():
        candidate = line.strip()
        if candidate.startswith("ENGR1.") and candidate.count(".") == 2:
            return candidate
    raise RuntimeError("durable purchase email is missing its signed key")


def _issue_and_email(email_addr: str, product_name: str, seats: int,
                     days: Optional[int], *, is_trial: bool = False,
                     subscription_id: str = "", order_id: str = "",
                     product_id: str = "", fulfillment_id: str = "") -> str:
    """Mint a signed key and create a recoverable delivery operation.

    Provider failures remain in the durable outbox. If the enqueue itself failed, persist
    the only raw key in the encrypted-backup-covered 0600 operator fallback instead.
    """
    email_idempotency_key = ""
    if order_id:
        # A renewal has a fresh order id. Reusing this stable key makes a retry converge
        # on the original durable outbox message rather than enqueueing a second email.
        email_idempotency_key = "purchase-license:" + order_id
        existing_key = _existing_license_delivery(email_idempotency_key)
        if existing_key:
            return existing_key
    elif fulfillment_id:
        # Seat changes and legacy trial lifecycle events have no order id. Hash the
        # route's server-derived fulfillment identity so a crash after durable outbox
        # enqueue but before webhook finalization reuses the original key/email instead
        # of minting a second entitlement on retry.
        digest = hashlib.sha256(fulfillment_id.encode("utf-8")).hexdigest()
        email_idempotency_key = "license-fulfillment:" + digest
        existing_key = _existing_license_delivery(email_idempotency_key)
        if existing_key:
            return existing_key

    # Resolve every mapping before minting. In vendor mode a missing product id must not
    # mint an un-emailable key, release the webhook claim, and mint another on each retry.
    label = _plan_label(product_name, product_id)
    key = issue_key(
        email_addr, product_name=product_name, seats=seats, days=days,
        trial=is_trial, subscription_id=subscription_id, order_id=order_id,
        product_id=product_id)
    try:
        send_license_email(
            email_addr, key, product_name=label, is_trial=is_trial,
            idempotency_key=email_idempotency_key)
    except Exception as exc:  # noqa: BLE001 — a delivery failure must not lose a key
        fp = hashlib.sha256(key.encode("ascii")).hexdigest()[:12]
        # A provider outage happens *after* enqueue; the durable outbox already contains
        # the exact key and will retry it, so creating a second plaintext copy would only
        # expand secret exposure. The 0600 fallback is reserved for the rarer case where
        # the durable enqueue itself failed and no recoverable delivery operation exists.
        try:
            queued_key = _existing_license_delivery(email_idempotency_key)
        except Exception:  # noqa: BLE001 - the outbox may be the failing dependency
            queued_key = None
        if queued_key and hmac.compare_digest(queued_key, key):
            logger.warning(
                "email delivery deferred (%s) — key %s remains in the durable outbox",
                type(exc).__name__, fp)
            return key
        saved = _persist_fallback_key(email_addr, key, product_name)
        if saved:
            logger.warning(
                "email delivery failed (%s) — key %s for customer_ref=%s saved to the private "
                "fallback file (deliver manually)",
                type(exc).__name__, fp, _log_ref(email_addr))
        else:
            logger.error(
                "email delivery failed (%s) AND could not persist key %s for "
                "customer_ref=%s — Polar delivery remains retryable; use "
                "`python -m scripts.license_admin issue` only if recovery stays down",
                type(exc).__name__, fp, _log_ref(email_addr))
            # No provider delivery, durable outbox row, or operator recovery file exists.
            # Never acknowledge the purchase in this state: let Polar redeliver so a
            # later healthy attempt can mint and persist a recoverable entitlement.
            raise RuntimeError("license delivery could not be persisted") from exc
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
    if not isinstance(product, dict):
        product = {}
    product_name = _extract_product_name(payload)
    from engraphis.commercial import extract_product_id
    product_id = extract_product_id(payload)
    seats = _extract_seats(payload)
    days = _key_days(product_name, product.get("metadata") or {}, product_id)
    return _issue_and_email(
        email_addr, product_name, seats, days,
        subscription_id=_extract_subscription_id(payload),
        order_id=_extract_order_id(payload), product_id=product_id,
        fulfillment_id=str(payload.get("_engraphis_fulfillment_id") or ""))


def handle_subscription_updated(payload: dict) -> Optional[str]:
    """Re-issue a key when a Team buyer changes seat count mid-cycle (adds or
    removes seats via the Customer Portal or API).

    Polar's ``subscription.updated`` is a catch-all also fired for cancel /
    uncancel / trialing / past-due / revoked transitions, so both this function
    and its route caller require ``status == active`` after a real seat-count
    change. Trialing replacements must remain trial-bounded and are therefore
    ignored until payment rather than minted as normal paid-period keys.
    """
    if str(payload.get("status", "")).strip().lower() != "active":
        return None
    email_addr = _extract_email(payload)
    if not email_addr:
        logger.warning("subscription.updated missing customer email — cannot re-issue key")
        return None
    product = payload.get("product") or {}
    if not isinstance(product, dict):
        product = {}
    product_name = _extract_product_name(payload)
    from engraphis.commercial import extract_product_id
    product_id = extract_product_id(payload)
    seats = _extract_seats(payload)
    # Bound the replacement key to the subscription's current paid period, NOT a fresh
    # full window from now — a mid-cycle seat change must not extend entitlement past the
    # period the customer has actually paid through.
    days = _subscription_key_days(
        payload, product_name, product.get("metadata") or {}, product_id)
    logger.info(
        "seat count changed for customer_ref=%s product_ref=%s -> %d seats, re-issuing key",
        _log_ref(email_addr), _log_ref(product_name), seats)
    return _issue_and_email(
        email_addr, product_name, seats, days,
        subscription_id=_extract_subscription_id(payload, object_is_subscription=True),
        product_id=product_id,
        fulfillment_id=str(payload.get("_engraphis_fulfillment_id") or ""))


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
    from engraphis.commercial import extract_product_id
    product_id = extract_product_id(payload)
    seats = _extract_seats(payload)
    days = _trial_days(payload.get("current_period_end"))
    logger.info(
        "trial started for customer_ref=%s product_ref=%s — issuing %d-day key",
        _log_ref(email_addr), _log_ref(product_name), days)
    return _issue_and_email(
        email_addr, product_name, seats, days, is_trial=True,
        subscription_id=_extract_subscription_id(payload, object_is_subscription=True),
        product_id=product_id,
        fulfillment_id=str(payload.get("_engraphis_fulfillment_id") or ""))
