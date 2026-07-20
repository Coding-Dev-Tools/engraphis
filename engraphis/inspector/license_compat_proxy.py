"""Bounded v1.0 compatibility proxy for retired customer-host license routes.

Keys issued before the control-plane split contain ``team.engraphis.com`` as their signed
server URL. Customer mode must therefore keep ``/license/v1/*`` reachable for the announced
90-day migration window even though issuance and leases now live on
``license.engraphis.com``. The proxy forwards only protocol headers required by license
clients: never cookies, customer API tokens, forwarded-client identity, or vendor secrets.
"""
from __future__ import annotations

from urllib.parse import quote

import httpx
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, Response

from engraphis import __version__
from engraphis.config import resolve_license_server_url, settings


MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
COMPAT_SUNSET = "Sat, 17 Oct 2026 00:00:00 GMT"
_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
_REQUEST_HEADERS = frozenset({"accept", "authorization", "content-type"})
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
    path = quote(compat_path or "", safe="/-._~")
    target = base + "/license/v1" + (("/" + path) if path else "")
    if query:
        target += "?" + query
    return target


async def _proxy(request: Request, compat_path: str = ""):
    if settings.service_mode != "customer":
        return JSONResponse({"error": "compatibility proxy is unavailable"}, status_code=404)
    if any(segment in (".", "..") for segment in compat_path.split("/")):
        return JSONResponse(
            {"error": "invalid license compatibility path"}, status_code=400,
            headers=_deprecation_headers())
    body, error = await _bounded_body(request)
    if error is not None:
        return error
    headers = {
        name: value for name, value in request.headers.items()
        if name.lower() in _REQUEST_HEADERS
    }
    headers["user-agent"] = "Engraphis/%s license-compat-v1" % __version__
    target = _target_url(compat_path, request.url.query)
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


@router.api_route("", methods=_METHODS)
async def proxy_license_root(request: Request):
    return await _proxy(request)


@router.api_route("/{compat_path:path}", methods=_METHODS)
async def proxy_license_path(compat_path: str, request: Request):
    return await _proxy(request, compat_path)


def mount_license_compat_proxy(app: FastAPI) -> bool:
    """Mount only on the isolated customer service; combined keeps local v1 routes."""
    if settings.service_mode != "customer":
        return False
    app.include_router(router)
    app.state._license_compat_proxy_mounted = True
    return True
