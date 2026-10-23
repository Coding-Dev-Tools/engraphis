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


def automatic_browser_session_allowed(api_token: Optional[str]) -> bool:
    """Whether this instance may open a browser session without an operator credential.

    A deployment that configured ``ENGRAPHIS_API_TOKEN`` opted into token-gated
    access, so every browser session it hands out is minted from that token through
    ``POST /api/auth/session``.  Only a zero-token (open) instance may open a
    session on its own.

    The alternative - trusting the browser's own signals - is not a boundary.
    ``Origin``, ``Sec-Fetch-Site`` and the dashboard's marker header are ordinary
    request headers that any client can write, and a raw TCP tunnel (``ssh -L``,
    ``netsh portproxy``) makes a remote caller look exactly like a loopback peer
    while the tunneler chooses ``Host`` and ``Origin`` itself.  Nothing the server
    can read separates a real local browser from a tunneled requester once the app
    listens on a loopback port, so the token policy is the only reliable gate.
    This is the owner's release-review decision for the local dashboard session
    (PR #270): the automatic session exists only where the operator did not opt
    into token-gated access.
    """
    return not str(api_token or "").strip()


def local_browser_request(request, *, require_origin: bool = True) -> bool:
    """Authenticate a same-origin browser bootstrap at the local-machine boundary.

    Unlike the open-mode network helper, this never trusts configured LAN peers or
    forwarding proxies. Literal loopback, a loopback Host, an exact browser Origin,
    and the dashboard's non-simple header are all required to mint a session.

    This is a *necessary* condition, never a sufficient one: every signal it reads
    is a request header a non-browser client can write, so it cannot by itself
    decide that a caller is the local browser.  Callers must also apply
    :func:`automatic_browser_session_allowed` before treating a caller as trusted.
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


def _constant_time_equals(supplied: Optional[str], expected: Optional[str]) -> bool:
    """Compare two credentials in constant time, whatever their encoding.

    ``hmac.compare_digest`` refuses non-ASCII ``str`` operands and raises
    ``TypeError``, so a client-supplied credential with a non-ASCII byte turned a
    rejected secret into an unhandled 500.  Both sides are compared as UTF-8
    bytes instead: the comparison stays constant time and every input shape is
    simply a mismatch.
    """
    left = str(supplied or "").encode("utf-8")
    right = str(expected or "").encode("utf-8")
    return hmac.compare_digest(left, right)


def bearer_ok(authorization: Optional[str], expected: Optional[str]) -> bool:
    """Constant-time validation for the local runtime's optional API token."""
    configured = str(expected or "")
    supplied = bearer_token(authorization)
    return bool(configured and supplied) and _constant_time_equals(supplied, configured)


def token_ok(supplied: Optional[str], expected: Optional[str]) -> bool:
    """Constant-time validation for a raw token submitted to the browser exchange."""

    configured = str(expected or "")
    candidate = str(supplied or "")
    return bool(configured and candidate) and _constant_time_equals(candidate, configured)


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
    return _constant_time_equals(parts[2], _session_signature(configured, issued_at))
