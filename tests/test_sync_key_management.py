"""In-product Cloud Sync key generation.

A paying customer must be able to reach a working sync without editing an environment
variable. These tests pin the whole promise: one-time reveal, owner-only storage, explicit
rotation, environment precedence, and a status payload that never contains the key.
"""
from __future__ import annotations

import base64
import json
import os
import stat

import pytest

from engraphis.backends import sync_relay

pytest.importorskip("fastapi", reason="full-stack extra not installed")

from fastapi import HTTPException  # noqa: E402

from engraphis.routes import v2_api  # noqa: E402


KEY_A = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("=")
KEY_B = base64.urlsafe_b64encode(bytes(range(1, 33))).decode("ascii").rstrip("=")


@pytest.fixture()
def state_dir(monkeypatch, tmp_path):
    root = tmp_path / ".engraphis"
    monkeypatch.setenv("ENGRAPHIS_STATE_DIR", str(root))
    monkeypatch.delenv(sync_relay.SYNC_E2EE_KEY_ENV, raising=False)
    return root


def _payload(response):
    return json.loads(response.body)


def test_generated_key_round_trips_and_status_never_discloses_it(state_dir):
    response = v2_api.create_sync_key()
    payload = _payload(response)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert payload["configured"] is True
    assert payload["key_source"] == "state_file"
    assert payload["restart_required"] is False
    assert len(payload["key"]) == 43
    assert (state_dir / "sync.e2ee_key").is_file()

    key = payload["key"]
    assert sync_relay.configured_sync_e2ee_key() == sync_relay.decode_sync_e2ee_key(key)

    status = v2_api.sync_status()
    assert status["key_source"] == "state_file"
    assert status["has_key"] is True
    assert status["key_state"] == "ready"
    assert status["can_generate_key"] is False
    assert status["can_rotate_key"] is True
    assert key not in json.dumps(status)


def test_a_second_generation_refuses_to_overwrite(state_dir):
    first = _payload(v2_api.create_sync_key())["key"]

    with pytest.raises(HTTPException) as caught:
        v2_api.create_sync_key()

    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "sync_key_exists"
    assert sync_relay.configured_sync_e2ee_key() == sync_relay.decode_sync_e2ee_key(first)


def test_rotation_is_explicit_and_replaces_the_saved_key(state_dir):
    first = _payload(v2_api.create_sync_key())["key"]
    second = _payload(
        v2_api.create_sync_key(v2_api._SyncKeyReq(rotate=True))
    )["key"]

    assert second != first
    assert sync_relay.configured_sync_e2ee_key() == sync_relay.decode_sync_e2ee_key(second)


def test_an_environment_key_keeps_precedence_and_cannot_be_replaced(state_dir, monkeypatch):
    monkeypatch.setenv(sync_relay.SYNC_E2EE_KEY_ENV, KEY_A)
    sync_relay.save_sync_e2ee_key(KEY_B)

    assert sync_relay.sync_e2ee_key_source() == "environment"
    assert sync_relay.configured_sync_e2ee_key() == sync_relay.decode_sync_e2ee_key(KEY_A)

    with pytest.raises(HTTPException) as caught:
        v2_api.create_sync_key()

    assert caught.value.detail["code"] == "sync_key_managed_by_environment"


def test_saved_key_is_owner_only(state_dir):
    if os.name == "nt":
        pytest.skip("POSIX permission semantics")
    v2_api.create_sync_key()

    mode = stat.S_IMODE((state_dir / "sync.e2ee_key").stat().st_mode)
    assert mode == 0o600


def test_status_reports_an_invalid_saved_key_without_disclosing_it(state_dir):
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "sync.e2ee_key").write_text("not-a-valid-key", encoding="utf-8")

    status = v2_api.sync_status()

    assert status["key_source"] == "state_file"
    assert status["has_key"] is False
    assert status["key_state"] == "invalid"
    assert status["can_generate_key"] is False
    assert status["can_rotate_key"] is True
    assert "not-a-valid-key" not in json.dumps(status)


def test_configured_key_fails_closed_when_absent(state_dir):
    with pytest.raises(sync_relay.RelayError):
        sync_relay.configured_sync_e2ee_key()
    assert sync_relay.sync_e2ee_key_source() == "none"
