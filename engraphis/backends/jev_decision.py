"""Experimental, opt-in decision adapter; never an authority for core memory writes.

The caller supplies a configured client and a pinned model. Importing this module
does not load an optional SDK or discover credentials or sibling repositories. No
backend request is made without explicit per-call authorization. Offline, unavailable,
fallback, uncertain and malformed responses defer to deterministic core behavior.
The adapter is intentionally not wired into the write or grounded-recall paths.
"""
from __future__ import annotations

import math
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Dict, Optional, Protocol, Sequence, Tuple

from engraphis.core.interfaces import MemoryRecord

MAX_STATE_CHARS = 16_000
MAX_RESPONSE_BYTES = 64 * 1024
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
    @property
    def selected(self) -> str: ...

    @property
    def confidence(self) -> float: ...


class SupportDecision(Protocol):
    @property
    def probability(self) -> float: ...

    @property
    def confidence(self) -> float: ...


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


def _deadline_handlers(deadline: float):
    import http.client
    import socket
    import urllib.request
    from engraphis.hosted_client import PinnedHTTPSConnection, PinnedHTTPSHandler

    def connect_socket(address, timeout=None, source_address=None):
        # socket.create_connection renews its timeout for each resolved address.
        # Share the request budget across direct, loopback and proxy dial retries.
        _remaining_time(deadline)
        host, port = address
        candidates = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
        last_error = None
        for family, kind, protocol, _, target in candidates:
            remaining = _remaining_time(deadline)
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
            self._create_connection = connect_socket

        def send(self, data):
            send_with_deadline(self, super().send, data)

    class DeadlineHTTPSConnection(PinnedHTTPSConnection):
        response_class = DeadlineResponse

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._create_connection = connect_socket

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
        raise ValueError("decision response exceeds the size limit")
    return bytes(data)


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


@dataclass(frozen=True)
class SimpleChoiceDecision:
    selected: str
    confidence: float


@dataclass(frozen=True)
class SimpleSupportDecision:
    probability: float
    confidence: float


@dataclass
class CloudDecisionBatch:
    is_fallback: bool
    choices: Dict[str, SimpleChoiceDecision]
    nouls: Dict[str, SimpleSupportDecision]

    def get_choice(self, question_id: str) -> Optional[ChoiceDecision]:
        return self.choices.get(question_id)

    def get_noul(self, question_id: str) -> Optional[SupportDecision]:
        return self.nouls.get(question_id)


class EngraphisCloudDecisionClient:
    """Experimental transport for an explicitly selected Cloud decision endpoint.

    Construction never performs network I/O. Calling evaluate authorizes a remote
    request; use JevDecisionBackend for offline and per-call consent checks.
    The timeout bounds socket operations and total HTTP response time. System
    DNS resolution itself cannot be interrupted by urllib.
    """

    def __init__(
        self,
        *,
        control_url: Optional[str] = None,
        token: Optional[str] = None,
        timeout_s: float = 2.0,
    ) -> None:
        self.control_url = (control_url if control_url is not None else os.environ.get(
            "ENGRAPHIS_CLOUD_CONTROL_URL", "https://api.engraphis.com",
        )).rstrip("/")
        self.token = token if token is not None else os.environ.get("ENGRAPHIS_CLOUD_ACCESS_TOKEN", "")
        if (type(timeout_s) not in (int, float) or not math.isfinite(timeout_s)
                or not 0 < timeout_s <= 30):
            raise ValueError("decision timeout must be finite and within (0, 30] seconds")
        self.timeout_s = timeout_s

    @property
    def is_configured(self) -> bool:
        return bool(self.control_url and 0 < len(self.token) <= 8192
                    and all(33 <= ord(char) <= 126 for char in self.token))

    @property
    def allow_fallback(self) -> bool:
        return False

    def evaluate(
        self, state: str, questions: Sequence[DecisionQuestion], *, model: str,
    ) -> DecisionBatch:
        import json
        import urllib.request
        from engraphis.hosted_client import build_pinned_https_opener, validate_cloud_base_url

        if not self.is_configured:
            raise ValueError("decision client is not configured")
        if len(state) > MAX_STATE_CHARS:
            raise ValueError("decision state exceeds the size limit")
        if not questions or len({q.id for q in questions}) != len(questions):
            raise ValueError("decision questions must be nonempty and have unique IDs")

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        payload = {
            "model": model,
            "state": state,
            "questions": [q.to_dict() for q in questions],
        }
        deadline = time.monotonic() + self.timeout_s
        url = validate_cloud_base_url(self.control_url) + "/v1/jev/decide"
        data = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.token}",
            "User-Agent": "engraphis-cloud-decision/1.0",
        }
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with build_pinned_https_opener(NoRedirect(), *_deadline_handlers(deadline)).open(
            req, timeout=_remaining_time(deadline),
        ) as resp:
            raw = _read_response(resp, deadline)
        body = json.loads(raw.decode("utf-8"))
        if not isinstance(body, dict) or body.get("is_fallback") is not False:
            return CloudDecisionBatch(is_fallback=True, choices={}, nouls={})
        raw_decisions = body.get("decisions")
        if not isinstance(raw_decisions, dict):
            raise ValueError("invalid decision response")
        choices: Dict[str, SimpleChoiceDecision] = {}
        nouls: Dict[str, SimpleSupportDecision] = {}
        for question in questions:
            val = raw_decisions.get(question.id)
            if not isinstance(val, dict) or val.get("type") != question.kind:
                continue
            conf = val.get("confidence")
            if not isinstance(conf, (int, float)) or not _probability(conf):
                continue
            if question.kind == "choice":
                selected = val.get("selected")
                if isinstance(selected, str) and selected in question.options:
                    choices[question.id] = SimpleChoiceDecision(selected=selected, confidence=conf)
            elif question.kind == "noul":
                probability = val.get("probability")
                if isinstance(probability, (int, float)) and _probability(probability):
                    nouls[question.id] = SimpleSupportDecision(probability=probability, confidence=conf)
        return CloudDecisionBatch(is_fallback=False, choices=choices, nouls=nouls)


def create_cloud_decision_client(
    *,
    control_url: Optional[str] = None,
    token: Optional[str] = None,
    timeout_s: float = 2.0,
) -> EngraphisCloudDecisionClient:
    """Create an experimental client; service availability is separately verified."""
    return EngraphisCloudDecisionClient(control_url=control_url, token=token, timeout_s=timeout_s)

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
