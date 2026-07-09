"""External LLM client — supports OpenAI, Anthropic, Google, OpenRouter, and
any OpenAI-compatible custom endpoint.

No provider SDK dependencies — uses httpx directly so the only network dep is
already in requirements. All providers are reached via their REST API.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

import httpx

from engraphis.config import settings

logger = logging.getLogger("engraphis.llm")

_PROVIDER_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "google": "https://generativelanguage.googleapis.com/v1beta",
}

_THOUGHT_SYSTEM_PROMPT = (
    "You are a memory consolidation engine. You receive recalled memory context "
    "and must produce a concise latent-state update as JSON only (no markdown, no "
    "prose outside JSON). Extract the most salient inferences, contradictions, "
    "follow-ups, and predicted next actions.\n\n"
    "Output schema:\n"
    '{"inference": "<one-sentence synthesis>", '
    '"contradiction": "<detected conflict or null>", '
    '"follow_up": "<suggested follow-up or null>", '
    '"next_action": "<candidate action or null>"}'
)

_CHAT_SYSTEM_PROMPT = (
    "You are a helpful assistant with access to the user's long-term memory. "
    "Use the provided context to ground your answers. If the context does not "
    "contain relevant information, say so and answer from your general knowledge."
)


class LLMClient:
    """Thin REST client for multiple LLM providers."""

    def __init__(
        self,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        extra_headers: Optional[dict] = None,
    ) -> None:
        self.provider = (provider or settings.llm_provider).lower()
        self.model = model or settings.llm_model
        self.api_key = api_key or settings.llm_api_key
        self.base_url = base_url or settings.llm_base_url or _PROVIDER_BASE_URLS.get(
            self.provider, ""
        )
        self.extra_headers = extra_headers or settings.llm_extra_headers
        self._http = httpx.Client(timeout=120)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "LLMClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ── Public API ──────────────────────────────────────────────────────────

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        system: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """Send a chat request and return the assistant's text reply."""
        if not self.api_key:
            raise ValueError(
                "No LLM API key configured. Set ENGRAPHIS_LLM_API_KEY in .env "
                f"or pass api_key= when constructing LLMClient (provider={self.provider})."
            )
        if self.provider == "anthropic":
            return self._chat_anthropic(messages, system, temperature, max_tokens)
        if self.provider == "google":
            return self._chat_google(messages, system, temperature, max_tokens)
        return self._chat_openai_compat(messages, system, temperature, max_tokens)

    def synthesize_thought(self, context: str, *, temperature: float = 0.3,
                           max_tokens: int = 512,
                           thought_prompt: Optional[str] = None) -> dict[str, Any]:
        """Phase 2 thought synthesis — returns parsed JSON latent state."""
        system = thought_prompt or _THOUGHT_SYSTEM_PROMPT
        raw = self.chat(
            [{"role": "user", "content": f"Memory context:\n\n{context}"}],
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return _parse_json_response(raw)

    def chat_with_context(
        self,
        user_prompt: str,
        context: str,
        *,
        system: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """Convenience: answer a user prompt using memory context."""
        sys = system or _CHAT_SYSTEM_PROMPT
        full_user = f"Context from memory:\n{context}\n\nUser question: {user_prompt}" if context else user_prompt
        return self.chat(
            [{"role": "user", "content": full_user}],
            system=sys,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    # ── Provider implementations ────────────────────────────────────────────

    def _chat_openai_compat(self, messages, system, temperature, max_tokens) -> str:
        """OpenAI / OpenRouter / custom OpenAI-compatible endpoints."""
        full_messages = []
        if system:
            full_messages.append({"role": "system", "content": system})
        full_messages.extend(messages)
        body: dict[str, Any] = {"model": self.model, "messages": full_messages}
        if temperature is not None:
            body["temperature"] = temperature
        if max_tokens is not None:
            body["max_tokens"] = max_tokens

        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        headers.update(self.extra_headers)

        url = f"{self.base_url}/chat/completions"
        logger.debug("LLM POST %s model=%s", url, self.model)
        resp = self._http.post(url, json=body, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError(f"Unexpected LLM response format: {data}") from exc

    def _chat_anthropic(self, messages, system, temperature, max_tokens) -> str:
        """Anthropic Messages API."""
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [_anthropic_msg(m) for m in messages],
            "max_tokens": max_tokens or 1024,
        }
        if system:
            body["system"] = system
        if temperature is not None:
            body["temperature"] = temperature

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        headers.update(self.extra_headers)

        url = f"{self.base_url}/messages"
        logger.debug("LLM POST %s model=%s", url, self.model)
        resp = self._http.post(url, json=body, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        try:
            return data["content"][0]["text"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError(f"Unexpected Anthropic response format: {data}") from exc

    def _chat_google(self, messages, system, temperature, max_tokens) -> str:
        """Google Gemini generateContent API."""
        contents = []
        for m in messages:
            role = "user" if m["role"] == "user" else "model"
            contents.append({"role": role, "parts": [{"text": m["content"]}]})
        body: dict[str, Any] = {"contents": contents}
        gen_config: dict[str, Any] = {}
        if temperature is not None:
            gen_config["temperature"] = temperature
        if max_tokens is not None:
            gen_config["maxOutputTokens"] = max_tokens
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if gen_config:
            body["generationConfig"] = gen_config

        headers = {"Content-Type": "application/json"}
        headers.update(self.extra_headers)

        url = (
            f"{self.base_url}/models/{self.model}:generateContent"
            f"?key={self.api_key}"
        )
        logger.debug("LLM POST %s model=%s", url, self.model)
        resp = self._http.post(url, json=body, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        try:
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError(f"Unexpected Google response format: {data}") from exc


def _anthropic_msg(m: dict[str, str]) -> dict[str, str]:
    """Anthropic only accepts user/assistant roles, not system."""
    role = m["role"]
    if role == "system":
        role = "user"
    return {"role": role, "content": m["content"]}


def _parse_json_response(raw: str) -> dict[str, Any]:
    """Best-effort parse of a JSON thought response, tolerating markdown fences."""
    text = raw.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:])
        if text.rstrip().endswith("```"):
            text = text.rsplit("```", 1)[0]
    try:
        return json.loads(text)
    except Exception:
        return {"raw": raw}
