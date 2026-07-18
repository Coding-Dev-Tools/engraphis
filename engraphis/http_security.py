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
* **CSP is tuned to the dashboard as it actually is**, not to an ideal: ``static/index.html``
  carries inline ``<script>``/``<style>`` blocks, so ``'unsafe-inline'`` is allowed. Its
  only external origin is a ``https://github.com`` hyperlink (navigation, which no
  fetch directive governs) — every script, style, and font is same-origin, so the policy
  needs no third-party allowances at all. That is weaker than a nonce policy but still
  blocks what matters here: third-party script injection, framing, form hijacking, plugin
  content, and ``<base>`` rewriting. Tightening to nonces is a follow-up that requires
  editing the template, not this module.
"""
from __future__ import annotations

import logging
import os

from starlette.responses import JSONResponse

logger = logging.getLogger("engraphis.http")

#: Content-Security-Policy. Override wholesale with ``ENGRAPHIS_CSP``; set that to an
#: empty string to omit the header entirely (e.g. when a fronting proxy sets its own).
DEFAULT_CSP = "; ".join([
    "default-src 'self'",
    # The dashboard's inline <script> and <style> blocks need 'unsafe-inline'. Nothing
    # else is third-party: the vendored d3/marked/DOMPurify bundles are served from
    # /static, and the Google webfonts were removed from index.html — allowlisting
    # origins the page no longer contacts would only widen the policy for nothing.
    "script-src 'self' 'unsafe-inline'",
    "style-src 'self' 'unsafe-inline'",
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


def install(app) -> None:
    """Attach the baseline security headers middleware to *app*. Idempotent."""
    if getattr(app.state, "_security_headers_installed", False):
        return
    app.state._security_headers_installed = True

    csp = os.environ.get("ENGRAPHIS_CSP")
    csp = DEFAULT_CSP if csp is None else csp.strip()
    hsts = os.environ.get("ENGRAPHIS_HSTS")
    hsts = DEFAULT_HSTS if hsts is None else hsts.strip()

    @app.middleware("http")
    async def _security_headers(request, call_next):
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
