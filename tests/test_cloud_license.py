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
    lic = parse_key(_key(plan="team", seats=3))
    n = 12
    barrier = threading.Barrier(n)
    results = [None] * n

    def worker(i):
        conn = reg.connect()                       # each thread its own connection
        try:
            barrier.wait()                         # release all claimants simultaneously
            reg.claim_seat(conn, lic, "dev-%d" % i)
            results[i] = "ok"
        except LicenseError:
            results[i] = "denied"
        except Exception as exc:                   # e.g. sqlite 'database is locked'
            results[i] = "err:%r" % exc
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

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
    monkeypatch.setenv("ENGRAPHIS_LICENSE_KEY", _enforced_key(cloud_url=""))
    got = licensing.current_license(refresh=True)
    assert got.plan == "free"
    assert "server-side verification" in licensing.license_error()


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


def test_offline_keys_stay_offline(monkeypatch):
    """Back-compat: a key WITHOUT the enforce claim still verifies fully offline."""
    monkeypatch.setenv("ENGRAPHIS_LICENSE_KEY", _key())
    got = licensing.current_license(refresh=True)
    assert got.plan == "pro" and got.enforce == "" and licensing.license_error() == ""
