"""Cloud license enforcement: registration issues a machine-bound signed lease; revoked/
expired/seat-limited keys are refused; forged leases are rejected; the client gate fails
closed in cloud mode. Also covers the salvaged local hardening (HMAC trial + monotonic
clock). Runs on the numpy-only gate (stdlib + fastapi TestClient).
"""
import io
import re
import time
import urllib.error
import urllib.parse

import pytest

pytest.importorskip("fastapi")
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from engraphis import cloud_license, licensing
from engraphis.config import DEFAULT_RELAY_URL, settings
from engraphis.inspector import license_cloud
from engraphis.inspector import license_registry as reg
from engraphis.licensing import LicenseError, ed25519_public_key, parse_key

SECRET = bytes(range(32))

# Exercises the real server-side license gate — opt out of conftest's approve stub.
pytestmark = pytest.mark.real_license_gate


@pytest.fixture(autouse=True)
def _cloud_env(monkeypatch, tmp_path):
    monkeypatch.setenv("ENGRAPHIS_LICENSE_PUBKEY", ed25519_public_key(SECRET).hex())
    monkeypatch.setenv("ENGRAPHIS_VENDOR_SIGNING_KEY", SECRET.hex())  # server signs leases
    monkeypatch.setenv("ENGRAPHIS_RELAY_DB", str(tmp_path / "relay.db"))
    monkeypatch.delenv("ENGRAPHIS_CLOUD_URL", raising=False)
    monkeypatch.delenv("ENGRAPHIS_LICENSE_KEY", raising=False)
    monkeypatch.delenv("ENGRAPHIS_FORWARDED_ALLOW_IPS", raising=False)
    # keep all client-side state files inside tmp
    monkeypatch.setattr(cloud_license, "_DIR", tmp_path)
    monkeypatch.setattr(cloud_license, "_LEASE_FILE", tmp_path / "lease.sig")
    monkeypatch.setattr(cloud_license, "_MACHINE_ID_FILE", tmp_path / "machine_id")
    monkeypatch.setattr(licensing, "_MONOTONIC_FILE", tmp_path / ".clock_anchor")
    monkeypatch.setattr(licensing, "_TRIAL_FILE", tmp_path / "trial.json")
    yield


@pytest.fixture(autouse=True)
def _reset_api_token():
    """Ensure settings.api_token is clean for each test (prevents cross-test leakage)."""
    original = settings.api_token
    yield
    settings.api_token = original


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
    key = _key()
    kid = parse_key(key).key_id
    reg.record_issued(key)
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


def test_gate_fails_closed_without_server(monkeypatch):
    # online-only: with no server to verify against, the gate DENIES (was inert-allow).
    monkeypatch.delenv("ENGRAPHIS_CLOUD_URL", raising=False)
    lic = parse_key(_key())
    allowed, reason = cloud_license.gate(lic, _key())
    assert allowed is False and "server" in reason.lower()


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


def test_cloud_gate_revocation_overrides_a_valid_cached_lease(monkeypatch):
    c = _app()
    _wire_register_to(c, monkeypatch)
    monkeypatch.setenv("ENGRAPHIS_CLOUD_URL", "http://cloud.test")
    key = _key()
    parsed = parse_key(key)
    assert cloud_license.gate(parsed, key)[0] is True
    assert cloud_license._LEASE_FILE.exists()

    def denied(*args, **kwargs):
        raise cloud_license.Revoked("denied")
    monkeypatch.setattr(cloud_license, "register", denied)
    allowed, reason = cloud_license.gate(parsed, key)

    assert allowed is False and "denied" in reason
    assert not cloud_license._LEASE_FILE.exists()


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


# ── background revocation re-validation (non-blocking) ──────────────────────────────────

def test_register_raises_revoked_on_server_denial(monkeypatch):
    # A 402/403 from the server is an authoritative DENIAL, not 'offline': register must
    # raise Revoked so revalidate/gate fail closed immediately instead of falling back to
    # the cached lease (offline grace). Network/5xx errors stay None (the grace path).
    class _HTTPError(urllib.error.HTTPError):
        def __init__(self, code): super().__init__("http://x", code, "denied", None, io.BytesIO(b""))
    def _urlopen(req, timeout=None): raise _HTTPError(402)
    monkeypatch.setattr(cloud_license.urllib.request, "urlopen", _urlopen)
    with pytest.raises(cloud_license.Revoked):
        cloud_license.register("http://cloud.test", _key(), "m-1")
    def _urlopen_5xx(req, timeout=None): raise _HTTPError(503)
    monkeypatch.setattr(cloud_license.urllib.request, "urlopen", _urlopen_5xx)
    assert cloud_license.register("http://cloud.test", _key(), "m-1") is None


def test_license_client_sets_cloudflare_safe_headers(monkeypatch):
    captured = {}

    class _Resp:
        def read(self): return b'{"lease": null}'
        def __enter__(self): return self
        def __exit__(self, *args): return False

    def fake_urlopen(req, timeout=None):
        captured["user_agent"] = req.get_header("User-agent")
        captured["accept"] = req.get_header("Accept")
        return _Resp()

    monkeypatch.setattr(cloud_license.urllib.request, "urlopen", fake_urlopen)
    assert cloud_license.register("http://cloud.test", _key(), "m-1") is None
    assert captured == {
        "user_agent": "Engraphis/1.0 (+https://engraphis.com)",
        "accept": "application/json",
    }


def test_revalidate_revoked_deletes_lease(monkeypatch):
    # A paid key with a valid local lease is periodically checked online. Background
    # revalidation uses the same denial path and deletes the lease immediately.
    c = _app()
    _wire_register_to(c, monkeypatch)
    monkeypatch.setenv("ENGRAPHIS_CLOUD_URL", "http://cloud.test")
    key = _key()
    lic = parse_key(key)
    assert cloud_license.gate(lic, key)[0] is True
    assert cloud_license._LEASE_FILE.exists()
    reg.revoke(lic.key_id)
    def _revoking_register(base, k, mid, timeout=6.0):
        r = c.post("/license/v1/register", json={"key": k, "machine_id": mid})
        if r.status_code in (402, 403):
            raise cloud_license.Revoked("denied")
        return r.json().get("lease") if r.status_code == 200 else None
    monkeypatch.setattr(cloud_license, "register", _revoking_register)
    assert cloud_license.revalidate(lic, key, base_url="http://cloud.test") == "revoked"
    assert not cloud_license._LEASE_FILE.exists()
    assert cloud_license.gate(lic, key)[0] is False


def test_revalidate_offline_keeps_lease_grace(monkeypatch):
    # A paying customer briefly offline: revalidate can't reach the server → 'offline', and
    # the cached lease STAYS (offline grace), so paid features keep working.
    c = _app()
    _wire_register_to(c, monkeypatch)
    monkeypatch.setenv("ENGRAPHIS_CLOUD_URL", "http://cloud.test")
    key = _key()
    lic = parse_key(key)
    cloud_license.gate(lic, key)
    monkeypatch.setattr(cloud_license, "register", lambda *a, **k: None)
    assert cloud_license.revalidate(lic, key, base_url="http://cloud.test") == "offline"
    assert cloud_license._LEASE_FILE.exists()
    assert cloud_license.gate(lic, key)[0] is True


def test_revalidate_ok_refreshes_lease(monkeypatch):
    # An online, still-valid key: revalidate re-registers (refreshing the seat + lease) and
    # returns 'ok'. This is the steady state for a paying customer.
    c = _app()
    _wire_register_to(c, monkeypatch)
    monkeypatch.setenv("ENGRAPHIS_CLOUD_URL", "http://cloud.test")
    key = _key()
    lic = parse_key(key)
    cloud_license.gate(lic, key)
    assert cloud_license.revalidate(lic, key, base_url="http://cloud.test") == "ok"
    assert cloud_license._LEASE_FILE.exists()


# ── server-issued trial + monotonic clock (lease anti-rollback) ─────────────────────────

def test_start_trial_activates_server_issued_pro_key(monkeypatch):
    """The Pro trial is now a REAL server-issued key (no local/offline grant to forge or
    tamper with). start_trial fetches it and activates it; online-only, it needs a lease."""
    now = time.time()
    pro_trial = licensing.compose_key(
        {"v": 1, "plan": "pro", "email": "trial@engraphis.local", "seats": 1,
         "issued": int(now), "expires": int(now + 3 * 86400), "trial": 1}, SECRET)
    monkeypatch.setattr(cloud_license, "request_trial_key",
                        lambda base, mid, plan="team", email="": (pro_trial, "", False))
    c = _app()
    _wire_register_to(c, monkeypatch)
    monkeypatch.setenv("ENGRAPHIS_CLOUD_URL", "http://cloud.test")
    out = licensing.start_trial(email="trial@engraphis.local")
    assert out["plan"] == "pro" and out["is_trial"] is True
    assert licensing.current_license(refresh=True).plan == "pro"
    assert licensing.has_feature("analytics") is True


def test_start_trial_is_idempotent_while_already_on_trial(monkeypatch):
    """Re-calling start_trial() while an active trial key is already installed must be
    a no-op that returns the current status — NOT the 'a paid license is already
    active' refusal (that's for genuinely PAID keys only). Regression test: before the
    fix, any locally-parseable key — trial or paid — hit that refusal, so re-opening
    the dashboard mid-trial (which calls this on every 'start trial' click) 400'd."""
    now = time.time()
    pro_trial = licensing.compose_key(
        {"v": 1, "plan": "pro", "email": "trial@engraphis.local", "seats": 1,
         "issued": int(now), "expires": int(now + 3 * 86400), "trial": 1}, SECRET)
    calls = []

    def _request(base, mid, plan="team", email=""):
        calls.append(1)
        return pro_trial, "", False

    monkeypatch.setattr(cloud_license, "request_trial_key", _request)
    c = _app()
    _wire_register_to(c, monkeypatch)
    monkeypatch.setenv("ENGRAPHIS_CLOUD_URL", "http://cloud.test")
    licensing.start_trial(email="trial@engraphis.local")
    assert len(calls) == 1
    out = licensing.start_trial(email="trial@engraphis.local")   # re-call: must not error,
    assert out["plan"] == "pro" and out["is_trial"] is True      # must not hit the relay again
    assert len(calls) == 1          # no second relay round-trip


def test_start_trial_refuses_if_paid_key_already_active(monkeypatch):
    """Refusal is only correct for a key the cloud gate ACTUALLY approves right now — see
    the 2026-07-13 fix below. Wire the gate to approve so this covers the genuine "this
    key really is active" case, not just "a key that merely parses"."""
    c = _app()
    _wire_register_to(c, monkeypatch)
    monkeypatch.setenv("ENGRAPHIS_CLOUD_URL", "http://cloud.test")
    monkeypatch.setenv("ENGRAPHIS_LICENSE_KEY", _key(plan="pro"))
    with pytest.raises(LicenseError, match="no trial needed"):
        licensing.start_trial()


def test_start_trial_proceeds_when_local_key_is_cloud_denied(monkeypatch):
    """2026-07-13 incident: a signature-valid, non-trial key is configured locally, but
    the cloud gate denies it (revoked / never registered / relay unreachable / seat cap).
    current_license() correctly falls back to the free tier — no paid features — but
    start_trial() used to refuse anyway with "a paid license is already active," because
    it only checked LOCAL signature validity (_local_material_license), never the cloud
    gate. That stranded the user with neither working features nor any way to get a
    trial. Fixed: start_trial() now re-verifies against current_license() before
    refusing, so a key that is cloud-denied no longer blocks a fresh trial."""
    stale_key = _key(plan="pro")
    c = _app()

    def fake_register(base, key, mid, timeout=6.0):
        if key == stale_key:
            return None                      # the existing key can no longer be verified
        r = c.post("/license/v1/register", json={"key": key, "machine_id": mid})
        return r.json().get("lease") if r.status_code == 200 else None

    monkeypatch.setattr(cloud_license, "register", fake_register)
    monkeypatch.setenv("ENGRAPHIS_CLOUD_URL", "http://cloud.test")
    licensing.activate(stale_key)            # persists (signature-only check, like a real
                                              # customer pasting an old key)
    assert licensing.current_license(refresh=True).plan == "free"   # cloud gate denies it
    assert licensing.has_feature("analytics") is False              # -> no paid features

    now = time.time()
    pro_trial = licensing.compose_key(
        {"v": 1, "plan": "pro", "email": "trial@engraphis.local", "seats": 1,
         "issued": int(now), "expires": int(now + 3 * 86400), "trial": 1}, SECRET)
    monkeypatch.setattr(cloud_license, "request_trial_key",
                        lambda base, mid, plan="pro", email="": (pro_trial, "", False))
    out = licensing.start_trial(email="trial@engraphis.local")  # must NOT raise "already active"
    assert out["plan"] == "pro" and out["is_trial"] is True
    assert licensing.has_feature("analytics") is True


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


def test_pro_trial_never_grants_team(monkeypatch):
    # the Pro trial is Pro-only; it must never unlock team (multi-user) capability
    now = time.time()
    pro_trial = licensing.compose_key(
        {"v": 1, "plan": "pro", "email": "trial@engraphis.local", "seats": 1,
         "issued": int(now), "expires": int(now + 3 * 86400), "trial": 1}, SECRET)
    monkeypatch.setattr(cloud_license, "request_trial_key",
                        lambda base, mid, plan="team", email="": (pro_trial, "", False))
    c = _app()
    _wire_register_to(c, monkeypatch)
    monkeypatch.setenv("ENGRAPHIS_CLOUD_URL", "http://cloud.test")
    licensing.start_trial(email="trial@engraphis.local")
    lic = licensing.current_license(refresh=True)
    assert lic.is_trial and lic.plan == "pro"
    assert licensing.has_feature("team") is False


# ── admin operations: revoke-by-email, key lookup, device visibility, deactivate ───────

def _admin(monkeypatch):
    monkeypatch.setattr(settings, "api_token", "adm1n")
    return {"Authorization": "Bearer adm1n"}


def test_revoke_by_email_kills_all_customer_keys(monkeypatch):
    c = _app()
    h = _admin(monkeypatch)
    k1, k2 = _key(email="team@corp.com", plan="team", seats=3), _key(email="team@corp.com")
    reg.record_issued(k1)
    reg.record_issued(k2)
    assert c.post("/license/v1/revoke-by-email").status_code == 401       # needs admin
    r = c.post("/license/v1/revoke-by-email", json={"email": "team@corp.com"}, headers=h)
    assert r.status_code == 200 and r.json()["count"] == 2
    assert reg.is_revoked(parse_key(k1).key_id) and reg.is_revoked(parse_key(k2).key_id)


def test_keys_lookup_by_email_shows_seat_usage(monkeypatch):
    c = _app()
    h = _admin(monkeypatch)
    key = _key(email="admin@corp.com", plan="team", seats=3)
    c.post("/license/v1/register", json={"key": key, "machine_id": "d1"})
    c.post("/license/v1/register", json={"key": key, "machine_id": "d2"})
    r = c.get("/license/v1/keys", params={"email": "admin@corp.com"}, headers=h)
    assert r.status_code == 200
    ks = r.json()["keys"]
    assert ks and ks[0]["plan"] == "team" and ks[0]["devices_used"] == 2 and ks[0]["seats"] == 3


def test_deactivate_frees_a_seat(monkeypatch):
    c = _app()
    h = _admin(monkeypatch)
    key = _key(plan="team", seats=2)
    kid = parse_key(key).key_id
    for m in ("d1", "d2"):
        assert c.post("/license/v1/register", json={"key": key, "machine_id": m}).status_code == 200
    assert c.post("/license/v1/register", json={"key": key, "machine_id": "d3"}).status_code == 402
    # free d1's seat, then d3 fits
    assert c.get("/license/v1/keys/%s/devices" % kid, headers=h).json()["devices"].__len__() == 2
    d = c.post("/license/v1/deactivate", json={"key_id": kid, "machine_id": "d1"}, headers=h)
    assert d.status_code == 200 and d.json()["deactivated"] is True
    assert c.post("/license/v1/register", json={"key": key, "machine_id": "d3"}).status_code == 200


def test_admin_endpoints_require_token():
    c = _app()  # no admin token set → all admin ops rejected
    assert c.get("/license/v1/keys", params={"email": "x@y.com"}).status_code == 401
    assert c.get("/license/v1/keys/abc/devices").status_code == 401
    assert c.post("/license/v1/deactivate", json={"key_id": "a", "machine_id": "b"}).status_code == 401


# ── seat reclamation: idle seats free automatically so the cap self-heals ───────────────

def test_register_reclaims_idle_seat(monkeypatch):
    monkeypatch.setenv("ENGRAPHIS_LEASE_TTL_HOURS", "1")   # ttl=1h → reclaim window 2h
    c = _app()
    key = _key(plan="team", seats=1)
    kid = parse_key(key).key_id
    assert c.post("/license/v1/register", json={"key": key, "machine_id": "old"}).status_code == 200
    # a 2nd device is blocked while 'old' holds the only seat
    assert c.post("/license/v1/register", json={"key": key, "machine_id": "new"}).status_code == 402
    # age 'old' past the reclaim window → its seat is auto-reclaimed on the next claim
    conn = reg.connect()
    conn.execute("UPDATE registrations SET last_seen=? WHERE key_id=? AND machine_id=?",
                 (time.time() - 10 * 3600, kid, "old"))
    conn.commit()
    conn.close()
    assert c.post("/license/v1/register", json={"key": key, "machine_id": "new"}).status_code == 200
    conn = reg.connect()
    assert reg.active_seat_count(conn, kid) == 1        # 'old' gone, only 'new' holds a seat
    conn.close()


def test_claim_seat_caps_reclaims_and_is_idempotent():
    conn = reg.connect()
    lic = parse_key(_key(plan="team", seats=2))
    t0 = 1_000_000.0
    reg.claim_seat(conn, lic, "d1", now=t0)
    reg.claim_seat(conn, lic, "d1", now=t0 + 5)          # idempotent refresh, still 1 seat
    reg.claim_seat(conn, lic, "d2", now=t0 + 5)
    assert reg.active_seat_count(conn, lic.key_id) == 2
    with pytest.raises(LicenseError, match="seat"):
        reg.claim_seat(conn, lic, "d3", now=t0 + 5)      # cap full of live devices
    # refresh d2 mid-window; let d1 go idle past the reclaim window
    mid = t0 + reg.seat_reclaim_seconds() / 2
    reg.claim_seat(conn, lic, "d2", now=mid)
    later = t0 + reg.seat_reclaim_seconds() + 100
    reg.claim_seat(conn, lic, "d3", now=later)           # d1 reclaimed (idle), d3 fits
    assert reg.active_seat_count(conn, lic.key_id) == 2  # d2 (live) + d3
    with pytest.raises(LicenseError, match="seat"):
        reg.claim_seat(conn, lic, "d4", now=later)       # cap still enforced after reclaim
    conn.close()


def test_release_seat_frees_slot():
    conn = reg.connect()
    lic = parse_key(_key(plan="team", seats=1))
    reg.claim_seat(conn, lic, "d1")
    assert reg.release_seat(conn, lic.key_id, "d1") is True
    assert reg.release_seat(conn, lic.key_id, "d1") is False   # already gone
    reg.claim_seat(conn, lic, "d2")                            # slot free again
    assert reg.active_seat_count(conn, lic.key_id) == 1
    conn.close()


def test_seat_cap_holds_under_concurrent_claims():
    """Regression for the check-then-insert race: many devices claim at once against a
    file-backed DB; the atomic BEGIN IMMEDIATE path must grant exactly `seats`, never more,
    and never surface a 'database is locked' error (busy_timeout serializes writers)."""
    import threading
    # Pre-create schema/WAL before the barrier. Otherwise a cold SQLite connection can do
    # journal setup while other workers are already waiting, turning a timing regression
    # into a hung test instead of a failure.
    reg.connect().close()
    lic = parse_key(_key(plan="team", seats=3))
    n = 12
    barrier = threading.Barrier(n)
    results = [None] * n

    def worker(i):
        conn = reg.connect()                       # each thread its own connection
        try:
            barrier.wait(timeout=10)               # release all claimants simultaneously
            reg.claim_seat(conn, lic, "dev-%d" % i)
            results[i] = "ok"
        except LicenseError:
            results[i] = "denied"
        except Exception as exc:                   # e.g. sqlite 'database is locked'
            results[i] = "err:%r" % exc
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(n)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=20)

    assert not any(th.is_alive() for th in threads), results
    assert not any(str(r).startswith("err:") for r in results), results
    assert results.count("ok") == 3, results       # exactly the cap, never overshoot
    conn = reg.connect()
    try:
        assert reg.active_seat_count(conn, lic.key_id) == 3
    finally:
        conn.close()


# ── per-key server-side enforcement (enforce: "cloud" in the signed payload) ───────────

def _enforced_key(cloud_url="", plan="pro"):
    now = time.time()
    return licensing.compose_key(
        {"v": 1, "plan": plan, "email": "b@x.co", "seats": 1, "issued": int(now),
         "expires": int(now + 30 * 86400), "enforce": "cloud", "cloud_url": cloud_url},
        SECRET)


def test_cloud_enforced_key_fails_closed_without_server(monkeypatch):
    """A key carrying ``enforce: "cloud"`` must be useless offline: with no env URL and
    no URL baked into the key, verification DENIES (free tier) rather than falling back
    to offline mode — so unsetting ENGRAPHIS_CLOUD_URL can't dodge revocation/leases."""
    monkeypatch.setattr(cloud_license, "register", lambda *a, **k: None)
    monkeypatch.setenv("ENGRAPHIS_LICENSE_KEY", _enforced_key(cloud_url=""))
    got = licensing.current_license(refresh=True)
    assert got.plan == "free"
    assert licensing.license_error()


def test_cloud_enforced_key_uses_baked_in_url(monkeypatch):
    """The signed-in cloud_url drives lease registration with no env var set; a valid
    lease from that server unlocks the plan. The URL is inside the Ed25519-signed
    payload, so pointing the client elsewhere means re-signing — i.e. it's vendor-only."""
    key = _enforced_key(cloud_url="https://lic.example")
    lic_parsed = parse_key(key)
    calls = {}

    def fake_register(base, k, mid, **kw):
        calls["base"] = base
        payload = {"v": 1, "key_id": lic_parsed.key_id, "plan": lic_parsed.plan,
                   "features": sorted(lic_parsed.features), "machine_id": mid,
                   "issued": int(time.time()), "expires": int(time.time() + 3600)}
        return cloud_license.compose_lease(payload, SECRET)

    monkeypatch.setattr(cloud_license, "register", fake_register)
    monkeypatch.setenv("ENGRAPHIS_LICENSE_KEY", key)
    got = licensing.current_license(refresh=True)
    assert calls["base"] == "https://lic.example"
    assert got.plan == "pro" and got.has("sync")


def test_retired_baked_in_url_migrates_to_current_relay(monkeypatch):
    """Existing signed keys must survive the vendor's Railway-to-domain migration."""
    key = _enforced_key(cloud_url="https://engraphis-production.up.railway.app")
    lic_parsed = parse_key(key)
    calls = {}

    def fake_register(base, k, mid, **kw):
        calls["base"] = base
        payload = {"v": 1, "key_id": lic_parsed.key_id, "plan": lic_parsed.plan,
                   "features": sorted(lic_parsed.features), "machine_id": mid,
                   "issued": int(time.time()), "expires": int(time.time() + 3600)}
        return cloud_license.compose_lease(payload, SECRET)

    monkeypatch.setattr(cloud_license, "register", fake_register)
    monkeypatch.setenv("ENGRAPHIS_LICENSE_KEY", key)
    got = licensing.current_license(refresh=True)
    assert calls["base"] == DEFAULT_RELAY_URL == "https://team.engraphis.com"
    assert got.plan == "pro" and got.has("sync")


def test_all_paid_keys_require_server_even_without_enforce_claim(monkeypatch):
    """Online-only (closes the offline-key bypass): even a key WITHOUT the enforce claim
    (old "offline" style) must obtain a live lease. Server unreachable → fail closed;
    a valid lease → unlocked. There is no offline pass-through anymore."""
    key = _key()                                             # no enforce / cloud_url
    monkeypatch.setenv("ENGRAPHIS_LICENSE_KEY", key)
    monkeypatch.setattr(cloud_license, "register", lambda *a, **k: None)   # unreachable
    assert licensing.current_license(refresh=True).plan == "free"
    lic_parsed = parse_key(key)                              # now the server issues a lease
    def ok_register(base, k, mid, **kw):
        payload = {"v": 1, "key_id": lic_parsed.key_id, "plan": lic_parsed.plan,
                   "features": sorted(lic_parsed.features), "machine_id": mid,
                   "issued": int(time.time()), "expires": int(time.time() + 3600)}
        return cloud_license.compose_lease(payload, SECRET)
    monkeypatch.setattr(cloud_license, "register", ok_register)
    got = licensing.current_license(refresh=True)
    assert got.plan == "pro" and got.has("sync")


def test_configured_key_retries_after_free_fallback_cache(monkeypatch):
    """A transient outage must not pin a valid configured key to free forever."""
    key = _key()
    lic_parsed = parse_key(key)
    monkeypatch.setenv("ENGRAPHIS_LICENSE_KEY", key)
    monkeypatch.setattr(cloud_license, "register", lambda *a, **k: None)
    assert licensing.current_license(refresh=True).plan == "free"
    assert licensing._cache_recheck_at != float("inf")

    def ok_register(base, k, mid, **kw):
        payload = {"v": 1, "key_id": lic_parsed.key_id, "plan": lic_parsed.plan,
                   "features": sorted(lic_parsed.features), "machine_id": mid,
                   "issued": int(time.time()), "expires": int(time.time() + 3600)}
        return cloud_license.compose_lease(payload, SECRET)

    monkeypatch.setattr(cloud_license, "register", ok_register)
    monkeypatch.setattr(licensing, "_cache_recheck_at", 0)
    assert licensing.current_license().plan == "pro"


# ── team-invite relay: self-hosted dashboards with no mail account of their own ────────
# borrow the vendor's, gated by a real 'team' key (same trust boundary as every other
# licensed feature) and rate-limited per key so it can't become an open relay.

def test_team_invite_relay_sends_with_valid_team_key(monkeypatch):
    from engraphis.inspector import webhooks as WH
    captured = {}
    monkeypatch.setattr(
        WH, "send_team_invite_email",
        lambda to, name, role, invited_by="", key="", dashboard_url=None:
            captured.update(to=to, name=name, role=role, invited_by=invited_by,
                            key=key, dashboard_url=dashboard_url))
    team_key = _key(plan="team", seats=3)
    c = _app()
    r = c.post("/license/v1/team-invite",
               json={"key": team_key, "to": "new@corp.com",
                     "name": "Mo", "role": "member", "invited_by": "admin@corp.com"})
    assert r.status_code == 200 and r.json()["sent"] is True
    # The relay now echoes the just-verified Team key into the email so the member
    # can activate Pro on their own machine; dashboard_url is "" when the caller
    # does not supply one.
    assert captured["to"] == "new@corp.com" and captured["invited_by"] == "admin@corp.com"
    assert captured["key"] == team_key
    assert captured["dashboard_url"] == ""


def test_team_invite_relay_rejects_non_team_key():
    c = _app()
    r = c.post("/license/v1/team-invite",
               json={"key": _key(plan="pro"), "to": "new@corp.com"})
    assert r.status_code == 402 and "team" in r.json()["error"].lower()


def test_team_invite_relay_rejects_revoked_key(monkeypatch):
    from engraphis.inspector import webhooks as WH
    monkeypatch.setattr(WH, "send_team_invite_email", lambda *a, **k: None)
    c = _app()
    key = _key(plan="team")
    reg.record_issued(key)                      # must be a known row for revoke to apply
    reg.revoke(parse_key(key).key_id)
    r = c.post("/license/v1/team-invite", json={"key": key, "to": "new@corp.com"})
    assert r.status_code == 402


def test_team_invite_relay_rejects_invalid_recipient_email():
    c = _app()
    r = c.post("/license/v1/team-invite",
               json={"key": _key(plan="team"), "to": "not-an-email"})
    assert r.status_code == 400


def test_team_invite_relay_ignores_malformed_invited_by(monkeypatch):
    from engraphis.inspector import webhooks as WH
    captured = {}
    monkeypatch.setattr(
        WH, "send_team_invite_email",
        lambda to, name, role, invited_by="", key="", dashboard_url=None:
            captured.update(invited_by=invited_by))
    c = _app()
    r = c.post("/license/v1/team-invite",
               json={"key": _key(plan="team"), "to": "new@corp.com",
                     "invited_by": "garbage"})
    assert r.status_code == 200 and captured["invited_by"] == ""


def test_team_invite_relay_enforces_daily_cap_per_key(monkeypatch):
    from engraphis.inspector import license_cloud
    from engraphis.inspector import webhooks as WH
    monkeypatch.setattr(WH, "send_team_invite_email", lambda *a, **k: None)
    monkeypatch.setattr(license_cloud, "_invite_daily_cap", lambda: 2)
    c = _app()
    key = _key(plan="team")
    for _ in range(2):
        r = c.post("/license/v1/team-invite", json={"key": key, "to": "new@corp.com"})
        assert r.status_code == 200
    over = c.post("/license/v1/team-invite", json={"key": key, "to": "new@corp.com"})
    assert over.status_code == 429 and "limit" in over.json()["error"].lower()
    # a DIFFERENT key is unaffected by another key's cap
    other = c.post("/license/v1/team-invite",
                   json={"key": _key(plan="team", email="other@corp.com"),
                         "to": "new@corp.com"})
    assert other.status_code == 200


def test_team_invite_relay_surfaces_delivery_failure_as_502(monkeypatch):
    from engraphis.inspector import webhooks as WH

    def boom(*a, **k):
        raise RuntimeError("simulated Resend outage")

    monkeypatch.setattr(WH, "send_team_invite_email", boom)
    c = _app()
    r = c.post("/license/v1/team-invite",
               json={"key": _key(plan="team"), "to": "new@corp.com"})
    assert r.status_code == 502


# ── team-invite relay: client function, end-to-end against the real endpoint ───────────

def _wire_urlopen_to(client, monkeypatch):
    """Route the client function's urllib POST into the in-process TestClient — proves
    the request cloud_license.send_team_invite actually builds is one the real endpoint
    accepts, not just what a mock expects."""
    class _Resp:
        def __init__(self, data): self._d = data
        def read(self): return self._d
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=None):
        path = urllib.parse.urlsplit(req.full_url).path
        resp = client.post(path, content=req.data or b"", headers=dict(req.headers))
        if resp.status_code >= 400:
            raise urllib.error.HTTPError(req.full_url, resp.status_code, resp.text,
                                         None, io.BytesIO(resp.content))
        return _Resp(resp.content)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)


def test_send_team_invite_client_roundtrip(monkeypatch):
    from engraphis.inspector import webhooks as WH
    captured = {}
    monkeypatch.setattr(
        WH, "send_team_invite_email",
        lambda to, name, role, invited_by="", key="", dashboard_url=None:
            captured.update(to=to))
    c = _app()
    _wire_urlopen_to(c, monkeypatch)
    sent, reason = cloud_license.send_team_invite(
        "http://relay.test", _key(plan="team"), "new@corp.com", "Mo", "member",
        "admin@corp.com")
    assert sent is True and reason == ""
    assert captured["to"] == "new@corp.com"


def test_send_team_invite_client_reports_reason_on_402(monkeypatch):
    c = _app()
    _wire_urlopen_to(c, monkeypatch)
    sent, reason = cloud_license.send_team_invite(
        "http://relay.test", _key(plan="pro"), "new@corp.com", "Mo", "member", "a@b.com")
    assert sent is False and "team" in reason.lower()


def test_send_team_invite_client_fails_closed_on_network_error(monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    sent, reason = cloud_license.send_team_invite(
        "http://relay.test", _key(plan="team"), "new@corp.com", "Mo", "member", "a@b.com")
    assert sent is False and "unreachable" in reason.lower()


# ── self-serve Team trial: real signed key, one-per-device, must work with the ─────────
# team-invite relay above (that's the whole point — a trial user needs the "click
# button, send invite" experience to actually work, or they never see the value).
#
# 2026-07-14 hardening: POST /start-trial no longer hands back a key synchronously — it
# emails a one-time magic link, and GET /start-trial/verify (opened from that email)
# mints the key. Tests below mock the outbound send (no real SMTP in CI) and drive the
# link explicitly, same as a user clicking it, via the two helpers immediately below.

def _capture_verify_url(monkeypatch):
    """Stub outbound trial-verification email (no real SMTP/Resend in tests); returns
    a dict populated with the last send's ``to``/``url``/``plan`` on each POST."""
    from engraphis.inspector import webhooks as WH
    captured: dict = {}

    def _fake_send(to, verify_url, plan="team", *, minutes=30):
        captured.update(to=to, url=verify_url, plan=plan)

    monkeypatch.setattr(WH, "send_trial_verification_email", _fake_send)
    return captured


def _token_from_url(url: str) -> str:
    return urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["token"][0]


def _key_from_verify_html(html: str) -> str:
    m = re.search(r"<pre[^>]*>([^<]+)</pre>", html)
    assert m, "no key found in verify-page HTML: %r" % html[:300]
    return m.group(1).strip()


def _start_and_confirm(c, captured, machine_id, email="dev@example.com", plan="team"):
    """POST /start-trial then immediately follow the (captured, mocked) magic link —
    the full happy path a real user drives by hand. Returns the confirmed key."""
    r = c.post("/license/v1/start-trial",
               json={"machine_id": machine_id, "email": email, "plan": plan})
    assert r.status_code == 200 and r.json().get("pending") is True
    token = _token_from_url(captured["url"])
    v = c.get("/license/v1/start-trial/verify", params={"token": token})
    assert v.status_code == 200, v.text
    return _key_from_verify_html(v.text)


def test_trial_signing_failure_does_not_consume_magic_link(monkeypatch):
    from engraphis.inspector import webhooks as WH

    c = _app()
    captured = _capture_verify_url(monkeypatch)
    assert c.post("/license/v1/start-trial", json={
        "machine_id": "dev-signing-retry",
        "email": "retry@example.com",
        "plan": "team",
    }).status_code == 200
    token = _token_from_url(captured["url"])
    issue_key = WH.issue_key
    monkeypatch.setattr(
        WH, "issue_key",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("signing failed")))

    with pytest.raises(RuntimeError, match="signing failed"):
        c.get("/license/v1/start-trial/verify", params={"token": token})

    monkeypatch.setattr(WH, "issue_key", issue_key)
    retry = c.get("/license/v1/start-trial/verify", params={"token": token})
    assert retry.status_code == 200
    assert parse_key(_key_from_verify_html(retry.text)).is_trial is True


def test_start_team_trial_issues_signed_team_key(monkeypatch):
    c = _app()
    captured = _capture_verify_url(monkeypatch)
    key = _start_and_confirm(c, captured, "dev-1")
    lic = parse_key(key)
    # Free Team trial is always 5 seats for TRIAL_DAYS (3) — see TEAM_TRIAL_SEATS.
    assert lic.plan == "team" and lic.has("team") and lic.seats == 5
    assert lic.is_trial is True
    assert lic.expires and lic.expires > time.time()
    days_left = (lic.expires - time.time()) / 86400
    assert 2.9 < days_left <= 3.0


def test_start_team_trial_grants_five_seats_regardless_of_request_body(monkeypatch):
    """5 seats, unconditionally — a caller cannot request a different seat count.
    The endpoint doesn't even read a ``seats`` field, so spoofing one in the body
    must have zero effect on what gets issued."""
    c = _app()
    captured = _capture_verify_url(monkeypatch)
    r = c.post("/license/v1/start-trial",
               json={"machine_id": "dev-spoof", "email": "a@example.com",
                     "seats": 1, "plan": "team"})
    assert r.status_code == 200
    token = _token_from_url(captured["url"])
    key = _key_from_verify_html(
        c.get("/license/v1/start-trial/verify", params={"token": token}).text)
    assert parse_key(key).seats == 5

    r2 = c.post("/license/v1/start-trial",
                json={"machine_id": "dev-spoof-2", "email": "b@example.com",
                      "seats": 999, "plan": "team"})
    assert r2.status_code == 200
    token2 = _token_from_url(captured["url"])
    key2 = _key_from_verify_html(
        c.get("/license/v1/start-trial/verify", params={"token": token2}).text)
    assert parse_key(key2).seats == 5


def test_start_pro_trial_stays_single_seat(monkeypatch):
    """The 5-seat grant is Team-specific; a Pro trial via this same relay endpoint
    is unaffected."""
    c = _app()
    captured = _capture_verify_url(monkeypatch)
    key = _start_and_confirm(c, captured, "dev-pro", email="pro@example.com", plan="pro")
    lic = parse_key(key)
    assert lic.plan == "pro" and lic.seats == 1


def test_start_team_trial_requires_machine_id():
    c = _app()
    assert c.post("/license/v1/start-trial",
                  json={"email": "a@example.com"}).status_code == 400


def test_start_team_trial_requires_valid_email():
    c = _app()
    assert c.post("/license/v1/start-trial",
                  json={"machine_id": "dev-1"}).status_code == 400
    assert c.post("/license/v1/start-trial",
                  json={"machine_id": "dev-1", "email": "not-an-email"}).status_code == 400


def test_start_team_trial_resend_supersedes_earlier_unclicked_link(monkeypatch):
    """A second, unconfirmed /start-trial request for the same still-pending device is
    a resend, not a conflict — trial_grants isn't written until a link is opened, so
    there's nothing to 409 on yet. The superseded OLD link must stop working."""
    c = _app()
    captured = _capture_verify_url(monkeypatch)
    first = c.post("/license/v1/start-trial",
                   json={"machine_id": "dev-1", "email": "dev@example.com"})
    assert first.status_code == 200
    old_token = _token_from_url(captured["url"])

    second = c.post("/license/v1/start-trial",
                    json={"machine_id": "dev-1", "email": "dev@example.com"})
    assert second.status_code == 200
    new_token = _token_from_url(captured["url"])
    assert new_token != old_token

    stale = c.get("/license/v1/start-trial/verify", params={"token": old_token})
    assert stale.status_code == 400

    fresh = c.get("/license/v1/start-trial/verify", params={"token": new_token})
    assert fresh.status_code == 200


def test_start_team_trial_rejects_grant_after_device_already_confirmed(monkeypatch):
    c = _app()
    captured = _capture_verify_url(monkeypatch)
    key = _start_and_confirm(c, captured, "dev-1")
    assert key

    # a fresh /start-trial request for the SAME (now-granted) device is refused outright
    r2 = c.post("/license/v1/start-trial",
               json={"machine_id": "dev-1", "email": "dev@example.com"})
    assert r2.status_code == 409

    # a DIFFERENT device is unaffected
    r3 = c.post("/license/v1/start-trial",
               json={"machine_id": "dev-2", "email": "other@example.com"})
    assert r3.status_code == 200


def test_start_team_trial_verify_rejects_unknown_token():
    c = _app()
    r = c.get("/license/v1/start-trial/verify", params={"token": "not-a-real-token"})
    assert r.status_code == 400


def test_start_team_trial_verify_link_is_one_time_use(monkeypatch):
    c = _app()
    captured = _capture_verify_url(monkeypatch)
    c.post("/license/v1/start-trial",
          json={"machine_id": "dev-1", "email": "dev@example.com"})
    token = _token_from_url(captured["url"])
    first = c.get("/license/v1/start-trial/verify", params={"token": token})
    assert first.status_code == 200
    replay = c.get("/license/v1/start-trial/verify", params={"token": token})
    assert replay.status_code == 400


def test_start_team_trial_verify_rejects_expired_token(monkeypatch):
    c = _app()
    captured = _capture_verify_url(monkeypatch)
    monkeypatch.setattr(license_cloud, "_TRIAL_TOKEN_TTL_SECONDS", -1)  # already expired
    r = c.post("/license/v1/start-trial",
               json={"machine_id": "dev-1", "email": "dev@example.com"})
    assert r.status_code == 200
    token = _token_from_url(captured["url"])
    v = c.get("/license/v1/start-trial/verify", params={"token": token})
    assert v.status_code == 400
    assert "expired" in v.text.lower()


def test_start_team_trial_surfaces_email_delivery_failure_as_502(monkeypatch):
    from engraphis.inspector import webhooks as WH

    def boom(*a, **k):
        raise RuntimeError("simulated Resend outage")

    monkeypatch.setattr(WH, "send_trial_verification_email", boom)
    c = _app()
    r = c.post("/license/v1/start-trial",
               json={"machine_id": "dev-1", "email": "dev@example.com"})
    assert r.status_code == 502


@pytest.mark.parametrize("trusted_peers", ["*", "testclient, 127.0.0.1"])
def test_start_team_trial_rate_limits_by_trusted_forwarded_source(monkeypatch, trusted_peers):
    """Trusted proxies may partition the rate limit by the forwarded client address."""
    monkeypatch.setenv("ENGRAPHIS_FORWARDED_ALLOW_IPS", trusted_peers)
    monkeypatch.setattr(license_cloud, "_trial_rate_limit_per_hour", lambda: 2)
    c = _app()
    _capture_verify_url(monkeypatch)
    headers = {"X-Forwarded-For": "203.0.113.9"}
    for i in range(2):
        r = c.post("/license/v1/start-trial",
                   json={"machine_id": "dev-%d" % i, "email": "dev%d@example.com" % i},
                   headers=headers)
        assert r.status_code == 200
    over = c.post("/license/v1/start-trial",
                  json={"machine_id": "dev-over", "email": "over@example.com"},
                  headers=headers)
    assert over.status_code == 429

    other = c.post("/license/v1/start-trial",
                   json={"machine_id": "dev-other", "email": "other@example.com"},
                   headers={"X-Forwarded-For": "198.51.100.4"})
    assert other.status_code == 200


def test_start_team_trial_ignores_forwarded_source_from_untrusted_peer(monkeypatch):
    """Spoofing X-Forwarded-For cannot evade a direct-peer rate limit."""
    monkeypatch.setattr(license_cloud, "_trial_rate_limit_per_hour", lambda: 2)
    c = _app()
    _capture_verify_url(monkeypatch)
    for i, spoofed in enumerate(("203.0.113.1", "203.0.113.2")):
        r = c.post(
            "/license/v1/start-trial",
            json={"machine_id": "dev-%d" % i, "email": "dev%d@example.com" % i},
            headers={"X-Forwarded-For": spoofed},
        )
        assert r.status_code == 200
    over = c.post(
        "/license/v1/start-trial",
        json={"machine_id": "dev-over", "email": "over@example.com"},
        headers={"X-Forwarded-For": "203.0.113.3"},
    )
    assert over.status_code == 429


def test_start_team_trial_key_actually_works_with_team_invite_relay(monkeypatch):
    """The regression this whole feature exists for: a trial key must be usable
    everywhere a purchased key is, specifically including the invite relay — an
    offline/local-only trial claim never could be."""
    from engraphis.inspector import webhooks as WH
    captured_invite = {}
    monkeypatch.setattr(
        WH, "send_team_invite_email",
        lambda to, name, role, invited_by="", key="", dashboard_url=None:
            captured_invite.update(to=to))
    c = _app()
    captured = _capture_verify_url(monkeypatch)
    key = _start_and_confirm(c, captured, "dev-1")
    r = c.post("/license/v1/team-invite", json={"key": key, "to": "teammate@corp.com"})
    assert r.status_code == 200 and r.json()["sent"] is True
    assert captured_invite["to"] == "teammate@corp.com"


def test_request_team_trial_key_client_returns_pending(monkeypatch):
    c = _app()
    _capture_verify_url(monkeypatch)
    _wire_urlopen_to(c, monkeypatch)
    key, reason, pending = cloud_license.request_team_trial_key(
        "http://relay.test", "dev-1", email="dev@example.com")
    assert key is None and pending is True and reason


def test_request_team_trial_key_client_reports_already_used(monkeypatch):
    c = _app()
    captured = _capture_verify_url(monkeypatch)
    _wire_urlopen_to(c, monkeypatch)
    cloud_license.request_team_trial_key("http://relay.test", "dev-1", email="dev@example.com")
    token = _token_from_url(captured["url"])
    confirmed = c.get("/license/v1/start-trial/verify", params={"token": token})
    assert confirmed.status_code == 200                 # the device now holds a grant
    key, reason, pending = cloud_license.request_team_trial_key(
        "http://relay.test", "dev-1", email="dev@example.com")
    assert key is None and pending is False
    assert "already been used" in reason


# ── licensing.start_team_trial: the client-facing entry point ──────────────────────────

def test_licensing_start_team_trial_activates_returned_key(monkeypatch):
    trial_key = _key(plan="team", email="trial@engraphis.local")
    monkeypatch.setattr(
        cloud_license, "request_team_trial_key",
        lambda base, mid, email="": (trial_key, "", False))
    c = _app()
    _wire_register_to(c, monkeypatch)            # online-only: lease the key
    monkeypatch.setenv("ENGRAPHIS_CLOUD_URL", "http://cloud.test")
    got = licensing.start_team_trial(email="trial@engraphis.local")
    assert got["plan"] == "team"
    assert licensing.current_license(refresh=True).plan == "team"
    assert licensing.has_feature("team") is True


def test_licensing_start_team_trial_refuses_if_paid_key_already_active(monkeypatch):
    """Same reasoning as test_start_trial_refuses_if_paid_key_already_active: only
    refuse when the cloud gate actually approves the existing key."""
    c = _app()
    _wire_register_to(c, monkeypatch)
    monkeypatch.setenv("ENGRAPHIS_CLOUD_URL", "http://cloud.test")
    monkeypatch.setenv("ENGRAPHIS_LICENSE_KEY", _key(plan="pro"))
    with pytest.raises(LicenseError, match="no trial needed"):
        licensing.start_team_trial()


def test_licensing_start_team_trial_proceeds_when_local_key_is_cloud_denied(monkeypatch):
    """Team-trial counterpart of test_start_trial_proceeds_when_local_key_is_cloud_denied
    — same 2026-07-13 incident, same fix, in start_team_trial()."""
    stale_key = _key(plan="pro")
    c = _app()

    def fake_register(base, key, mid, timeout=6.0):
        if key == stale_key:
            return None
        r = c.post("/license/v1/register", json={"key": key, "machine_id": mid})
        return r.json().get("lease") if r.status_code == 200 else None

    monkeypatch.setattr(cloud_license, "register", fake_register)
    monkeypatch.setenv("ENGRAPHIS_CLOUD_URL", "http://cloud.test")
    licensing.activate(stale_key)
    assert licensing.current_license(refresh=True).plan == "free"
    assert licensing.has_feature("team") is False

    trial_key = _key(plan="team", email="trial@engraphis.local")
    monkeypatch.setattr(
        cloud_license, "request_team_trial_key",
        lambda base, mid, email="": (trial_key, "", False))
    out = licensing.start_team_trial(email="trial@engraphis.local")  # must NOT raise "already active"
    assert out["plan"] == "team"
    assert licensing.has_feature("team") is True


def test_licensing_start_team_trial_is_idempotent_while_already_on_trial(monkeypatch):
    """Same regression as the Pro trial: re-calling while already on an active Team
    trial must no-op (200/current status), not 400 with 'no trial needed'."""
    now = time.time()
    team_trial = licensing.compose_key(
        {"v": 1, "plan": "team", "email": "trial@engraphis.local", "seats": 5,
         "issued": int(now), "expires": int(now + 3 * 86400), "trial": 1}, SECRET)
    calls = []

    def _request(base, mid, email=""):
        calls.append(1)
        return team_trial, "", False

    monkeypatch.setattr(cloud_license, "request_team_trial_key", _request)
    c = _app()
    _wire_register_to(c, monkeypatch)
    monkeypatch.setenv("ENGRAPHIS_CLOUD_URL", "http://cloud.test")
    licensing.start_team_trial(email="trial@engraphis.local")
    assert len(calls) == 1
    out = licensing.start_team_trial(email="trial@engraphis.local")
    assert out["plan"] == "team" and out["is_trial"] is True
    assert len(calls) == 1          # no second relay round-trip


def test_licensing_start_team_trial_surfaces_relay_denial(monkeypatch):
    monkeypatch.setattr(
        cloud_license, "request_team_trial_key",
        lambda base, mid, email="": (None, "the free Team trial has already been used", False))
    with pytest.raises(LicenseError, match="already been used"):
        licensing.start_team_trial(email="trial@engraphis.local")


def test_licensing_start_trial_requires_email(monkeypatch):
    """The 2026-07-14 hardening: a bare call with no email must fail locally, fast,
    without ever reaching the relay (machine_id alone is no longer sufficient)."""
    called = []
    monkeypatch.setattr(
        cloud_license, "request_trial_key",
        lambda *a, **k: called.append(1) or (None, "should not be called", False))
    with pytest.raises(LicenseError, match="email"):
        licensing.start_trial()
    with pytest.raises(LicenseError, match="email"):
        licensing.start_trial(email="not-an-email")
    assert not called


def test_licensing_start_trial_surfaces_pending_status(monkeypatch):
    """The normal successful outcome of licensing.start_trial() is now 'pending' — no
    key, nothing activated — since the relay only emails a magic link. Regression
    guard against silently treating a pending response as an activated license."""
    monkeypatch.setattr(
        cloud_license, "request_trial_key",
        lambda base, mid, plan="pro", email="":
            (None, "check your email to confirm and activate the trial", True))
    out = licensing.start_trial(email="me@example.com")
    assert out == {"pending": True,
                   "message": "check your email to confirm and activate the trial"}
    assert licensing.current_license(refresh=True).plan == "free"  # nothing activated yet
