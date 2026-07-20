"""Cloud license enforcement: registration issues a machine-bound signed lease; revoked/
expired/seat-limited keys are refused; forged leases are rejected; and every paid key fails
closed without a valid lease. Also covers rejection of the retired local trial and monotonic
clock hardening. Runs on the numpy-only gate (stdlib + fastapi TestClient).
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
from engraphis.config import DEFAULT_LICENSE_SERVER_URL, settings
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
    monkeypatch.setenv("ENGRAPHIS_RELAY_PUBLIC_URL", "https://relay.example.test")
    monkeypatch.delenv("ENGRAPHIS_CLOUD_URL", raising=False)
    monkeypatch.delenv("ENGRAPHIS_LICENSE_KEY", raising=False)
    monkeypatch.delenv("ENGRAPHIS_FORWARDED_ALLOW_IPS", raising=False)
    monkeypatch.setattr(license_cloud, "REGISTER_RATE_PER_MINUTE", 10_000)
    license_cloud._REGISTER_BUCKETS.clear()
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
    assert payload["signing_key_id"] == ed25519_public_key(SECRET).hex()[:16]


@pytest.mark.parametrize("body", [
    [], {"key": 1, "machine_id": "m"}, {"key": "x", "machine_id": ["m"]},
    {"key": "x\nforged", "machine_id": "m"},
    {"key": "x", "machine_id": "m" * 201},
])
def test_register_rejects_malformed_or_unbounded_fields_without_crypto(body):
    assert _app().post("/license/v1/register", json=body).status_code == 400


def test_license_json_body_is_bounded_before_decode():
    response = _app().post(
        "/license/v1/register",
        content=b'{"key":"' + b"x" * license_cloud.MAX_JSON_BODY_BYTES + b'"}',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413


def test_license_json_body_rejects_excessive_nesting_without_500():
    nested = b"[" * 1100 + b"0" + b"]" * 1100
    response = _app().post(
        "/license/v1/register", content=nested,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400


def test_register_crypto_work_is_rate_limited(monkeypatch):
    monkeypatch.setattr(license_cloud, "REGISTER_RATE_PER_MINUTE", 2)
    license_cloud._REGISTER_BUCKETS.clear()
    c = _app()
    payload = {"key": "well-formed-but-invalid", "machine_id": "m"}
    assert c.post("/license/v1/register", json=payload).status_code == 402
    assert c.post("/license/v1/register", json=payload).status_code == 402
    limited = c.post("/license/v1/register", json=payload)
    assert limited.status_code == 429 and limited.headers["Retry-After"] == "60"


def test_public_verify_probe_is_rate_limited(monkeypatch):
    """The status probe was the last unauthenticated route here with no limit at all.

    It shares /register's burst budget — safe because nothing polls it (only
    scripts/smoke_cloud.py), so there is no legitimate high-frequency caller to starve."""
    monkeypatch.setattr(license_cloud, "REGISTER_RATE_PER_MINUTE", 2)
    license_cloud._REGISTER_BUCKETS.clear()
    c = _app()
    assert c.get("/license/v1/verify/deadbeef").status_code == 200
    assert c.get("/license/v1/verify/deadbeef").status_code == 200
    limited = c.get("/license/v1/verify/deadbeef")
    assert limited.status_code == 429 and limited.headers["Retry-After"] == "60"


def test_verify_probe_shares_the_register_budget(monkeypatch):
    """Alternating between the two routes must not buy extra work — same reasoning as
    the existing /register + /team-invite shared budget."""
    monkeypatch.setattr(license_cloud, "REGISTER_RATE_PER_MINUTE", 2)
    license_cloud._REGISTER_BUCKETS.clear()
    c = _app()
    assert c.get("/license/v1/verify/deadbeef").status_code == 200
    assert c.post("/license/v1/register",
                  json={"key": "well-formed-but-invalid", "machine_id": "m"}
                  ).status_code == 402
    assert c.get("/license/v1/verify/deadbeef").status_code == 429


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
    # Seat caps apply to TEAM (the seat-priced tier). Pro is the individual multi-device
    # tier and is intentionally NOT device-capped (see test_pro_register_is_not_device_capped
    # and sync_relay.py) — so this exercises the cap with a team key.
    c = _app()
    key = _key(plan="team", seats=1)
    assert c.post("/license/v1/register", json={"key": key, "machine_id": "A"}).status_code == 200
    # a second distinct machine exceeds the 1-seat cap
    over = c.post("/license/v1/register", json={"key": key, "machine_id": "B"})
    assert over.status_code == 402 and "seat" in over.json()["error"].lower()
    # the already-registered machine can always renew
    assert c.post("/license/v1/register", json={"key": key, "machine_id": "A"}).status_code == 200


def test_pro_register_is_not_device_capped():
    """Pro is the individual multi-device tier: one person's many devices all register
    under the same key. The register endpoint issues the online-enforcement lease that
    gates EVERY paid feature, so seat-capping Pro here would lock a paying customer's
    second device out of all Pro features — including the multi-device sync they bought.
    Mirrors test_pro_relay_is_not_device_capped for the register/lease path."""
    c = _app()
    key = _key(plan="pro", seats=1)                             # Pro keys are minted seats=1
    for m in ("p1", "p2", "p3"):
        r = c.post("/license/v1/register", json={"key": key, "machine_id": m})
        assert r.status_code == 200, r.text
        assert r.json()["plan"] == "pro"


# -- vendor-relayed password reset ----------------------------------------------------

def test_password_reset_relay_queues_once_and_pins_paid_origin(monkeypatch):
    from engraphis.inspector import webhooks as WH

    queued = []

    def enqueue(to, name, reset_url, *, idempotency_key):
        queued.append((to, name, reset_url, idempotency_key))
        return "eml_test"

    monkeypatch.setattr(WH, "queue_password_reset_email", enqueue)
    key = _key(plan="pro")
    body = {
        "key": key,
        "to": "Owner@Example.com",
        "name": "Owner",
        "reset_url": "https://team.customer.test/#reset_token=one-time-secret",
    }
    client = _app()
    first = client.post("/license/v1/password-reset", json=body)
    replay = client.post("/license/v1/password-reset", json=body)

    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json() == {"queued": True}
    assert len(queued) == 1
    assert queued[0][0] == "owner@example.com"
    assert queued[0][3].startswith("password-reset-relay:")
    assert "one-time-secret" not in first.text

    moved = dict(body, reset_url="https://other.customer.test/#reset_token=new-secret")
    denied = client.post("/license/v1/password-reset", json=moved)
    assert denied.status_code == 409
    assert "new-secret" not in denied.text


def test_password_reset_relay_accepts_canonical_dashboard_subpath(monkeypatch):
    from engraphis.inspector import webhooks as WH

    queued = []
    monkeypatch.setattr(
        WH, "queue_password_reset_email",
        lambda *args, **kwargs: queued.append((args, kwargs)) or "eml_subpath",
    )
    response = _app().post("/license/v1/password-reset", json={
        "key": _key(plan="pro", email="subpath@corp.com"),
        "to": "subpath@corp.com",
        "name": "Subpath",
        "reset_url": "https://customer.example/memory/#reset_token=subpath-secret",
    })
    assert response.status_code == 200 and response.json() == {"queued": True}
    assert queued[0][0][2].startswith("https://customer.example/memory/#")


def test_password_reset_relay_requires_bound_trial_origin(monkeypatch):
    from engraphis.inspector import webhooks as WH

    # A free trial may only relay vendor-branded mail to the relay's OWN configured
    # dashboard origin (never a caller-attested one) — the operator sets that here.
    monkeypatch.setenv("ENGRAPHIS_DASHBOARD_URL", "https://trial.customer.test")
    queued = []
    monkeypatch.setattr(
        WH, "queue_password_reset_email",
        lambda *args, **kwargs: queued.append((args, kwargs)) or "eml_trial",
    )
    key = _trial_team_key()
    now = time.time()
    conn = reg.connect()
    try:
        conn.executescript(license_cloud._TRIAL_CLAIM_SCHEMA)
        conn.execute(
            "INSERT INTO trial_claims(claim_id,confirmation_hash,deployment_hash,"
            "machine_id,email,plan,dashboard_url,created_at,expires_at,confirmed_at,"
            "license_key) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("claim-test", "confirm-test", "deploy-test", "machine-test",
             "trial@corp.com", "team", "https://trial.customer.test", now,
             now + 1800, now, key),
        )
        conn.commit()
    finally:
        conn.close()

    good = _app().post("/license/v1/password-reset", json={
        "key": key, "to": "owner@example.com", "name": "Owner",
        "reset_url": "https://trial.customer.test/#reset_token=trial-secret",
    })
    assert good.status_code == 200 and good.json() == {"queued": True}
    assert len(queued) == 1


def test_password_reset_relay_survives_provider_outage_as_pending_outbox(monkeypatch):
    from engraphis import email_outbox

    monkeypatch.delenv("ENGRAPHIS_RESEND_API_KEY", raising=False)
    monkeypatch.delenv("ENGRAPHIS_SMTP_HOST", raising=False)
    response = _app().post("/license/v1/password-reset", json={
        "key": _key(plan="team"), "to": "owner@example.com", "name": "Owner",
        "reset_url": "https://outage.customer.test/#reset_token=pending-secret",
    })

    assert response.status_code == 200 and response.json() == {"queued": True}
    assert "pending-secret" not in response.text
    assert email_outbox.health()["backlog"] == 1


@pytest.mark.parametrize("reset_url", [
    "https://team.customer.test/reset",
    "https://team.customer.test/reset#reset_token=x",
    "https://team.customer.test/#reset_token=",
    "https://team.customer.test/#reset_token=x&next=https://evil.test",
    "https://team.customer.test/?reset_token=query-secret",
    "https://team.customer.test/?next=x#reset_token=fragment-secret",
    "https://team.customer.test/#reset_token=x%2Dy",
    "https://user:pass@team.customer.test/#reset_token=x",
])
def test_password_reset_relay_rejects_unsafe_reset_urls(reset_url):
    response = _app().post("/license/v1/password-reset", json={
        "key": _key(), "to": "owner@example.com", "reset_url": reset_url,
    })
    assert response.status_code == 400
    assert "reset_token" not in response.text


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
    # The per-instance service credential must NOT open vendor-wide admin routes: the
    # ENGRAPHIS_API_TOKEN fallback was removed 2026-07-18 (audit finding M5).
    monkeypatch.setattr(settings, "api_token", "adm1n")
    monkeypatch.delenv("ENGRAPHIS_VENDOR_ADMIN_TOKEN", raising=False)
    denied = c.post("/license/v1/revoke/%s" % kid, headers={"Authorization": "Bearer adm1n"})
    assert denied.status_code == 401 and reg.is_revoked(kid) is False
    monkeypatch.setenv(
        "ENGRAPHIS_VENDOR_ADMIN_TOKEN", "vendor-admin-token-at-least-32-characters")
    ok = c.post("/license/v1/revoke/%s" % kid,
                headers={"Authorization": (
                    "Bearer vendor-admin-token-at-least-32-characters")})
    assert ok.status_code == 200 and reg.is_revoked(kid) is True


def test_forged_lease_is_rejected():
    forged = cloud_license.compose_lease(
        {"v": 1, "key_id": "x", "plan": "pro", "features": ["sync"],
         "machine_id": "m", "issued": int(time.time()), "expires": int(time.time() + 9999)},
        b"\x09" * 32)                                        # attacker's own key
    with pytest.raises(LicenseError, match="signature"):
        cloud_license.verify_lease(forged)


def test_cached_lease_accepts_pinned_previous_signer_and_derives_legacy_id(monkeypatch):
    import json

    old_secret = bytes(reversed(range(32)))
    old_public = ed25519_public_key(old_secret)
    new_public = ed25519_public_key(SECRET)
    monkeypatch.setattr(licensing, "_TEST_MODE_PUBKEY_OVERRIDE", False)
    monkeypatch.setattr(licensing, "_VENDOR_PUBKEY_HEX", new_public.hex())
    monkeypatch.setattr(
        licensing, "_PREVIOUS_VENDOR_PUBKEY_HEXES", (old_public.hex(),))
    now = int(time.time())
    payload = {"v": 1, "key_id": "old", "plan": "pro", "features": ["sync"],
               "machine_id": "machine", "issued": now, "expires": now + 300}

    # Pre-v1 leases had no signed key id. The explicitly pinned previous verifier keeps
    # their outage-grace cache valid and supplies the verified id to callers.
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    legacy = "%s.%s.%s" % (
        cloud_license._LEASE_PREFIX, licensing._b64u_encode(body),
        licensing._b64u_encode(licensing.ed25519_sign(old_secret, body)))
    verified = cloud_license.verify_lease(legacy, now=now)
    assert verified["signing_key_id"] == old_public.hex()[:16]

    # Once the old verifier is removed, the same cached lease fails closed.
    monkeypatch.setattr(licensing, "_PREVIOUS_VENDOR_PUBKEY_HEXES", ())
    with pytest.raises(LicenseError, match="signature"):
        cloud_license.verify_lease(legacy, now=now)


def test_lease_signing_key_id_must_match_verified_signer(monkeypatch):
    import json

    public = ed25519_public_key(SECRET)
    monkeypatch.setattr(licensing, "_TEST_MODE_PUBKEY_OVERRIDE", False)
    monkeypatch.setattr(licensing, "_VENDOR_PUBKEY_HEX", public.hex())
    monkeypatch.setattr(licensing, "_PREVIOUS_VENDOR_PUBKEY_HEXES", ())
    now = int(time.time())
    payload = {"v": 1, "key_id": "bad-kid", "plan": "pro", "features": ["sync"],
               "machine_id": "machine", "issued": now, "expires": now + 300,
               "signing_key_id": "0" * 16}
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    lease = "%s.%s.%s" % (
        cloud_license._LEASE_PREFIX, licensing._b64u_encode(body),
        licensing._b64u_encode(licensing.ed25519_sign(SECRET, body)))
    with pytest.raises(LicenseError, match="signing-key id"):
        cloud_license.verify_lease(lease, now=now)


def test_expired_lease_is_rejected():
    expired = cloud_license.compose_lease(
        {"v": 1, "key_id": "x", "plan": "pro", "features": ["sync"],
         "machine_id": "m", "issued": 0, "expires": int(time.time() - 10)}, SECRET)
    with pytest.raises(LicenseError, match="expired"):
        cloud_license.verify_lease(expired)


@pytest.mark.parametrize("expires", [float("nan"), float("inf"), "inf", "NaN"])
def test_non_finite_lease_expiry_is_rejected(expires):
    """A lease whose ``expires`` is NaN or Infinity must NOT verify.

    Both `now > nan` and `now > inf` evaluate to False, so a non-finite expiry would
    sail straight past the expiry comparison and yield a lease that never expires — the
    one fail-OPEN in verify_lease. json.loads accepts a bare `NaN`/`Infinity` literal by
    default and float() accepts the string forms, so both are reachable from a payload."""
    lease = cloud_license.compose_lease(
        {"v": 1, "key_id": "x", "plan": "pro", "features": ["sync"],
         "machine_id": "m", "issued": 0, "expires": expires}, SECRET)
    with pytest.raises(LicenseError, match="finite"):
        cloud_license.verify_lease(lease)


@pytest.mark.parametrize("payload", [5, None, [], "lease"])
def test_non_object_lease_payload_is_rejected(payload):
    """A correctly-signed body that decodes to valid-but-non-dict JSON must raise
    LicenseError, not AttributeError — verify_lease documents LicenseError, and a caller
    that catches it specifically (rather than bare Exception) would otherwise crash."""
    lease = cloud_license.compose_lease(payload, SECRET)
    with pytest.raises(LicenseError, match="JSON object"):
        cloud_license.verify_lease(lease)


def test_non_numeric_lease_expiry_is_rejected():
    lease = cloud_license.compose_lease(
        {"v": 1, "key_id": "x", "plan": "pro", "features": ["sync"],
         "machine_id": "m", "issued": 0, "expires": "whenever"}, SECRET)
    with pytest.raises(LicenseError, match="not a number"):
        cloud_license.verify_lease(lease)


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
    monkeypatch.setenv("ENGRAPHIS_CLOUD_URL", "http://127.0.0.1")
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
    monkeypatch.setenv("ENGRAPHIS_CLOUD_URL", "http://127.0.0.1")
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
    monkeypatch.setenv("ENGRAPHIS_CLOUD_URL", "http://127.0.0.1")
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
        cloud_license.register("http://127.0.0.1", _key(), "m-1")
    def _urlopen_5xx(req, timeout=None): raise _HTTPError(503)
    monkeypatch.setattr(cloud_license.urllib.request, "urlopen", _urlopen_5xx)
    assert cloud_license.register("http://127.0.0.1", _key(), "m-1") is None


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
    assert cloud_license.register("http://127.0.0.1", _key(), "m-1") is None
    assert captured == {
        "user_agent": "Engraphis/1.0 (+https://engraphis.com)",
        "accept": "application/json",
    }


@pytest.mark.parametrize("headers, expected", [
    ({"Retry-After": "60"}, "retry shortly"),
    ({}, "retry tomorrow"),
])
def test_team_invite_429_distinguishes_burst_gate_from_daily_cap(monkeypatch, headers,
                                                                 expected):
    """The relay returns 429 for two unrelated reasons and only one is a day-long wait.

    The per-IP burst gate in front of the Ed25519 verify sends Retry-After; the per-key
    daily cap does not. Collapsing both into "retry tomorrow" tells an admin who hit a
    60-second limit to give up for the day.
    """
    def raise_429(req, timeout=None):
        raise cloud_license.urllib.error.HTTPError(
            req.full_url, 429, "Too Many Requests", headers, None)

    monkeypatch.setattr(cloud_license.urllib.request, "urlopen", raise_429)
    sent, reason = cloud_license.send_team_invite(
        "https://relay.example", _key(plan="team"), "new@corp.com",
        "Mo", "member", "admin@corp.com",
    )
    assert sent is False
    assert expected in reason, reason


def test_license_clients_refuse_plain_http_off_loopback(monkeypatch):
    def unexpected_network(*args, **kwargs):
        raise AssertionError("insecure URL must be rejected before opening a connection")

    monkeypatch.setattr(cloud_license.urllib.request, "urlopen", unexpected_network)
    assert cloud_license.register("http://relay.example", _key(), "m-1") is None
    sent, reason = cloud_license.send_team_invite(
        "http://relay.example", _key(plan="team"), "new@corp.com",
        "Mo", "member", "admin@corp.com",
    )
    assert sent is False and "HTTPS" in reason
    key, reason, pending = cloud_license.request_team_trial_key(
        "http://relay.example", "m-1", email="new@corp.com"
    )
    assert key is None and pending is False and "HTTPS" in reason


def test_gate_rejects_invalid_server_url_even_with_cached_lease_and_redacts_it(monkeypatch):
    c = _app()
    _wire_register_to(c, monkeypatch)
    key = _key()
    lic = parse_key(key)
    assert cloud_license.gate(lic, key, base_url="http://127.0.0.1")[0] is True
    assert cloud_license._LEASE_FILE.exists()

    allowed, reason = cloud_license.gate(
        lic, key, base_url="https://user:super-secret@relay.example"
    )

    assert allowed is False
    assert "embedded credentials" in reason
    assert "super-secret" not in reason

    allowed, reason = cloud_license.gate(
        lic, key, base_url="https://relay.example:not-a-port"
    )
    assert allowed is False
    assert "invalid port" in reason


def test_revalidate_revoked_deletes_lease(monkeypatch):
    # A paid key with a valid local lease is periodically checked online. Background
    # revalidation uses the same denial path and deletes the lease immediately.
    c = _app()
    _wire_register_to(c, monkeypatch)
    monkeypatch.setenv("ENGRAPHIS_CLOUD_URL", "http://127.0.0.1")
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
    assert cloud_license.revalidate(lic, key, base_url="http://127.0.0.1") == "revoked"
    assert not cloud_license._LEASE_FILE.exists()
    assert cloud_license.gate(lic, key)[0] is False


def test_revalidate_offline_keeps_lease_grace(monkeypatch):
    # A paying customer briefly offline: revalidate can't reach the server → 'offline', and
    # the cached lease STAYS (offline grace), so paid features keep working.
    c = _app()
    _wire_register_to(c, monkeypatch)
    monkeypatch.setenv("ENGRAPHIS_CLOUD_URL", "http://127.0.0.1")
    key = _key()
    lic = parse_key(key)
    cloud_license.gate(lic, key)
    monkeypatch.setattr(cloud_license, "register", lambda *a, **k: None)
    assert cloud_license.revalidate(lic, key, base_url="http://127.0.0.1") == "offline"
    assert cloud_license._LEASE_FILE.exists()
    assert cloud_license.gate(lic, key)[0] is True


def test_revalidate_ok_refreshes_lease(monkeypatch):
    # An online, still-valid key: revalidate re-registers (refreshing the seat + lease) and
    # returns 'ok'. This is the steady state for a paying customer.
    c = _app()
    _wire_register_to(c, monkeypatch)
    monkeypatch.setenv("ENGRAPHIS_CLOUD_URL", "http://127.0.0.1")
    key = _key()
    lic = parse_key(key)
    cloud_license.gate(lic, key)
    assert cloud_license.revalidate(lic, key, base_url="http://127.0.0.1") == "ok"
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
    monkeypatch.setenv("ENGRAPHIS_CLOUD_URL", "http://127.0.0.1")
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
    monkeypatch.setenv("ENGRAPHIS_CLOUD_URL", "http://127.0.0.1")
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
    monkeypatch.setenv("ENGRAPHIS_CLOUD_URL", "http://127.0.0.1")
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
    monkeypatch.setenv("ENGRAPHIS_CLOUD_URL", "http://127.0.0.1")
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
    monkeypatch.setenv("ENGRAPHIS_CLOUD_URL", "http://127.0.0.1")
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
    monkeypatch.setenv("ENGRAPHIS_CLOUD_URL", "http://127.0.0.1")
    licensing.start_trial(email="trial@engraphis.local")
    lic = licensing.current_license(refresh=True)
    assert lic.is_trial and lic.plan == "pro"
    assert licensing.has_feature("team") is False


# ── admin operations: revoke-by-email, key lookup, device visibility, deactivate ───────

def _admin(monkeypatch):
    """Vendor admin credential. Deliberately NOT settings.api_token — those are separate
    secrets and the fallback between them was removed 2026-07-18 (audit finding M5)."""
    monkeypatch.setenv(
        "ENGRAPHIS_VENDOR_ADMIN_TOKEN", "vendor-admin-token-at-least-32-characters")
    return {"Authorization": "Bearer vendor-admin-token-at-least-32-characters"}


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


def test_vendor_json_routes_reject_non_object_or_non_string_fields(monkeypatch):
    c = _app()
    h = _admin(monkeypatch)
    assert c.post("/license/v1/revoke-by-email", json=[], headers=h).status_code == 400
    assert c.post(
        "/license/v1/deactivate",
        json={"key_id": ["bad"], "machine_id": "d1"},
        headers=h,
    ).status_code == 400


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
    assert calls["base"] == DEFAULT_LICENSE_SERVER_URL == "https://license.engraphis.com"
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
        WH, "queue_team_invite_email",
        lambda to, name, role, invited_by="", invite_url="", dashboard_url=None, **kwargs:
            captured.update(to=to, name=name, role=role, invited_by=invited_by,
                            invite_url=invite_url, dashboard_url=dashboard_url, **kwargs))
    team_key = _key(plan="team", seats=3)
    c = _app()
    r = c.post("/license/v1/team-invite",
               json={"key": team_key, "to": "new@corp.com",
                     "name": "Mo", "role": "member", "invited_by": "admin@corp.com",
                     "dashboard_url": "https://team.customer.test",
                     "invite_url":
                         "https://team.customer.test/#invite_token=one-time-secret"})
    assert r.status_code == 200 and r.json()["sent"] is True
    assert captured["to"] == "new@corp.com" and captured["invited_by"] == "admin@corp.com"
    assert "key" not in captured
    assert captured["invite_url"] == (
        "https://team.customer.test/#invite_token=one-time-secret")
    assert captured["dashboard_url"] == "https://team.customer.test"


def test_team_invite_relay_rejects_non_team_key():
    c = _app()
    r = c.post("/license/v1/team-invite",
               json={"key": _key(plan="pro"), "to": "new@corp.com"})
    assert r.status_code == 402 and "team" in r.json()["error"].lower()


def test_team_invite_relay_rejects_revoked_key(monkeypatch):
    from engraphis.inspector import webhooks as WH
    monkeypatch.setattr(WH, "queue_team_invite_email", lambda *a, **k: None)
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


def test_team_invite_relay_rejects_malformed_invited_by():
    c = _app()
    r = c.post("/license/v1/team-invite",
               json={"key": _key(plan="team"), "to": "new@corp.com",
                     "invited_by": "garbage"})
    assert r.status_code == 400


@pytest.mark.parametrize("field,value", [
    ("name", ["not", "text"]),
    ("role", "owner"),
    ("dashboard_url", "javascript:alert(1)"),
    ("dashboard_url", "https://user:pass@example.com"),
    ("invite_url", "https://team.example/?invite_token=once"),
    ("invite_url", "https://team.example/?invite_token=once&next=https://evil.test"),
    ("invite_url", "https://team.example/other#invite_token=once"),
    ("invite_url", "https://team.example/%2e%2e/#invite_token=once"),
    ("invite_url", "https://team.example/#invite_token=once&next=evil"),
    ("invite_url", "https://team.example/?next=evil#invite_token=once"),
    ("invite_url", "https://team.example/#invite_token=once%2Dencoded"),
])
def test_team_invite_relay_rejects_hostile_fields(field, value):
    body = {"key": _key(plan="team"), "to": "new@corp.com", field: value}
    assert _app().post("/license/v1/team-invite", json=body).status_code == 400


def test_team_invite_relay_accepts_canonical_dashboard_subpath(monkeypatch):
    from engraphis.inspector import webhooks as WH

    queued = []
    monkeypatch.setattr(
        WH, "queue_team_invite_email",
        lambda *args, **kwargs: queued.append((args, kwargs)) or "eml_subpath",
    )
    response = _app().post("/license/v1/team-invite", json={
        "key": _key(plan="team", email="subpath@corp.com"),
        "to": "new@corp.com",
        "dashboard_url": "https://customer.example/memory",
        "invite_url": "https://customer.example/memory/#invite_token=subpath-secret",
    })
    assert response.status_code == 200 and response.json()["queued"] is True
    assert queued[0][1]["dashboard_url"] == "https://customer.example/memory"


def test_team_invite_relay_enforces_daily_cap_per_key(monkeypatch):
    from engraphis.inspector import license_cloud
    from engraphis.inspector import webhooks as WH
    monkeypatch.setattr(WH, "queue_team_invite_email", lambda *a, **k: None)
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


def test_team_invite_relay_surfaces_queue_failure_as_502(monkeypatch):
    from engraphis.inspector import webhooks as WH
    from engraphis.inspector import license_cloud

    def boom(*a, **k):
        raise RuntimeError("api_key=RESEND_SECRET_123 C:/private/customer.db")

    monkeypatch.setattr(WH, "queue_team_invite_email", boom)
    monkeypatch.setattr(license_cloud, "_invite_daily_cap", lambda: 1)
    c = _app()
    key = _key(plan="team")
    r = c.post("/license/v1/team-invite",
               json={"key": key, "to": "new@corp.com"})
    assert r.status_code == 502
    assert "RESEND_SECRET_123" not in r.text and "private" not in r.text
    # A failed durable enqueue does not consume the accepted-message cap.
    monkeypatch.setattr(WH, "queue_team_invite_email", lambda *a, **k: None)
    retry = c.post("/license/v1/team-invite",
                   json={"key": key, "to": "new@corp.com"})
    assert retry.status_code == 200


def test_team_invite_request_retry_reuses_one_durable_outbox_operation(monkeypatch):
    from engraphis import email_outbox

    for name in (
        "ENGRAPHIS_RESEND_API_KEY", "ENGRAPHIS_SMTP_HOST", "ENGRAPHIS_SMTP_USER",
        "ENGRAPHIS_SMTP_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)
    c = _app()
    body = {"key": _key(plan="team"), "to": "new@corp.com", "role": "member"}

    first = c.post("/license/v1/team-invite", json=body)
    retry = c.post("/license/v1/team-invite", json=body)

    assert first.status_code == 200
    assert retry.status_code == 200
    conn = email_outbox._connect()
    try:
        rows = conn.execute(
            "SELECT idempotency_key,status FROM email_outbox WHERE kind='invitation'"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0]["idempotency_key"].startswith("team-invite-relay:")
    assert rows[0]["status"] == "pending"


def test_team_invite_refund_failure_keeps_provider_error_sanitized(monkeypatch):
    from engraphis.inspector import webhooks as WH
    from engraphis.inspector import license_cloud

    monkeypatch.setattr(
        WH, "queue_team_invite_email",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("token=SECRET")))
    monkeypatch.setattr(
        license_cloud, "_refund_invite_count",
        lambda *a: (_ for _ in ()).throw(OSError("C:/private/relay.db")))
    response = _app().post(
        "/license/v1/team-invite",
        json={"key": _key(plan="team"), "to": "new@corp.com"})
    assert response.status_code == 502
    assert "SECRET" not in response.text and "private" not in response.text


def test_team_invite_refund_targets_the_reserved_day_across_midnight(monkeypatch):
    key_id = "midnight-key"
    assert license_cloud._bump_invite_count(key_id, "2026-07-18") is True
    monkeypatch.setattr(license_cloud, "_today", lambda: "2026-07-19")

    license_cloud._refund_invite_count(key_id, "2026-07-18")

    conn = reg.connect()
    try:
        row = conn.execute(
            "SELECT count FROM team_invite_sends WHERE key_id=? AND day=?",
            (key_id, "2026-07-18"),
        ).fetchone()
    finally:
        conn.close()
    assert row["count"] == 0


# ── team-invite relay is a VENDOR-DOMAIN mail sender, so who picks the link matters ────
# `key` is the only authentication, and a free 3-day trial key satisfies verify_for_feature
# (..., "team"). Unrestricted caller-supplied `dashboard_url` therefore let anyone send a
# genuine, correctly-signed engraphis.com email pointing wherever they liked.

def _trial_team_key(email="trial@corp.com"):
    now = time.time()
    return licensing.compose_key(
        {"v": 1, "plan": "team", "email": email, "seats": 5, "trial": 1,
         "issued": int(now), "expires": int(now + 3 * 86400)}, SECRET)


def test_trial_key_cannot_choose_the_dashboard_url_in_a_vendor_email(monkeypatch):
    from engraphis.inspector import webhooks as WH
    captured = {}
    monkeypatch.setattr(
        WH, "queue_team_invite_email",
        lambda to, name, role, invited_by="", invite_url="", dashboard_url=None, **kwargs:
            captured.update(dashboard_url=dashboard_url, invite_url=invite_url))
    key = _trial_team_key()
    assert parse_key(key).is_trial is True
    r = _app().post("/license/v1/team-invite",
                    json={"key": key, "to": "victim@corp.com",
                          "dashboard_url": "https://engraphis-team.attacker.test/",
                          "invite_url": "https://engraphis-team.attacker.test/#invite_token=trial-secret"})
    # A legacy/unbound trial key has no verified deployment origin, so it cannot use the
    # vendor's mail reputation to send any link at all. Deployment-bound trials pass an
    # invite URL whose origin is checked against their confirmed claim.
    assert r.status_code == 409
    assert "dashboard origin" in r.json()["error"]
    assert captured == {}


def test_paid_key_pins_its_dashboard_url_on_first_use(monkeypatch):
    from engraphis.inspector import webhooks as WH
    seen = []
    monkeypatch.setattr(
        WH, "queue_team_invite_email",
        lambda to, name, role, invited_by="", invite_url="", dashboard_url=None, **kwargs:
            seen.append(dashboard_url))
    c = _app()
    key = _key(plan="team")
    body = {"key": key, "to": "new@corp.com", "dashboard_url": "https://team.corp.example/",
            "invite_url": "https://team.corp.example/#invite_token=pin-secret"}
    assert c.post("/license/v1/team-invite", json=body).status_code == 200
    # the same URL keeps working...
    assert c.post("/license/v1/team-invite", json=body).status_code == 200
    # ...a different one is refused, and never reaches the mail provider
    moved = dict(body, dashboard_url="https://engraphis-team.attacker.test/",
                 invite_url="https://engraphis-team.attacker.test/#invite_token=moved-secret")
    r = c.post("/license/v1/team-invite", json=moved)
    assert r.status_code == 409 and "dashboard url" in r.json()["error"].lower()
    # validate_cloud_base_url canonicalizes before the pin is taken, so an equivalent
    # spelling of the SAME URL keeps working while a different host cannot.
    assert seen == ["https://team.corp.example"] * 2
    # A key pinned by one customer does not constrain anybody else's key.
    other = c.post("/license/v1/team-invite",
                   json={"key": _key(plan="team", email="other@corp.com"),
                         "to": "new@corp.com",
                         "dashboard_url": "https://other.example/",
                         "invite_url": "https://other.example/#invite_token=other-secret"})
    assert other.status_code == 200


def test_rejected_dashboard_url_does_not_consume_the_daily_invite_cap(monkeypatch):
    from engraphis.inspector import license_cloud
    from engraphis.inspector import webhooks as WH
    monkeypatch.setattr(WH, "queue_team_invite_email", lambda *a, **k: None)
    monkeypatch.setattr(license_cloud, "_invite_daily_cap", lambda: 2)
    c = _app()
    key = _key(plan="team")
    assert c.post("/license/v1/team-invite",
                  json={"key": key, "to": "a@corp.com",
                        "dashboard_url": "https://team.corp.example/",
                        "invite_url": "https://team.corp.example/#invite_token=cap-pin-secret"}).status_code == 200
    for _ in range(3):
        assert c.post("/license/v1/team-invite",
                      json={"key": key, "to": "a@corp.com",
                            "dashboard_url": "https://evil.test/",
                            "invite_url": "https://evil.test/#invite_token=evil-secret"}).status_code == 409
    # the one remaining legitimate send is still available
    assert c.post("/license/v1/team-invite",
                  json={"key": key, "to": "b@corp.com",
                        "dashboard_url": "https://team.corp.example/",
                        "invite_url": "https://team.corp.example/#invite_token=cap-pin-secret"}).status_code == 200


def test_relay_invite_never_forwards_the_license_key(monkeypatch):
    from engraphis.inspector import webhooks as WH
    seen = {}
    monkeypatch.setattr(
        WH, "queue_team_invite_email",
        lambda to, name, role, **kwargs:
            seen.__setitem__(role, kwargs))
    c = _app()
    key = _key(plan="team")
    for role in ("viewer", "member"):
        assert c.post("/license/v1/team-invite",
                      json={"key": key, "to": "new@corp.com",
                            "role": role,
                            "invite_url": "https://team.customer.test/#invite_token=key-secret"}).status_code == 200
    assert "key" not in seen["viewer"]
    assert "key" not in seen["member"]


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
        WH, "queue_team_invite_email",
        lambda to, name, role, **kwargs:
            captured.update(to=to))
    c = _app()
    _wire_urlopen_to(c, monkeypatch)
    sent, reason = cloud_license.send_team_invite(
        "http://127.0.0.1", _key(plan="team"), "new@corp.com", "Mo", "member",
        "admin@corp.com",
        invite_url="https://team.customer.test/#invite_token=client-secret")
    assert sent is True and reason == ""
    assert captured["to"] == "new@corp.com"


def test_send_team_invite_client_reports_reason_on_402(monkeypatch):
    c = _app()
    _wire_urlopen_to(c, monkeypatch)
    sent, reason = cloud_license.send_team_invite(
        "http://127.0.0.1", _key(plan="pro"), "new@corp.com", "Mo", "member", "a@b.com",
        invite_url="http://127.0.0.1/#invite_token=402")
    assert sent is False and "team" in reason.lower()


def test_send_team_invite_client_fails_closed_on_network_error(monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    sent, reason = cloud_license.send_team_invite(
        "http://127.0.0.1", _key(plan="team"), "new@corp.com", "Mo", "member", "a@b.com")
    assert sent is False and "unreachable" in reason.lower()


def test_send_password_reset_client_roundtrip(monkeypatch):
    from engraphis.inspector import webhooks as WH

    captured = {}
    monkeypatch.setattr(
        WH, "queue_password_reset_email",
        lambda to, name, reset_url, **kwargs:
            captured.update(to=to, reset_url=reset_url) or "eml_roundtrip",
    )
    client = _app()
    _wire_urlopen_to(client, monkeypatch)
    sent, reason = cloud_license.send_password_reset(
        "http://127.0.0.1", _key(plan="team"), "owner@corp.com", "Owner",
        "https://team.corp.test/#reset_token=server-secret",
    )
    assert sent is True and reason == ""
    assert captured["to"] == "owner@corp.com"
    assert captured["reset_url"].endswith("reset_token=server-secret")


# ── self-serve Team trial: real signed key, one-per-device, must work with the ─────────
# team-invite relay above (that's the whole point — a trial user needs the "click
# button, send invite" experience to actually work, or they never see the value).
#
# 2026-07-14 hardening: POST /start-trial no longer hands back a key synchronously — it
# emails a one-time magic link. Opening that link (GET) only renders a confirm page;
# the key is minted by the POST that the page's button sends — see
# test_get_on_magic_link_does_not_redeem_it for why that split exists. Tests below mock
# the outbound send (no real SMTP in CI) and drive the link explicitly, same as a user
# clicking it, via the two helpers immediately below.

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
    parts = urllib.parse.urlsplit(url)
    assert parts.query == "", "one-time trial token must never enter an HTTP query"
    return urllib.parse.parse_qs(parts.fragment)["token"][0]


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
    v = c.post("/license/v1/start-trial/verify", json={"token": token})
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
        c.post("/license/v1/start-trial/verify", json={"token": token})

    monkeypatch.setattr(WH, "issue_key", issue_key)
    retry = c.post("/license/v1/start-trial/verify", json={"token": token})
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
        c.post("/license/v1/start-trial/verify", json={"token": token}).text)
    assert parse_key(key).seats == 5

    r2 = c.post("/license/v1/start-trial",
                json={"machine_id": "dev-spoof-2", "email": "b@example.com",
                      "seats": 999, "plan": "team"})
    assert r2.status_code == 200
    token2 = _token_from_url(captured["url"])
    key2 = _key_from_verify_html(
        c.post("/license/v1/start-trial/verify", json={"token": token2}).text)
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


def test_start_team_trial_requires_configured_public_url(monkeypatch):
    monkeypatch.delenv("ENGRAPHIS_RELAY_PUBLIC_URL")
    captured = _capture_verify_url(monkeypatch)
    response = _app().post(
        "/license/v1/start-trial",
        json={"machine_id": "dev-1", "email": "dev@example.com"},
        headers={"Host": "attacker.invalid"},
    )
    assert response.status_code == 503
    assert captured == {}


def test_start_team_trial_rejects_oversized_machine_id():
    response = _app().post(
        "/license/v1/start-trial",
        json={"machine_id": "m" * 129, "email": "dev@example.com"},
    )
    assert response.status_code == 400


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

    stale = c.post("/license/v1/start-trial/verify", json={"token": old_token})
    assert stale.status_code == 400

    fresh = c.post("/license/v1/start-trial/verify", json={"token": new_token})
    assert fresh.status_code == 200


def test_start_trial_sweeps_expired_pending_links(monkeypatch):
    """A magic link that is never opened must not sit in trial_pending forever.

    Before the sweep, a pending row was only ever cleared by the SAME machine_id asking
    again or by that exact token being redeemed — so links that simply go unclicked
    (bounced mail, a scanner that never follows, an attacker who never intends to redeem)
    accumulated one row per request, letting anyone at the /start-trial rate-limit ceiling
    grow the relay volume without bound. Any later reservation must now drop them."""
    c = _app()
    captured = _capture_verify_url(monkeypatch)

    assert c.post("/license/v1/start-trial",
                  json={"machine_id": "dev-abandoned",
                        "email": "abandoned@example.com"}).status_code == 200
    abandoned_token = _token_from_url(captured["url"])

    def _pending_machine_ids():
        conn = reg.connect()
        try:
            return {r[0] for r in conn.execute(
                "SELECT machine_id FROM trial_pending").fetchall()}
        finally:
            conn.close()

    assert "dev-abandoned" in _pending_machine_ids()

    # Age the abandoned link well past its retention window, not merely past its TTL:
    # a link that lapsed only moments ago is deliberately RETAINED so that
    # verify_team_trial can still say "expired" rather than "invalid" (see
    # test_expired_link_keeps_saying_expired_after_an_unrelated_reservation below).
    conn = reg.connect()
    try:
        conn.execute("UPDATE trial_pending SET expires_at=? WHERE machine_id=?",
                     (time.time() - license_cloud._TRIAL_PENDING_RETENTION_SECONDS - 60,
                      "dev-abandoned"))
        conn.commit()
    finally:
        conn.close()

    # A reservation by a DIFFERENT device is what has to collect the garbage — the
    # abandoned device is by definition never coming back to clear its own row.
    assert c.post("/license/v1/start-trial",
                  json={"machine_id": "dev-other",
                        "email": "other@example.com"}).status_code == 200

    remaining = _pending_machine_ids()
    assert "dev-abandoned" not in remaining, "expired pending link was not swept"
    assert "dev-other" in remaining, "sweep must not drop the still-valid link"

    # The swept link is genuinely dead, not merely hidden.
    assert c.post("/license/v1/start-trial/verify",
                  json={"token": abandoned_token}).status_code == 400


def test_get_on_magic_link_does_not_redeem_it(monkeypatch):
    """A bare GET must NOT burn the one-time trial grant.

    Corporate mail gateways and antivirus link-prescanners (Outlook Safe Links, Proofpoint
    URL Defense) GET every URL in an email before the recipient sees it. While a GET
    redeemed the token, a prescanner silently consumed the grant and the human who then
    clicked got "invalid or has already been used" on a legitimate first attempt — worst
    at exactly the corporate mail estates most likely to be buying Team. GET is now
    read-only; only the POST the confirm button sends grants anything."""
    c = _app()
    captured = _capture_verify_url(monkeypatch)
    assert c.post("/license/v1/start-trial",
                  json={"machine_id": "dev-prescanned",
                        "email": "scanned@example.com"}).status_code == 200
    token = _token_from_url(captured["url"])

    # The prescanner sweeps the link — possibly more than once.
    for _ in range(3):
        peek = c.get("/license/v1/start-trial/verify")
        assert peek.status_code == 200, peek.text[:200]
        assert token not in peek.text

    # No grant was recorded by any of that.
    conn = reg.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM trial_grants").fetchone()[0] == 0, (
            "a GET must not record a trial grant")
        assert conn.execute(
            "SELECT COUNT(*) FROM trial_pending WHERE machine_id=?",
            ("dev-prescanned",)).fetchone()[0] == 1, "a GET must not consume the token"
    finally:
        conn.close()

    # ...so the human's click still works and yields a real key.
    confirmed = c.post("/license/v1/start-trial/verify", json={"token": token})
    assert confirmed.status_code == 200, confirmed.text[:300]
    assert parse_key(_key_from_verify_html(confirmed.text)).is_trial is True


def test_fragment_confirmation_survives_a_sub_path_relay(monkeypatch):
    """The browser posts to its current path, including a configured relay sub-path."""
    monkeypatch.setenv("ENGRAPHIS_RELAY_PUBLIC_URL", "https://example.test/relay")
    c = _app()
    captured = _capture_verify_url(monkeypatch)
    assert c.post("/license/v1/start-trial",
                  json={"machine_id": "dev-subpath",
                        "email": "subpath@example.com"}).status_code == 200
    url = captured["url"]
    assert url.startswith("https://example.test/relay/license/v1/start-trial/verify")

    token = _token_from_url(url)
    body = c.get("/license/v1/start-trial/verify").text
    assert "fetch(window.location.pathname" in body
    assert token not in body
    assert "<form" not in body.lower()


def test_trial_verify_routes_are_rate_limited(monkeypatch):
    """Verification POSTs share a gate; scanner-safe GETs remain static and cheap."""
    monkeypatch.setattr(license_cloud, "REGISTER_RATE_PER_MINUTE", 2)
    license_cloud._REGISTER_BUCKETS.clear()
    c = _app()
    assert c.post("/license/v1/start-trial/verify",
                  json={"token": "junk"}).status_code == 400
    assert c.post("/license/v1/start-trial/verify",
                  json={"token": "junk"}).status_code == 400
    limited = c.post("/license/v1/start-trial/verify", json={"token": "junk"})
    assert limited.status_code == 429 and limited.headers["Retry-After"] == "60"
    assert c.get("/license/v1/start-trial/verify").status_code == 200


def test_every_trial_verify_response_is_uncacheable(monkeypatch):
    """EVERY /start-trial/verify response — success, each error, and the 429 — must carry
    the no-store/no-referrer headers, on both the GET and the POST.

    The URL fragment is removed before POST, while the success body contains a full key.
    This pins no-store/no-referrer across both static and dynamic responses."""
    def _assert_locked_down(r, label):
        assert "no-store" in r.headers.get("Cache-Control", ""), \
            "%s (%s) is cacheable" % (label, r.status_code)
        assert r.headers.get("Referrer-Policy") == "no-referrer", \
            "%s (%s) leaks Referer" % (label, r.status_code)

    c = _app()
    captured = _capture_verify_url(monkeypatch)

    # Static page plus missing/bogus POST bodies.
    _assert_locked_down(c.get("/license/v1/start-trial/verify"), "GET no token")
    _assert_locked_down(c.post("/license/v1/start-trial/verify"), "POST no token")
    _assert_locked_down(
        c.post("/license/v1/start-trial/verify", json={"token": "nope"}),
        "POST bogus token")
    # A query-string credential is deliberately ignored and rejected.
    _assert_locked_down(
        c.post("/license/v1/start-trial/verify", params={"token": "nope"}),
        "POST query token")

    # Confirm page and success page.
    assert c.post("/license/v1/start-trial",
                  json={"machine_id": "dev-hdrs", "email": "hdrs@example.com"}
                  ).status_code == 200
    token = _token_from_url(captured["url"])
    _assert_locked_down(
        c.get("/license/v1/start-trial/verify"), "confirm page")
    granted = c.post("/license/v1/start-trial/verify", json={"token": token})
    assert granted.status_code == 200
    _assert_locked_down(granted, "success page (contains the key)")

    # Already-used-device 409.
    assert c.post("/license/v1/start-trial",
                  json={"machine_id": "dev-hdrs", "email": "hdrs@example.com"}
                  ).status_code == 409

    # And the 429, which must keep Retry-After as well.
    monkeypatch.setattr(license_cloud, "REGISTER_RATE_PER_MINUTE", 1)
    license_cloud._REGISTER_BUCKETS.clear()
    c.post("/license/v1/start-trial/verify", json={"token": "nope"})
    throttled = c.post("/license/v1/start-trial/verify", json={"token": "nope"})
    assert throttled.status_code == 429
    assert throttled.headers["Retry-After"] == "60"
    _assert_locked_down(throttled, "429")


def test_confirm_page_keeps_token_out_of_server_html(monkeypatch):
    """The static page uses fragment-to-body JS without reflecting secret or customer."""
    c = _app()
    captured = _capture_verify_url(monkeypatch)
    assert c.post("/license/v1/start-trial",
                  json={"machine_id": "dev-confirm-form",
                        "email": "form@example.com"}).status_code == 200
    token = _token_from_url(captured["url"])

    page = c.get("/license/v1/start-trial/verify")
    assert page.status_code == 200
    body = page.text
    assert 'id="trial-confirm"' in body
    assert "JSON.stringify({token:token})" in body
    assert "window.history.replaceState" in body
    assert token not in body
    assert "form@example.com" not in body
    assert "form-action 'none'" in page.headers["Content-Security-Policy"]


@pytest.mark.parametrize("bad", ["", "not-a-real-token"])
def test_confirm_page_reports_a_bad_token_without_mutating(bad):
    """Only the bounded POST performs token diagnostics or state access."""
    c = _app()
    r = c.post("/license/v1/start-trial/verify", json={"token": bad})
    assert r.status_code == 400
    assert "invalid" in r.text.lower() or "missing" in r.text.lower()


def test_confirm_page_reports_an_expired_link_as_expired(monkeypatch):
    c = _app()
    captured = _capture_verify_url(monkeypatch)
    assert c.post("/license/v1/start-trial",
                  json={"machine_id": "dev-lapsed-peek",
                        "email": "lapsed@example.com"}).status_code == 200
    token = _token_from_url(captured["url"])

    conn = reg.connect()
    try:
        conn.execute("UPDATE trial_pending SET expires_at=? WHERE machine_id=?",
                     (time.time() - 60, "dev-lapsed-peek"))
        conn.commit()
    finally:
        conn.close()

    r = c.post("/license/v1/start-trial/verify", json={"token": token})
    assert r.status_code == 400
    assert "expired" in r.text.lower()
    # A human POST consumed the expired one-time row; static GET never touches it.
    conn = reg.connect()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM trial_pending WHERE machine_id=?",
            ("dev-lapsed-peek",)).fetchone()[0] == 0
    finally:
        conn.close()


def test_expired_link_keeps_saying_expired_after_an_unrelated_reservation(monkeypatch):
    """The retention window exists so the expiry diagnostic survives the sweep.

    Sweeping rows the instant they lapse would bound the table but silently downgrade
    "this link has expired — request a new trial" into "this link is invalid or has
    already been used", and would do it NON-deterministically: which message a user saw
    would depend on whether some unrelated device happened to call /start-trial in
    between. Regression test for exactly that."""
    c = _app()
    captured = _capture_verify_url(monkeypatch)

    assert c.post("/license/v1/start-trial",
                  json={"machine_id": "dev-late-clicker",
                        "email": "late@example.com"}).status_code == 200
    token = _token_from_url(captured["url"])

    # Lapse the link (past TTL) but keep it inside the retention window.
    conn = reg.connect()
    try:
        conn.execute("UPDATE trial_pending SET expires_at=? WHERE machine_id=?",
                     (time.time() - 60, "dev-late-clicker"))
        conn.commit()
    finally:
        conn.close()

    # An unrelated device reserves — this is what runs the sweep.
    assert c.post("/license/v1/start-trial",
                  json={"machine_id": "dev-unrelated",
                        "email": "unrelated@example.com"}).status_code == 200

    late = c.post("/license/v1/start-trial/verify", json={"token": token})
    assert late.status_code == 400
    assert "expired" in late.text.lower(), (
        "expired link must still report EXPIRED after an unrelated reservation, got: %s"
        % late.text[:200])


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


def test_legacy_trial_migration_flow_allows_only_one_grant_per_normalized_email(
        monkeypatch):
    """An explicitly enabled legacy flow must retain the GA one-trial-per-email gate."""
    c = _app()
    captured = _capture_verify_url(monkeypatch)
    assert _start_and_confirm(
        c, captured, "dev-email-1", email="Person@Example.com")

    second = c.post(
        "/license/v1/start-trial",
        json={"machine_id": "dev-email-2", "email": "person@example.com"},
    )
    assert second.status_code == 409
    assert "already been used" in second.json()["error"]


def test_start_team_trial_verify_rejects_unknown_token():
    c = _app()
    r = c.post("/license/v1/start-trial/verify", json={"token": "not-a-real-token"})
    assert r.status_code == 400


def test_start_team_trial_verify_link_is_one_time_use(monkeypatch):
    c = _app()
    captured = _capture_verify_url(monkeypatch)
    c.post("/license/v1/start-trial",
          json={"machine_id": "dev-1", "email": "dev@example.com"})
    token = _token_from_url(captured["url"])
    first = c.post("/license/v1/start-trial/verify", json={"token": token})
    assert first.status_code == 200
    replay = c.post("/license/v1/start-trial/verify", json={"token": token})
    assert replay.status_code == 400


def test_start_team_trial_verify_rejects_expired_token(monkeypatch):
    c = _app()
    captured = _capture_verify_url(monkeypatch)
    monkeypatch.setattr(license_cloud, "_TRIAL_TOKEN_TTL_SECONDS", -1)  # already expired
    r = c.post("/license/v1/start-trial",
               json={"machine_id": "dev-1", "email": "dev@example.com"})
    assert r.status_code == 200
    token = _token_from_url(captured["url"])
    v = c.post("/license/v1/start-trial/verify", json={"token": token})
    assert v.status_code == 400
    assert "expired" in v.text.lower()


def test_start_team_trial_surfaces_email_delivery_failure_as_502(monkeypatch):
    from engraphis.inspector import webhooks as WH

    def boom(*a, **k):
        raise RuntimeError("api_key=RESEND_SECRET_123 C:/private/customer.db")

    monkeypatch.setattr(WH, "send_trial_verification_email", boom)
    c = _app()
    r = c.post("/license/v1/start-trial",
               json={"machine_id": "dev-1", "email": "dev@example.com"})
    assert r.status_code == 502
    assert "RESEND_SECRET_123" not in r.text and "private" not in r.text


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


def test_start_team_trial_ignores_client_prepended_forwarded_prefix(monkeypatch):
    """A client cannot mint fresh rate-limit buckets by prepending its own value to
    X-Forwarded-For: the trusted proxy appends the real client IP to the RIGHT, so only
    the rightmost entry is authoritative. Simulate a Railway-style single trusted hop
    (``*``) that saw one real client (203.0.113.9) but a client that keeps rotating a
    spoofed left prefix — the cap must still bite on the real (rightmost) address."""
    monkeypatch.setenv("ENGRAPHIS_FORWARDED_ALLOW_IPS", "*")
    monkeypatch.setattr(license_cloud, "_trial_rate_limit_per_hour", lambda: 2)
    c = _app()
    _capture_verify_url(monkeypatch)
    for i in range(2):
        r = c.post("/license/v1/start-trial",
                   json={"machine_id": "dev-%d" % i, "email": "dev%d@example.com" % i},
                   headers={"X-Forwarded-For": "10.0.0.%d, 203.0.113.9" % i})
        assert r.status_code == 200, r.text
    over = c.post("/license/v1/start-trial",
                  json={"machine_id": "dev-over", "email": "over@example.com"},
                  headers={"X-Forwarded-For": "10.0.0.250, 203.0.113.9"})
    assert over.status_code == 429


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


def test_legacy_team_trial_key_cannot_use_deployment_bound_invite_relay(monkeypatch):
    """Legacy trial issuance is intentionally not enough to send branded invite links.

    Only the deployment-bound claim flow records a verified dashboard origin; the
    explicitly retained legacy flow therefore fails closed at the invite relay.
    """
    from engraphis.inspector import webhooks as WH
    captured_invite = {}
    monkeypatch.setattr(
        WH, "queue_team_invite_email",
        lambda to, name, role, **kwargs:
            captured_invite.update(to=to))
    c = _app()
    captured = _capture_verify_url(monkeypatch)
    key = _start_and_confirm(c, captured, "dev-1")
    r = c.post("/license/v1/team-invite",
               json={"key": key, "to": "teammate@corp.com",
                     "invite_url": "https://team.customer.test/#invite_token=legacy-secret"})
    assert r.status_code == 409
    assert "dashboard origin" in r.json()["error"]
    assert captured_invite == {}


def test_request_team_trial_key_client_returns_pending(monkeypatch):
    c = _app()
    _capture_verify_url(monkeypatch)
    _wire_urlopen_to(c, monkeypatch)
    key, reason, pending = cloud_license.request_team_trial_key(
        "http://127.0.0.1", "dev-1", email="dev@example.com")
    assert key is None and pending is True and reason


def test_request_team_trial_key_client_reports_already_used(monkeypatch):
    c = _app()
    captured = _capture_verify_url(monkeypatch)
    _wire_urlopen_to(c, monkeypatch)
    cloud_license.request_team_trial_key(
        "http://127.0.0.1", "dev-1", email="dev@example.com"
    )
    token = _token_from_url(captured["url"])
    confirmed = c.post("/license/v1/start-trial/verify", json={"token": token})
    assert confirmed.status_code == 200                 # the device now holds a grant
    key, reason, pending = cloud_license.request_team_trial_key(
        "http://127.0.0.1", "dev-1", email="dev@example.com")
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
    monkeypatch.setenv("ENGRAPHIS_CLOUD_URL", "http://127.0.0.1")
    got = licensing.start_team_trial(email="trial@engraphis.local")
    assert got["plan"] == "team"
    assert licensing.current_license(refresh=True).plan == "team"
    assert licensing.has_feature("team") is True


def test_licensing_start_team_trial_refuses_if_paid_key_already_active(monkeypatch):
    """Same reasoning as test_start_trial_refuses_if_paid_key_already_active: only
    refuse when the cloud gate actually approves the existing key."""
    c = _app()
    _wire_register_to(c, monkeypatch)
    monkeypatch.setenv("ENGRAPHIS_CLOUD_URL", "http://127.0.0.1")
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
    monkeypatch.setenv("ENGRAPHIS_CLOUD_URL", "http://127.0.0.1")
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
    monkeypatch.setenv("ENGRAPHIS_CLOUD_URL", "http://127.0.0.1")
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


@pytest.mark.parametrize(
    ("starter", "client_name"),
    [
        (licensing.start_trial, "request_trial_key"),
        (licensing.start_team_trial, "request_team_trial_key"),
    ],
)
def test_trial_requests_migrate_retired_server_override(
        monkeypatch, starter, client_name):
    captured = {}

    def pending(base, *args, **kwargs):
        captured["base"] = base
        return None, "check your email", True

    monkeypatch.setenv(
        "ENGRAPHIS_CLOUD_URL",
        "https://engraphis-production.up.railway.app/",
    )
    monkeypatch.setattr(cloud_license, client_name, pending)

    assert starter(email="me@example.com")["pending"] is True
    assert captured["base"] == DEFAULT_LICENSE_SERVER_URL


def test_rejected_lease_counter_is_content_free_and_thresholded(monkeypatch):
    monkeypatch.setenv("ENGRAPHIS_REJECTED_LEASE_ALERT_THRESHOLD", "2")
    now = time.time()
    assert reg.rejected_lease_health(now=now) is True
    reg.record_control_plane_event("lease_rejected", now=now - 1)
    assert reg.rejected_lease_health(now=now) is True
    reg.record_control_plane_event("lease_rejected", now=now)
    assert reg.rejected_lease_health(now=now) is False
    conn = reg.connect()
    try:
        columns = [row[1] for row in conn.execute(
            "PRAGMA table_info(control_plane_events)").fetchall()]
    finally:
        conn.close()
    assert columns == ["id", "kind", "occurred_at"]
