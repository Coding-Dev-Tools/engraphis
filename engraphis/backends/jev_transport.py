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
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, Optional, Sequence
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from engraphis.backends.jev_decision import DecisionQuestion

MODEL = "jev-1.13.0"
MAX_REQUEST_BYTES = 24 * 1024
MAX_RESPONSE_BYTES = 256 * 1024
_PURPOSES = {"guard_command", "classify_contradiction", "verify_support", "verify_completion", "custom"}
_SECRETS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|"
               r"xox[baprs]-[A-Za-z0-9-]{16,}|AKIA[A-Z0-9]{16}|"
               r"engr_(?:rt|dev)_[A-Za-z0-9_-]{16,})\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~-]{12,}", re.IGNORECASE),
    re.compile(r"(?i)\b(?:api[_ -]?key|password|secret|access[_ -]?token)\b"
               r"[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9/+_.~-]{8,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
)


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
    if any(pattern.search(value) for value in texts for pattern in _SECRETS):
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


def _remaining_time(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("decision request deadline exceeded")
    return remaining


@contextmanager
def _socket_deadline(sock, deadline: float):
    """Interrupt a blocking HTTP parser even when every receive makes progress."""
    import socket
    import threading

    timer = None
    if isinstance(sock, socket.socket):
        # Even read1() can consume several reads while parsing chunk framing.
        # Interrupt the socket at the deadline so slow chunk headers cannot keep
        # a single read1() alive. Shutdown does not acquire the reader's lock.
        def expire():
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        timer = threading.Timer(_remaining_time(deadline), expire)
        timer.daemon = True
        timer.start()
    try:
        yield
    finally:
        if timer is not None:
            timer.cancel()


def _deadline_handlers(deadline: float, *, loopback_only: bool = False):
    import http.client
    import ipaddress
    import socket
    import urllib.request
    from functools import partial
    from engraphis.hosted_client import PinnedHTTPSConnection, PinnedHTTPSHandler

    def connect_socket(address, timeout=None, source_address=None, *, loopback_only=False):
        # socket.create_connection renews its timeout for each resolved address.
        # Share the request budget across direct, loopback and proxy dial retries.
        _remaining_time(deadline)
        host, port = address
        candidates = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
        last_error = None
        for family, kind, protocol, _, target in candidates:
            remaining = _remaining_time(deadline)
            if loopback_only and not ipaddress.ip_address(target[0]).is_loopback:
                raise ValueError("loopback decisions must connect to loopback")
            sock = None
            try:
                sock = socket.socket(family, kind, protocol)
                sock.settimeout(remaining)
                if source_address is not None:
                    sock.bind(source_address)
                sock.connect(target)
                # TLS must receive only the budget left after the TCP dial.
                sock.settimeout(_remaining_time(deadline))
                return sock
            except OSError as exc:
                if sock is not None:
                    sock.close()
                last_error = exc
        if last_error is not None:
            raise last_error
        raise OSError("decision endpoint has no connectable address")

    def send_with_deadline(connection, send, data):
        if connection.sock is None:
            if not connection.auto_open:
                raise http.client.NotConnected()
            connection.connect()
        if connection.sock is None:
            raise http.client.NotConnected()
        connection.sock.settimeout(_remaining_time(deadline))
        # Headers and bodies are separate sends; SSL/file sends may also loop.
        with _socket_deadline(connection.sock, deadline):
            send(data)
            _remaining_time(deadline)

    class DeadlineResponse(http.client.HTTPResponse):
        def __init__(self, sock, *args, **kwargs):
            self._deadline_socket = sock
            super().__init__(sock, *args, **kwargs)

        def begin(self):
            # getresponse() parses status and headers before urllib.open()
            # returns. Protect that phase before a response body is available.
            with _socket_deadline(self._deadline_socket, deadline):
                super().begin()
                _remaining_time(deadline)

    class DeadlineHTTPConnection(http.client.HTTPConnection):
        response_class = DeadlineResponse

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._create_connection = partial(connect_socket, loopback_only=True)

        def send(self, data):
            send_with_deadline(self, super().send, data)

    class DeadlineHTTPSConnection(PinnedHTTPSConnection):
        response_class = DeadlineResponse

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._create_connection = partial(connect_socket, loopback_only=loopback_only)

        def _connect_deadline(self):
            return deadline

        def _attempt_timeout(self, connect_deadline):
            # The shared hosted client has a 500 ms floor; decisions do not.
            return _remaining_time(deadline)

        def send(self, data):
            send_with_deadline(self, super().send, data)

        def _tunnel(self):
            # CONNECT parses its response directly, bypassing response.begin().
            with _socket_deadline(self.sock, deadline):
                # typeshed omits this private standard-library method.
                getattr(super(), "_tunnel")()
                # TLS follows CONNECT and shares its remaining budget.
                self.sock.settimeout(_remaining_time(deadline))

    class DeadlineHTTPHandler(urllib.request.HTTPHandler):
        def http_open(self, req):
            return self.do_open(DeadlineHTTPConnection, req)

    class DeadlineHTTPSHandler(PinnedHTTPSHandler):
        # Run before the shared opener's ordinary pinned HTTPS handler.
        handler_order = 499

        def do_open(self, http_class, req, **kwargs):
            return super().do_open(DeadlineHTTPSConnection, req, **kwargs)

    return DeadlineHTTPHandler(), DeadlineHTTPSHandler()


def _read_response(response, deadline: float) -> bytes:
    """Bound total body-read time, including a peer that continuously drips bytes."""
    sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
    with _socket_deadline(sock, deadline):
        return _read_response_chunks(response, deadline)


def _read_response_chunks(response, deadline: float) -> bytes:
    data = bytearray()
    while len(data) <= MAX_RESPONSE_BYTES:
        remaining = _remaining_time(deadline)
        # urllib's HTTPResponse wraps SocketIO in a BufferedReader. Tighten the
        # underlying socket deadline for each read rather than renewing the full
        # timeout. fp is None after a length-delimited response reaches EOF.
        sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
        if sock is not None:
            sock.settimeout(remaining)
        # read() tries to fill its entire buffer; read1() returns after a single
        # buffered/socket read, letting the absolute deadline run between chunks.
        chunk = response.read1(min(4096, MAX_RESPONSE_BYTES + 1 - len(data)))
        _remaining_time(deadline)
        if not chunk:
            break
        data.extend(chunk)
    if len(data) > MAX_RESPONSE_BYTES:
        raise DecisionClientError("malformed_response")
    return bytes(data)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise DecisionClientError("remote_unavailable")


def _post_json(url: str, token: str, payload: dict, timeout_s: float) -> object:
    from engraphis.hosted_client import (
        _is_loopback_host, build_pinned_https_opener, validate_cloud_base_url,
    )

    deadline = time.monotonic() + timeout_s
    try:
        if (urlsplit(url).scheme != "https" or not token or token != token.strip()
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
        code = "allowance_exhausted" if exc.code == 429 else "remote_unavailable"
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
            return cloud_session.configured(require_compute=False)
        except Exception:
            return False

    @property
    def allow_fallback(self) -> bool:
        return False

    def evaluate(self, state: str, questions: Sequence[DecisionQuestion], *, model: str,
                 allow_remote: bool = False, purpose: str = "custom",
                 data_classification: str = "internal") -> CloudDecisionBatch:
        payload = _request_payload(state, questions, model, allow_remote=allow_remote,
                                   purpose=purpose, data_classification=data_classification)
        from engraphis import cloud_session
        try:
            before = cloud_session.credential_bound_control_url()
            token, _organization, _compute = cloud_session.access_for_workspace(
                None, require_compute=False,
            )
            control = cloud_session.credential_bound_control_url()
            if not before or control != before:
                raise DecisionClientError("session_changed")
            body = _post_json(control.rstrip("/") + "/v1/jev/decide", token, payload, self.timeout_s)
            return parse_decision_batch(body, questions, normalized=True)
        except DecisionClientError:
            raise
        except Exception:
            raise DecisionClientError("remote_unavailable") from None


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
                 data_classification: str = "internal") -> CloudDecisionBatch:
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
        body = _post_json(self.base_url + "/v1/systemone", self.api_key,
                          {"model": model, "state": state, "questions": wire}, self.timeout_s)
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
