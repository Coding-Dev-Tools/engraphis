"""Baseline HTTP security response headers, shared by every FastAPI entrypoint.

Added 2026-07-18. Prior to this the deployed dashboard sent NONE of these — verified
live against the production relay: no HSTS, no ``X-Frame-Options``, no
``X-Content-Type-Options``, no CSP, no ``Referrer-Policy``. For a surface that hands out
session cookies, license keys, and a full team's memories, the clickjacking and
MIME-sniffing exposure that implies is not acceptable at GA.

Design notes
------------
* **One middleware, mounted by every entrypoint** (``dashboard_app``, ``app``,
  ``inspector.app``) so a new entrypoint cannot silently ship without headers.
* **Never overwrite** a header a route already set deliberately — the trial-key page sets
  its own ``Cache-Control``/``Referrer-Policy``, and it should win.
* **HSTS only over HTTPS.** Sending it on a plain-HTTP localhost dashboard would pin
  ``127.0.0.1`` to HTTPS in the developer's browser for a year and break every other
  local project on that origin. The proxy case is handled by honouring the forwarded
  proto, which is what Railway presents.
* **Two policies, chosen by content type.** The JSON API and every error short-circuit
  get the strict :data:`DEFAULT_CSP` — they render no markup, so an ``unsafe-inline``
  allowance would buy an attacker nothing there. The dashboard assets are fully
  externalized (``dashboard.js``, ``dashboard.css``, vendored libs loaded via
  ``<script src>`` / ``<link rel="stylesheet">``), with event handlers delegated
  through ``data-on*`` attributes — so ``text/html`` responses get
  :data:`DASHBOARD_CSP`, which is identical to :data:`DEFAULT_CSP` (no
  ``unsafe-inline``). DOMPurify remains the sanitization boundary for ingested
  memory content. An explicit ``ENGRAPHIS_CSP`` override still wins wholesale,
  for both.
"""
from __future__ import annotations

import logging
import os
from urllib.parse import urlsplit, urlunsplit

from starlette.responses import JSONResponse, RedirectResponse

logger = logging.getLogger("engraphis.http")

#: Content-Security-Policy. Override wholesale with ``ENGRAPHIS_CSP``; set that to an
#: empty string to omit the header entirely (e.g. when a fronting proxy sets its own).
DEFAULT_CSP = "; ".join([
    "default-src 'self'",
    # Vendored d3/marked/DOMPurify and the dashboard assets are all served from /static.
    "script-src 'self'",
    "script-src-attr 'none'",
    "worker-src 'self'",
    "style-src 'self'",
    "style-src-attr 'none'",
    "font-src 'self'",
    "img-src 'self' data:",
    "connect-src 'self'",
    # The three that carry most of the value: no framing (clickjacking), no plugins,
    # no <base> rewrite, and forms can only post back to us.
    "frame-ancestors 'none'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
])

#: Alias for backward compatibility. The dashboard is now fully externalized
#: (no inline scripts/styles/handlers), so the same strict policy applies everywhere.
DASHBOARD_CSP = DEFAULT_CSP

#: 1 year, and explicitly NOT preload — a preload commitment is the operator's call to
#: make for their own domain, not something a library should make on their behalf.
DEFAULT_HSTS = "max-age=31536000; includeSubDomains"

def _trusted_forwarded_proto(request) -> str:
    """Return the rightmost proxy-reported scheme only for a trusted direct peer."""
    client = getattr(request, "client", None)
    direct = ((getattr(client, "host", "") if client else "") or "unknown")[:64]
    from engraphis.netutil import trusted_proxy
    if not trusted_proxy(
            direct, os.environ.get("ENGRAPHIS_FORWARDED_ALLOW_IPS", "")):
        return ""
    values = [
        item.strip().lower()
        for item in (request.headers.get("x-forwarded-proto") or "").split(",")
        if item.strip()
    ]
    return values[-1] if values else ""


def wants_https(request) -> bool:
    if request.url.scheme == "https":
        return True
    return _trusted_forwarded_proto(request) == "https"


def _canonical_https_origin() -> str:
    """Configured public HTTPS origin, or empty when no safe canonical host exists."""
    value = (
        os.environ.get("ENGRAPHIS_PUBLIC_URL", "").strip()
        or os.environ.get("ENGRAPHIS_DASHBOARD_URL", "").strip()
        or os.environ.get("ENGRAPHIS_RELAY_PUBLIC_URL", "").strip()
    )
    try:
        parts = urlsplit(value)
        parts.port
    except ValueError:
        return ""
    if (
        parts.scheme.lower() != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or chr(92) in parts.netloc
        or any(char.isspace() or ord(char) < 32 for char in parts.netloc)
    ):
        return ""
    return urlunsplit(("https", parts.netloc, "", "", "")).rstrip("/")


def _host_matches_origin(request, origin: str) -> bool:
    try:
        expected = urlsplit(origin).hostname
        supplied = urlsplit("//" + (request.headers.get("host") or "")).hostname
    except ValueError:
        return False
    return bool(expected and supplied and expected.lower() == supplied.lower())


def install(app) -> None:
    """Attach the baseline security headers middleware to *app*. Idempotent."""
    if getattr(app.state, "_security_headers_installed", False):
        return
    app.state._security_headers_installed = True

    csp_override = os.environ.get("ENGRAPHIS_CSP")
    csp = DEFAULT_CSP if csp_override is None else csp_override.strip()

    hsts = os.environ.get("ENGRAPHIS_HSTS")
    hsts = DEFAULT_HSTS if hsts is None else hsts.strip()
    https_origin = _canonical_https_origin()

    @app.middleware("http")
    async def _security_headers(request, call_next):
        if (
            https_origin
            and not wants_https(request)
            and _host_matches_origin(request, https_origin)
        ):
            raw_path = request.scope.get("raw_path") or b"/"
            path = raw_path.decode("ascii", "ignore")
            if not path.startswith("/"):
                path = "/"
            query = (request.scope.get("query_string") or b"").decode("ascii", "ignore")
            target = https_origin + path + (("?" + query) if query else "")
            response = RedirectResponse(target, status_code=308)
        else:
            try:
                response = await call_next(request)
            except Exception as exc:  # noqa: BLE001 - boundary: never expose internals
                logger.error(
                    "unhandled %s request failure (%s)",
                    request.method,
                    type(exc).__name__,
                )
                response = JSONResponse(
                    {"error": "internal server error"}, status_code=500)
        headers = response.headers
        # setdefault throughout: a route that set one of these on purpose wins.
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        # No reason for a memory dashboard to expose camera/mic/geolocation to anything.
        headers.setdefault("Permissions-Policy",
                           "geolocation=(), microphone=(), camera=(), payment=()")
        if csp:
            headers.setdefault("Content-Security-Policy", csp)
        if hsts and wants_https(request):
            headers.setdefault("Strict-Transport-Security", hsts)
        return response
