"""Host/URL helpers shared by every entrypoint.

Stdlib-only and — deliberately — importable WITHOUT touching :mod:`engraphis.config`:
``scripts/start_dashboard.py`` must set env vars *before* the config module's
module-level ``settings = Settings()`` snapshot happens, so it can only import helpers
that don't drag config in.

Why this exists: three entrypoints independently built ``http://{host}:{port}`` by
interpolation, which is malformed for an IPv6 literal (``::`` needs brackets:
``http://[::]:8700``) — the exact bind used to fix the 2026-07-16 Railway healthcheck
outage. A bind-all host is also not *connectable*, so URLs shown to humans (or used as
redirect targets) additionally need the wildcard mapped to loopback.
"""
from __future__ import annotations

#: Bind-all hosts. Fine to LISTEN on; meaningless to CONNECT to from a URL.
_IPV4_WILDCARD_HOSTS = ("", "0.0.0.0")  # noqa: S104 — classified, not bound
_IPV6_WILDCARD_HOSTS = ("::", "[::]")


def bracket_host(host: str) -> str:
    """Bracket a bare IPv6 literal for use inside a URL authority; anything else is
    returned unchanged (idempotent for an already-bracketed host)."""
    host = host or ""
    if ":" in host and not host.startswith("["):
        return "[%s]" % host
    return host


def connect_host(bind_host: str) -> str:
    """Map a bind-all host to loopback so the result is actually connectable; a real
    hostname/address passes through unchanged."""
    normalized = (bind_host or "").strip()
    if normalized in _IPV6_WILDCARD_HOSTS:
        return "::1"
    if normalized in _IPV4_WILDCARD_HOSTS:
        return "127.0.0.1"
    return bind_host


def display_base_url(host: str, port: int, *, scheme: str = "http") -> str:
    """A well-formed, connectable base URL for a server bound to *host*:*port* —
    wildcard binds become loopback and IPv6 literals are bracketed."""
    return "%s://%s:%d" % (scheme, bracket_host(connect_host(host)), int(port))


def trusted_proxy(peer: str, allowed_raw: str) -> bool:
    """Whether *peer* matches ``*``, an exact address/name, or an IP network.

    Invalid entries are ignored instead of breaking request handling. IPv6 zone ids are
    removed before comparison; exact non-IP names remain supported for ASGI fixtures and
    explicitly named local proxies.
    """
    import ipaddress

    peer = (peer or "unknown")[:64]
    entries = [item.strip() for item in (allowed_raw or "").split(",") if item.strip()]
    if "*" in entries or peer in entries:
        return True
    try:
        address = ipaddress.ip_address(peer.split("%", 1)[0])
    except ValueError:
        return False
    for entry in entries:
        try:
            if address in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            continue
    return False


def client_ip(request) -> str:
    """The caller's address for rate-limiting and audit purposes.

    *request* is duck-typed (anything with ``.client.host`` and ``.headers.get``) so this
    module stays stdlib-only and importable before ``engraphis.config`` exists.

    Returns the direct peer unless that peer is configured as a trusted forwarding proxy
    via ``ENGRAPHIS_FORWARDED_ALLOW_IPS`` — ``*`` (for platforms such as Railway, where
    the proxy peer is not a stable address) or a comma-separated list of exact peers/IP
    networks. When the peer IS trusted, the RIGHTMOST ``X-Forwarded-For`` entry is used.

    A trusted proxy appends the address it observed, so the rightmost entry is the hop
    our proxy actually saw. Everything left of it is client-supplied and freely spoofable.
    Reading the leftmost
    token — which is what uvicorn's ``proxy_headers`` does when populating
    ``request.client`` under ``forwarded_allow_ips='*'`` — lets a caller mint a fresh
    identity per request simply by pre-seeding the header, defeating any per-IP limit or
    lockout built on it and forging the address recorded in the audit log.

    Use this helper instead of ``request.client.host`` for per-IP security decisions.
    """
    import os
    client = getattr(request, "client", None)
    direct = ((getattr(client, "host", "") if client else "") or "unknown")[:64]
    allowed_raw = os.environ.get("ENGRAPHIS_FORWARDED_ALLOW_IPS", "").strip()
    if not trusted_proxy(direct, allowed_raw):
        return direct
    forwarded = request.headers.get("x-forwarded-for", "") or ""
    parts = [part.strip() for part in forwarded.split(",") if part.strip()]
    return parts[-1][:64] if parts else direct


def is_local_request(request) -> bool:
    """Whether a request genuinely arrived from loopback without proxy headers.

    This is a safe bootstrap backstop, not a general authentication mechanism. Any
    forwarding header disqualifies the request because a proxied internet request can
    have a loopback socket peer. Unknown peers fail closed. ``testclient`` is the one
    explicit non-IP exception used by Starlette's in-process test transport; shipped
    ASGI servers provide an IP address or ``None``.

    Container NAT is the one opt-in exception. ``ENGRAPHIS_LOCAL_TRUSTED_PEERS`` may
    contain exact peers or CIDRs that are treated as local when no forwarding header is
    present. The shipped Compose file uses this only together with a loopback-only host
    port, so a browser on the Docker host retains the zero-config local quickstart without
    making the port reachable from another machine.
    """
    import ipaddress

    for header in (
        "x-forwarded-for",
        "forwarded",
        "x-real-ip",
        "x-forwarded-host",
        "x-forwarded-proto",
        "x-forwarded-port",
        "x-forwarded-prefix",
    ):
        if request.headers.get(header):
            return False
    scope = getattr(request, "scope", {}) or {}
    client = scope.get("client")
    host = (client[0] if client else "") or ""
    if not host:
        return False
    try:
        address = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return host == "testclient"
    # A dual-stack IPv6 listener reports IPv4 loopback as ::ffff:127.0.0.1.
    normalized = getattr(address, "ipv4_mapped", None) or address
    if normalized.is_loopback:
        return True
    import os
    local_peers = os.environ.get("ENGRAPHIS_LOCAL_TRUSTED_PEERS", "").strip()
    return bool(local_peers and trusted_proxy(str(normalized), local_peers))
