"""One managed-decision budget, including credential acquisition and rotation.

All credentials and responses are synthetic; real sockets target loopback only.
"""
from __future__ import annotations

import http.client
import io
import json
import multiprocessing
import socket
import threading
import time
import urllib.error
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest

from engraphis import cloud_session, hosted_client, http_deadline
from engraphis.backends import jev_transport
from engraphis.backends.jev_decision import DecisionQuestion


def _hold_process_lock(state_dir, ready, release):
    # Spawned processes must not discover any developer's private session.
    import os
    os.environ["ENGRAPHIS_STATE_DIR"] = state_dir
    with cloud_session._refresh_lock():
        ready.set()
        if not release.wait(15):
            raise RuntimeError("synthetic lock test did not release its helper")


@pytest.fixture
def saved_session(monkeypatch, tmp_path):
    monkeypatch.setenv("ENGRAPHIS_STATE_DIR", str(tmp_path))
    for name in ("ENGRAPHIS_CLOUD_ACCESS_TOKEN", "ENGRAPHIS_CLOUD_REFRESH_CREDENTIAL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(cloud_session, "validate_cloud_base_url", lambda value: value)
    cloud_session._save({
        "schema": "engraphis-cloud-session/v1",
        "control_url": "https://control.example.invalid",
        "organization_id": "org_synthetic",
        "refresh_credential": "synthetic-unspent",
        "token_subject": "member",
    })
    return tmp_path


def _evaluate(timeout=0.1):
    return jev_transport.create_cloud_decision_client(timeout_s=timeout).evaluate(
        "Synthetic local evidence.", [DecisionQuestion("q", "Is this supported?", "noul")],
        model=jev_transport.MODEL, allow_remote=True, data_classification="public",
    )


def _no_network(*args, **kwargs):
    pytest.fail("an expired or contended request reached the network")


def _rotation():
    return {"access_token": "synthetic-access", "refresh_credential": "synthetic-rotated",
            "organization_id": "org_synthetic", "token_subject": "member"}


def _decision():
    return {"model": jev_transport.MODEL, "is_fallback": False, "decisions": {"q": {
        "type": "noul", "probability": 0.9, "confidence": 0.8,
        "confidence_source": "derived_decisiveness",
    }}}


@pytest.mark.parametrize("lock_kind", ["thread", "process"])
def test_managed_deadline_bounds_contended_locks_without_spending_refresh(
    monkeypatch, saved_session, lock_kind,
):
    original = cloud_session._load()
    monkeypatch.setattr(cloud_session, "_post_refresh", _no_network)
    monkeypatch.setattr(jev_transport, "_post_json", _no_network)
    if lock_kind == "process":
        context = multiprocessing.get_context("spawn")
        ready, release = context.Event(), context.Event()
        holder = context.Process(target=_hold_process_lock,
                                 args=(str(saved_session), ready, release))
    else:
        ready, release = threading.Event(), threading.Event()

        def hold():
            with cloud_session._refresh_lock():
                ready.set()
                release.wait(15)

        holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    try:
        assert ready.wait(10), "helper did not acquire the actual lock"
        started = time.monotonic()
        with pytest.raises(jev_transport.DecisionClientError, match="^remote_timeout$"):
            _evaluate()
        assert time.monotonic() - started < 1.5
        assert cloud_session._load() == original
        assert not cloud_session._refresh_is_unusable(original, "synthetic-unspent")
    finally:
        release.set()
        holder.join(timeout=10)
        if lock_kind == "process" and holder.is_alive():
            holder.terminate()
            holder.join(timeout=5)
    assert not holder.is_alive()
    if lock_kind == "process":
        assert holder.exitcode == 0
    # A cancelled waiter must leave both locks available to the next caller.
    with cloud_session._refresh_lock(deadline=time.monotonic() + 1):
        pass


def test_refresh_and_decision_receive_the_same_remaining_budget(monkeypatch, saved_session):
    clock = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    calls = []

    def refresh(control, credential, workspace, subject, *, deadline):
        calls.append(("refresh", deadline))
        clock[0] += 3
        return _rotation()

    def open_request(request, timeout):
        calls.append(("decision", timeout))
        response = io.BytesIO(json.dumps(_decision()).encode())
        response.status = 200
        response.headers = {"Content-Type": "application/json"}
        return response

    monkeypatch.setattr(cloud_session, "_post_refresh", refresh)
    monkeypatch.setattr(hosted_client, "validate_cloud_base_url", lambda value: value)
    monkeypatch.setattr(hosted_client, "build_pinned_https_opener",
                        lambda *handlers: SimpleNamespace(open=open_request))
    assert _evaluate(5).get_noul("q").probability == 0.9
    assert calls == [("refresh", 105.0), ("decision", 2.0)]
    assert cloud_session._load()["refresh_credential"] == "synthetic-rotated"


@pytest.mark.parametrize("expiry_phase", ["refresh", "save"])
def test_completed_rotation_is_saved_before_reporting_timeout(
    monkeypatch, saved_session, expiry_phase,
):
    clock = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    save = cloud_session._save

    def refresh(*args, deadline):
        if expiry_phase == "refresh":
            clock[0] = deadline
        return _rotation()

    def slow_save(value):
        save(value)
        if expiry_phase == "save":
            clock[0] += 5

    monkeypatch.setattr(cloud_session, "_post_refresh", refresh)
    monkeypatch.setattr(cloud_session, "_save", slow_save)
    monkeypatch.setattr(jev_transport, "_post_json", _no_network)
    with pytest.raises(jev_transport.DecisionClientError, match="^remote_timeout$"):
        _evaluate(5)
    saved = cloud_session._load()
    assert saved["refresh_credential"] == "synthetic-rotated"
    assert not cloud_session._refresh_is_unusable(saved, "synthetic-rotated")
    assert "refresh_unusable" not in saved


def _http_body_at_deadline(monkeypatch, framing, *, incomplete=False, trailer=b"\r\n"):
    raw = json.dumps(_rotation()).encode()
    if framing == "length":
        headers = b"Content-Length: " + str(len(raw) + int(incomplete)).encode() + b"\r\n"
        body = raw
    elif framing == "chunked":
        headers = b"Transfer-Encoding: chunked\r\n"
        body = ("%x\r\n" % len(raw)).encode() + raw + b"\r\n0\r\n" + trailer
    else:
        headers = b"Connection: close\r\n"
        body = raw

    class SyntheticSocket:
        def makefile(self, *args, **kwargs):
            return io.BytesIO(b"HTTP/1.1 200 OK\r\n" + headers + b"\r\n" + body)

    clock = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    # Construct the production response parser without opening a real socket.
    handler = http_deadline.deadline_handlers(105.0)[0]
    monkeypatch.setattr(handler, "do_open",
                        lambda connection, request: connection.response_class(SyntheticSocket()))
    response = handler.http_open(urllib.request.Request("http://127.0.0.1/"))
    response.begin()
    read = response.read1
    calls = []

    def read_chunk(size=-1):
        chunk = read(min(size, 17))
        calls.append(chunk)
        if response.length == 0 or not chunk:
            clock[0] = 105.0
        return chunk

    monkeypatch.setattr(response, "read1", read_chunk)
    return response, calls, raw, clock


def _interrupt_after_eof(monkeypatch, response):
    interrupted = threading.Event()
    read = response.read1

    def read_then_interrupt(size=-1):
        chunk = read(size)
        if not chunk:
            interrupted.set()
        return chunk

    @contextmanager
    def watchdog(sock, deadline):
        yield interrupted

    monkeypatch.setattr(response, "read1", read_then_interrupt)
    monkeypatch.setattr(http_deadline, "socket_deadline", watchdog)


@pytest.mark.parametrize("trailer", [b"\r\n", b"\n", b"X-Test: complete\r\n\r\n"])
def test_completed_chunked_rotation_survives_watchdog_edge(monkeypatch, saved_session, trailer):
    response, _reads, _raw, _clock = _http_body_at_deadline(
        monkeypatch, "chunked", trailer=trailer,
    )
    _interrupt_after_eof(monkeypatch, response)
    monkeypatch.setattr(cloud_session, "build_pinned_https_opener",
                        lambda *handlers: SimpleNamespace(open=lambda *args, **kwargs: response))
    monkeypatch.setattr(jev_transport, "_post_json", _no_network)

    with pytest.raises(jev_transport.DecisionClientError, match="^remote_timeout$"):
        _evaluate(5)

    saved = cloud_session._load()
    assert saved["refresh_credential"] == "synthetic-rotated"
    assert "refresh_unusable" not in saved
    assert response.closed


@pytest.mark.parametrize("trailer", [b"", b"X-Test: partial", b"X-Test: complete\r\n"])
def test_unterminated_chunked_rotation_is_not_saved(monkeypatch, saved_session, trailer):
    response, _reads, _raw, _clock = _http_body_at_deadline(
        monkeypatch, "chunked", trailer=trailer,
    )
    monkeypatch.setattr(cloud_session, "build_pinned_https_opener",
                        lambda *handlers: SimpleNamespace(open=lambda *args, **kwargs: response))
    monkeypatch.setattr(jev_transport, "_post_json", _no_network)

    with pytest.raises(jev_transport.DecisionClientError, match="^remote_unavailable$"):
        _evaluate(5)

    saved = cloud_session._load()
    assert saved.get("refresh_credential") != "synthetic-rotated"
    assert cloud_session._refresh_is_unusable(saved, "synthetic-unspent")
    assert response.closed


def test_complete_chunked_watchdog_edge_keeps_ordinary_deadline(monkeypatch):
    response, _reads, _raw, _clock = _http_body_at_deadline(monkeypatch, "chunked")
    _interrupt_after_eof(monkeypatch, response)
    with response, pytest.raises(TimeoutError):
        jev_transport._read_response(response, 105.0)


def test_chunked_trailer_keeps_standard_line_limit(monkeypatch):
    response, _reads, _raw, _clock = _http_body_at_deadline(
        monkeypatch, "chunked", trailer=b"X-Test: " + b"a" * 65536 + b"\r\n\r\n",
    )
    with response, pytest.raises(http.client.LineTooLong):
        http_deadline.read_response(response, 105.0, max_bytes=4096, preserve_complete=True)


@pytest.mark.parametrize("framing", ["length", "close", "chunked"])
def test_completed_http_rotation_is_saved_at_body_deadline(monkeypatch, saved_session, framing):
    response, reads, raw, _clock = _http_body_at_deadline(monkeypatch, framing)
    monkeypatch.setattr(cloud_session, "build_pinned_https_opener",
                        lambda *handlers: SimpleNamespace(open=lambda *args, **kwargs: response))
    monkeypatch.setattr(jev_transport, "_post_json", _no_network)

    with pytest.raises(jev_transport.DecisionClientError, match="^remote_timeout$"):
        _evaluate(5)

    saved = cloud_session._load()
    assert saved["refresh_credential"] == "synthetic-rotated"
    assert not cloud_session._refresh_is_unusable(saved, "synthetic-rotated")
    assert "refresh_unusable" not in saved
    assert response.closed
    assert b"".join(reads) == raw
    assert (reads[-1] == b"") is (framing != "length")


@pytest.mark.parametrize("failure", ["truncated", "partial", "oversized"])
def test_unfinished_http_rotation_at_deadline_is_not_saved(monkeypatch, saved_session, failure):
    response, _reads, raw, clock = _http_body_at_deadline(
        monkeypatch, "length", incomplete=failure == "truncated",
    )
    if failure == "partial":
        read = response.read1

        def read_partial(size=-1):
            chunk = read(size)
            clock[0] = 105.0
            return chunk

        monkeypatch.setattr(response, "read1", read_partial)
    elif failure == "oversized":
        monkeypatch.setattr(cloud_session, "_MAX_RESPONSE_BYTES", len(raw) - 1)
    monkeypatch.setattr(cloud_session, "build_pinned_https_opener",
                        lambda *handlers: SimpleNamespace(open=lambda *args, **kwargs: response))
    monkeypatch.setattr(jev_transport, "_post_json", _no_network)

    with pytest.raises(jev_transport.DecisionClientError, match="^remote_timeout$"):
        _evaluate(5)

    saved = cloud_session._load()
    assert saved.get("refresh_credential") != "synthetic-rotated"
    assert cloud_session._refresh_is_unusable(saved, "synthetic-unspent")
    assert response.closed


@pytest.mark.parametrize("framing", ["length", "close", "chunked"])
def test_ordinary_decision_reader_keeps_strict_deadline(monkeypatch, framing):
    response, _reads, _raw, _clock = _http_body_at_deadline(monkeypatch, framing)
    with response, pytest.raises(TimeoutError):
        jev_transport._read_response(response, 105.0)


@pytest.mark.parametrize("expiry_phase", ["origin", "url_validation"])
def test_exhausted_preflight_never_spends_refresh(monkeypatch, saved_session, expiry_phase):
    clock = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    original = cloud_session._load()
    if expiry_phase == "origin":
        lookup = cloud_session.credential_bound_control_url

        def delayed_origin():
            value = lookup()
            clock[0] += 6
            return value

        monkeypatch.setattr(cloud_session, "credential_bound_control_url", delayed_origin)
    else:
        def delayed_validation(value):
            clock[0] += 6
            return value

        monkeypatch.setattr(cloud_session, "validate_cloud_base_url", delayed_validation)
    monkeypatch.setattr(cloud_session, "_post_refresh", _no_network)
    monkeypatch.setattr(jev_transport, "_post_json", _no_network)
    with pytest.raises(jev_transport.DecisionClientError, match="^remote_timeout$"):
        _evaluate(5)
    assert cloud_session._load() == original


def test_exhausted_refresh_budget_does_not_open_http_or_retire_credential(monkeypatch):
    monkeypatch.setattr(cloud_session, "build_pinned_https_opener",
                        lambda *handlers: SimpleNamespace(open=_no_network))
    with pytest.raises(TimeoutError):
        cloud_session._post_refresh("https://control.example.invalid", "synthetic-unspent",
                                    None, "member", deadline=time.monotonic() - 1)


def test_refresh_http_uses_remaining_budget_and_keeps_default_timeout(monkeypatch):
    timeouts = []

    def open_request(request, timeout):
        timeouts.append(timeout)
        return io.BytesIO(json.dumps(_rotation()).encode())

    monkeypatch.setattr(time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(cloud_session, "build_pinned_https_opener",
                        lambda *handlers: SimpleNamespace(open=open_request))
    assert cloud_session._post_refresh("https://control.example.invalid", "synthetic-unspent",
                                       None, "member", deadline=100.75) == _rotation()
    assert cloud_session._post_refresh("https://control.example.invalid", "synthetic-unspent",
                                       None, "member") == _rotation()
    assert timeouts == [0.75, 10.0]


def test_uncertain_refresh_timeout_retires_spent_credential_without_decision(
    monkeypatch, saved_session,
):
    clock = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    def open_request(request, timeout):
        clock[0] += timeout
        raise urllib.error.URLError(TimeoutError("synthetic send interrupted"))

    monkeypatch.setattr(cloud_session, "build_pinned_https_opener",
                        lambda *handlers: SimpleNamespace(open=open_request))
    monkeypatch.setattr(jev_transport, "_post_json", _no_network)
    with pytest.raises(jev_transport.DecisionClientError, match="^remote_timeout$"):
        _evaluate(5)
    saved = cloud_session._load()
    assert cloud_session._refresh_is_unusable(saved, "synthetic-unspent")
    monkeypatch.setattr(cloud_session, "_post_refresh", _no_network)
    assert not cloud_session.configured(require_compute=False)
    with pytest.raises(cloud_session.CloudSessionError):
        cloud_session.access_for_workspace(None, require_compute=False)


@pytest.mark.parametrize("phase", ["complete", "headers", "chunk_framing", "chunk_trailer", "eof_body"])
def test_real_loopback_refresh_shares_deadline_and_never_uses_proxy(
    monkeypatch, saved_session, phase,
):
    requests = []
    stopped = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append((self.path, json.loads(self.rfile.read(
                int(self.headers["Content-Length"]),
            ))))
            try:
                if self.path == "/v1/tokens/refresh" and phase != "complete":
                    raw = json.dumps(_rotation()).encode()
                    if phase == "headers":
                        prefix = b"HTTP/1.1 200 OK\r\nX-Slow: "
                    elif phase == "eof_body":
                        prefix = b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n" + raw
                    else:
                        prefix = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
                        if phase == "chunk_trailer":
                            prefix += ("%x\r\n" % len(raw)).encode() + raw + b"\r\n0\r\nX-Slow: "
                    self.wfile.write(prefix)
                    self.wfile.flush()
                    # Continuous progress must not turn watchdog shutdown into
                    # proof of EOF or a complete chunk trailer.
                    while not stopped.wait(0.01):
                        self.wfile.write(b" " if phase == "eof_body" else b"0")
                        self.wfile.flush()
                    return
                value = _rotation() if self.path == "/v1/tokens/refresh" else _decision()
                raw = json.dumps(value).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            except OSError:
                pass

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    def resolve(host, port, *args, **kwargs):
        assert host == "127.0.0.1", "test attempted non-loopback resolution"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, port))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    saved = cloud_session._load()
    saved["control_url"] = "http://127.0.0.1:%s" % server.server_port
    cloud_session._save(saved)
    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://proxy.invalid:8080")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, "")
    try:
        started = time.monotonic()
        if phase == "complete":
            assert _evaluate(2).get_noul("q").probability == 0.9
            assert [entry[0] for entry in requests] == ["/v1/tokens/refresh", "/v1/jev/decide"]
            assert cloud_session._load()["refresh_credential"] == "synthetic-rotated"
        else:
            with pytest.raises(jev_transport.DecisionClientError, match="^remote_timeout$"):
                _evaluate(0.15)
            assert time.monotonic() - started < 1.5
            assert [entry[0] for entry in requests] == ["/v1/tokens/refresh"]
            assert cloud_session._refresh_is_unusable(cloud_session._load(), "synthetic-unspent")
        assert requests[0][1]["refresh_credential"] == "synthetic-unspent"
    finally:
        stopped.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert not thread.is_alive()
