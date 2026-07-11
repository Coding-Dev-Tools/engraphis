"""Server-side sync-relay gate: only a valid, unexpired, non-revoked, sync-plan license
can push or pull, and accounts are isolated. Also exercises the client RelayTransport
end-to-end against the real endpoints via a TestClient shim.

Runs on the numpy-only gate: stdlib + fastapi TestClient, no network.
"""
import base64
import io
import time
import urllib.error
import urllib.parse

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from engraphis import licensing
from engraphis.licensing import LicenseError, ed25519_public_key
from engraphis.inspector import license_registry as reg
from engraphis.inspector import sync_relay
from engraphis.backends.sync_relay import RelayTransport, RelayError

SECRET = bytes(range(32))  # deterministic test vendor keypair


@pytest.fixture(autouse=True)
def _relay_env(monkeypatch, tmp_path):
    # verify against the test keypair (conftest already sets _TEST_MODE_PUBKEY_OVERRIDE)
    monkeypatch.setenv("ENGRAPHIS_LICENSE_PUBKEY", ed25519_public_key(SECRET).hex())
    monkeypatch.setenv("ENGRAPHIS_RELAY_DB", str(tmp_path / "relay.db"))
    monkeypatch.delenv("ENGRAPHIS_LICENSE_KEY", raising=False)
    yield


def _key(plan="pro", email="buyer@example.com", *, expires_in_days=30):
    now = time.time()
    exp = None if expires_in_days is None else int(now + expires_in_days * 86400)
    payload = {"v": 1, "plan": plan, "email": email, "seats": 1,
               "issued": int(now), "expires": exp}
    return licensing.compose_key(payload, SECRET)


def _app():
    app = FastAPI()
    app.include_router(sync_relay.router)

    @app.exception_handler(LicenseError)
    async def _license(request, exc):
        return JSONResponse({"error": str(exc), "feature": getattr(exc, "feature", None)},
                            status_code=402)

    return TestClient(app)


# ── server-side gate ─────────────────────────────────────────────────────────────────

def _auth(key):
    return {"Authorization": "Bearer %s" % key}


def test_valid_pro_key_can_push_and_pull():
    c = _app()
    r = c.post("/relay/v1/ws1/bundles/bundle-devA.json",
               content=b'{"hello":1}', headers=_auth(_key()))
    assert r.status_code == 200 and r.json()["bytes"] == 11
    got = c.get("/relay/v1/ws1/bundles", headers=_auth(_key()))
    assert got.status_code == 200
    bundles = got.json()["bundles"]
    assert len(bundles) == 1 and bundles[0]["name"] == "bundle-devA.json"
    assert base64.b64decode(bundles[0]["data"]) == b'{"hello":1}'


def test_missing_key_is_rejected():
    c = _app()
    assert c.get("/relay/v1/ws1/bundles").status_code == 402
    assert c.post("/relay/v1/ws1/bundles/x.json", content=b"{}").status_code == 402


def test_garbage_key_is_rejected():
    c = _app()
    assert c.get("/relay/v1/ws1/bundles", headers=_auth("ENGR1.!!!.???")).status_code == 402


def test_expired_key_is_rejected():
    c = _app()
    r = c.get("/relay/v1/ws1/bundles", headers=_auth(_key(expires_in_days=-1)))
    assert r.status_code == 402


def test_revoked_key_is_rejected():
    c = _app()
    key = _key()
    reg.record_issued(key)                       # in the registry, active
    assert c.get("/relay/v1/ws1/bundles", headers=_auth(key)).status_code == 200
    lic = licensing.parse_key(key)
    assert reg.revoke(lic.key_id) is True
    assert c.get("/relay/v1/ws1/bundles", headers=_auth(key)).status_code == 402


def test_accounts_are_isolated():
    c = _app()
    a, b = _key(email="a@x.com"), _key(email="b@x.com")
    c.post("/relay/v1/ws1/bundles/bundle-A.json", content=b"AAA", headers=_auth(a))
    # B shares the workspace name but a different license identity → sees nothing of A's
    assert c.get("/relay/v1/ws1/bundles", headers=_auth(b)).json()["bundles"] == []
    c.post("/relay/v1/ws1/bundles/bundle-B.json", content=b"BBB", headers=_auth(b))
    a_view = c.get("/relay/v1/ws1/bundles", headers=_auth(a)).json()["bundles"]
    assert [x["name"] for x in a_view] == ["bundle-A.json"]  # still only A's own


def test_wrong_plan_feature_is_rejected():
    # a pro key does not grant 'team'; verify_for_feature must refuse it
    with pytest.raises(LicenseError, match="team"):
        reg.verify_for_feature(_key(plan="pro"), "team")


# ── registry unit behavior ─────────────────────────────────────────────────────────────

def test_registry_record_then_revoke():
    key = _key()
    kid = reg.record_issued(key)
    assert reg.is_revoked(kid) is False
    assert reg.revoke(kid) is True
    assert reg.is_revoked(kid) is True
    assert reg.revoke(kid) is False            # already revoked → no-op


def test_unknown_key_is_not_treated_as_revoked():
    # a validly-signed key with no registry row (sold pre-registry) is allowed
    assert reg.is_revoked("deadbeef0000") is False


# ── client RelayTransport, end-to-end against the real endpoints ────────────────────────

def _wire_transport_to(client, monkeypatch):
    """Route the transport's urllib calls into the in-process TestClient."""
    class _Resp:
        def __init__(self, data): self._d = data
        def read(self): return self._d
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=None):
        path = urllib.parse.urlsplit(req.full_url).path
        headers = dict(req.headers)
        if req.method == "POST":
            resp = client.post(path, content=req.data or b"", headers=headers)
        else:
            resp = client.get(path, headers=headers)
        if resp.status_code >= 400:
            raise urllib.error.HTTPError(req.full_url, resp.status_code, resp.text,
                                         None, io.BytesIO(resp.content))
        return _Resp(resp.content)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)


def test_relay_transport_roundtrip(monkeypatch):
    c = _app()
    _wire_transport_to(c, monkeypatch)
    t = RelayTransport("http://relay.test", "ws1", license_key=_key())
    t.push("bundle-devA.json", b'{"m":1}')
    assert t.list_names() == ["bundle-devA.json"]
    assert t.pull() == [("bundle-devA.json", b'{"m":1}')]


def test_relay_transport_surfaces_license_rejection(monkeypatch):
    c = _app()
    _wire_transport_to(c, monkeypatch)
    t = RelayTransport("http://relay.test", "ws1", license_key="")  # no license
    with pytest.raises(RelayError) as ei:
        t.pull()
    assert ei.value.status == 402
