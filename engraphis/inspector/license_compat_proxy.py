"""Bounded v1.0 compatibility proxy for retired customer-host license routes.

Keys issued before the control-plane split contain ``team.engraphis.com`` as their signed
server URL. The dedicated relay therefore keeps an explicit legacy route/method allowlist
reachable until the announced sunset even though issuance and leases now live on
``license.engraphis.com``. At the deadline every compatibility request returns 410 without an
upstream call. The proxy forwards only content-negotiation headers required by old clients:
never Authorization, cookies, customer API or deployment tokens, forwarded-client identity,
or vendor secrets.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote

import httpx
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, Response

from engraphis import __version__
from engraphis.config import resolve_license_server_url, settings


MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
COMPAT_SUNSET = "Sat, 17 Oct 2026 00:00:00 GMT"
COMPAT_SUNSET_EPOCH = datetime(2026, 10, 17, tzinfo=timezone.utc).timestamp()
_EXACT_ROUTE_METHODS = {
    "register": frozenset({"POST"}),
    "team-invite": frozenset({"POST"}),
    "password-reset": frozenset({"POST"}),
    "start-trial": frozenset({"POST"}),
    "start-trial/verify": frozenset({"GET", "HEAD", "POST"}),
}
_VERIFY_ROUTE = re.compile(r"verify/[0-9a-f]{12}")
_REQUEST_HEADERS = frozenset({"accept", "content-type"})
_RESPONSE_HEADERS = frozenset({
    "cache-control", "content-disposition", "content-type", "etag", "location",
    "referrer-policy", "retry-after", "www-authenticate",
})

router = APIRouter(prefix="/license/v1", include_in_schema=False)


def _deprecation_headers() -> dict:
    return {
        "Deprecation": "true",
        "Sunset": COMPAT_SUNSET,
        "Link": '<https://license.engraphis.com>; rel="successor-version"',
    }


def _allowed_methods(compat_path: str):
    exact = _EXACT_ROUTE_METHODS.get(compat_path)
    if exact is not None:
        return exact
    if _VERIFY_ROUTE.fullmatch(compat_path):
        return frozenset({"GET", "HEAD"})
    return None


def _sunset_reached(now: Optional[float] = None) -> bool:
    current = time.time() if now is None else float(now)
    return current >= COMPAT_SUNSET_EPOCH


async def _bounded_body(request: Request):
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        return None, JSONResponse(
            {"error": "invalid content length"}, status_code=400,
            headers=_deprecation_headers())
    if declared < 0:
        return None, JSONResponse(
            {"error": "invalid content length"}, status_code=400,
            headers=_deprecation_headers())
    if declared > MAX_REQUEST_BYTES:
        return None, JSONResponse(
            {"error": "license compatibility request is too large"}, status_code=413,
            headers=_deprecation_headers())
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_REQUEST_BYTES:
            return None, JSONResponse(
                {"error": "license compatibility request is too large"}, status_code=413,
                headers=_deprecation_headers())
        body.extend(chunk)
    return bytes(body), None


async def _send_upstream(method: str, url: str, headers: dict, body: bytes) -> httpx.Response:
    timeout = httpx.Timeout(15.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        return await client.request(method, url, headers=headers, content=body)


def _target_url(compat_path: str, query: str) -> str:
    base = resolve_license_server_url().rstrip("/")
    # Defense-in-depth: validate the resolved URL blocks private/reserved ranges
    # (SSRF) even though the source is a server env var, not user input.
    from engraphis.cloud_license import validate_cloud_base_url
    try:
        base = validate_cloud_base_url(base)
    except ValueError as exc:
        raise ValueError("license server URL is invalid: %s" % exc) from None
    path = quote(compat_path or "", safe="/-._~")
    target = base + "/license/v1" + (("/" + path) if path else "")
    if query:
        target += "?" + query
    return target


async def _proxy(request: Request, compat_path: str = ""):
    if settings.service_mode != "relay":
        return JSONResponse({"error": "compatibility proxy is unavailable"}, status_code=404)
    if _sunset_reached():
        return JSONResponse(
            {
                "error": "the license compatibility window has ended",
                "replacement": "https://license.engraphis.com/license/v1/",
            },
            status_code=410,
            headers=_deprecation_headers(),
        )
    if any(segment in (".", "..") for segment in compat_path.split("/")):
        return JSONResponse(
            {"error": "invalid license compatibility path"}, status_code=400,
            headers=_deprecation_headers())
    allowed = _allowed_methods(compat_path)
    if allowed is None:
        return JSONResponse(
            {"error": "license compatibility route is unavailable"}, status_code=404,
            headers=_deprecation_headers())
    if request.method.upper() not in allowed:
        headers = _deprecation_headers()
        headers["Allow"] = ", ".join(sorted(allowed))
        return JSONResponse(
            {"error": "method is not allowed for this compatibility route"},
            status_code=405,
            headers=headers,
        )
    body, error = await _bounded_body(request)
    if error is not None:
        return error
    headers = {
        name: value for name, value in request.headers.items()
        if name.lower() in _REQUEST_HEADERS
    }
    headers["user-agent"] = "Engraphis/%s license-compat-v1" % __version__
    try:
        target = _target_url(compat_path, request.url.query)
    except ValueError:
        return JSONResponse(
            {"error": "license server URL is misconfigured"}, status_code=502,
            headers=_deprecation_headers())
    try:
        upstream = await _send_upstream(request.method, target, headers, body or b"")
    except (httpx.TimeoutException, httpx.NetworkError):
        return JSONResponse(
            {"error": "license service is temporarily unavailable"}, status_code=503,
            headers=_deprecation_headers())
    except httpx.HTTPError:
        return JSONResponse(
            {"error": "license compatibility request failed"}, status_code=502,
            headers=_deprecation_headers())
    if len(upstream.content) > MAX_RESPONSE_BYTES:
        return JSONResponse(
            {"error": "license service response exceeded the compatibility limit"},
            status_code=502, headers=_deprecation_headers())
    response_headers = _deprecation_headers()
    response_headers.update({
        name: value for name, value in upstream.headers.items()
        if name.lower() in _RESPONSE_HEADERS
    })
    return Response(
        content=b"" if request.method == "HEAD" else upstream.content,
        status_code=upstream.status_code,
        headers=response_headers,
    )


@router.post("/register")
async def proxy_register(request: Request):
    return await _proxy(request, "register")


@router.api_route("/verify/{key_id}", methods=["GET", "HEAD"])
async def proxy_verify(key_id: str, request: Request):
    return await _proxy(request, "verify/" + key_id)


@router.post("/team-invite")
async def proxy_team_invite(request: Request):
    return await _proxy(request, "team-invite")


@router.post("/password-reset")
async def proxy_password_reset(request: Request):
    return await _proxy(request, "password-reset")


@router.post("/start-trial")
async def proxy_start_trial(request: Request):
    return await _proxy(request, "start-trial")


@router.api_route("/start-trial/verify", methods=["GET", "HEAD", "POST"])
async def proxy_start_trial_verify(request: Request):
    return await _proxy(request, "start-trial/verify")


def mount_license_compat_proxy(app: FastAPI) -> bool:
    """Mount only on the isolated relay; customer installs are never forwarders."""
    if settings.service_mode != "relay":
        return False
    app.include_router(router)
    app.state._license_compat_proxy_mounted = True
    return True
