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
_WILDCARD_HOSTS = ("", "0.0.0.0", "::", "[::]")  # noqa: S104 — classified, not bound


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
    return "127.0.0.1" if (bind_host or "").strip() in _WILDCARD_HOSTS else bind_host


def display_base_url(host: str, port: int, *, scheme: str = "http") -> str:
    """A well-formed, connectable base URL for a server bound to *host*:*port* —
    wildcard binds become loopback and IPv6 literals are bracketed."""
    return "%s://%s:%d" % (scheme, bracket_host(connect_host(host)), int(port))
