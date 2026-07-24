from __future__ import annotations

import socket

import pytest

from engraphis import hosted_client, licensing


def test_hosted_lifecycle_constants_keep_trial_and_grace_separate():
    assert hosted_client.TRIAL_DAYS == 3
    assert hosted_client.TRIAL_SECONDS == 259_200
    assert hosted_client.MAX_LOCAL_WRITE_GRACE_SECONDS == 86_400


def test_upgrade_urls_are_hosted_metadata_only(monkeypatch):
    monkeypatch.delenv("ENGRAPHIS_UPGRADE_URL", raising=False)
    monkeypatch.delenv("ENGRAPHIS_PRO_UPGRADE_URL", raising=False)
    monkeypatch.delenv("ENGRAPHIS_TEAM_UPGRADE_URL", raising=False)
    monkeypatch.delenv("ENGRAPHIS_CLOUD_URL", raising=False)

    assert hosted_client.upgrade_url("pro") == "https://api.engraphis.com/account"
    assert hosted_client.upgrade_url("team") == "https://api.engraphis.com/account"
    assert hosted_client.required_plan("sync") == "pro"
    assert hosted_client.required_plan("team") == "team"


def test_cloud_url_validation_requires_safe_remote_https(monkeypatch):
    monkeypatch.setattr(
        hosted_client.socket,
        "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("1.2.3.4", 0))],
    )
    assert hosted_client.validate_cloud_base_url("http://127.0.0.1:8700/") == (
        "http://127.0.0.1:8700"
    )
    assert hosted_client.validate_cloud_base_url("https://cloud.example/path/") == (
        "https://cloud.example/path"
    )

    for invalid in (
        "http://cloud.example",
        "https://user:secret@cloud.example",
        "https://cloud.example/path?secret=value",
    ):
        with pytest.raises(ValueError):
            hosted_client.validate_cloud_base_url(invalid)


def test_cloud_url_validation_rejects_unresolvable_hosts(monkeypatch):
    monkeypatch.setattr(
        hosted_client.socket,
        "getaddrinfo",
        lambda *a, **k: (_ for _ in ()).throw(hosted_client.socket.gaierror),
    )
    with pytest.raises(ValueError, match="could not be resolved"):
        hosted_client.validate_cloud_base_url("https://unresolvable.example/")


@pytest.mark.parametrize("address", ["10.0.0.1", "100.64.0.1", "169.254.169.254"])
def test_cloud_url_validation_rejects_every_non_global_address(address):
    with pytest.raises(ValueError, match="private/reserved"):
        hosted_client.validate_cloud_base_url("https://%s" % address)


def test_pinned_https_connection_uses_vetted_address_and_original_tls_name(monkeypatch):
    monkeypatch.setattr(
        hosted_client.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )
    connected = []
    wrapped = []

    class _Context:
        def wrap_socket(self, sock, *, server_hostname):
            wrapped.append(server_hostname)
            return sock

    connection = hosted_client.PinnedHTTPSConnection("cloud.example")
    connection._context = _Context()
    connection._create_connection = (
        lambda address, timeout, source: connected.append(address) or object()
    )

    connection.connect()

    assert connected == [("93.184.216.34", 443)]
    assert wrapped == ["cloud.example"]


def test_pinned_https_connection_rejects_rebound_private_address(monkeypatch):
    monkeypatch.setattr(
        hosted_client.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
        ],
    )
    connection = hosted_client.PinnedHTTPSConnection("cloud.example")
    connection._create_connection = lambda *args: pytest.fail(
        "private rebound address reached the socket"
    )

    with pytest.raises(ValueError, match="private/reserved"):
        connection.connect()


def test_pinned_https_proxy_tunnel_uses_vetted_ip_and_original_tls_name(monkeypatch):
    monkeypatch.setattr(
        hosted_client.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )
    connection = hosted_client.PinnedHTTPSConnection("proxy.example")

    connection.set_tunnel("cloud.example", 443)

    assert connection._tunnel_host == "93.184.216.34"
    assert connection._tls_server_hostname == "cloud.example"


def test_licensing_facade_exposes_no_local_entitlement_engine():
    assert licensing.TRIAL_DAYS == 3
    assert licensing.production_warnings() == []
    for removed in (
        "activate",
        "compose_key",
        "current_license",
        "has_feature",
        "parse_key",
        "require_feature",
        "start_trial",
    ):
        assert not hasattr(licensing, removed)
