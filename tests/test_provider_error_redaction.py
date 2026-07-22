"""Regression coverage for secrets crossing external-provider error boundaries."""
# ruff: noqa: E402 -- optional-stack guard must run before HTTP-dependent modules
from __future__ import annotations

import asyncio
import io
import logging
from types import SimpleNamespace
import urllib.error

import pytest

httpx = pytest.importorskip(
    "httpx", reason="provider-boundary coverage requires the optional HTTP stack"
)

from engraphis.backends import sync_relay
from engraphis.backends.embedder_api import ApiEmbedder
from engraphis.backends.sync_relay import RelayError, RelayTransport, RelayUnreachable
from engraphis import cloud_license
from engraphis.cloud_license import create_trial_claim
from engraphis.core.engine import MemoryEngine
from engraphis.inspector import webhooks
from engraphis.llm.client import LLMClient, validate_llm_base_url
from engraphis.routes import v2_api


class _LLMResponseClient:
    def __init__(self, *, status: int, body: str) -> None:
        self.status = status
        self.body = body

    def post(self, url, **_kwargs):
        request = httpx.Request("POST", url)
        return httpx.Response(self.status, request=request, text=self.body)


@pytest.mark.parametrize("value", [
    "provider.example/v1",
    "http://provider.example/v1",
    "file:///private/provider",
    "https://user:private-credential-value@provider.example/v1",
    "https://provider.example:not-a-port/v1",
    "https://provider.example:0/v1",
    "https://provider.example:65536/v1",
    "https://[::1/v1",
    "https://provider.example/v1?token=private-credential-value",
    "https://provider.example/v1#private-credential-value",
    "https://provider.example/v1\nX-Secret: private-credential-value",
    " https://provider.example/v1",
    "https://provider.example\\@private-credential-value.example/v1",
])
def test_llm_base_url_rejects_credentialed_control_or_ambiguous_shapes(value):
    with pytest.raises(ValueError) as caught:
        LLMClient(
            provider="custom", model="safe", api_key="safe", base_url=value
        )
    assert "private-credential-value" not in str(caught.value)
    assert "private/provider" not in str(caught.value)


def test_llm_base_url_normalizes_a_path_without_disclosing_it():
    assert validate_llm_base_url("https://provider.example/custom/v1/") == (
        "https://provider.example/custom/v1"
    )
    assert validate_llm_base_url("http://[::1]:11434/v1/") == (
        "http://[::1]:11434/v1"
    )


def test_legacy_config_status_does_not_reflect_custom_llm_base_url(monkeypatch):
    from engraphis.config import settings
    from engraphis.routes.memory import get_config

    marker = "private-provider-route-secret"
    monkeypatch.setattr(
        settings, "llm_base_url", "https://provider.example/%s" % marker
    )
    payload = asyncio.run(get_config())["data"]

    assert payload["llm_custom_base_url_set"] is True
    assert "llm_base_url" not in payload
    assert marker not in repr(payload)


def test_llm_http_error_hides_key_url_model_and_provider_body(caplog):
    api_key = "query-key-should-not-escape"
    model = "private-model-owner@example.com"
    endpoint_marker = "private-customer-endpoint"
    body_marker = "provider-body-bearer-secret"
    client = LLMClient(
        provider="google",
        model=model,
        api_key=api_key,
        base_url="https://provider.example/%s" % endpoint_marker,
    )
    client._http.close()
    client._http = _LLMResponseClient(status=401, body=body_marker)

    with caplog.at_level(logging.DEBUG, logger="engraphis.llm"):
        with pytest.raises(RuntimeError) as caught:
            client.chat([{"role": "user", "content": "hello"}])

    exposed = "%s\n%s\n%s" % (caught.value, repr(caught.value), caplog.text)
    assert "HTTP 401" in exposed
    assert caught.value.__suppress_context__ is True
    for marker in (api_key, model, endpoint_marker, body_marker, "owner@example.com"):
        assert marker not in exposed


def test_llm_malformed_response_does_not_reflect_provider_payload():
    marker = "malformed-provider-payload-secret"
    client = LLMClient(
        provider="openai", model="safe-model", api_key="safe-key",
        base_url="https://provider.example",
    )
    client._http.close()
    client._http = _LLMResponseClient(status=200, body='{"private":"%s"}' % marker)

    with pytest.raises(ValueError) as caught:
        client.chat([{"role": "user", "content": "hello"}])

    assert str(caught.value) == "Unexpected LLM response format"
    assert marker not in repr(caught.value)
    assert caught.value.__suppress_context__ is True


def test_api_embedder_logs_no_model_endpoint_or_provider_index(monkeypatch, caplog):
    model_marker = "embedding-model-owner@example.com"
    endpoint_marker = "signed-endpoint-token"
    index_marker = "provider-index-secret"

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"index": index_marker}]}

    class _Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, *_args, **_kwargs):
            return _Response()

    monkeypatch.setattr(httpx, "Client", _Client)
    with caplog.at_level(logging.INFO, logger="engraphis.embedder_api"):
        embedder = ApiEmbedder(
            model=model_marker,
            base_url="https://provider.example/%s" % endpoint_marker,
            api_key="safe-key",
        )
        result = embedder.embed(["hello"])

    assert result.shape == (1, 384)
    for marker in (model_marker, endpoint_marker, index_marker, "owner@example.com"):
        assert marker not in caplog.text


def test_resend_error_does_not_reflect_provider_body(monkeypatch):
    marker = "provider-response-includes-recipient@example.com-and-secret"
    response = httpx.Response(
        400,
        request=httpx.Request("POST", "https://api.resend.com/emails"),
        text=marker,
    )
    monkeypatch.setattr(httpx, "post", lambda *_args, **_kwargs: response)

    with pytest.raises(RuntimeError) as caught:
        webhooks._send_via_resend_api(
            "recipient@example.com", "subject", "body", "sender@example.com", "key"
        )

    assert "HTTP 400" in str(caught.value)
    assert marker not in repr(caught.value)


def test_missing_signing_key_error_does_not_expose_private_path(tmp_path):
    private_marker = "customer@example.com-private-path"
    missing = tmp_path / private_marker / "signing.key"

    with pytest.raises(RuntimeError) as caught:
        webhooks._read_seed_file(missing)

    assert private_marker not in str(caught.value)
    assert str(missing) not in repr(caught.value)
    assert caught.value.__suppress_context__ is True


@pytest.mark.parametrize("status", [402, 500])
def test_relay_http_error_discards_response_body_and_request_url(monkeypatch, status):
    url_marker = "relay-customer@example.com"
    body_marker = "relay-response-secret"

    def fail(req, **_kwargs):
        raise urllib.error.HTTPError(
            req.full_url, status, "failed", None, io.BytesIO(body_marker.encode("utf-8"))
        )

    monkeypatch.setattr(sync_relay, "_urlopen_no_redirect", fail)
    transport = RelayTransport(
        "https://relay.example/%s" % url_marker, "workspace", license_key="safe-key"
    )

    with pytest.raises(RelayError) as caught:
        transport.list_names()

    assert caught.value.status == status
    assert caught.value.__suppress_context__ is True
    for marker in (url_marker, body_marker, "customer@example.com"):
        assert marker not in "%s\n%s" % (caught.value, repr(caught.value))


def test_relay_network_error_discards_base_url_and_reason(monkeypatch):
    marker = "relay-private-customer@example.com"
    monkeypatch.setattr(
        sync_relay,
        "_urlopen_no_redirect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(urllib.error.URLError(marker)),
    )
    transport = RelayTransport(
        "https://relay.example/%s" % marker, "workspace", license_key="safe-key"
    )

    with pytest.raises(RelayUnreachable) as caught:
        transport.list_names()

    assert marker not in "%s\n%s" % (caught.value, repr(caught.value))
    assert caught.value.__suppress_context__ is True


def test_trial_claim_error_discards_control_plane_body_and_request(monkeypatch):
    body_marker = "deployment-token-and-owner@example.com"
    url_marker = "private-trial-customer"

    def fail(req, **_kwargs):
        raise urllib.error.HTTPError(
            req.full_url, 400, "failed", None, io.BytesIO(
                ('{"error":"%s"}' % body_marker).encode("utf-8")
            )
        )

    monkeypatch.setattr(cloud_license, "_urlopen_no_redirect", fail)
    with pytest.raises(RuntimeError) as caught:
        create_trial_claim(
            "https://license.example/%s" % url_marker,
            "deployment-secret",
            "machine",
            "owner@example.com",
            "team",
        )

    assert "HTTP 400" in str(caught.value)
    assert caught.value.__suppress_context__ is True
    for marker in (body_marker, url_marker, "deployment-secret", "owner@example.com"):
        assert marker not in "%s\n%s" % (caught.value, repr(caught.value))


@pytest.mark.real_license_gate
def test_cloud_gate_error_does_not_reflect_configured_endpoint_path(monkeypatch):
    marker = "customer@example.com-private-license-path"
    monkeypatch.setattr(cloud_license, "machine_id", lambda: "machine")
    monkeypatch.setattr(cloud_license, "_valid_lease_for", lambda *_args: None)
    monkeypatch.setattr(cloud_license, "register", lambda *_args: None)

    allowed, reason = cloud_license.gate(
        SimpleNamespace(key_id="key-id"),
        "key-material",
        base_url="https://license.example/%s" % marker,
    )

    assert allowed is False
    assert "configured license server" in reason
    assert marker not in reason


def test_dashboard_sync_error_does_not_reflect_transport_exception(monkeypatch, caplog):
    marker = "https://relay.example/private?token=sync-secret"
    engine = MemoryEngine.create(":memory:")
    try:
        engine.store.get_or_create_workspace("shared")
        svc = SimpleNamespace(
            engine=engine,
            store=engine.store,
            list_workspaces=lambda: {
                "workspaces": [{"name": "shared", "visibility": "shared"}]
            },
        )

        def fail_transport(*_args, **_kwargs):
            raise RuntimeError(marker)

        monkeypatch.setattr("engraphis.backends.sync_folder.get_transport", fail_transport)
        with caplog.at_level(logging.ERROR, logger="engraphis.api"):
            result = v2_api._sync_all(svc)
    finally:
        engine.store.close()

    assert result["errors"] == [
        {"workspace": "shared", "error": "sync workspace failed"}
    ]
    assert marker not in caplog.text
