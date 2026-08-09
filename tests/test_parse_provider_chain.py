"""Regression coverage for parse_provider_chain URL parsing and cost ceiling extraction."""
from __future__ import annotations

import os
from unittest import mock

import pytest

pytest.importorskip("httpx", reason="LLM provider client extra not installed")

from engraphis.llm.client import LLMProviderChain, parse_provider_chain


def _parse(env_value: str) -> LLMProviderChain:
    """Helper to parse a provider chain string without polluting real env."""
    with mock.patch.dict(os.environ, {"ENGRAPHIS_LLM_PROVIDERS": env_value}):
        return parse_provider_chain()


class TestParseProviderChainPortPreservation:
    """Verify that URL ports are never mistaken for cost ceilings."""

    def test_bare_port_without_path_is_preserved(self):
        """http://localhost:8080 must keep the port, not treat 8080 as a ceiling."""
        chain = _parse("openai:gpt-4o:sk-test:http://localhost:8080")
        assert len(chain._clients) == 1
        client = chain._clients[0]
        assert client.base_url == "http://localhost:8080"
        assert not (chain._cost_ceilings or {})

    def test_port_with_path_and_ceiling_is_parsed_correctly(self):
        """http://localhost:8080/v1:0.50 — port preserved, ceiling extracted."""
        chain = _parse("openai:gpt-4o:sk-test:http://localhost:8080/v1:0.50")
        assert len(chain._clients) == 1
        client = chain._clients[0]
        assert client.base_url == "http://localhost:8080/v1"
        assert chain._cost_ceilings is not None
        assert chain._cost_ceilings[0] == pytest.approx(0.50)

    def test_integer_ceiling_after_url_path_is_not_mistaken_for_port(self):
        chain = _parse("openai:gpt-4o:sk-test:https://api.example/v1:1")

        assert chain._clients[0].base_url == "https://api.example/v1"
        assert chain._cost_ceilings == {0: pytest.approx(1.0)}

    def test_low_bare_port_is_preserved(self):
        chain = _parse("openai:gpt-4o:sk-test:http://localhost:1")

        assert chain._clients[0].base_url == "http://localhost:1"
        assert not (chain._cost_ceilings or {})

    def test_https_api_url_with_ceiling(self):
        """Standard HTTPS API URL with trailing ceiling."""
        chain = _parse("openai:gpt-4o-mini:sk-abc:https://api.openai.com/v1:0.75")
        client = chain._clients[0]
        assert client.provider == "openai"
        assert client.model == "gpt-4o-mini"
        assert client.api_key == "sk-abc"
        assert client.base_url == "https://api.openai.com/v1"
        assert chain._cost_ceilings[0] == pytest.approx(0.75)

    def test_url_without_port_or_ceiling(self):
        """Plain URL with no port and no ceiling."""
        chain = _parse("anthropic:claude-3:sk-key:https://api.anthropic.com")
        client = chain._clients[0]
        assert client.base_url == "https://api.anthropic.com"
        assert not (chain._cost_ceilings or {})

    def test_high_port_number_not_treated_as_ceiling(self):
        """Port 9999 should not be parsed as a $9999 cost ceiling."""
        chain = _parse("custom:model:key:http://localhost:9999")
        client = chain._clients[0]
        assert client.base_url == "http://localhost:9999"
        assert not (chain._cost_ceilings or {})


class TestParseProviderChainMultiEntry:
    """Verify comma-separated multi-provider chains."""

    def test_two_providers_with_mixed_ceilings(self):
        raw = (
            "openai:gpt-4o:sk-a:https://api.openai.com/v1:1.00,"
            "anthropic:claude-3:sk-b:https://api.anthropic.com"
        )
        chain = _parse(raw)
        assert len(chain._clients) == 2
        assert chain._clients[0].provider == "openai"
        assert chain._clients[1].provider == "anthropic"
        assert chain._cost_ceilings is not None
        assert chain._cost_ceilings[0] == pytest.approx(1.00)
        assert 1 not in chain._cost_ceilings

    def test_empty_entries_are_skipped(self):
        chain = _parse("openai:gpt-4o:sk-a:https://api.openai.com/v1,,")
        assert len(chain._clients) == 1


class TestParseProviderChainFallback:
    """Verify fallback behavior when env var is empty or missing."""

    def test_empty_env_returns_default_chain(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            chain = parse_provider_chain()
        assert len(chain._clients) == 1

    def test_whitespace_only_env_returns_default_chain(self):
        chain = _parse("   ")
        assert len(chain._clients) == 1


class TestLLMProviderErrorConstruction:
    """Verify _LLMProviderError accepts both positional and keyword arguments."""

    def test_positional_string_arg_does_not_raise_type_error(self):
        from engraphis.llm.client import _LLMProviderError
        err = _LLMProviderError("All providers skipped: cost ceiling exceeded.")
        assert "cost ceiling" in str(err)

    def test_message_kwarg_takes_precedence(self):
        from engraphis.llm.client import _LLMProviderError
        err = _LLMProviderError(
            "positional ignored",
            message="keyword wins",
            status=429,
        )
        assert str(err) == "keyword wins"
        assert err.status == 429

    def test_status_kwarg_without_message(self):
        from engraphis.llm.client import _LLMProviderError
        err = _LLMProviderError(status=503)
        assert "503" in str(err)
        assert err.status == 503

    def test_unreachable_kwarg(self):
        from engraphis.llm.client import _LLMProviderError
        err = _LLMProviderError(unreachable=True)
        assert "reach" in str(err).lower()
        assert err.unreachable is True
