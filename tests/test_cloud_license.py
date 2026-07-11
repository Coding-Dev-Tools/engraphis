"""Cloud license enforcement: registration issues a machine-bound signed lease; revoked/
expired/seat-limited keys are refused; forged leases are rejected; the client gate fails
closed in cloud mode. Also covers the salvaged local hardening (HMAC trial + monotonic
clock). Runs on the numpy-only gate (stdlib + fastapi TestClient).
"""
import json
import time

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from engraphis import cloud_license, licensing
from engraphis.config import settings
from engraphis.inspector import license_cloud
from engraphis.inspector import license_registry as reg
from engraphis.licensing import LicenseError, ed25519_public_key, parse_key

SECRET = bytes(range(32))


@pytest.fixture(autouse=True)
def _cloud_env(monkeypatch, tmp_path):
    monkeypatch.setenv("ENGRAPHIS_LICENSE_PUBKEY", ed25519_public_key(SECRET).hex())
    monkeypatch.setenv("ENGRAPHIS_VENDOR_SIGNING_KEY", SECRET.hex())  # server signs leases
    monkeypatch.setenv("ENGRAPHIS_RELAY_DB", str(tmp_path / "relay.db"))
    monkeypatch.delenv("ENGRAPHIS_CLOUD_URL", raising=False)
    monkeypatch.delenv("ENGRAPHIS_LICENSE_KEY", raising=False)
    # keep all client-side state files inside tmp
    monkeypatch.setattr(cloud_license, "_DIR", tmp_path)
    monkeypatch.setattr(cloud_license, "_LEASE_FILE", tmp_path / "lease.sig")
    monkeypatch.setattr(cloud_license, "_MACHINE_ID_FILE", tmp_path / "machine_id")
    monkeypatch.setattr(licensing, "_MONOTONIC_FILE", tmp_path / ".clock_anchor")
    monkeypatch.setattr(licensing, "_TRIAL_FILE", tmp_path / "trial.json")
    yield


def _key(plan="pro", email="buyer@example.com", *, seats=1, expires_in_days=30):
    now = time.time()
    exp = None if expires_in_days is None else int(now + expires_in_days * 86400)
    return licensing.compose_key(
        {"v": 1, "plan": plan, "email": email, "seats": seats,
         "issued": int(now), "expires": exp}, SECRET)


def _app():
    app = FastAPI()
    app.include_router(license_cloud.router)

    @app.exception_handler(LicenseError)
    async def _le(request, exc):
        return JSONResponse({"error": str(exc)}, status_code=402)

    return TestClient(app)


# ── server: registration + lease ──────────────────────────────────────────────────────

def test_register_issues_valid_machine_bound_lease():
    c = _app()
    r = c.post("/license/v1/register", json={"key": _key(), "machine_id": "m-1"})
    assert r.status_code == 200
    lease = r.json()["lease"]
    payload = cloud_license.verify_lease(lease)             # verifies signature + expiry
    assert payload["machine_id"] == "m-1" and payload["plan"] == "pro"
    assert "sync" in payload["features"]


def test_register_rejects_revoked_key():
    c = _app()
    key = _key()
    assert c.post("/license/v1/register", json={"key": key, "machine_id": "m"}).status_code == 200
    reg.revoke(parse_key(key).key_id)
    r = c.post("/license/v1/register", json={"key": key, "machine_id": "m2"})
    assert r.status_code == 402 and "revoked" in r.json()["error"]


def test_register_rejects_expired_key():
    c = _app()
    r = c.post("/license/v1/register",
               json={"key": _key(expires_in_days=-1), "machine_id": "m"})
    assert r.status_code == 402


def test_seat_cap_enforced():
    c = _app()
    key = _key(seats=1)
    assert c.post("/license/v1/register", json={"key": key, "machine_id": "A"}).status_code == 200
    # a second distinct machine exceeds the 1-seat cap
    over = c.post("/license/v1/register", json={"key": key, "machine_id": "B"})
    assert over.status_code == 402 and "seat" in over.json()["error"].lower()
    # the already-registered machine can always renew
    assert c.post("/license/v1/register", json={"key": key, "machine_id": "A"}).status_code == 200


def test_verify_endpoint_reflects_status():
    c = _app()
    key = _key()
    reg.record_issued(key)
    kid = parse_key(key).key_id
    assert c.get("/license/v1/verify/%s" % kid).json()["valid"] is True
    assert c.get("/license/v1/verify/unknownkey").json()["known"] is False
    reg.revoke(kid)
    assert c.get("/license/v1/verify/%s" % kid).json()["valid"] is False


def test_revoke_endpoint_requires_admin_token(monkeypatch):
    c = _app()
    kid = parse_key(_key()).key_id
    reg.record_issued(_key())
    assert c.post("/license/v1/revoke/%s" % kid).status_code == 401
    monkeypatch.setattr(settings, "api_token", "adm1n")
    ok = c.post("/license/v1/revoke/%s" % kid, headers={"Authorization": "Bearer adm1n"})
    assert ok.status_code == 200 and reg.is_revoked(kid) is True


def test_forged_lease_is_rejected():
    forged = cloud_license.compose_lease(
        {"v": 1, "key_id": "x", "plan": "pro", "features": ["sync"],
         "machine_id": "m", "issued": int(time.time()), "expires": int(time.time() + 9999)},
        b"\x09" * 32)                                        # attacker's own key
    with pytest.raises(LicenseError, match="signature"):
        cloud_license.verify_lease(forged)


def test_expired_lease_is_rejected():
    expired = cloud_license.compose_lease(
        {"v": 1, "key_id": "x", "plan": "pro", "features": ["sync"],
         "machine_id": "m", "issued": 0, "expires": int(time.time() - 10)}, SECRET)
    with pytest.raises(LicenseError, match="expired"):
        cloud_license.verify_lease(expired)


# ── client gate: cloud mode fails closed ────────────────────────────────────────────────

def _wire_register_to(client, monkeypatch):
    def fake_register(base, key, mid, timeout=6.0):
        r = client.post("/license/v1/register", json={"key": key, "machine_id": mid})
        return r.json().get("lease") if r.status_code == 200 else None
    monkeypatch.setattr(cloud_license, "register", fake_register)


def test_offline_mode_gate_always_allows(monkeypatch):
    # no ENGRAPHIS_CLOUD_URL → local signature is the gate
    lic = parse_key(_key())
    allowed, _ = cloud_license.gate(lic, _key())
    assert allowed is True


def test_cloud_gate_allows_then_fails_closed_after_revoke(monkeypatch, tmp_path):
    c = _app()
    _wire_register_to(c, monkeypatch)
    monkeypatch.setenv("ENGRAPHIS_CLOUD_URL", "http://cloud.test")
    key = _key()
    lic = parse_key(key)
    allowed, _ = cloud_license.gate(lic, key)               # registers, stores lease
    assert allowed is True and cloud_license._LEASE_FILE.exists()
    # revoke server-side and drop the cached lease → renewal denied → fail closed
    reg.revoke(lic.key_id)
    cloud_license._LEASE_FILE.unlink()
    allowed2, reason = cloud_license.gate(lic, key)
    assert allowed2 is False and "cloud" in reason.lower()


def test_current_license_enforces_cloud_mode(monkeypatch):
    c = _app()
    _wire_register_to(c, monkeypatch)
    monkeypatch.setenv("ENGRAPHIS_CLOUD_URL", "http://cloud.test")
    key = _key()
    monkeypatch.setenv("ENGRAPHIS_LICENSE_KEY", key)
    assert licensing.current_license(refresh=True).plan == "pro"   # registered → paid
    reg.revoke(parse_key(key).key_id)
    cloud_license._LEASE_FILE.unlink()
    assert licensing.current_license(refresh=True) == licensing.License.free()  # revoked → free


# ── salvaged local hardening: HMAC trial + monotonic clock ──────────────────────────────

def test_trial_tamper_is_rejected():
    licensing.start_trial()
    assert licensing.trial_status()["active"] is True
    # hand-edit the trial file to extend it → HMAC no longer matches → ignored
    raw = json.loads(licensing._TRIAL_FILE.read_text())
    raw["data"]["expires"] = int(time.time() + 9999 * 86400)
    licensing._TRIAL_FILE.write_text(json.dumps(raw))
    assert licensing.trial_status()["active"] is False


def test_monotonic_clock_never_goes_backward(monkeypatch):
    t0 = licensing._monotonic_now()
    monkeypatch.setattr(licensing.time, "time", lambda: t0 - 100000)  # roll clock back
    assert licensing._monotonic_now() >= t0


# ── team licensing: server-gated, seat-capped, revocable, lease-backed ─────────────────

def test_team_key_registers_with_team_feature():
    c = _app()
    r = c.post("/license/v1/register",
               json={"key": _key(plan="team", seats=3), "machine_id": "t1"})
    assert r.status_code == 200
    payload = cloud_license.verify_lease(r.json()["lease"])
    assert payload["plan"] == "team" and "team" in payload["features"]


def test_team_seat_cap_blocks_extra_devices():
    c = _app()
    key = _key(plan="team", seats=2)
    assert c.post("/license/v1/register", json={"key": key, "machine_id": "d1"}).status_code == 200
    assert c.post("/license/v1/register", json={"key": key, "machine_id": "d2"}).status_code == 200
    over = c.post("/license/v1/register", json={"key": key, "machine_id": "d3"})
    assert over.status_code == 402 and "seat" in over.json()["error"].lower()


def test_team_feature_cannot_be_bypassed_in_cloud_mode(monkeypatch):
    """The team gate (has_feature('team')) is lease-backed in cloud mode: a revoked team
    key with no lease loses team capability — a local patch to trial/key can't restore it."""
    c = _app()
    _wire_register_to(c, monkeypatch)
    monkeypatch.setenv("ENGRAPHIS_CLOUD_URL", "http://cloud.test")
    key = _key(plan="team", seats=3)
    monkeypatch.setenv("ENGRAPHIS_LICENSE_KEY", key)
    assert licensing.current_license(refresh=True).plan == "team"
    assert licensing.has_feature("team") is True                 # registered → team active
    reg.revoke(parse_key(key).key_id)
    cloud_license._LEASE_FILE.unlink()
    licensing.current_license(refresh=True)
    assert licensing.has_feature("team") is False                # revoked → team gone


def test_trial_never_grants_team():
    # the local trial is Pro-only; it must never unlock team (multi-user) capability
    licensing.start_trial()
    lic = licensing.current_license(refresh=True)
    assert lic.is_trial and lic.plan == "pro"
    assert licensing.has_feature("team") is False
