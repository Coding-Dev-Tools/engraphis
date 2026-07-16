"""Config wiring tests — env vars must reach Settings, and the offline defaults must hold.

Covers the ENGRAPHIS_RERANK_MODEL knob added so the cross-encoder reranker (the biggest
precision win on top of hybrid retrieval) can be turned on by config
instead of only in code. The default must stay empty so the offline/numpy-only CI path is
unchanged (empty -> None -> IdentityReranker, no torch).
"""
from engraphis.config import (
    DEFAULT_RELAY_URL,
    Settings,
    resolve_license_server_url,
    settings,
)


RETIRED_RELAY_URL = "https://engraphis-production.up.railway.app"


def test_rerank_model_defaults_to_empty(monkeypatch):
    monkeypatch.delenv("ENGRAPHIS_RERANK_MODEL", raising=False)
    assert Settings().rerank_model == ""


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


def test_retired_cloud_url_override_is_canonicalized(monkeypatch):
    monkeypatch.setenv("ENGRAPHIS_CLOUD_URL", RETIRED_RELAY_URL + "/")
    assert resolve_license_server_url("https://signed.example") == DEFAULT_RELAY_URL


def test_retired_relay_url_override_is_canonicalized(monkeypatch):
    monkeypatch.delenv("ENGRAPHIS_CLOUD_URL", raising=False)
    monkeypatch.setattr(settings, "relay_url", RETIRED_RELAY_URL)
    assert resolve_license_server_url() == DEFAULT_RELAY_URL
