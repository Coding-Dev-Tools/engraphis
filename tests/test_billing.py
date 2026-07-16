"""Billing webhook tests — the fixes for the dead fulfillment pipeline.

Covers the four regressions that made live purchases silently fail:
  1. the route must be mounted on the DEPLOYED public server (engraphis.app),
     not only on the Inspector;
  2. a ``whsec_``-prefixed Polar secret must verify (prefix stripped);
  3. the vendor seed must load from an INLINE hex env var (container reality);
  4. a redelivered webhook-id must not mint a second key (idempotency).
"""
import asyncio
import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("fastapi", reason="full-stack extra not installed")
from fastapi.testclient import TestClient  # noqa: E402

from engraphis import billing as B  # noqa: E402
from engraphis.licensing import ed25519_public_key, parse_key  # noqa: E402

# Ephemeral test keypair, generated per run. The REAL vendor seed must never live
# in the repo (only in .secrets/ and the Railway env) — anyone with it could forge
# keys. Tests point ENGRAPHIS_LICENSE_PUBKEY at this pair (see the fixture) so
# parse_key validates the keys these tests issue.
VENDOR_SEED = secrets.token_hex(32)
VENDOR_PUB = ed25519_public_key(bytes.fromhex(VENDOR_SEED)).hex()

# A Polar-style secret: "whsec_" + unpadded base64 of 32 random-ish bytes.
_RAW = base64.b64encode(bytes(range(32))).decode("ascii").rstrip("=")
WHSEC = "whsec_" + _RAW


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Hermetic billing state + no ambient SMTP/signing env leaking in."""
    B._mem_seen.clear()
    # Validate issued keys against the ephemeral TEST pubkey, not the pinned one.
    monkeypatch.setenv("ENGRAPHIS_LICENSE_PUBKEY", VENDOR_PUB)
    # Fresh per-test durable dedup DB + fallback dir under tmp_path.
    monkeypatch.setenv("ENGRAPHIS_WEBHOOK_STATE", str(tmp_path / "webhooks.db"))
    monkeypatch.setenv("ENGRAPHIS_RELAY_DB", str(tmp_path / "relay.db"))
    for var in ("ENGRAPHIS_SIGNING_KEY", "ENGRAPHIS_SMTP_HOST", "ENGRAPHIS_SMTP_USER",
                "ENGRAPHIS_SMTP_PASSWORD", "ENGRAPHIS_SMTP_FROM", "ENGRAPHIS_SMTP_PORT"):
        monkeypatch.delenv(var, raising=False)
    yield


def _sign(secret_env, webhook_id, ts, body: bytes) -> str:
    key = B._decode_webhook_secret(secret_env)
    mac = hmac.new(key, f"{webhook_id}.{ts}.{body.decode('utf-8')}".encode("utf-8"),
                   hashlib.sha256).digest()
    return "v1," + base64.b64encode(mac).decode("ascii")


def _post(client, secret_env, webhook_id, body: bytes, ts=None):
    ts = ts or str(int(time.time()))
    return client.post(
        "/webhooks/polar", content=body,
        headers={
            "Content-Type": "application/json",
            "webhook-id": webhook_id,
            "webhook-timestamp": ts,
            "webhook-signature": _sign(secret_env, webhook_id, ts, body),
        },
    )


def _inspector_client(monkeypatch, *, secret=WHSEC, seed=VENDOR_SEED):
    from engraphis.inspector.app import create_app
    from engraphis.service import MemoryService
    if secret is not None:
        monkeypatch.setenv("POLAR_WEBHOOK_SECRET", secret)
    if seed is not None:
        monkeypatch.setenv("ENGRAPHIS_VENDOR_SIGNING_KEY", seed)
    return TestClient(create_app(MemoryService.create(":memory:")))


# ── fix 1: the deployed public server actually serves the route ────────────────
def test_main_public_server_mounts_the_route(monkeypatch):
    # The bug: /webhooks/polar lived only on the Inspector; the deployed server
    # (engraphis.app) 404'd it. Prove it's reachable now: with no secret the route
    # answers 500 "not configured"; an UNMOUNTED route would 404 instead.
    monkeypatch.delenv("POLAR_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("ENGRAPHIS_DB_PATH", ":memory:")
    monkeypatch.setenv("ENGRAPHIS_LOOP_INTERVAL", "0")
    from engraphis.app import create_app as create_main_app
    client = TestClient(create_main_app())
    r = client.post("/webhooks/polar", json={"type": "order.paid", "data": {}})
    assert r.status_code != 404, "regression: /webhooks/polar not mounted on engraphis.app"
    assert r.status_code == 500 and "POLAR_WEBHOOK_SECRET" in r.json()["error"]


# ── fix 2: whsec_ prefix is stripped before HMAC ───────────────────────────────
def test_decode_strips_whsec_prefix():
    assert B._decode_webhook_secret(WHSEC) == B._decode_webhook_secret(_RAW)
    # and it is NOT the same as naively decoding the whole prefixed string
    naive = base64.b64decode(WHSEC + "=" * (-len(WHSEC) % 4))
    assert B._decode_webhook_secret(WHSEC) != naive


def test_order_paid_with_whsec_secret_fulfills(monkeypatch):
    client = _inspector_client(monkeypatch)
    body = (b'{"type":"order.paid","data":{'
            b'"customer":{"email":"buyer@example.com"},'
            b'"product":{"name":"Engraphis Pro"}}}')
    r = _post(client, WHSEC, "evt_pro_1", body)
    assert r.status_code == 202, r.text
    assert r.json()["status"] == "fulfilled"
    assert r.json()["key_issued"] is True


# ── fix 3: inline hex seed from env (no key file in the container) ─────────────
def test_inline_hex_seed_issues_valid_key(monkeypatch):
    from engraphis.inspector.webhooks import issue_key
    monkeypatch.setenv("ENGRAPHIS_VENDOR_SIGNING_KEY", VENDOR_SEED)
    key = issue_key("buyer@example.com", product_name="Engraphis Team", seats=4, days=30)
    lic = parse_key(key)  # validates against the ephemeral test pubkey (see fixture)
    assert lic.plan == "team" and lic.seats == 4 and "team" in lic.features


# ── fix 4: redelivered webhook-id does not mint a second key ───────────────────
def test_duplicate_delivery_is_idempotent(monkeypatch):
    client = _inspector_client(monkeypatch)
    body = (b'{"type":"order.paid","data":{'
            b'"customer":{"email":"buyer@example.com"},'
            b'"product":{"name":"Engraphis Pro"}}}')
    first = _post(client, WHSEC, "evt_dupe", body)
    second = _post(client, WHSEC, "evt_dupe", body)
    assert first.json()["status"] == "fulfilled"
    assert second.status_code == 202 and second.json()["status"] == "duplicate"


# ── hardening: durable dedup survives a "restart" (new app instance) ───────────
def test_reservation_is_durable_across_instances(monkeypatch):
    body = (b'{"type":"order.paid","data":{'
            b'"customer":{"email":"buyer@example.com"},'
            b'"product":{"name":"Engraphis Pro"}}}')
    c1 = _inspector_client(monkeypatch)
    first = _post(c1, WHSEC, "evt_durable", body)
    assert first.json()["status"] == "fulfilled"
    # Simulate a worker restart: clear the in-memory guard; the SQLite reservation
    # (same ENGRAPHIS_WEBHOOK_STATE) must still catch the redelivery.
    B._mem_seen.clear()
    c2 = _inspector_client(monkeypatch)
    second = _post(c2, WHSEC, "evt_durable", body)
    assert second.json()["status"] == "duplicate"


def test_reserve_release_roundtrip():
    assert B.reserve_webhook("wid_x") is True    # first claim
    assert B.reserve_webhook("wid_x") is False   # already claimed
    B.release_webhook("wid_x")                   # failure path frees it
    assert B.reserve_webhook("wid_x") is True     # retry can claim again


def test_stale_processing_reservation_can_be_reclaimed(monkeypatch):
    monkeypatch.setattr(B, "_RESERVATION_TTL_SECONDS", 5)
    assert B.reserve_webhook("wid_crashed") is True
    conn = B._dedup_conn()
    with conn:
        conn.execute(
            "UPDATE processed SET ts=? WHERE webhook_id=?",
            (time.time() - 10, "wid_crashed"))
    conn.close()

    assert B.reserve_webhook("wid_crashed") is True
    B.complete_webhook("wid_crashed")
    conn = B._dedup_conn()
    with conn:
        conn.execute(
            "UPDATE processed SET ts=? WHERE webhook_id=?",
            (time.time() - 10, "wid_crashed"))
    conn.close()
    assert B.reserve_webhook("wid_crashed") is False

def test_sqlite_reservation_failure_is_retryable_without_fulfillment(monkeypatch):
    from engraphis.inspector import webhooks as WH

    conn = B._dedup_conn()
    with conn:
        conn.execute(
            "CREATE TRIGGER fail_reservation BEFORE INSERT ON processed "
            "BEGIN SELECT RAISE(FAIL, 'reservation unavailable'); END")
    conn.close()

    fulfilled = []
    monkeypatch.setattr(WH, "handle_order_paid", lambda data: fulfilled.append(data))
    client = _inspector_client(monkeypatch)
    body = _body({"type": "order.paid", "data": {
        "id": "order_reservation_error",
        "customer": {"email": "buyer@example.com"},
        "product": {"name": "Engraphis Pro"}}})

    response = _post(client, WHSEC, "evt_reservation_error", body)

    assert response.status_code == 503
    assert response.json()["error"] == "webhook state unavailable"
    assert fulfilled == []




# ── hardening: oversized body is rejected before buffering/fulfillment ─────────
def test_oversized_body_rejected(monkeypatch):
    monkeypatch.setenv("POLAR_WEBHOOK_SECRET", WHSEC)
    client = _inspector_client(monkeypatch, secret=WHSEC)
    r = client.post(
        "/webhooks/polar", content=b"x" * (70 * 1024),
        headers={"Content-Type": "application/json", "webhook-id": "evt_big",
                 "webhook-timestamp": str(int(time.time())), "webhook-signature": "v1,x"},
    )
    assert r.status_code == 413

def test_lengthless_chunked_oversize_stops_streaming(monkeypatch):
    monkeypatch.setenv("POLAR_WEBHOOK_SECRET", WHSEC)
    chunk = b"x" * (B._MAX_BODY_BYTES // 2)
    messages = [
        {"type": "http.request", "body": chunk, "more_body": True},
        {"type": "http.request", "body": chunk, "more_body": True},
        {"type": "http.request", "body": b"x", "more_body": True},
        {"type": "http.request", "body": b"must-not-be-read", "more_body": False},
    ]
    reads = 0

    async def receive():
        nonlocal reads
        reads += 1
        return messages.pop(0)

    from fastapi import Request
    request = Request({
        "type": "http", "method": "POST", "path": "/webhooks/polar",
        "headers": [], "query_string": b"",
    }, receive)

    response = asyncio.run(B.polar_webhook(request))

    assert response.status_code == 413
    assert reads == 3


# ── email delivery: Resend HTTPS API (hosts like Railway block outbound SMTP) ──
def test_resend_api_key_prefers_env_then_smtp_password(monkeypatch):
    from engraphis.inspector import webhooks as WH
    monkeypatch.delenv("ENGRAPHIS_RESEND_API_KEY", raising=False)
    monkeypatch.setenv("ENGRAPHIS_SMTP_HOST", "smtp.resend.com")
    monkeypatch.setenv("ENGRAPHIS_SMTP_PASSWORD", "re_testkey_abc")
    assert WH._resend_api_key() == "re_testkey_abc"   # reuse the SMTP Resend key
    monkeypatch.setenv("ENGRAPHIS_RESEND_API_KEY", "re_explicit")
    assert WH._resend_api_key() == "re_explicit"       # explicit env wins


def test_send_license_email_uses_resend_api_not_smtp(monkeypatch):
    from engraphis.inspector import webhooks as WH
    captured = {}

    def fake_api(to, subject, text_body, from_addr, api_key, reply_to=None):
        captured.update(to=to, subject=subject, from_addr=from_addr,
                        api_key=api_key, has_key="ENGR1" in text_body)

    monkeypatch.setattr(WH, "_send_via_resend_api", fake_api)
    monkeypatch.setenv("ENGRAPHIS_RESEND_API_KEY", "re_test")
    monkeypatch.setenv("ENGRAPHIS_SMTP_FROM", "keys@engraphis.com")
    # No SMTP host set — if this fell through to SMTP it would raise instead.
    WH.send_license_email("buyer@example.com", "ENGR1.abc.def", product_name="Pro")
    assert captured["to"] == "buyer@example.com"
    assert captured["api_key"] == "re_test"
    assert captured["from_addr"] == "keys@engraphis.com"
    assert captured["has_key"] and "Pro" in captured["subject"]


def test_email_configured_reflects_resend_or_smtp(monkeypatch):
    from engraphis.inspector import webhooks as WH
    for var in ("ENGRAPHIS_RESEND_API_KEY", "ENGRAPHIS_SMTP_HOST",
               "ENGRAPHIS_SMTP_USER", "ENGRAPHIS_SMTP_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    assert WH.email_configured() is False
    monkeypatch.setenv("ENGRAPHIS_RESEND_API_KEY", "re_x")
    assert WH.email_configured() is True
    monkeypatch.delenv("ENGRAPHIS_RESEND_API_KEY")
    assert WH.email_configured() is False
    monkeypatch.setenv("ENGRAPHIS_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("ENGRAPHIS_SMTP_USER", "u")
    monkeypatch.setenv("ENGRAPHIS_SMTP_PASSWORD", "p")
    assert WH.email_configured() is True


def test_team_invite_email_names_inviter_and_sets_reply_to(monkeypatch):
    from engraphis.inspector import webhooks as WH
    captured = {}
    monkeypatch.setattr(
        WH, "_send_via_resend_api",
        lambda to, subject, text_body, from_addr, api_key, reply_to=None: captured.update(
            text_body=text_body, reply_to=reply_to))
    monkeypatch.setenv("ENGRAPHIS_RESEND_API_KEY", "re_test")
    WH.send_team_invite_email("newmember@example.com", "Mo", "member",
                              invited_by="admin@corp.com")
    assert "admin@corp.com" in captured["text_body"]
    assert captured["reply_to"] == "admin@corp.com"


def test_team_invite_email_ignores_malformed_invited_by(monkeypatch):
    from engraphis.inspector import webhooks as WH
    captured = {}
    monkeypatch.setattr(
        WH, "_send_via_resend_api",
        lambda to, subject, text_body, from_addr, api_key, reply_to=None: captured.update(
            reply_to=reply_to))
    monkeypatch.setenv("ENGRAPHIS_RESEND_API_KEY", "re_test")
    WH.send_team_invite_email("newmember@example.com", "Mo", "member",
                              invited_by="not-an-email")
    assert captured["reply_to"] is None


def test_team_invite_email_uses_resend_api_and_never_contains_a_password(monkeypatch):
    from engraphis.inspector import webhooks as WH
    captured = {}

    def fake_api(to, subject, text_body, from_addr, api_key, reply_to=None):
        captured.update(to=to, subject=subject, text_body=text_body,
                        from_addr=from_addr, api_key=api_key)

    monkeypatch.setattr(WH, "_send_via_resend_api", fake_api)
    monkeypatch.setenv("ENGRAPHIS_RESEND_API_KEY", "re_test")
    monkeypatch.setenv("ENGRAPHIS_SMTP_FROM", "keys@engraphis.com")
    monkeypatch.delenv("ENGRAPHIS_DASHBOARD_URL", raising=False)
    # No SMTP host set — if this fell through to SMTP it would raise instead.
    WH.send_team_invite_email("newmember@example.com", "Mo", "member")
    assert captured["to"] == "newmember@example.com"
    assert captured["api_key"] == "re_test"
    assert "member" in captured["text_body"]
    assert "Mo" in captured["text_body"]
    # the whole point: an invite never carries a live credential
    assert "password" not in captured["text_body"].lower() or \
        "does not contain it" in captured["text_body"]


def test_team_invite_email_includes_dashboard_url_when_configured(monkeypatch):
    from engraphis.inspector import webhooks as WH
    captured = {}
    monkeypatch.setattr(
        WH, "_send_via_resend_api",
        lambda to, subject, text_body, from_addr, api_key, reply_to=None: captured.update(
            text_body=text_body))
    monkeypatch.setenv("ENGRAPHIS_RESEND_API_KEY", "re_test")
    monkeypatch.setenv("ENGRAPHIS_DASHBOARD_URL", "https://dash.example.com")
    WH.send_team_invite_email("newmember@example.com", "", "admin")
    assert "https://dash.example.com" in captured["text_body"]


def test_team_invite_email_defaults_to_hosted_team_dashboard(monkeypatch):
    # When neither the caller nor ENGRAPHIS_DASHBOARD_URL supplies a dashboard URL,
    # the invite falls back to the canonical hosted team dashboard
    # (DEFAULT_TEAM_DASHBOARD_URL) so the member always gets a clickable sign-in
    # link instead of "ask your admin". An explicit ENGRAPHIS_DASHBOARD_URL still
    # wins, so a self-hoster pointing at their own dashboard is honoured.
    from engraphis.inspector import webhooks as WH
    captured = {}
    monkeypatch.setattr(
        WH, "_send_via_resend_api",
        lambda to, subject, text_body, from_addr, api_key, reply_to=None: captured.update(
            text_body=text_body))
    monkeypatch.setenv("ENGRAPHIS_RESEND_API_KEY", "re_test")
    monkeypatch.delenv("ENGRAPHIS_DASHBOARD_URL", raising=False)
    WH.send_team_invite_email("newmember@example.com", "", "admin")
    assert WH.DEFAULT_TEAM_DASHBOARD_URL in captured["text_body"]
    assert "Ask your admin" not in captured["text_body"]


def test_team_invite_email_includes_license_key_when_provided(monkeypatch):
    # A Team-licensed instance hands the member the shared team key so they can turn on
    # Pro features on their own machine — this is what makes them a licensed member, not
    # just a dashboard login.
    from engraphis.inspector import webhooks as WH
    captured = {}
    monkeypatch.setattr(
        WH, "_send_via_resend_api",
        lambda to, subject, text_body, from_addr, api_key, reply_to=None: captured.update(
            text_body=text_body))
    monkeypatch.setenv("ENGRAPHIS_RESEND_API_KEY", "re_test")
    monkeypatch.delenv("ENGRAPHIS_DASHBOARD_URL", raising=False)
    WH.send_team_invite_email("newmember@example.com", "Mo", "member",
                              key="ENGR-TEAM-ABC123")
    body = captured["text_body"]
    assert "ENGR-TEAM-ABC123" in body           # the actual activation key
    assert "Settings -> License" in body        # how to activate it
    assert "Pro features" in body


def test_team_invite_email_omits_activation_when_no_key(monkeypatch):
    # Without a key (instance not Team-licensed), the invite is dashboard-only and must
    # not advertise a Pro-activation section it can't back up — no OPTION 2 block, no
    # "Settings -> License" activation steps, and no key value. Option 1 may still
    # mention the words "license key" as a reassurance ("no license key is needed"),
    # which is the opposite of advertising activation, so the assertion targets the
    # activation section specifically rather than the literal phrase.
    from engraphis.inspector import webhooks as WH
    captured = {}
    monkeypatch.setattr(
        WH, "_send_via_resend_api",
        lambda to, subject, text_body, from_addr, api_key, reply_to=None: captured.update(
            text_body=text_body))
    monkeypatch.setenv("ENGRAPHIS_RESEND_API_KEY", "re_test")
    monkeypatch.delenv("ENGRAPHIS_DASHBOARD_URL", raising=False)
    WH.send_team_invite_email("newmember@example.com", "Mo", "member")
    body = captured["text_body"]
    assert "OPTION 2" not in body
    assert "Shared team license key" not in body
    assert "Settings -> License" not in body
    assert "Pro features" not in body


def test_team_invite_relay_forwards_key_and_dashboard_url(monkeypatch):
    # cloud_license.send_team_invite must POST the key AND dashboard_url to the relay so a
    # relay-delivered invite can carry both activation and the admin's own dashboard link.
    import json
    from engraphis import cloud_license as CL

    seen = {}

    class _Resp:
        def read(self):
            return json.dumps({"sent": True}).encode("utf-8")
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["payload"] = json.loads(req.data.decode("utf-8"))
        return _Resp()

    monkeypatch.setattr(CL.urllib.request, "urlopen", fake_urlopen)
    sent, reason = CL.send_team_invite(
        "https://relay.example", "ENGR-TEAM-XYZ", "m@e.com", "Mo", "member",
        "admin@corp.com", dashboard_url="https://dash.corp.com")
    assert sent is True
    assert seen["url"].endswith("/license/v1/team-invite")
    assert seen["payload"]["key"] == "ENGR-TEAM-XYZ"
    assert seen["payload"]["dashboard_url"] == "https://dash.corp.com"


def test_delivery_failure_persists_key_and_still_202(monkeypatch, tmp_path):
    # A provider/network failure must NOT lose a paid key: it lands in the 0600
    # fallback file and the webhook still returns 202 (no Polar retry storm).
    from engraphis.inspector import webhooks as WH

    def boom(*a, **k):
        raise RuntimeError("simulated Resend outage")

    monkeypatch.setattr(WH, "_send_via_resend_api", boom)
    monkeypatch.setenv("ENGRAPHIS_RESEND_API_KEY", "re_test")
    client = _inspector_client(monkeypatch)
    body = (b'{"type":"order.paid","data":{"customer":{"email":"buyer@example.com"},'
            b'"product":{"name":"Engraphis Pro"}}}')
    r = _post(client, WHSEC, "evt_delivery_fail", body)
    assert r.status_code == 202 and r.json()["key_issued"] is True
    fallback = tmp_path / "undelivered_license_keys.tsv"
    assert fallback.exists() and "buyer@example.com" in fallback.read_text()


# ── signature must still be rejected when wrong ────────────────────────────────
def test_bad_signature_rejected(monkeypatch):
    client = _inspector_client(monkeypatch)
    ts = str(int(time.time()))
    r = client.post(
        "/webhooks/polar", content=b'{"type":"order.paid","data":{}}',
        headers={"Content-Type": "application/json", "webhook-id": "evt_bad",
                 "webhook-timestamp": ts, "webhook-signature": "v1,not-a-real-sig"},
    )
    assert r.status_code == 403


# ── trials: give a key at trial START that expires at trial END (no free Pro) ───
def _iso_in_days(n):
    return (datetime.now(timezone.utc) + timedelta(days=n)).isoformat()


def _body(obj):
    return json.dumps(obj).encode("utf-8")


def test_trial_subscription_issues_short_lived_key(monkeypatch):
    from engraphis.inspector import webhooks as WH
    monkeypatch.setenv("ENGRAPHIS_VENDOR_SIGNING_KEY", VENDOR_SEED)
    key = WH.handle_subscription_created({
        "id": "sub_trial_1", "status": "trialing",
        "customer": {"email": "trialer@example.com"},
        "product": {"name": "Engraphis Pro"},
        "current_period_end": _iso_in_days(3)})
    lic = parse_key(key)
    days_left = (lic.expires - time.time()) / 86400
    assert lic.plan == "pro"
    assert lic.is_trial is True
    # short (covers the ~3-day trial), and nowhere near the 35-day paid key
    assert 2 < days_left <= 5, f"trial key lasted {days_left:.1f}d"


def test_non_trial_subscription_created_is_noop(monkeypatch):
    from engraphis.inspector import webhooks as WH
    monkeypatch.setenv("ENGRAPHIS_VENDOR_SIGNING_KEY", VENDOR_SEED)
    assert WH.handle_subscription_created({
        "id": "sub_paid", "status": "active",
        "customer": {"email": "buyer@example.com"},
        "product": {"name": "Engraphis Pro"}}) is None


def test_paid_order_key_is_full_length_not_trial(monkeypatch):
    from engraphis.inspector import webhooks as WH
    monkeypatch.setenv("ENGRAPHIS_VENDOR_SIGNING_KEY", VENDOR_SEED)
    key = WH.handle_order_paid({
        "id": "order_1", "customer": {"email": "buyer@example.com"},
        "product": {"name": "Engraphis Pro"}})
    lic = parse_key(key)
    days_left = (lic.expires - time.time()) / 86400
    assert days_left > 30, f"paid monthly key should be ~35d, got {days_left:.1f}d"
    assert lic.subscription_id == ""


def test_route_trial_then_conversion_two_distinct_keys(monkeypatch):
    client = _inspector_client(monkeypatch)
    trial = _body({"type": "subscription.created", "data": {
        "id": "subX", "status": "trialing", "customer": {"email": "c@example.com"},
        "product": {"name": "Engraphis Pro"}, "current_period_end": _iso_in_days(3)}})
    order = _body({"type": "order.paid", "data": {
        "id": "orderX", "subscription_id": "subX",
        "customer": {"email": "c@example.com"}, "product": {"name": "Engraphis Pro"}}})
    r1 = _post(client, WHSEC, "evt_trialX", trial)
    r2 = _post(client, WHSEC, "evt_orderX", order)
    assert r1.json() == {"status": "fulfilled", "key_issued": True}
    assert r2.json() == {"status": "fulfilled", "key_issued": True}


def test_route_non_trial_subscription_ignored(monkeypatch):
    client = _inspector_client(monkeypatch)
    body = _body({"type": "subscription.created", "data": {
        "id": "subA", "status": "active", "customer": {"email": "c@example.com"},
        "product": {"name": "Engraphis Pro"}}})
    r = _post(client, WHSEC, "evt_subA", body)
    assert r.status_code == 202 and r.json()["status"] == "ignored"


def test_route_trial_redelivery_no_second_key(monkeypatch):
    # Same trial via a different webhook-id must NOT mint a second key.
    client = _inspector_client(monkeypatch)
    body = _body({"type": "subscription.created", "data": {
        "id": "subDup", "status": "trialing", "customer": {"email": "c@example.com"},
        "product": {"name": "Engraphis Team"}, "current_period_end": _iso_in_days(3)}})
    r1 = _post(client, WHSEC, "evt_td1", body)
    r2 = _post(client, WHSEC, "evt_td2", body)
    assert r1.json()["status"] == "fulfilled" and r1.json()["key_issued"] is True
    assert r2.json()["status"] == "already_fulfilled"


def test_trial_days_helper_is_short_and_bounded():
    from engraphis.inspector import webhooks as WH
    now = time.time()
    assert 3 <= WH._trial_days(WH._parse_ts(_iso_in_days(3)), now=now) <= 4
    assert WH._trial_days(None, now=now) == 4          # env default fallback
    assert WH._trial_days(WH._parse_ts(_iso_in_days(-1)), now=now) == 4  # past -> fallback


# ── seats: "Engraphis Team" uses Polar's native seat-based pricing, NOT a flat
# price + quantity/metadata scheme. Polar's Order schema documents its top-level
# ``seats`` field as populated "for seat-based one-time orders" only; for our
# recurring Team subscription the real count lives nested at
# ``order.subscription.seats``. A regression here silently caps every real Team
# buyer's key at 1 seat regardless of how many they paid for. ───────────────────
def test_extract_seats_from_top_level_field():
    from engraphis.inspector.webhooks import _extract_seats
    # subscription.created / subscription.updated payloads ARE a Subscription
    # object, so Polar puts `seats` at the top level.
    assert _extract_seats({"seats": 5, "customer": {}}) == 5


def test_extract_seats_from_nested_order_subscription():
    from engraphis.inspector.webhooks import _extract_seats
    # order.paid for a RECURRING seat-based product: top-level `seats` is null
    # per Polar's schema (that field is one-time-order-only); the real count is
    # nested under `subscription.seats`.
    payload = {
        "id": "order_1", "seats": None,
        "customer": {"email": "buyer@example.com"},
        "product": {"name": "Engraphis Team"},
        "subscription": {"id": "sub_1", "seats": 3},
    }
    assert _extract_seats(payload) == 3


def test_extract_seats_falls_back_to_metadata_when_absent():
    from engraphis.inspector.webhooks import _extract_seats
    payload = {"product": {"name": "Engraphis Team", "metadata": {"seats": 2}}}
    assert _extract_seats(payload) == 2


def test_extract_seats_defaults_to_one_when_nothing_present():
    from engraphis.inspector.webhooks import _extract_seats
    assert _extract_seats({"product": {"name": "Engraphis Team"}}) == 1


def test_extract_seats_prefers_real_fields_over_metadata():
    from engraphis.inspector.webhooks import _extract_seats
    # A stale/wrong metadata default must never override the actual purchase.
    payload = {
        "subscription": {"seats": 7},
        "product": {"name": "Engraphis Team", "metadata": {"seats": 1}},
    }
    assert _extract_seats(payload) == 7


def test_order_paid_recurring_team_order_issues_correct_seat_count(monkeypatch):
    # End-to-end regression for the bug: before the fix, this issued a 1-seat key.
    from engraphis.inspector import webhooks as WH
    monkeypatch.setenv("ENGRAPHIS_VENDOR_SIGNING_KEY", VENDOR_SEED)
    key = WH.handle_order_paid({
        "id": "order_team_1", "seats": None,
        "customer": {"email": "lead@example.com"},
        "product": {"name": "Engraphis Team"},
        "subscription": {"id": "sub_team_1", "seats": 5},
    })
    lic = parse_key(key)
    assert lic.plan == "team" and lic.seats == 5


def test_subscription_created_trial_team_uses_top_level_seats(monkeypatch):
    from engraphis.inspector import webhooks as WH
    monkeypatch.setenv("ENGRAPHIS_VENDOR_SIGNING_KEY", VENDOR_SEED)
    key = WH.handle_subscription_created({
        "id": "sub_team_trial", "status": "trialing", "seats": 4,
        "customer": {"email": "lead@example.com"},
        "product": {"name": "Engraphis Team"},
        "current_period_end": _iso_in_days(3)})
    lic = parse_key(key)
    assert lic.plan == "team" and lic.seats == 4


# ── mid-cycle seat sync: subscription.updated fires for MANY unrelated status
# transitions AND for genuine seat-count changes. Must reissue ONLY on a real
# seat-count change for an active subscription, never while trialing, never on
# first sighting, and never on unrelated status churn. ─────────────────────────
def _sub_updated_body(sub_id, status, seats, product="Engraphis Team"):
    return _body({"type": "subscription.updated", "data": {
        "id": sub_id, "status": status, "seats": seats,
        "customer": {"email": "lead@example.com"}, "product": {"name": product}}})


def test_first_sighting_of_subscription_updated_only_records_baseline(monkeypatch):
    # The update that immediately follows creation must NOT mint a second key —
    # order.paid/trial already issued the correct one.
    client = _inspector_client(monkeypatch)
    body = _sub_updated_body("sub_new", "active", 3)
    r = _post(client, WHSEC, "evt_su_first", body)
    assert r.status_code == 202
    assert r.json() == {"status": "ignored", "reason": "baseline recorded",
                        "type": "subscription.updated"}


def test_seat_increase_reissues_key_with_new_count(monkeypatch):
    client = _inspector_client(monkeypatch)
    baseline = _sub_updated_body("sub_grow", "active", 3)
    _post(client, WHSEC, "evt_su_baseline", baseline)
    grown = _sub_updated_body("sub_grow", "active", 7)
    r = _post(client, WHSEC, "evt_su_grown", grown)
    assert r.json() == {"status": "fulfilled", "key_issued": True}


def test_order_paid_records_baseline_before_first_real_seat_change(monkeypatch):
    client = _inspector_client(monkeypatch)
    order = _body({"type": "order.paid", "data": {
        "id": "order_team_baseline",
        "customer": {"email": "lead@example.com"},
        "product": {"name": "Engraphis Team"},
        "subscription": {"id": "sub_from_order", "seats": 3},
    }})
    assert _post(
        client, WHSEC, "evt_order_baseline", order).json()["status"] == "fulfilled"
    assert B.get_known_seats("sub_from_order") == 3

    changed = _sub_updated_body("sub_from_order", "active", 7)
    result = _post(client, WHSEC, "evt_first_real_change", changed)
    assert result.json() == {"status": "fulfilled", "key_issued": True}
    assert B.get_known_seats("sub_from_order") == 7


def test_seat_decrease_reissues_key_with_new_count(monkeypatch):
    client = _inspector_client(monkeypatch)
    baseline = _sub_updated_body("sub_shrink", "active", 5)
    _post(client, WHSEC, "evt_su_shrink_base", baseline)
    shrunk = _sub_updated_body("sub_shrink", "active", 2)
    r = _post(client, WHSEC, "evt_su_shrunk", shrunk)
    assert r.json() == {"status": "fulfilled", "key_issued": True}
    from engraphis.inspector.webhooks import issue_key  # noqa: F401 (import sanity)


def test_unrelated_status_update_does_not_reissue(monkeypatch):
    # e.g. subscription.updated fired for a cancel-at-period-end flag flip; seats
    # unchanged, so this must be a no-op even though status is still "active".
    client = _inspector_client(monkeypatch)
    baseline = _sub_updated_body("sub_cancel_flag", "active", 4)
    _post(client, WHSEC, "evt_su_cf_base", baseline)
    same_seats = _sub_updated_body("sub_cancel_flag", "active", 4)
    r = _post(client, WHSEC, "evt_su_cf_same", same_seats)
    assert r.json()["status"] == "ignored"


def test_revoked_subscription_update_never_reissues(monkeypatch):
    client = _inspector_client(monkeypatch)
    baseline = _sub_updated_body("sub_revoke", "active", 3)
    _post(client, WHSEC, "evt_su_rv_base", baseline)
    revoked = _sub_updated_body("sub_revoke", "canceled", 3)
    r = _post(client, WHSEC, "evt_su_rv", revoked)
    assert r.json()["status"] == "ignored"
    assert r.json()["reason"] == "not an active subscription"


def test_seat_sync_key_reflects_correct_new_count(monkeypatch):
    from engraphis.inspector import webhooks as WH
    monkeypatch.setenv("ENGRAPHIS_VENDOR_SIGNING_KEY", VENDOR_SEED)
    key = WH.handle_subscription_updated({
        "id": "sub_direct", "status": "active",
        "customer": {"email": "lead@example.com"},
        "product": {"name": "Engraphis Team"}, "seats": 9})
    lic = parse_key(key)
    assert lic.plan == "team" and lic.seats == 9
    assert lic.subscription_id == "sub_direct"


def test_get_record_known_seats_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("ENGRAPHIS_WEBHOOK_STATE", str(tmp_path / "wh.db"))
    assert B.get_known_seats("sub_xyz") is None
    B.record_known_seats("sub_xyz", 3)
    assert B.get_known_seats("sub_xyz") == 3
    B.record_known_seats("sub_xyz", 6)  # upsert overwrites
    assert B.get_known_seats("sub_xyz") == 6


def test_failed_seatsync_fulfillment_does_not_advance_baseline(monkeypatch):
    """Crash-window guard: if the re-issue on a real seat change fails, the seat baseline
    must NOT advance — otherwise Polar's retry would see prior == new and skip the
    re-issue forever. The baseline is persisted only after a key actually goes out."""
    from engraphis.inspector import webhooks as WH
    client = _inspector_client(monkeypatch)
    orig = WH.handle_subscription_updated

    # First sighting seeds the baseline at 3 (no fulfillment).
    _post(client, WHSEC, "evt_cw_base", _sub_updated_body("sub_cw", "active", 3))
    assert B.get_known_seats("sub_cw") == 3

    # Real change 3 -> 8, but fulfillment blows up (e.g. email provider down).
    monkeypatch.setattr(WH, "handle_subscription_updated",
                        lambda _p: (_ for _ in ()).throw(RuntimeError("provider down")))
    r = _post(client, WHSEC, "evt_cw_fail", _sub_updated_body("sub_cw", "active", 8))
    assert r.status_code == 500
    assert B.get_known_seats("sub_cw") == 3          # baseline UNCHANGED — retry can re-detect

    # Polar retries the same change; fulfillment now succeeds and advances the baseline.
    monkeypatch.setattr(WH, "handle_subscription_updated", orig)
    r2 = _post(client, WHSEC, "evt_cw_retry", _sub_updated_body("sub_cw", "active", 8))
    assert r2.json() == {"status": "fulfilled", "key_issued": True}
    assert B.get_known_seats("sub_cw") == 8


def test_trialing_seat_update_cannot_mint_paid_key(monkeypatch):
    from engraphis.inspector import webhooks as WH

    trial_payload = {
        "id": "sub_trial_update", "status": "trialing", "seats": 5,
        "customer": {"email": "lead@example.com"},
        "product": {"name": "Engraphis Team"}}
    assert WH.handle_subscription_updated(trial_payload) is None
    B.record_known_seats("sub_trial_update", 2)
    issued = []
    monkeypatch.setattr(
        WH, "handle_subscription_updated", lambda payload: issued.append(payload) or "key")
    client = _inspector_client(monkeypatch)

    response = _post(
        client, WHSEC, "evt_trial_seats",
        _sub_updated_body("sub_trial_update", "trialing", 5))

    assert response.status_code == 202
    assert response.json()["status"] == "ignored"
    assert issued == []
    assert B.get_known_seats("sub_trial_update") == 2


def test_seat_fulfillment_is_versioned_across_repeated_counts(monkeypatch):
    from engraphis.inspector import webhooks as WH

    sent = []
    monkeypatch.setattr(
        WH, "send_license_email",
        lambda _to, key, **_kwargs: sent.append(parse_key(key).seats))
    client = _inspector_client(monkeypatch)
    B.record_known_seats("sub_cycle", 2)

    first = _post(
        client, WHSEC, "evt_seat_v1",
        _sub_updated_body("sub_cycle", "active", 4))
    retry = _post(
        client, WHSEC, "evt_seat_v1",
        _sub_updated_body("sub_cycle", "active", 4))
    second = _post(
        client, WHSEC, "evt_seat_v2",
        _sub_updated_body("sub_cycle", "active", 2))
    repeated = _post(
        client, WHSEC, "evt_seat_v3",
        _sub_updated_body("sub_cycle", "active", 4))

    assert first.json()["key_issued"] is True
    assert retry.json()["status"] == "ignored"
    assert second.json()["key_issued"] is True
    assert repeated.json()["key_issued"] is True
    assert sent == [4, 2, 4]


def test_baseline_and_completion_failure_releases_claims_for_retry(monkeypatch):
    from engraphis.inspector import webhooks as WH

    monkeypatch.setattr(WH, "send_license_email", lambda *_args, **_kwargs: None)
    client = _inspector_client(monkeypatch)
    B.record_known_seats("sub_atomic", 3)
    conn = B._dedup_conn()
    with conn:
        conn.execute(
            "CREATE TRIGGER fail_baseline BEFORE UPDATE ON subscription_seats "
            "BEGIN SELECT RAISE(FAIL, 'baseline unavailable'); END")
    conn.close()
    body = _sub_updated_body("sub_atomic", "active", 8)

    failed = _post(client, WHSEC, "evt_atomic", body)

    assert failed.status_code == 503
    assert B.get_known_seats("sub_atomic") == 3
    conn = B._dedup_conn()
    pending = conn.execute(
        "SELECT COUNT(*) FROM processed WHERE webhook_id IN (?, ?)",
        ("dlv:evt_atomic", "ful:seatsync:sub_atomic:evt_atomic")).fetchone()[0]
    with conn:
        conn.execute("DROP TRIGGER fail_baseline")
    conn.close()
    assert pending == 0

    retried = _post(client, WHSEC, "evt_atomic", body)
    assert retried.json() == {"status": "fulfilled", "key_issued": True}
    assert B.get_known_seats("sub_atomic") == 8


def test_order_replacement_records_subscription_then_revokes_older_key(monkeypatch):
    from engraphis.inspector import license_registry as LR
    from engraphis.inspector import webhooks as WH

    monkeypatch.setenv("ENGRAPHIS_VENDOR_SIGNING_KEY", VENDOR_SEED)
    old_key = WH.issue_key(
        "buyer@example.com", product_name="Engraphis Team", seats=2, days=10,
        subscription_id="sub_renew")
    old_id = parse_key(old_key).key_id
    sent = []
    monkeypatch.setattr(
        WH, "send_license_email", lambda _to, key, **_kwargs: sent.append(key))
    client = _inspector_client(monkeypatch)
    body = _body({"type": "order.paid", "data": {
        "id": "order_renewal",
        "customer": {"email": "buyer@example.com"},
        "product": {"name": "Engraphis Team"},
        "subscription": {"id": "  sub_renew  ", "seats": 3}}})

    response = _post(client, WHSEC, "evt_renewal", body)

    assert response.json() == {"status": "fulfilled", "key_issued": True}
    replacement = parse_key(sent[0])
    assert replacement.subscription_id == "sub_renew"
    assert LR.is_revoked(old_id) is True
    assert LR.is_revoked(replacement.key_id) is False


def test_registry_failure_never_revokes_existing_subscription_key(monkeypatch):
    from engraphis.inspector import license_registry as LR
    from engraphis.inspector import webhooks as WH

    monkeypatch.setenv("ENGRAPHIS_VENDOR_SIGNING_KEY", VENDOR_SEED)
    old_key = WH.issue_key(
        "buyer@example.com", product_name="Engraphis Team", seats=2, days=10,
        subscription_id="sub_registry_guard")
    old_id = parse_key(old_key).key_id
    revoke_calls = []

    def fail_record(_key):
        raise sqlite3.OperationalError("registry unavailable")

    monkeypatch.setattr(LR, "record_issued", fail_record)
    monkeypatch.setattr(
        LR, "revoke_superseded",
        lambda *args, **kwargs: revoke_calls.append((args, kwargs)))

    WH.issue_key(
        "buyer@example.com", product_name="Engraphis Team", seats=5, days=35,
        subscription_id="sub_registry_guard")

    assert revoke_calls == []
    assert LR.is_revoked(old_id) is False
