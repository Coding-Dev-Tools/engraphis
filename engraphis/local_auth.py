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
import os
import secrets
import time
from typing import Optional, Tuple
from urllib.parse import urlsplit


BROWSER_SESSION_COOKIE = "engraphis_local_session"
BROWSER_SESSION_SECONDS = 12 * 60 * 60
_BROWSER_SESSION_VERSION = "v1"

# One-time browser sign-in links. The code is a signed, short-lived secret that a
# person clicks; nobody ever types the deployment token into a page.
BROWSER_HANDOFF_PREFIX = "egh1"
BROWSER_HANDOFF_SECONDS = 15 * 60

_BROWSER_MARKER_HEADER = "x-engraphis-browser-session"
_FORWARDING_HEADERS = frozenset({"forwarded", "x-real-ip", "via"})
_WILDCARD_HOSTS = frozenset({"0.0.0.0", "::", "[::]", "*"})


def _trusted_local_peer(peer: str) -> bool:
    """Whether ``ENGRAPHIS_LOCAL_TRUSTED_PEERS`` opts this socket peer in as local."""
    allowed = os.environ.get("ENGRAPHIS_LOCAL_TRUSTED_PEERS", "").strip()
    if not allowed:
        return False
    from engraphis.netutil import trusted_proxy

    return trusted_proxy(peer, allowed)


def _trusted_forwarding_proxy(scope) -> bool:
    """Whether this socket peer is an operator-declared forwarding proxy."""
    allowed = os.environ.get("ENGRAPHIS_FORWARDED_ALLOW_IPS", "").strip()
    if not allowed:
        return False
    client = scope.get("client")
    peer = ((client[0] if client else "") or "")[:64]
    if not peer:
        return False
    from engraphis.netutil import trusted_proxy

    return trusted_proxy(peer, allowed)


def _configured_browser_hosts() -> set:
    """Hostnames the operator published for this deployment."""
    names = set()
    dashboard_url = os.environ.get("ENGRAPHIS_DASHBOARD_URL", "").strip()
    if dashboard_url:
        try:
            hostname = urlsplit(dashboard_url).hostname
        except ValueError:
            hostname = None
        if hostname:
            names.add(hostname.lower().rstrip("."))
    bind = os.environ.get("ENGRAPHIS_HOST", "").strip().lower().strip("[]")
    if bind and bind not in _WILDCARD_HOSTS:
        names.add(bind.rstrip("."))
    return names


def published_browser_host(hostname: str) -> bool:
    """Whether *hostname* is an address this deployment can serve the dashboard from.

    Any literal address qualifies: a DNS-rebinding attacker needs a public name, and a
    page cannot claim an address origin without controlling it. Automatic access is
    limited to local names and the operator's published URL, so a rebound public
    hostname (``evil.example`` pointing at this server) is rejected unless configured.
    """
    name = (hostname or "").strip().lower().rstrip(".")
    if not name:
        return False
    if name == "localhost" or name.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        address = None
    if address is not None:
        return not address.is_unspecified and not address.is_multicast
    # mDNS/RFC 6762 and RFC 8375 names never resolve through public DNS, and a
    # single-label name is not a public FQDN.
    if name.endswith(".local") or name.endswith(".home.arpa") or "." not in name:
        return True
    return name in _configured_browser_hosts()


def _browser_request_origin(
    request, *, allow_forwarded: bool, require_origin: bool,
) -> Optional[tuple]:
    """Return ``(scheme, hostname, port)`` for a same-origin browser request."""

    scope = request.scope
    headers = {}
    forwarded = False
    for name, value in scope.get("headers", ()):
        name = name.decode("latin1").lower()
        if name.startswith("x-forwarded-") or name in _FORWARDING_HEADERS:
            forwarded = True
            if not allow_forwarded:
                return None
        headers.setdefault(name, []).append(value.decode("latin1"))
    if forwarded and not _trusted_forwarding_proxy(scope):
        return None
    if headers.get(_BROWSER_MARKER_HEADER) != ["1"]:
        return None
    if "sec-fetch-site" in headers and headers["sec-fetch-site"] != ["same-origin"]:
        return None

    hosts = headers.get("host", [])
    origins = headers.get("origin", [])
    if forwarded:
        forwarded_hosts = headers.get("x-forwarded-host", [])
        if len(forwarded_hosts) > 1:
            return None
        if forwarded_hosts:
            hosts = forwarded_hosts
    if len(hosts) != 1 or len(origins) > 1:
        return None
    host = hosts[0]
    scheme = scope.get("scheme", "")
    if scheme not in {"http", "https"}:
        return None
    if forwarded:
        # TLS normally terminates at the operator's proxy, so the edge-decided scheme is
        # the one the browser used. Only a trusted proxy may supply it.
        protos = headers.get("x-forwarded-proto", [])
        if len(protos) > 1:
            return None
        if protos:
            scheme = protos[0].strip().lower()
            if scheme not in {"http", "https"}:
                return None
    if any(char.isspace() or ord(char) < 32 or char in "\\/@?#," for char in host):
        return None
    try:
        target = urlsplit(scheme + "://" + host)
        hostname = target.hostname or ""
        if not hostname:
            return None
        target_port = target.port if target.port is not None else (
            443 if scheme == "https" else 80)
        if target_port < 1:
            return None
        if not origins:
            if require_origin:
                return None
            return scheme, hostname, target_port
        origin = origins[0]
        if any(char.isspace() or ord(char) < 32 or char == "\\" for char in origin):
            return None
        parsed = urlsplit(origin)
        origin_port = parsed.port if parsed.port is not None else (
            443 if parsed.scheme == "https" else 80)
        if not (
            parsed.scheme == scheme and parsed.hostname == target.hostname
            and origin_port == target_port
            and parsed.username is None and parsed.password is None
            and not parsed.path and not parsed.query and not parsed.fragment
        ):
            return None
    except ValueError:
        return None
    return scheme, hostname, target_port


def browser_bootstrap_request(request, *, require_origin: bool = True) -> bool:
    """Whether a browser may open this deployment's dashboard automatically.

    The dashboard is the human surface of a single-user node, so the deployment's
    network reachability is its boundary: a same-origin request that reaches this
    origin opens it. Machine clients still need ``Authorization: Bearer`` for ``/api/*``
    and ``/mcp``. Cross-site pages cannot forge this: the request must carry the
    dashboard's non-simple header, an Origin that exactly matches a published Host, and
    (behind a proxy) arrive from an operator-declared trusted proxy. Operators who
    expose the dashboard to an untrusted network set
    ``ENGRAPHIS_BROWSER_AUTO_SESSION=0`` and use one-time links instead.
    """
    info = _browser_request_origin(
        request, allow_forwarded=True, require_origin=require_origin,
    )
    return bool(info) and published_browser_host(info[1])


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

    "Configured" means exactly what the request middleware means by it: any
    non-empty value.  Stripping whitespace here would let a present-but-blank
    deployment skip the browser gate while the API still demanded a credential.
    """
    return not str(api_token or "")


def local_browser_request(
    request, *, require_origin: bool = True, allow_trusted_peers: bool = False,
) -> bool:
    """Authenticate a same-origin browser bootstrap at the local-machine boundary.

    Literal loopback, a loopback Host, an exact browser Origin, and the dashboard's
    non-simple header are required, and forwarding proxies never qualify.
    ``allow_trusted_peers`` additionally accepts container-NAT peers listed in
    ``ENGRAPHIS_LOCAL_TRUSTED_PEERS`` (the shipped Compose setup) while still requiring
    a loopback Host, so a browser on the Docker host that opens
    ``http://127.0.0.1:<port>`` needs no credential and another machine cannot use it.

    This is a *necessary* condition, never a sufficient one: every signal it reads
    is a request header a non-browser client can write, so it cannot by itself
    decide that a caller is the local browser. Callers must also apply
    :func:`automatic_browser_session_allowed` before treating a caller as trusted.
    """
    scope = request.scope
    client = scope.get("client")
    try:
        peer = ipaddress.ip_address(client[0] if client else "")
    except ValueError:
        return False
    normalized_peer = getattr(peer, "ipv4_mapped", None) or peer
    if normalized_peer.is_unspecified:
        return False
    if not normalized_peer.is_loopback and not (
        allow_trusted_peers and _trusted_local_peer(str(normalized_peer))
    ):
        return False

    info = _browser_request_origin(
        request, allow_forwarded=False, require_origin=require_origin,
    )
    if info is None:
        return False
    hostname = info[1].lower()
    if hostname == "localhost":
        return True
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return (getattr(address, "ipv4_mapped", None) or address).is_loopback


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


def _handoff_signature(expected: str, epoch: bytes, expires_at: int, nonce: str) -> str:
    message = ("%s:%d:%s" % (BROWSER_HANDOFF_PREFIX, expires_at, nonce)).encode("ascii")
    digest = hmac.new(
        expected.encode("utf-8") + b"\x00" + bytes(epoch), message, hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def browser_handoff(
    expected: Optional[str], epoch: bytes, *, now: Optional[float] = None,
    ttl: int = BROWSER_HANDOFF_SECONDS,
) -> Tuple[str, int]:
    """Mint a short-lived, single-use sign-in code for one browser.

    The code travels only in a URL fragment (never in a query string, log, or Referer)
    and buys one browser session. It is signed with the deployment token *and* a
    per-process epoch, so restarting the server invalidates every outstanding link.
    """

    configured = str(expected or "")
    if not configured:
        raise ValueError("a configured API token is required")
    if not isinstance(epoch, (bytes, bytearray)) or not epoch:
        raise ValueError("a per-process handoff epoch is required")
    current = int(time.time() if now is None else now)
    expires_at = current + max(60, min(int(ttl), 60 * 60))
    nonce = secrets.token_urlsafe(12)
    return "%s.%d.%s.%s" % (
        BROWSER_HANDOFF_PREFIX,
        expires_at,
        nonce,
        _handoff_signature(configured, epoch, expires_at, nonce),
    ), expires_at


def browser_handoff_ok(
    value: Optional[str], expected: Optional[str], epoch: bytes, *,
    now: Optional[float] = None,
) -> Optional[Tuple[str, int]]:
    """Return ``(nonce, expires_at)`` for an authentic, unexpired sign-in code."""

    configured = str(expected or "")
    if not configured or not isinstance(epoch, (bytes, bytearray)) or not epoch:
        return None
    parts = str(value or "").split(".")
    if len(parts) != 4 or parts[0] != BROWSER_HANDOFF_PREFIX:
        return None
    nonce = parts[2]
    if not nonce or len(nonce) > 128 or any(ord(char) < 33 or ord(char) > 126 for char in nonce):
        return None
    try:
        expires_at = int(parts[1])
    except ValueError:
        return None
    if expires_at <= int(time.time() if now is None else now):
        return None
    if not _constant_time_equals(
        parts[3], _handoff_signature(configured, epoch, expires_at, nonce),
    ):
        return None
    return nonce, expires_at
