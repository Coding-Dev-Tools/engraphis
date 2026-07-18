"""Webhook fulfillment tests — Polar order.paid → signed key → email."""
import pytest

pytest.importorskip("fastapi", reason="full-stack extra not installed")

from engraphis.inspector.app import create_app
from engraphis.service import MemoryService
from fastapi.testclient import TestClient


class TestPolarWebhook:
    def test_no_secret_configured_returns_500(self):
        svc = MemoryService.create(":memory:")
        client = TestClient(create_app(svc))
        r = client.post("/webhooks/polar", json={"type": "order.paid", "data": {}})
        assert r.status_code == 500
        assert "POLAR_WEBHOOK_SECRET" in r.json()["error"]

    def test_invalid_signature_returns_403(self, monkeypatch):
        monkeypatch.setenv("POLAR_WEBHOOK_SECRET", "dGhpc2lzYXRlc3RzZWNyZXQ=")
        svc = MemoryService.create(":memory:")
        client = TestClient(create_app(svc))
        r = client.post(
            "/webhooks/polar",
            json={"type": "order.paid", "data": {}},
            headers={
                "webhook-id": "msg_123",
                "webhook-timestamp": "0",
                "webhook-signature": "v1,bad",
            },
        )
        assert r.status_code == 403

    def test_non_order_event_is_acknowledged(self, monkeypatch):
        import base64
        import hashlib
        import hmac
        import time

        secret_b64 = "dGhpc2lzYXRlc3RzZWNyZXQ="
        monkeypatch.setenv("POLAR_WEBHOOK_SECRET", secret_b64)
        svc = MemoryService.create(":memory:")
        client = TestClient(create_app(svc))

        ts = str(int(time.time()))
        webhook_id = "msg_test_001"
        body = b'{"type":"subscription.updated","data":{}}'
        signed_content = f"{webhook_id}.{ts}.{body.decode('utf-8')}"
        sig_bytes = hmac.new(
            base64.b64decode(secret_b64),
            signed_content.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        sig = f"v1,{base64.b64encode(sig_bytes).decode('ascii')}"

        r = client.post(
            "/webhooks/polar",
            content=body,
            headers={
                "Content-Type": "application/json",
                "webhook-id": webhook_id,
                "webhook-timestamp": ts,
                "webhook-signature": sig,
            },
        )
        assert r.status_code == 202
        assert r.json()["type"] == "subscription.updated"

    def test_fulfillment_fails_when_no_signing_key(self, monkeypatch):
        import base64
        import hashlib
        import hmac
        import time

        secret_b64 = "dGhpc2lzYXRlc3RzZWNyZXQ="
        monkeypatch.setenv("POLAR_WEBHOOK_SECRET", secret_b64)
        monkeypatch.setenv(
            "ENGRAPHIS_SIGNING_KEY",
            "/does/not/exist/vendor_signing.key",
        )
        svc = MemoryService.create(":memory:")
        client = TestClient(create_app(svc))

        ts = str(int(time.time()))
        webhook_id = "msg_test_002"
        body = (
            b'{"type":"order.paid","data":{'
            b'"customer":{"email":"buyer@example.com"},'
            b'"product":{"name":"Engraphis Pro Monthly"}}}'
        )
        signed_content = f"{webhook_id}.{ts}.{body.decode('utf-8')}"
        sig_bytes = hmac.new(
            base64.b64decode(secret_b64),
            signed_content.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        sig = f"v1,{base64.b64encode(sig_bytes).decode('ascii')}"

        r = client.post(
            "/webhooks/polar",
            content=body,
            headers={
                "Content-Type": "application/json",
                "webhook-id": webhook_id,
                "webhook-timestamp": ts,
                "webhook-signature": sig,
            },
        )
        assert r.status_code == 500
        assert "fulfillment failed" in r.json()["error"].lower()


class TestTeamInviteEmailCopy:
    """Viewers are never sent the shared Team key (they hold no seat), and neither is
    anyone on an instance without a Team license — so the keyless invite must not read
    like half of a two-part email."""

    def _text(self, key=""):
        from engraphis.inspector.webhooks import _team_invite_email_text
        return _team_invite_email_text(
            "Dana", "viewer", "https://team.example", invited_by="Admin",
            key=key, to="dana@example.com")

    def test_keyless_invite_never_advertises_a_missing_second_option(self):
        text = self._text()
        assert "two ways" not in text
        assert "OPTION" not in text            # nothing to number against
        assert "the second is optional" not in text
        assert "license key" not in text.split("Sign in to the team dashboard")[0]
        # the one path that does exist is still fully described
        assert "Sign in to the team dashboard" in text
        assert "dana@example.com" in text and "https://team.example" in text

    def test_keyed_invite_still_renders_both_options(self):
        text = self._text(key="ENG-TEAM-KEY")
        assert "two ways" in text and "the second is optional" in text
        assert "OPTION 1 (required to join the team)" in text
        assert "OPTION 2 (optional)" in text
        assert "ENG-TEAM-KEY" in text
