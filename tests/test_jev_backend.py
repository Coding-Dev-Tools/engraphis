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
    mock_payload = b'{"is_fallback":false,"decisions":{"q1":{"type":"choice","selected":"reinforces","confidence":0.95}}}'
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
    adapter = cloud_backend(monkeypatch, {"is_fallback": False, "decisions": {"has_support": decision}})
    assert adapter.verify_grounded_support("database?", "Postgres", allow_remote=True) == (False, 0.0)


@pytest.mark.parametrize("payload", [
    [], None, {}, {"decisions": []}, {"decisions": {"has_support": []}},
    {"decisions": {"has_support": {"type": "noul", "probability": 0.9}}},
    {"decisions": {"has_support": {"type": "noul", "confidence": 0.9}}},
    {"decisions": {"has_support": {"type": "choice", "probability": 0.9, "confidence": 0.9}}},
    b"not json", pytest.param(b"x" * (MAX_RESPONSE_BYTES + 1), id="oversized"),
])
def test_cloud_invalid_or_oversized_responses_defer(monkeypatch, payload):
    if isinstance(payload, dict):
        payload = {"is_fallback": False, **payload}
    adapter = cloud_backend(monkeypatch, payload)
    assert adapter.verify_grounded_support("database?", "Postgres", allow_remote=True) == (False, 0.0)


@pytest.mark.parametrize("flag_fields", [
    pytest.param({}, id="missing"), {"is_fallback": True}, {"is_fallback": "false"},
    {"is_fallback": None}, {"is_fallback": 0},
])
def test_cloud_fallbacks_and_malformed_flags_defer(monkeypatch, flag_fields):
    adapter = cloud_backend(monkeypatch, {**flag_fields, "decisions": {
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


@pytest.mark.parametrize("url", [
    "http://localhost:9000", "http://127.0.0.1:9000", "http://[::1]:9000",
    "https://localhost:9000", "https://127.0.0.1:9000", "https://[::1]:9000",
    "https://api.engraphis.com",
])
def test_cloud_loopback_bypasses_ambient_proxies(monkeypatch, url):
    import urllib.request
    from urllib.parse import urlsplit
    from urllib.response import addinfourl

    monkeypatch.setattr(urllib.request, "getproxies", lambda: {
        "http": "http://recording-proxy.example:8080",
        "https": "http://recording-proxy.example:8080",
    })
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: False)
    routes = []

    def capture_route(self, connection_class, req, **kwargs):
        routes.append((req.host, req._tunnel_host, req.get_header("Authorization")))
        response = addinfourl(io.BytesIO(b'{"is_fallback":false,"decisions":{"has_support":'
                                        b'{"type":"noul","probability":0.9,"confidence":0.95}}}'),
                             {}, req.full_url, 200)
        response.msg = "OK"
        return response

    monkeypatch.setattr(urllib.request.AbstractHTTPHandler, "do_open", capture_route)
    client = create_cloud_decision_client(control_url=url, token="local-test-token")
    assert backend(client).verify_grounded_support("database?", "Postgres", allow_remote=True) == (True, 0.9)
    expected = ("recording-proxy.example:8080", "api.engraphis.com") if url.endswith("engraphis.com") else (
        urlsplit(url).netloc, None,
    )
    assert routes == [(*expected, "Bearer local-test-token")]


def test_cloud_loopback_token_never_reaches_recording_proxy(monkeypatch):
    import socket
    import threading
    import urllib.request
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    requests = []

    def handler_for(destination):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                requests.append((destination, self.headers.get("Authorization")))
                payload = b'{"is_fallback":false,"decisions":{"has_support":'
                payload += b'{"type":"noul","probability":0.9,"confidence":0.95}}}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        return Handler

    endpoint = ThreadingHTTPServer(("127.0.0.1", 0), handler_for("endpoint"))
    proxy = ThreadingHTTPServer(("127.0.0.1", 0), handler_for("proxy"))
    threads = [threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
               for server in (endpoint, proxy)]
    for thread in threads:
        thread.start()
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {"http": f"http://127.0.0.1:{proxy.server_port}"})
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: False)

    def resolve(host, port, *args):
        assert host == "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, port))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    try:
        client = create_cloud_decision_client(control_url=f"http://127.0.0.1:{endpoint.server_port}", token="synthetic-test-token")
        assert backend(client).verify_grounded_support("database?", "Postgres", allow_remote=True) == (True, 0.9)
        assert requests == [("endpoint", "Bearer synthetic-test-token")]
    finally:
        for server in (endpoint, proxy):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)
    assert all(not thread.is_alive() for thread in threads)


@pytest.mark.parametrize("transport", ["http", "https"])
@pytest.mark.parametrize("hosts", [["93.184.216.34"], ["127.0.0.1", "93.184.216.34"]])
def test_cloud_loopback_resolution_cannot_dial_external_addresses(monkeypatch, transport, hosts):
    import socket
    import time

    connection = deadline_connection(monkeypatch, time.monotonic() + 2, transport, loopback_only=True)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, 9000)) for host in hosts
    ])
    dials = []
    closed = []

    def connect(target):
        dials.append(target)
        raise OSError("loopback endpoint unavailable")

    monkeypatch.setattr(socket, "socket", lambda *args: SimpleNamespace(
        settimeout=lambda value: None, connect=connect, close=lambda: closed.append(True),
    ))
    with pytest.raises(ValueError, match="must connect to loopback"):
        connection._create_connection(("localhost", 9000))
    expected = [("127.0.0.1", 9000)] if len(hosts) == 2 else []
    assert dials == expected
    assert len(closed) == len(expected)


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


def deadline_connection(monkeypatch, deadline, transport="https", *, loopback_only=False):
    import urllib.request
    from engraphis.backends.jev_decision import _deadline_handlers

    selected = []
    monkeypatch.setattr(urllib.request.AbstractHTTPHandler, "do_open",
                        lambda self, connection_class, req, **kwargs: selected.append(connection_class))
    http_handler, https_handler = _deadline_handlers(deadline, loopback_only=loopback_only)
    if transport == "http":
        http_handler.http_open(None)
    else:
        https_handler.https_open(None)
    return selected[0]("localhost" if transport == "http" else "cloud.example", timeout=0.05)


@pytest.mark.parametrize("transport", ["http", "https", "https_proxy"])
def test_cloud_dial_retries_and_tls_share_short_budget(monkeypatch, transport):
    import socket
    import engraphis.backends.jev_decision as module

    clock = [10.0]
    connection = deadline_connection(monkeypatch, 10.05, transport)
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    hosts = ["127.0.0.1", "127.0.0.2"] if transport == "http" else ["93.184.216.34", "93.184.216.35"]
    addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, 443)) for host in hosts]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args: addresses)
    monkeypatch.setattr("engraphis.hosted_client._validated_addresses", lambda host: ["93.184.216.34"])
    sockets = []

    class DialSocket:
        def __init__(self, *args):
            self.timeouts = []
            self.closed = False
            sockets.append(self)

        def settimeout(self, value):
            self.timeouts.append(value)

        def setsockopt(self, *args):
            pass

        def connect(self, address):
            clock[0] += 0.03 if len(sockets) == 1 else 0.01
            if len(sockets) == 1:
                raise OSError("first address unavailable")

        def close(self):
            self.closed = True

    monkeypatch.setattr(socket, "socket", DialSocket)
    if transport != "http":
        assert connection._attempt_timeout(99) == pytest.approx(0.05)
        assert connection._connect_deadline() == 10.05

        if transport == "https_proxy":
            connection._tunnel_host = "93.184.216.34"
            connection._tunnel_port = 443

            def tunnel():
                clock[0] += 0.005
                connection.sock.settimeout(module._remaining_time(10.05))

            connection._tunnel = tunnel

        def tls(sock, server_hostname):
            assert server_hostname == "cloud.example"
            assert sock.timeouts[-1] == pytest.approx(0.005 if transport == "https_proxy" else 0.01)
            clock[0] += 0.02
            return sock

        connection._context = SimpleNamespace(wrap_socket=tls)
        with pytest.raises(TimeoutError):
            connection.send(b"must not send after TLS consumes the budget")
    else:
        connection.connect()
    assert len(sockets) == 2
    assert sockets[0].closed
    assert sockets[0].timeouts[0] == pytest.approx(0.05)
    expected = [0.02, 0.01, 0.005] if transport == "https_proxy" else [0.02, 0.01]
    assert sockets[1].timeouts == pytest.approx(expected)
    connection.close()
    assert sockets[1].closed


@pytest.mark.parametrize("expire_during", ["dns", "connect"])
def test_cloud_expired_resolution_or_dial_cannot_proceed(monkeypatch, expire_during):
    import socket
    import engraphis.backends.jev_decision as module

    clock = [10.0]
    connection = deadline_connection(monkeypatch, 10.05)
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])

    def resolve(*args):
        if expire_during == "dns":
            clock[0] += 0.1
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    sockets = []

    class DialSocket:
        def __init__(self, *args):
            self.closed = False
            sockets.append(self)

        def settimeout(self, value):
            pass

        def connect(self, address):
            clock[0] += 0.1

        def close(self):
            self.closed = True

    monkeypatch.setattr(socket, "socket", DialSocket)
    with pytest.raises(TimeoutError):
        connection._create_connection(("proxy.example", 443), 0.5)
    assert len(sockets) == (0 if expire_during == "dns" else 1)
    assert all(sock.closed for sock in sockets)


def test_cloud_dial_skips_unsupported_address_family(monkeypatch):
    import socket
    import time

    connection = deadline_connection(monkeypatch, time.monotonic() + 2)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args: [
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 443, 0, 0)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
    ])
    families = []
    connected = []
    usable = SimpleNamespace(settimeout=lambda value: None, connect=connected.append)

    def create(family, kind, protocol):
        families.append(family)
        if family == socket.AF_INET6:
            raise OSError("IPv6 disabled")
        return usable

    monkeypatch.setattr(socket, "socket", create)
    assert connection._create_connection(("localhost", 443)) is usable
    assert families == [socket.AF_INET6, socket.AF_INET]
    assert connected == [("127.0.0.1", 443)]


@pytest.mark.parametrize("transport", ["http", "https"])
def test_cloud_deadline_interrupts_blocked_request_send(monkeypatch, transport):
    import http.client
    import itertools
    import socket
    import time

    started = time.monotonic()
    connection = deadline_connection(monkeypatch, started + 0.1, transport)
    connection.auto_open = False
    with pytest.raises(http.client.NotConnected):
        connection.send(b"disabled")
    reader, writer = socket.socketpair()
    writer.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
    connection.sock = writer
    try:
        with pytest.raises((TimeoutError, OSError)):
            connection.send(itertools.repeat(b"x" * 65536))
        assert time.monotonic() - started < 1.5
    finally:
        connection.close()
        reader.close()
