"""Thread-safe managed LLM budget orchestrator, complexity gate, and result cache.

Stretches the included 100/day managed AI LLM allowance by tracking live allowance
state, failing fast (0ms) on rolling allowance exhaustion, reserving interactive
headroom, skipping LLM extraction for short atomic single-sentence facts, and
deduplicating repeated calls via a bounded in-memory content-hash LRU cache.
"""
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

DEFAULT_INTERACTIVE_RESERVE = 15
MIN_INTERACTIVE_RESERVE = 0
MAX_INTERACTIVE_RESERVE = 50
DEFAULT_LLM_EXTRACT_MIN_CHARS = 180
DEFAULT_CACHE_MAX_ENTRIES = 256
DEFAULT_CACHE_TTL_SECONDS = 300.0
_MAX_CACHED_VALUE_BYTES = 65_536

_STRUCTURED_MARKER_RE = re.compile(
    r"(?:^|\n|\s)(?:user|assistant|system|human|ai|decision|todo|"
    r"action(?:\s+item)?|handoff|blocker|follow[- ]?up|meeting|summary)\s*:",
    re.IGNORECASE,
)
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*•+]|\d+[.)])\s+\S", re.MULTILINE)
_HEADING_LINE_RE = re.compile(r"^\s*#{1,6}\s+\S", re.MULTILINE)
_ABBREV_RE = re.compile(
    r"\b(?:e\.g|i\.e|etc|vs|dr|mr|ms|mrs|prof|inc|ltd|corp|st|jr|sr)\.",
    re.IGNORECASE,
)
_SENTENCE_BOUNDARY_RE = re.compile(r"[.!?]+(?:\s+|$)")


def _parse_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except (TypeError, ValueError):
        return default


def interactive_reserve_floor(default: int = DEFAULT_INTERACTIVE_RESERVE) -> int:
    """Return configured interactive headroom reserve, clamped to 0..50."""
    return max(
        MIN_INTERACTIVE_RESERVE,
        min(MAX_INTERACTIVE_RESERVE, _parse_int_env("ENGRAPHIS_LLM_INTERACTIVE_RESERVE", default)),
    )


def extract_min_chars(default: int = DEFAULT_LLM_EXTRACT_MIN_CHARS) -> int:
    """Return configured minimum length for automatic LLM extraction skip."""
    return max(0, _parse_int_env("ENGRAPHIS_LLM_EXTRACT_MIN_CHARS", default))


def _parse_epoch_timestamp(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        fval = float(value)
        return fval if math.isfinite(fval) and fval > 0.0 else None
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            fval = float(raw)
            return fval if math.isfinite(fval) and fval > 0.0 else None
        except ValueError:
            pass
        try:
            iso = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            return None
    return None


def _parse_non_negative_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float) and value.is_integer():
        return max(0, int(value))
    return None


class ManagedAllowanceTracker:
    """Process-wide, thread-safe tracker for managed Cloud LLM rolling allowance."""

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self._remaining_requests: Optional[int] = None
        self._limit: Optional[int] = None
        self._next_release_at: Optional[float] = None
        self._exhausted_until: float = 0.0
        self._updated_at: float = 0.0

    def record_allowance(self, allowance_dict: Any) -> None:
        """Update cached allowance state from a managed ``/v1/llm/chat/completions`` payload."""
        if not isinstance(allowance_dict, dict):
            return
        remaining = _parse_non_negative_int(allowance_dict.get("remaining_requests"))
        if remaining is None:
            remaining = _parse_non_negative_int(allowance_dict.get("remaining"))
        limit = _parse_non_negative_int(allowance_dict.get("limit"))
        next_release = _parse_epoch_timestamp(allowance_dict.get("next_release_at"))

        windows = allowance_dict.get("windows")
        if isinstance(windows, list):
            chosen_window: Optional[dict[str, Any]] = None
            min_rem: Optional[int] = None
            for win in windows:
                if not isinstance(win, dict):
                    continue
                win_rem = _parse_non_negative_int(win.get("remaining_requests"))
                if chosen_window is None or (win_rem is not None and (min_rem is None or win_rem < min_rem)):
                    chosen_window, min_rem = win, win_rem
            if chosen_window is not None:
                if remaining is None:
                    remaining = min_rem
                if limit is None:
                    limit = _parse_non_negative_int(chosen_window.get("limit"))
                if next_release is None:
                    next_release = _parse_epoch_timestamp(chosen_window.get("next_release_at"))

        if remaining is None and limit is None and next_release is None:
            return
        now = self._clock()
        with self._lock:
            if remaining is not None:
                self._remaining_requests = remaining
            if limit is not None:
                self._limit = limit
            if next_release is not None:
                self._next_release_at = next_release
            self._updated_at = now
            if self._remaining_requests is not None:
                if self._remaining_requests <= 0:
                    self._remaining_requests = 0
                    self._exhausted_until = (
                        self._next_release_at
                        if self._next_release_at is not None and self._next_release_at > now
                        else now + 60.0
                    )
                else:
                    self._exhausted_until = 0.0

    def record_exhausted(self, next_release_at: Any = None, retry_after_s: float = 60.0) -> None:
        """Trip the circuit breaker immediately on quota exhaustion (HTTP 429)."""
        now = self._clock()
        parsed_release = _parse_epoch_timestamp(next_release_at)
        try:
            safe_retry = float(retry_after_s)
            if not (math.isfinite(safe_retry) and safe_retry > 0.0):
                safe_retry = 60.0
        except (TypeError, ValueError):
            safe_retry = 60.0
        safe_retry = max(1.0, min(safe_retry, 86_400.0))
        target_until = parsed_release if (parsed_release is not None and parsed_release > now) else now + safe_retry
        with self._lock:
            self._remaining_requests = 0
            self._exhausted_until = max(self._exhausted_until, target_until)
            self._next_release_at = parsed_release if parsed_release is not None else target_until
            self._updated_at = now

    def can_call(
        self, *, priority: str = "interactive", headroom_floor: Optional[int] = None,
    ) -> tuple[bool, str]:
        """Return ``(allowed, reason)`` for a managed LLM call of the given priority."""
        effective_floor = (
            interactive_reserve_floor()
            if headroom_floor is None
            else max(MIN_INTERACTIVE_RESERVE, min(MAX_INTERACTIVE_RESERVE, int(headroom_floor)))
        )
        norm_priority = (priority or "interactive").strip().lower()
        now = self._clock()
        with self._lock:
            if self._exhausted_until > 0.0:
                if now < self._exhausted_until:
                    return False, "rolling_allowance_exhausted"
                self._exhausted_until = 0.0
                if self._remaining_requests == 0:
                    self._remaining_requests = None
            if (
                norm_priority == "background"
                and self._remaining_requests is not None
                and self._remaining_requests <= effective_floor
            ):
                return False, "headroom_reserved_for_interactive"
            return True, "ok"

    def snapshot(self) -> dict[str, Any]:
        """Return a content-free snapshot of current cached allowance state."""
        now = self._clock()
        with self._lock:
            exhausted = self._exhausted_until > 0.0 and now < self._exhausted_until
            return {
                "remaining_requests": self._remaining_requests,
                "limit": self._limit,
                "next_release_at": self._next_release_at,
                "exhausted_until": self._exhausted_until if exhausted else 0.0,
                "exhausted": exhausted,
                "updated_at": self._updated_at,
                "interactive_reserve": interactive_reserve_floor(),
            }

    def reset(self) -> None:
        """Clear all cached allowance state (primarily for tests)."""
        with self._lock:
            self._remaining_requests = None
            self._limit = None
            self._next_release_at = None
            self._exhausted_until = 0.0
            self._updated_at = 0.0


_MANAGED_ALLOWANCE_TRACKER = ManagedAllowanceTracker()


def managed_allowance_tracker() -> ManagedAllowanceTracker:
    """Return the process-wide ``ManagedAllowanceTracker`` singleton."""
    return _MANAGED_ALLOWANCE_TRACKER


def should_llm_extract(text: str, *, min_chars: Optional[int] = None) -> tuple[bool, str]:
    """Deterministically decide whether ``text`` warrants an LLM extraction call."""
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
    if len([clause.strip() for clause in stripped.split(";") if len(clause.strip()) >= 12]) >= 2:
        return True, "multi_clause"
    return False, "short_atomic_fact"


class LLMResultCache:
    """Bounded, thread-safe, in-memory LRU+TTL cache keyed strictly by content hash."""

    def __init__(
        self,
        *,
        max_entries: int = DEFAULT_CACHE_MAX_ENTRIES,
        ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_entries = max(1, int(max_entries))
        self._ttl_seconds = max(1.0, float(ttl_seconds))
        self._clock = clock
        self._lock = threading.RLock()
        self._entries: OrderedDict[tuple[str, str, str, str], tuple[float, Any]] = OrderedDict()
        self._hits = 0
        self._misses = 0

    @staticmethod
    def make_key(op_kind: str, provider: str, model_profile: str, payload: Any) -> tuple[str, str, str, str]:
        """Build a content-hash cache key without storing raw input text."""
        normalized = (
            " ".join(payload.split())
            if isinstance(payload, str)
            else json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
        )
        digest = hashlib.sha256(normalized.encode("utf-8", "replace")).hexdigest()
        return (
            str(op_kind or "").strip().lower(),
            str(provider or "").strip().lower(),
            str(model_profile or "").strip().lower(),
            digest,
        )

    def get(self, key: tuple[str, str, str, str]) -> tuple[bool, Any]:
        now = self._clock()
        with self._lock:
            item = self._entries.get(key)
            if item is None:
                self._misses += 1
                return False, None
            expires_at, value = item
            if now >= expires_at:
                self._entries.pop(key, None)
                self._misses += 1
                return False, None
            self._entries.move_to_end(key)
            self._hits += 1
            return True, copy.deepcopy(value)

    def put(self, key: tuple[str, str, str, str], value: Any) -> bool:
        try:
            raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
            if len(raw.encode("utf-8", "replace")) > _MAX_CACHED_VALUE_BYTES:
                return False
        except (TypeError, ValueError):
            return False
        expires_at = self._clock() + self._ttl_seconds
        stored = copy.deepcopy(value)
        with self._lock:
            self._entries[key] = (expires_at, stored)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)
        return True

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._hits = 0
            self._misses = 0

    def stats(self) -> dict[str, Any]:
        now = self._clock()
        with self._lock:
            for key in [k for k, (exp, _) in self._entries.items() if now >= exp]:
                self._entries.pop(key, None)
            return {
                "entries": len(self._entries),
                "max_entries": self._max_entries,
                "ttl_seconds": self._ttl_seconds,
                "hits": self._hits,
                "misses": self._misses,
            }


_LLM_RESULT_CACHE = LLMResultCache()


def llm_result_cache() -> LLMResultCache:
    """Return the process-wide ``LLMResultCache`` singleton."""
    return _LLM_RESULT_CACHE
