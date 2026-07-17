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


def _map_polar_product_to_plan(product_name: str) -> str:
    """Map Polar product name to an Engraphis plan tier.

    An operator-configured exact-name override (``ENGRAPHIS_POLAR_PRODUCT_MAP``) wins so
    tiering can be pinned to real business data rather than inferred. Otherwise match on
    substrings so "Engraphis Pro Monthly" and "Engraphis Pro Annual" both resolve to
    ``pro``. A paid order with an *unrecognized* product name still resolves to ``pro``
    (never free) and logs loudly: a customer who paid must never be silently stiffed with
    a useless free-tier key. Correct Pro-vs-Team routing depends on the Polar product name
    containing "pro"/"team" (or an explicit override) — keep them named so.
    """
    name = (product_name or "").lower()
    override = _product_plan_overrides().get(name.strip())
    if override:
        return override
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


# Grace added over a subscription's current_period_end when re-issuing a key mid-cycle,
# so a slightly-late renewal webhook never briefly locks out a paying customer. Kept
# small (matches the 5-day grace baked into the 35-day monthly _key_days window) — the
# whole point of bounding to the period end is that a mid-cycle change must NOT hand out
# a fresh full 35/395-day window that outlives the paid period.
_KEY_PERIOD_GRACE_DAYS = 5


def _subscription_key_days(payload: dict, product_name: str, metadata: dict,
                           *, now: Optional[float] = None) -> int:
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
    return _key_days(product_name, metadata)


def _plan_label(product_name: str) -> str:
    """Clean tier label for customer-facing email copy ("Pro"/"Team")."""
    return _map_polar_product_to_plan(product_name).title()


def _extract_email(data: dict) -> Optional[str]:
    """Pull the buyer email from an order OR subscription payload, defensively."""
    cust = data.get("customer") or {}
    user = data.get("user") or {}
    return (cust.get("email") or data.get("customer_email")
            or data.get("email") or user.get("email"))

def _extract_subscription_id(data: dict, *, object_is_subscription: bool = False) -> str:
    """Normalized Polar subscription id carried into the signed license payload."""
    raw = data.get("subscription_id")
    if not raw:
        subscription = data.get("subscription")
        raw = subscription.get("id") if isinstance(subscription, dict) else subscription
    if not raw and object_is_subscription:
        raw = data.get("id")
    return str(raw or "").strip()[:128]


def _extract_product_name(data: dict) -> str:
    product = data.get("product") or {}
    return product.get("name") or data.get("product_name") or "Pro"


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
    product = data.get("product") or {}
    meta = product.get("metadata") or data.get("metadata") or {}
    subscription = data.get("subscription") or {}
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
              days: Optional[int] = None, metadata: Optional[dict] = None,
              *, trial: bool = False, record: bool = True,
              subscription_id: str = "") -> str:
    """Generate a signed ``ENGR1.xxx.yyy`` key for *email_addr*.

    Uses the pinned vendor signing key (``.secrets/vendor_signing.key`` or
    ``ENGRAPHIS_SIGNING_KEY`` env). ``product_name`` maps to a plan tier; ``days``
    (or product/metadata inference via :func:`_key_days`) sets validity. ``record=False``
    is for callers already holding the relay DB write lock; they should call
    ``license_registry.record_issued`` after committing.
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

    subscription_id = str(subscription_id or "").strip()[:128]
    now = time.time()
    payload = {
        "v": 1,
        "plan": plan,
        "email": email_addr,
        "seats": max(1, int(seats)),
        "issued": int(now),
        "expires": int(now + days * 86400),
        **({"subscription_id": subscription_id} if subscription_id else {}),
    }
    if trial:
        payload["trial"] = 1               # signed trial marker -> License.is_trial (UI)
    # Server-side enforcement (online-only): every minted key carries a signed
    # ``enforce: "cloud"`` claim plus the license-server URL, so the client requires a live
    # lease (register/renew against that URL) and the key is useless offline or after
    # revocation. The URL is ENGRAPHIS_KEY_CLOUD_URL if set, else the built-in vendor relay
    # (settings.relay_url) — so keys are cloud-enforced by DEFAULT now, not opt-in. The
    # claim rides inside the Ed25519-signed payload, so customers cannot strip it.
    from engraphis.config import canonicalize_relay_url, settings
    enforce_url = canonicalize_relay_url(
        os.environ.get("ENGRAPHIS_KEY_CLOUD_URL", "") or settings.relay_url)
    if enforce_url:
        payload["enforce"] = "cloud"
        payload["cloud_url"] = enforce_url
    key = compose_key(payload, secret)
    logger.info("issued %s key for %s (expires in %d days)", plan, email_addr, days)
    if record:
        try:  # Registry writes remain best-effort; never revoke before recording.
            from engraphis.inspector.license_registry import (
                record_issued, revoke_superseded)
            key_id = record_issued(key)
            if subscription_id:
                revoke_superseded(subscription_id, key_id)
        except Exception as exc:
            logger.warning("could not record/reconcile issued key in registry: %s", exc)
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
                         api_key: str, *, reply_to: Optional[str] = None) -> None:
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
    try:
        resp = httpx.post(_RESEND_API_URL, json=payload, headers=headers, timeout=20.0)
    except httpx.HTTPError as exc:
        raise RuntimeError("Resend API unreachable: %s" % exc) from exc
    if resp.status_code not in (200, 201):
        raise RuntimeError(
            "Resend API error HTTP %s: %s" % (resp.status_code, resp.text[:200]))


def email_configured() -> bool:
    """True if THIS process has its own outbound email delivery set up (Resend or
    SMTP). Callers use this to decide *before* attempting a send whether to use
    local delivery or fall back to something else (e.g. the vendor relay's
    ``/license/v1/team-invite`` for a self-hosted dashboard with no mail account
    of its own) — cheaper and clearer than attempting-and-catching."""
    if _resend_api_key():
        return True
    return bool(os.environ.get("ENGRAPHIS_SMTP_HOST", "").strip()
                and os.environ.get("ENGRAPHIS_SMTP_USER", "").strip()
                and os.environ.get("ENGRAPHIS_SMTP_PASSWORD", "").strip())


def _send_text_email(to: str, subject: str, text_body: str, *,
                     reply_to: Optional[str] = None) -> None:
    """Deliver a plain-text email to *to*. Shared transport for every outbound
    email this module sends (license keys, team invites, ...).

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
    from_addr = os.environ.get("ENGRAPHIS_SMTP_FROM", "keys@engraphis.com").strip()

    api_key = _resend_api_key()
    if api_key:
        logger.info("sending email to %s via Resend API", to)
        _send_via_resend_api(to, subject, text_body, from_addr, api_key, reply_to=reply_to)
        logger.info("email delivered to %s (Resend API)", to)
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
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(text_body)
    logger.info("sending email to %s via %s:%d", to, smtp_host, smtp_port)
    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(smtp_user, smtp_pass)
        smtp.send_message(msg)
    logger.info("email delivered to %s (SMTP)", to)


def send_license_email(to: str, key: str, product_name: str = "Pro",
                       is_trial: bool = False) -> None:
    """Deliver a license key to *to*.

    Raises RuntimeError if nothing is configured, and raises on delivery
    failure — see :func:`_send_text_email`. ``product_name`` should be a clean
    tier label ("Pro"/"Team"); ``is_trial`` selects trial-vs-purchase copy.
    """
    subject = ("Your Engraphis %s Trial License Key" if is_trial
               else "Your Engraphis %s License Key") % product_name
    text_body = _license_email_text(key, product_name, is_trial=is_trial)
    _send_text_email(to, subject, text_body)


def _team_invite_email_text(name: str, role: str, dashboard_url: str,
                            invited_by: str = "", key: str = "",
                            to: str = "") -> str:
    greeting = "Hi %s," % name if name else "Hi,"
    who = "%s has added you" % invited_by if invited_by else "You've been added"
    where = ("    %s\n\n" % dashboard_url if dashboard_url else
             "    Ask your admin for your team dashboard's web address.\n\n")
    reply_note = ("\nQuestions? Just reply to this email — it goes straight to %s.\n"
                  % invited_by if invited_by else "")
    login_email = to or "the address this email was sent to"
    # When this instance is genuinely Team-licensed, the invite carries the *shared*
    # team license key so the member can turn on Pro features (and cloud sync) on
    # their OWN machine, taking one server-enforced seat. Built with %-formatting so
    # the key's own characters are never re-evaluated by the outer .format() below.
    #
    # The email is deliberately structured as two clearly-separated options:
    #   1. JOIN the team dashboard (the hosted/Railway instance) — sign in with email
    #      + password. NO license key is involved here, and pasting the key on the
    #      hosted instance is NOT how membership works (it just re-activates a license
    #      that is already active there). This was a real support confusion: members
    #      followed the concrete "paste the key" steps on the hosted dashboard and
    #      thought they'd joined, when joining = logging in with credentials.
    #   2. OPTIONALLY run Engraphis locally and access the team's shared memories from
    #      your own machine — THAT is what the shared key is for. It unlocks Pro
    #      features AND cloud sync; turning on Cloud Sync then pulls the team's
    #      converged memory store down to your local SQLite file. The key goes into
    #      your LOCAL dashboard (http://127.0.0.1:8700), never the team dashboard.
    activate = ("""
──────────────────────────────────────────────────────────────────────────────
OPTION 2 (optional): run Engraphis on your OWN computer and access the team's
memories locally
──────────────────────────────────────────────────────────────────────────────
Your team plan also unlocks Pro features (analytics, export, automation, and
cloud sync) on your own machine. The shared team license key below is for THIS —
activating Pro on a LOCAL copy of Engraphis you run yourself — NOT for the team
dashboard above (that instance is already licensed; please don't paste this key
there).

Shared team license key:

    %s

To activate on your own machine:
    1. Install and run Engraphis locally (the dashboard opens at
       http://127.0.0.1:8700)
    2. Go to Settings -> License
    3. Paste the key above and click Activate

(Or set the ENGRAPHIS_LICENSE_KEY environment variable, or save the key to
~/.engraphis/license.key.) The key verifies against our license server on first
use and takes one of your team's seats — please use it only on your own devices.

To then access the team's shared memories on your machine: after activating, go
to Settings -> Cloud Sync and turn sync on for the same workspace your team
uses. Your local store converges with the team's (new memories and edits flow
both ways); you keep a full local copy that works offline.
""" % key if key else "")
    return """{greeting}

{who} to an Engraphis team dashboard as a {role}.

You have two ways to use Engraphis as part of this team. You only need the first
one to be a member; the second is optional.

──────────────────────────────────────────────────────────────────────────────
OPTION 1 (required to join the team): sign in to the team dashboard
──────────────────────────────────────────────────────────────────────────────
The team's shared memories live on a hosted dashboard. Open it and sign in:

{where}Log in with this email address: {login_email}
Your admin sets your password directly and will share it with you separately —
this email does not contain it.

No license key is needed here, and you should NOT paste a license key into this
dashboard — joining the team means signing in with your email and password,
nothing else.
{activate}{reply_note}
— The Engraphis team

Learn more: {site_url}
Source & self-hosting: {repo_url}
""".format(greeting=greeting, who=who, role=role, where=where,
               login_email=login_email, activate=activate, reply_note=reply_note,
               site_url=SITE_URL, repo_url=REPO_URL)



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
    _send_text_email(to, subject, text_body)


def _trial_verify_email_text(verify_url: str, plan: str, minutes: int) -> str:
    label = plan.title()
    return f"""Someone requested a free {label} trial for Engraphis using this email address.

Confirm it's you and get your trial key here — this link works once and expires in
{minutes} minutes:

    {verify_url}

Opening it mints your trial key and shows it on a confirmation page, with
instructions to activate it in your dashboard (Settings -> License -> paste key ->
Activate).

If you didn't request this, you can safely ignore this email: no trial has been
issued, and none will be unless this link is opened.

— The Engraphis team
"""


def send_trial_verification_email(to: str, verify_url: str, plan: str = "team", *,
                                   minutes: int = 30) -> None:
    """Deliver a one-time magic link that mints a self-serve trial key on click.

    Part of the 2026-07-14 trial-abuse hardening: ``inspector.license_cloud``'s
    ``POST /license/v1/start-trial`` no longer issues a key synchronously from a bare
    machine_id (trivially reset by deleting one local file — see that module's
    comment); it emails this link instead, and the matching ``GET .../start-trial/
    verify`` mints the key only once it's opened. Raises on delivery failure (see
    :func:`_send_text_email`) — unlike the password-reset send above, the caller DOES
    let a failure change the HTTP response (502): trial start is opt-in self-serve
    (no account to enumerate), and silently swallowing the failure would strand the
    requester with a pending token they can never redeem.
    """
    subject = "Confirm your Engraphis %s trial" % plan.title()
    text_body = _trial_verify_email_text(verify_url, plan, minutes)
    _send_text_email(to, subject, text_body)


# The canonical hosted team dashboard (the Railway deployment members sign in
# to). Used as the default ``dashboard_url`` in team-invite emails when neither
# the caller nor ``ENGRAPHIS_DASHBOARD_URL`` supplies one, so an invite always
# carries a clickable "sign in" link instead of "ask your admin". A self-hoster
# running their own dashboard overrides this by setting ``ENGRAPHIS_DASHBOARD_URL``
# (or by passing ``dashboard_url`` explicitly), which both take precedence.
DEFAULT_TEAM_DASHBOARD_URL = "https://team.engraphis.com/"


def send_team_invite_email(to: str, name: str, role: str, *, invited_by: str = "",
                           key: str = "", dashboard_url: Optional[str] = None) -> None:
    """Notify a newly added dashboard team member (``/api/auth/users``) that
    their account exists, and — when ``key`` is given — hand them the shared
    Team license key so they can turn on Pro features (and cloud sync) on their
    own machine, taking one of the team's server-enforced seats.

    Still deliberately carries NO password: the admin sets the initial dashboard
    password directly in the "Add member" form, so it is already known only to the
    admin. ``key`` is different in kind — it is the account's *shared* Team key
    (the same one every member of this team uses; see ``docs/SYNC.md``), not a
    per-user secret, and seat count is enforced server-side — so including it is
    what actually makes a member a licensed member rather than just a dashboard
    login. Callers pass it only for a genuinely Team-licensed instance (see
    ``routes.v2_team._send_invite``). ``invited_by`` (the admin's own email) is
    named in the body and set as Reply-To — important when this is sent through
    the shared vendor relay (see ``inspector.license_cloud.team_invite``), where
    the visible From address is the vendor's, not the actual team's. ``dashboard_url``
    overrides ``ENGRAPHIS_DASHBOARD_URL`` when not None (so a relay send can carry
    the admin's own dashboard URL instead of the relay host's). Raises on delivery
    failure (see :func:`_send_text_email`); the caller treats this as best-effort
    and must not let it block account creation.
    """
    from engraphis.inspector.auth import _EMAIL_RE
    subject = "You've been added to an Engraphis team"
    if not (dashboard_url or "").strip():
        dashboard_url = os.environ.get("ENGRAPHIS_DASHBOARD_URL", "").strip()
    if not dashboard_url:
        dashboard_url = DEFAULT_TEAM_DASHBOARD_URL
    text_body = _team_invite_email_text(name, role, dashboard_url,
                                        invited_by=invited_by, key=key, to=to)
    reply_to = invited_by if invited_by and _EMAIL_RE.match(invited_by) else None
    _send_text_email(to, subject, text_body, reply_to=reply_to)


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
                     days: Optional[int], *, is_trial: bool = False,
                     subscription_id: str = "") -> str:
    """Mint a signed key and email it. On ANY delivery failure, persist the key to
    the 0600 fallback file (never the log) and still return it, so a paid or trial
    key is never lost and the webhook can 202 without a Polar retry-storm."""
    key = issue_key(
        email_addr, product_name=product_name, seats=seats, days=days,
        trial=is_trial, subscription_id=subscription_id)
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
    return _issue_and_email(
        email_addr, product_name, seats, days,
        subscription_id=_extract_subscription_id(payload))


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
    product_name = _extract_product_name(payload)
    seats = _extract_seats(payload)
    # Bound the replacement key to the subscription's current paid period, NOT a fresh
    # full window from now — a mid-cycle seat change must not extend entitlement past the
    # period the customer has actually paid through.
    days = _subscription_key_days(payload, product_name, product.get("metadata") or {})
    logger.info("seat count changed for %s (%s) -> %d seats, re-issuing key",
                email_addr, product_name, seats)
    return _issue_and_email(
        email_addr, product_name, seats, days,
        subscription_id=_extract_subscription_id(payload, object_is_subscription=True))


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
    return _issue_and_email(
        email_addr, product_name, seats, days, is_trial=True,
        subscription_id=_extract_subscription_id(payload, object_is_subscription=True))
