"""The vendor admin token is a SEPARATE secret from the instance API token.

/license/v1's vendor-only routes (revoke, keys, revoke-by-email, deactivate, devices)
authorize vendor-wide actions against every customer on the shared relay. They must
prefer ``ENGRAPHIS_VENDOR_ADMIN_TOKEN``; the ``ENGRAPHIS_API_TOKEN`` fallback exists
only for continuity until the operator sets the dedicated variable (and warns once).
Same fixture posture as test_cloud_license.py (real signer, tmp relay DB).
"""
import time

import pytest

pytest.importorskip("fastapi")
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from engraphis import cloud_license, licensing
from engraphis.config import settings
from engraphis.inspector import license_cloud
from engraphis.inspector import license_registry as reg
from engraphis.licensing import LicenseError, ed25519_public_key, parse_key

SECRET = bytes(range(32))

pytestmark = pytest.mark.real_license_gate


@pytest.fixture(autouse=True)
def _cloud_env(monkeypatch, tmp_path):
    monkeypatch.setenv("ENGRAPHIS_LICENSE_PUBKEY", ed25519_public_key(SECRET).hex())
    monkeypatch.setenv("ENGRAPHIS_VENDOR_SIGNING_KEY", SECRET.hex())
    monkeypatch.setenv("ENGRAPHIS_RELAY_DB", str(tmp_path / "relay.db"))
    monkeypatch.delenv("ENGRAPHIS_VENDOR_ADMIN_TOKEN", raising=False)
    monkeypatch.setattr(cloud_license, "_DIR", tmp_path)
    monkeypatch.setattr(cloud_license, "_LEASE_FILE", tmp_path / "lease.sig")
    monkeypatch.setattr(cloud_license, "_MACHINE_ID_FILE", tmp_path / "machine_id")
    yield


def _key(plan="pro", email="buyer@example.com"):
    now = time.time()
    return licensing.compose_key(
        {"v": 1, "plan": plan, "email": email, "seats": 1,
         "issued": int(now), "expires": int(now + 30 * 86400)}, SECRET)


def _app():
    app = FastAPI()
    app.include_router(license_cloud.router)

    @app.exception_handler(LicenseError)
    async def _le(request, exc):
        return JSONResponse({"error": str(exc)}, status_code=402)

    return TestClient(app)


def _issued_kid():
    key = _key()
    reg.record_issued(key)
    return parse_key(key).key_id


def test_vendor_token_wins_and_api_token_stops_working(monkeypatch):
    """Once the dedicated vendor token is set, the shared instance token must NOT
    authorize vendor admin actions any more — that separation is the whole point."""
    c = _app()
    kid = _issued_kid()
    monkeypatch.setenv("ENGRAPHIS_VENDOR_ADMIN_TOKEN", "vendor-secret")
    monkeypatch.setattr(settings, "api_token", "instance-token")
    denied = c.post("/license/v1/revoke/%s" % kid,
                    headers={"Authorization": "Bearer instance-token"})
    assert denied.status_code == 401
    assert reg.is_revoked(kid) is False
    ok = c.post("/license/v1/revoke/%s" % kid,
                headers={"Authorization": "Bearer vendor-secret"})
    assert ok.status_code == 200 and reg.is_revoked(kid) is True


def test_fallback_to_api_token_until_operator_migrates(monkeypatch):
    """No vendor token configured → the API token still works (continuity), with a
    one-time warning nudging the operator to split the secrets."""
    c = _app()
    kid = _issued_kid()
    monkeypatch.setattr(settings, "api_token", "instance-token")
    monkeypatch.setattr(license_cloud, "_VENDOR_FALLBACK_WARNED", False)
    ok = c.post("/license/v1/revoke/%s" % kid,
                headers={"Authorization": "Bearer instance-token"})
    assert ok.status_code == 200 and reg.is_revoked(kid) is True
    assert license_cloud._VENDOR_FALLBACK_WARNED is True    # warning fired exactly once


def test_no_tokens_configured_fails_closed(monkeypatch):
    c = _app()
    kid = _issued_kid()
    monkeypatch.setattr(settings, "api_token", "")
    assert c.post("/license/v1/revoke/%s" % kid).status_code == 401
    assert c.post("/license/v1/revoke/%s" % kid,
                  headers={"Authorization": "Bearer anything"}).status_code == 401
    assert reg.is_revoked(kid) is False


def test_every_vendor_admin_route_uses_the_vendor_token(monkeypatch):
    c = _app()
    kid = _issued_kid()
    monkeypatch.setenv("ENGRAPHIS_VENDOR_ADMIN_TOKEN", "vendor-secret")
    monkeypatch.setattr(settings, "api_token", "instance-token")
    bad = {"Authorization": "Bearer instance-token"}
    good = {"Authorization": "Bearer vendor-secret"}
    probes = [
        ("GET", "/license/v1/keys?email=buyer@example.com", None),
        ("POST", "/license/v1/revoke-by-email", {"email": "buyer@example.com"}),
        ("GET", "/license/v1/keys/%s/devices" % kid, None),
        ("POST", "/license/v1/deactivate", {"key_id": kid, "machine_id": "m-1"}),
    ]
    for method, path, body in probes:
        r = c.request(method, path, headers=bad, json=body)
        assert r.status_code == 401, f"{path} accepted the instance token"
        r = c.request(method, path, headers=good, json=body)
        assert r.status_code == 200, f"{path} rejected the vendor token: {r.text}"
