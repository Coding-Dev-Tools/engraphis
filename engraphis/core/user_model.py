"""Deterministic user preference model for recall personalization.

This is the local-first foundation for personalized recall. It does not call an LLM and
it does not mutate the store; callers can persist ``UserModel.to_dict()`` wherever makes
sense later. The model learns lightweight preferences from interactions:

* topics from queries and selected memories
* preferred memory types / provenance sources
* preferred detail level (concise vs detailed)

``bias_recall`` returns copied result dicts with adjusted scores, leaving the original
recall output untouched. It is intentionally small and explainable so future LLM-based
personalization can be layered on top instead of replacing it.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Optional

from engraphis.core.textutil import tokenize

_MAX_WEIGHT = 10.0
_DECAY = 0.995
_DEFAULT_STRENGTH = 0.20


@dataclass
class Feedback:
    """Feedback signal for one interaction.

    ``rating`` is clamped to ``[-1, 1]``: positive means "more like this", negative means
    "less like this". ``detail`` may be ``"concise"`` or ``"detailed"``.
    """

    rating: float = 1.0
    detail: Optional[str] = None


@dataclass
class UserModel:
    """Small, serializable preference profile."""

    topics: dict[str, float] = field(default_factory=dict)
    mtypes: dict[str, float] = field(default_factory=dict)
    sources: dict[str, float] = field(default_factory=dict)
    detail_level: float = 0.5  # 0=concise, 1=detailed
    interactions: int = 0

    def update_from_interaction(
        self,
        query: str,
        selected_memories: Iterable[Any],
        feedback: Optional[Feedback | dict[str, Any]] = None,
    ) -> "UserModel":
        """Learn preferences from a query + memories the user/agent selected.

        The update is additive with tiny decay, so stale preferences fade without needing a
        background job. Returns ``self`` for convenient chaining.
        """
        fb = _feedback(feedback)
        signal = _clamp(float(fb.rating), -1.0, 1.0)
        memories = list(selected_memories or [])
        if not query and not memories:
            return self
        self.interactions += 1
        self._decay()

        topic_tokens = set(tokenize(query))
        for mem in memories:
            topic_tokens |= set(tokenize(_memory_text(mem)))
            mtype = _value(mem, "mtype")
            if mtype:
                self._add(self.mtypes, str(mtype), signal * 0.6)
            source = _source(mem)
            if source:
                self._add(self.sources, source, signal * 0.5)
        for token in topic_tokens:
            self._add(self.topics, token, signal)

        if fb.detail in ("concise", "detailed"):
            target = 0.2 if fb.detail == "concise" else 0.8
            self.detail_level = _lerp(self.detail_level, target, 0.25)
        elif memories:
            avg_len = sum(len(_value(m, "content") or "") for m in memories) / len(memories)
            if avg_len > 700:
                self.detail_level = _lerp(self.detail_level, 0.75, 0.08)
            elif avg_len < 180:
                self.detail_level = _lerp(self.detail_level, 0.25, 0.08)
        self.detail_level = _clamp(self.detail_level, 0.0, 1.0)
        return self

    def bias_recall(
        self,
        query: str,
        base_results: Iterable[Any],
        *,
        strength: float = _DEFAULT_STRENGTH,
    ) -> list[dict[str, Any]]:
        """Return personalized copies of recall results sorted by adjusted score.

        Each result includes:
        * ``base_score`` — original score if present, else 0
        * ``personalization`` — explanation block
        * ``score`` — adjusted score used for sorting
        """
        q_tokens = tokenize(query)
        strength = _clamp(float(strength), 0.0, 1.0)
        out: list[dict[str, Any]] = []
        for idx, item in enumerate(base_results or []):
            row = _as_dict(item)
            base = _float(row.get("score"), 0.0)
            pref, why = self._preference_score(row, q_tokens)
            adjusted = base + strength * pref
            row["base_score"] = base
            row["score"] = round(adjusted, 6)
            row["personalization"] = {"preference_score": round(pref, 6), **why}
            row["_rank_before_personalization"] = idx
            out.append(row)
        return sorted(out, key=lambda r: (-float(r.get("score", 0.0)),
                                          r.get("_rank_before_personalization", 0)))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[dict[str, Any]]) -> "UserModel":
        data = data or {}
        return cls(
            topics={str(k): float(v) for k, v in (data.get("topics") or {}).items()},
            mtypes={str(k): float(v) for k, v in (data.get("mtypes") or {}).items()},
            sources={str(k): float(v) for k, v in (data.get("sources") or {}).items()},
            detail_level=_clamp(_float(data.get("detail_level"), 0.5), 0.0, 1.0),
            interactions=max(0, int(data.get("interactions") or 0)),
        )

    def _preference_score(self, row: dict[str, Any], q_tokens: set[str]) -> tuple[float, dict]:
        mem_tokens = tokenize(_memory_text(row))
        # Query tokens get a small boost so personalization favors preferred topics that
        # are also relevant to the current task, not only globally popular topics.
        token_pool = mem_tokens | (q_tokens & mem_tokens)
        topic_hits = {t: self.topics[t] for t in token_pool if t in self.topics}
        topic_score = _avg(topic_hits.values())
        mtype = str(row.get("mtype") or "")
        source = _source(row)
        mtype_score = _norm(self.mtypes.get(mtype, 0.0)) if mtype else 0.0
        source_score = _norm(self.sources.get(source, 0.0)) if source else 0.0
        detail_score = self._detail_match(row)
        combined = _clamp(0.62 * topic_score + 0.18 * mtype_score
                          + 0.12 * source_score + 0.08 * detail_score, -1.0, 1.0)
        return combined, {
            "topic_hits": sorted(topic_hits)[:12],
            "topic_score": round(topic_score, 6),
            "mtype_score": round(mtype_score, 6),
            "source_score": round(source_score, 6),
            "detail_score": round(detail_score, 6),
        }

    def _detail_match(self, row: dict[str, Any]) -> float:
        content_len = len(str(row.get("content") or ""))
        if content_len >= 700:
            candidate_detail = 1.0
        elif content_len <= 180:
            candidate_detail = 0.0
        else:
            candidate_detail = (content_len - 180) / 520.0
        # Convert distance from preferred detail level into [-1, 1].
        return _clamp(1.0 - 2.0 * abs(self.detail_level - candidate_detail), -1.0, 1.0)

    def _decay(self) -> None:
        for bucket in (self.topics, self.mtypes, self.sources):
            for key in list(bucket):
                bucket[key] *= _DECAY
                if abs(bucket[key]) < 0.01:
                    del bucket[key]

    def _add(self, bucket: dict[str, float], key: str, delta: float) -> None:
        key = key.strip().lower() if key else ""
        if not key:
            return
        bucket[key] = _clamp(bucket.get(key, 0.0) + delta, -_MAX_WEIGHT, _MAX_WEIGHT)


def _feedback(value: Optional[Feedback | dict[str, Any]]) -> Feedback:
    if isinstance(value, Feedback):
        return value
    if isinstance(value, dict):
        return Feedback(rating=_float(value.get("rating"), 1.0),
                        detail=value.get("detail"))
    return Feedback()


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    out: dict[str, Any] = {}
    for key in ("id", "title", "content", "mtype", "score", "provenance"):
        if hasattr(value, key):
            out[key] = getattr(value, key)
    return out


def _memory_text(value: Any) -> str:
    return f"{_value(value, 'title') or ''}\n{_value(value, 'content') or ''}"


def _value(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)


def _source(value: Any) -> str:
    prov = _value(value, "provenance") or {}
    if isinstance(prov, dict):
        return str(prov.get("source") or "").strip().lower()
    return ""


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _avg(values: Iterable[float]) -> float:
    vals = [_norm(v) for v in values]
    return sum(vals) / len(vals) if vals else 0.0


def _norm(value: float) -> float:
    return _clamp(float(value) / _MAX_WEIGHT, -1.0, 1.0)


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _lerp(current: float, target: float, alpha: float) -> float:
    return current + (target - current) * alpha
