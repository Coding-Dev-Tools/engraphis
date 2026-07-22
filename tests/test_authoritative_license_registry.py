"""Authoritative issuance, opaque tenancy, and scoped relay-device tokens."""
import base64
import hashlib
import inspect
import json
import sqlite3
import time

import pytest

pytest.importorskip("fastapi")
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from engraphis import licensing
from engraphis.inspector import license_cloud
from engraphis.inspector import license_registry as reg
from engraphis.licensing import LicenseError, ed25519_public_key


LICENSE_SECRET = bytes(range(32))
TOKEN_SECRET = bytes(reversed(range(32)))
PREVIOUS_TOKEN_SECRET = b"\x55" * 32


@pytest.fixture(autouse=True)
def _registry_env(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "ENGRAPHIS_LICENSE_PUBKEY", ed25519_public_key(LICENSE_SECRET).hex())
    monkeypatch.setenv("ENGRAPHIS_VENDOR_SIGNING_KEY", LICENSE_SECRET.hex())
    monkeypatch.setenv("ENGRAPHIS_RELAY_DB", str(tmp_path / "relay.db"))
    monkeypatch.setenv(
        "ENGRAPHIS_RELAY_TOKEN_SIGNING_KEY", TOKEN_SECRET.hex())
    monkeypatch.setenv(
        "ENGRAPHIS_RELAY_TOKEN_PUBKEY", ed25519_public_key(TOKEN_SECRET).hex())
    monkeypatch.setenv(
        "ENGRAPHIS_RELAY_TOKEN_AUDIENCE", "https://relay.example.test")
    monkeypatch.delenv("ENGRAPHIS_RELAY_TOKEN_PREVIOUS_KEYS", raising=False)
    monkeypatch.delenv("ENGRAPHIS_RELAY_TOKEN_PREVIOUS_PUBKEYS", raising=False)
    monkeypatch.delenv("ENGRAPHIS_LEGACY_LICENSE_MIGRATION_UNTIL", raising=False)
    monkeypatch.setattr(license_cloud, "REGISTER_RATE_PER_MINUTE", 10_000)
    license_cloud._REGISTER_BUCKETS.clear()
    yield
    license_cloud._REGISTER_BUCKETS.clear()


def _key(*, plan="pro", email="buyer@example.test", issued=None, expires=None,
         seats=1, subscription_id="", order_id=""):
    now = int(time.time())
    payload = {
        "v": 1,
        "plan": plan,
        "email": email,
        "seats": seats,
        "issued": now if issued is None else issued,
        "expires": now + 86400 if expires is None else expires,
    }
    if subscription_id:
        payload["subscription_id"] = subscription_id
    if order_id:
        payload["order_id"] = order_id
    return licensing.compose_key(payload, LICENSE_SECRET)


def _app():
    app = FastAPI()
    app.include_router(license_cloud.router)

    @app.exception_handler(LicenseError)
    async def _license_error(_request, exc):
        return JSONResponse({"error": str(exc)}, status_code=402)

    return TestClient(app)


def _decode_payload(token):
    encoded = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(
        encoded + "=" * (-len(encoded) % 4)).decode("utf-8"))


def test_signature_valid_unissued_key_fails_closed_without_leaking_claims():
    key = _key(email="private-buyer@example.test")
    with pytest.raises(LicenseError, match="not issued") as caught:
        reg.verify_for_feature(key, "sync")
    assert key not in str(caught.value)
    assert "private-buyer" not in str(caught.value)

    response = _app().post(
        "/license/v1/register", json={"key": key, "machine_id": "device-1"})
    assert response.status_code == 402
    assert key not in response.text
    assert "private-buyer" not in response.text
    connection = reg.connect()
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM issued_licenses").fetchone()[0] == 0
    finally:
        connection.close()


@pytest.mark.parametrize(("column", "bad_value"), [
    ("email", "different@example.test"),
    ("plan", "pro"),
    ("seats", 99),
    ("issued", 1),
    ("expires", 2),
    ("subscription_id", "sub_different"),
    ("order_id", "order_different"),
    ("signing_key_id", "0" * 16),
])
def test_active_issued_row_must_match_every_signed_entitlement_claim(column, bad_value):
    key = _key(
        plan="team",
        seats=4,
        subscription_id="sub_authoritative",
        order_id="order_authoritative",
    )
    key_id = reg.record_issued(key)
    assert reg.verify_for_feature(key, "team").key_id == key_id

    connection = reg.connect()
    try:
        connection.execute(
            "UPDATE issued_licenses SET %s=? WHERE key_id=?" % column,
            (bad_value, key_id),
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(LicenseError, match="claims do not match"):
        reg.verify_for_feature(key, "team")


def test_legacy_enrollment_requires_a_live_deadline_with_a_hard_maximum(monkeypatch):
    now = time.time()
    key = _key(issued=int(now), expires=int(now + 86400))

    monkeypatch.setenv(
        "ENGRAPHIS_LEGACY_LICENSE_MIGRATION_UNTIL",
        str(now + reg.LEGACY_MIGRATION_MAX_WINDOW_SECONDS + 1),
    )
    with pytest.raises(LicenseError, match="not issued"):
        reg.verify_issued_license(key, now=now)

    monkeypatch.setenv(
        "ENGRAPHIS_LEGACY_LICENSE_MIGRATION_UNTIL", str(now + 3600))
    migrated = reg.verify_issued_license(key, now=now)
    assert migrated.key_id == licensing.parse_key(key, now=now).key_id

    monkeypatch.setenv(
        "ENGRAPHIS_LEGACY_LICENSE_MIGRATION_UNTIL", str(now - 1))
    # Once durably enrolled, the key no longer depends on the temporary window.
    assert reg.verify_issued_license(key, now=now).key_id == migrated.key_id


def test_legacy_window_fills_missing_fields_but_never_overwrites_conflicts(
        monkeypatch):
    now = time.time()
    key = _key(plan="team", seats=3, issued=int(now), expires=int(now + 86400))
    parsed = licensing.parse_key(key, now=now)
    connection = reg.connect()
    try:
        connection.execute(
            "INSERT INTO issued_licenses"
            "(key_id,organization_id,plan,status,created_at) VALUES(?,?,?,?,?)",
            (parsed.key_id, "org_" + "1" * 32, "team", "active", now),
        )
        connection.commit()
    finally:
        connection.close()
    monkeypatch.setenv(
        "ENGRAPHIS_LEGACY_LICENSE_MIGRATION_UNTIL", str(now + 3600))
    assert reg.verify_issued_license(key, now=now).seats == 3

    connection = reg.connect()
    try:
        row = connection.execute(
            "SELECT email,seats,issued,expires,signing_key_id "
            "FROM issued_licenses WHERE key_id=?",
            (parsed.key_id,),
        ).fetchone()
        assert row["email"] == "buyer@example.test"
        assert row["seats"] == 3
        connection.execute(
            "UPDATE issued_licenses SET plan='pro' WHERE key_id=?", (parsed.key_id,))
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(LicenseError, match="claims do not match"):
        reg.verify_issued_license(key, now=now)


def test_opaque_organization_ids_do_not_merge_customers_by_email():
    first = _key(email="procurement@example.test", issued=int(time.time()) - 2)
    second = _key(email="procurement@example.test", issued=int(time.time()) - 1)
    reg.record_issued(first)
    reg.record_issued(second)
    first_id = reg.account_id_for(licensing.parse_key(first))
    second_id = reg.account_id_for(licensing.parse_key(second))
    assert first_id.startswith("org_") and len(first_id) == 36
    assert second_id.startswith("org_") and len(second_id) == 36
    assert first_id != second_id
    assert "procurement" not in first_id + second_id


def test_subscription_renewals_and_signer_replacements_reuse_the_tenant():
    first = _key(subscription_id="sub_stable", issued=int(time.time()) - 2)
    second = _key(subscription_id="sub_stable", issued=int(time.time()) - 1)
    reg.record_issued(first)
    reg.record_issued(second)
    assert reg.account_id_for(licensing.parse_key(first)) == reg.account_id_for(
        licensing.parse_key(second))

    connection = reg.connect()
    try:
        connection.execute(
            "UPDATE issued_licenses SET organization_id=? WHERE key_id=?",
            ("org_" + "f" * 32, licensing.parse_key(second).key_id),
        )
        connection.commit()
    finally:
        connection.close()
    third = _key(subscription_id="sub_stable", issued=int(time.time()))
    with pytest.raises(LicenseError, match="identity is ambiguous"):
        reg.record_issued(third)


def test_old_registry_rows_receive_deterministic_pii_free_organization_ids(tmp_path):
    database = tmp_path / "old-registry.db"
    connection = sqlite3.connect(str(database))
    connection.execute(
        "CREATE TABLE issued_licenses (key_id TEXT PRIMARY KEY,email TEXT,plan TEXT,"
        "seats INTEGER,issued REAL,expires REAL,subscription_id TEXT,order_id TEXT,"
        "signing_key_id TEXT,status TEXT NOT NULL DEFAULT 'active',"
        "created_at REAL NOT NULL,revoked_at REAL)")
    connection.execute(
        "CREATE TABLE sync_bundles (account_id TEXT NOT NULL,workspace_id TEXT NOT NULL,"
        "name TEXT NOT NULL,data BLOB NOT NULL,updated_at REAL NOT NULL,"
        "PRIMARY KEY(account_id,workspace_id,name))")
    connection.executemany(
        "INSERT INTO issued_licenses"
        "(key_id,email,subscription_id,status,created_at) VALUES(?,?,?,?,?)",
        [
            ("legacy-one", "pii-one@example.test", "sub_shared", "active", 1),
            ("legacy-two", "pii-two@example.test", "sub_shared", "active", 2),
            ("legacy-collision", "pii-one@example.test", "sub_other", "active", 3),
            ("legacy-isolated", "pii-three@example.test", None, "active", 4),
        ],
    )
    old_one = hashlib.sha256(b"pii-one@example.test").hexdigest()[:16]
    old_two = hashlib.sha256(b"pii-two@example.test").hexdigest()[:16]
    connection.executemany(
        "INSERT INTO sync_bundles(account_id,workspace_id,name,data,updated_at) "
        "VALUES(?,?,?,?,?)",
        [
            (old_one, "ws", "one.json", b"one", 1),
            (old_two, "ws", "two.json", b"two", 2),
            (old_one, "ws", "conflict.json", b"older", 3),
            (old_two, "ws", "conflict.json", b"newer", 4),
        ],
    )
    connection.commit()
    connection.close()

    migrated = reg.connect(str(database))
    try:
        first = {
            row["key_id"]: row["organization_id"]
            for row in migrated.execute(
                "SELECT key_id,organization_id FROM issued_licenses ORDER BY key_id")
        }
    finally:
        migrated.close()
    reopened = reg.connect(str(database))
    try:
        second = {
            row["key_id"]: row["organization_id"]
            for row in reopened.execute(
                "SELECT key_id,organization_id FROM issued_licenses ORDER BY key_id")
        }
    finally:
        reopened.close()
    assert first == second
    assert first["legacy-one"] == first["legacy-two"]
    # These purchases had already collided in the retired email-derived namespace, so
    # migration preserves that existing group instead of arbitrarily assigning its mixed
    # bundles to only one purchase.
    assert first["legacy-collision"] == first["legacy-one"]
    assert first["legacy-isolated"] != first["legacy-one"]
    assert all(value.startswith("org_") and len(value) == 36 for value in first.values())
    assert all("pii" not in value for value in first.values())

    verify_bundles = reg.connect(str(database))
    try:
        moved = verify_bundles.execute(
            "SELECT account_id,name,data FROM sync_bundles ORDER BY name").fetchall()
    finally:
        verify_bundles.close()
    assert [(row["account_id"], row["name"], row["data"]) for row in moved] == [
        (first["legacy-one"], "conflict.json", b"newer"),
        (first["legacy-one"], "one.json", b"one"),
        (first["legacy-one"], "two.json", b"two"),
    ]


def test_bundle_namespace_migration_keeps_blob_copying_inside_sqlite():
    source = inspect.getsource(reg._backfill_organization_ids)
    assert "SELECT ?,workspace_id,name,data,updated_at FROM sync_bundles" in source
    assert '"SELECT workspace_id,name,data,updated_at FROM sync_bundles "' not in source


def test_device_token_exchange_is_scoped_opaque_and_revocation_aware():
    key = _key(email="private-buyer@example.test")
    key_id = reg.record_issued(key)
    response = _app().post(
        "/license/v1/device-token",
        json={"key": key, "machine_id": "device-A"},
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    document = response.json()
    token = document["device_token"]
    assert token.startswith(reg.RELAY_DEVICE_TOKEN_PREFIX + ".")
    assert key not in response.text
    assert "private-buyer" not in response.text
    payload = _decode_payload(token)
    assert payload["typ"] == "relay_device"
    assert payload["aud"] == "https://relay.example.test"
    assert payload["key_id"] == key_id
    assert payload["device_id"] == "device-A"
    assert payload["scopes"] == ["sync:read", "sync:write"]
    assert payload["account_id"].startswith("org_")
    assert 0 < payload["expires"] - payload["issued"] <= 3600
    assert "email" not in payload and "key" not in payload
    assert reg.verify_relay_device_token(token, "sync:write")["jti"] == payload["jti"]

    assert reg.revoke(key_id) is True
    with pytest.raises(LicenseError, match="no longer entitled"):
        reg.verify_relay_device_token(token, "sync:read")
    # Split customer data planes have no vendor registry. They must opt out explicitly;
    # revocation then converges at the token's hard one-hour maximum expiry.
    assert reg.verify_relay_device_token(
        token, "sync:read", check_registry=False)["account_id"] == payload["account_id"]


def test_relay_device_token_audience_is_canonical_and_exact(monkeypatch):
    key = _key()
    reg.record_issued(key)
    parsed = licensing.parse_key(key)
    account_id = reg.account_id_for(parsed)
    token, payload = reg.compose_relay_device_token(
        parsed,
        account_id,
        "device-A",
        TOKEN_SECRET,
        audience="HTTPS://Relay.Example.Test:443/",
    )
    assert payload["aud"] == "https://relay.example.test"
    assert reg.verify_relay_device_token(
        token, expected_audience="https://relay.example.test/")["aud"] == payload["aud"]
    with pytest.raises(LicenseError, match="wrong audience"):
        reg.verify_relay_device_token(
            token, expected_audience="https://other-relay.example.test")

    monkeypatch.delenv("ENGRAPHIS_RELAY_TOKEN_AUDIENCE")
    with pytest.raises(LicenseError, match="not configured"):
        reg.verify_relay_device_token(token)
    with pytest.raises(LicenseError, match="not configured"):
        reg.compose_relay_device_token(
            parsed, account_id, "device-A", TOKEN_SECRET)


@pytest.mark.parametrize("audience", [
    "http://relay.example.test",
    "https://relay.example.test/path",
    "https://user:pass@relay.example.test",
    "https://relay.example.test/?query=1",
    "https://relay.example.test/#fragment",
    "https://relay.example.test:0",
    "https://relay.example.test\x00",
])
def test_relay_device_token_audience_rejects_non_origins(audience):
    with pytest.raises(LicenseError, match="audience"):
        reg.canonical_relay_audience(audience)


def test_previous_relay_key_cannot_mint_after_its_issuance_cutoff(monkeypatch):
    key = _key()
    reg.record_issued(key)
    parsed = licensing.parse_key(key)
    account_id = reg.account_id_for(parsed)
    cutoff = int(time.time())
    monkeypatch.setenv(
        "ENGRAPHIS_RELAY_TOKEN_PREVIOUS_KEYS",
        json.dumps([{
            "public_key": ed25519_public_key(PREVIOUS_TOKEN_SECRET).hex(),
            "issued_before": cutoff,
            "not_after": cutoff + 3600,
        }]),
    )
    old_token, _ = reg.compose_relay_device_token(
        parsed,
        account_id,
        "device-A",
        PREVIOUS_TOKEN_SECRET,
        now=cutoff - 10,
        ttl_seconds=300,
    )
    assert reg.verify_relay_device_token(old_token, now=cutoff + 1)["device_id"] == "device-A"

    fresh_old_token, _ = reg.compose_relay_device_token(
        parsed,
        account_id,
        "device-A",
        PREVIOUS_TOKEN_SECRET,
        now=cutoff + 1,
        ttl_seconds=300,
    )
    with pytest.raises(LicenseError, match="rotation window"):
        reg.verify_relay_device_token(fresh_old_token, now=cutoff + 2)

    cutoff_token, _ = reg.compose_relay_device_token(
        parsed,
        account_id,
        "device-A",
        PREVIOUS_TOKEN_SECRET,
        now=cutoff,
        ttl_seconds=300,
    )
    with pytest.raises(LicenseError, match="rotation window"):
        reg.verify_relay_device_token(cutoff_token, now=cutoff + 1)

    monkeypatch.setenv(
        "ENGRAPHIS_RELAY_TOKEN_PREVIOUS_KEYS",
        json.dumps([{
            "public_key": ed25519_public_key(PREVIOUS_TOKEN_SECRET).hex(),
            "issued_before": cutoff,
            "not_after": cutoff + 100,
        }]),
    )
    with pytest.raises(LicenseError, match="rotation window"):
        reg.verify_relay_device_token(old_token, now=cutoff + 1)


@pytest.mark.parametrize("metadata", [
    "not-json",
    "{}",
    '[{"public_key":"00"}]',
    '[{"public_key":"%s","issued_before":true,"not_after":2}]'
    % ed25519_public_key(PREVIOUS_TOKEN_SECRET).hex(),
    '[{"public_key":"%s","issued_before":1,"not_after":4002}]'
    % ed25519_public_key(PREVIOUS_TOKEN_SECRET).hex(),
])
def test_previous_relay_key_metadata_is_strict(monkeypatch, metadata):
    monkeypatch.setenv("ENGRAPHIS_RELAY_TOKEN_PREVIOUS_KEYS", metadata)
    with pytest.raises(LicenseError, match="previous relay-token|cutoff"):
        reg.relay_token_verifiers()


def test_previous_relay_key_cutoff_cannot_be_staged_indefinitely(monkeypatch):
    current = int(time.time())
    monkeypatch.setenv(
        "ENGRAPHIS_RELAY_TOKEN_PREVIOUS_KEYS",
        json.dumps([{
            "public_key": ed25519_public_key(PREVIOUS_TOKEN_SECRET).hex(),
            "issued_before": current + 301,
            "not_after": current + 601,
        }]),
    )
    with pytest.raises(LicenseError, match="cutoff window"):
        reg.relay_token_verifiers(now=current)


def test_expired_previous_relay_key_must_be_removed(monkeypatch):
    current = int(time.time())
    monkeypatch.setenv(
        "ENGRAPHIS_RELAY_TOKEN_PREVIOUS_KEYS",
        json.dumps([{
            "public_key": ed25519_public_key(PREVIOUS_TOKEN_SECRET).hex(),
            "issued_before": current - 300,
            "not_after": current,
        }]),
    )
    with pytest.raises(LicenseError, match="has expired; remove"):
        reg.relay_token_verifiers(now=current)


def test_unbounded_previous_relay_key_configuration_is_rejected(monkeypatch):
    monkeypatch.setenv(
        "ENGRAPHIS_RELAY_TOKEN_PREVIOUS_PUBKEYS",
        ed25519_public_key(PREVIOUS_TOKEN_SECRET).hex(),
    )
    with pytest.raises(LicenseError, match="unbounded previous"):
        reg.relay_token_verifiers()


def test_device_token_exchange_fails_closed_on_unissued_or_misconfigured_keypair(
        monkeypatch):
    key = _key()
    denied = _app().post(
        "/license/v1/device-token", json={"key": key, "machine_id": "device-A"})
    assert denied.status_code == 402

    reg.record_issued(key)
    monkeypatch.setenv(
        "ENGRAPHIS_RELAY_TOKEN_PUBKEY", ed25519_public_key(b"x" * 32).hex())
    unavailable = _app().post(
        "/license/v1/device-token", json={"key": key, "machine_id": "device-A"})
    assert unavailable.status_code == 503
    assert key not in unavailable.text


def test_team_device_token_exchange_is_rejected_in_favor_of_named_user_tokens():
    key = _key(plan="team", seats=5)
    reg.record_issued(key)
    response = _app().post(
        "/license/v1/device-token", json={"key": key, "machine_id": "device-A"})
    assert response.status_code == 402
    assert "named-user" in response.text
    assert key not in response.text

    parsed = licensing.parse_key(key)
    with pytest.raises(ValueError, match="only for Pro"):
        reg.compose_relay_device_token(
            parsed,
            reg.account_id_for(parsed),
            "device-A",
            TOKEN_SECRET,
        )
