"""Billing webhook tests — the fixes for the dead fulfillment pipeline.

Covers the four regressions that made live purchases silently fail:
  1. the route must be mounted on the DEPLOYED public server (engraphis.app),
     not only on the Inspector;
  2. a ``whsec_``-prefixed Polar secret must verify (prefix stripped);
  3. the vendor seed must load from an INLINE hex env var (container reality);
  4. a redelivered webhook-id must not mint a second key (idempotency).
"""
import base64
import hashlib
import hmac
import secrets
import time

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
