"""Retained PR238 deadline regressions, applied to both Jev transports."""
from types import SimpleNamespace

import pytest


def test_cloud_deadline_interrupts_slow_chunk_framing():
    import http.client
    import socket
    import threading
    import time
    from engraphis.backends.jev_transport import _read_response

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
    from engraphis.backends.jev_transport import _deadline_handlers

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
    from engraphis.backends.jev_transport import _deadline_handlers

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
    import engraphis.backends.jev_transport as module

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
    import engraphis.backends.jev_transport as module

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
