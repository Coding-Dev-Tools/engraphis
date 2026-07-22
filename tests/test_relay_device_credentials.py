"""Cloud Sync accepts only short-lived scoped bearer credentials."""
from __future__ import annotations

import io
import json
import socket
import urllib.error

import pytest

from engraphis.backends import sync_relay as relay_backend
from engraphis.backends.sync_relay import RelayError, RelayTransport


TOKEN = "engr_access_" + "a" * 48


def test_relay_url_dns_resolution_failure_fails_closed(monkeypatch):
    def fail_resolution(*args, **kwargs):
        raise socket.gaierror("private resolver detail")

    monkeypatch.setattr(socket, "getaddrinfo", fail_resolution)

    with pytest.raises(ValueError, match="could not be resolved"):
        RelayTransport("https://unresolved.example", "workspace", access_token=TOKEN)


def test_scoped_bearer_is_the_only_authorization_sent(monkeypatch):
    requests = []

    class _Response:
        def read(self, _limit=-1):
            return b'{"names":[]}'

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def open_request(request, *, timeout):
        requests.append(request)
        return _Response()

    monkeypatch.setattr(relay_backend, "_urlopen_no_redirect", open_request)
    transport = RelayTransport(
        "http://127.0.0.1", "workspace", access_token=TOKEN
    )

    assert transport.list_names() == []
    assert requests[0].headers["Authorization"] == "Bearer " + TOKEN
    assert "X-engraphis-machine-id" not in requests[0].headers


def test_retired_paid_key_is_rejected_before_network(monkeypatch):
    monkeypatch.setattr(
        relay_backend,
        "_urlopen_no_redirect",
        lambda *_args, **_kwargs: pytest.fail("paid key reached the relay"),
    )

    with pytest.raises(RelayError, match="legacy license keys") as caught:
        RelayTransport(
            "http://127.0.0.1",
            "workspace",
            access_token="ENGR1.payload.signature",
        )

    assert caught.value.status == 401


def test_legacy_named_parameter_is_only_a_bearer_alias():
    transport = RelayTransport(
        "http://127.0.0.1", "workspace", license_key=TOKEN
    )
    assert transport.key == TOKEN

    with pytest.raises(ValueError, match="access_token only"):
        RelayTransport(
            "http://127.0.0.1",
            "workspace",
            access_token=TOKEN,
            license_key=TOKEN,
        )


def test_saved_bearer_is_never_forwarded_to_another_relay(monkeypatch):
    relay_backend.save_sync_token(
        TOKEN, relay_origin="https://1.1.1.1"
    )
    monkeypatch.setattr(
        relay_backend,
        "_urlopen_no_redirect",
        lambda *_args, **_kwargs: pytest.fail("mismatched token reached the network"),
    )

    with pytest.raises(RelayError, match="another relay") as caught:
        RelayTransport("https://8.8.8.8", "workspace")

    assert caught.value.status == 409


def test_saved_bearer_metadata_contains_no_entitlement_claims():
    relay_backend.save_sync_token(TOKEN, relay_origin="http://127.0.0.1")

    metadata = json.loads(
        relay_backend._sync_token_meta_path().read_text(encoding="utf-8")
    )

    assert set(metadata) == {"v", "relay_origin", "token_sha256"}
    assert TOKEN not in metadata.values()


def test_auth_failure_is_not_automatically_replayed(monkeypatch):
    calls = []

    def reject(request, *, timeout):
        calls.append((request.method, request.data))
        raise urllib.error.HTTPError(
            request.full_url, 401, "expired", None, io.BytesIO(b"")
        )

    monkeypatch.setattr(relay_backend, "_urlopen_no_redirect", reject)
    transport = RelayTransport(
        "http://127.0.0.1", "workspace", access_token=TOKEN
    )

    with pytest.raises(RelayError) as caught:
        transport.push("bundle.json", b"one-upload")

    assert caught.value.status == 401
    assert calls == [("POST", b"one-upload")]


def test_relay_client_refuses_redirects():
    handler = relay_backend._NoRedirectHandler()
    assert handler.redirect_request(
        object(), None, 307, "Temporary Redirect", {}, "https://attacker.invalid"
    ) is None
