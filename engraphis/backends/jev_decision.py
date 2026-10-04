"""Experimental, opt-in decision adapter; never an authority for core memory writes.

The caller supplies a configured client and a pinned model. This module neither
imports an optional SDK nor discovers credentials or sibling repositories. No
request is made without explicit per-call authorization. Offline, unavailable,
fallback, uncertain and malformed responses defer to deterministic core behavior.
The adapter is intentionally not wired into the write or grounded-recall paths.

Backing a ``DecisionClient`` with Claude: pin an exact model id such as
``claude-sonnet-5-5`` (bounded classification questions rarely justify
``claude-opus-5-5``; measure before paying for it), never an alias ending in ``latest``.
Keep ``allow_fallback`` false so a silent model switch cannot change the pinned identity.
Newer Claude models reject ``temperature`` and forced ``tool_choice``, so ask the
``choice`` and ``noul`` questions through structured output or a plain JSON reply, and
let ``LLMClient`` (``engraphis.llm.client``) drop sampling parameters for those models.
"""
from __future__ import annotations

import inspect
import math
import os
import re
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Protocol, Sequence, Tuple, cast

from engraphis.core.interfaces import MemoryRecord

MAX_STATE_CHARS = 16_000
_VERDICTS = frozenset(("contradicts_and_supersedes", "reinforces", "orthogonal"))
_SAFE_CLIENT_ERROR_CODES = frozenset({
    "allowance_exhausted",
    "remote_timeout",
    "remote_unavailable",
    "malformed_response",
    "session_changed",
    "managed_operation_unsupported",
})


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


@dataclass(frozen=True)
class AdvisoryDecisionResult:
    """Visible state for an advisory result; unresolved values never authorize work."""

    status: str
    value: Optional[object] = None
    confidence: Optional[float] = None
    fallback_reason: Optional[str] = None
    support_probability: Optional[float] = None


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
    Clients may additionally accept per-call consent and classification keywords;
    the adapter binds the supported signature before making a single invocation.
    """

    @property
    def is_configured(self) -> bool: ...

    @property
    def allow_fallback(self) -> bool: ...

    def evaluate(
        self, state: str, questions: Sequence[DecisionQuestion], *, model: str,
        allow_remote: bool = False, purpose: str = "custom",
        data_classification: str = "internal", timeout_s: Optional[float] = None,
    ) -> object: ...


def _probability(value: object) -> bool:
    return (type(value) in (int, float) and isinstance(value, (int, float))
            and math.isfinite(value) and 0 <= value <= 1)


def get_decision_backend(
    name: Optional[str] = None, *, client: Optional[DecisionClient] = None,
    model: Optional[str] = None, offline_mode: bool = False,
) -> Optional[JevDecisionBackend]:
    selected = (name or os.environ.get("ENGRAPHIS_DECISION_BACKEND", "none")).strip().lower()
    if selected in ("jev", "typesafe", "system1", "byok", "managed", "auto"):
        backend = JevDecisionBackend(client=client, model=model, offline_mode=offline_mode)
        if backend.is_available:
            return backend
    return None


# Public compatibility imports; transports are kept separate from the advisory adapter.
from engraphis.backends.jev_transport import (  # noqa: E402,F401
    CloudDecisionBatch,
    EngraphisCloudDecisionClient,
    SimpleChoiceDecision,
    SimpleSupportDecision,
    TypeSafeDecisionClient,
    create_cloud_decision_client,
    create_typesafe_decision_client,
    select_decision_client,
)


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
        purpose: str, data_classification: str,
        timeout_s: Optional[float] = None,
    ) -> tuple[Optional[DecisionBatch], str, Optional[str]]:
        from engraphis.backends.jev_transport import DecisionClientError, contains_sensitive_content

        if allow_remote is not True:
            return None, "fallback", "remote_not_authorized"
        if not isinstance(data_classification, str) or data_classification not in {"public", "internal"}:
            return None, "fallback", "invalid_data_classification"
        if not isinstance(state, str) or not state.strip():
            return None, "fallback", "invalid_input"
        if len(state) > MAX_STATE_CHARS:
            return None, "fallback", "input_too_large"
        if (not isinstance(question, DecisionQuestion)
                or not isinstance(question.options, tuple)
                or not isinstance(question.id, str)
                or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", question.id)
                or not isinstance(question.prompt, str) or not question.prompt.strip()
                or len(question.prompt) > 1024
                or not isinstance(question.kind, str)
                or question.kind not in {"choice", "noul", "score"}
                or (question.kind == "noul" and question.options)
                or (question.kind != "noul" and not 2 <= len(question.options) <= 10)
                or any(not isinstance(option, str) or not option.strip() or len(option) > 256
                       for option in question.options)
                or len(set(question.options)) != len(question.options)):
            return None, "fallback", "invalid_input"
        question_fields = (question.id, question.prompt, question.kind, *question.options)
        if any(contains_sensitive_content(value) for value in (state, *question_fields)):
            return None, "fallback", "sensitive_content"
        call_timeout: Optional[float] = None
        if timeout_s is not None and (
            type(timeout_s) not in (int, float) or not math.isfinite(timeout_s)
            or timeout_s <= 0 or timeout_s > 15
        ):
            return None, "fallback", "deadline_exhausted"
        if timeout_s is not None:
            call_timeout = float(timeout_s)
        call_deadline = time.monotonic() + call_timeout if call_timeout is not None else None
        if not self.is_available:
            return None, "fallback", "backend_unavailable"
        client, model = self.client, self.model
        if client is None or model is None:
            return None, "fallback", "backend_unavailable"
        try:
            evaluate: Callable[..., object] = client.evaluate
            signature = inspect.signature(evaluate)
            options: Dict[str, object] = {
                "model": model, "allow_remote": True, "purpose": purpose,
                "data_classification": data_classification,
            }
            if call_timeout is not None:
                options["timeout_s"] = call_timeout
            try:
                signature.bind(state, [question], **options)
            except TypeError:
                if call_timeout is not None:
                    return None, "fallback", "client_deadline_unsupported"
                # Preserve the original injected-client contract. Never retry a
                # provider invocation: a TypeError can follow a completed request.
                try:
                    signature.bind(state, [question], model=model)
                except TypeError:
                    return None, "fallback", "client_contract_invalid"
                options = {"model": model}
            if call_deadline is not None and call_timeout is not None:
                remaining = min(call_timeout, call_deadline - time.monotonic())
                if remaining <= 0:
                    return None, "fallback", "deadline_exhausted"
                options["timeout_s"] = remaining
            response = evaluate(state, [question], **options)
            if (response is None or getattr(response, "is_fallback", None) is not False
                    or not callable(getattr(response, "get_choice", None))
                    or not callable(getattr(response, "get_noul", None))):
                return None, "fallback", "provider_fallback"
            batch = cast(DecisionBatch, response)
            return batch, "decision", None
        except DecisionClientError as exc:
            # Preserve only fixed transport states that help the caller recover.
            # Injected clients can raise this type too, so never echo an unknown code.
            code = exc.code
            reason = (code if isinstance(code, str) and code in _SAFE_CLIENT_ERROR_CODES
                      else "remote_unavailable")
            return None, "fallback", reason
        except Exception:
            # Provider exceptions may contain request text or credentials. Do not log them.
            return None, "fallback", "remote_unavailable"

    def choose_option(
        self, state: str, *, question_id: str, prompt: str,
        options: Sequence[str], allow_remote: bool = False,
        purpose: str = "custom", data_classification: str = "internal",
        timeout_s: Optional[float] = None,
    ) -> AdvisoryDecisionResult:
        """Choose among caller-supplied bounded options, or report uncertainty."""
        if (not isinstance(question_id, str) or not question_id
                or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", question_id)
                or not isinstance(prompt, str) or not prompt.strip()
                or len(prompt) > 1024
                or not isinstance(options, (list, tuple))
                or not 2 <= len(options) <= 10
                or any(not isinstance(option, str) or not option.strip() or len(option) > 256
                       for option in options)
                or len(set(options)) != len(options)):
            return AdvisoryDecisionResult("fallback", fallback_reason="invalid_input")
        question = DecisionQuestion(question_id, prompt, "choice", tuple(options))
        batch, status, reason = self._evaluate(
            state, question, allow_remote, purpose, data_classification, timeout_s,
        )
        if batch is None:
            return AdvisoryDecisionResult(status, fallback_reason=reason)
        try:
            decision = batch.get_choice(question_id)
            if (decision is None or decision.selected not in options
                    or not _probability(decision.confidence)):
                return AdvisoryDecisionResult("fallback", fallback_reason="malformed_response")
            if decision.confidence <= 0.5:
                return AdvisoryDecisionResult("uncertain", confidence=float(decision.confidence))
            return AdvisoryDecisionResult(
                "decision", value=decision.selected, confidence=float(decision.confidence),
            )
        except Exception:
            return AdvisoryDecisionResult("fallback", fallback_reason="malformed_response")

    def classify_contradiction_result(
        self, candidate_text: str, existing_memory: MemoryRecord, *,
        allow_remote: bool = False, data_classification: str = "internal",
    ) -> AdvisoryDecisionResult:
        """Return a visible contradiction classification without write authority."""
        if (not isinstance(candidate_text, str) or not candidate_text.strip()
                or not existing_memory.content.strip()):
            return AdvisoryDecisionResult("fallback", fallback_reason="invalid_input")
        state = (f"EXISTING FACT: {existing_memory.title}\n{existing_memory.content}\n\n"
                 f"NEW CANDIDATE FACT:\n{candidate_text}")
        question = DecisionQuestion(
            "verdict", "Classify the relationship between the candidate and existing fact.",
            "choice", ("contradicts_and_supersedes", "reinforces", "orthogonal"),
        )
        batch, status, reason = self._evaluate(
            state, question, allow_remote, "classify_contradiction", data_classification,
        )
        if batch is None:
            return AdvisoryDecisionResult(status, fallback_reason=reason)
        try:
            decision = batch.get_choice("verdict")
            if (decision is None or decision.selected not in _VERDICTS
                    or not _probability(decision.confidence)):
                return AdvisoryDecisionResult("fallback", fallback_reason="malformed_response")
            if decision.confidence <= 0.5:
                return AdvisoryDecisionResult("uncertain", confidence=float(decision.confidence))
            return AdvisoryDecisionResult(
                "decision", value=decision.selected, confidence=float(decision.confidence),
            )
        except Exception:
            return AdvisoryDecisionResult("fallback", fallback_reason="malformed_response")

    def verify_grounded_support_result(
        self, query: str, evidence_text: str, *, allow_remote: bool = False,
        data_classification: str = "internal",
    ) -> AdvisoryDecisionResult:
        """Return visible support status; this never participates in grounded recall."""
        if (not isinstance(query, str) or not query.strip()
                or not isinstance(evidence_text, str) or not evidence_text.strip()):
            return AdvisoryDecisionResult("fallback", fallback_reason="invalid_input")
        state = f"QUERY: {query}\n\nEVIDENCE:\n{evidence_text}"
        question = DecisionQuestion(
            "has_support", "Does the evidence directly support answering the query?", "noul",
        )
        batch, status, reason = self._evaluate(
            state, question, allow_remote, "verify_support", data_classification,
        )
        if batch is None:
            return AdvisoryDecisionResult(status, fallback_reason=reason)
        try:
            decision = batch.get_noul("has_support")
            if (decision is None or not _probability(decision.probability)
                    or not _probability(decision.confidence)):
                return AdvisoryDecisionResult("fallback", fallback_reason="malformed_response")
            if decision.confidence <= 0.5 or decision.probability == 0.5:
                return AdvisoryDecisionResult(
                    "uncertain", confidence=float(decision.confidence),
                    support_probability=float(decision.probability),
                )
            return AdvisoryDecisionResult(
                "decision", value=decision.probability > 0.5,
                confidence=float(decision.confidence),
                support_probability=float(decision.probability),
            )
        except Exception:
            return AdvisoryDecisionResult("fallback", fallback_reason="malformed_response")

    def classify_contradiction(
        self, candidate_text: str, existing_memory: MemoryRecord, *, allow_remote: bool = False,
        data_classification: str = "internal",
    ) -> Tuple[str, float]:
        """Return an advisory relationship, or ('orthogonal', 0.0) to defer.

        Even a confident provider response must never authorize invalidation;
        deterministic resolution and scope/temporal checks remain authoritative.
        """
        if (not isinstance(candidate_text, str) or not candidate_text.strip()
                or not isinstance(existing_memory.content, str)
                or not existing_memory.content.strip()):
            return "orthogonal", 0.0
        result = self.classify_contradiction_result(
            candidate_text, existing_memory, allow_remote=allow_remote,
            data_classification=data_classification,
        )
        if result.status == "decision" and isinstance(result.value, str):
            return result.value, float(result.confidence or 0.0)
        return "orthogonal", 0.0

    def verify_grounded_support(
        self, query: str, evidence_text: str, *, allow_remote: bool = False,
        data_classification: str = "internal",
    ) -> Tuple[bool, float]:
        """Return advisory support; absent or uncertain evidence never certifies it."""
        if (not isinstance(query, str) or not query.strip()
                or not isinstance(evidence_text, str) or not evidence_text.strip()):
            return False, 0.0
        result = self.verify_grounded_support_result(
            query, evidence_text, allow_remote=allow_remote,
            data_classification=data_classification,
        )
        if result.status == "decision":
            # Preserve this legacy API's probability-valued return contract.
            probability = result.support_probability
            if probability is not None:
                return bool(result.value), float(probability)
            return bool(result.value), float(result.confidence or 0.0)
        return False, 0.0
