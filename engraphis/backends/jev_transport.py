"""Explicitly authorized Jev transports and strict typed wire validation.

Construction and configuration inspection never make network requests. Managed
requests use the normal rotating Cloud session and its credential-bound origin.
Only fixed error categories leave this module; response bodies are never logged.
"""
from __future__ import annotations

import json
import math
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, Optional, Sequence
from urllib.parse import urlsplit
from uuid import uuid4

from engraphis.http_deadline import (
    deadline_handlers as _deadline_handlers,
    read_response as _read_deadline_response,
    remaining_time as _remaining_time,
)

if TYPE_CHECKING:
    from engraphis.backends.jev_decision import DecisionQuestion

MODEL = "jev-1.13.0"
MAX_REQUEST_BYTES = 24 * 1024
MAX_RESPONSE_BYTES = 256 * 1024
MAX_ERROR_RESPONSE_BYTES = 4 * 1024
_PURPOSES = {"guard_command", "classify_contradiction", "verify_support", "verify_completion", "custom"}
_CLOUD_HTTP_ERROR_CODES = {
    "jev_rolling_allowance_exhausted": "allowance_exhausted",
    "jev_provider_protection_limit": "provider_protection_limit",
}
_SECRETS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|"
               r"xox[baprs]-[A-Za-z0-9-]{16,}|AKIA[A-Z0-9]{16}|"
               r"engr_(?:rt|dev)_[A-Za-z0-9_-]{16,})\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~-]{12,}", re.IGNORECASE),
    # Recognize conventional credential assignments in raw text, including
    # prefixed identifiers, JSON, shell and environment-index forms. This is a
    # best-effort boundary, not proof that arbitrary prose contains no secrets.
    re.compile(r"(?i)\b[a-z0-9_]*(?:api[_ -]?key|password|passwd|"
               r"secret(?:[_ -]?(?:access[_ -]?key|key))?|private[_ -]?key|"
               r"token|auth(?:orization)?|bearer)\b"
               r"[\"']?\s*(?:[\]}]\s*)?(?:=(?!=)|:)\s*"
               r"(?:\"(?:\\[^\r\n]|[^\"\\\r\n])+\""
               r"|'(?:\\[^\r\n]|[^'\\\r\n])+'"
               r"|[^\s\"'`;,\[\]{}=]+)"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
)


def contains_sensitive_content(value: str) -> bool:
    """Best-effort local check for conventional secrets before an advisory call.

    This is a transport safeguard, not a general privacy classifier. Callers must
    still obtain per-call consent and an explicit public/internal classification.
    """
    return isinstance(value, str) and any(pattern.search(value) for pattern in _SECRETS)


def _http_error_fallback_code(url: str, error: urllib.error.HTTPError) -> str:
    """Map only the two fixed Cloud 429 reasons; never expose provider text."""
    try:
        if error.code != 429 or urlsplit(url).path.rstrip("/") != "/v1/jev/decide":
            return "remote_unavailable"
        headers = error.headers or {}
        if headers.get("Content-Type", "").partition(";")[0].strip() != "application/json":
            return "remote_unavailable"
        length = headers.get("Content-Length", "")
        if length and (not length.isdigit() or int(length) > MAX_ERROR_RESPONSE_BYTES):
            return "remote_unavailable"
        raw = error.read(MAX_ERROR_RESPONSE_BYTES + 1)
        if not isinstance(raw, bytes) or len(raw) > MAX_ERROR_RESPONSE_BYTES:
            return "remote_unavailable"

        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate JSON key")
                result[key] = value
            return result

        def invalid(_value):
            raise ValueError("non-finite JSON value")

        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
                             parse_constant=invalid)
        detail = payload.get("detail") if isinstance(payload, dict) else None
        if not isinstance(detail, dict) or detail.get("is_fallback") is not False:
            return "remote_unavailable"
        code = detail.get("code")
        if not isinstance(code, str):
            return "remote_unavailable"
        return _CLOUD_HTTP_ERROR_CODES.get(code, "remote_unavailable")
    except Exception:
        return "remote_unavailable"


class DecisionClientError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class SimpleChoiceDecision:
    selected: str
    confidence: float
    confidence_source: str = "provider"


@dataclass(frozen=True)
class SimpleSupportDecision:
    probability: float
    confidence: float
    confidence_source: str = "derived_decisiveness"


@dataclass(frozen=True)
class SimpleScoreDecision:
    score: float
    confidence: float
    probabilities: Dict[str, float]
    legend: Dict[str, str]
    confidence_source: str = "provider"


@dataclass
class CloudDecisionBatch:
    is_fallback: bool
    choices: Dict[str, SimpleChoiceDecision]
    nouls: Dict[str, SimpleSupportDecision]
    scores: Dict[str, SimpleScoreDecision] = field(default_factory=dict)

    def get_choice(self, question_id: str) -> Optional[SimpleChoiceDecision]:
        return self.choices.get(question_id)

    def get_noul(self, question_id: str) -> Optional[SimpleSupportDecision]:
        return self.nouls.get(question_id)

    def get_score(self, question_id: str) -> Optional[SimpleScoreDecision]:
        return self.scores.get(question_id)


def _number(value: object, maximum: float = 1.0) -> float:
    if (type(value) not in (int, float) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not 0 <= value <= maximum):
        raise DecisionClientError("malformed_response")
    return float(value)


def _distribution(value: object, keys: set) -> Dict[str, float]:
    if not isinstance(value, dict) or set(value) != keys:
        raise DecisionClientError("malformed_response")
    result = {key: _number(probability) for key, probability in value.items()}
    if not math.isclose(sum(result.values()), 1.0, abs_tol=0.001):
        raise DecisionClientError("malformed_response")
    return result


def parse_decision_batch(
    body: object, questions: Sequence[DecisionQuestion], *, normalized: bool,
) -> CloudDecisionBatch:
    if not isinstance(body, dict) or body.get("model") != MODEL:
        raise DecisionClientError("malformed_response")
    # Managed envelopes declare success explicitly; the native TypeSafe schema
    # omits this field but may still return an explicit fallback marker.
    fallback = body.get("is_fallback") if normalized else body.get("is_fallback", False)
    if type(fallback) is not bool:
        raise DecisionClientError("malformed_response")
    if fallback:
        return CloudDecisionBatch(True, {}, {})
    values = body.get("decisions" if normalized else "answers")
    if not isinstance(values, dict) or set(values) != {q.id for q in questions}:
        raise DecisionClientError("malformed_response")
    batch = CloudDecisionBatch(False, {}, {})
    for question in questions:
        answer = values[question.id]
        if not isinstance(answer, dict) or answer.get("type") != question.kind:
            raise DecisionClientError("malformed_response")
        if question.kind == "noul":
            probability = _number(answer.get("probability" if normalized else "noul"))
            confidence = abs(2 * probability - 1)
            if normalized and (
                answer.get("confidence_source") != "derived_decisiveness"
                or not math.isclose(_number(answer.get("confidence")), confidence, abs_tol=1e-9)
            ):
                raise DecisionClientError("malformed_response")
            batch.nouls[question.id] = SimpleSupportDecision(probability, confidence)
        elif question.kind == "choice":
            selected = answer.get("selected" if normalized else "choice")
            if not isinstance(selected, str) or selected not in question.options:
                raise DecisionClientError("malformed_response")
            probabilities = _distribution(answer.get("probabilities"), set(question.options))
            if probabilities[selected] + 0.000001 < max(probabilities.values()):
                raise DecisionClientError("malformed_response")
            confidence = _number(answer.get("confidence"))
            if normalized and answer.get("confidence_source") != "provider":
                raise DecisionClientError("malformed_response")
            batch.choices[question.id] = SimpleChoiceDecision(selected, confidence)
        elif question.kind == "score":
            legend = {str(index): label for index, label in enumerate(question.options)}
            if answer.get("legend") != legend:
                raise DecisionClientError("malformed_response")
            probabilities = _distribution(answer.get("probabilities"), set(legend))
            score = _number(answer.get("score"), len(question.options) - 1)
            weighted = sum(int(index) * probability for index, probability in probabilities.items())
            if not math.isclose(score, weighted, abs_tol=0.01):
                raise DecisionClientError("malformed_response")
            confidence = _number(answer.get("confidence"))
            if normalized and answer.get("confidence_source") != "provider":
                raise DecisionClientError("malformed_response")
            batch.scores[question.id] = SimpleScoreDecision(score, confidence, probabilities, legend)
        else:
            raise DecisionClientError("malformed_response")
    return batch


def _request_payload(
    state: str, questions: Sequence[DecisionQuestion], model: str, *,
    allow_remote: bool, purpose: str, data_classification: str,
) -> dict:
    if allow_remote is not True:
        raise DecisionClientError("remote_not_authorized")
    if model != MODEL or purpose not in _PURPOSES or data_classification not in {"public", "internal"}:
        raise DecisionClientError("invalid_request")
    if (not isinstance(state, str) or not state.strip() or len(state) > 16000
            or not 1 <= len(questions) <= 4):
        raise DecisionClientError("invalid_request")
    texts = [state]
    seen = set()
    for q in questions:
        if (not isinstance(q.id, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", q.id)
                or q.id in seen or not isinstance(q.prompt, str) or not q.prompt.strip()
                or len(q.prompt) > 1024 or q.kind not in {"choice", "noul", "score"}):
            raise DecisionClientError("invalid_request")
        seen.add(q.id)
        if q.kind == "noul":
            if q.options:
                raise DecisionClientError("invalid_request")
        elif not 2 <= len(q.options) <= 10:
            raise DecisionClientError("invalid_request")
        if any(not isinstance(option, str) or not option.strip() or len(option) > 256
               for option in q.options) or len(set(q.options)) != len(q.options):
            raise DecisionClientError("invalid_request")
        texts.extend((q.id, q.prompt, *q.options))
    if any(contains_sensitive_content(value) for value in texts):
        raise DecisionClientError("sensitive_content")
    payload = {"model": model, "state": state, "questions": [q.to_dict() for q in questions],
               "allow_remote": True, "purpose": purpose, "data_classification": data_classification}
    if len(json.dumps(payload, ensure_ascii=False).encode()) > MAX_REQUEST_BYTES:
        raise DecisionClientError("invalid_request")
    return payload


def _timeout(value: float) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 15:
        raise ValueError("decision timeout must be between zero and fifteen seconds")
    return float(value)


def _validate_managed_context(
    state: str, questions: Sequence[DecisionQuestion], purpose: str,
) -> None:
    """Match Cloud's fixed advisory questions and concrete context schemas.

    Known MCP and direct-adapter wire forms remain compatible. A purpose label
    cannot turn arbitrary questions into an included managed operation. These
    checks do not establish the provenance or truth of supplied plaintext.
    """
    expected = {
        "guard_command": (
            ("is_safe", "Is this command free of destructive data loss or secret leakage?",
             "noul", ()),
            ("category", "Categorize this operation", "choice",
             ("read_only", "state_change", "destructive_or_leak")),
        ),
        "classify_contradiction": (
            ("verdict", "Classify the relationship between the facts.", "choice",
             ("contradicts_and_supersedes", "reinforces", "orthogonal")),
        ),
        "verify_support": (
            ("has_support", "Does this evidence directly support answering the query?",
             "noul", ()),
        ),
        "verify_completion": (
            ("is_complete", "Does the supplied evidence establish the task goal?", "noul", ()),
        ),
    }.get(purpose)
    if expected is None:
        raise DecisionClientError("managed_operation_unsupported")
    actual = tuple((q.id, q.prompt, q.kind, tuple(q.options)) for q in questions)
    fields = [(state, 16000)]
    if purpose in {"classify_contradiction", "verify_support"}:
        if purpose == "classify_contradiction":
            prefix, delimiter = "EXISTING FACT: ", "\nNEW CANDIDATE FACT: "
            direct_prompt = "Classify the relationship between the candidate and existing fact."
            direct_delimiter = "\n\nNEW CANDIDATE FACT:\n"
            first_limit = 16000
        else:
            prefix, delimiter = "QUERY: ", "\nEVIDENCE: "
            direct_prompt = "Does the evidence directly support answering the query?"
            direct_delimiter = "\n\nEVIDENCE:\n"
            first_limit = 4096
        first = expected[0]
        direct = ((first[0], direct_prompt, first[2], first[3]),)
        if actual == direct:
            expected, delimiter = direct, direct_delimiter
        if not state.startswith(prefix) or state.count(delimiter) != 1:
            raise DecisionClientError("invalid_request")
        before, after = state[len(prefix):].split(delimiter)
        fields = [(before, first_limit), (after, 16000)]
    elif purpose == "verify_completion":
        if (not state.startswith("GOAL: ") or state.count("\nACTIONS: ") != 1
                or state.count("\nOUTPUT: ") != 1):
            raise DecisionClientError("invalid_request")
        goal, remaining = state[len("GOAL: "):].split("\nACTIONS: ")
        if "\nOUTPUT: " not in remaining:
            raise DecisionClientError("invalid_request")
        actions, output = remaining.split("\nOUTPUT: ")
        if len(actions) > 8192:
            raise DecisionClientError("invalid_request")
        fields = [(goal, 4096), (output, 16000)]
    if actual != expected or any(not value.strip() or len(value) > limit for value, limit in fields):
        raise DecisionClientError("invalid_request")


def _read_response(response, deadline: float) -> bytes:
    raw = _read_deadline_response(response, deadline, max_bytes=MAX_RESPONSE_BYTES)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise DecisionClientError("malformed_response")
    return raw


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise DecisionClientError("remote_unavailable")


def _post_json(
    url: str, token: str, payload: dict, timeout_s: float, *, deadline: Optional[float] = None,
) -> object:
    from engraphis.hosted_client import (
        _is_loopback_host, build_pinned_https_opener, validate_cloud_base_url,
    )

    if deadline is None:
        deadline = time.monotonic() + timeout_s
    try:
        _remaining_time(deadline)
        parts = urlsplit(url)
        permitted_transport = parts.scheme == "https" or (
            parts.scheme == "http" and _is_loopback_host(parts.hostname or "")
        )
        if (not permitted_transport or not token or token != token.strip()
                or any(char.isspace() for char in token)):
            raise DecisionClientError("invalid_configuration")
        # Validation and DNS occur only inside an explicitly authorized call.
        url = validate_cloud_base_url(url)
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
        if len(encoded) > MAX_REQUEST_BYTES:
            raise DecisionClientError("invalid_request")
        request = urllib.request.Request(url, data=encoded, method="POST", headers={
            "Authorization": "Bearer " + token, "Content-Type": "application/json",
            "Accept": "application/json", "User-Agent": "engraphis-jev/1",
        })
        loopback_only = _is_loopback_host(urlsplit(url).hostname or "")
        handlers = [_NoRedirect(), *_deadline_handlers(deadline, loopback_only=loopback_only)]
        if loopback_only:
            handlers.append(urllib.request.ProxyHandler({}))
        with build_pinned_https_opener(*handlers).open(
            request, timeout=_remaining_time(deadline),
        ) as response:
            if response.status != 200:
                raise DecisionClientError("remote_unavailable")
            if response.headers.get("Content-Type", "").partition(";")[0].strip() != "application/json":
                raise DecisionClientError("malformed_response")
            size = response.headers.get("Content-Length", "")
            if size and (not size.isdigit() or int(size) > MAX_RESPONSE_BYTES):
                raise DecisionClientError("malformed_response")
            raw = _read_response(response, deadline)

        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise DecisionClientError("malformed_response")
                result[key] = value
            return result

        def invalid(_value):
            raise DecisionClientError("malformed_response")

        try:
            return json.loads(bytes(raw).decode("utf-8"), object_pairs_hook=unique,
                              parse_constant=invalid)
        except (UnicodeError, ValueError):
            raise DecisionClientError("malformed_response") from None
    except DecisionClientError:
        raise
    except urllib.error.HTTPError as exc:
        try:
            code = _http_error_fallback_code(url, exc)
        finally:
            exc.close()
        raise DecisionClientError(code) from None
    except TimeoutError:
        raise DecisionClientError("remote_timeout") from None
    except Exception:
        code = "remote_timeout" if time.monotonic() >= deadline else "remote_unavailable"
        raise DecisionClientError(code) from None


class EngraphisCloudDecisionClient:
    """Managed allowance uses the saved Cloud login; no standalone bearer override."""

    def __init__(self, *, timeout_s: float = 10.0) -> None:
        self.timeout_s = _timeout(timeout_s)

    @property
    def is_configured(self) -> bool:
        from engraphis import cloud_session
        try:
            return bool(cloud_session.configured(require_compute=False)
                        and cloud_session.credential_bound_control_url())
        except Exception:
            return False

    @property
    def allow_fallback(self) -> bool:
        return False

    def evaluate(self, state: str, questions: Sequence[DecisionQuestion], *, model: str,
                 allow_remote: bool = False, purpose: str = "custom",
                 data_classification: str = "internal",
                 timeout_s: Optional[float] = None,
                 request_key: Optional[str] = None) -> CloudDecisionBatch:
        """Evaluate once; callers can reuse an explicit key after a lost reply.

        Cloud charges evaluated questions and rejects duplicate keys without a
        second provider call. This client never retries automatically.
        """
        effective_timeout = min(self.timeout_s, _timeout(timeout_s)) if timeout_s is not None else self.timeout_s
        deadline = time.monotonic() + effective_timeout
        payload = _request_payload(state, questions, model, allow_remote=allow_remote,
                                   purpose=purpose, data_classification=data_classification)
        _validate_managed_context(state, questions, purpose)
        if request_key is not None and (
            not isinstance(request_key, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", request_key)
        ):
            raise DecisionClientError("invalid_request")
        payload["request_key"] = request_key if request_key is not None else uuid4().hex
        if len(json.dumps(payload, ensure_ascii=False).encode()) > MAX_REQUEST_BYTES:
            raise DecisionClientError("invalid_request")
        from engraphis import cloud_session
        from engraphis.hosted_client import validate_cloud_base_url
        try:
            before = cloud_session.credential_bound_control_url()
            _remaining_time(deadline)
            # The first refresh persists this validated form. Compare the same
            # full base URL before and after bootstrap, including its path/port.
            before = validate_cloud_base_url(before)
            _remaining_time(deadline)
            token, _organization, _compute = cloud_session.access_for_workspace(
                None, require_compute=False, deadline=deadline,
            )
            _remaining_time(deadline)
            control = cloud_session.credential_bound_control_url()
            _remaining_time(deadline)
            control = validate_cloud_base_url(control)
            _remaining_time(deadline)
            if not before or control != before:
                raise DecisionClientError("session_changed")
            body = _post_json(control.rstrip("/") + "/v1/jev/decide", token, payload,
                              effective_timeout, deadline=deadline)
            result = parse_decision_batch(body, questions, normalized=True)
            _remaining_time(deadline)
            return result
        except DecisionClientError:
            raise
        except TimeoutError:
            raise DecisionClientError("remote_timeout") from None
        except Exception:
            code = "remote_timeout" if time.monotonic() >= deadline else "remote_unavailable"
            raise DecisionClientError(code) from None


class TypeSafeDecisionClient:
    """Explicit BYOK route to the pinned TypeSafe origin; never an automatic fallback."""

    def __init__(self, *, api_key: Optional[str] = None, base_url: Optional[str] = None,
                 timeout_s: float = 10.0) -> None:
        self.api_key = (api_key if api_key is not None else
                        os.environ.get("TYPESAFE_API_KEY") or os.environ.get("JEV_API_KEY") or "")
        self.base_url = (base_url if base_url is not None else
                         os.environ.get("TYPESAFE_BASE_URL", "https://api.typesafe.ai")).rstrip("/")
        self.timeout_s = _timeout(timeout_s)

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key and self.api_key.strip() == self.api_key
                    and self.api_key not in {"mock", "offline"}
                    and self.base_url == "https://api.typesafe.ai")

    @property
    def allow_fallback(self) -> bool:
        return False

    def evaluate(self, state: str, questions: Sequence[DecisionQuestion], *, model: str,
                 allow_remote: bool = False, purpose: str = "custom",
                 data_classification: str = "internal",
                 timeout_s: Optional[float] = None) -> CloudDecisionBatch:
        _request_payload(state, questions, model, allow_remote=allow_remote,
                         purpose=purpose, data_classification=data_classification)
        if not self.is_configured:
            raise DecisionClientError("invalid_configuration")
        wire: Dict[str, Dict[str, object]] = {}
        for q in questions:
            wire[q.id] = {"type": q.kind, "instructions": q.prompt}
            if q.kind == "choice":
                wire[q.id]["criteria"] = {label: label for label in q.options}
            elif q.kind == "score":
                wire[q.id]["criteria"] = list(q.options)
        effective_timeout = min(self.timeout_s, _timeout(timeout_s)) if timeout_s is not None else self.timeout_s
        body = _post_json(self.base_url + "/v1/systemone", self.api_key,
                          {"model": model, "state": state, "questions": wire}, effective_timeout)
        return parse_decision_batch(body, questions, normalized=False)


def create_cloud_decision_client(*, timeout_s: float = 10.0) -> EngraphisCloudDecisionClient:
    return EngraphisCloudDecisionClient(timeout_s=timeout_s)


def create_typesafe_decision_client(*, api_key: Optional[str] = None,
                                   base_url: Optional[str] = None,
                                   timeout_s: float = 10.0) -> TypeSafeDecisionClient:
    return TypeSafeDecisionClient(api_key=api_key, base_url=base_url, timeout_s=timeout_s)


def select_decision_client(name: Optional[str] = None, *, offline_mode: bool = False):
    selected = (name if name is not None else
                os.environ.get("ENGRAPHIS_DECISION_BACKEND", "none")).strip().lower()
    if offline_mode or selected in {"none", "local"}:
        return None, "local_heuristic"
    if selected in {"managed", "auto"}:
        client = create_cloud_decision_client()
        return (client, "engraphis_cloud") if client.is_configured else (None, "local_heuristic")
    if selected in {"byok", "typesafe", "jev", "system1"}:
        direct = create_typesafe_decision_client()
        return (direct, "typesafe_byok") if direct.is_configured else (None, "local_heuristic")
    return None, "local_heuristic"
