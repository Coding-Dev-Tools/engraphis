"""GA contract tests for the isolated commercial control plane."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sqlite3
import stat
import threading
import time
from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("fastapi", reason="commercial control-plane tests need the server extra")
pytest.importorskip("httpx", reason="commercial control-plane tests need httpx")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from engraphis import email_outbox, licensing
from engraphis.inspector import license_registry
from engraphis.inspector.auth import AuthError, AuthStore


def test_invitation_reserves_seat_and_recipient_sets_password(monkeypatch, tmp_path):
    monkeypatch.setattr(licensing, "require_feature", lambda feature: None)
    store = AuthStore(str(tmp_path / "users.db"), iterations=1_000)
    admin = store.create_user(
        "admin@example.com", "Admin", "correct-horse-1", "admin", seat_limit=2)
    invitation = store.create_invitation(
        "member@example.com", "Member", "member", created_by=admin["id"], seat_limit=2)
    assert store.count_users() == 1
    with pytest.raises(AuthError, match="seat limit"):
        store.create_invitation(
            "other@example.com", "Other", "viewer", created_by=admin["id"], seat_limit=2)
    member = store.accept_invitation(invitation["token"], "recipient-chosen-1")
    assert member["email"] == "member@example.com"
    assert store.login("member@example.com", "recipient-chosen-1")["role"] == "member"
    with pytest.raises(AuthError, match="invalid or expired"):
        store.accept_invitation(invitation["token"], "recipient-chosen-1")


def test_invitation_resend_invalidates_prior_token(monkeypatch, tmp_path):
    monkeypatch.setattr(licensing, "require_feature", lambda feature: None)
    store = AuthStore(str(tmp_path / "users.db"), iterations=1_000)
    admin = store.create_user(
        "admin@example.com", "Admin", "correct-horse-1", "admin", seat_limit=2)
    original = store.create_invitation(
        "member@example.com", "Member", "member", created_by=admin["id"], seat_limit=2)
    resent = store.resend_invitation(original["id"])
    with pytest.raises(AuthError, match="invalid or expired"):
        store.accept_invitation(original["token"], "recipient-chosen-1")
    assert store.accept_invitation(resent["token"], "recipient-chosen-1")["role"] == "member"


def test_pending_invitation_keeps_its_reserved_seat_during_reenable(monkeypatch, tmp_path):
    monkeypatch.setattr(licensing, "require_feature", lambda feature: None)
    store = AuthStore(str(tmp_path / "users.db"), iterations=1_000)
    admin = store.create_user(
        "admin@example.com", "Admin", "correct-horse-1", "admin", seat_limit=3)
    disabled = store.create_user(
        "disabled@example.com", "Disabled", "correct-horse-1", "member", seat_limit=3)
    store.update_user(disabled["id"], disabled=True, seat_limit=3)
    invitation = store.create_invitation(
        "member@example.com", "Member", "member", created_by=admin["id"], seat_limit=2)

    with pytest.raises(AuthError, match="seat limit"):
        store.update_user(disabled["id"], disabled=False, seat_limit=2)

    accepted = store.accept_invitation(invitation["token"], "recipient-chosen-1")
    assert accepted["email"] == "member@example.com"
    assert store.count_active_users() == 2


def test_api_tokens_are_scoped_hashed_expiring_and_revoked(monkeypatch, tmp_path):
    monkeypatch.setattr(licensing, "require_feature", lambda feature: None)
    store = AuthStore(str(tmp_path / "users.db"), iterations=1_000)
    user = store.create_user(
        "admin@example.com", "Admin", "correct-horse-1", "admin", seat_limit=1)
    issued = store.create_api_token(
        user["id"], scopes=["agent", "sync:read", "sync:write"], ttl=600)
    row = store.conn.execute(
        "SELECT token_hash,scopes FROM api_tokens WHERE id=?", (issued["id"],)).fetchone()
    assert issued["token"] not in row["token_hash"]
    assert store.resolve_api_token(issued["token"])["token_scopes"] == [
        "agent", "sync:read", "sync:write"]
    store.conn.execute("UPDATE api_tokens SET expires_at=0 WHERE id=?", (issued["id"],))
    store.conn.commit()
    assert store.resolve_api_token(issued["token"]) is None


def test_email_outbox_retries_and_reduces_provider_events(monkeypatch, tmp_path):
    monkeypatch.setenv("ENGRAPHIS_RELAY_DB", str(tmp_path / "relay.db"))
    message_id = email_outbox.enqueue(
        "invitation", "person@example.com", "Invite", "Private body")

    def fail(*args):
        raise RuntimeError("provider payload must not be persisted")

    with pytest.raises(RuntimeError):
        email_outbox.deliver_now(message_id, fail)
    conn = license_registry.connect()
    conn.execute("UPDATE email_outbox SET next_attempt_at=0 WHERE id=?", (message_id,))
    conn.commit()
    conn.close()
    result = email_outbox.process_due(lambda *args: ("resend", "provider-123"))
    assert result == {"processed": 1, "sent": 1, "failed": 0}
    assert email_outbox.record_provider_event(
        "event-1", "provider-123", "email.bounced") is True
    recent = email_outbox.recent_redacted()
    assert recent[0]["status"] == "bounced"
    assert "recipient" not in recent[0] and "subject" not in recent[0]
    assert "person@example.com" not in json.dumps(recent)


@pytest.mark.skipif(os.name == "nt", reason="Windows does not expose POSIX owner modes")
def test_relay_registry_is_created_owner_only(monkeypatch, tmp_path):
    from engraphis import billing

    relay = tmp_path / "private" / "relay.db"
    webhooks = tmp_path / "private" / "polar-webhooks.db"
    monkeypatch.setenv("ENGRAPHIS_RELAY_DB", str(relay))
    monkeypatch.setenv("ENGRAPHIS_WEBHOOK_STATE", str(webhooks))
    conn = license_registry.connect()
    conn.close()
    assert billing.webhook_state_ready(require_durable=True)
    assert stat.S_IMODE(relay.stat().st_mode) == 0o600
    assert stat.S_IMODE(webhooks.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="Windows does not expose POSIX owner modes")
def test_team_auth_database_is_created_owner_only(tmp_path):
    users = tmp_path / "private" / "users.db"
    store = AuthStore(str(users), iterations=1_000)
    store.conn.close()
    assert stat.S_IMODE(users.stat().st_mode) == 0o600


def test_customer_local_email_worker_drains_the_durable_outbox(monkeypatch):
    from engraphis import dashboard_app
    from engraphis.inspector import webhooks

    deliverer = object()
    seen = {}
    monkeypatch.setattr(webhooks, "_deliver_text_email", deliverer)

    def process(selected, *, limit):
        seen.update(deliverer=selected, limit=limit)
        return {"processed": 1, "sent": 1, "failed": 0}

    monkeypatch.setattr(email_outbox, "process_due", process)
    assert dashboard_app._process_due_email() == {
        "processed": 1, "sent": 1, "failed": 0}
    assert seen == {"deliverer": deliverer, "limit": 20}


def test_email_outbox_recovers_expired_claim_and_terminal_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("ENGRAPHIS_RELAY_DB", str(tmp_path / "relay.db"))
    recovered = email_outbox.enqueue(
        "invitation", "person@example.com", "Invite", "Private body")
    terminal = email_outbox.enqueue(
        "reset", "person@example.com", "Reset", "Private body", max_attempts=1)
    conn = license_registry.connect()
    conn.execute(
        "UPDATE email_outbox SET status='sending',attempts=1,next_attempt_at=0 WHERE id=?",
        (recovered,))
    conn.execute(
        "UPDATE email_outbox SET status='sending',attempts=1,next_attempt_at=0 WHERE id=?",
        (terminal,))
    conn.commit()
    conn.close()
    delivered = []
    result = email_outbox.process_due(
        lambda to, *_args: (delivered.append(to) or ("resend", "provider-recovered")))
    assert result["processed"] == 2 and result["sent"] == 1
    assert delivered == ["person@example.com"]
    assert email_outbox.health()["failed"] == 1
    assert email_outbox.requeue_failed([terminal]) == 1
    assert email_outbox.process_due(lambda *_args: ("resend", "provider-terminal"))["sent"] == 1
    assert email_outbox.health()["healthy"] is True


def test_manual_email_requeue_is_explicit_and_permanently_bounded(monkeypatch, tmp_path):
    monkeypatch.setenv("ENGRAPHIS_RELAY_DB", str(tmp_path / "relay.db"))
    message_id = email_outbox.enqueue(
        "reset", "person@example.com", "Reset", "Private body", max_attempts=1)

    def fail(*_args):
        raise RuntimeError("provider down")

    for cycle in range(email_outbox.MAX_MANUAL_REQUEUES + 1):
        with pytest.raises(RuntimeError):
            email_outbox.deliver_now(message_id, fail)
        expected = 1 if cycle < email_outbox.MAX_MANUAL_REQUEUES else 0
        assert email_outbox.requeue_failed([message_id]) == expected
        if expected == 0:
            break


def test_failed_email_resolution_requires_durable_claim_and_redacts_atomically(
        monkeypatch, tmp_path):
    from engraphis import billing

    monkeypatch.setenv("ENGRAPHIS_RELAY_DB", str(tmp_path / "relay.db"))
    monkeypatch.setenv("ENGRAPHIS_WEBHOOK_STATE", str(tmp_path / "webhooks.db"))
    claim = "ful:order:manual-closeout"
    message_id = email_outbox.enqueue(
        "purchase_license", "buyer@example.com", "Private subject",
        "Your key is ENGR1.private.recovery", reply_to="support@example.com",
        idempotency_key="purchase-license:manual-closeout",
        retention_claim=claim, max_attempts=1)
    conn = license_registry.connect()
    try:
        conn.execute(
            "INSERT INTO license_fulfillment_keys(retention_claim,license_key,created_at) "
            "VALUES (?,?,?)", (claim, "ENGR1.private.recovery", 1.0))
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(RuntimeError):
        email_outbox.deliver_now(
            message_id,
            lambda *_args: (_ for _ in ()).throw(RuntimeError("provider private body")))
    assert email_outbox.health()["healthy"] is False
    # Never destroy exact-key recovery while Polar can still retry an unfinished claim.
    assert email_outbox.resolve_failed([message_id], limit=1) == 0

    assert billing.claim_webhook(claim) == "claimed"
    billing.complete_webhook(claim)
    assert email_outbox.resolve_failed([message_id], limit=1) == 1
    conn = license_registry.connect()
    try:
        row = conn.execute(
            "SELECT status,recipient,subject,text_body,reply_to,retention_claim,last_error "
            "FROM email_outbox WHERE id=?", (message_id,)).fetchone()
        assert dict(row) == {
            "status": "resolved", "recipient": "", "subject": "", "text_body": "",
            "reply_to": None, "retention_claim": "", "last_error": "",
        }
        assert conn.execute(
            "SELECT 1 FROM license_fulfillment_keys WHERE retention_claim=?", (claim,)
        ).fetchone() is None
    finally:
        conn.close()
    assert email_outbox.health()["healthy"] is True
    assert email_outbox.resolve_failed([message_id], limit=1) == 0


def test_email_outbox_preserves_strongest_event_across_delivery_race(
        monkeypatch, tmp_path):
    monkeypatch.setenv("ENGRAPHIS_RELAY_DB", str(tmp_path / "relay.db"))
    message_id = email_outbox.enqueue(
        "invitation", "person@example.com", "Invite", "Private body")
    assert email_outbox.record_provider_event(
        "event-before-send", "provider-race", "email.bounced")
    assert email_outbox.deliver_now(
        message_id, lambda *_args: ("resend", "provider-race"))
    assert email_outbox.recent_redacted()[0]["status"] == "bounced"
    assert email_outbox.record_provider_event(
        "event-delivered-late", "provider-race", "email.delivered")
    assert email_outbox.recent_redacted()[0]["status"] == "bounced"
    assert email_outbox.record_provider_event(
        "event-complained", "provider-race", "email.complained")
    assert email_outbox.record_provider_event(
        "event-bounced-late", "provider-race", "email.bounced")
    assert email_outbox.recent_redacted()[0]["status"] == "complained"

    other = email_outbox.enqueue(
        "invitation", "other@example.com", "Invite", "Other private body")
    assert email_outbox.deliver_now(
        other, lambda *_args: ("resend", "provider-other"))
    # Reusing a previously reduced Svix ID must be an acknowledged no-op, even if a
    # different signed body is presented later; it may not mutate another message.
    assert email_outbox.record_provider_event(
        "event-complained", "provider-other", "email.bounced")
    statuses = {row["id"]: row["status"] for row in email_outbox.recent_redacted()}
    assert statuses[other] == "sent"
    assert not email_outbox.record_provider_event(
        "x" * 256, "provider-other", "email.bounced")
    assert not email_outbox.record_provider_event(
        "event-new", "y" * 161, "email.bounced")


def test_email_outbox_invalid_health_config_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("ENGRAPHIS_RELAY_DB", str(tmp_path / "relay.db"))
    monkeypatch.setenv("ENGRAPHIS_EMAIL_MAX_BOUNCE_RATE", "not-a-number")
    result = email_outbox.health()
    assert result["healthy"] is False
    assert result["configuration_valid"] is False


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "-0.1", "1.1"])
def test_email_outbox_nonfinite_or_out_of_range_health_config_fails_closed(
        monkeypatch, tmp_path, value):
    monkeypatch.setenv("ENGRAPHIS_RELAY_DB", str(tmp_path / "relay.db"))
    monkeypatch.setenv("ENGRAPHIS_EMAIL_MAX_BOUNCE_RATE", value)
    result = email_outbox.health()
    assert result["healthy"] is False
    assert result["configuration_valid"] is False


def test_email_outbox_rejects_oversize_and_header_control_inputs(monkeypatch, tmp_path):
    monkeypatch.setenv("ENGRAPHIS_RELAY_DB", str(tmp_path / "relay.db"))
    with pytest.raises(ValueError, match="idempotency_key is too long"):
        email_outbox.enqueue(
            "invitation", "person@example.com", "Invite", "Private body",
            idempotency_key="x" * (email_outbox.MAX_IDEMPOTENCY_KEY_CHARS + 1))
    with pytest.raises(ValueError, match="subject contains control characters"):
        email_outbox.enqueue(
            "invitation", "person@example.com", "Invite\r\nBcc: victim@example.com",
            "Private body")
    with pytest.raises(ValueError, match="text_body is empty or too large"):
        email_outbox.enqueue(
            "invitation", "person@example.com", "Invite",
            "x" * (email_outbox.MAX_TEXT_BODY_BYTES + 1))


def test_resend_signature_is_timestamped_and_verified(monkeypatch):
    from engraphis.resend_events import verify_signature
    secret = b"test-webhook-secret"
    monkeypatch.setenv("RESEND_WEBHOOK_SECRET", "whsec_" + base64.b64encode(secret).decode())
    body = b'{"type":"email.delivered"}'
    stamp = str(int(time.time()))
    signed = b"evt." + stamp.encode() + b"." + body
    signature = "v1," + base64.b64encode(hmac.new(secret, signed, hashlib.sha256).digest()).decode()
    assert verify_signature(body, "evt", stamp, signature)
    assert not verify_signature(body + b"x", "evt", stamp, signature)
    assert not verify_signature(body, "evt", "1", signature)
    epoch_signature = "v1," + base64.b64encode(
        hmac.new(secret, b"evt.0." + body, hashlib.sha256).digest()).decode()
    assert verify_signature(body, "evt", "0", epoch_signature, now=0)
    monkeypatch.setenv("RESEND_WEBHOOK_SECRET", "whsec_not-valid-base64!")
    assert not verify_signature(body, "evt", stamp, signature)
    monkeypatch.setenv("RESEND_WEBHOOK_SECRET", "short")
    short_signature = "v1," + base64.b64encode(
        hmac.new(b"short", signed, hashlib.sha256).digest()).decode()
    assert not verify_signature(body, "evt", stamp, short_signature)


def test_resend_webhook_rejects_oversize_and_malformed_data(monkeypatch):
    from engraphis.resend_events import MAX_BODY_BYTES, router

    monkeypatch.setenv("RESEND_WEBHOOK_SECRET", "test-webhook-secret")
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    assert client.post("/email/v1/resend-events", content=b"x" * (MAX_BODY_BYTES + 1)
                       ).status_code == 413
    body = b'{"type":"email.delivered","data":[]}'
    stamp = str(int(time.time()))
    signed = b"evt." + stamp.encode() + b"." + body
    signature = "v1," + base64.b64encode(
        hmac.new(b"test-webhook-secret", signed, hashlib.sha256).digest()).decode()
    response = client.post("/email/v1/resend-events", content=body, headers={
        "svix-id": "evt", "svix-timestamp": stamp, "svix-signature": signature})
    assert response.status_code == 400


def test_vendor_email_worker_survives_transient_iteration_failure(monkeypatch):
    from engraphis.config import settings
    from engraphis import vendor_app

    monkeypatch.setattr(settings, "service_mode", "vendor")
    monkeypatch.setattr(vendor_app, "EMAIL_WORKER_INTERVAL_SECONDS", 0.01)
    calls = []
    recovered = threading.Event()

    def flaky(*_args, **_kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("transient lock")
        recovered.set()
        return {"processed": 0, "sent": 0, "failed": 0}

    monkeypatch.setattr(email_outbox, "process_due", flaky)
    app = vendor_app.create_app()
    with TestClient(app):
        assert recovered.wait(timeout=2)
        assert app.state.email_worker.done() is False


def test_vendor_readiness_proves_matching_signer_but_honors_release_gate(
        monkeypatch):
    from engraphis import commercial, vendor_app
    from engraphis.config import settings
    from engraphis.inspector import webhooks

    seed = b"\x52" * 32
    public = licensing.ed25519_public_key(seed).hex()
    monkeypatch.setattr(settings, "service_mode", "vendor")
    monkeypatch.setattr(licensing, "_TEST_MODE_PUBKEY_OVERRIDE", False)
    monkeypatch.setattr(licensing, "_VENDOR_PUBKEY_HEX", public)
    monkeypatch.setattr(licensing, "VENDOR_SIGNER_RELEASE_READY", False)
    monkeypatch.setenv("ENGRAPHIS_VENDOR_SIGNING_KEY", seed.hex())
    relay_seed = b"\x53" * 32
    monkeypatch.setenv("ENGRAPHIS_RELAY_TOKEN_SIGNING_KEY", relay_seed.hex())
    monkeypatch.setenv(
        "ENGRAPHIS_RELAY_TOKEN_PUBKEY",
        licensing.ed25519_public_key(relay_seed).hex(),
    )
    monkeypatch.setenv(
        "ENGRAPHIS_RELAY_TOKEN_AUDIENCE", "https://relay.example.test")
    admin_token = "vendor-admin-secret-at-least-32-characters"
    monkeypatch.setenv("ENGRAPHIS_VENDOR_ADMIN_TOKEN", admin_token)
    monkeypatch.setenv("POLAR_WEBHOOK_SECRET", "polar-webhook-secret")
    monkeypatch.setenv("POLAR_ORGANIZATION_ID", "polar-org")
    monkeypatch.setenv("RESEND_WEBHOOK_SECRET", "resend-webhook-secret")
    monkeypatch.setattr(
        commercial, "product_catalog",
        lambda: {f"product-{index}": {} for index in range(len(commercial.PRODUCT_ENV))})
    monkeypatch.setattr(commercial, "_registry_writable", lambda: True)
    monkeypatch.setattr(commercial, "_disk_ok", lambda: True)
    monkeypatch.setattr(commercial, "_backup_fresh", lambda: True)
    monkeypatch.setattr(webhooks, "email_configured", lambda: True)
    monkeypatch.setattr(email_outbox, "health", lambda: {"healthy": True})
    monkeypatch.setattr(
        email_outbox, "process_due",
        lambda *_args, **_kwargs: {"processed": 0, "sent": 0, "failed": 0})

    app = vendor_app.create_app()
    with TestClient(app) as client:
        public_response = client.get("/api/ready")
        operations_response = client.get(
            "/ops/ready",
            headers={"Authorization": "Bearer " + admin_token},
        )

    assert public_response.status_code == 503
    assert public_response.json()["ready"] is False
    assert operations_response.status_code == 503
    checks = operations_response.json()
    assert checks["signer"] is True
    assert checks["signer_release_ready"] is False
    assert checks["ready"] is False
    assert all(
        value is True
        for name, value in checks.items()
        if name not in {"signer_release_ready", "ready"}
    )

    # Once the external signer ceremony is complete, stale backup state must hold the
    # authenticated operational gate closed without deadlocking the platform probe that
    # must stay up so an operator can run /ops/backup.
    monkeypatch.setattr(licensing, "VENDOR_SIGNER_RELEASE_READY", True)
    monkeypatch.setattr(commercial, "_backup_fresh", lambda: False)
    assert commercial.vendor_serving_readiness()["ready"] is True
    operational = commercial.vendor_readiness()
    assert operational["backup"] is False and operational["ready"] is False
    assert operational["manual_fulfillment"] is True
    with TestClient(app) as client:
        assert client.get("/api/ready").status_code == 503


def test_manual_fulfillment_fallback_is_an_authenticated_ops_alert(monkeypatch, tmp_path):
    from engraphis.inspector import webhooks

    monkeypatch.setenv("ENGRAPHIS_WEBHOOK_STATE", str(tmp_path / "polar-webhooks.db"))
    assert webhooks.manual_fulfillment_clear() is True
    fallback = tmp_path / webhooks.UNDELIVERED_LICENSE_KEYS_NAME
    fallback.write_text(
        "1784485000\tbuyer@example.com\tPro\tENGR1.payload.signature\n",
        encoding="utf-8")
    assert webhooks.manual_fulfillment_clear() is False


def test_deployment_bound_trial_never_returns_key_to_confirmation_browser(
        monkeypatch, tmp_path):
    from engraphis.inspector import license_cloud, webhooks

    monkeypatch.setenv("ENGRAPHIS_RELAY_DB", str(tmp_path / "relay.db"))
    monkeypatch.setenv("ENGRAPHIS_RELAY_PUBLIC_URL", "https://license.engraphis.test")
    sent = {}

    def capture(to, url, plan="team", **kwargs):
        sent.update(to=to, url=url, plan=plan)

    monkeypatch.setattr(webhooks, "send_trial_claim_email", capture)
    monkeypatch.setattr(webhooks, "issue_key", lambda *args, **kwargs: "signed-secret-key")
    monkeypatch.setattr(license_registry, "record_issued", lambda key: None)
    app = FastAPI()
    app.include_router(license_cloud.router)
    client = TestClient(app)
    request = {"deployment_token": "d" * 32, "machine_id": "machine-1",
               "email": "Person@Example.com", "plan": "team",
               "dashboard_url": "https://memory.example.com"}
    started = client.post("/license/v1/trial-claims", json=request)
    assert started.status_code == 200
    claim_id = started.json()["claim_id"]
    assert "key" not in started.json()
    emailed_url = urlsplit(sent["url"])
    assert emailed_url.query == ""
    token = parse_qs(emailed_url.fragment)["token"][0]
    preview = client.get("/license/v1/trial-claims/verify")
    assert preview.status_code == 200 and "signed-secret-key" not in preview.text
    assert token not in preview.text
    assert "form-action 'none'" in preview.headers["content-security-policy"]
    assert client.get("/license/v1/trial-claims/%s" % claim_id).json()["confirmed"] is False
    query_attempt = client.post(
        "/license/v1/trial-claims/verify", params={"token": token})
    assert query_attempt.status_code == 400
    confirmed = client.post("/license/v1/trial-claims/verify", json={"token": token})
    assert confirmed.status_code == 200 and "signed-secret-key" not in confirmed.text
    denied = client.post(
        "/license/v1/trial-claims/%s/claim" % claim_id,
        json={"deployment_token": "x" * 32, "machine_id": "machine-1"})
    assert denied.status_code == 404
    malformed = client.post(
        "/license/v1/trial-claims/%s/claim" % claim_id,
        json={"deployment_token": "x" * 513, "machine_id": "machine-1"})
    assert malformed.status_code == 401
    assert client.get("/license/v1/trial-claims/not-a-claim").status_code == 404
    claimed = client.post(
        "/license/v1/trial-claims/%s/claim" % claim_id,
        json={"deployment_token": "d" * 32, "machine_id": "machine-1"})
    assert claimed.json()["key"] == "signed-secret-key"
    status = client.get("/license/v1/trial-claims/%s" % claim_id).json()
    assert status["active"] is True and "key" not in status
    replay = client.post("/license/v1/trial-claims", json=request)
    assert replay.status_code == 200 and replay.json()["status"] == "confirmed"


def test_vendor_mode_disables_key_copy_trial_route(monkeypatch):
    from engraphis.config import settings
    from engraphis.inspector import license_cloud

    monkeypatch.setattr(settings, "service_mode", "vendor")
    monkeypatch.delenv("ENGRAPHIS_ENABLE_LEGACY_TRIAL_FLOW", raising=False)
    app = FastAPI()
    app.include_router(license_cloud.router)
    response = TestClient(app).post("/license/v1/start-trial", json={
        "machine_id": "machine-1",
        "email": "person@example.com",
        "plan": "pro",
    })
    assert response.status_code == 410
    assert response.headers["Deprecation"] == "true"
    assert response.json()["replacement"] == "/license/v1/trial-claims"


def test_expired_unconfirmed_trial_claim_does_not_squat_on_email(monkeypatch, tmp_path):
    from engraphis.inspector import license_cloud

    monkeypatch.setenv("ENGRAPHIS_RELAY_DB", str(tmp_path / "relay.db"))
    first_id, first_token, first_state = license_cloud._reserve_trial_claim(
        "machine-old", "person@example.com", "team", "a" * 32,
        "https://old.example",
    )
    assert first_id and first_token and first_state == "created"
    conn = license_registry.connect()
    try:
        conn.execute("UPDATE trial_claims SET expires_at=0 WHERE claim_id=?", (first_id,))
        conn.commit()
    finally:
        conn.close()

    second_id, second_token, second_state = license_cloud._reserve_trial_claim(
        "machine-new", "person@example.com", "team", "b" * 32,
        "https://new.example",
    )
    assert second_id != first_id
    assert second_token and second_state == "created"


def test_confirmed_trial_keeps_permanent_deployment_tombstone_after_claim_sweep(
        monkeypatch, tmp_path):
    from engraphis.inspector import license_cloud, webhooks

    monkeypatch.setenv("ENGRAPHIS_RELAY_DB", str(tmp_path / "relay.db"))
    monkeypatch.setattr(webhooks, "issue_key", lambda *_args, **_kwargs: "signed-key")
    deployment_token = "deployment-binding-" + "x" * 24
    claim_id, confirmation, state = license_cloud._reserve_trial_claim(
        "machine-original", "first@example.com", "team", deployment_token,
        "https://memory.example.com")
    assert claim_id and confirmation and state == "created"
    assert license_cloud._verify_trial_claim_token(confirmation).status_code == 200

    conn = license_registry.connect()
    try:
        conn.execute("UPDATE trial_claims SET expires_at=0 WHERE claim_id=?", (claim_id,))
        conn.commit()
    finally:
        conn.close()

    new_id, new_token, new_state = license_cloud._reserve_trial_claim(
        "machine-rotated", "second@example.com", "team", deployment_token,
        "https://other.example.com")
    assert new_id == "" and new_token is None and new_state == "used"
    conn = license_registry.connect()
    try:
        grant = conn.execute(
            "SELECT deployment_hash FROM trial_grants WHERE machine_id='machine-original'"
        ).fetchone()
        assert grant["deployment_hash"] == license_cloud._deployment_hash(deployment_token)
        assert conn.execute(
            "SELECT 1 FROM trial_claims WHERE claim_id=?", (claim_id,)).fetchone() is None
    finally:
        conn.close()


def test_trial_grant_upgrade_backfills_deployment_binding_before_index(
        monkeypatch, tmp_path):
    from engraphis.inspector import license_cloud

    monkeypatch.setenv("ENGRAPHIS_RELAY_DB", str(tmp_path / "relay.db"))
    conn = license_registry.connect()
    try:
        conn.execute(
            "CREATE TABLE trial_grants(machine_id TEXT PRIMARY KEY,email TEXT,"
            "plan TEXT,issued_at REAL NOT NULL)")
        conn.execute(
            "INSERT INTO trial_grants VALUES ('legacy-machine','legacy@example.com',"
            "'team',1)")
        conn.executescript(license_cloud._TRIAL_CLAIM_SCHEMA)
        conn.execute(
            "INSERT INTO trial_claims(claim_id,confirmation_hash,deployment_hash,"
            "machine_id,email,plan,created_at,expires_at,confirmed_at) "
            "VALUES ('clm_legacy','confirmation','%s','legacy-machine',"
            "'legacy@example.com','team',1,2,2)" % ("a" * 64))
        license_cloud._ensure_trial_plan_column(conn)
        row = conn.execute(
            "SELECT deployment_hash FROM trial_grants WHERE machine_id='legacy-machine'"
        ).fetchone()
        assert row["deployment_hash"] == "a" * 64
    finally:
        conn.close()


def test_commercial_backup_is_encrypted_verified_and_restorable(monkeypatch, tmp_path):
    from engraphis.config import settings
    from scripts import commercial_backup

    database = tmp_path / "engraphis.db"
    conn = sqlite3.connect(str(database))
    conn.execute("CREATE TABLE facts(value TEXT)")
    conn.execute("INSERT INTO facts VALUES ('private')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(settings, "db_path", str(database))
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setenv("ENGRAPHIS_STATE_DIR", str(state_dir))
    monkeypatch.setenv("ENGRAPHIS_RELAY_DB", str(state_dir / "relay.db"))
    monkeypatch.setenv("ENGRAPHIS_BACKUP_KEY", "ab" * 32)
    state_values = {
        "license.key": "ENGR1.signed.customer.key\n",
        "machine_id": "machine-stable\n",
        "lease.sig": "signed-lease\n",
        "sync.token": "scoped-sync-token-value\n",
        "sync.read_only": "1\n",
        "trial_used.json": '{"used":true}\n',
        ".clock_anchor": "1770000000\n",
    }
    for name, value in state_values.items():
        (state_dir / name).write_text(value, encoding="utf-8")
    (state_dir / "vendor_signing.key").write_text("never archive", encoding="utf-8")
    marker = tmp_path / "status" / "backup.json"
    output = tmp_path / "off-volume"
    artifact = commercial_backup.backup(
        output, marker, retention_days=30, allow_same_device=True)
    payload = artifact.read_bytes()
    assert b"private" not in payload
    inventory = commercial_backup._verify_plain(commercial_backup._decrypt(artifact))
    assert inventory["databases"][0]["name"] == "memory.db"
    assert {item["name"] for item in inventory["state_files"]} == set(state_values)
    restored = tmp_path / "restored"
    commercial_backup._verify_plain(commercial_backup._decrypt(artifact), restored)
    conn = sqlite3.connect(str(restored / "memory.db"))
    assert conn.execute("SELECT value FROM facts").fetchone()[0] == "private"
    conn.close()
    for name, value in state_values.items():
        assert (restored / ".engraphis" / name).read_text(encoding="utf-8") == value
    assert not (restored / ".engraphis" / "vendor_signing.key").exists()
    restore_plan = json.loads(
        (restored / "RESTORE_PLAN.json").read_text(encoding="utf-8"))
    assert restore_plan["service_must_be_stopped"] is True
    assert restore_plan["automatic_overwrite"] is False
    planned = {item["staged"]: item["destination"] for item in restore_plan["files"]}
    assert planned["memory.db"] == str(database.resolve())
    assert planned[".engraphis/license.key"] == str(state_dir / "license.key")
    marker_status = json.loads(marker.read_text(encoding="utf-8"))
    assert marker_status["schema"] == "engraphis-backup-status/v1"
    assert marker_status["artifact"] == str(artifact)
    assert marker_status["bytes"] == artifact.stat().st_size
    monkeypatch.setenv("ENGRAPHIS_BACKUP_STATUS_FILE", str(marker))
    monkeypatch.setenv("ENGRAPHIS_BACKUP_OUTPUT_DIR", str(output))
    from engraphis import commercial
    assert commercial._backup_fresh() is True
    existing = tmp_path / "existing-restore"
    existing.mkdir()
    (existing / "keep.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(FileExistsError):
        commercial_backup._verify_plain(
            commercial_backup._decrypt(artifact), existing)
    assert (existing / "keep.txt").read_text(encoding="utf-8") == "keep"
    damaged = bytearray(artifact.read_bytes())
    damaged[-1] ^= 1
    artifact.write_bytes(damaged)
    assert artifact.stat().st_size == marker_status["bytes"]
    assert commercial._backup_fresh() is False
    artifact.unlink()
    assert commercial._backup_fresh() is False


def test_configured_backup_returns_only_status_booleans(monkeypatch, tmp_path):
    from engraphis import commercial
    from scripts import commercial_backup

    artifact = tmp_path / "off-volume" / "backup.egbak"
    artifact.parent.mkdir()
    artifact.write_bytes(b"encrypted")
    monkeypatch.setenv("ENGRAPHIS_BACKUP_OUTPUT_DIR", str(artifact.parent))
    monkeypatch.setenv("ENGRAPHIS_BACKUP_STATUS_FILE", str(tmp_path / "status.json"))
    monkeypatch.setattr(commercial_backup, "backup", lambda *_args: artifact)
    monkeypatch.setattr(commercial, "_backup_fresh", lambda: True)
    assert commercial.run_configured_backup() == {"ok": True, "verified": True}


def test_backup_readiness_rejects_artifact_outside_configured_volume(monkeypatch, tmp_path):
    from engraphis import commercial

    output = tmp_path / "off-volume"
    output.mkdir()
    artifact = tmp_path / "other-volume" / "copied.egbak"
    artifact.parent.mkdir()
    artifact.write_bytes(b"same-size-encrypted-artifact")
    created_at = time.time()
    marker = tmp_path / "backup-status.json"
    marker.write_text(json.dumps({
        "schema": "engraphis-backup-status/v1",
        "artifact": str(artifact.resolve()),
        "bytes": artifact.stat().st_size,
        "created_at": created_at,
        "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
    }), encoding="utf-8")
    monkeypatch.setenv("ENGRAPHIS_BACKUP_OUTPUT_DIR", str(output))
    monkeypatch.setenv("ENGRAPHIS_BACKUP_STATUS_FILE", str(marker))
    assert commercial._backup_fresh() is False


def test_vendor_backup_endpoint_requires_admin_and_redacts_storage(monkeypatch):
    from engraphis import commercial, vendor_app
    from engraphis.config import settings

    monkeypatch.setattr(settings, "service_mode", "vendor")
    admin_token = "vendor-admin-secret-at-least-32-characters"
    monkeypatch.setattr(email_outbox, "process_due", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(commercial, "run_configured_backup",
                        lambda: {"ok": True, "verified": True})
    app = vendor_app.create_app()
    with TestClient(app) as client:
        assert client.post("/ops/backup").status_code == 401
        monkeypatch.setenv("ENGRAPHIS_VENDOR_ADMIN_TOKEN", "short")
        assert client.post("/ops/backup", headers={
            "Authorization": "Bearer short"}).status_code == 401
        monkeypatch.setenv("ENGRAPHIS_VENDOR_ADMIN_TOKEN", admin_token)
        response = client.post("/ops/backup", headers={
            "Authorization": "Bearer " + admin_token})
    assert response.status_code == 200
    assert response.json() == {"ok": True, "verified": True}


def test_vendor_email_retry_requires_one_explicit_message(monkeypatch):
    from engraphis import vendor_app
    from engraphis.config import settings

    monkeypatch.setattr(settings, "service_mode", "vendor")
    admin_token = "vendor-admin-secret-at-least-32-characters"
    monkeypatch.setenv("ENGRAPHIS_VENDOR_ADMIN_TOKEN", admin_token)
    selected = []
    monkeypatch.setattr(
        email_outbox, "requeue_failed",
        lambda ids, *, limit: selected.extend(ids) or 1)
    monkeypatch.setattr(
        email_outbox, "deliver_now",
        lambda message_id, _deliverer: message_id == "eml_selected")
    app = vendor_app.create_app()
    headers = {"Authorization": "Bearer " + admin_token}
    with TestClient(app) as client:
        assert client.post("/ops/email/retry").status_code == 401
        assert client.post("/ops/email/retry", headers=headers).status_code == 400
        response = client.post(
            "/ops/email/retry?message_id=eml_selected", headers=headers)
    assert response.json()["requeued"] == 1
    assert selected == ["eml_selected"]


def test_vendor_email_resolution_requires_auth_selected_id_and_ack(monkeypatch):
    from engraphis import vendor_app
    from engraphis.config import settings

    monkeypatch.setattr(settings, "service_mode", "vendor")
    admin_token = "vendor-admin-secret-at-least-32-characters"
    monkeypatch.setenv("ENGRAPHIS_VENDOR_ADMIN_TOKEN", admin_token)
    selected = []
    monkeypatch.setattr(
        email_outbox, "resolve_failed",
        lambda ids, *, limit: selected.extend(ids) or 1)
    app = vendor_app.create_app()
    headers = {"Authorization": "Bearer " + admin_token}
    with TestClient(app) as client:
        assert client.post(
            "/ops/email/resolve?message_id=eml_selected&acknowledged=true"
        ).status_code == 401
        assert client.post("/ops/email/resolve", headers=headers).status_code == 400
        assert client.post(
            "/ops/email/resolve?message_id=wrong&acknowledged=true", headers=headers
        ).status_code == 400
        assert client.post(
            "/ops/email/resolve?message_id=eml_selected", headers=headers
        ).status_code == 400
        response = client.post(
            "/ops/email/resolve?message_id=eml_selected&acknowledged=true",
            headers=headers)
    assert response.status_code == 200
    assert response.json() == {"resolved": 1}
    assert selected == ["eml_selected"]


def test_customer_operations_readiness_is_boolean_only_and_fail_closed(monkeypatch):
    from engraphis import commercial
    from engraphis.config import settings
    from engraphis.routes.v2_api import router

    monkeypatch.setattr(settings, "service_mode", "customer")
    monkeypatch.setattr(commercial, "_customer_disk_ok", lambda: True)
    monkeypatch.setattr(commercial, "_backup_fresh", lambda: True)
    assert commercial.customer_operations_readiness() == {
        "service_mode": True, "disk": True, "backup": True, "ready": True}

    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        response = client.get("/api/ops/ready")
    assert response.status_code == 200
    assert response.json() == {
        "service_mode": True, "disk": True, "backup": True, "ready": True}

    monkeypatch.setattr(commercial, "_backup_fresh", lambda: False)
    with TestClient(app) as client:
        response = client.get("/api/ops/ready")
    assert response.status_code == 503
    assert response.json() == {
        "service_mode": True, "disk": True, "backup": False, "ready": False}
    assert all(isinstance(value, bool) for value in response.json().values())


def test_commercial_manifest_rejects_product_mapping_and_checkout_drift():
    from copy import deepcopy
    from engraphis.commercial import manifest
    from scripts.check_commercial_manifest import _product_mapping

    data = deepcopy(manifest())
    errors = []
    mapping = _product_mapping(data, errors)
    assert not errors
    assert mapping["POLAR_TEAM_ANNUAL_PRODUCT_ID"] == ("team", "annual")
    data["plans"]["team"]["products"]["annual"]["checkout_url"] = \
        "https://example.com/wrong"
    errors = []
    _product_mapping(data, errors)
    assert any("checkout URL" in error for error in errors)


def test_commercial_manifest_repository_gate_accepts_live_pypi_badge():
    from engraphis.commercial import manifest
    from scripts.check_commercial_manifest import _check_repository

    errors = []
    _check_repository(manifest(), errors)
    assert errors == []


def _entrypoint_paths(application):
    """Collect every mounted path, descending into FastAPI's deferred ``_IncludedRouter``
    wrappers — ``app.routes`` stops flattening ``include_router`` in fastapi>=0.139, so a
    flat ``route.path`` comprehension silently drops (and thus fails to assert absence of)
    every included route."""
    paths = set()

    def add(routes):
        for route in routes:
            path = getattr(route, "path", None)
            if path:
                paths.add(path)
            included = getattr(route, "original_router", None)
            if included is not None:
                add(included.routes)

    add(application.routes)
    return paths


def test_legacy_entrypoint_customer_mode_excludes_vendor_control_plane(monkeypatch):
    """Every shipped entrypoint must preserve customer/vendor secret isolation."""
    from engraphis import app as legacy_app
    from engraphis.config import settings

    monkeypatch.setattr(settings, "service_mode", "customer")
    application = legacy_app.create_app()
    paths = _entrypoint_paths(application)

    assert "/webhooks/polar" not in paths
    assert "/license/v1/register" not in paths
    assert "/license/v1/revoke/{key_id}" not in paths
    assert "/license/v1/keys" not in paths
    # Ordinary customer deployments are no longer public compatibility forwarders;
    # only the explicit relay-mode entrypoint carries the bounded sunset proxy.
    assert "/license/v1/{compat_path:path}" not in paths
    assert any(path.startswith("/relay/v1/") for path in paths)


def test_legacy_entrypoint_vendor_mode_dispatches_to_isolated_control_plane(monkeypatch):
    from engraphis import app as legacy_app
    from engraphis.config import settings

    monkeypatch.setattr(settings, "service_mode", "vendor")
    application = legacy_app.create_app()
    paths = _entrypoint_paths(application)

    assert "/webhooks/polar" in paths
    assert "/license/v1/register" in paths
    assert "/api/ready" in paths
    assert not any(path.startswith("/memory/") for path in paths)
    assert not any(path.startswith("/relay/v1/") for path in paths)
