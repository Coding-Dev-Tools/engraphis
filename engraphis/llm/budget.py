"""Thread-safe managed LLM budget orchestrator, complexity gate, and result cache."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Callable, Optional

DEFAULT_INTERACTIVE_RESERVE, MIN_INTERACTIVE_RESERVE, MAX_INTERACTIVE_RESERVE = 15, 0, 50
DEFAULT_LLM_EXTRACT_MIN_CHARS = 180
DEFAULT_CACHE_MAX_ENTRIES, DEFAULT_CACHE_TTL_SECONDS = 256, 300.0
DEFAULT_MAX_TRACKER_SCOPES, _MAX_CACHED_VALUE_BYTES = 16, 65_536

_STRUCTURED_MARKER_RE = re.compile(
    r"(?:^|\n|\s)(?:user|assistant|system|human|ai|decision|todo|action(?:\s+item)?|handoff|blocker|follow[- ]?up|meeting|summary)\s*:",
    re.IGNORECASE,
)
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*•+]|\d+[.)])\s+\S", re.MULTILINE)
_HEADING_LINE_RE = re.compile(r"^\s*#{1,6}\s+\S", re.MULTILINE)
_ABBREV_RE = re.compile(r"\b(?:e\.g|i\.e|etc|vs|dr|mr|ms|mrs|prof|inc|ltd|corp|st|jr|sr)\.", re.IGNORECASE)
_SENTENCE_BOUNDARY_RE = re.compile(r"[.!?]+(?:\s+|$)")
_ISO_FRAC_RE = re.compile(r"(\.\d+)(?=(?:[+-]\d{2}:?\d{2})?$)")
_ISO_OFFSET_RE = re.compile(r"([+-]\d{2})(\d{2})$")


def _parse_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except (TypeError, ValueError):
        return default


def interactive_reserve_floor(default: int = DEFAULT_INTERACTIVE_RESERVE) -> int:
    val = _parse_int_env("ENGRAPHIS_LLM_INTERACTIVE_RESERVE", default)
    return max(MIN_INTERACTIVE_RESERVE, min(MAX_INTERACTIVE_RESERVE, val))


def extract_min_chars(default: int = DEFAULT_LLM_EXTRACT_MIN_CHARS) -> int:
    return max(0, _parse_int_env("ENGRAPHIS_LLM_EXTRACT_MIN_CHARS", default))


def _parse_epoch_timestamp(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        fval = float(value)
        return fval if math.isfinite(fval) and fval > 0.0 else None
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        fval = float(raw)
        return fval if math.isfinite(fval) and fval > 0.0 else None
    except ValueError:
        pass
    try:
        iso = raw[:-1] + "+00:00" if raw[-1] in ("Z", "z") else raw
        iso = _ISO_FRAC_RE.sub(lambda m: "." + m.group(1)[1:7].ljust(6, "0"), iso, count=1)
        iso = _ISO_OFFSET_RE.sub(r"\1:\2", iso)
        dt = datetime.fromisoformat(iso)
        return (dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt).timestamp()
    except ValueError:
        return None


def _parse_non_negative_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float) and value.is_integer():
        return max(0, int(value))
    return None


def _extract_remaining(mapping: dict[str, Any]) -> Optional[int]:
    rem = _parse_non_negative_int(mapping.get("remaining_requests"))
    return rem if rem is not None else _parse_non_negative_int(mapping.get("remaining"))


def _extract_next_release(mapping: dict[str, Any]) -> Optional[float]:
    for key in ("next_release_at", "resets_at", "reset_at"):
        ts = _parse_epoch_timestamp(mapping.get(key))
        if ts is not None:
            return ts
    return None


def managed_session_scope() -> tuple[str, str, str]:
    """Return a non-secret scope tuple ``(control_url, org_id, session_digest)``."""
    try:
        from engraphis import cloud_session

        control_url = str(cloud_session.credential_bound_control_url() or "").strip()
        saved = cloud_session.saved_entitlement()
        org_id = str(saved.get("organization_id") or "").strip() if isinstance(saved, dict) else ""
        org_id = org_id or os.environ.get("ENGRAPHIS_CLOUD_ORGANIZATION_ID", "").strip()
        digest = cloud_session.saved_session_digest()
        return (control_url, org_id, "" if digest is None else str(digest))
    except Exception:  # noqa: BLE001
        return ("", "", "")


class _AllowanceState:
    __slots__ = ("remaining_requests", "limit", "next_release_at", "exhausted_until", "updated_at")

    def __init__(self) -> None:
        self.remaining_requests: Optional[int] = None
        self.limit: Optional[int] = None
        self.next_release_at: Optional[float] = None
        self.exhausted_until: float = 0.0
        self.updated_at: float = 0.0


class ManagedAllowanceTracker:
    """Process-wide, thread-safe tracker scoped by active managed Cloud session."""

    def __init__(
        self, *, clock: Callable[[], float] = time.time,
        scope_fn: Callable[[], tuple[str, str, str]] = managed_session_scope,
        max_entries: int = DEFAULT_MAX_TRACKER_SCOPES,
    ) -> None:
        self._clock, self._scope_fn, self._max_entries = clock, scope_fn, max(1, int(max_entries))
        self._lock = threading.RLock()
        self._states: OrderedDict[tuple[str, str, str], _AllowanceState] = OrderedDict()

    def _state(self, *, create: bool) -> _AllowanceState:
        scope = self._scope_fn()
        state = self._states.get(scope)
        if state is not None:
            self._states.move_to_end(scope)
            return state
        if not create:
            return _AllowanceState()
        state = _AllowanceState()
        self._states[scope] = state
        while len(self._states) > self._max_entries:
            self._states.popitem(last=False)
        return state

    def record_allowance(self, allowance_dict: Any) -> None:
        if not isinstance(allowance_dict, dict):
            return
        remaining = _extract_remaining(allowance_dict)
        limit = _parse_non_negative_int(allowance_dict.get("limit"))
        next_release = _extract_next_release(allowance_dict)
        windows = allowance_dict.get("windows")
        if isinstance(windows, list):
            chosen_window: Optional[dict[str, Any]] = None
            min_rem: Optional[int] = None
            for win in windows:
                if not isinstance(win, dict):
                    continue
                win_rem = _extract_remaining(win)
                if chosen_window is None or (win_rem is not None and (min_rem is None or win_rem < min_rem)):
                    chosen_window, min_rem = win, win_rem
            if chosen_window is not None:
                if min_rem is not None:
                    remaining = min_rem if remaining is None else min(remaining, min_rem)
                if limit is None:
                    limit = _parse_non_negative_int(chosen_window.get("limit"))
                if next_release is None:
                    next_release = _extract_next_release(chosen_window)
        if remaining is None and limit is None and next_release is None:
            return
        now = self._clock()
        with self._lock:
            state = self._state(create=True)
            if remaining is not None:
                state.remaining_requests = remaining
            if limit is not None:
                state.limit = limit
            if next_release is not None:
                state.next_release_at = next_release
            state.updated_at = now
            if state.remaining_requests is not None:
                if state.remaining_requests <= 0:
                    state.remaining_requests = 0
                    target = next_release if (next_release is not None and next_release > now) else now + 60.0
                    state.exhausted_until = target
                    if next_release is None:
                        state.next_release_at = target
                else:
                    state.exhausted_until = 0.0

    def record_exhausted(self, next_release_at: Any = None, retry_after_s: float = 60.0) -> None:
        now = self._clock()
        parsed_release = _parse_epoch_timestamp(next_release_at)
        try:
            safe_retry = float(retry_after_s)
            safe_retry = safe_retry if math.isfinite(safe_retry) and safe_retry > 0.0 else 60.0
        except (TypeError, ValueError):
            safe_retry = 60.0
        safe_retry = max(1.0, min(safe_retry, 86_400.0))
        target_until = parsed_release if (parsed_release is not None and parsed_release > now) else now + safe_retry
        with self._lock:
            state = self._state(create=True)
            state.remaining_requests = 0
            state.exhausted_until = max(state.exhausted_until, target_until)
            state.next_release_at = parsed_release if parsed_release is not None else target_until
            state.updated_at = now

    def can_call(self, *, priority: str = "interactive", headroom_floor: Optional[int] = None) -> tuple[bool, str]:
        effective_floor = (
            interactive_reserve_floor() if headroom_floor is None
            else max(MIN_INTERACTIVE_RESERVE, min(MAX_INTERACTIVE_RESERVE, int(headroom_floor)))
        )
        norm_priority = (priority or "interactive").strip().lower()
        now = self._clock()
        with self._lock:
            state = self._state(create=False)
            if state.exhausted_until > 0.0:
                if now < state.exhausted_until:
                    return False, "rolling_allowance_exhausted"
                state.exhausted_until = 0.0
                if state.remaining_requests == 0:
                    state.remaining_requests = None
            if norm_priority == "background" and state.remaining_requests is not None and state.remaining_requests <= effective_floor:
                return False, "headroom_reserved_for_interactive"
            return True, "ok"

    def snapshot(self) -> dict[str, Any]:
        now = self._clock()
        with self._lock:
            state = self._state(create=False)
            exhausted = state.exhausted_until > 0.0 and now < state.exhausted_until
            return {
                "remaining_requests": state.remaining_requests, "limit": state.limit,
                "next_release_at": state.next_release_at,
                "exhausted_until": state.exhausted_until if exhausted else 0.0,
                "exhausted": exhausted, "updated_at": state.updated_at,
                "interactive_reserve": interactive_reserve_floor(),
            }

    def reset(self) -> None:
        with self._lock:
            self._states.clear()


_MANAGED_ALLOWANCE_TRACKER = ManagedAllowanceTracker()


def managed_allowance_tracker() -> ManagedAllowanceTracker:
    return _MANAGED_ALLOWANCE_TRACKER


def should_llm_extract(text: str, *, min_chars: Optional[int] = None) -> tuple[bool, str]:
    stripped = (text or "").strip()
    if not stripped:
        return False, "empty_text"
    threshold = extract_min_chars() if min_chars is None else max(0, int(min_chars))
    if threshold <= 0:
        return True, "min_chars_disabled"
    if len(stripped) >= threshold:
        return True, "long_text"
    if _STRUCTURED_MARKER_RE.search(stripped):
        return True, "structured_marker"
    if _LIST_ITEM_RE.search(stripped):
        return True, "list_or_bullets"
    if _HEADING_LINE_RE.search(stripped):
        return True, "markdown_heading"
    if len([line for line in stripped.splitlines() if line.strip()]) >= 2:
        return True, "multi_line_note"
    if len(_SENTENCE_BOUNDARY_RE.findall(_ABBREV_RE.sub("abbr", stripped))) > 1:
        return True, "multi_sentence"
    if len([c.strip() for c in stripped.split(";") if len(c.strip()) >= 12]) >= 2:
        return True, "multi_clause"
    return False, "short_atomic_fact"


class LLMResultCache:
    """Bounded, thread-safe, in-memory LRU+TTL cache keyed strictly by content hash."""

    def __init__(
        self, *, max_entries: int = DEFAULT_CACHE_MAX_ENTRIES,
        ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS, clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_entries, self._ttl_seconds, self._clock = max(1, int(max_entries)), max(1.0, float(ttl_seconds)), clock
        self._lock = threading.RLock()
        self._entries: OrderedDict[tuple[str, str, str, str], tuple[float, Any]] = OrderedDict()
        self._active_scope: Any = None
        self._hits, self._misses = 0, 0

    @staticmethod
    def make_key(
        op_kind: str, provider: str, model_profile: str, payload: Any, *, scope: Any = None,
    ) -> tuple[str, str, str, str]:
        normalized = (
            " ".join(payload.split()) if isinstance(payload, str)
            else json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
        )
        if scope is not None:
            scope_blob = json.dumps(scope, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
            normalized = f"{scope_blob}\0{normalized}"
        digest = hashlib.sha256(normalized.encode("utf-8", "replace")).hexdigest()
        return (str(op_kind or "").strip().lower(), str(provider or "").strip().lower(), str(model_profile or "").strip().lower(), digest)

    def sync_scope(self, scope: Any) -> bool:
        with self._lock:
            if self._active_scope is not None and self._active_scope != scope:
                self._entries.clear()
                self._hits = self._misses = 0
                self._active_scope = scope
                return True
            self._active_scope = scope
            return False

    def update_scope(self, scope: Any) -> None:
        with self._lock:
            self._active_scope = scope

    def get(self, key: tuple[str, str, str, str]) -> tuple[bool, Any]:
        now = self._clock()
        with self._lock:
            item = self._entries.get(key)
            if item is None or now >= item[0]:
                if item is not None:
                    self._entries.pop(key, None)
                self._misses += 1
                return False, None
            self._entries.move_to_end(key)
            self._hits += 1
            return True, copy.deepcopy(item[1])

    def put(self, key: tuple[str, str, str, str], value: Any) -> bool:
        try:
            raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
            if len(raw.encode("utf-8", "replace")) > _MAX_CACHED_VALUE_BYTES:
                return False
        except (TypeError, ValueError):
            return False
        expires_at, stored = self._clock() + self._ttl_seconds, copy.deepcopy(value)
        with self._lock:
            self._entries[key] = (expires_at, stored)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)
        return True

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._active_scope = None
            self._hits = self._misses = 0

    def stats(self) -> dict[str, Any]:
        now = self._clock()
        with self._lock:
            for key in [k for k, (exp, _) in self._entries.items() if now >= exp]:
                self._entries.pop(key, None)
            return {
                "entries": len(self._entries), "max_entries": self._max_entries,
                "ttl_seconds": self._ttl_seconds, "hits": self._hits, "misses": self._misses,
            }


_LLM_RESULT_CACHE = LLMResultCache()


def llm_result_cache() -> LLMResultCache:
    return _LLM_RESULT_CACHE
