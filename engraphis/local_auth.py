"""Minimal authentication for the single-user local runtime.

Hosted identities, organizations, roles, invitations, seats, sessions, and recovery
belong to Engraphis Cloud.  The open package supports only one optional deployment
secret for machine-to-machine access: ``ENGRAPHIS_API_TOKEN``.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import time
from typing import Optional
from urllib.parse import urlsplit


BROWSER_SESSION_COOKIE = "engraphis_local_session"
BROWSER_SESSION_SECONDS = 12 * 60 * 60
_BROWSER_SESSION_VERSION = "v1"


def local_browser_request(request, *, require_origin: bool = True) -> bool:
    """Authenticate a same-origin browser bootstrap at the local-machine boundary.

    Unlike the open-mode network helper, this never trusts configured LAN peers or
    forwarding proxies. Literal loopback, a loopback Host, an exact browser Origin,
    and the dashboard's non-simple header are all required to mint a session.
    """
    scope = request.scope
    client = scope.get("client")
    try:
        peer = ipaddress.ip_address(client[0] if client else "")
    except ValueError:
        return False
    if not (getattr(peer, "ipv4_mapped", None) or peer).is_loopback:
        return False

    headers = {}
    for name, value in scope.get("headers", ()):
        name = name.decode("latin1").lower()
        if name.startswith("x-forwarded-") or name in {"forwarded", "x-real-ip", "via"}:
            return False
        headers.setdefault(name, []).append(value.decode("latin1"))
    if headers.get("x-engraphis-browser-session") != ["1"]:
        return False
    if "sec-fetch-site" in headers and headers["sec-fetch-site"] != ["same-origin"]:
        return False
    hosts = headers.get("host", [])
    origins = headers.get("origin", [])
    if len(hosts) != 1 or len(origins) > 1:
        return False
    host = hosts[0]
    scheme = scope.get("scheme", "")
    if scheme not in {"http", "https"} or any(
        char.isspace() or ord(char) < 32 or char in "\\/@?#," for char in host
    ):
        return False
    try:
        target = urlsplit(scheme + "://" + host)
        hostname = target.hostname or ""
        if hostname.lower() != "localhost":
            address = ipaddress.ip_address(hostname)
            if not (getattr(address, "ipv4_mapped", None) or address).is_loopback:
                return False
        target_port = target.port if target.port is not None else (443 if scheme == "https" else 80)
        if target_port < 1:
            return False
        if not origins:
            return not require_origin
        origin = origins[0]
        if any(char.isspace() or ord(char) < 32 or char == "\\" for char in origin):
            return False
        parsed = urlsplit(origin)
        origin_port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
        return bool(
            parsed.scheme == scheme and parsed.hostname == target.hostname
            and origin_port == target_port
            and parsed.username is None and parsed.password is None
            and not parsed.path and not parsed.query and not parsed.fragment
        )
    except ValueError:
        return False


def bearer_token(authorization: Optional[str]) -> str:
    """Return a stripped bearer credential, or an empty string for another scheme."""
    value = str(authorization or "")
    if value[:7].lower() != "bearer ":
        return ""
    return value[7:].strip()


def bearer_ok(authorization: Optional[str], expected: Optional[str]) -> bool:
    """Constant-time validation for the local runtime's optional API token."""
    configured = str(expected or "")
    supplied = bearer_token(authorization)
    return bool(configured and supplied) and hmac.compare_digest(supplied, configured)


def token_ok(supplied: Optional[str], expected: Optional[str]) -> bool:
    """Constant-time validation for a raw token submitted to the browser exchange."""

    configured = str(expected or "")
    candidate = str(supplied or "")
    return bool(configured and candidate) and hmac.compare_digest(candidate, configured)


def _session_signature(expected: str, issued_at: int) -> str:
    message = ("%s:%d" % (_BROWSER_SESSION_VERSION, issued_at)).encode("ascii")
    digest = hmac.new(expected.encode("utf-8"), message, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def browser_session(expected: str, *, now: Optional[float] = None) -> str:
    """Mint a short-lived signed browser session without persisting the API token."""

    configured = str(expected or "")
    if not configured:
        raise ValueError("a configured API token is required")
    issued_at = int(time.time() if now is None else now)
    return "%s.%d.%s" % (
        _BROWSER_SESSION_VERSION,
        issued_at,
        _session_signature(configured, issued_at),
    )


def browser_session_ok(
    value: Optional[str],
    expected: Optional[str],
    *,
    now: Optional[float] = None,
    max_age: int = BROWSER_SESSION_SECONDS,
) -> bool:
    """Validate an HttpOnly browser session signed by the configured API token."""

    configured = str(expected or "")
    parts = str(value or "").split(".")
    if not configured or len(parts) != 3 or parts[0] != _BROWSER_SESSION_VERSION:
        return False
    try:
        issued_at = int(parts[1])
    except ValueError:
        return False
    current = int(time.time() if now is None else now)
    # Five minutes of positive skew tolerates a corrected host clock without accepting a
    # session minted arbitrarily far in the future.
    if issued_at > current + 300 or current - issued_at > max(0, int(max_age)):
        return False
    return hmac.compare_digest(parts[2], _session_signature(configured, issued_at))
