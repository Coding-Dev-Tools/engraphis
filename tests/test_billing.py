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
import os
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="full-stack extra not installed")
from fastapi.testclient import TestClient  # noqa: E402

from engraphis import billing as B  # noqa: E402
from engraphis.licensing import ed25519_public_key, parse_key  # noqa: E402
from engraphis.config import DEFAULT_LICENSE_SERVER_URL  # noqa: E402

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
    for var in ("ENGRAPHIS_SIGNING_KEY", "ENGRAPHIS_KEY_CLOUD_URL",
                "ENGRAPHIS_SMTP_HOST", "ENGRAPHIS_SMTP_USER",
                "ENGRAPHIS_SMTP_PASSWORD", "ENGRAPHIS_SMTP_FROM",
                "ENGRAPHIS_SMTP_PORT"):
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


@pytest.mark.parametrize("raw_secret", [
    "polar_whs_ovyN6cPrTv56AApvzCaJno08SSmGJmgbWilb33N2JuK",
    "6t3c8ce2247c493a3ade20aea4484d64",
])
def test_polar_raw_secret_formats_verify_live_delivery(monkeypatch, raw_secret):
    """Polar supplies raw secrets; Standard Webhooks signs their UTF-8 bytes."""
    client = _inspector_client(monkeypatch, secret=raw_secret)
    body = (b'{"type":"order.paid","data":{'
            b'"customer":{"email":"buyer@example.com"},'
            b'"product":{"name":"Engraphis Pro"}}}')
    webhook_id = "evt_polar_raw_secret"
    stamp = str(int(time.time()))
    signed = f"{webhook_id}.{stamp}.".encode("utf-8") + body
    signature = "v1," + base64.b64encode(
        hmac.new(raw_secret.encode("utf-8"), signed, hashlib.sha256).digest()
    ).decode("ascii")

    response = client.post(
        "/webhooks/polar", content=body,
        headers={
            "Content-Type": "application/json",
            "webhook-id": webhook_id,
            "webhook-timestamp": stamp,
            "webhook-signature": signature,
        },
    )
    assert response.status_code == 202, response.text
    assert response.json()["key_issued"] is True


def test_polar_signature_requires_standard_webhooks_v1_prefix(monkeypatch):
    client = _inspector_client(monkeypatch)
    body = _body({"type": "ignored.event", "data": {}})
    webhook_id = "evt_missing_signature_version"
    stamp = str(int(time.time()))
    signature_without_version = _sign(WHSEC, webhook_id, stamp, body).split(",", 1)[1]
    response = client.post(
        "/webhooks/polar", content=body,
        headers={
            "webhook-id": webhook_id,
            "webhook-timestamp": stamp,
            "webhook-signature": signature_without_version,
        },
    )
    assert response.status_code == 403
    assert response.json()["error"] == "invalid signature format"


def test_polar_webhook_rejects_bruteforceable_short_secret(monkeypatch):
    client = _inspector_client(monkeypatch, secret="short")
    body = _body({"type": "ignored.event", "data": {}})
    response = _post(client, base64.b64encode(b"short").decode(), "evt_short", body)
    assert response.status_code == 500
    assert response.json()["error"] == "POLAR_WEBHOOK_SECRET is invalid"


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


@pytest.mark.skipif(os.name == "nt", reason="Windows does not expose POSIX owner modes")
def test_signing_seed_file_must_be_owner_only(tmp_path):
    from engraphis.inspector.webhooks import _read_seed_file

    seed_file = tmp_path / "vendor-signing.key"
    seed_file.write_text(VENDOR_SEED, encoding="ascii")
    seed_file.chmod(0o644)
    with pytest.raises(RuntimeError, match="owner-only"):
        _read_seed_file(seed_file)
    seed_file.chmod(0o600)
    assert _read_seed_file(seed_file) == bytes.fromhex(VENDOR_SEED)


def test_issued_key_migrates_retired_relay_url(monkeypatch):
    from engraphis.inspector.webhooks import issue_key
    monkeypatch.setenv("ENGRAPHIS_VENDOR_SIGNING_KEY", VENDOR_SEED)
    monkeypatch.setenv(
        "ENGRAPHIS_KEY_CLOUD_URL",
        "https://engraphis-production.up.railway.app/",
    )
    key = issue_key("buyer@example.com", product_name="Engraphis Pro", days=30)
    assert parse_key(key).cloud_url == DEFAULT_LICENSE_SERVER_URL


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


@pytest.mark.parametrize(
    "payload, expected_error",
    [
        ([], "webhook event must be an object"),
        ({"type": 7, "data": {}}, "webhook event type must be a string"),
        ({"type": "order.paid", "data": []},
         "webhook event data must be an object"),
    ],
)
def test_signed_malformed_event_shapes_are_rejected(monkeypatch, payload, expected_error):
    """A valid signature does not make a structurally invalid provider event safe."""
    client = _inspector_client(monkeypatch, secret=WHSEC)
    response = _post(
        client, WHSEC, "evt_malformed_shape", json.dumps(payload).encode("utf-8"))
    assert response.status_code == 400
    assert response.json()["error"] == expected_error


def test_nonfinite_webhook_timestamp_is_rejected(monkeypatch):
    client = _inspector_client(monkeypatch, secret=WHSEC)
    body = b'{"type":"order.paid","data":{}}'
    response = _post(client, WHSEC, "evt_nan_time", body, ts="nan")
    assert response.status_code == 400
    assert response.json()["error"] == "invalid webhook timestamp"

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

    def fake_api(to, subject, text_body, from_addr, api_key, reply_to=None,
                 idempotency_key=""):
        captured.update(to=to, subject=subject, from_addr=from_addr,
                        api_key=api_key, has_key="ENGR1" in text_body,
                        idempotency_key=idempotency_key)

    monkeypatch.setattr(WH, "_send_via_resend_api", fake_api)
    monkeypatch.setenv("ENGRAPHIS_RESEND_API_KEY", "re_test")
    monkeypatch.setenv("ENGRAPHIS_SMTP_FROM", "keys@engraphis.com")
    # No SMTP host set — if this fell through to SMTP it would raise instead.
    WH.send_license_email("buyer@example.com", "ENGR1.abc.def", product_name="Pro")
    assert captured["to"] == "buyer@example.com"
    assert captured["api_key"] == "re_test"
    assert captured["from_addr"] == "keys@engraphis.com"
    assert captured["has_key"] and "Pro" in captured["subject"]
    assert captured["idempotency_key"].startswith("eml_")


def test_resend_api_sets_stable_idempotency_header(monkeypatch):
    import httpx
    from engraphis.inspector import webhooks as WH
    captured = {}

    class Response:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {"id": "provider-message-id"}

    def fake_post(url, *, json, headers, timeout):
        captured.update(url=url, json=json, headers=headers, timeout=timeout)
        return Response()

    monkeypatch.setattr(httpx, "post", fake_post)
    provider_id = WH._send_via_resend_api(
        "buyer@example.com", "Subject", "Body", "keys@engraphis.com", "re_test",
        idempotency_key="eml_stable123")
    assert provider_id == "provider-message-id"
    assert captured["headers"]["Idempotency-Key"] == "eml_stable123"


def test_resend_success_without_provider_id_stays_retryable(monkeypatch):
    import httpx
    from engraphis.inspector import webhooks as WH

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {}

    monkeypatch.setattr(httpx, "post", lambda *_args, **_kwargs: Response())
    with pytest.raises(RuntimeError, match="omitted its message id"):
        WH._send_via_resend_api(
            "buyer@example.com", "Subject", "Body", "keys@engraphis.com", "re_test",
            idempotency_key="eml_stable123")


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
        lambda to, subject, text_body, from_addr, api_key, reply_to=None,
        idempotency_key="": captured.update(
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
        lambda to, subject, text_body, from_addr, api_key, reply_to=None,
        idempotency_key="": captured.update(
            reply_to=reply_to))
    monkeypatch.setenv("ENGRAPHIS_RESEND_API_KEY", "re_test")
    WH.send_team_invite_email("newmember@example.com", "Mo", "member",
                              invited_by="not-an-email")
    assert captured["reply_to"] is None


def test_team_invite_email_uses_resend_api_and_contains_only_password_setup(monkeypatch):
    from engraphis.inspector import webhooks as WH
    captured = {}

    def fake_api(to, subject, text_body, from_addr, api_key, reply_to=None,
                 idempotency_key=""):
        captured.update(to=to, subject=subject, text_body=text_body,
                        from_addr=from_addr, api_key=api_key)

    monkeypatch.setattr(WH, "_send_via_resend_api", fake_api)
    monkeypatch.setenv("ENGRAPHIS_RESEND_API_KEY", "re_test")
    monkeypatch.setenv("ENGRAPHIS_SMTP_FROM", "keys@engraphis.com")
    monkeypatch.delenv("ENGRAPHIS_DASHBOARD_URL", raising=False)
    # No SMTP host set — if this fell through to SMTP it would raise instead.
    invite_url = "https://team.example/#invite_token=one-time-token"
    WH.send_team_invite_email(
        "newmember@example.com", "Mo", "member", invite_url=invite_url)
    assert captured["to"] == "newmember@example.com"
    assert captured["api_key"] == "re_test"
    assert "member" in captured["text_body"]
    assert "Mo" in captured["text_body"]
    # The recipient chooses a password through the one-time link; no temporary
    # credential or account-wide license key is delivered in the message.
    assert invite_url in captured["text_body"]
    assert "does not contain a temporary password" in captured["text_body"]
    assert "account-wide\nlicense key" in captured["text_body"]


def test_team_invite_email_includes_dashboard_url_when_configured(monkeypatch):
    from engraphis.inspector import webhooks as WH
    captured = {}
    monkeypatch.setattr(
        WH, "_send_via_resend_api",
        lambda to, subject, text_body, from_addr, api_key, reply_to=None,
        idempotency_key="": captured.update(
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
        lambda to, subject, text_body, from_addr, api_key, reply_to=None,
        idempotency_key="": captured.update(
            text_body=text_body))
    monkeypatch.setenv("ENGRAPHIS_RESEND_API_KEY", "re_test")
    monkeypatch.delenv("ENGRAPHIS_DASHBOARD_URL", raising=False)
    WH.send_team_invite_email("newmember@example.com", "", "admin")
    assert WH.DEFAULT_TEAM_DASHBOARD_URL.rstrip("/") in captured["text_body"]
    assert "Ask your admin" not in captured["text_body"]


def test_team_invite_email_ignores_deprecated_license_key_argument(monkeypatch):
    # The old compatibility argument is authentication material for the relay, never
    # recipient content. Agent/sync access now uses per-user scoped device tokens.
    from engraphis.inspector import webhooks as WH
    captured = {}
    monkeypatch.setattr(
        WH, "_send_via_resend_api",
        lambda to, subject, text_body, from_addr, api_key, reply_to=None,
        idempotency_key="": captured.update(
            text_body=text_body))
    monkeypatch.setenv("ENGRAPHIS_RESEND_API_KEY", "re_test")
    monkeypatch.delenv("ENGRAPHIS_DASHBOARD_URL", raising=False)
    WH.send_team_invite_email("newmember@example.com", "Mo", "member",
                              key="ENGR-TEAM-ABC123")
    body = captured["text_body"]
    assert "ENGR-TEAM-ABC123" not in body
    assert "Settings -> Connect an agent" in body
    assert "account-wide\nlicense key" in body


def test_team_invite_email_omits_activation_when_no_key(monkeypatch):
    # Invitations never advertise shared-key activation. The recipient gets only the
    # account-acceptance path and can later mint a scoped device token.
    from engraphis.inspector import webhooks as WH
    captured = {}
    monkeypatch.setattr(
        WH, "_send_via_resend_api",
        lambda to, subject, text_body, from_addr, api_key, reply_to=None,
        idempotency_key="": captured.update(
            text_body=text_body))
    monkeypatch.setenv("ENGRAPHIS_RESEND_API_KEY", "re_test")
    monkeypatch.delenv("ENGRAPHIS_DASHBOARD_URL", raising=False)
    WH.send_team_invite_email("newmember@example.com", "Mo", "member")
    body = captured["text_body"]
    assert "OPTION 2" not in body
    assert "Shared team license key" not in body
    assert "Settings -> License" not in body
    assert "Pro features" not in body


def test_team_invite_relay_forwards_auth_key_and_one_time_url(monkeypatch):
    # The account key authenticates the sending deployment to the vendor relay; it is
    # never recipient content. The email payload is the one-time invitation URL.
    import json
    from engraphis import cloud_license as CL

    seen = {}

    class _Resp:
        def read(self, limit=-1):
            return json.dumps({"sent": True}).encode("utf-8")[:limit]
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["payload"] = json.loads(req.data.decode("utf-8"))
        return _Resp()

    monkeypatch.setattr(CL, "_urlopen_no_redirect", fake_urlopen)
    sent, reason = CL.send_team_invite(
        "https://relay.example", "ENGR-TEAM-XYZ", "m@e.com", "Mo", "member",
        "admin@corp.com",
        invite_url="https://dash.corp.com/#invite_token=one-time-token")
    assert sent is True
    assert seen["url"].endswith("/license/v1/team-invite")
    assert seen["payload"]["key"] == "ENGR-TEAM-XYZ"
    assert seen["payload"]["invite_url"] == (
        "https://dash.corp.com/#invite_token=one-time-token")


def test_delivery_failure_persists_key_and_still_202(monkeypatch, tmp_path, caplog):
    # If durable enqueue itself fails, the only paid key lands in the encrypted-backup-
    # covered 0600 fallback and the webhook still returns 202 (no Polar retry storm).
    from engraphis.inspector import webhooks as WH
    caplog.set_level("INFO")

    def boom(*a, **k):
        raise RuntimeError("simulated Resend outage")

    monkeypatch.setattr(WH, "send_license_email", boom)
    client = _inspector_client(monkeypatch)
    body = (b'{"type":"order.paid","data":{"customer":{"email":"buyer@example.com"},'
            b'"product":{"name":"Engraphis Pro"}}}')
    r = _post(client, WHSEC, "evt_delivery_fail", body)
    assert r.status_code == 202 and r.json()["key_issued"] is True
    fallback = tmp_path / "undelivered_license_keys.tsv"
    assert fallback.exists() and "buyer@example.com" in fallback.read_text()
    raw_key = fallback.read_text(encoding="utf-8").rstrip("\n").split("\t")[-1]
    # Redacting formatters are optional. Sensitive/provider values must already be
    # absent from records at the source logger call.
    assert "buyer@example.com" not in caplog.text
    assert "evt_delivery_fail" not in caplog.text
    assert raw_key not in caplog.text


def test_purchase_stays_retryable_when_all_delivery_persistence_fails(
        monkeypatch, tmp_path):
    from engraphis.inspector import webhooks as WH

    def outbox_down(*_args, **_kwargs):
        raise RuntimeError("outbox down")

    monkeypatch.setattr(WH, "send_license_email", outbox_down)
    monkeypatch.setattr(WH, "_persist_fallback_key", lambda *_args: None)
    client = _inspector_client(monkeypatch)
    body = (b'{"type":"order.paid","data":{"id":"order_recovery_down",'
            b'"customer":{"email":"buyer@example.com"},'
            b'"product":{"name":"Engraphis Pro"}}}')

    failed = _post(client, WHSEC, "evt_recovery_down", body)
    assert failed.status_code == 500
    assert failed.json()["error"] == "license fulfillment failed; retry delivery"
    assert not (tmp_path / "undelivered_license_keys.tsv").exists()

    delivered = []
    monkeypatch.setattr(
        WH, "send_license_email",
        lambda _to, key, **_kwargs: delivered.append(key),
    )
    retry = _post(client, WHSEC, "evt_recovery_down", body)
    assert retry.status_code == 202
    assert retry.json() == {"status": "fulfilled", "key_issued": True}
    assert len(delivered) == 1


def test_provider_outage_keeps_key_only_in_durable_outbox(monkeypatch, tmp_path):
    from engraphis import email_outbox
    from engraphis.inspector import webhooks as WH
    monkeypatch.setenv("ENGRAPHIS_VENDOR_SIGNING_KEY", VENDOR_SEED)

    def provider_down(*_args, **_kwargs):
        raise RuntimeError("simulated provider outage")

    monkeypatch.setattr(email_outbox, "deliver_now", provider_down)
    key = WH._issue_and_email(
        "buyer@example.com", "Engraphis Pro", 1, 35,
        order_id="order-outbox-recovery", fulfillment_id="order:outbox-recovery")

    assert key.startswith("ENGR1.")
    assert not (tmp_path / "undelivered_license_keys.tsv").exists()
    conn = email_outbox._connect()
    try:
        row = conn.execute(
            "SELECT status,text_body FROM email_outbox WHERE idempotency_key=?",
            ("purchase-license:order-outbox-recovery",),
        ).fetchone()
    finally:
        conn.close()
    assert row["status"] == "pending"
    assert key in row["text_body"]


def test_manual_key_fallback_rejects_links_and_malformed_keys(monkeypatch, tmp_path):
    from engraphis.inspector import webhooks as WH

    monkeypatch.setenv("ENGRAPHIS_WEBHOOK_STATE", str(tmp_path / "polar-webhooks.db"))
    target = tmp_path / "operator-notes.txt"
    target.write_text("preserve\n", encoding="utf-8")
    fallback = tmp_path / WH.UNDELIVERED_LICENSE_KEYS_NAME
    try:
        fallback.symlink_to(target)
    except (NotImplementedError, OSError):
        pytest.skip("this platform cannot create test symlinks")
    assert WH._persist_fallback_key(
        "buyer@example.com", "ENGR1.payload.signature", "Pro") is None
    assert target.read_text(encoding="utf-8") == "preserve\n"
    fallback.unlink()
    assert WH._persist_fallback_key(
        "buyer@example.com", "ENGR1.payload.signature\nsecond-record", "Pro") is None
    assert not fallback.exists()


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

def _registry_rows():
    from engraphis.inspector import license_registry as reg
    conn = reg.connect()
    try:
        rows = conn.execute(
            "SELECT key_id, status, subscription_id, order_id FROM issued_licenses "
            "ORDER BY created_at").fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


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

def test_order_paid_records_polar_ids_for_refunds(monkeypatch):
    client = _inspector_client(monkeypatch)
    order = _body({"type": "order.paid", "data": {
        "id": "order_ids", "subscription_id": "sub_ids",
        "customer": {"email": "ids@example.com"},
        "product": {"name": "Engraphis Pro"}}})
    r = _post(client, WHSEC, "evt_order_ids", order)
    assert r.json() == {"status": "fulfilled", "key_issued": True}
    rows = _registry_rows()
    assert len(rows) == 1
    assert rows[0]["status"] == "active"
    assert rows[0]["subscription_id"] == "sub_ids"
    assert rows[0]["order_id"] == "order_ids"


def test_order_refunded_revokes_subscription_keys_immediately(monkeypatch):
    from engraphis.inspector import license_registry as reg

    client = _inspector_client(monkeypatch)
    order = _body({"type": "order.paid", "data": {
        "id": "order_refund", "subscription_id": "sub_refund",
        "customer": {"email": "refund@example.com"},
        "product": {"name": "Engraphis Pro"}}})
    assert _post(client, WHSEC, "evt_refund_paid", order).json()["key_issued"] is True
    key_id = _registry_rows()[0]["key_id"]

    refund = _body({"type": "order.refunded", "data": {
        "id": "order_refund", "subscription_id": "sub_refund"}})
    r = _post(client, WHSEC, "evt_refund", refund)
    assert r.status_code == 202
    assert r.json()["status"] == "revoked"
    assert r.json()["reason"] == "refund"
    assert r.json()["revoked"] == 1
    assert "subscription_id" not in r.json()
    assert reg.is_revoked(key_id) is True


def test_order_refunded_without_subscription_revokes_by_order(monkeypatch):
    from engraphis.inspector import license_registry as reg

    client = _inspector_client(monkeypatch)
    order = _body({"type": "order.paid", "data": {
        "id": "order_only",
        "customer": {"email": "order-only@example.com"},
        "product": {"name": "Engraphis Pro"}}})
    assert _post(client, WHSEC, "evt_order_only_paid", order).json()["key_issued"] is True
    key_id = _registry_rows()[0]["key_id"]

    refund = _body({"type": "order.refunded", "data": {"id": "order_only"}})
    r = _post(client, WHSEC, "evt_order_only_refund", refund)
    assert r.status_code == 202
    assert r.json()["status"] == "revoked"
    assert "order_id" not in r.json()
    assert reg.is_revoked(key_id) is True


def test_webhook_responses_never_reflect_provider_payload_fields(monkeypatch):
    client = _inspector_client(monkeypatch)
    marker = "provider-controlled-marker-never-reflect"
    response = _post(
        client, WHSEC, "evt_no_reflection",
        _body({"type": marker, "data": {
            "id": marker, "customer": {"email": marker + "@example.com"}}}))
    assert response.status_code == 202
    assert response.json() == {"status": "ignored"}
    assert marker not in response.text


def test_subscription_canceled_honors_paid_period(monkeypatch):
    client = _inspector_client(monkeypatch)
    order = _body({"type": "order.paid", "data": {
        "id": "order_cancel", "subscription_id": "sub_cancel",
        "customer": {"email": "cancel@example.com"},
        "product": {"name": "Engraphis Pro"}}})
    assert _post(client, WHSEC, "evt_cancel_paid", order).json()["key_issued"] is True

    cancel = _body({"type": "subscription.canceled", "data": {"id": "sub_cancel"}})
    r = _post(client, WHSEC, "evt_cancel", cancel)
    assert r.status_code == 202
    assert r.json() == {"status": "ignored", "reason": "paid period honored"}
    assert _registry_rows()[0]["status"] == "active"


def test_subscription_revoked_ends_access_after_paid_period(monkeypatch):
    from engraphis.inspector import license_registry as reg

    client = _inspector_client(monkeypatch)
    order = _body({"type": "order.paid", "data": {
        "id": "order_revoke", "subscription_id": "sub_revoke_end",
        "customer": {"email": "revoke@example.com"},
        "product": {"name": "Engraphis Pro"}}})
    assert _post(client, WHSEC, "evt_revoke_paid", order).json()["key_issued"] is True
    key_id = _registry_rows()[0]["key_id"]

    revoked = _body({"type": "subscription.revoked", "data": {"id": "sub_revoke_end"}})
    r = _post(client, WHSEC, "evt_revoke", revoked)
    assert r.status_code == 202
    assert r.json()["status"] == "revoked"
    assert r.json()["reason"] == "subscription_revoked"
    assert r.json()["revoked"] == 1
    assert reg.is_revoked(key_id) is True


def test_subscription_updated_revoked_revokes_keys(monkeypatch):
    from engraphis.inspector import license_registry as reg

    client = _inspector_client(monkeypatch)
    order = _body({"type": "order.paid", "data": {
        "id": "order_update_revoke", "subscription_id": "sub_update_revoke",
        "customer": {"email": "update-revoke@example.com"},
        "product": {"name": "Engraphis Pro"}}})
    assert _post(client, WHSEC, "evt_update_revoke_paid", order).json()["key_issued"] is True
    key_id = _registry_rows()[0]["key_id"]

    revoked = _body({"type": "subscription.updated", "data": {
        "id": "sub_update_revoke", "status": "revoked", "seats": 1,
        "customer": {"email": "update-revoke@example.com"},
        "product": {"name": "Engraphis Pro"}}})
    r = _post(client, WHSEC, "evt_update_revoke", revoked)
    assert r.status_code == 202
    assert r.json()["status"] == "revoked"
    assert r.json()["reason"] == "subscription_revoked"
    assert reg.is_revoked(key_id) is True


def test_unmappable_revoke_event_is_retryable_not_silently_dropped(monkeypatch):
    """A revoking event we cannot map to a key must NOT answer 2xx.

    Polar stops redelivering once it sees a 2xx, so returning 202 for an unmappable
    revoke would silently drop the revocation entirely — a refunded customer keeps a
    working paid key with nothing left to retry. A 5xx keeps the delivery on Polar's
    retry queue where it stays visible."""
    client = _inspector_client(monkeypatch)
    # A revoking event whose payload carries no subscription id and no order id at all.
    orphan = _body({"type": "subscription.revoked", "data": {"customer": {}}})

    first = _post(client, WHSEC, "evt_revoke_no_target", orphan)
    assert first.status_code >= 500, (
        "unmappable revoke must be retryable on first delivery, got %s" % first.status_code)
    assert first.json().get("error") == "missing revoke target"


def test_unmappable_revoke_event_converges_instead_of_retrying_forever(monkeypatch):
    """...but it must not 5xx forever either.

    A payload with no ids will NEVER become mappable, so an unconditional 5xx means every
    redelivery fails identically and sustained failures can get the whole endpoint
    disabled — which would then drop real order.paid fulfillments. One retryable answer,
    then converge to 2xx."""
    client = _inspector_client(monkeypatch)
    orphan = _body({"type": "subscription.revoked", "data": {"customer": {}}})

    assert _post(client, WHSEC, "evt_revoke_converge", orphan).status_code >= 500
    # Simulate a provider retry arriving after the normal processing-claim TTL. The
    # first response must have durably latched "seen once" rather than relying on an
    # in-flight reservation that would be reclaimed and 5xx forever.
    conn = B._dedup_conn()
    try:
        row = conn.execute(
            "SELECT state FROM processed WHERE webhook_id=?",
            ("unmappable:evt_revoke_converge",),
        ).fetchone()
        assert row[0] == "fulfilled"
        conn.execute(
            "UPDATE processed SET ts=0 WHERE webhook_id=?",
            ("unmappable:evt_revoke_converge",),
        )
        conn.commit()
    finally:
        conn.close()
    # Same webhook-id redelivered: proven deterministic, so stop the retry loop.
    replay = _post(client, WHSEC, "evt_revoke_converge", orphan)
    assert replay.status_code == 202, (
        "redelivery of an unmappable revoke must converge, got %s" % replay.status_code)
    assert replay.json().get("status") == "unmappable"
    # And it stays converged.
    assert _post(client, WHSEC, "evt_revoke_converge", orphan).status_code == 202

    # A DIFFERENT delivery still gets its own first-time retryable answer — convergence
    # is per-delivery, not a global latch that would mute a later real failure.
    assert _post(client, WHSEC, "evt_revoke_other", orphan).status_code >= 500


def test_vendor_revoked_subscription_update_does_not_require_product(monkeypatch):
    from engraphis.inspector import license_registry as reg
    from engraphis.inspector import webhooks as WH

    product_id = _configure_vendor_product(monkeypatch, "POLAR_PRO_MONTHLY_PRODUCT_ID")
    client = _inspector_client(monkeypatch)
    key = WH.issue_key(
        "buyer@example.com", "Pro", subscription_id="sub_vendor_revoked",
        product_id=product_id, days=30,
    )
    revoked = _body({"type": "subscription.updated", "data": {
        "id": "sub_vendor_revoked", "status": "revoked",
        "organization_id": "org_engraphis",
    }})
    response = _post(client, WHSEC, "evt_vendor_update_revoke", revoked)
    assert response.status_code == 202
    assert response.json()["status"] == "revoked"
    assert reg.is_revoked(parse_key(key).key_id) is True


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
    assert WH._trial_days(WH._parse_ts(_iso_in_days(3)), now=now) == 3
    assert WH._trial_days(None, now=now) == 3          # canonical trial fallback
    assert WH._trial_days(WH._parse_ts(_iso_in_days(-1)), now=now) == 3  # past -> fallback


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
    assert r.json() == {"status": "ignored", "reason": "baseline recorded"}


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


# ── #3: in-flight vs completed must not be conflated ───────────────────────────
def test_claim_webhook_is_tristate(monkeypatch, tmp_path):
    monkeypatch.setenv("ENGRAPHIS_WEBHOOK_STATE", str(tmp_path / "wh.db"))
    assert B.claim_webhook("wid") == "claimed"       # fresh slot
    assert B.claim_webhook("wid") == "in_flight"     # held, younger than the TTL
    B.complete_webhook("wid")
    assert B.claim_webhook("wid") == "fulfilled"     # completed → a true duplicate


def test_in_flight_delivery_is_retryable_not_duplicate(monkeypatch):
    # The crash-window regression: a delivery whose earlier attempt is still in flight
    # (or crashed mid-fulfillment, within the TTL) must get a RETRYABLE 503 so Polar
    # keeps retrying — NOT a 2xx "duplicate", which would cancel retries and lose the key.
    client = _inspector_client(monkeypatch)
    assert B.claim_webhook("dlv:evt_inflight") == "claimed"   # simulate an in-flight attempt
    body = (b'{"type":"order.paid","data":{"customer":{"email":"buyer@example.com"},'
            b'"product":{"name":"Engraphis Pro"}}}')
    r = _post(client, WHSEC, "evt_inflight", body)
    assert r.status_code == 503
    assert r.json()["status"] == "processing"


# ── #2: refund / cancellation / revocation revokes issued keys ─────────────────
def test_order_refunded_revokes_subscription_keys(monkeypatch):
    from engraphis.inspector import license_registry as LR
    from engraphis.inspector import webhooks as WH
    monkeypatch.setenv("ENGRAPHIS_VENDOR_SIGNING_KEY", VENDOR_SEED)
    key = WH.issue_key("buyer@example.com", product_name="Engraphis Team", seats=3,
                       days=395, subscription_id="sub_refund")
    kid = parse_key(key).key_id
    assert LR.is_revoked(kid) is False
    client = _inspector_client(monkeypatch)
    body = _body({"type": "order.refunded", "data": {
        "subscription_id": "sub_refund", "customer": {"email": "buyer@example.com"}}})
    r = _post(client, WHSEC, "evt_refund", body)
    assert r.status_code == 202
    assert r.json()["status"] == "revoked" and r.json()["keys_revoked"] == 1
    assert LR.is_revoked(kid) is True


def test_subscription_revoked_removes_access_by_top_level_id(monkeypatch):
    # subscription.* payloads ARE a Subscription object, so the id is top-level (not
    # subscription_id). An explicit access revocation must still find and revoke the key.
    from engraphis.inspector import license_registry as LR
    from engraphis.inspector import webhooks as WH
    monkeypatch.setenv("ENGRAPHIS_VENDOR_SIGNING_KEY", VENDOR_SEED)
    key = WH.issue_key("buyer@example.com", product_name="Engraphis Pro", seats=1,
                       days=395, subscription_id="sub_revoked")
    kid = parse_key(key).key_id
    client = _inspector_client(monkeypatch)
    body = _body({"type": "subscription.revoked", "data": {
        "id": "sub_revoked", "status": "canceled",
        "customer": {"email": "buyer@example.com"}}})
    r = _post(client, WHSEC, "evt_revoked", body)
    assert r.status_code == 202 and r.json()["status"] == "revoked"
    assert LR.is_revoked(kid) is True


def test_subscription_canceled_keeps_plan_until_period_end(monkeypatch):
    # Cancel-at-period-end must NOT revoke: the buyer paid for the period and keeps their
    # plan until their period-bounded key naturally expires. Only refund / revoked pulls
    # access immediately.
    from engraphis.inspector import license_registry as LR
    from engraphis.inspector import webhooks as WH
    monkeypatch.setenv("ENGRAPHIS_VENDOR_SIGNING_KEY", VENDOR_SEED)
    key = WH.issue_key("buyer@example.com", product_name="Engraphis Team", seats=3,
                       days=395, subscription_id="sub_keep")
    kid = parse_key(key).key_id
    client = _inspector_client(monkeypatch)
    body = _body({"type": "subscription.canceled", "data": {
        "id": "sub_keep", "status": "active",
        "customer": {"email": "buyer@example.com"}}})
    r = _post(client, WHSEC, "evt_keep", body)
    assert r.status_code == 202 and r.json()["status"] == "ignored"
    assert LR.is_revoked(kid) is False          # key stays valid until it expires


# ── #4: a mid-cycle reissue is bounded to the paid period, not a full new window ─
def test_seat_change_key_is_bounded_to_current_period_end(monkeypatch):
    from engraphis.inspector import webhooks as WH
    monkeypatch.setenv("ENGRAPHIS_VENDOR_SIGNING_KEY", VENDOR_SEED)
    key = WH.handle_subscription_updated({
        "id": "sub_pe", "status": "active", "seats": 5,
        "customer": {"email": "lead@example.com"},
        "product": {"name": "Engraphis Team Annual"},
        "current_period_end": _iso_in_days(20)})
    lic = parse_key(key)
    days_left = (lic.expires - time.time()) / 86400
    # bounded to ~20d period end (+5d grace), NOT a fresh 395-day annual window from now
    assert 20 <= days_left <= 30, f"expected period-bounded key, got {days_left:.1f}d"


def test_seat_change_without_period_end_falls_back_to_key_days(monkeypatch):
    from engraphis.inspector import webhooks as WH
    monkeypatch.setenv("ENGRAPHIS_VENDOR_SIGNING_KEY", VENDOR_SEED)
    key = WH.handle_subscription_updated({
        "id": "sub_nope", "status": "active", "seats": 5,
        "customer": {"email": "lead@example.com"},
        "product": {"name": "Engraphis Team"}})            # monthly, no period end
    days_left = (parse_key(key).expires - time.time()) / 86400
    assert 30 < days_left <= 36, f"expected ~35d monthly fallback, got {days_left:.1f}d"


# ── #5: an out-of-order (older) subscription.updated must not regress a newer count ─
def _sub_updated_body_ts(sub_id, seats, modified_at, status="active",
                         product="Engraphis Team"):
    return _body({"type": "subscription.updated", "data": {
        "id": sub_id, "status": status, "seats": seats, "modified_at": modified_at,
        "customer": {"email": "lead@example.com"}, "product": {"name": product}}})


def test_out_of_order_subscription_update_is_ignored(monkeypatch):
    client = _inspector_client(monkeypatch)
    base = datetime.now(timezone.utc)
    t0 = base.isoformat()
    t1 = (base + timedelta(days=1)).isoformat()
    t2 = (base + timedelta(days=2)).isoformat()
    # first sighting seeds baseline (seats=3 @ t0)
    r0 = _post(client, WHSEC, "evt_oo_seed", _sub_updated_body_ts("sub_oo", 3, t0))
    assert r0.json()["reason"] == "baseline recorded"
    # newer delivery: 3 -> 7 @ t2 -> fulfilled
    r2 = _post(client, WHSEC, "evt_oo_new", _sub_updated_body_ts("sub_oo", 7, t2))
    assert r2.json()["key_issued"] is True
    # OLDER delivery arrives late: seats=5 @ t1 (< t2) -> ignored as out-of-order
    r1 = _post(client, WHSEC, "evt_oo_old", _sub_updated_body_ts("sub_oo", 5, t1))
    assert r1.json()["status"] == "ignored"
    assert r1.json()["reason"] == "out-of-order update"
    assert B.get_known_seats("sub_oo") == 7           # NOT regressed to 5


# ── #7: organization enforcement + explicit product-tier override ──────────────
def test_organization_id_mismatch_is_rejected(monkeypatch):
    monkeypatch.setenv("POLAR_ORGANIZATION_ID", "org_expected")
    client = _inspector_client(monkeypatch)
    body = _body({"type": "order.paid", "data": {
        "organization_id": "org_other",
        "customer": {"email": "buyer@example.com"},
        "product": {"name": "Engraphis Pro"}}})
    r = _post(client, WHSEC, "evt_org_bad", body)
    assert r.status_code == 403 and r.json()["error"] == "organization mismatch"


def test_organization_id_match_is_allowed(monkeypatch):
    monkeypatch.setenv("POLAR_ORGANIZATION_ID", "org_ok")
    client = _inspector_client(monkeypatch)
    body = _body({"type": "order.paid", "data": {
        "organization_id": "org_ok",
        "customer": {"email": "buyer@example.com"},
        "product": {"name": "Engraphis Pro"}}})
    r = _post(client, WHSEC, "evt_org_ok", body)
    assert r.status_code == 202 and r.json()["key_issued"] is True


def test_product_map_override_pins_tier(monkeypatch):
    from engraphis.inspector import webhooks as WH
    monkeypatch.setenv("ENGRAPHIS_POLAR_PRODUCT_MAP", '{"Engraphis Enterprise": "team"}')
    assert WH._map_polar_product_to_plan("Engraphis Enterprise") == "team"
    monkeypatch.delenv("ENGRAPHIS_POLAR_PRODUCT_MAP", raising=False)
    # without the override an unrecognized product still defaults to Pro (never free)
    assert WH._map_polar_product_to_plan("Engraphis Enterprise") == "pro"


# ── GA vendor control-plane gates ─────────────────────────────────────────────
def _configure_vendor_product(monkeypatch, env_name):
    from engraphis.commercial import expected_product_ids
    from engraphis.config import settings

    product_id = expected_product_ids()[env_name]["id"]
    monkeypatch.setattr(settings, "service_mode", "vendor")
    monkeypatch.setenv(env_name, product_id)
    monkeypatch.setenv("POLAR_ORGANIZATION_ID", "org_engraphis")
    return product_id


def test_vendor_exact_product_id_controls_plan_interval_and_retry(monkeypatch):
    from engraphis.inspector import webhooks as WH

    product_id = _configure_vendor_product(
        monkeypatch, "POLAR_TEAM_ANNUAL_PRODUCT_ID")
    sent = []
    monkeypatch.setattr(
        WH, "send_license_email",
        lambda _to, key, **kwargs: sent.append((key, kwargs)),
    )
    client = _inspector_client(monkeypatch)
    body = _body({"type": "order.paid", "data": {
        "id": "order_vendor_team_annual",
        "organization_id": "org_engraphis",
        "customer": {"email": "buyer@example.com"},
        # The editable display name is intentionally misleading. The validated id wins.
        "product": {"id": product_id, "name": "Engraphis Pro Monthly"},
        "subscription": {"id": "sub_vendor_team", "seats": 6},
    }})

    first = _post(client, WHSEC, "evt_vendor_paid_1", body)
    retry = _post(client, WHSEC, "evt_vendor_paid_2", body)

    assert first.json() == {"status": "fulfilled", "key_issued": True}
    assert retry.json() == {"status": "already_fulfilled", "key_issued": False}
    assert len(sent) == 1
    license_key, email_options = sent[0]
    lic = parse_key(license_key)
    assert lic.plan == "team" and lic.seats == 6
    assert 390 <= (lic.expires - time.time()) / 86400 <= 396
    assert email_options["product_name"] == "Team"
    assert email_options["idempotency_key"] == \
        "purchase-license:order_vendor_team_annual"


def test_vendor_exact_product_interval_cannot_be_overridden_by_metadata(monkeypatch):
    from engraphis.inspector import webhooks as WH

    product_id = _configure_vendor_product(monkeypatch, "POLAR_PRO_MONTHLY_PRODUCT_ID")
    assert WH._key_days(
        "Misleading Annual Name", {"license_days": 36500}, product_id) == 35


def test_vendor_rejects_signed_paid_event_with_missing_organization(monkeypatch):
    from engraphis.inspector import webhooks as WH

    product_id = _configure_vendor_product(monkeypatch, "POLAR_PRO_MONTHLY_PRODUCT_ID")
    issued = []
    monkeypatch.setattr(WH, "issue_key", lambda *args, **kwargs: issued.append((args, kwargs)))
    client = _inspector_client(monkeypatch)
    body = _body({"type": "order.paid", "data": {
        "id": "order_missing_org",
        "customer": {"email": "buyer@example.com"},
        "product": {"id": product_id, "name": "Engraphis Pro Monthly"},
    }})

    response = _post(client, WHSEC, "evt_missing_org", body)

    assert response.status_code == 403
    assert response.json()["error"] == "organization mismatch"
    assert issued == []


def test_vendor_rejects_paid_key_without_revocable_fulfillment_identity(monkeypatch):
    from engraphis.inspector import webhooks as WH

    product_id = _configure_vendor_product(monkeypatch, "POLAR_PRO_MONTHLY_PRODUCT_ID")
    issued = []
    monkeypatch.setattr(WH, "issue_key", lambda *args, **kwargs: issued.append((args, kwargs)))
    client = _inspector_client(monkeypatch)
    body = _body({"type": "order.paid", "data": {
        "organization_id": "org_engraphis",
        "customer": {"email": "buyer@example.com"},
        "product": {"id": product_id, "name": "Engraphis Pro Monthly"},
    }})

    response = _post(client, WHSEC, "evt_missing_fulfillment_identity", body)

    assert response.status_code == 400
    assert "order or subscription id" in response.json()["error"]
    assert issued == []


def test_paid_order_without_delivery_target_stays_retryable(monkeypatch):
    product_id = _configure_vendor_product(monkeypatch, "POLAR_PRO_MONTHLY_PRODUCT_ID")
    client = _inspector_client(monkeypatch)
    body = _body({"type": "order.paid", "data": {
        "id": "order_no_email",
        "organization_id": "org_engraphis",
        "product": {"id": product_id, "name": "Engraphis Pro Monthly"},
    }})

    first = _post(client, WHSEC, "evt_no_email", body)
    retry = _post(client, WHSEC, "evt_no_email", body)

    assert first.status_code == 503 and retry.status_code == 503
    assert first.json()["error"] == "license fulfillment incomplete; retry delivery"


def test_paid_order_with_malformed_email_stays_retryable_without_issuance(monkeypatch):
    from engraphis.inspector import webhooks as WH

    product_id = _configure_vendor_product(monkeypatch, "POLAR_PRO_MONTHLY_PRODUCT_ID")
    issued = []
    monkeypatch.setattr(WH, "issue_key", lambda *args, **kwargs: issued.append((args, kwargs)))
    client = _inspector_client(monkeypatch)
    body = _body({"type": "order.paid", "data": {
        "id": "order_bad_email",
        "organization_id": "org_engraphis",
        "customer": {"email": 12345},
        "product": {"id": product_id, "name": "Engraphis Pro Monthly"},
    }})

    response = _post(client, WHSEC, "evt_bad_email", body)

    assert response.status_code == 503
    assert response.json()["error"] == "license fulfillment incomplete; retry delivery"
    assert issued == []


def test_retry_after_finalize_failure_reuses_durable_purchase_email(monkeypatch):
    from engraphis import email_outbox
    from engraphis.inspector import webhooks as WH

    product_id = _configure_vendor_product(monkeypatch, "POLAR_PRO_MONTHLY_PRODUCT_ID")
    monkeypatch.setattr(
        WH, "_deliver_text_email",
        lambda *_args, **_kwargs: ("resend", "provider-finalize-retry"))
    real_issue = WH.issue_key
    issued = []

    def counted_issue(*args, **kwargs):
        key = real_issue(*args, **kwargs)
        issued.append(key)
        return key

    monkeypatch.setattr(WH, "issue_key", counted_issue)
    real_finalize = B._finalize_webhook
    finalize_calls = 0

    def flaky_finalize(*args, **kwargs):
        nonlocal finalize_calls
        finalize_calls += 1
        if finalize_calls == 1:
            raise B.WebhookStateError("simulated commit failure")
        return real_finalize(*args, **kwargs)

    monkeypatch.setattr(B, "_finalize_webhook", flaky_finalize)
    client = _inspector_client(monkeypatch)
    body = _body({"type": "order.paid", "data": {
        "id": "order_finalize_retry",
        "organization_id": "org_engraphis",
        "customer": {"email": "buyer@example.com"},
        "product": {"id": product_id, "name": "Engraphis Pro Monthly"},
    }})

    first = _post(client, WHSEC, "evt_finalize_retry", body)
    conn = email_outbox._connect()
    try:
        retained = conn.execute(
            "SELECT text_body,retention_claim,status FROM email_outbox "
            "WHERE idempotency_key=?",
            ("purchase-license:order_finalize_retry",)).fetchone()
        assert "ENGR1." in retained["text_body"]
        assert retained["retention_claim"] == "ful:order:order_finalize_retry"
        assert retained["status"] == "sent"
    finally:
        conn.close()
    retry = _post(client, WHSEC, "evt_finalize_retry", body)

    assert first.status_code == 503
    assert retry.json() == {"status": "fulfilled", "key_issued": True}
    assert len(issued) == 1
    conn = email_outbox._connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n,text_body,retention_claim FROM email_outbox "
            "WHERE idempotency_key=?",
            ("purchase-license:order_finalize_retry",)).fetchone()
        assert row["n"] == 1
        assert row["text_body"] == "" and row["retention_claim"] == ""
    finally:
        conn.close()


def test_host_death_before_outbox_enqueue_reuses_registry_journal_key(monkeypatch):
    from engraphis.inspector import license_registry as LR
    from engraphis.inspector import webhooks as WH

    monkeypatch.setenv("ENGRAPHIS_VENDOR_SIGNING_KEY", VENDOR_SEED)
    fulfillment_id = "order:journal-crash"

    def host_died(*_args, **_kwargs):
        # BaseException deliberately escapes the ordinary delivery recovery handler,
        # modeling process death after the registry transaction but before enqueue.
        raise SystemExit("simulated host death")

    monkeypatch.setattr(WH, "send_license_email", host_died)
    with pytest.raises(SystemExit, match="simulated host death"):
        WH._issue_and_email(
            "buyer@example.com", "Engraphis Pro", 1, 35,
            order_id="order-journal-crash", fulfillment_id=fulfillment_id)

    claim = "ful:" + fulfillment_id
    original = LR.fulfillment_key(claim)
    assert original and original.startswith("ENGR1.")
    sent = []
    monkeypatch.setattr(
        WH, "send_license_email",
        lambda _to, key, **_kwargs: sent.append(key))

    retry = WH._issue_and_email(
        "buyer@example.com", "Engraphis Pro", 1, 35,
        order_id="order-journal-crash", fulfillment_id=fulfillment_id)

    assert retry == original
    assert sent == [original]
    conn = LR.connect()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM issued_licenses WHERE order_id=?",
            ("order-journal-crash",)).fetchone()[0] == 1
    finally:
        conn.close()


def test_post_commit_cleanup_failure_does_not_reopen_fulfilled_claims(monkeypatch):
    from engraphis.inspector import license_registry as LR

    delivery_id = "dlv:cleanup-crash"
    fulfillment_id = "ful:cleanup-crash"
    assert B.claim_webhook(delivery_id) == "claimed"
    assert B.claim_webhook(fulfillment_id) == "claimed"
    monkeypatch.setattr(
        LR, "redact_fulfillment_key",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            sqlite3.OperationalError("registry unavailable")))

    with pytest.raises(B.WebhookStateError, match="could not redact"):
        B._finalize_webhook(delivery_id, fulfillment_id)
    # This is the route's normal best-effort rollback after any finalize exception.
    # It may release processing claims, but completed tombstones are permanent.
    B._release_claims(delivery_id, fulfillment_id)

    assert B.claim_webhook(delivery_id) == "fulfilled"
    assert B.claim_webhook(fulfillment_id) == "fulfilled"


def test_seat_change_finalize_retry_reuses_durable_key_and_email(monkeypatch):
    from engraphis import email_outbox
    from engraphis.inspector import webhooks as WH

    monkeypatch.setattr(
        WH, "_deliver_text_email",
        lambda *_args, **_kwargs: ("resend", "provider-seat-finalize"))
    real_issue = WH.issue_key
    issued = []

    def counted_issue(*args, **kwargs):
        key = real_issue(*args, **kwargs)
        issued.append(key)
        return key

    monkeypatch.setattr(WH, "issue_key", counted_issue)
    B.record_known_seats("sub_seat_finalize", 2)
    real_finalize = B._finalize_webhook
    finalize_calls = 0

    def flaky_finalize(*args, **kwargs):
        nonlocal finalize_calls
        finalize_calls += 1
        if finalize_calls == 1:
            raise B.WebhookStateError("simulated commit failure")
        return real_finalize(*args, **kwargs)

    monkeypatch.setattr(B, "_finalize_webhook", flaky_finalize)
    client = _inspector_client(monkeypatch)
    body = _sub_updated_body("sub_seat_finalize", "active", 7)

    first = _post(client, WHSEC, "evt_seat_finalize", body)
    retry = _post(client, WHSEC, "evt_seat_finalize", body)

    assert first.status_code == 503
    assert retry.json() == {"status": "fulfilled", "key_issued": True}
    assert len(issued) == 1
    expected = "license-fulfillment:" + hashlib.sha256(
        b"seatsync:sub_seat_finalize:evt_seat_finalize").hexdigest()
    conn = email_outbox._connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n,text_body,retention_claim FROM email_outbox "
            "WHERE idempotency_key=?", (expected,)).fetchone()
        assert row["n"] == 1
        assert row["text_body"] == "" and row["retention_claim"] == ""
    finally:
        conn.close()


def test_deferred_license_send_redacts_after_already_finalized_claim(monkeypatch):
    from engraphis import email_outbox
    from engraphis.inspector import webhooks as WH

    product_id = _configure_vendor_product(
        monkeypatch, "POLAR_PRO_MONTHLY_PRODUCT_ID")

    def provider_down(*_args, **_kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(WH, "_deliver_text_email", provider_down)
    client = _inspector_client(monkeypatch)
    body = _body({"type": "order.paid", "data": {
        "id": "order_deferred_redaction",
        "organization_id": "org_engraphis",
        "customer": {"email": "buyer@example.com"},
        "product": {"id": product_id, "name": "Engraphis Pro Monthly"},
    }})
    response = _post(client, WHSEC, "evt_deferred_redaction", body)
    assert response.json() == {"status": "fulfilled", "key_issued": True}

    conn = email_outbox._connect()
    try:
        row = conn.execute(
            "SELECT id,status,text_body,retention_claim FROM email_outbox "
            "WHERE idempotency_key='purchase-license:order_deferred_redaction'"
        ).fetchone()
        assert row["status"] == "retry"
        assert "ENGR1." in row["text_body"]
        assert row["retention_claim"] == "ful:order:order_deferred_redaction"
        conn.execute(
            "UPDATE email_outbox SET next_attempt_at=0 WHERE id=?", (row["id"],))
        conn.commit()
        message_id = row["id"]
    finally:
        conn.close()

    assert email_outbox.deliver_now(
        message_id, lambda *_args: ("resend", "provider-deferred"))
    conn = email_outbox._connect()
    try:
        row = conn.execute(
            "SELECT status,text_body,retention_claim FROM email_outbox WHERE id=?",
            (message_id,)).fetchone()
        assert row["status"] == "sent"
        assert row["text_body"] == "" and row["retention_claim"] == ""
    finally:
        conn.close()


def test_concurrent_seat_updates_serialize_before_issuing(monkeypatch):
    from engraphis.inspector import webhooks as WH

    B.record_known_seats("sub_serial", 2)
    started = threading.Event()
    release = threading.Event()
    issued = []

    def controlled(payload):
        issued.append(WH._extract_seats(payload))
        if len(issued) == 1:
            started.set()
            assert release.wait(timeout=5)
        return "signed-key-placeholder"

    monkeypatch.setattr(WH, "handle_subscription_updated", controlled)
    client = _inspector_client(monkeypatch)
    first_body = _sub_updated_body("sub_serial", "active", 5)
    second_body = _sub_updated_body("sub_serial", "active", 7)
    first_result = {}

    def first_request():
        first_result["response"] = _post(
            client, WHSEC, "evt_serial_first", first_body)

    worker = threading.Thread(target=first_request)
    worker.start()
    assert started.wait(timeout=5)
    try:
        concurrent = _post(client, WHSEC, "evt_serial_second", second_body)
        assert concurrent.status_code == 503
        assert concurrent.json()["status"] == "processing"
        assert issued == [5]
    finally:
        release.set()
        worker.join(timeout=5)
    assert not worker.is_alive()
    assert first_result["response"].status_code == 202

    retried = _post(client, WHSEC, "evt_serial_second", second_body)
    assert retried.status_code == 202
    assert issued == [5, 7]
    assert B.get_known_seats("sub_serial") == 7


def test_vendor_registry_failure_never_emails_unusable_paid_key(monkeypatch):
    from engraphis.inspector import license_registry as LR
    from engraphis.inspector import webhooks as WH

    product_id = _configure_vendor_product(monkeypatch, "POLAR_PRO_MONTHLY_PRODUCT_ID")

    def fail_record(*_args, **_kwargs):
        raise sqlite3.OperationalError("registry down")

    monkeypatch.setattr(LR, "record_fulfillment_key", fail_record)
    sent = []
    monkeypatch.setattr(
        WH, "send_license_email",
        lambda *args, **kwargs: sent.append((args, kwargs)),
    )
    client = _inspector_client(monkeypatch)
    body = _body({"type": "order.paid", "data": {
        "id": "order_registry_down",
        "organization_id": "org_engraphis",
        "customer": {"email": "buyer@example.com"},
        "product": {"id": product_id, "name": "Engraphis Pro Monthly"},
    }})

    response = _post(client, WHSEC, "evt_registry_down", body)

    assert response.status_code == 500
    assert response.json()["error"] == "license fulfillment failed; retry delivery"
    assert sent == []


def test_vendor_webhook_state_defaults_beside_registry(monkeypatch, tmp_path):
    from engraphis.config import settings

    monkeypatch.setattr(settings, "service_mode", "vendor")
    monkeypatch.delenv("ENGRAPHIS_WEBHOOK_STATE", raising=False)
    monkeypatch.delenv("ENGRAPHIS_DB_PATH", raising=False)
    monkeypatch.delenv("ENGRAPHIS_STATE_DIR", raising=False)
    monkeypatch.setenv("ENGRAPHIS_RELAY_DB", str(tmp_path / "state" / "relay.db"))

    expected = (tmp_path / "state" / "polar-webhooks.db").resolve()
    assert Path(B._dedup_path()) == expected
    assert B.webhook_state_ready(require_durable=True) is True
    assert expected.is_file()


def test_vendor_webhook_state_rejects_explicit_memory_override(monkeypatch):
    from engraphis.config import settings

    monkeypatch.setattr(settings, "service_mode", "vendor")
    monkeypatch.setenv("ENGRAPHIS_WEBHOOK_STATE", ":memory:")
    assert B.webhook_state_ready(require_durable=True) is False


def test_vendor_readiness_fails_closed_when_polar_ledger_unavailable(monkeypatch):
    from engraphis import commercial
    from engraphis.config import settings

    monkeypatch.setattr(settings, "service_mode", "vendor")
    monkeypatch.setattr(B, "webhook_state_ready", lambda **_kwargs: False)

    checks = commercial.vendor_readiness()

    assert checks["polar_idempotency"] is False
    assert checks["ready"] is False


def test_webhook_backlog_health_fails_on_stale_processing_claim():
    assert B.webhook_backlog_healthy() is True
    conn = B._dedup_conn()
    try:
        conn.execute(
            "INSERT INTO processed(webhook_id,ts,state) VALUES (?,?,?)",
            ("stuck", time.time() - B._RESERVATION_TTL_SECONDS - 1, "processing"))
        conn.commit()
    finally:
        conn.close()
    assert B.webhook_backlog_healthy() is False
