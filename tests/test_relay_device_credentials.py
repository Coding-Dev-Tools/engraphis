"""Client-side relay credential exchange: raw account keys never reach bundle routes."""
from __future__ import annotations

import base64
import io
import json
import time
import urllib.error
import urllib.parse

import pytest

from engraphis import cloud_license, licensing
from engraphis.backends import sync_relay as relay_backend
from engraphis.backends.sync_relay import RelayError, RelayTransport
from engraphis.licensing import ed25519_public_key


SECRET = bytes(range(32))


def _fake_token(key_id: str = "a" * 12, marker: str = "one", *,
                expires: float | None = None) -> str:
    payload = json.dumps({
        "aud": "http://127.0.0.1",
        "account_id": "org_" + "1" * 32,
        "expires": int(time.time()) + 3600 if expires is None else expires,
        "key_id": key_id,
        "marker": marker,
    }, separators=(",", ":"), sort_keys=True).encode("utf-8")
    body = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return "ENGRDT1.%s.%s" % (body, "s" * 32)


def _fake_account_token(key_id: str, account_digit: str, *,
                        expires: float | None = None) -> str:
    payload = json.dumps({
        "aud": "http://127.0.0.1",
        "account_id": "org_" + account_digit * 32,
        "expires": int(time.time()) + 3600 if expires is None else expires,
        "key_id": key_id,
    }, separators=(",", ":"), sort_keys=True).encode("utf-8")
    body = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return "ENGRDT1.%s.%s" % (body, "s" * 32)


TOKEN_ONE = _fake_token()
TOKEN_TWO = _fake_token(marker="two")


def _key() -> str:
    now = int(time.time())
    return licensing.compose_key({
        "v": 1,
        "plan": "pro",
        "email": "buyer@example.com",
        "seats": 1,
        "issued": now,
        "expires": now + 86400,
        "cloud_url": "http://127.0.0.1",
    }, SECRET)


@pytest.fixture(autouse=True)
def _credential_env(monkeypatch, tmp_path):
    monkeypatch.setenv("ENGRAPHIS_LICENSE_PUBKEY", ed25519_public_key(SECRET).hex())
    monkeypatch.setenv("ENGRAPHIS_CLOUD_URL", "http://127.0.0.1")
    monkeypatch.setenv("ENGRAPHIS_STATE_DIR", str(tmp_path))
    monkeypatch.setenv(
        "ENGRAPHIS_RELAY_TOKEN_AUDIENCE", "https://relay.example.test")
    monkeypatch.delenv("ENGRAPHIS_SYNC_TOKEN", raising=False)


def test_device_token_exchange_posts_key_only_to_control_plane(monkeypatch):
    captured = {}

    class _Response:
        def read(self, limit=-1):
            return json.dumps({"device_token": TOKEN_ONE}).encode("utf-8")[:limit]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_open(req, *, timeout):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _Response()

    monkeypatch.setattr(cloud_license, "_urlopen_no_redirect", fake_open)
    token = cloud_license.request_relay_device_token(
        "http://127.0.0.1", "ENGR1.account-key", "device-1")

    assert token == TOKEN_ONE
    assert captured == {
        "url": "http://127.0.0.1/license/v1/device-token",
        "body": {"key": "ENGR1.account-key", "machine_id": "device-1"},
    }


def test_raw_license_is_exchanged_and_only_device_token_is_sent(monkeypatch):
    raw_key = _key()
    exchanged = []
    sent_authorization = []

    monkeypatch.setattr(cloud_license, "machine_id", lambda: "device-1")

    def exchange(base, key, mid, **_kwargs):
        exchanged.append((base, key, mid))
        return TOKEN_ONE

    monkeypatch.setattr(cloud_license, "request_relay_device_token", exchange)

    class _Response:
        def read(self, _limit=-1):
            return b'{"names":[]}'

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def relay_open(req, *, timeout):
        sent_authorization.append(req.headers["Authorization"])
        return _Response()

    monkeypatch.setattr(relay_backend, "_urlopen_no_redirect", relay_open)
    transport = RelayTransport(
        "http://127.0.0.1", "workspace", license_key=raw_key)

    assert transport.list_names() == []
    assert exchanged == [("http://127.0.0.1", raw_key, "device-1")]
    assert sent_authorization == ["Bearer " + TOKEN_ONE]
    assert raw_key not in sent_authorization[0]
    assert relay_backend._sync_token_path().read_text(encoding="utf-8").strip() == TOKEN_ONE
    binding = json.loads(
        relay_backend._sync_token_meta_path().read_text(encoding="utf-8"))
    assert binding["expires"] == relay_backend._unverified_device_token_claims(
        TOKEN_ONE)["expires"]


def test_expired_device_token_refreshes_once_without_relaying_license(monkeypatch):
    raw_key = _key()
    key_id = licensing.parse_key(raw_key).key_id
    token_one = _fake_token(key_id, "one")
    token_two = _fake_token(key_id, "two")
    issued = iter((token_one, token_two))
    monkeypatch.setattr(cloud_license, "machine_id", lambda: "device-1")
    monkeypatch.setattr(
        cloud_license, "request_relay_device_token",
        lambda *_args, **_kwargs: next(issued),
    )

    sent_authorization = []

    class _Response:
        def read(self, _limit=-1):
            return b'{"names":[]}'

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def relay_open(req, *, timeout):
        sent_authorization.append(req.headers["Authorization"])
        if len(sent_authorization) == 1:
            raise urllib.error.HTTPError(
                req.full_url, 401, "expired", None, io.BytesIO(b""))
        return _Response()

    monkeypatch.setattr(relay_backend, "_urlopen_no_redirect", relay_open)
    transport = RelayTransport(
        "http://127.0.0.1", "workspace", license_key=raw_key)

    assert transport.list_names() == []
    assert sent_authorization == ["Bearer " + token_one, "Bearer " + token_two]
    assert all(raw_key not in header for header in sent_authorization)


def test_near_expiry_device_token_refreshes_before_upload(monkeypatch):
    raw_key = _key()
    key_id = licensing.parse_key(raw_key).key_id
    near_expiry = _fake_token(key_id, "near", expires=time.time() + 5)
    fresh = _fake_token(key_id, "fresh", expires=time.time() + 3600)
    issued = iter((near_expiry, fresh))
    exchanges = []

    monkeypatch.setattr(cloud_license, "machine_id", lambda: "device-1")

    def exchange(*args, **_kwargs):
        exchanges.append(args)
        return next(issued)

    monkeypatch.setattr(cloud_license, "request_relay_device_token", exchange)
    uploads = []

    class _Response:
        def read(self, _limit=-1):
            return b""

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def relay_open(req, *, timeout):
        uploads.append((req.headers["Authorization"], req.data))
        return _Response()

    monkeypatch.setattr(relay_backend, "_urlopen_no_redirect", relay_open)
    transport = RelayTransport(
        "http://127.0.0.1", "workspace", license_key=raw_key)
    payload = b"large-bundle-placeholder"

    transport.push("bundle-device-1.json", payload)

    assert len(exchanges) == 2
    assert uploads == [("Bearer " + fresh, payload)]


def test_upload_auth_failure_refreshes_but_never_replays_body(monkeypatch):
    raw_key = _key()
    key_id = licensing.parse_key(raw_key).key_id
    first = _fake_token(key_id, "first")
    refreshed = _fake_token(key_id, "refreshed")
    issued = iter((first, refreshed))
    monkeypatch.setattr(cloud_license, "machine_id", lambda: "device-1")
    monkeypatch.setattr(
        cloud_license, "request_relay_device_token",
        lambda *_args, **_kwargs: next(issued),
    )
    uploads = []

    def reject(req, *, timeout):
        uploads.append((req.headers["Authorization"], req.data))
        raise urllib.error.HTTPError(
            req.full_url, 401, "stale", None, io.BytesIO(b""))

    monkeypatch.setattr(relay_backend, "_urlopen_no_redirect", reject)
    transport = RelayTransport(
        "http://127.0.0.1", "workspace", license_key=raw_key)

    with pytest.raises(RelayError, match="was not replayed") as exc:
        transport.push("bundle-device-1.json", b"one-upload-only")

    assert exc.value.status == 401
    assert uploads == [("Bearer " + first, b"one-upload-only")]
    assert transport.key == refreshed


def test_transient_device_exchange_error_is_not_reported_as_missing_token(monkeypatch):
    def unavailable(req, *, timeout):
        raise urllib.error.HTTPError(
            req.full_url, 503, "unavailable", None, io.BytesIO(b""))

    monkeypatch.setattr(cloud_license, "_urlopen_no_redirect", unavailable)

    with pytest.raises(RelayError, match="temporarily unavailable") as exc:
        RelayTransport("http://127.0.0.1", "workspace", license_key=_key())

    assert exc.value.status == 503


def test_activation_clears_old_device_token_but_preserves_read_only_policy():
    old_key = _key()
    old_id = licensing.parse_key(old_key).key_id
    relay_backend.save_sync_token(
        _fake_account_token(old_id, "1"), relay_origin="http://127.0.0.1")
    relay_backend.save_sync_read_only(True)
    now = int(time.time())
    replacement = licensing.compose_key({
        "v": 1, "plan": "pro", "email": "replacement@example.com", "seats": 1,
        "issued": now, "expires": now + 86400,
        "cloud_url": "http://127.0.0.1",
    }, SECRET)

    licensing.activate(replacement)

    assert relay_backend.has_sync_token() is False
    assert relay_backend.sync_read_only() is True


def test_device_token_exchange_refuses_redirects():
    handler = cloud_license._NoRedirectHandler()
    request = object()
    assert handler.redirect_request(
        request, None, 307, "Temporary Redirect", {}, "https://attacker.invalid",
    ) is None


def test_saved_token_is_never_forwarded_to_another_relay(monkeypatch):
    relay_backend.save_sync_token(
        "engr_ut_" + "u" * 40, relay_origin="https://relay-one.example")
    monkeypatch.setattr(
        relay_backend, "_urlopen_no_redirect",
        lambda *_args, **_kwargs: pytest.fail("mismatched token reached the network"),
    )

    with pytest.raises(RelayError, match="another relay") as exc:
        RelayTransport("https://relay-two.example", "workspace")
    assert exc.value.status == 409


def test_saved_device_token_metadata_backfills_expiry_without_stranding(monkeypatch):
    raw_key = _key()
    key_id = licensing.parse_key(raw_key).key_id
    token = _fake_account_token(key_id, "1")
    monkeypatch.setenv("ENGRAPHIS_LICENSE_KEY", raw_key)
    relay_backend.save_sync_token(token, relay_origin="http://127.0.0.1")
    meta_path = relay_backend._sync_token_meta_path()
    legacy = json.loads(meta_path.read_text(encoding="utf-8"))
    legacy.pop("expires")
    meta_path.write_text(json.dumps(legacy), encoding="utf-8")

    transport = RelayTransport("http://127.0.0.1", "workspace")

    assert transport.key == token
    repaired = json.loads(meta_path.read_text(encoding="utf-8"))
    assert repaired["expires"] == relay_backend._unverified_device_token_claims(
        token)["expires"]


def test_saved_device_token_is_replaced_after_license_account_switch(monkeypatch):
    first = _key()
    first_id = licensing.parse_key(first).key_id
    token = _fake_account_token(first_id, "1")
    relay_backend.save_sync_token(token, relay_origin="http://127.0.0.1")

    now = int(time.time())
    second = licensing.compose_key({
        "v": 1, "plan": "pro", "email": "other@example.com", "seats": 1,
        "issued": now, "expires": now + 86400,
        "cloud_url": "http://127.0.0.1",
    }, SECRET)
    monkeypatch.setenv("ENGRAPHIS_LICENSE_KEY", second)
    second_id = licensing.parse_key(second).key_id
    replacement = _fake_account_token(second_id, "2")
    exchanged = []

    def exchange(base, key, machine_id, **_kwargs):
        exchanged.append((base, key, machine_id))
        return replacement

    monkeypatch.setattr(cloud_license, "machine_id", lambda: "device-2")
    monkeypatch.setattr(cloud_license, "request_relay_device_token", exchange)

    RelayTransport("http://127.0.0.1", "workspace")

    assert exchanged == [("http://127.0.0.1", second, "device-2")]
    assert relay_backend._sync_token_path().read_text(
        encoding="utf-8").strip() == replacement


def test_refresh_stops_when_control_plane_changes_account_binding(monkeypatch):
    raw_key = _key()
    key_id = licensing.parse_key(raw_key).key_id
    old_token = _fake_account_token(key_id, "1")
    other_account_token = _fake_account_token(key_id, "2")
    issued = iter((old_token, other_account_token))
    monkeypatch.setattr(cloud_license, "machine_id", lambda: "device-1")
    monkeypatch.setattr(
        cloud_license, "request_relay_device_token",
        lambda *_args, **_kwargs: next(issued),
    )

    def expired(req, *, timeout):
        raise urllib.error.HTTPError(
            req.full_url, 401, "expired", None, io.BytesIO(b""))

    monkeypatch.setattr(relay_backend, "_urlopen_no_redirect", expired)
    transport = RelayTransport(
        "http://127.0.0.1", "workspace", license_key=raw_key)

    with pytest.raises(RelayError, match="changed account binding") as exc:
        transport.list_names()
    assert exc.value.status == 409
    assert relay_backend.has_sync_token() is False


def test_initial_device_token_exchange_failure_fails_before_bundle_request(monkeypatch):
    monkeypatch.setattr(cloud_license, "machine_id", lambda: "device-1")
    monkeypatch.setattr(
        cloud_license, "request_relay_device_token", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        relay_backend, "_urlopen_no_redirect",
        lambda *_args, **_kwargs: pytest.fail("bundle request used an empty bearer"),
    )

    with pytest.raises(RelayError, match="no relay credential") as exc:
        RelayTransport("http://127.0.0.1", "workspace", license_key=_key())
    assert exc.value.status == 503


def test_raw_license_exchange_and_relay_roundtrip_end_to_end(monkeypatch, tmp_path):
    """The only HTTP request containing ENGR1 is the token exchange request."""
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient

    from engraphis.config import settings
    from engraphis.inspector import license_cloud, license_registry, sync_relay
    from engraphis.licensing import LicenseError

    raw_key = _key()
    relay_secret = bytes(reversed(range(32)))
    monkeypatch.setenv("ENGRAPHIS_RELAY_DB", str(tmp_path / "relay.db"))
    monkeypatch.setenv("ENGRAPHIS_RELAY_TOKEN_SIGNING_KEY", relay_secret.hex())
    monkeypatch.setenv(
        "ENGRAPHIS_RELAY_TOKEN_PUBKEY", ed25519_public_key(relay_secret).hex())
    monkeypatch.setattr(settings, "service_mode", "combined")
    monkeypatch.setattr(cloud_license, "machine_id", lambda: "device-1")
    license_cloud._REGISTER_BUCKETS.clear()
    license_cloud._RELAY_BUCKETS.clear()
    license_registry.record_issued(raw_key)

    app = FastAPI()
    app.include_router(license_cloud.router)
    app.include_router(sync_relay.router)

    @app.exception_handler(LicenseError)
    async def license_error(_request, exc):
        return JSONResponse({"error": str(exc)}, status_code=402)

    client = TestClient(app)
    observed = []

    class _Response:
        def __init__(self, data):
            self.data = data

        def read(self, limit=-1):
            return self.data if limit < 0 else self.data[:limit]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def route_request(req, *, timeout):
        path = urllib.parse.urlsplit(req.full_url).path
        authorization = req.get_header("Authorization") or ""
        observed.append((path, authorization, req.data or b""))
        if req.method == "POST":
            response = client.post(path, content=req.data or b"", headers=dict(req.headers))
        else:
            response = client.get(path, headers=dict(req.headers))
        if response.status_code >= 400:
            raise urllib.error.HTTPError(
                req.full_url, response.status_code, response.text,
                None, io.BytesIO(response.content),
            )
        return _Response(response.content)

    monkeypatch.setattr(cloud_license, "_urlopen_no_redirect", route_request)
    monkeypatch.setattr(relay_backend, "_urlopen_no_redirect", route_request)

    transport = RelayTransport(
        "http://127.0.0.1", "workspace", license_key=raw_key)
    transport.push("bundle-device-1.json", b'{"memory":1}')
    assert transport.list_names() == ["bundle-device-1.json"]

    exchange = observed[0]
    assert exchange[0] == "/license/v1/device-token"
    assert raw_key.encode("utf-8") in exchange[2]
    relay_calls = observed[1:]
    assert relay_calls
    assert all(path.startswith("/relay/v1/") for path, _auth, _body in relay_calls)
    assert all(auth.startswith("Bearer ENGRDT1.") for _path, auth, _body in relay_calls)
    assert all(raw_key not in auth for _path, auth, _body in relay_calls)
