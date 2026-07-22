"""In-process transport from customer cloud clients to the real vendor relay routes."""
from __future__ import annotations

import io
import json
import urllib.error
import urllib.parse

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from engraphis.inspector import license_cloud
from engraphis.licensing import LicenseError


def wire_vendor_relay(monkeypatch, tmp_path) -> TestClient:
    """Route ``urllib`` vendor-client calls through the real FastAPI relay endpoints."""
    monkeypatch.setenv("ENGRAPHIS_RELAY_DB", str(tmp_path / "vendor-relay.db"))
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

        def read(self, limit=-1):
            return self._data if limit < 0 else self._data[:limit]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def _urlopen(request, timeout=None):
        del timeout
        path = urllib.parse.urlsplit(request.full_url).path
        # The customer and vendor are separate production processes. This in-process
        # adapter must therefore switch the shared test settings only for the vendor call.
        from engraphis.config import settings
        from engraphis.inspector import license_registry
        prior_mode = settings.service_mode
        settings.service_mode = "vendor"
        try:
            payload = json.loads((request.data or b"{}").decode("utf-8"))
            key = payload.get("key") if isinstance(payload, dict) else None
            if key:
                # This fixture represents a previously fulfilled purchase. The hardened
                # vendor gate accepts only keys present in its authoritative registry.
                license_registry.record_issued(key)
            response = vendor.post(
                path, content=request.data or b"", headers=dict(request.headers))
        finally:
            settings.service_mode = prior_mode
        if response.status_code >= 400:
            raise urllib.error.HTTPError(
                request.full_url, response.status_code, response.text,
                response.headers, io.BytesIO(response.content),
            )
        return _Response(response.content)

    from engraphis import cloud_license
    monkeypatch.setattr(cloud_license, "_urlopen_no_redirect", _urlopen)
    return vendor
