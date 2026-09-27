"""Experimental, opt-in decision adapter; never an authority for core memory writes.

The caller supplies a configured client and a pinned model. This module neither
imports an optional SDK nor discovers credentials or sibling repositories. No
request is made without explicit per-call authorization. Offline, unavailable,
fallback, uncertain and malformed responses defer to deterministic core behavior.
The adapter is intentionally not wired into the write or grounded-recall paths.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Dict, Optional, Protocol, Sequence, Tuple

from engraphis.core.interfaces import MemoryRecord

MAX_STATE_CHARS = 16_000
_VERDICTS = frozenset(("contradicts_and_supersedes", "reinforces", "orthogonal"))


@dataclass(frozen=True)
class DecisionQuestion:
    """Dependency-free question accepted by an explicitly injected client."""

    id: str
    prompt: str
    kind: str
    options: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, object]:
        result: Dict[str, object] = {"id": self.id, "prompt": self.prompt, "type": self.kind}
        if self.options:
            result["options"] = list(self.options)
        return result


class ChoiceDecision(Protocol):
    selected: str
    confidence: float


class SupportDecision(Protocol):
    probability: float
    confidence: float


class DecisionBatch(Protocol):
    is_fallback: bool

    def get_choice(self, question_id: str) -> Optional[ChoiceDecision]: ...

    def get_noul(self, question_id: str) -> Optional[SupportDecision]: ...


class DecisionClient(Protocol):
    """Local contract, not an SDK interface; use a separately tested SDK wrapper.

    Caller owns immutable model identity, endpoint, deadlines, data filtering and
    credentials. Passing an arbitrary SDK object is not a verified integration.
    """

    @property
    def is_configured(self) -> bool: ...

    @property
    def allow_fallback(self) -> bool: ...

    def evaluate(
        self, state: str, questions: Sequence[DecisionQuestion], *, model: str,
    ) -> DecisionBatch: ...


def _probability(value: object) -> bool:
    return (type(value) in (int, float) and isinstance(value, (int, float))
            and math.isfinite(value) and 0 <= value <= 1)


def get_decision_backend(
    name: Optional[str] = None, *, client: Optional[DecisionClient] = None,
    model: Optional[str] = None, offline_mode: bool = False,
) -> Optional[JevDecisionBackend]:
    selected = (name or os.environ.get("ENGRAPHIS_DECISION_BACKEND", "none")).strip().lower()
    if selected in ("jev", "typesafe", "system1", "auto"):
        backend = JevDecisionBackend(client=client, model=model, offline_mode=offline_mode)
        if backend.is_available:
            return backend
    return None


class JevDecisionBackend:
    """Advisory decisions only; zero confidence means defer to the core."""

    identity = "engraphis.backend.jev.v1"

    def __init__(
        self, *, client: Optional[DecisionClient] = None, model: Optional[str] = None,
        offline_mode: bool = False,
    ) -> None:
        self.client = client
        self.model = model
        self.offline_mode = offline_mode

    @property
    def is_available(self) -> bool:
        if self.offline_mode or self.client is None or not self.model:
            return False
        if (not self.model.strip() or self.model != self.model.strip()
                or self.model.casefold().endswith("latest")):
            return False
        try:
            return self.client.is_configured is True and self.client.allow_fallback is False
        except Exception:
            return False

    def _evaluate(
        self, state: str, question: DecisionQuestion, allow_remote: bool,
    ) -> Optional[DecisionBatch]:
        if allow_remote is not True or len(state) > MAX_STATE_CHARS or not self.is_available:
            return None
        client, model = self.client, self.model
        if client is None or model is None:
            return None
        try:
            batch = client.evaluate(state, [question], model=model)
            return batch if batch.is_fallback is False else None
        except Exception:
            # Provider exceptions may contain request text or credentials. Do not log them.
            return None

    def classify_contradiction(
        self, candidate_text: str, existing_memory: MemoryRecord, *, allow_remote: bool = False,
    ) -> Tuple[str, float]:
        """Return an advisory relationship, or ('orthogonal', 0.0) to defer.

        Even a confident provider response must never authorize invalidation;
        deterministic resolution and scope/temporal checks remain authoritative.
        """
        if not candidate_text.strip() or not existing_memory.content.strip():
            return "orthogonal", 0.0
        state = (f"EXISTING FACT: {existing_memory.title}\n{existing_memory.content}\n\n"
                 f"NEW CANDIDATE FACT:\n{candidate_text}")
        question = DecisionQuestion(
            "verdict", "Classify the relationship between the candidate and existing fact.",
            "choice", ("contradicts_and_supersedes", "reinforces", "orthogonal"),
        )
        batch = self._evaluate(state, question, allow_remote)
        try:
            decision = batch.get_choice("verdict") if batch is not None else None
            if (decision is not None and decision.selected in _VERDICTS
                    and _probability(decision.confidence) and decision.confidence > 0.5):
                return decision.selected, float(decision.confidence)
        except Exception:
            pass
        return "orthogonal", 0.0

    def verify_grounded_support(
        self, query: str, evidence_text: str, *, allow_remote: bool = False,
    ) -> Tuple[bool, float]:
        """Return advisory support; absent or uncertain evidence never certifies it."""
        if not query.strip() or not evidence_text.strip():
            return False, 0.0
        state = f"QUERY: {query}\n\nEVIDENCE:\n{evidence_text}"
        question = DecisionQuestion(
            "has_support", "Does the evidence directly support answering the query?", "noul",
        )
        batch = self._evaluate(state, question, allow_remote)
        try:
            decision = batch.get_noul("has_support") if batch is not None else None
            if (decision is not None and _probability(decision.probability)
                    and _probability(decision.confidence) and decision.confidence > 0.5
                    and decision.probability != 0.5):
                return decision.probability > 0.5, float(decision.probability)
        except Exception:
            pass
        return False, 0.0
