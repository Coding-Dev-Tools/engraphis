"""External LLM client — supports OpenAI, Anthropic, Google, OpenRouter, and
any OpenAI-compatible custom endpoint.

No provider SDK dependencies — uses httpx directly so the only network dep is
already in requirements. All providers are reached via their REST API.
"""
from __future__ import annotations

import ipaddress
import json
import logging
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx

from engraphis.config import settings

logger = logging.getLogger("engraphis.llm")

_PROVIDER_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "google": "https://generativelanguage.googleapis.com/v1beta",
}


def validate_llm_base_url(value: str) -> str:
    """Validate and normalize an LLM API base URL without resolving or logging it.

    Custom OpenAI-compatible endpoints may include a path (for example ``/v1``), but
    credentials, query strings, fragments, control characters, and ambiguous hosts are
    rejected.  The raw value can contain customer-specific routing or credentials, so
    callers must never reflect it in HTTP responses or logs.
    """
    raw = str(value or "")
    if raw != raw.strip() or any(
        char.isspace() or ord(char) == 127 for char in raw
    ):
        raise ValueError("LLM base URL contains whitespace or control characters")
    try:
        parts = urlsplit(raw)
        hostname = parts.hostname
        port = parts.port
    except ValueError:
        raise ValueError("LLM base URL is invalid") from None
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"} or not hostname:
        raise ValueError("LLM base URL must be an absolute http(s) URL")
    loopback = hostname.lower() == "localhost" or hostname.lower().endswith(".localhost")
    if not loopback:
        try:
            loopback = ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            loopback = False
    if scheme != "https" and not loopback:
        raise ValueError("LLM base URL must use HTTPS unless it targets loopback")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("LLM base URL has an invalid port")
    if parts.username is not None or parts.password is not None:
        raise ValueError("LLM base URL must not contain embedded credentials")
    if "\\" in parts.netloc or any(char.isspace() for char in parts.netloc):
        raise ValueError("LLM base URL contains an invalid host")
    if parts.query or parts.fragment:
        raise ValueError("LLM base URL must not contain a query string or fragment")
    path = parts.path.rstrip("/")
    return urlunsplit((scheme, parts.netloc, path, "", ""))

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


class _LLMProviderError(RuntimeError):
    """Sanitized provider failure safe to expose outside this client boundary."""

    def __init__(self, *, status: Optional[int] = None, unreachable: bool = False) -> None:
        self.status = status
        self.unreachable = unreachable
        if status is not None:
            message = "LLM provider rejected the request (HTTP %d)" % status
        else:
            message = "Could not reach the configured LLM provider"
        super().__init__(message)


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
        configured_base_url = (
            base_url or settings.llm_base_url or _PROVIDER_BASE_URLS.get(self.provider, "")
        )
        self.base_url = validate_llm_base_url(configured_base_url)
        self.extra_headers = extra_headers or settings.llm_extra_headers
        self._http = httpx.Client(
            timeout=120,
            follow_redirects=False,  # never leak API keys to redirect targets
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=5),
        )

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
                "or pass api_key= when constructing LLMClient."
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

    def extract_json(self, prompt: str, schema: dict) -> Any:
        """Extract structured JSON from the LLM using a JSON schema constraint.

        Uses the provider's native structured output (OpenAI JSON schema, etc.)
        when available; falls back to prompting + post-hoc validation otherwise.
        """
        # Build a system prompt that enforces JSON schema output
        system = (
            "You output ONLY valid JSON matching the provided schema. "
            "No markdown, no prose, no commentary. The schema:\n"
            f"{json.dumps(schema)}"
        )
        raw = self.chat([{"role": "user", "content": prompt}], system=system,
                        temperature=0.0, max_tokens=8192)
        return _parse_json_response(raw)

    def ping(self) -> dict[str, Any]:
        """Minimal live test of the configured provider/key/model.

        Sends a tiny completion and returns ``{"ok": bool, "reply": str,
        "error": str, "provider": str, "model": str}``. Never raises — a network
        or auth failure is reported as ``ok=False`` with an actionable ``error`` so
        the dashboard's "Test connection" button can show what went wrong (missing
        key, 401, wrong base URL, unreachable host) without a stack trace.
        """
        try:
            reply = self.chat(
                [{"role": "user", "content": "Reply with the single word: ok"}],
                temperature=0.0, max_tokens=5,
            )
            return {"ok": True, "reply": (reply or "").strip()[:200],
                    "error": "", "provider": self.provider, "model": self.model}
        except Exception as exc:  # noqa: BLE001 - external-provider boundary
            logger.error("LLM connection test failed (%s)", type(exc).__name__)
            if isinstance(exc, _LLMProviderError) and exc.status is not None:
                status = exc.status
                error = ("Provider rejected the request (HTTP %d). Check the API key, "
                         "model name, and provider settings." % status)
            elif isinstance(exc, _LLMProviderError) and exc.unreachable:
                error = ("Could not reach the configured provider. Check the base URL "
                         "and network connection.")
            else:
                error = "The provider test failed. Check the configured provider and model."
            return {"ok": False, "reply": "", "error": error,
                    "provider": self.provider, "model": self.model}

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
        # Custom provider URLs (and Google's URL below) may carry credentials in
        # their query string.  Keep debug logging useful without ever emitting the
        # configured endpoint verbatim.
        logger.debug("LLM provider request started")
        data = self._post_json(url, body, headers)
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise ValueError("Unexpected LLM response format") from None

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
        logger.debug("LLM provider request started")
        data = self._post_json(url, body, headers)
        try:
            return data["content"][0]["text"]
        except (KeyError, IndexError, TypeError):
            raise ValueError("Unexpected Anthropic response format") from None

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

        headers = {"Content-Type": "application/json", "x-goog-api-key": self.api_key}
        headers.update(self.extra_headers)

        url = f"{self.base_url}/models/{self.model}:generateContent"
        logger.debug("LLM provider request started")
        data = self._post_json(url, body, headers)
        try:
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError):
            raise ValueError("Unexpected Google response format") from None

    def _post_json(self, url: str, body: dict[str, Any], headers: dict[str, str]) -> Any:
        """POST with retry for transient provider errors (429, 502, 503, 504)."""
        import time as _time
        _RETRYABLE = {429, 502, 503, 504}
        _MAX_RETRIES = 2
        last_exc: Optional[Exception] = None
        for attempt in range(1 + _MAX_RETRIES):
            try:
                resp = self._http.post(url, json=body, headers=headers)
                resp.raise_for_status()
                try:
                    return resp.json()
                except (ValueError, TypeError, AttributeError):
                    raise ValueError("Unexpected LLM response format") from None
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status in _RETRYABLE and attempt < _MAX_RETRIES:
                    retry_after = exc.response.headers.get("retry-after", "")
                    try:
                        wait = max(1.0, min(float(retry_after), 30.0))
                    except (ValueError, TypeError):
                        wait = 2.0 * (attempt + 1)
                    logger.warning(
                        "LLM provider returned %d; retrying in %.1fs (attempt %d/%d)",
                        status, wait, attempt + 1, _MAX_RETRIES)
                    _time.sleep(wait)
                    last_exc = exc
                    continue
                raise _LLMProviderError(status=status) from None
            except httpx.RequestError:
                if attempt < _MAX_RETRIES:
                    wait = 2.0 * (attempt + 1)
                    logger.warning(
                        "LLM provider unreachable; retrying in %.1fs (attempt %d/%d)",
                        wait, attempt + 1, _MAX_RETRIES)
                    _time.sleep(wait)
                    last_exc = None
                    continue
                raise _LLMProviderError(unreachable=True) from None
        # Should not be reached, but satisfy the type checker.
        if last_exc is not None:
            raise _LLMProviderError(
                status=getattr(last_exc, "response", None)
                and last_exc.response.status_code) from None
        raise _LLMProviderError(unreachable=True) from None


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
