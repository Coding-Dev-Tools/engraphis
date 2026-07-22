"""Focused readiness checks for the split relay-token issuer and verifier roles."""
from __future__ import annotations

import json
import os
import time

import pytest

from engraphis import commercial, licensing
from engraphis.config import settings


@pytest.fixture(autouse=True)
def _relay_token_env(monkeypatch):
    for name in (
        "ENGRAPHIS_RELAY_TOKEN_SIGNING_KEY",
        "ENGRAPHIS_RELAY_TOKEN_PUBKEY",
        "ENGRAPHIS_RELAY_TOKEN_PREVIOUS_PUBKEYS",
        "ENGRAPHIS_RELAY_TOKEN_PREVIOUS_KEYS",
        "ENGRAPHIS_RELAY_TOKEN_AUDIENCE",
        "ENGRAPHIS_RELAY_DEVICE_TOKEN_TTL_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)


def _configure_pair(monkeypatch, seed: bytes) -> None:
    monkeypatch.setenv("ENGRAPHIS_RELAY_TOKEN_SIGNING_KEY", seed.hex())
    monkeypatch.setenv(
        "ENGRAPHIS_RELAY_TOKEN_PUBKEY", licensing.ed25519_public_key(seed).hex())
    monkeypatch.setenv(
        "ENGRAPHIS_RELAY_TOKEN_AUDIENCE", "https://relay.example.test")


def test_vendor_relay_token_issuer_requires_matching_pair_and_bounded_ttl(monkeypatch):
    seed = b"\x71" * 32
    _configure_pair(monkeypatch, seed)

    assert commercial.relay_token_issuer_ready() is True

    monkeypatch.setenv("ENGRAPHIS_RELAY_DEVICE_TOKEN_TTL_SECONDS", "299")
    assert commercial.relay_token_issuer_ready() is False
    monkeypatch.setenv("ENGRAPHIS_RELAY_DEVICE_TOKEN_TTL_SECONDS", "3601")
    assert commercial.relay_token_issuer_ready() is False
    monkeypatch.setenv("ENGRAPHIS_RELAY_DEVICE_TOKEN_TTL_SECONDS", "not-a-number")
    assert commercial.relay_token_issuer_ready() is False

    monkeypatch.delenv("ENGRAPHIS_RELAY_DEVICE_TOKEN_TTL_SECONDS")
    monkeypatch.setenv(
        "ENGRAPHIS_RELAY_TOKEN_PUBKEY",
        licensing.ed25519_public_key(b"\x72" * 32).hex(),
    )
    assert commercial.relay_token_issuer_ready() is False


def test_managed_relay_verifier_contract_needs_relay_mode_key_and_storage(monkeypatch):
    seed = b"\x73" * 32
    monkeypatch.setattr(settings, "service_mode", "relay")
    monkeypatch.setattr(commercial, "_registry_writable", lambda: True)
    monkeypatch.setattr(commercial, "_relay_disk_ok", lambda: True)
    monkeypatch.setenv(
        "ENGRAPHIS_RELAY_TOKEN_PUBKEY", licensing.ed25519_public_key(seed).hex())
    monkeypatch.setenv(
        "ENGRAPHIS_RELAY_TOKEN_AUDIENCE", "https://relay.example.test")

    assert commercial.managed_relay_verifier_readiness() == {
        "service_mode": True,
        "relay_token_verifier": True,
        "relay_db": True,
        "disk": True,
        "ready": True,
    }
    assert "ENGRAPHIS_RELAY_TOKEN_SIGNING_KEY" not in os.environ

    monkeypatch.setattr(settings, "service_mode", "vendor")
    wrong_role = commercial.managed_relay_verifier_readiness()
    assert wrong_role["service_mode"] is False and wrong_role["ready"] is False

    monkeypatch.setattr(settings, "service_mode", "relay")
    monkeypatch.setenv("ENGRAPHIS_RELAY_TOKEN_PUBKEY", "not-hex")
    invalid_key = commercial.managed_relay_verifier_readiness()
    assert invalid_key["relay_token_verifier"] is False
    assert invalid_key["ready"] is False

    monkeypatch.setenv(
        "ENGRAPHIS_RELAY_TOKEN_PUBKEY", licensing.ed25519_public_key(seed).hex())
    monkeypatch.setenv("ENGRAPHIS_RELAY_TOKEN_AUDIENCE", "https://relay.example.test/path")
    invalid_audience = commercial.managed_relay_verifier_readiness()
    assert invalid_audience["relay_token_verifier"] is False

    monkeypatch.setenv(
        "ENGRAPHIS_RELAY_TOKEN_AUDIENCE", "https://relay.example.test")
    monkeypatch.setattr(commercial, "_registry_writable", lambda: False)
    unavailable_storage = commercial.managed_relay_verifier_readiness()
    assert unavailable_storage["relay_db"] is False
    assert unavailable_storage["ready"] is False


def test_verifier_readiness_requires_bounded_structured_rotation_metadata(monkeypatch):
    current_seed = b"\x74" * 32
    previous_seed = b"\x75" * 32
    monkeypatch.setattr(settings, "service_mode", "relay")
    monkeypatch.setattr(commercial, "_registry_writable", lambda: True)
    monkeypatch.setattr(commercial, "_relay_disk_ok", lambda: True)
    monkeypatch.setenv(
        "ENGRAPHIS_RELAY_TOKEN_PUBKEY",
        licensing.ed25519_public_key(current_seed).hex(),
    )
    monkeypatch.setenv(
        "ENGRAPHIS_RELAY_TOKEN_AUDIENCE", "https://relay.example.test")
    cutoff = int(time.time()) + 60
    monkeypatch.setenv(
        "ENGRAPHIS_RELAY_TOKEN_PREVIOUS_KEYS",
        json.dumps([{
            "public_key": licensing.ed25519_public_key(previous_seed).hex(),
            "issued_before": cutoff,
            "not_after": cutoff + 3600,
        }]),
    )

    assert commercial.managed_relay_verifier_readiness()["ready"] is True

    monkeypatch.setenv(
        "ENGRAPHIS_RELAY_TOKEN_PREVIOUS_PUBKEYS",
        licensing.ed25519_public_key(previous_seed).hex(),
    )
    assert commercial.managed_relay_verifier_readiness()["ready"] is False


def test_vendor_serving_readiness_fails_when_relay_issuer_is_missing(monkeypatch):
    monkeypatch.setattr(settings, "service_mode", "vendor")
    monkeypatch.setattr(commercial, "_signer_matches", lambda: True)
    monkeypatch.setattr(commercial, "_registry_writable", lambda: True)
    monkeypatch.setattr(commercial, "_disk_ok", lambda: True)
    monkeypatch.setattr(commercial, "product_catalog", lambda: {
        str(index): {} for index in range(len(commercial.PRODUCT_ENV))})
    monkeypatch.setattr(licensing, "VENDOR_SIGNER_RELEASE_READY", True)

    from engraphis import billing
    from engraphis.inspector import webhooks
    monkeypatch.setattr(billing, "webhook_secret_ready", lambda: True)
    monkeypatch.setattr(billing, "webhook_state_ready", lambda **_kwargs: True)
    monkeypatch.setattr(webhooks, "email_configured", lambda: True)
    monkeypatch.setenv("POLAR_ORGANIZATION_ID", "configured")

    checks = commercial.vendor_serving_readiness()

    assert checks["relay_token_issuer"] is False
    assert checks["ready"] is False


def test_relay_app_exposes_only_transport_health_and_bounded_compat(monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from engraphis import relay_app

    monkeypatch.setattr(settings, "service_mode", "relay")
    ready = {
        "service_mode": True,
        "relay_token_verifier": True,
        "relay_db": True,
        "disk": True,
        "ready": True,
    }
    monkeypatch.setattr(relay_app, "managed_relay_verifier_readiness", lambda: ready)
    app = relay_app.create_app()

    paths = set()

    def collect(routes):
        for route in routes:
            path = getattr(route, "path", None)
            if path:
                paths.add(path)
            included = getattr(route, "original_router", None)
            if included is not None:
                collect(included.routes)

    collect(app.routes)
    assert "/api/health" in paths
    assert "/api/ready" in paths
    assert "/relay/v1/{workspace_id}/bundles" in paths
    assert "/license/v1/register" in paths
    assert "/license/v1/verify/{key_id}" in paths
    assert "/license/v1/{compat_path:path}" not in paths
    assert "/license/v1/trial-claims" not in paths
    assert "/api/auth/state" not in paths
    assert "/api/workspaces" not in paths
    assert "/license/v1/device-token" not in paths

    with TestClient(app) as client:
        assert client.get("/api/health").json()["service"] == "relay"
        response = client.get("/api/ready")
        assert response.status_code == 200
        assert response.json()["checks"] == ready
        assert client.post("/license/v1/device-token", json={}).status_code == 404
