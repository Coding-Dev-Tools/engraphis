"""Config wiring tests — env vars must reach Settings, and the offline defaults must hold.

Covers the ENGRAPHIS_RERANK_MODEL knob added so the cross-encoder reranker (the biggest
precision win on top of hybrid retrieval) can be turned on by config
instead of only in code. The default must stay empty so the offline/numpy-only CI path is
unchanged (empty -> None -> IdentityReranker, no torch).
"""
import pytest

from engraphis import config
from engraphis.config import Settings


RETIRED_RELAY_URL = "https://engraphis-production.up.railway.app"


def test_rerank_model_defaults_to_empty(monkeypatch):
    monkeypatch.delenv("ENGRAPHIS_RERANK_MODEL", raising=False)
    assert Settings().rerank_model == ""


def test_cors_default_origins_follow_configured_port():
    # The empty-CORS default derives loopback origins from the port, so running on a
    # non-default ENGRAPHIS_PORT doesn't lock the dashboard's own origin out.
    assert config._parse_origins("", 9000) == [
        "http://127.0.0.1:9000", "http://localhost:9000"]
    # Explicit origins pass through unchanged.
    assert config._parse_origins("https://app.example.com", 9000) == [
        "https://app.example.com"]


def test_cors_origins_use_engraphis_port_env(monkeypatch):
    monkeypatch.delenv("ENGRAPHIS_CORS_ORIGINS", raising=False)
    monkeypatch.setenv("ENGRAPHIS_PORT", "9100")
    assert Settings().cors_origins == [
        "http://127.0.0.1:9100", "http://localhost:9100"]


def test_rerank_model_read_from_env(monkeypatch):
    monkeypatch.setenv("ENGRAPHIS_RERANK_MODEL", "cross-encoder/ms-marco-MiniLM-L-6-v2")
    assert Settings().rerank_model == "cross-encoder/ms-marco-MiniLM-L-6-v2"


def test_empty_rerank_model_normalizes_to_none(monkeypatch):
    # This is the exact expression the service builders pass:
    #   MemoryService.create(..., rerank_model=settings.rerank_model or None)
    # Empty must become None so get_reranker returns the offline IdentityReranker.
    monkeypatch.delenv("ENGRAPHIS_RERANK_MODEL", raising=False)
    assert (Settings().rerank_model or None) is None


def test_service_builds_offline_with_default_rerank_model(monkeypatch):
    # End-to-end: with no rerank model configured, a MemoryService builds on numpy alone
    # (DeterministicEmbedder + IdentityReranker) and serves a round-trip — the CI path.
    monkeypatch.delenv("ENGRAPHIS_RERANK_MODEL", raising=False)
    from engraphis.service import MemoryService
    svc = MemoryService.create(":memory:", rerank_model=(Settings().rerank_model or None))
    assert svc.remember("a durable fact", workspace="w", repo="r")["stored"] is True
    assert svc.recall("a durable fact", workspace="w", repo="r")["count"] >= 1


def test_embed_dim_defaults_to_default_model_dimension(monkeypatch):
    monkeypatch.delenv("ENGRAPHIS_EMBED_DIM", raising=False)
    assert Settings().embed_dim == 384


def test_license_server_url_precedence(monkeypatch):
    # Relay routing and commercial control-plane routing are intentionally independent.
    monkeypatch.setattr(config.settings, "relay_url", "https://relay.example/")
    monkeypatch.delenv("ENGRAPHIS_CLOUD_URL", raising=False)
    assert config.resolve_license_server_url() == config.DEFAULT_LICENSE_SERVER_URL
    assert config.resolve_license_server_url(
        "https://signed.example/",
    ) == "https://signed.example"

    monkeypatch.setenv("ENGRAPHIS_CLOUD_URL", "https://override.example/")
    assert config.resolve_license_server_url(
        "https://signed.example/",
    ) == "https://override.example"


def test_license_server_url_migrates_retired_signed_host(monkeypatch):
    monkeypatch.setattr(config.settings, "relay_url", config.DEFAULT_RELAY_URL)
    monkeypatch.delenv("ENGRAPHIS_CLOUD_URL", raising=False)
    assert config.resolve_license_server_url(
        "https://engraphis-production.up.railway.app/",
    ) == config.DEFAULT_LICENSE_SERVER_URL


def test_retired_cloud_url_override_is_canonicalized(monkeypatch):
    monkeypatch.setenv("ENGRAPHIS_CLOUD_URL", RETIRED_RELAY_URL + "/")
    assert (
        config.resolve_license_server_url("https://signed.example")
        == config.DEFAULT_LICENSE_SERVER_URL
    )


def test_retired_relay_url_override_is_canonicalized():
    assert config.canonicalize_relay_url(RETIRED_RELAY_URL) == config.DEFAULT_RELAY_URL

def test_invalid_service_mode_exits_process(monkeypatch):
    """Invalid ENGRAPHIS_SERVICE_MODE must fail-closed (sys.exit), not silently fall back."""
    monkeypatch.setenv("ENGRAPHIS_SERVICE_MODE", "bogus")
    with pytest.raises(SystemExit):
        Settings()


def test_service_mode_defaults_to_customer_trust_domain(monkeypatch):
    monkeypatch.delenv("ENGRAPHIS_SERVICE_MODE", raising=False)
    configured = Settings()

    assert configured.service_mode == "customer"
    assert configured.customer_service is True
    assert configured.relay_service is False
    assert configured.vendor_service is False


def test_valid_service_modes_accepted(monkeypatch):
    # Relay, vendor, and combined modes remain available only when selected explicitly.
    for mode in ("customer", "relay", "vendor", "combined"):
        monkeypatch.setenv("ENGRAPHIS_SERVICE_MODE", mode)
        configured = Settings()
        assert configured.service_mode == mode
        assert configured.relay_service is (mode == "relay")
