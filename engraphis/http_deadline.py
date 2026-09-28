"""Absolute HTTP deadlines for credential refresh and optional remote decisions.

Pinned address and TLS validation stay in hosted_client. Blocking OS resolver and
filesystem calls cannot be interrupted here; callers recheck the deadline after
those operations and never begin a later network phase with an exhausted budget.
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Optional


def remaining_time(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("HTTP request deadline exceeded")
    return remaining


@contextmanager
def socket_deadline(sock, deadline: float):
    """Interrupt a blocking HTTP parser even when every receive makes progress."""
    import socket

    interrupted = threading.Event()
    timer = None
    if isinstance(sock, socket.socket):
        # Even read1() can consume several reads while parsing chunk framing.
        # Interrupt the socket at the deadline so slow chunk headers cannot keep
        # a single read1() alive. Shutdown does not acquire the reader's lock.
        def expire():
            interrupted.set()
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        timer = threading.Timer(remaining_time(deadline), expire)
        timer.daemon = True
        timer.start()
    try:
        yield interrupted
    finally:
        if timer is not None:
            timer.cancel()


def deadline_handlers(deadline: float, *, loopback_only: bool = False):
    import http.client
    import ipaddress
    import socket
    import urllib.request
    from functools import partial
    from engraphis.hosted_client import PinnedHTTPSConnection, PinnedHTTPSHandler

    def connect_socket(address, timeout=None, source_address=None, *, loopback_only=False):
        # socket.create_connection renews its timeout for each resolved address.
        # Share the request budget across direct, loopback and proxy dial retries.
        remaining_time(deadline)
        host, port = address
        candidates = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
        last_error = None
        for family, kind, protocol, _, target in candidates:
            remaining = remaining_time(deadline)
            if loopback_only and not ipaddress.ip_address(target[0]).is_loopback:
                raise ValueError("loopback requests must connect to loopback")
            sock = None
            try:
                sock = socket.socket(family, kind, protocol)
                sock.settimeout(remaining)
                if source_address is not None:
                    sock.bind(source_address)
                sock.connect(target)
                # TLS must receive only the budget left after the TCP dial.
                sock.settimeout(remaining_time(deadline))
                return sock
            except OSError as exc:
                if sock is not None:
                    sock.close()
                last_error = exc
        if last_error is not None:
            raise last_error
        raise OSError("HTTP endpoint has no connectable address")

    def send_with_deadline(connection, send, data):
        if connection.sock is None:
            if not connection.auto_open:
                raise http.client.NotConnected()
            connection.connect()
        if connection.sock is None:
            raise http.client.NotConnected()
        connection.sock.settimeout(remaining_time(deadline))
        # Headers and bodies are separate sends; SSL/file sends may also loop.
        with socket_deadline(connection.sock, deadline):
            send(data)
            remaining_time(deadline)

    class DeadlineResponse(http.client.HTTPResponse):
        def __init__(self, sock, *args, **kwargs):
            self._deadline_socket = sock
            super().__init__(sock, *args, **kwargs)

        def begin(self):
            # getresponse() parses status and headers before urllib.open()
            # returns. Protect that phase before a response body is available.
            with socket_deadline(self._deadline_socket, deadline):
                super().begin()
                remaining_time(deadline)

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
            # The shared hosted client has a 500 ms floor; bounded requests do not.
            return remaining_time(deadline)

        def send(self, data):
            send_with_deadline(self, super().send, data)

        def _tunnel(self):
            # CONNECT parses its response directly, bypassing response.begin().
            with socket_deadline(self.sock, deadline):
                # typeshed omits this private standard-library method.
                getattr(super(), "_tunnel")()
                # TLS follows CONNECT and shares its remaining budget.
                self.sock.settimeout(remaining_time(deadline))

    class DeadlineHTTPHandler(urllib.request.HTTPHandler):
        def http_open(self, req):
            return self.do_open(DeadlineHTTPConnection, req)

    class DeadlineHTTPSHandler(PinnedHTTPSHandler):
        # Run before the shared opener's ordinary pinned HTTPS handler.
        handler_order = 499

        def do_open(self, http_class, req, **kwargs):
            return super().do_open(DeadlineHTTPSConnection, req, **kwargs)

    return DeadlineHTTPHandler(), DeadlineHTTPSHandler()


def read_response(response, deadline: float, *, max_bytes: int,
                  preserve_complete: bool = False) -> bytes:
    """Bound body reads; optionally retain a complete body for credential rotation."""
    sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
    with socket_deadline(sock, deadline) as interrupted:
        return read_response_chunks(response, deadline, max_bytes=max_bytes,
                                    preserve_complete=preserve_complete, interrupted=interrupted)


def read_response_chunks(response, deadline: float, *, max_bytes: int,
                         preserve_complete: bool = False,
                         interrupted: Optional[threading.Event] = None) -> bytes:
    data = bytearray()
    while len(data) <= max_bytes:
        remaining = remaining_time(deadline)
        # urllib's HTTPResponse wraps SocketIO in a BufferedReader. Tighten the
        # underlying socket deadline for each read rather than renewing the full
        # timeout. fp is None after a length-delimited response reaches EOF.
        sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
        if sock is not None:
            sock.settimeout(remaining)
        # read() tries to fill its entire buffer; read1() returns after a single
        # buffered/socket read, letting the absolute deadline run between chunks.
        chunk = response.read1(min(4096, max_bytes + 1 - len(data)))
        data.extend(chunk)
        if preserve_complete:
            if len(data) > max_bytes:
                break  # The caller rejects oversized bodies before parsing.
            # A final length-delimited chunk needs no additional EOF read. Let
            # refresh callers persist its rotation before reporting the timeout.
            if not getattr(response, "chunked", False) and getattr(response, "length", None) == 0:
                break
            if not chunk:
                # Shutdown can manufacture EOF, including inside chunk trailers;
                # only a natural EOF establishes a complete unframed body.
                if interrupted is not None and interrupted.is_set():
                    raise TimeoutError("HTTP request deadline exceeded")
                outstanding = getattr(response, "length", None)
                if outstanding is not None and outstanding > 0:
                    from http.client import IncompleteRead
                    raise IncompleteRead(bytes(data), outstanding)
                break
        remaining_time(deadline)
        if not chunk:
            break
    return bytes(data)
