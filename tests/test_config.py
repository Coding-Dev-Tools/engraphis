"""Config wiring tests — env vars must reach Settings, and the offline defaults must hold.

Covers the ENGRAPHIS_RERANK_MODEL knob added so the cross-encoder reranker (the biggest
precision win on top of hybrid retrieval) can be turned on by config
instead of only in code. The default must stay empty so the offline/numpy-only CI path is
unchanged (empty -> None -> IdentityReranker, no torch).
"""
from pathlib import Path

import pytest

from engraphis import config
from engraphis.config import Settings


REPO_ROOT = Path(__file__).resolve().parents[1]
RETIRED_RELAY_URLS = (
    "https://engraphis-production.up.railway.app",
    "https://team.engraphis.com",
)


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


def test_sample_operational_config_matches_runtime_contract(monkeypatch):
    example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    monkeypatch.delenv("ENGRAPHIS_RATE_LIMIT", raising=False)
    assert Settings().rate_limit == 0
    assert "# ENGRAPHIS_RATE_LIMIT=0" in example

    monkeypatch.setenv("ENGRAPHIS_WORKSPACES", "acme,personal")
    assert Settings().allowed_workspaces == ["acme", "personal"]
    assert "# ENGRAPHIS_WORKSPACES=acme,personal" in example

    assert "http://127.0.0.1:<ENGRAPHIS_PORT>" in example
    assert "http://localhost:<ENGRAPHIS_PORT>" in example
    assert "These settings do not change CORS" in example
    assert "# ENGRAPHIS_DASHBOARD_URL=https://engraphis.example.com" in example
    assert "# ENGRAPHIS_API_TOKEN=<a-long-random-secret>" in example
    assert "docker-compose.lan.yml" in example
    assert "# ENGRAPHIS_DASHBOARD_URL=http://192.168.10.151:8700" in example
    assert "# ENGRAPHIS_DASHBOARD_URL=http://engraphis.local" in example

    monkeypatch.delenv("ENGRAPHIS_LLM_AUTO_EXTRACT", raising=False)
    assert Settings().llm_auto_extract is False
    assert "ENGRAPHIS_LLM_AUTO_EXTRACT=0" in example
    assert "| `ENGRAPHIS_LLM_AUTO_EXTRACT` | `0` |" in readme

    for name in (
        "ENGRAPHIS_DECAY_HALFLIFE_DAYS",
        "ENGRAPHIS_LOOP_INTERVAL",
        "ENGRAPHIS_LOOP_TOP_K",
    ):
        monkeypatch.delenv(name, raising=False)
    configured = Settings()
    assert f"# ENGRAPHIS_DECAY_HALFLIFE_DAYS={configured.decay_halflife_days:g}" in example
    assert f"# ENGRAPHIS_LOOP_INTERVAL={configured.loop_interval}" in example
    assert f"# ENGRAPHIS_LOOP_TOP_K={configured.loop_top_k}" in example

    from engraphis.backends.extractor import (
        CHUNK_MAX,
        CHUNK_OVERLAP_TOKENS,
        CHUNK_TARGET_TOKENS,
    )

    assert f"# ENGRAPHIS_CHUNK_TOKENS={CHUNK_TARGET_TOKENS}" in example
    assert f"# ENGRAPHIS_CHUNK_MAX={CHUNK_MAX}" in example
    assert f"# ENGRAPHIS_CHUNK_OVERLAP={CHUNK_OVERLAP_TOKENS}" in example


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
    from engraphis.backends.vector_numpy import NumpyVectorIndex
    svc = MemoryService.create(":memory:", rerank_model=(Settings().rerank_model or None))
    assert isinstance(svc.engine.index, NumpyVectorIndex)
    pending = svc.remember("a durable fact", workspace="w", repo="r")
    assert pending["stored"] is True
    svc.engine.approve_for_prompt(
        pending["id"], reviewer="test_operator", reason="offline control",
    )
    assert svc.recall("a durable fact", workspace="w", repo="r")["count"] >= 1


def test_embed_dim_defaults_to_default_model_dimension(monkeypatch):
    monkeypatch.delenv("ENGRAPHIS_EMBED_DIM", raising=False)
    assert Settings().embed_dim == 384


def test_vector_backend_defaults_to_numpy(monkeypatch):
    monkeypatch.delenv("ENGRAPHIS_VECTOR_BACKEND", raising=False)
    assert Settings().vector_backend == "numpy"


def test_vector_backend_reads_env(monkeypatch):
    monkeypatch.setenv("ENGRAPHIS_VECTOR_BACKEND", "sqlite-vec")
    assert Settings().vector_backend == "sqlite-vec"



@pytest.mark.parametrize("raw", ("", "unsupported", " sqlite vec "))
def test_vector_backend_invalid_values_fail_closed_to_numpy(monkeypatch, raw):
    monkeypatch.setenv("ENGRAPHIS_VECTOR_BACKEND", raw)
    assert Settings().vector_backend == "numpy"


def test_blank_llm_provider_uses_documented_default(monkeypatch):
    monkeypatch.setenv("ENGRAPHIS_LLM_PROVIDER", " ")
    assert Settings().llm_provider == "openai"


def test_malformed_llm_headers_are_ignored(monkeypatch):
    monkeypatch.setenv("ENGRAPHIS_LLM_EXTRA_HEADERS", "[]")
    assert Settings().llm_extra_headers == {}
    monkeypatch.setenv("ENGRAPHIS_LLM_EXTRA_HEADERS", '{"X-Test": 1}')
    assert Settings().llm_extra_headers == {}


@pytest.mark.parametrize("raw", ("nan", "inf", "-inf", "not-a-number"))
def test_nonfinite_or_malformed_decay_halflife_uses_default(monkeypatch, raw):
    monkeypatch.setenv("ENGRAPHIS_DECAY_HALFLIFE_DAYS", raw)
    assert Settings().decay_halflife_days == 7.0


def test_malformed_boolean_values_use_safe_default(monkeypatch):
    monkeypatch.setenv("ENGRAPHIS_LLM_AUTO_EXTRACT", "perhaps")
    assert Settings().llm_auto_extract is False
    monkeypatch.setenv("ENGRAPHIS_UPDATE_CHECK", "perhaps")
    assert Settings().update_check is False

@pytest.mark.parametrize("url", RETIRED_RELAY_URLS)
def test_retired_relay_url_override_is_canonicalized(url):
    assert config.canonicalize_relay_url(url) == config.DEFAULT_RELAY_URL


def test_customer_relay_url_is_not_rewritten():
    url = "https://relay.customer.example/team/"
    assert config.canonicalize_relay_url(url) == url.rstrip("/")

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


def test_private_service_modes_are_not_available_in_the_public_package(monkeypatch):
    for mode in ("relay", "vendor", "combined"):
        monkeypatch.setenv("ENGRAPHIS_SERVICE_MODE", mode)
        with pytest.raises(SystemExit):
            Settings()
