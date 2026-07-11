"""Fact extractors — implementations of the ``core.interfaces.Extractor`` protocol.

Every SOTA memory system (mem0, A-Mem, Letta) auto-distills raw text into discrete
facts before storage; Engraphis makes that step *pluggable and optional* so the core
stays offline-capable (AGENTS.md §3.8):

* ``PassthroughExtractor`` — the default: the caller's text is stored exactly as given
  (today's behaviour, zero dependencies, zero network).
* ``LLMExtractor``        — distills a raw blob (a conversation turn, a log, a diff
  summary) into discrete, self-contained facts with type/importance/keyword hints,
  using any configured LLM. Fails soft: any error degrades to passthrough, never to a
  lost write.

Selected via ``get_extractor()`` from ``ENGRAPHIS_EXTRACTOR`` (= ``none`` | ``llm``) —
a config change, not a refactor, matching every other backend swap in this codebase.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

from engraphis.core.interfaces import ExtractedFact, MemoryType

MAX_FACTS = 12

# LLM output is untrusted input too (indirect prompt injection can steer it): strip the
# same control characters service.py strips from direct writes, so extracted facts can't
# smuggle hidden-instruction / terminal-escape payloads past the validation layer.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _defang(value: str, limit: int) -> str:
    return _CONTROL_RE.sub("", value)[:limit].strip()

_EXTRACT_SYSTEM_PROMPT = (
    "You distill raw text into discrete, self-contained memory facts for an AI agent's "
    "long-term memory. Each fact must stand alone (no pronouns that depend on the "
    "original text), be worth remembering beyond this moment, and be stated in one or "
    "two sentences. Skip filler, pleasantries, and transient chatter.\n\n"
    "Classify each fact:\n"
    "- semantic: durable facts, preferences, conventions ('The API uses PASETO tokens')\n"
    "- episodic: events and decisions with a when ('On 2026-06-30 PR #99 was merged')\n"
    "- procedural: how-tos and playbooks ('To rebuild the index, run ...')\n"
    "- working: transient state only relevant right now ('Currently blocked on CI')\n\n"
    "Respond with JSON only, no markdown fences, no prose:\n"
    '{"facts": [{"content": str, "title": str, "mtype": "semantic|episodic|procedural|'
    'working", "importance": <0..1>, "keywords": [str, ...]}]}'
)


class PassthroughExtractor:
    """The offline default: one fact, the text as given."""

    def extract(self, text: str, *, context: str = "") -> list[ExtractedFact]:
        return [ExtractedFact(content=text)]


class LLMExtractor:
    """LLM-backed fact distillation behind the ``Extractor`` protocol.

    ``llm`` may be anything with a ``chat(messages, system=...) -> str`` method (the
    engraphis v1 ``LLMClient``) or a ``complete(messages) -> str`` method (the
    ``core.interfaces.LLM`` protocol). Untrusted input goes *into* the prompt; the
    output is parsed defensively and every fact re-validated — a malformed or
    adversarial response degrades to passthrough rather than corrupting the store.
    """

    def __init__(self, llm: Any, *, max_facts: int = MAX_FACTS) -> None:
        self.llm = llm
        self.max_facts = max_facts

    def extract(self, text: str, *, context: str = "") -> list[ExtractedFact]:
        prompt = f"Context: {context}\n\nText to distill:\n{text}" if context else text
        try:
            raw = self._ask(prompt)
            facts = self._parse(raw)
        except Exception:
            facts = []
        return facts or [ExtractedFact(content=text)]

    # ── internals ────────────────────────────────────────────────────────────
    def _ask(self, prompt: str) -> str:
        messages = [{"role": "user", "content": prompt}]
        if hasattr(self.llm, "chat"):
            return self.llm.chat(messages, system=_EXTRACT_SYSTEM_PROMPT)
        return self.llm.complete(
            [{"role": "system", "content": _EXTRACT_SYSTEM_PROMPT}, *messages])

    def _parse(self, raw: str) -> list[ExtractedFact]:
        data = _loads_lenient(raw)
        out: list[ExtractedFact] = []
        for item in (data.get("facts") or [])[: self.max_facts]:
            if not isinstance(item, dict):
                continue
            content = _defang(str(item.get("content") or ""), 100_000)
            if not content:
                continue
            mtype: Optional[MemoryType] = None
            try:
                mtype = MemoryType(str(item.get("mtype", "")).strip().lower())
            except ValueError:
                mtype = None
            try:
                importance = max(0.0, min(1.0, float(item.get("importance", 0.0))))
            except (TypeError, ValueError):
                importance = 0.0
            keywords = [_defang(str(k), 128) for k in (item.get("keywords") or [])[:16]
                        if isinstance(k, (str, int, float))]
            out.append(ExtractedFact(content=content,
                                     title=_defang(str(item.get("title") or ""), 1_000),
                                     mtype=mtype, importance=importance,
                                     keywords=[k for k in keywords if k]))
        return out


def _loads_lenient(raw: str) -> dict:
    """Parse JSON that may arrive wrapped in markdown fences or prose."""
    raw = (raw or "").strip()
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, RecursionError):
        pass
    m = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except (json.JSONDecodeError, RecursionError):
            pass
    return {}


def get_extractor(kind: str = "none", llm: Any = None):
    """Factory mirroring ``get_embedder``/``get_vector_index``: config in, backend out.

    ``kind='llm'`` with no ``llm`` builds the v1 multi-provider ``LLMClient`` from
    settings (heavy import gated here, never in ``core/``). Anything else — including
    an LLM kind with no usable client — returns the offline passthrough.
    """
    if (kind or "none").lower() != "llm":
        return PassthroughExtractor()
    if llm is None:
        try:
            from engraphis.llm.client import LLMClient
            llm = LLMClient()
        except Exception:
            return PassthroughExtractor()
    return LLMExtractor(llm)
