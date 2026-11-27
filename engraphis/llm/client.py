"""External LLM client — supports OpenAI, Anthropic, Google, OpenRouter, and
any OpenAI-compatible custom endpoint.

No provider SDK dependencies — uses httpx directly so the only network dep is
already in requirements. All providers are reached via their REST API.
"""
from __future__ import annotations

import atexit
from collections import OrderedDict
import hashlib
import ipaddress
import json
import logging
import math
import os
import re
import secrets
import threading
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit
import weakref

try:
    import httpx
except ImportError:  # pragma: no cover - core-floor (numpy-only) installs
    httpx = None  # type: ignore[assignment]

from engraphis.config import settings
from engraphis.llm.budget import (
    LLMResultCache,
    llm_result_cache,
    managed_allowance_tracker,
    managed_session_scope as _managed_session_scope,
)

logger = logging.getLogger("engraphis.llm")
_CACHE_SCOPE_SALT = secrets.token_bytes(16)

DEFAULT_MANAGED_MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"
MANAGED_MODEL_PROFILES = ("auto", "fast", "reasoning")
_MANAGED_PROFILE_SET = frozenset(MANAGED_MODEL_PROFILES)
MANAGED_MAX_MESSAGE_CHARS = 22_000
MANAGED_MAX_MESSAGES = 16
_QUOTA_EXHAUSTED_CODES = frozenset({
    "llm_rolling_allowance_exhausted",
    "rolling_allowance_exhausted",
    "insufficient_quota",
    "quota_exceeded",
})
_MANAGED_EXHAUSTED_MESSAGE = (
    "Daily managed AI LLM allowance reached; using deterministic local fallback"
)
_MANAGED_HEADROOM_MESSAGE = (
    "Managed AI LLM background headroom reserved for interactive calls; "
    "using deterministic local fallback"
)
_BYOK_QUOTA_EXHAUSTED_MESSAGE = (
    "Provider quota exceeded (HTTP 429). Check your provider API billing and quota limits."
)
_MANAGED_NON_RETRYABLE_4XX_CODES = frozenset({
    "secret_detected", "secrets_rejected", "content_policy_violation", "payload_too_large",
    "message_too_large", "prompt_too_large", "request_too_large", "oversize_payload",
    "oversized_request", "max_chars_exceeded", "max_messages_exceeded",
})
_JSON_MODE_LOCK = threading.RLock()
_JSON_MODE_UNSUPPORTED: OrderedDict[tuple[str, str, str], None] = OrderedDict()
_JSON_MODE_MAX_ENTRIES = 128
ALLOWED_MANAGED_FREE_MODELS = (
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "openrouter/free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "google/gemma-3-27b-it:free",
    "deepseek/deepseek-chat-v3-0324:free",
    "qwen/qwq-32b:free",
)

_PROVIDER_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "google": "https://generativelanguage.googleapis.com/v1beta",
    "managed": "https://api.engraphis.com/v1/llm",
}


_MANAGED_CLOUD_PLANS = frozenset({"pro", "team"})
_MANAGED_ACTIVE_STATUSES = frozenset({"active", "trialing"})


def managed_cloud_llm_available() -> bool:
    """Return True only when an active paid or trial Pro/Team Cloud session is verified."""
    try:
        from engraphis import cloud_session
        if not (
            cloud_session.configured(require_compute=False)
            and cloud_session.credential_bound_control_url()
        ):
            return False
        saved = cloud_session.saved_entitlement()
        if not isinstance(saved, dict) or not saved:
            return False
        if saved.get("cloud_access_active") is not True:
            return False
        plan = str(saved.get("plan") or "").strip().lower()
        if plan not in _MANAGED_CLOUD_PLANS:
            return False
        status = str(saved.get("status") or "").strip().lower()
        if status and status not in _MANAGED_ACTIVE_STATUSES:
            return False
        return True
    except Exception:  # noqa: BLE001
        return False


def auto_managed_llm_selected() -> bool:
    """Return True when included managed Cloud LLM is the effective provider without BYOK override."""
    raw_provider = (settings.llm_provider or "openai").strip().lower()
    if raw_provider == "managed":
        return managed_cloud_llm_available()
    return (
        not settings.llm_api_key
        and not settings.llm_base_url
        and raw_provider in {"", "openai"}
        and managed_cloud_llm_available()
    )


def auto_managed_extractor_allowed() -> bool:
    """Return True when automatic managed extraction on ingest is permitted and not opted out."""
    if "ENGRAPHIS_EXTRACTOR" in os.environ:
        return False
    raw_auto = os.environ.get("ENGRAPHIS_LLM_AUTO_EXTRACT")
    if raw_auto is not None and raw_auto.strip().lower() in {"0", "false", "no", "off"}:
        return False
    return auto_managed_llm_selected()


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

# Fable and Mythos ids may omit a numeric version (``claude-mythos-preview``); every
# member of those families rejects sampling and thinks by default.
_CLAUDE_MODEL_RE = re.compile(
    r"claude-(?:(opus|sonnet|haiku)-(\d+)(?:-(\d{1,2})(?!\d))?|(fable|mythos)(?![a-z0-9]))"
)

# Newer Claude models think by default, so hidden reasoning tokens share ``max_tokens`` with
# the visible reply. Keep enough headroom that a small caller cap cannot end the turn before
# any text is produced.
_ANTHROPIC_THINKING_MIN_TOKENS = 4096


def _anthropic_model_traits(model: Optional[str]) -> tuple[bool, bool]:
    """Return ``(accepts_sampling, thinks_by_default)`` for a Claude model id.

    Opus 4.7+, Sonnet 5+, Fable and Mythos reject ``temperature``/``top_p``/``top_k``
    with HTTP 400, and Opus 5+, Sonnet 5+, Fable and Mythos run adaptive thinking unless
    told otherwise. Older or unrecognized ids keep the classic contract (sampling accepted,
    no default thinking), so legacy models and proxies behave exactly as before.
    """
    match = _CLAUDE_MODEL_RE.search((model or "").lower())
    if match is None:
        return True, False
    if match.group(4):
        return False, True
    family, major, minor = match.group(1), int(match.group(2)), int(match.group(3) or 0)
    if family == "opus":
        return (major, minor) < (4, 7), major >= 5
    if family == "sonnet":
        return major < 5, major >= 5
    return major < 5, False


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


def _normalize_managed_profile(profile: Optional[str]) -> Optional[str]:
    norm = str(profile).strip().lower() if profile is not None else ""
    return norm if norm in _MANAGED_PROFILE_SET else None


def _truncate_text_to_budget(text: str, budget: int) -> str:
    if budget <= 0:
        return ""
    if len(text) <= budget:
        return text
    marker = "\n...[truncated]\n"
    if budget <= len(marker) + 16:
        return text[:budget].rstrip() or text[:budget]
    avail = budget - len(marker)
    head_len = max(1, int(avail * 0.8))
    tail_len = avail - head_len
    if tail_len <= 0:
        out = text[:avail].rstrip()
        return (out + marker[: budget - len(out)])[:budget]
    combined = f"{text[:head_len].rstrip()}{marker}{text[-tail_len:].lstrip()}"[:budget]
    return combined if combined.strip() else text[:budget]


def _trim_messages_for_managed_limit(
    messages: list[dict[str, str]], max_chars: int = MANAGED_MAX_MESSAGE_CHARS, max_messages: int = MANAGED_MAX_MESSAGES,
) -> list[dict[str, str]]:
    """Bound managed Cloud messages below the char/message cap without slicing system prompts."""
    cap, msg_cap = max(64, int(max_chars)), max(1, int(max_messages))
    normalized = [
        {"role": str(m.get("role") or "user"), "content": str(m.get("content") or "")}
        for m in messages if str(m.get("content") or "").strip()
    ]
    if not normalized:
        return []
    if len(normalized) > msg_cap:
        raw_sys = [m for m in normalized if m["role"] == "system"]
        sys_msgs = [{"role": "system", "content": "\n\n".join(m["content"] for m in raw_sys)}] if raw_sys else []
        non_sys = [m for m in normalized if m["role"] != "system"]
        keep_slots = max(0, msg_cap - len(sys_msgs))
        kept_non_sys: list[dict[str, str]] = [] if keep_slots == 0 else non_sys[-keep_slots:]
        if keep_slots > 0 and not any(m["role"] == "user" for m in kept_non_sys):
            last_user = next((m for m in reversed(non_sys) if m["role"] == "user"), None)
            if last_user is not None:
                kept_non_sys = [*kept_non_sys[-(keep_slots - 1):], last_user] if keep_slots > 1 else [last_user]
        normalized = [*sys_msgs, *kept_non_sys]

    sys_indices = [i for i, m in enumerate(normalized) if m["role"] == "system"]
    sys_total = sum(len(normalized[i]["content"]) for i in sys_indices)
    if sys_indices and sys_total > cap - 128:
        raise ValueError("system prompt exceeds managed LLM character limit")
    if sum(len(m["content"]) for m in normalized) <= cap:
        return normalized

    non_sys_indices = [i for i, m in enumerate(normalized) if m["role"] != "system"]
    non_sys_budget = cap - sys_total
    if non_sys_indices:
        last_user_idx = next((i for i in reversed(non_sys_indices) if normalized[i]["role"] == "user"), non_sys_indices[-1])
        kept_non_sys_indices = list(non_sys_indices)
        while len(kept_non_sys_indices) > 1 and sum(len(normalized[i]["content"]) for i in kept_non_sys_indices) > non_sys_budget:
            drop_candidate = next((i for i in kept_non_sys_indices if i != last_user_idx), None)
            if drop_candidate is None:
                break
            kept_non_sys_indices.remove(drop_candidate)
        if sum(len(normalized[i]["content"]) for i in kept_non_sys_indices) > non_sys_budget:
            remaining = non_sys_budget
            for pos, idx in enumerate(kept_non_sys_indices):
                alloc = max(1, remaining - (len(kept_non_sys_indices) - pos - 1) * 32)
                normalized[idx]["content"] = _truncate_text_to_budget(normalized[idx]["content"], alloc)
                remaining = max(0, remaining - len(normalized[idx]["content"]))
        kept_set = set(sys_indices) | set(kept_non_sys_indices)
        normalized = [m for i, m in enumerate(normalized) if i in kept_set and m["content"].strip()]

    while sum(len(m["content"]) for m in normalized) > cap:
        non_sys_pos = [i for i, m in enumerate(normalized) if m["role"] != "system"]
        if not non_sys_pos:
            break
        longest_idx = max(non_sys_pos, key=lambda i: len(normalized[i]["content"]))
        current = normalized[longest_idx]["content"]
        target = max(1, len(current) - (sum(len(m["content"]) for m in normalized) - cap))
        normalized[longest_idx]["content"] = _truncate_text_to_budget(current, target)
        if len(normalized[longest_idx]["content"]) >= len(current):
            normalized[longest_idx]["content"] = current[:target]
            break
    return normalized


def _make_managed_request_key(model: str, messages: list[dict[str, str]]) -> str:
    nonce = secrets.token_hex(16)
    digest_input = f"{nonce}:{model}:{len(messages)}:{sum(len(m.get('content', '')) for m in messages)}"
    return hashlib.sha256(digest_input.encode("utf-8")).hexdigest()[:32]


def _extract_error_code(response: Any) -> str:
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001
        return ""
    if not isinstance(payload, dict):
        return ""
    detail, error = payload.get("detail"), payload.get("error")
    code = ""
    if isinstance(detail, dict):
        code = str(detail.get("code") or "")
    elif isinstance(detail, str) and len(detail) <= 64 and " " not in detail.strip():
        code = detail
    if not code and isinstance(error, dict):
        code = str(error.get("code") or error.get("type") or "")
    return (code or str(payload.get("code") or "")).strip().lower()[:64]


def _extract_quota_exhaustion(response: Any) -> Optional[dict[str, Any]]:
    code = _extract_error_code(response)
    if code not in _QUOTA_EXHAUSTED_CODES:
        return None
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001
        payload = None
    next_release_at, retry_after_raw = None, None
    if isinstance(payload, dict):
        detail, error, allowance = payload.get("detail"), payload.get("error"), payload.get("allowance")
        if isinstance(detail, dict):
            next_release_at, retry_after_raw = detail.get("next_release_at"), detail.get("retry_after")
        if next_release_at is None and isinstance(error, dict):
            next_release_at = error.get("next_release_at")
        if next_release_at is None:
            next_release_at = payload.get("next_release_at")
        if next_release_at is None and isinstance(allowance, dict):
            next_release_at = allowance.get("next_release_at")
    headers = getattr(response, "headers", None) or {}
    if retry_after_raw is None:
        retry_after_raw = headers.get("retry-after") or headers.get("Retry-After")
    retry_after_s = 60.0
    if retry_after_raw is not None:
        try:
            parsed = float(retry_after_raw)
            if math.isfinite(parsed) and parsed > 0:
                retry_after_s = parsed
        except (TypeError, ValueError):
            pass
    return {"code": code, "next_release_at": next_release_at, "retry_after_s": retry_after_s}


def _is_managed_non_retryable_4xx(code: str) -> bool:
    return bool(code) and (code in _MANAGED_NON_RETRYABLE_4XX_CODES or any(tok in code for tok in ("secret", "oversiz", "too_large", "too_long")))


def _resolve_managed_model(model: Optional[str] = None) -> str:
    resolved = model or settings.llm_model
    return DEFAULT_MANAGED_MODEL if (not resolved or (model is None and resolved == "gpt-4o-mini")) else resolved


class _LLMProviderError(RuntimeError):
    """Sanitized provider failure safe to expose outside this client boundary."""

    def __init__(
        self,
        *args: Any,
        status: Optional[int] = None,
        unreachable: bool = False,
        message: Optional[str] = None,
        error_code: str = "",
    ) -> None:
        self.status = status
        self.unreachable = unreachable
        self.error_code = str(error_code or "").strip().lower()[:64]
        if message is not None:
            resolved = message
        elif args:
            resolved = str(args[0])
        elif status is not None:
            resolved = "LLM provider rejected the request (HTTP %d)" % status
        else:
            resolved = "Could not reach the configured LLM provider"
        super().__init__(resolved)


class LLMClient:
    """Thin REST client for multiple LLM providers."""

    def __init__(
        self,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        extra_headers: Optional[dict] = None,
        effort: Optional[str] = None,
        profile: Optional[str] = None,
        default_priority: str = "interactive",
    ) -> None:
        raw_provider = (provider or settings.llm_provider or "openai").lower()
        raw_key = api_key if api_key is not None else settings.llm_api_key
        raw_base_url = base_url if base_url is not None else settings.llm_base_url
        if (
            not raw_key
            and provider is None
            and raw_provider in {"", "openai"}
            and not raw_base_url
            and managed_cloud_llm_available()
        ):
            raw_provider = "managed"
        self.provider = raw_provider
        self.profile = _normalize_managed_profile(profile)
        self.default_priority = (default_priority or "interactive").strip().lower()
        self._shared_managed = False
        self.model = (
            _resolve_managed_model(model)
            if self.provider == "managed"
            else (model or settings.llm_model)
        )
        self.effort = effort or settings.llm_effort
        self.api_key = "" if self.provider == "managed" else raw_key
        configured_base_url = (
            _PROVIDER_BASE_URLS.get("managed", "")
            if self.provider == "managed"
            else (raw_base_url or _PROVIDER_BASE_URLS.get(self.provider, ""))
        )
        self.base_url = validate_llm_base_url(configured_base_url)
        self.extra_headers = {} if self.provider == "managed" else (extra_headers or settings.llm_extra_headers)
        self._http = httpx.Client(
            timeout=120,
            follow_redirects=False,  # never leak API keys to redirect targets
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=5),
        )

    @property
    def has_credentials(self) -> bool:
        """Return True when a static API key or active managed Cloud session is available."""
        return bool(self.api_key) or (self.provider == "managed" and managed_cloud_llm_available())

    def close(self) -> None:
        if self._shared_managed:
            return
        self._http.close()

    def _force_close(self) -> None:
        self._http.close()

    def _ensure_credentials(self) -> None:
        """Validate credentials/session before cache lookup or network dispatch."""
        if self.provider == "managed":
            if not managed_cloud_llm_available():
                raise ValueError(
                    "No LLM API key or connected Engraphis Cloud session configured. "
                    "Connect a Pro or Team account with engraphis-connect or set ENGRAPHIS_LLM_API_KEY."
                )
            return
        if not self.api_key:
            raise ValueError(
                "No LLM API key configured. Set ENGRAPHIS_LLM_API_KEY in .env "
                "or pass api_key= when constructing LLMClient."
            )

    def _cache_scope(self) -> tuple[str, ...]:
        if self.provider == "managed":
            return _managed_session_scope()
        tag = getattr(self, "_byok_scope_tag", None)
        if tag is None:
            tag = hashlib.pbkdf2_hmac(
                "sha256",
                self.api_key.encode("utf-8"),
                _CACHE_SCOPE_SALT,
                100_000,
                dklen=16,
            ).hex()
            self._byok_scope_tag = tag
        return (self.base_url, tag, str(self.effort or ""))

    def _cache_lookup(self, op_kind: str, model_profile: str, payload: Any) -> tuple[tuple[str, ...], tuple[str, str, str, str], bool, Any]:
        scope = self._cache_scope()
        if self.provider == "managed":
            llm_result_cache().sync_scope(scope)
        key = LLMResultCache.make_key(op_kind, self.provider, model_profile, payload, scope=scope)
        hit, cached = llm_result_cache().get(key)
        return scope, key, hit, cached

    def _cache_store(self, op_kind: str, model_profile: str, payload: Any, scope: tuple[str, ...], key: tuple[str, str, str, str], value: Any) -> None:
        if self.provider == "managed":
            post_scope = self._cache_scope()
            llm_result_cache().update_scope(post_scope)
            if post_scope != scope:
                key = LLMResultCache.make_key(op_kind, self.provider, model_profile, payload, scope=post_scope)
        llm_result_cache().put(key, value)

    def __enter__(self) -> "LLMClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ── Public API ──────────────────────────────────────────────────────────

    def _resolve_managed_auth(self, timeout: Optional[float] = None) -> tuple[str, str]:
        """Resolve a live rotating Cloud session token and credential-bound control URL."""
        import time as _time

        from engraphis import cloud_session
        from engraphis.hosted_client import validate_cloud_base_url

        if not managed_cloud_llm_available():
            raise ValueError(
                "No LLM API key or connected Engraphis Cloud session configured. "
                "Connect a Pro or Team account with engraphis-connect or set ENGRAPHIS_LLM_API_KEY."
            )
        budget = float(timeout) if timeout is not None and timeout > 0 else 15.0
        deadline = _time.monotonic() + min(max(budget, 1.0), 60.0)
        try:
            before = validate_cloud_base_url(cloud_session.credential_bound_control_url())
            token, _organization, _compute = cloud_session.access_for_workspace(
                None, require_compute=False, deadline=deadline
            )
            control = validate_cloud_base_url(cloud_session.credential_bound_control_url())
        except TimeoutError:
            raise
        except Exception as exc:  # noqa: BLE001
            status = getattr(exc, "status", None)
            raise _LLMProviderError(
                status=status if isinstance(status, int) and 400 <= status <= 599 else 401
            ) from None
        if not before or control != before:
            raise _LLMProviderError(status=401)
        return token, validate_llm_base_url(control.rstrip("/") + "/v1/llm")

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        system: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        timeout: Optional[float] = None,
        profile: Optional[str] = None,
        priority: Optional[str] = None,
        response_format: Optional[dict[str, Any]] = None,
        use_cache: bool = False,
    ) -> str:
        """Send a chat request and return the assistant's text reply."""
        self._ensure_credentials()
        effective_profile = _normalize_managed_profile(profile) or self.profile
        effective_priority = (priority or self.default_priority or "interactive").strip().lower()
        cache_ctx: Optional[tuple[Any, tuple[str, ...], tuple[str, str, str, str]]] = None
        if use_cache:
            payload = {"system": system, "messages": messages, "temperature": temperature, "max_tokens": max_tokens, "response_format": response_format}
            scope, key, hit, cached = self._cache_lookup("chat", effective_profile or self.model, payload)
            if hit and isinstance(cached, str):
                return cached
            cache_ctx = (payload, scope, key)

        if self.provider == "managed":
            allowed, reason = managed_allowance_tracker().can_call(priority=effective_priority)
            if not allowed:
                raise _LLMProviderError(status=429, message=_MANAGED_HEADROOM_MESSAGE if reason == "headroom_reserved_for_interactive" else _MANAGED_EXHAUSTED_MESSAGE)
            managed_key, managed_url = self._resolve_managed_auth(timeout=timeout)
            compat_kwargs: dict[str, Any] = {"api_key_override": managed_key, "base_url_override": managed_url}
            if response_format is not None:
                compat_kwargs["response_format"] = response_format
            if effective_profile is not None:
                compat_kwargs["profile"] = effective_profile
            result = self._chat_openai_compat(messages, system, temperature, max_tokens, timeout, **compat_kwargs)
        elif self.provider == "anthropic":
            result = self._chat_anthropic(messages, system, temperature, max_tokens, timeout)
        elif self.provider == "google":
            result = self._chat_google(messages, system, temperature, max_tokens, timeout)
        else:
            compat_kwargs = {"response_format": response_format} if response_format is not None else {}
            result = self._chat_openai_compat(messages, system, temperature, max_tokens, timeout, **compat_kwargs)

        if cache_ctx is not None and isinstance(result, str) and result.strip():
            self._cache_store("chat", effective_profile or self.model, cache_ctx[0], cache_ctx[1], cache_ctx[2], result)
        return result

    def synthesize_thought(self, context: str, *, temperature: float = 0.3,
                           max_tokens: int = 4096,
                           thought_prompt: Optional[str] = None) -> dict[str, Any]:
        """Phase 2 thought synthesis — returns parsed JSON latent state.

        The default completion budget leaves headroom for reasoning models that
        spend hidden reasoning tokens before emitting the JSON payload.
        """
        # Security: user-supplied thought_prompt is appended as guidance, never
        # allowed to replace the system prompt entirely. This prevents prompt
        # injection via the /memories/thoughts route.
        if thought_prompt:
            safe_suffix = thought_prompt.strip()[:500]
            system = f"{_THOUGHT_SYSTEM_PROMPT}\n\nAdditional guidance: {safe_suffix}"
        else:
            system = _THOUGHT_SYSTEM_PROMPT
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
        profile: Optional[str] = None,
        priority: Optional[str] = None,
        use_cache: bool = True,
    ) -> Any:
        """Extract structured JSON from the LLM using a JSON schema constraint."""
        self._ensure_credentials()
        effective_profile = _normalize_managed_profile(profile) or self.profile
        cache_ctx: Optional[tuple[Any, tuple[str, ...], tuple[str, str, str, str]]] = None
        if use_cache:
            payload = {"prompt": prompt, "schema": schema}
            scope, key, hit, cached = self._cache_lookup("extract_json", effective_profile or self.model, payload)
            if hit:
                return cached
            cache_ctx = (payload, scope, key)

        system = (
            "You output ONLY valid JSON matching the provided schema. "
            f"No markdown, no prose, no commentary. The schema:\n{json.dumps(schema)}"
        )
        request_model = effective_profile if (self.provider == "managed" and effective_profile is not None) else self.model
        json_mode_key = (self.provider, self.base_url, str(request_model or ""))
        with _JSON_MODE_LOCK:
            known_unsupported = json_mode_key in _JSON_MODE_UNSUPPORTED
            if known_unsupported:
                _JSON_MODE_UNSUPPORTED.move_to_end(json_mode_key)
        use_json_mode = self.provider not in {"anthropic", "google"} and not known_unsupported
        chat_kwargs: dict[str, Any] = {"system": system, "temperature": 0.0, "max_tokens": 8192, "timeout": timeout}
        if profile is not None:
            chat_kwargs["profile"] = profile
        if priority is not None:
            chat_kwargs["priority"] = priority
        if use_json_mode:
            chat_kwargs["response_format"] = {"type": "json_object"}
        try:
            raw = self.chat([{"role": "user", "content": prompt}], **chat_kwargs)
        except _LLMProviderError as exc:
            if use_json_mode and exc.status in (400, 422) and not (self.provider == "managed" and _is_managed_non_retryable_4xx(exc.error_code)):
                logger.debug("LLM provider rejected response_format with HTTP %d; retrying without it", exc.status)
                chat_kwargs.pop("response_format", None)
                raw = self.chat([{"role": "user", "content": prompt}], **chat_kwargs)
                with _JSON_MODE_LOCK:
                    _JSON_MODE_UNSUPPORTED[json_mode_key] = None
                    _JSON_MODE_UNSUPPORTED.move_to_end(json_mode_key)
                    while len(_JSON_MODE_UNSUPPORTED) > _JSON_MODE_MAX_ENTRIES:
                        _JSON_MODE_UNSUPPORTED.popitem(last=False)
            else:
                raise
        parsed, valid_json = _parse_json_response_with_status(raw)
        if cache_ctx is not None and valid_json:
            self._cache_store("extract_json", effective_profile or self.model, cache_ctx[0], cache_ctx[1], cache_ctx[2], parsed)
        return parsed

    def ping(self) -> dict[str, Any]:
        """Minimal live test of the configured provider/key/model.

        Sends a tiny completion and returns ``{"ok": bool, "reply": str,
        "error": str, "provider": str, "model": str}``. Never raises — a network
        or auth failure is reported as ``ok=False`` with an actionable ``error`` so
        the dashboard's "Test connection" button can show what went wrong (missing
        key, 401, wrong base URL, unreachable host) without a stack trace.
        """
        try:
            # Reasoning models spend the completion budget on hidden reasoning
            # tokens before any visible content; a tiny cap can return an empty
            # reply and read as a false negative.
            reply = self.chat(
                [{"role": "user", "content": "Reply with the single word: ok"}],
                temperature=0.0, max_tokens=1024,
            )
            return {"ok": True, "reply": (reply or "").strip()[:200],
                    "error": "", "provider": self.provider, "model": self.model}
        except Exception as exc:  # noqa: BLE001 - external-provider boundary
            logger.error("LLM connection test failed (%s)", type(exc).__name__)
            if isinstance(exc, _LLMProviderError) and exc.status == 429 and (
                "allowance" in str(exc).lower() or "quota" in str(exc).lower()
            ):
                error = str(exc)
            elif isinstance(exc, _LLMProviderError) and exc.status is not None:
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
        self,
        messages,
        system,
        temperature,
        max_tokens,
        timeout=None,
        *,
        api_key_override: Optional[str] = None,
        base_url_override: Optional[str] = None,
        response_format: Optional[dict[str, Any]] = None,
        profile: Optional[str] = None,
    ) -> str:
        """OpenAI / OpenRouter / Managed Cloud / custom OpenAI-compatible endpoints."""
        full_messages = []
        if system:
            full_messages.append({"role": "system", "content": system})
        full_messages.extend(messages)
        if self.provider == "managed":
            full_messages = _trim_messages_for_managed_limit(
                full_messages, max_chars=MANAGED_MAX_MESSAGE_CHARS
            )

        effective_profile = _normalize_managed_profile(profile) or self.profile
        request_model = (
            effective_profile
            if (self.provider == "managed" and effective_profile is not None)
            else self.model
        )
        body: dict[str, Any] = {"model": request_model, "messages": full_messages}
        if temperature is not None:
            body["temperature"] = temperature
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if response_format is not None:
            body["response_format"] = response_format
        if self.provider == "managed":
            body["request_key"] = _make_managed_request_key(request_model, full_messages)

        active_key = api_key_override if api_key_override is not None else self.api_key
        active_url = base_url_override if base_url_override is not None else self.base_url
        headers = {"Authorization": f"Bearer {active_key}", "Content-Type": "application/json"}
        headers.update(self.extra_headers)

        url = f"{active_url}/chat/completions"
        # Custom provider URLs (and Google's URL below) may carry credentials in
        # their query string.  Keep debug logging useful without ever emitting the
        # configured endpoint verbatim.
        logger.debug("LLM provider request started")
        data = self._post_json(url, body, headers, timeout=timeout)
        if (
            self.provider == "managed"
            and isinstance(data, dict)
            and isinstance(data.get("allowance"), dict)
        ):
            managed_allowance_tracker().record_allowance(data["allowance"])
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise ValueError("Unexpected LLM response format") from None

    def _chat_anthropic(
        self, messages, system, temperature, max_tokens, timeout=None
    ) -> str:
        """Anthropic Messages API."""
        accepts_sampling, thinks_by_default = _anthropic_model_traits(self.model)
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [_anthropic_msg(m) for m in messages],
            "max_tokens": max_tokens or 1024,
        }
        if system:
            body["system"] = system
        if temperature is not None and accepts_sampling:
            body["temperature"] = temperature
        if thinks_by_default:
            body["max_tokens"] = max(body["max_tokens"], _ANTHROPIC_THINKING_MIN_TOKENS)
            body["output_config"] = {"effort": self.effort}

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        headers.update(self.extra_headers)

        url = f"{self.base_url}/messages"
        logger.debug("LLM provider request started")
        data = self._post_json(url, body, headers, timeout=timeout)
        # Thinking models lead with a ``thinking`` block that has no ``text`` key, so read
        # the text blocks by type instead of assuming ``content[0]`` is the reply.
        try:
            text = "".join(
                block["text"] for block in data["content"]
                if isinstance(block, dict) and block.get("type") == "text"
            )
        except (KeyError, TypeError):
            raise ValueError("Unexpected Anthropic response format") from None
        if not text:
            raise ValueError("Unexpected Anthropic response format")
        return text

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
                if status == 429:
                    quota_info = _extract_quota_exhaustion(exc.response)
                    if quota_info is not None:
                        if self.provider == "managed":
                            managed_allowance_tracker().record_exhausted(
                                next_release_at=quota_info["next_release_at"],
                                retry_after_s=quota_info["retry_after_s"],
                            )
                            raise _LLMProviderError(
                                status=429,
                                message=_MANAGED_EXHAUSTED_MESSAGE,
                            ) from None
                        raise _LLMProviderError(
                            status=429,
                            message=_BYOK_QUOTA_EXHAUSTED_MESSAGE,
                        ) from None
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
                err_code = _extract_error_code(exc.response) if status in (400, 422) else ""
                raise _LLMProviderError(status=status, error_code=err_code) from None
            except httpx.TimeoutException:
                raise TimeoutError("LLM request exceeded its deadline") from None
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


_REAL_LLM_CLIENT_CLS = LLMClient
_SHARED_MANAGED_LOCK = threading.RLock()
_SHARED_MANAGED_CLIENTS: OrderedDict[tuple[Optional[str], str, str], LLMClient] = OrderedDict()
_SHARED_MANAGED_MAX_CLIENTS = 16


def _close_http_quietly(http_client: Any) -> None:
    close = getattr(http_client, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001
            pass


def get_shared_managed_llm_client(
    *, profile: Optional[str] = None, default_priority: str = "interactive", model: Optional[str] = None,
) -> LLMClient:
    """Return a thread-safe pooled managed ``LLMClient`` reused across calls."""
    norm_profile = _normalize_managed_profile(profile)
    norm_priority = (default_priority or "interactive").strip().lower()
    if LLMClient is not _REAL_LLM_CLIENT_CLS:
        kwargs: dict[str, Any] = {"provider": "managed", "profile": norm_profile, "default_priority": norm_priority}
        if model is not None:
            kwargs["model"] = model
        return LLMClient(**kwargs)
    key = (norm_profile, norm_priority, "" if norm_profile is not None else _resolve_managed_model(model))
    with _SHARED_MANAGED_LOCK:
        client = _SHARED_MANAGED_CLIENTS.get(key)
        if client is None or getattr(getattr(client, "_http", None), "is_closed", False):
            client = LLMClient(
                provider="managed", model=model if norm_profile is None else None,
                profile=norm_profile, default_priority=norm_priority,
            )
            client._shared_managed = True
            _SHARED_MANAGED_CLIENTS[key] = client
            while len(_SHARED_MANAGED_CLIENTS) > _SHARED_MANAGED_MAX_CLIENTS:
                _, evicted = _SHARED_MANAGED_CLIENTS.popitem(last=False)
                if getattr(evicted, "_http", None) is not None:
                    weakref.finalize(evicted, _close_http_quietly, evicted._http)
        else:
            _SHARED_MANAGED_CLIENTS.move_to_end(key)
        return client


def close_shared_managed_llm_client() -> None:
    """Close and discard all pooled shared managed ``LLMClient`` instances."""
    with _SHARED_MANAGED_LOCK:
        clients = list(_SHARED_MANAGED_CLIENTS.values())
        _SHARED_MANAGED_CLIENTS.clear()
    for client in clients:
        try:
            client._force_close()
        except Exception:  # noqa: BLE001
            pass


atexit.register(close_shared_managed_llm_client)


# Rough per-1K-token pricing (USD) for cost estimation. Keys are provider names.
_PROVIDER_PRICING: dict[str, float] = {
    "openai": 0.002,
    "anthropic": 0.003,
    "google": 0.001,
    "openrouter": 0.002,
    "managed": 0.0,
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
        profile: Optional[str] = None,
        priority: Optional[str] = None,
        response_format: Optional[dict[str, Any]] = None,
        use_cache: bool = False,
    ) -> str:
        kwargs: dict[str, Any] = {
            "system": system,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "timeout": timeout,
        }
        if profile is not None:
            kwargs["profile"] = profile
        if priority is not None:
            kwargs["priority"] = priority
        if response_format is not None:
            kwargs["response_format"] = response_format
        if use_cache:
            kwargs["use_cache"] = True
        return self._dispatch("chat", messages, **kwargs)

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
        profile: Optional[str] = None,
        priority: Optional[str] = None,
        use_cache: bool = True,
    ) -> Any:
        kwargs: dict[str, Any] = {"timeout": timeout}
        if profile is not None:
            kwargs["profile"] = profile
        if priority is not None:
            kwargs["priority"] = priority
        if not use_cache:
            kwargs["use_cache"] = False
        return self._dispatch("extract_json", prompt, schema, **kwargs)

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
        last_colon = remainder.rfind(":")
        if last_colon >= 0:
            candidate = remainder[last_colon + 1:].strip()
            # Only treat an integer as a URL port when it follows the authority
            # directly. Once the URL has a path, a numeric suffix is the documented
            # cost ceiling (for example ``https://api.example/v1:1``). Decimal
            # suffixes cannot be ports and are always parsed as ceilings.
            prefix = remainder[:last_colon]
            is_port = False
            if candidate.isdigit() and "://" in prefix:
                try:
                    prefix_parts = prefix.split(":", 3)
                    url_before_suffix = urlsplit(
                        prefix_parts[3] if len(prefix_parts) == 4 else ""
                    )
                    is_port = bool(
                        url_before_suffix.scheme
                        and url_before_suffix.hostname
                        and url_before_suffix.path in {"", "/"}
                    )
                except ValueError:
                    # Leave malformed URL handling to LLMClient's normal validator.
                    is_port = False
            if (
                candidate
                and not candidate.startswith("//")
                and "/" not in candidate
                and not is_port
            ):
                try:
                    float(candidate)
                    ceiling_str = candidate
                    remainder = prefix
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


def _parse_json_response_with_status(raw: str) -> tuple[dict[str, Any], bool]:
    """Parse JSON response (tolerating markdown fences) and report whether parsing succeeded."""
    text = raw.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:])
        if text.rstrip().endswith("```"):
            text = text.rsplit("```", 1)[0]
    try:
        parsed = json.loads(text)
        return parsed, True
    except Exception as exc:
        logger.debug("LLM JSON parse fallback to raw (%s)", type(exc).__name__)
        return {"raw": raw}, False


def _parse_json_response(raw: str) -> dict[str, Any]:
    """Best-effort parse of a JSON thought response, tolerating markdown fences."""
    parsed, _ok = _parse_json_response_with_status(raw)
    return parsed
