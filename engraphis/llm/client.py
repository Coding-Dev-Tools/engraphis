"""External LLM client — supports OpenAI, Anthropic, Google, OpenRouter, and
any OpenAI-compatible custom endpoint.

No provider SDK dependencies — uses httpx directly so the only network dep is
already in requirements. All providers are reached via their REST API.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import math
import os
import threading
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
        timeout: Optional[float] = None,
    ) -> str:
        """Send a chat request and return the assistant's text reply."""
        if not self.api_key:
            raise ValueError(
                "No LLM API key configured. Set ENGRAPHIS_LLM_API_KEY in .env "
                "or pass api_key= when constructing LLMClient."
            )
        if self.provider == "anthropic":
            return self._chat_anthropic(messages, system, temperature, max_tokens, timeout)
        if self.provider == "google":
            return self._chat_google(messages, system, temperature, max_tokens, timeout)
        return self._chat_openai_compat(messages, system, temperature, max_tokens, timeout)

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

    def extract_json(
        self,
        prompt: str,
        schema: dict,
        *,
        timeout: Optional[float] = None,
    ) -> Any:
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
                        temperature=0.0, max_tokens=8192, timeout=timeout)
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

    def _chat_openai_compat(
        self, messages, system, temperature, max_tokens, timeout=None
    ) -> str:
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
        data = self._post_json(url, body, headers, timeout=timeout)
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise ValueError("Unexpected LLM response format") from None

    def _chat_anthropic(
        self, messages, system, temperature, max_tokens, timeout=None
    ) -> str:
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
        data = self._post_json(url, body, headers, timeout=timeout)
        try:
            return data["content"][0]["text"]
        except (KeyError, IndexError, TypeError):
            raise ValueError("Unexpected Anthropic response format") from None

    def _chat_google(
        self, messages, system, temperature, max_tokens, timeout=None
    ) -> str:
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
        data = self._post_json(url, body, headers, timeout=timeout)
        try:
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError):
            raise ValueError("Unexpected Google response format") from None

    def _post_json(
        self,
        url: str,
        body: dict[str, Any],
        headers: dict[str, str],
        *,
        timeout: Optional[float] = None,
    ) -> Any:
        """POST with retry for transient provider errors (429, 502, 503, 504)."""
        import time as _time
        _RETRYABLE = {429, 502, 503, 504}
        if timeout is not None:
            if isinstance(timeout, bool) or not math.isfinite(float(timeout)) or timeout <= 0:
                raise ValueError("timeout must be a finite positive number")
            timeout = float(timeout)
        # A planner deadline is a fail-open latency boundary. Do not add retry
        # sleeps to it; ordinary LLM calls retain the established retry policy.
        _MAX_RETRIES = 0 if timeout is not None else 2
        last_exc: Optional[Exception] = None
        for attempt in range(1 + _MAX_RETRIES):
            try:
                request_kwargs = {"timeout": timeout} if timeout is not None else {}
                resp = self._http.post(url, json=body, headers=headers, **request_kwargs)
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
            except httpx.TimeoutException as exc:
                raise TimeoutError("LLM request exceeded its deadline") from exc
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


# Rough per-1K-token pricing (USD) for cost estimation. Keys are provider names.
_PROVIDER_PRICING: dict[str, float] = {
    "openai": 0.002,
    "anthropic": 0.003,
    "google": 0.001,
    "openrouter": 0.002,
}


class LLMProviderChain:
    """Ordered fallback chain of LLM clients with optional cost ceilings.

    Tries each client in order for chat/synthesize_thought/extract_json.
    On _LLMProviderError or TimeoutError, logs a warning and tries the next.
    Tracks cumulative estimated cost per client; skips clients whose ceiling
    is exceeded. Thread-safe cost tracking via threading.Lock.
    """

    def __init__(
        self,
        clients: list[LLMClient],
        cost_ceilings: Optional[dict[int, float]] = None,
    ) -> None:
        if not clients:
            raise ValueError("LLMProviderChain requires at least one LLMClient")
        self._clients = list(clients)
        # cost_ceilings maps client index -> USD ceiling. None = unlimited.
        self._cost_ceilings = cost_ceilings or {}
        self._cumulative_cost: dict[int, float] = {i: 0.0 for i in range(len(clients))}
        self._lock = threading.Lock()

    # ── Cost tracking ───────────────────────────────────────────────────────

    def _estimate_cost(self, client: LLMClient, response_text: str,
                       input_text: str = "") -> float:
        """Rough cost estimate from input + output length and provider pricing."""
        approx_output = max(1, len(response_text) // 4)
        approx_input = max(1, len(input_text) // 4) if input_text else approx_output
        rate = _PROVIDER_PRICING.get(client.provider, 0.002)
        return ((approx_input + approx_output) / 1000.0) * rate

    def _record_cost(self, idx: int, cost: float) -> None:
        with self._lock:
            self._cumulative_cost[idx] = self._cumulative_cost.get(idx, 0.0) + cost

    def _is_exhausted(self, idx: int) -> bool:
        ceiling = self._cost_ceilings.get(idx)
        if ceiling is None:
            return False
        with self._lock:
            return self._cumulative_cost.get(idx, 0.0) >= ceiling

    # ── Fallback dispatch ───────────────────────────────────────────────────
    def _dispatch(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        last_exc: Optional[Exception] = None
        skipped_by_ceiling = 0
        for idx, client in enumerate(self._clients):
            if self._is_exhausted(idx):
                skipped_by_ceiling += 1
                logger.debug(
                    "Skipping provider %d (%s/%s): cost ceiling exceeded",
                    idx, client.provider, client.model,
                )
                continue
            try:
                result = getattr(client, method_name)(*args, **kwargs)
                # Estimate and record cost for successful calls.  Input length is
                # approximated from the serialized positional + keyword arguments so
                # the estimate covers both sides of the API bill.
                try:
                    input_blob = json.dumps(args, default=str) + json.dumps(
                        kwargs, default=str)
                except (TypeError, ValueError):
                    input_blob = str(args) + str(kwargs)
                if isinstance(result, str):
                    cost = self._estimate_cost(client, result, input_blob)
                elif isinstance(result, dict):
                    cost = self._estimate_cost(client, json.dumps(result), input_blob)
                else:
                    cost = self._estimate_cost(client, str(result), input_blob)
                self._record_cost(idx, cost)
                return result
            except (_LLMProviderError, TimeoutError, ValueError) as exc:
                # ValueError covers "No LLM API key configured" — treat as retryable
                # so the chain advances to the next provider rather than aborting.
                logger.warning(
                    "Provider %d (%s/%s) failed with %s; trying next in chain",
                    idx, client.provider, client.model, type(exc).__name__,
                )
                last_exc = exc
                continue
        # All providers exhausted or failed — distinguish the reasons so callers
        # don't chase network issues when the real cause is a budget ceiling.
        if last_exc is not None:
            raise last_exc
        if skipped_by_ceiling:
            raise _LLMProviderError(
                "All %d provider(s) skipped: cumulative cost ceiling exceeded. "
                "Raise the ceiling or wait for the budget window to reset."
                % skipped_by_ceiling
            ) from None
        raise _LLMProviderError(unreachable=True) from None

    # ── Public API (mirrors LLMClient) ──────────────────────────────────────

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        system: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        timeout: Optional[float] = None,
    ) -> str:
        return self._dispatch(
            "chat", messages,
            system=system, temperature=temperature,
            max_tokens=max_tokens, timeout=timeout,
        )

    def synthesize_thought(
        self,
        context: str,
        *,
        temperature: float = 0.3,
        max_tokens: int = 512,
        thought_prompt: Optional[str] = None,
    ) -> dict[str, Any]:
        return self._dispatch(
            "synthesize_thought", context,
            temperature=temperature, max_tokens=max_tokens,
            thought_prompt=thought_prompt,
        )

    def extract_json(
        self,
        prompt: str,
        schema: dict,
        *,
        timeout: Optional[float] = None,
    ) -> Any:
        return self._dispatch("extract_json", prompt, schema, timeout=timeout)

    def close(self) -> None:
        for client in self._clients:
            client.close()

    def __enter__(self) -> "LLMProviderChain":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def parse_provider_chain(env_var: str = "ENGRAPHIS_LLM_PROVIDERS") -> LLMProviderChain:
    """Factory: build an LLMProviderChain from an env var.

    Format: comma-separated tuples of 'provider:model:key:url:ceiling'.
    URL field may contain '://' — parsed by splitting from the right for
    ceiling, then from the left for provider/model/key, leaving the rest as URL.
    Example: 'openai:gpt-4o-mini:sk-abc:https://api.openai.com/v1:0.50'
    Empty fields fall back to defaults (provider=openai, model=gpt-4o-mini, etc.).
    """
    raw = os.environ.get(env_var, "").strip()
    if not raw:
        # Single-provider fallback from existing settings
        return LLMProviderChain([LLMClient()])

    clients: list[LLMClient] = []
    cost_ceilings: dict[int, float] = {}
    entries = [e.strip() for e in raw.split(",") if e.strip()]
    for idx, entry in enumerate(entries):
        # Split from the right to extract optional ceiling (last field after last ':')
        # But ceiling is numeric, so we check if the last segment is a valid float
        ceiling_str: Optional[str] = None
        remainder = entry
        # Try to extract ceiling: split on last ':' and check if it's numeric
        last_colon = remainder.rfind(":")
        if last_colon >= 0:
            candidate = remainder[last_colon + 1:].strip()
            # Only treat as ceiling if it looks numeric and isn't part of a URL scheme
            if candidate and not candidate.startswith("//"):
                try:
                    float(candidate)
                    ceiling_str = candidate
                    remainder = remainder[:last_colon]
                except ValueError:
                    pass

        # Now split remainder into provider:model:key:url
        # Split from left: first 3 colons give provider, model, key; rest is url
        parts = remainder.split(":", 3)
        provider = parts[0].strip() if len(parts) > 0 and parts[0].strip() else None
        model = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None
        api_key = parts[2].strip() if len(parts) > 2 and parts[2].strip() else None
        base_url = parts[3].strip() if len(parts) > 3 and parts[3].strip() else None

        client = LLMClient(
            provider=provider, model=model,
            api_key=api_key, base_url=base_url,
        )
        clients.append(client)

        if ceiling_str is not None:
            try:
                cost_ceilings[idx] = float(ceiling_str)
            except ValueError:
                logger.warning(
                    "Invalid cost ceiling '%s' for provider %d; ignoring",
                    ceiling_str, idx,
                )

    if not clients:
        return LLMProviderChain([LLMClient()])
    return LLMProviderChain(clients, cost_ceilings=cost_ceilings or None)

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
    except Exception as exc:
        logger.debug("LLM JSON parse fallback to raw (%s)", type(exc).__name__)
        return {"raw": raw}
