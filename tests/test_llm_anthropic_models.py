"""Anthropic request shape and response parsing across Claude model generations."""
# ruff: noqa: E402 -- optional-stack guard must run before HTTP-dependent modules
from __future__ import annotations

import json
from unittest import mock

import pytest

httpx = pytest.importorskip(
    "httpx", reason="provider-boundary coverage requires the optional HTTP stack"
)

from engraphis.config import Settings
from engraphis.llm.client import (
    LLMClient,
    _anthropic_model_traits,
    _ANTHROPIC_THINKING_MIN_TOKENS,
)


class _Recorder:
    """Stands in for ``httpx.Client`` and records the request body it is given."""

    def __init__(self, payload):
        self.payload = payload
        self.bodies = []

    def post(self, url, **kwargs):
        self.bodies.append(kwargs["json"])
        return httpx.Response(200, request=httpx.Request("POST", url), text=json.dumps(self.payload))


def _client(model, payload, **kwargs):
    # Install the synthetic transport before construction so request-shape
    # coverage never depends on host proxy configuration or optional SOCKS extras.
    with mock.patch.object(httpx, "Client", return_value=_Recorder(payload)):
        return LLMClient(provider="anthropic", model=model, api_key="test-key", **kwargs)


_TEXT = {"content": [{"type": "text", "text": "ok"}]}


@pytest.mark.parametrize("model,accepts_sampling,thinks", [
    ("claude-opus-5-5", False, True),
    ("claude-opus-5", False, True),
    ("claude-sonnet-5-5", False, True),
    ("claude-sonnet-5", False, True),
    ("claude-fable-5-1", False, True),
    ("claude-fable-5", False, True),
    ("claude-mythos-5-1", False, True),
    ("claude-mythos-preview", False, True),
    ("anthropic.claude-mythos-preview", False, True),
    ("claude-fabled-1", True, False),
    ("claude-opus-4-8", False, False),
    ("claude-opus-4-7", False, False),
    ("claude-opus-4-6", True, False),
    ("claude-sonnet-4-6", True, False),
    ("claude-haiku-4-5", True, False),
    ("claude-opus-4-5-20251101", True, False),
    ("claude-opus-4-20250514", True, False),
    ("anthropic.claude-opus-5-5", False, True),
    ("claude-3-5-sonnet-20241022", True, False),
    ("claude-3", True, False),
    ("", True, False),
])
def test_model_traits_follow_the_documented_generations(model, accepts_sampling, thinks):
    assert _anthropic_model_traits(model) == (accepts_sampling, thinks)


@pytest.mark.parametrize("model", ["claude-opus-5-5", "claude-sonnet-5-5", "claude-opus-4-7",
                                   "claude-mythos-preview"])
def test_sampling_parameters_are_omitted_for_models_that_reject_them(model):
    client = _client(model, _TEXT)
    assert client.chat([{"role": "user", "content": "hi"}], temperature=0.0) == "ok"
    assert "temperature" not in client._http.bodies[0]


@pytest.mark.parametrize("model", ["claude-opus-4-6", "claude-haiku-4-5", "claude-3-5-sonnet-20241022"])
def test_sampling_parameters_are_kept_for_models_that_accept_them(model):
    client = _client(model, _TEXT)
    client.chat([{"role": "user", "content": "hi"}], temperature=0.2, max_tokens=700)
    body = client._http.bodies[0]
    assert body["temperature"] == 0.2
    assert body["max_tokens"] == 700
    assert "output_config" not in body


def test_thinking_models_get_effort_and_output_headroom():
    client = _client("claude-sonnet-5-5", _TEXT, effort="low")
    client.chat([{"role": "user", "content": "hi"}], max_tokens=700)
    body = client._http.bodies[0]
    assert body["output_config"] == {"effort": "low"}
    assert body["max_tokens"] == _ANTHROPIC_THINKING_MIN_TOKENS
    assert "thinking" not in body  # thinking cannot be disabled on these models

    client.chat([{"role": "user", "content": "hi"}], max_tokens=8192)
    assert client._http.bodies[1]["max_tokens"] == 8192


def test_effort_defaults_to_the_configured_setting(monkeypatch):
    monkeypatch.delenv("ENGRAPHIS_LLM_EFFORT", raising=False)
    assert Settings().llm_effort == "medium"
    monkeypatch.setenv("ENGRAPHIS_LLM_EFFORT", " XHigh ")
    assert Settings().llm_effort == "xhigh"
    monkeypatch.setenv("ENGRAPHIS_LLM_EFFORT", "turbo")
    assert Settings().llm_effort == "medium"


def test_reply_is_read_from_text_blocks_after_a_leading_thinking_block():
    payload = {"content": [
        {"type": "thinking", "thinking": "", "signature": "sig"},
        {"type": "text", "text": "hello "},
        {"type": "text", "text": "world"},
    ]}
    client = _client("claude-opus-5-5", payload)
    assert client.chat([{"role": "user", "content": "hi"}]) == "hello world"


@pytest.mark.parametrize("payload", [
    {"content": []},
    {"content": [{"type": "thinking", "thinking": ""}]},
    {"content": None},
    {"content": "not-a-list"},
    {"private": "provider-payload-secret"},
    [],
])
def test_reply_without_text_is_a_sanitized_error(payload):
    client = _client("claude-opus-5-5", payload)
    with pytest.raises(ValueError) as caught:
        client.chat([{"role": "user", "content": "hi"}])
    assert str(caught.value) == "Unexpected Anthropic response format"
    assert "provider-payload-secret" not in repr(caught.value)
