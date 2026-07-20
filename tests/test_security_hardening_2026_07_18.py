"""Regression tests for the 2026-07-18 security batch.

Every test here corresponds to a specific finding from the Team/Pro systems audit. They
are grouped by finding id so a failure names the vulnerability it re-opens rather than
just the function it touches.

Runs on the numpy-only offline gate: stdlib + fastapi TestClient, no network.
"""
import os
import asyncio
import stat
import time

import pytest

pytest.importorskip("fastapi")
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from engraphis import licensing, netutil
from engraphis.inspector import license_cloud, sync_relay
from engraphis.licensing import LicenseError, ed25519_public_key

SECRET = bytes(range(32))  # deterministic test vendor keypair


@pytest.fixture(autouse=True)
def _relay_env(monkeypatch, tmp_path):
    monkeypatch.setenv("ENGRAPHIS_LICENSE_PUBKEY", ed25519_public_key(SECRET).hex())
    monkeypatch.setenv("ENGRAPHIS_RELAY_DB", str(tmp_path / "relay.db"))
    monkeypatch.delenv("ENGRAPHIS_LICENSE_KEY", raising=False)
    monkeypatch.delenv("ENGRAPHIS_RELAY_PUBLIC_URL", raising=False)
    monkeypatch.delenv("ENGRAPHIS_FORWARDED_ALLOW_IPS", raising=False)
    yield


def _key(plan="pro", email="buyer@example.com", *, expires_in_days=30, seats=1):
    now = time.time()
    payload = {"v": 1, "plan": plan, "email": email, "seats": seats,
               "issued": int(now),
               "expires": None if expires_in_days is None
                          else int(now + expires_in_days * 86400)}
    return licensing.compose_key(payload, SECRET)


def _relay_client():
    app = FastAPI()
    app.include_router(sync_relay.router)
    app.include_router(license_cloud.router)

    @app.exception_handler(LicenseError)
    async def _license(request, exc):  # noqa: ANN001
        return JSONResponse({"error": str(exc)}, status_code=402)

    return TestClient(app)


# ── H1: magic-link base URL must never come from the Host header ─────────────────────

def test_h1_relay_public_base_is_empty_when_unset():
    """The Host-header fallback is gone entirely — unset means unset, not 'guess'."""
    assert license_cloud._relay_public_base() == ""


def test_h1_relay_public_base_takes_no_request_argument():
    """Structural guard: the fix is that the function CANNOT see the Host header.

    If someone reintroduces a ``request`` parameter, this fails loudly even if the
    fallback logic itself looks harmless at the time.
    """
    import inspect
    assert list(inspect.signature(license_cloud._relay_public_base).parameters) == []


def test_h1_start_trial_refuses_when_public_url_unset():
    client = _relay_client()
    response = client.post("/license/v1/start-trial",
                           json={"machine_id": "dev-1", "email": "victim@corp.example",
                                 "plan": "pro"},
                           headers={"Host": "attacker.example"})
    assert response.status_code == 503
    assert "not configured" in response.json()["error"]


def test_h1_hostile_host_header_cannot_reach_the_email(monkeypatch):
    """The end-to-end property: with the relay configured, the emailed link points at
    the CONFIGURED host no matter what Host the attacker sends."""
    monkeypatch.setenv("ENGRAPHIS_RELAY_PUBLIC_URL", "https://team.engraphis.com")
    sent = {}

    def _capture(email, verify_url, plan, minutes=30):
        sent["email"], sent["url"] = email, verify_url

    monkeypatch.setattr("engraphis.inspector.webhooks.send_trial_verification_email",
                        _capture)
    client = _relay_client()
    response = client.post("/license/v1/start-trial",
                           json={"machine_id": "dev-1", "email": "victim@corp.example",
                                 "plan": "pro"},
                           headers={"Host": "attacker.example"})
    assert response.status_code == 200
    assert sent["url"].startswith("https://team.engraphis.com/license/v1/start-trial/verify")
    assert "attacker.example" not in sent["url"]


def test_h1_malformed_public_url_fails_closed(monkeypatch):
    """A typo'd/hostile env value must disable trials, not ship a bad link."""
    for bad in ("http://team.engraphis.com",          # not HTTPS
                "https://user:pw@team.engraphis.com",  # embedded credentials
                "not a url"):
        monkeypatch.setenv("ENGRAPHIS_RELAY_PUBLIC_URL", bad)
        assert license_cloud._relay_public_base() == "", bad


# ── H2: per-account relay storage ceilings ───────────────────────────────────────────

def test_h2_account_byte_cap_spans_distinct_workspaces(monkeypatch):
    """The exploit was unlimited caller-chosen workspace ids, each with its own quota."""
    monkeypatch.setattr(sync_relay, "MAX_ACCOUNT_BYTES", 3000)
    monkeypatch.setattr(sync_relay, "MAX_WORKSPACES_PER_ACCOUNT", 0)  # isolate byte cap
    client = _relay_client()
    key = _key()
    auth = {"Authorization": "Bearer %s" % key}
    blob = b"x" * 1000
    for index in range(3):                       # 3000 bytes across 3 workspaces — fits
        r = client.post("/relay/v1/ws%d/bundles/b.json" % index, content=blob, headers=auth)
        assert r.status_code == 200, (index, r.text)
    r = client.post("/relay/v1/ws99/bundles/b.json", content=blob, headers=auth)
    assert r.status_code == 413
    assert "account relay storage limit" in r.json()["error"]


def test_h2_account_workspace_count_cap(monkeypatch):
    monkeypatch.setattr(sync_relay, "MAX_WORKSPACES_PER_ACCOUNT", 2)
    monkeypatch.setattr(sync_relay, "MAX_ACCOUNT_BYTES", 0)   # isolate the count cap
    client = _relay_client()
    auth = {"Authorization": "Bearer %s" % _key()}
    for index in range(2):
        assert client.post("/relay/v1/w%d/bundles/b.json" % index,
                           content=b"{}", headers=auth).status_code == 200
    r = client.post("/relay/v1/w2/bundles/b.json", content=b"{}", headers=auth)
    assert r.status_code == 413
    assert "too many synced workspaces" in r.json()["error"]


def test_h2_caps_do_not_block_updating_an_existing_bundle(monkeypatch):
    """An account AT the ceiling must still be able to re-push, or sync would deadlock
    into delete-only. The replaced row's bytes are credited back before the check."""
    monkeypatch.setattr(sync_relay, "MAX_ACCOUNT_BYTES", 1000)
    monkeypatch.setattr(sync_relay, "MAX_WORKSPACES_PER_ACCOUNT", 1)
    client = _relay_client()
    auth = {"Authorization": "Bearer %s" % _key()}
    assert client.post("/relay/v1/ws/bundles/b.json", content=b"y" * 1000,
                       headers=auth).status_code == 200
    # Same size, same slot — exactly at the cap, must succeed.
    assert client.post("/relay/v1/ws/bundles/b.json", content=b"z" * 1000,
                       headers=auth).status_code == 200
    # A second bundle in the SAME workspace is a byte-cap decision, not a count one.
    assert client.post("/relay/v1/ws/bundles/c.json", content=b"z",
                       headers=auth).status_code == 413


def test_h2_accounts_are_capped_independently():
    client = _relay_client()
    a = {"Authorization": "Bearer %s" % _key(email="a@example.com")}
    b = {"Authorization": "Bearer %s" % _key(email="b@example.com")}
    assert client.post("/relay/v1/ws/bundles/b.json", content=b"{}",
                       headers=a).status_code == 200
    assert client.post("/relay/v1/ws/bundles/b.json", content=b"{}",
                       headers=b).status_code == 200


# ── M2: register is burst-capped and does not block the event loop ───────────────────

def test_m2_register_rate_limits_invalid_key_flood(monkeypatch):
    monkeypatch.setattr(license_cloud, "REGISTER_RATE_PER_MINUTE", 3)
    license_cloud._REGISTER_BUCKETS.clear()
    client = _relay_client()
    body = {"key": "ENGR1.aaaa.bbbb", "machine_id": "dev-1"}
    statuses = [client.post("/license/v1/register", json=body).status_code
                for _ in range(5)]
    assert 429 in statuses, statuses
    assert statuses[-1] == 429


def test_m2_team_invite_rate_limits_invalid_key_flood(monkeypatch):
    """/team-invite runs the same unauthenticated Ed25519 verify as /register.

    It was originally given only the worker-thread half of the /register fix, leaving an
    uncapped ~3 ms crypto path on a sibling route — an attacker just switches endpoints.
    """
    monkeypatch.setattr(license_cloud, "REGISTER_RATE_PER_MINUTE", 3)
    license_cloud._REGISTER_BUCKETS.clear()
    client = _relay_client()
    body = {"key": "ENGR1.aaaa.bbbb", "to": "x@example.com", "role": "member", "invite_url": "https://team.customer.test/#invite_token=ok"}
    statuses = [client.post("/license/v1/team-invite", json=body).status_code
                for _ in range(5)]
    assert 429 in statuses, statuses
    assert statuses[-1] == 429


def test_m2_invite_and_register_share_one_burst_budget(monkeypatch):
    """One bucket covers the whole unauthenticated crypto surface, so alternating
    endpoints cannot buy double the budget for the same work."""
    monkeypatch.setattr(license_cloud, "REGISTER_RATE_PER_MINUTE", 2)
    license_cloud._REGISTER_BUCKETS.clear()
    client = _relay_client()
    invite = {"key": "ENGR1.aaaa.bbbb", "to": "x@example.com", "role": "member", "invite_url": "https://team.customer.test/#invite_token=ok"}
    register = {"key": "ENGR1.aaaa.bbbb", "machine_id": "dev-1"}
    assert client.post("/license/v1/register", json=register).status_code != 429
    assert client.post("/license/v1/team-invite", json=invite).status_code != 429
    # Two tokens spent across the two routes; the third call is refused on either.
    assert client.post("/license/v1/team-invite", json=invite).status_code == 429
    assert client.post("/license/v1/register", json=register).status_code == 429


def test_m2_rate_limiter_bucket_table_is_bounded(monkeypatch):
    """The limiter must not become the memory-exhaustion DoS it exists to prevent."""
    monkeypatch.setattr(license_cloud, "REGISTER_RATE_PER_MINUTE", 60)
    license_cloud._REGISTER_BUCKETS.clear()
    for index in range(license_cloud._REGISTER_BUCKETS_MAX + 50):
        license_cloud._register_rate_ok("10.0.%d.%d" % (index // 256, index % 256))
    assert len(license_cloud._REGISTER_BUCKETS) <= license_cloud._REGISTER_BUCKETS_MAX


def test_m2_rate_limit_disabled_when_zero(monkeypatch):
    monkeypatch.setattr(license_cloud, "REGISTER_RATE_PER_MINUTE", 0)
    license_cloud._REGISTER_BUCKETS.clear()
    assert all(license_cloud._register_rate_ok("1.2.3.4") for _ in range(200))


def test_m2_forwarding_header_cannot_disable_limiter(monkeypatch):
    monkeypatch.delenv("ENGRAPHIS_FORWARDED_ALLOW_IPS", raising=False)
    request = _FakeRequest("10.0.0.1", {"x-forwarded-for": "198.51.100.7"})
    assert license_cloud._register_rate_key(request) == "10.0.0.1"
    monkeypatch.setattr(license_cloud, "REGISTER_RATE_PER_MINUTE", 1)
    license_cloud._REGISTER_BUCKETS.clear()
    assert license_cloud._register_rate_ok("10.0.0.1") is True
    assert license_cloud._register_rate_ok("10.0.0.1") is False


def test_m2_limiter_active_when_proxy_is_trusted(monkeypatch):
    """With the proxy trusted we have a real per-caller identity, so limit normally."""
    monkeypatch.setenv("ENGRAPHIS_FORWARDED_ALLOW_IPS", "*")
    request = _FakeRequest("10.0.0.1", {"x-forwarded-for": "198.51.100.7"})
    assert license_cloud._register_rate_key(request) == "198.51.100.7"


def test_m2_limiter_active_for_a_direct_caller(monkeypatch):
    """No proxy in front at all — the direct peer IS the caller identity."""
    monkeypatch.delenv("ENGRAPHIS_FORWARDED_ALLOW_IPS", raising=False)
    assert license_cloud._register_rate_key(_FakeRequest("203.0.113.9")) == "203.0.113.9"


# ── H2 follow-up: a quota the customer cannot remediate is an outage ─────────────────

def test_h2_delete_frees_quota_so_a_capped_account_can_recover(monkeypatch):
    """Found by adversarial re-review: with no delete route, hitting either new cap was
    permanent and needed vendor DB surgery."""
    monkeypatch.setattr(sync_relay, "MAX_WORKSPACES_PER_ACCOUNT", 2)
    monkeypatch.setattr(sync_relay, "MAX_ACCOUNT_BYTES", 0)
    client = _relay_client()
    auth = {"Authorization": "Bearer %s" % _key()}
    for index in range(2):
        assert client.post("/relay/v1/w%d/bundles/b.json" % index,
                           content=b"{}", headers=auth).status_code == 200
    assert client.post("/relay/v1/w9/bundles/b.json", content=b"{}",
                       headers=auth).status_code == 413
    gone = client.request("DELETE", "/relay/v1/w0/bundles/b.json", headers=auth)
    assert gone.status_code == 200 and gone.json()["deleted"] is True
    assert client.post("/relay/v1/w9/bundles/b.json", content=b"{}",
                       headers=auth).status_code == 200


def test_h2_delete_is_idempotent_and_account_scoped():
    client = _relay_client()
    mine = {"Authorization": "Bearer %s" % _key(email="a@example.com")}
    theirs = {"Authorization": "Bearer %s" % _key(email="b@example.com")}
    assert client.post("/relay/v1/ws/bundles/b.json", content=b"{}",
                       headers=theirs).status_code == 200
    # Deleting "the same" path as another account must not touch their row.
    first = client.request("DELETE", "/relay/v1/ws/bundles/b.json", headers=mine)
    assert first.status_code == 200 and first.json()["deleted"] is False
    assert client.get("/relay/v1/ws/bundles/b.json", headers=theirs).status_code == 200


def test_h2_delete_requires_a_license():
    client = _relay_client()
    assert client.request("DELETE", "/relay/v1/ws/bundles/b.json").status_code == 402


def test_h2_delete_does_not_block_the_event_loop(monkeypatch):
    def slow_authorize(request):
        time.sleep(0.2)
        return None, "account"

    monkeypatch.setattr(sync_relay, "_authorize", slow_authorize)
    monkeypatch.setattr(sync_relay, "_delete_bundle", lambda *args: False)

    async def probe():
        started = time.monotonic()
        task = asyncio.create_task(sync_relay.delete_bundle(
            "workspace", "bundle.json", object()))
        await asyncio.sleep(0.02)
        elapsed = time.monotonic() - started
        result = await task
        return elapsed, result

    elapsed, result = asyncio.run(probe())
    assert elapsed < 0.1
    assert result["deleted"] is False


def test_trial_start_does_not_block_the_event_loop(monkeypatch):
    async def body(request):
        return {"machine_id": "device", "email": "person@example.com", "plan": "team"}

    def slow_rate(ip):
        time.sleep(0.2)
        return True

    monkeypatch.setattr(license_cloud, "_bounded_json_object", body)
    monkeypatch.setattr(license_cloud, "_relay_public_base", lambda: "https://relay.example")
    monkeypatch.setattr(license_cloud, "_bump_trial_rate", slow_rate)
    monkeypatch.setattr(license_cloud, "_reserve_trial", lambda *args: "token")
    monkeypatch.setattr(
        "engraphis.inspector.webhooks.send_trial_verification_email",
        lambda *args, **kwargs: None)

    async def probe():
        started = time.monotonic()
        task = asyncio.create_task(
            license_cloud.start_team_trial(_FakeRequest("203.0.113.8")))
        await asyncio.sleep(0.02)
        elapsed = time.monotonic() - started
        result = await task
        return elapsed, result

    elapsed, result = asyncio.run(probe())
    assert elapsed < 0.1
    assert result["pending"] is True


def test_llm_ping_redacts_unexpected_provider_exception():
    from engraphis.llm.client import LLMClient

    client = object.__new__(LLMClient)
    client.provider = "openai"
    client.model = "model"

    def fail(*args, **kwargs):
        raise RuntimeError("api_key=SECRET C:/private/customer.db")

    client.chat = fail
    result = client.ping()
    assert result["ok"] is False
    assert "SECRET" not in result["error"] and "private" not in result["error"]


# ── M4: per-IP decisions read the RIGHTMOST X-Forwarded-For entry ────────────────────

class _FakeRequest:
    def __init__(self, peer, headers=None):
        self.client = type("C", (), {"host": peer})()
        self.headers = headers or {}


def test_m4_untrusted_peer_ignores_forwarded_header():
    request = _FakeRequest("203.0.113.9", {"x-forwarded-for": "1.1.1.1"})
    assert netutil.client_ip(request) == "203.0.113.9"


def test_m4_trusted_proxy_uses_rightmost_not_leftmost(monkeypatch):
    """Leftmost is attacker-supplied. Reading it would let a caller mint a fresh
    lockout/rate-limit identity per request by pre-seeding the header."""
    monkeypatch.setenv("ENGRAPHIS_FORWARDED_ALLOW_IPS", "*")
    request = _FakeRequest("10.0.0.1",
                           {"x-forwarded-for": "spoofed-1, spoofed-2, 198.51.100.7"})
    assert netutil.client_ip(request) == "198.51.100.7"


def test_m4_proxy_allow_list_accepts_ip_networks(monkeypatch):
    monkeypatch.setenv("ENGRAPHIS_FORWARDED_ALLOW_IPS", "10.20.0.0/16,2001:db8::/32")
    request = _FakeRequest("10.20.4.9", {"x-forwarded-for": "198.51.100.7"})
    assert netutil.client_ip(request) == "198.51.100.7"
    request6 = _FakeRequest("2001:db8::9", {"x-forwarded-for": "2001:db9::1"})
    assert netutil.client_ip(request6) == "2001:db9::1"


def test_m4_login_lockout_cannot_be_reset_by_rotating_forwarded_for(monkeypatch):
    """The audit finding, end to end: rotating X-Forwarded-For must NOT produce a new
    identity when the proxy is trusted."""
    monkeypatch.setenv("ENGRAPHIS_FORWARDED_ALLOW_IPS", "*")
    seen = {netutil.client_ip(_FakeRequest(
        "10.0.0.1", {"x-forwarded-for": "%d.%d.%d.%d, 198.51.100.7" % ((n,) * 4)}))
        for n in range(1, 20)}
    assert seen == {"198.51.100.7"}


# ── M5: vendor admin token has no fallback ───────────────────────────────────────────

def test_m5_no_fallback_to_api_token(monkeypatch):
    """Unset vendor token ⇒ empty expected secret ⇒ bearer_ok always False. The service
    account credential must not inherit vendor-wide revoke authority."""
    monkeypatch.delenv("ENGRAPHIS_VENDOR_ADMIN_TOKEN", raising=False)
    monkeypatch.setenv("ENGRAPHIS_API_TOKEN", "service-account-token")
    assert license_cloud._vendor_admin_token() == ""


def test_m5_api_token_is_never_returned_as_the_vendor_token(monkeypatch):
    """Behavioural guard against the fallback being reintroduced: whatever the service
    account credential is set to, it must never come back out of this function."""
    from engraphis.config import settings as _settings
    monkeypatch.setenv("ENGRAPHIS_API_TOKEN", "service-account-token")
    monkeypatch.setattr(_settings, "api_token", "service-account-token", raising=False)
    monkeypatch.delenv("ENGRAPHIS_VENDOR_ADMIN_TOKEN", raising=False)
    assert license_cloud._vendor_admin_token() != "service-account-token"
    monkeypatch.setenv("ENGRAPHIS_VENDOR_ADMIN_TOKEN", "short")
    assert license_cloud._vendor_admin_token() == ""
    token = "vendor-only-secret-at-least-32-characters"
    monkeypatch.setenv("ENGRAPHIS_VENDOR_ADMIN_TOKEN", token)
    assert license_cloud._vendor_admin_token() == token


def test_m5_admin_routes_fail_closed_with_the_api_token(monkeypatch):
    monkeypatch.delenv("ENGRAPHIS_VENDOR_ADMIN_TOKEN", raising=False)
    monkeypatch.setenv("ENGRAPHIS_API_TOKEN", "service-account-token")
    client = _relay_client()
    r = client.post("/license/v1/revoke/deadbeefcafe",
                    headers={"Authorization": "Bearer service-account-token"})
    assert r.status_code == 401


def test_m5_dedicated_token_still_works(monkeypatch):
    token = "vendor-only-secret-at-least-32-characters"
    monkeypatch.setenv("ENGRAPHIS_VENDOR_ADMIN_TOKEN", token)
    client = _relay_client()
    r = client.post("/license/v1/revoke/deadbeefcafe",
                    headers={"Authorization": "Bearer " + token})
    assert r.status_code != 401


# ── L1: the persisted license key is never briefly world-readable ────────────────────

@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_l1_license_file_is_created_0600(monkeypatch, tmp_path):
    monkeypatch.setenv("ENGRAPHIS_LICENSE_PUBKEY", ed25519_public_key(SECRET).hex())
    licensing._LICENSE_FILE = tmp_path / "license.key"
    licensing.activate(_key())
    mode = stat.S_IMODE(os.stat(licensing._LICENSE_FILE).st_mode)
    assert mode == 0o600, oct(mode)


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_l1_reactivation_truncates_and_keeps_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("ENGRAPHIS_LICENSE_PUBKEY", ed25519_public_key(SECRET).hex())
    licensing._LICENSE_FILE = tmp_path / "license.key"
    licensing.activate(_key(email="long-address-so-the-first-key-is-longer@example.com"))
    short = _key(email="a@b.co")
    licensing.activate(short)
    assert licensing._LICENSE_FILE.read_text(encoding="utf-8").strip() == short
    assert stat.S_IMODE(os.stat(licensing._LICENSE_FILE).st_mode) == 0o600


def test_l1_failed_atomic_replace_preserves_previous_key(monkeypatch, tmp_path):
    monkeypatch.setenv("ENGRAPHIS_LICENSE_PUBKEY", ed25519_public_key(SECRET).hex())
    licensing._LICENSE_FILE = tmp_path / "license.key"
    old = _key(email="old@example.com")
    licensing.activate(old)

    def fail_replace(source, target):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failure"):
        licensing.activate(_key(email="new@example.com"))
    assert licensing._LICENSE_FILE.read_text(encoding="utf-8").strip() == old
    assert not list(tmp_path.glob(".license.key.*"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_l1_reactivation_replaces_a_permissive_inode_privately(monkeypatch, tmp_path):
    monkeypatch.setenv("ENGRAPHIS_LICENSE_PUBKEY", ed25519_public_key(SECRET).hex())
    licensing._LICENSE_FILE = tmp_path / "license.key"
    licensing._LICENSE_FILE.write_text("stale\n", encoding="utf-8")
    os.chmod(licensing._LICENSE_FILE, 0o644)
    licensing.activate(_key())
    assert stat.S_IMODE(os.stat(licensing._LICENSE_FILE).st_mode) == 0o600


# ── L3: rate-limit tables are pruned ─────────────────────────────────────────────────

def test_l3_trial_rate_table_drops_stale_windows(monkeypatch):
    from engraphis.inspector import license_registry as reg
    monkeypatch.setattr(license_cloud, "_hour_bucket", lambda now=None: "2020-01-01-00")
    license_cloud._bump_trial_rate("1.2.3.4")
    monkeypatch.setattr(license_cloud, "_hour_bucket", lambda now=None: "2020-01-01-01")
    license_cloud._bump_trial_rate("1.2.3.4")
    conn = reg.connect()
    try:
        windows = {row[0] for row in
                   conn.execute("SELECT DISTINCT window FROM trial_start_attempts")}
    finally:
        conn.close()
    assert windows == {"2020-01-01-01"}
