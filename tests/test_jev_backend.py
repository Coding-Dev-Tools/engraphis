"""The experimental adapter is offline-safe and never trusts fallback decisions."""
from __future__ import annotations

import io
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from engraphis.backends.jev_decision import (
    MAX_RESPONSE_BYTES, MAX_STATE_CHARS, DecisionQuestion, JevDecisionBackend,
    create_cloud_decision_client, get_decision_backend,
)
from engraphis.core.interfaces import MemoryRecord, MemoryType, Scope


class FakeClient:
    is_configured = True
    allow_fallback = False

    def __init__(self, *, fallback=False, confidence=0.9, probability=0.9, verdict="reinforces"):
        self.calls = []
        self.batch = SimpleNamespace(
            is_fallback=fallback,
            get_choice=lambda _: SimpleNamespace(selected=verdict, confidence=confidence),
            get_noul=lambda _: SimpleNamespace(probability=probability, confidence=confidence),
        )

    def evaluate(self, state, questions, *, model):
        self.calls.append((state, [question.to_dict() for question in questions], model))
        return self.batch


def memory():
    return MemoryRecord(
        id="mem_1", scope=Scope.WORKSPACE, workspace_id="ws_1", mtype=MemoryType.SEMANTIC,
        title="Database", content="We use Postgres for primary user data storage.",
    )


def backend(client, **kwargs):
    return JevDecisionBackend(client=client, model="test-model-1.0", **kwargs)


def test_import_never_discovers_optional_sdk_or_sibling_checkout():
    script = """
import importlib.abc, sys
class DenySdk(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname == 'jev_decision':
            raise AssertionError('optional SDK import attempted')
sys.meta_path.insert(0, DenySdk())
before = list(sys.path)
from engraphis.backends.jev_decision import JevDecisionBackend
assert sys.path == before
assert not JevDecisionBackend().is_available
assert JevDecisionBackend().verify_grounded_support('database?', '') == (False, 0.0)
"""
    subprocess.run([sys.executable, "-c", script], check=True, capture_output=True, text=True)


def test_factory_requires_an_explicit_client_and_model(monkeypatch):
    monkeypatch.setenv("ENGRAPHIS_DECISION_BACKEND", "jev")
    assert get_decision_backend() is None
    assert get_decision_backend(client=FakeClient()) is None
    assert get_decision_backend(client=FakeClient(), model="jev-latest") is None
    assert get_decision_backend(client=FakeClient(), model="jev-latest ") is None
    assert get_decision_backend(client=FakeClient(), model="JEV-LATEST") is None
    assert get_decision_backend("none", client=FakeClient(), model="test-model-1.0") is None
    result = get_decision_backend(client=FakeClient(), model="test-model-1.0")
    assert result is not None and result.identity == "engraphis.backend.jev.v1"


def test_offline_and_unapproved_requests_never_call_client():
    client = FakeClient()
    for adapter, approved in ((backend(client), False), (backend(client, offline_mode=True), True)):
        assert adapter.verify_grounded_support("database?", "Postgres", allow_remote=approved) == (False, 0.0)
        assert adapter.classify_contradiction("Use SQLite", memory(), allow_remote=approved) == ("orthogonal", 0.0)
    assert client.calls == []


def test_explicitly_authorized_valid_response_is_advisory():
    client = FakeClient()
    adapter = backend(client)
    assert adapter.verify_grounded_support("database?", "Postgres", allow_remote=True) == (True, 0.9)
    assert adapter.classify_contradiction("Use Postgres", memory(), allow_remote=True) == ("reinforces", 0.9)
    assert all(call[2] == "test-model-1.0" for call in client.calls)
    assert client.calls[1][1][0]["options"] == ["contradicts_and_supersedes", "reinforces", "orthogonal"]


@pytest.mark.parametrize("field,value", [
    ("confidence", float("nan")), ("confidence", float("inf")), ("confidence", True),
    ("confidence", -0.1), ("confidence", 1.1), ("confidence", 0.5),
    ("probability", float("nan")), ("probability", float("inf")), ("probability", True),
    ("probability", -0.1), ("probability", 1.1), ("probability", 0.5),
])
def test_invalid_or_uncertain_support_never_certifies_evidence(field, value):
    adapter = backend(FakeClient(**{field: value}))
    assert adapter.verify_grounded_support("database?", "Postgres", allow_remote=True) == (False, 0.0)
    if field == "confidence":
        assert adapter.classify_contradiction("Use SQLite", memory(), allow_remote=True) == ("orthogonal", 0.0)


def test_fallbacks_and_unknown_verdicts_cannot_authorize_a_decision():
    for client in (FakeClient(fallback=True), FakeClient(verdict="delete_everything")):
        assert backend(client).classify_contradiction("Use SQLite", memory(), allow_remote=True) == ("orthogonal", 0.0)
    assert backend(FakeClient(fallback=True)).verify_grounded_support(
        "database?", "Bananas are yellow.", allow_remote=True,
    ) == (False, 0.0)
    client = FakeClient()
    client.allow_fallback = True
    assert not backend(client).is_available


def test_missing_malformed_and_failed_responses_defer_without_logging_payloads(caplog):
    for batch in (None, SimpleNamespace(is_fallback=False)):
        client = FakeClient()
        client.batch = batch
        assert backend(client).verify_grounded_support("database?", "Postgres", allow_remote=True) == (False, 0.0)
    class FailingClient(FakeClient):
        def evaluate(self, *args, **kwargs):
            raise RuntimeError("private request content")
    adapter = backend(FailingClient())
    assert adapter.verify_grounded_support("database?", "Postgres", allow_remote=True) == (False, 0.0)
    assert adapter.classify_contradiction("Use SQLite", memory(), allow_remote=True) == ("orthogonal", 0.0)
    assert "private request content" not in caplog.text


@pytest.mark.parametrize("query,evidence", [("", "Postgres"), ("database?", " "), ("database?", "x" * MAX_STATE_CHARS)])
def test_empty_and_oversized_inputs_do_not_leave_the_process(query, evidence):
    client = FakeClient()
    assert backend(client).verify_grounded_support(query, evidence, allow_remote=True) == (False, 0.0)
    assert client.calls == []


def test_cloud_decision_client_configuration(monkeypatch):
    # Unconfigured
    monkeypatch.delenv("ENGRAPHIS_CLOUD_ACCESS_TOKEN", raising=False)
    client = create_cloud_decision_client(token="")
    assert client.is_configured is False
    assert client.allow_fallback is False

    # Configured
    client_configured = create_cloud_decision_client(token="test-token", control_url="https://api.engraphis.com")
    assert client_configured.is_configured is True
    assert client_configured.allow_fallback is False

    # Mock evaluate response
    mock_payload = b'{"decisions": {"q1": {"type": "choice", "selected": "reinforces", "confidence": 0.95}}}'
    mock_resp = io.BytesIO(mock_payload)
    mock_resp.status = 200

    monkeypatch.setattr("engraphis.hosted_client.build_pinned_https_opener",
                        lambda *handlers: SimpleNamespace(open=lambda req, timeout: mock_resp))

    q = DecisionQuestion("q1", "prompt", "choice", ("reinforces", "orthogonal"))
    batch = client_configured.evaluate("test state", [q], model="test-model-1.0")
    assert batch.is_fallback is False
    assert batch.get_choice("q1").selected == "reinforces"
    assert batch.get_choice("q1").confidence == 0.95


def cloud_backend(monkeypatch, payload):
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    monkeypatch.setattr("engraphis.hosted_client.build_pinned_https_opener",
                        lambda *handlers: SimpleNamespace(open=lambda req, timeout: io.BytesIO(raw)))
    return backend(create_cloud_decision_client(token="test-token"))


@pytest.mark.parametrize("field,value", [
    ("confidence", None), ("confidence", True), ("confidence", "0.9"),
    ("confidence", float("nan")), ("confidence", float("inf")),
    ("confidence", -0.1), ("confidence", 1.1),
    ("probability", None), ("probability", True), ("probability", "0.9"),
    ("probability", float("nan")), ("probability", float("inf")),
    ("probability", -0.1), ("probability", 1.1),
])
def test_cloud_malformed_numeric_fields_never_certify(monkeypatch, field, value):
    decision = {"type": "noul", "probability": 0.9, "confidence": 0.9, field: value}
    adapter = cloud_backend(monkeypatch, {"decisions": {"has_support": decision}})
    assert adapter.verify_grounded_support("database?", "Postgres", allow_remote=True) == (False, 0.0)


@pytest.mark.parametrize("payload", [
    [], None, {}, {"decisions": []}, {"decisions": {"has_support": []}},
    {"decisions": {"has_support": {"type": "noul", "probability": 0.9}}},
    {"decisions": {"has_support": {"type": "noul", "confidence": 0.9}}},
    {"decisions": {"has_support": {"type": "choice", "probability": 0.9, "confidence": 0.9}}},
    b"not json", pytest.param(b"x" * (MAX_RESPONSE_BYTES + 1), id="oversized"),
])
def test_cloud_invalid_or_oversized_responses_defer(monkeypatch, payload):
    adapter = cloud_backend(monkeypatch, payload)
    assert adapter.verify_grounded_support("database?", "Postgres", allow_remote=True) == (False, 0.0)


@pytest.mark.parametrize("fallback", [True, "false", None, 0])
def test_cloud_fallbacks_and_malformed_flags_defer(monkeypatch, fallback):
    adapter = cloud_backend(monkeypatch, {"is_fallback": fallback, "decisions": {
        "has_support": {"type": "noul", "probability": 0.9, "confidence": 0.9},
        "verdict": {"type": "choice", "selected": "reinforces", "confidence": 0.9},
    }})
    assert adapter.verify_grounded_support("database?", "Postgres", allow_remote=True) == (False, 0.0)
    assert adapter.classify_contradiction("Use Postgres", memory(), allow_remote=True) == ("orthogonal", 0.0)


def test_cloud_valid_response_and_bounded_transport(monkeypatch):
    import urllib.request

    seen = []
    class Response(io.BytesIO):
        def read1(self, size=-1):
            assert 0 < size <= 4096
            return super().read1(size)

    def opener(handler, *deadline_handlers):
        def open_request(req, timeout):
            assert req.full_url == "https://api.engraphis.com/v1/jev/decide"
            assert req.get_header("Authorization") == "Bearer test-token"
            assert 0 < timeout <= 2.0
            assert json.loads(req.data)["model"] == "test-model-1.0"
            assert handler.redirect_request(req, None, 302, "Found", {},
                                            "https://unrelated.example/") is None
            seen.append(req)
            return Response(b'{"is_fallback":false,"decisions":{"has_support":'
                            b'{"type":"noul","probability":0.9,"confidence":0.95}}}')
        assert isinstance(handler, urllib.request.HTTPRedirectHandler)
        assert len(deadline_handlers) == 2
        return SimpleNamespace(open=open_request)

    monkeypatch.setattr("engraphis.hosted_client.build_pinned_https_opener", opener)
    adapter = backend(create_cloud_decision_client(token="test-token"))
    assert adapter.verify_grounded_support("database?", "Postgres", allow_remote=True) == (True, 0.9)
    assert len(seen) == 1


@pytest.mark.parametrize("url", [
    "http://api.engraphis.com", "https://192.168.1.1", "https://user:secret@example.com",
    "https://example.com?other=1", "https://example.com#other", "file:///tmp/decision",
])
def test_cloud_unsafe_destinations_never_receive_credentials(monkeypatch, url):
    def unexpected(*args, **kwargs):
        pytest.fail("unsafe destination reached transport")
    monkeypatch.setattr("engraphis.hosted_client.build_pinned_https_opener", unexpected)
    adapter = backend(create_cloud_decision_client(token="test-token", control_url=url))
    assert adapter.verify_grounded_support("database?", "Postgres", allow_remote=True) == (False, 0.0)


def test_cloud_disabled_calls_do_not_resolve_or_send(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("disabled cloud client performed network work")
    monkeypatch.setattr("engraphis.hosted_client.validate_cloud_base_url", unexpected)
    monkeypatch.setattr("engraphis.hosted_client.build_pinned_https_opener", unexpected)
    client = create_cloud_decision_client(token="test-token")
    assert backend(client).verify_grounded_support("database?", "Postgres") == (False, 0.0)
    assert backend(client, offline_mode=True).verify_grounded_support(
        "database?", "Postgres", allow_remote=True,
    ) == (False, 0.0)


def test_cloud_explicit_empty_configuration_does_not_load_ambient_credentials(monkeypatch):
    monkeypatch.setenv("ENGRAPHIS_CLOUD_ACCESS_TOKEN", "ambient-token")
    assert not create_cloud_decision_client(token="").is_configured
    assert not create_cloud_decision_client(control_url="").is_configured


@pytest.mark.parametrize("timeout", [None, True, 0, -1, float("nan"), float("inf"), 31])
def test_cloud_timeout_is_bounded(timeout):
    with pytest.raises(ValueError, match="timeout"):
        create_cloud_decision_client(timeout_s=timeout)


def test_cloud_slow_drip_response_obeys_one_deadline(monkeypatch):
    import engraphis.backends.jev_decision as module

    clock = [10.0]
    timeouts = []
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])

    class SlowResponse(io.BytesIO):
        fp = SimpleNamespace(raw=SimpleNamespace(_sock=SimpleNamespace(
            settimeout=lambda value: timeouts.append(value),
        )))

        def read1(self, size=-1):
            # Every receive makes progress inside the original two-second socket
            # timeout; an unbounded read() would keep waiting for the whole body.
            clock[0] += 0.75
            return b" "

    response = SlowResponse()
    monkeypatch.setattr("engraphis.hosted_client.build_pinned_https_opener",
                        lambda *handlers: SimpleNamespace(open=lambda req, timeout: response))
    adapter = backend(create_cloud_decision_client(token="test-token", timeout_s=2))
    assert adapter.verify_grounded_support("database?", "Postgres", allow_remote=True) == (False, 0.0)
    assert timeouts == [2.0, 1.25, 0.5]
    assert response.closed


def test_cloud_validation_time_consumes_the_request_budget(monkeypatch):
    import engraphis.backends.jev_decision as module

    clock = [10.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])

    def delayed_validation(url):
        clock[0] += 3.0
        return url

    def unexpected(*args, **kwargs):
        pytest.fail("expired validation budget reached network transport")

    monkeypatch.setattr("engraphis.hosted_client.validate_cloud_base_url", delayed_validation)
    monkeypatch.setattr("engraphis.hosted_client.build_pinned_https_opener",
                        lambda *handlers: SimpleNamespace(open=unexpected))
    adapter = backend(create_cloud_decision_client(token="test-token", timeout_s=2))
    assert adapter.verify_grounded_support("database?", "Postgres", allow_remote=True) == (False, 0.0)


def test_cloud_deadline_interrupts_slow_chunk_framing():
    import http.client
    import socket
    import threading
    import time
    from engraphis.backends.jev_decision import _read_response

    reader, writer = socket.socketpair()
    stopped = threading.Event()
    writer.sendall(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
    response = http.client.HTTPResponse(reader)
    response.begin()

    def drip_chunk_header():
        try:
            for _ in range(200):
                if stopped.wait(0.01):
                    return
                writer.sendall(b"0")
            writer.shutdown(socket.SHUT_WR)
        except OSError:
            pass

    producer = threading.Thread(target=drip_chunk_header, daemon=True)
    producer.start()
    started = time.monotonic()
    try:
        with pytest.raises((TimeoutError, OSError, http.client.HTTPException)):
            _read_response(response, started + 0.1)
        assert time.monotonic() - started < 1.5
    finally:
        stopped.set()
        response.close()
        reader.close()
        writer.close()
        producer.join(timeout=2)
    assert not producer.is_alive()


@pytest.mark.parametrize("header_prefix", [b"HTTP/1.1 ", b"HTTP/1.1 200 OK\r\nX-Slow: "])
@pytest.mark.parametrize("transport", ["http", "https", "https_proxy"])
def test_cloud_deadline_interrupts_status_and_headers(monkeypatch, header_prefix, transport):
    import http.client
    import socket
    import threading
    import time
    import urllib.request
    from engraphis.backends.jev_decision import _deadline_handlers

    reader, writer = socket.socketpair()
    stopped = threading.Event()
    writer.sendall(header_prefix)

    def drip_header():
        try:
            for _ in range(200):
                if stopped.wait(0.01):
                    return
                writer.sendall(b"x")
            writer.shutdown(socket.SHUT_WR)
        except OSError:
            pass

    producer = threading.Thread(target=drip_header, daemon=True)
    producer.start()
    started = time.monotonic()
    response = None
    try:
        http_handler, https_handler = _deadline_handlers(started + 0.1)
        # Exercise the actual HTTP and pinned HTTPS response classes selected by
        # the handlers, without needing network access or a TLS certificate.
        selected = []
        def inspect_connection(self, connection_class, req, **kwargs):
            selected.append(connection_class)
        monkeypatch.setattr(urllib.request.AbstractHTTPHandler, "do_open", inspect_connection)
        if transport == "http":
            http_handler.http_open(None)
        else:
            https_handler.https_open(None)
        if transport == "https_proxy":
            connection = selected[0]("proxy.example")
            connection.sock = reader
            connection._tunnel_host = "target.example"
            connection._tunnel_port = 443
            with pytest.raises((TimeoutError, OSError, http.client.HTTPException)):
                connection._tunnel()
        else:
            response = selected[0].response_class(reader)
            with pytest.raises((TimeoutError, OSError, http.client.HTTPException)):
                response.begin()
        assert time.monotonic() - started < 1.5
        assert https_handler.handler_order < 500
    finally:
        stopped.set()
        if response is not None:
            response.close()
        reader.close()
        writer.close()
        producer.join(timeout=2)
    assert not producer.is_alive()
