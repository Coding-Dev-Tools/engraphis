"""In-process transport from customer cloud clients to the real vendor relay routes."""
from __future__ import annotations

import io
import urllib.error
import urllib.parse

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from engraphis.inspector import license_cloud
from engraphis.licensing import LicenseError


def wire_vendor_relay(monkeypatch) -> TestClient:
    """Route ``urllib`` vendor-client calls through the real FastAPI relay endpoints."""
    app = FastAPI()
    app.include_router(license_cloud.router)

    @app.exception_handler(LicenseError)
    async def _license_error(_request, exc):
        return JSONResponse(
            {"error": str(exc), "feature": getattr(exc, "feature", None)},
            status_code=402,
        )

    vendor = TestClient(app)

    class _Response:
        def __init__(self, data: bytes):
            self._data = data

        def read(self):
            return self._data

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def _urlopen(request, timeout=None):
        del timeout
        path = urllib.parse.urlsplit(request.full_url).path
        response = vendor.post(path, content=request.data or b"", headers=dict(request.headers))
        if response.status_code >= 400:
            raise urllib.error.HTTPError(
                request.full_url, response.status_code, response.text,
                response.headers, io.BytesIO(response.content),
            )
        return _Response(response.content)

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    return vendor
