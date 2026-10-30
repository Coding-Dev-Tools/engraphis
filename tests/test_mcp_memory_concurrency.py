"""Reproduce desktop memory stalls without long deadlines or live state."""
from concurrent.futures import ThreadPoolExecutor
from collections import OrderedDict
import asyncio
import io
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
import sqlite3
import socket
import threading
import time
from types import SimpleNamespace

import pytest

from engraphis.service import MemoryService, current_user
from engraphis.service_context import bind_service, bound_service


def test_batch_and_recall_receipts_do_not_invert_connection_lock(monkeypatch):
    service = MemoryService.create(":memory:")
    role = threading.local()
    recall_ready, batch_ready, recall_waiting = (threading.Event() for _ in range(3))
    original_receipt = service.store.record_receipt
    original_execute = service.store.conn.execute
    monkeypatch.setattr(type(service.store.conn), "_ACQUIRE_TIMEOUT", 0.3)

    def receipt(operation, **kwargs):
        if getattr(role, "value", "") == "recall":
            recall_ready.set()
            assert batch_ready.wait(3)
        elif getattr(role, "value", "") == "batch":
            batch_ready.set()
            assert recall_waiting.wait(3)
        return original_receipt(operation, **kwargs)

    def execute(sql, *args, **kwargs):
        if getattr(role, "value", "") == "recall" and sql == "BEGIN IMMEDIATE":
            recall_waiting.set()
        return original_execute(sql, *args, **kwargs)

    # The wrapper forwards normal setattr to sqlite; patch its class method instead.
    connection_type = type(service.store.conn)
    original_class_execute = connection_type.execute

    def class_execute(connection, sql, *args, **kwargs):
        if connection is service.store.conn:
            return execute(sql, *args, **kwargs)
        return original_class_execute(connection, sql, *args, **kwargs)

    def recall():
        role.value = "recall"
        return service.recall("copper lantern", workspace="desktop")

    def batch():
        role.value = "batch"
        return service.remember_batch(
            [{"content": "The desktop marker is amber compass."}], workspace="desktop",
        )

    try:
        service.remember("The desktop marker is copper lantern.", workspace="desktop")
        monkeypatch.setattr(service.store, "record_receipt", receipt)
        monkeypatch.setattr(connection_type, "execute", class_execute)
        with ThreadPoolExecutor(max_workers=2) as pool:
            reader = pool.submit(recall)
            assert recall_ready.wait(3)
            writer = pool.submit(batch)
            assert writer.result(timeout=4)["succeeded"] == 1
            assert reader.result(timeout=4)["count"] >= 1
        assert service.verify_receipts(workspace="desktop")["valid"]
    finally:
        service.close()


def test_batch_and_ordinary_save_take_engine_before_connection(monkeypatch):
    service = MemoryService.create(":memory:")
    role = threading.local()
    ordinary_ready, batch_requested = threading.Event(), threading.Event()
    original_lock = service.engine._write_lock
    monkeypatch.setattr(type(service.store.conn), "_ACQUIRE_TIMEOUT", 0.3)

    class GatedLock:
        def acquire(self, *args, **kwargs):
            if getattr(role, "value", "") == "batch":
                batch_requested.set()
            acquired = original_lock.acquire(*args, **kwargs)
            if acquired and getattr(role, "value", "") == "ordinary":
                ordinary_ready.set()
                assert batch_requested.wait(3)
            return acquired

        def release(self):
            original_lock.release()

        def __enter__(self):
            self.acquire()
            return self

        def __exit__(self, *_args):
            self.release()

    def ordinary():
        role.value = "ordinary"
        return service.remember("The separate marker is silver compass.", workspace="desktop")

    def batch():
        role.value = "batch"
        return service.remember_batch(
            [{"content": "The batch marker is copper lantern."}], workspace="desktop",
        )

    try:
        service.store.get_or_create_workspace("desktop")
        monkeypatch.setattr(service.engine, "_write_lock", GatedLock())
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(ordinary)
            assert ordinary_ready.wait(3)
            second = pool.submit(batch)
            assert first.result(timeout=4)["stored"]
            assert second.result(timeout=4)["succeeded"] == 1
        assert service.store.count_memories() == 2
        assert service.verify_receipts(workspace="desktop")["valid"]
    finally:
        service.close()


def test_caller_owned_transaction_fails_promptly_without_losing_caller_work():
    service = MemoryService.create(":memory:")
    ready, release = threading.Event(), threading.Event()

    def competing_writer():
        with service.engine._write_lock:
            ready.set()
            assert release.wait(3)

    try:
        wid = service.store.get_or_create_workspace("original")
        with ThreadPoolExecutor(max_workers=1) as pool:
            writer = pool.submit(competing_writer)
            assert ready.wait(3)
            conn = service.store.conn
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE workspaces SET name=? WHERE id=?", ("caller-survives", wid))
            try:
                with pytest.raises(sqlite3.OperationalError, match="caller-owned"):
                    service.remember_batch([{"content": "New memory."}], workspace="original")
                assert conn.transaction_owned_by_current_thread()
                assert conn.execute("SELECT name FROM workspaces WHERE id=?", (wid,)).fetchone()[0] == (
                    "caller-survives"
                )
            finally:
                conn.rollback()
                release.set()
            writer.result(timeout=4)
        assert conn.execute("SELECT name FROM workspaces WHERE id=?", (wid,)).fetchone()[0] == "original"
        assert service.store.count_memories() == 0
    finally:
        release.set()
        service.close()


def test_parallel_receipt_writes_keep_one_chain_and_nested_rollback():
    service = MemoryService.create(":memory:")
    try:
        wid = service.store.get_or_create_workspace("desktop")
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: service.store.record_receipt(
                "recall", workspace_id=wid,
            ), range(40)))
        assert len({result["id"] for result in results}) == 40
        assert service.verify_receipts(workspace="desktop")["valid"]
        with pytest.raises(RuntimeError, match="abort"):
            with service.store.write_transaction():
                service.store.record_receipt("remember", workspace_id=wid)
                raise RuntimeError("abort")
        assert len(service.store.list_receipts(workspace_id=wid)) == 40
        assert service.verify_receipts(workspace="desktop")["valid"]
    finally:
        service.close()


@pytest.mark.parametrize("surface", ["mcp", "classic_mcp"])
def test_slow_memory_tool_does_not_block_other_clients(monkeypatch, surface):
    pytest.importorskip("mcp")
    from engraphis import mcp_server as server

    entered, release = threading.Event(), threading.Event()

    def remember(*_args, **_kwargs):
        entered.set()
        assert release.wait(3)
        return {"id": "mem_test", "stored": True}

    monkeypatch.setattr(server, "_service", SimpleNamespace(remember=remember))

    async def exercise():
        transport = getattr(server, surface)
        slow = asyncio.create_task(transport.call_tool("engraphis_remember", {"content": "test"}))
        try:
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(0.005)
            assert entered.is_set()
            assert not slow.done(), "A blocking tool monopolized the transport event loop"
            catalog = await asyncio.wait_for(transport.list_tools(), timeout=0.5)
            assert any(tool.name == "engraphis_remember" for tool in catalog)
            assert not slow.done()
        finally:
            release.set()
            await slow

    asyncio.run(exercise())


def test_concurrent_mcp_workers_keep_service_and_principal_context():
    pytest.importorskip("mcp")
    from engraphis import mcp_server as server

    barrier = threading.Barrier(2, timeout=3)

    class ScopedService:
        def __init__(self, name):
            self.name = name

        def remember(self, *_args, **_kwargs):
            barrier.wait()
            assert bound_service() is self
            assert current_user()["id"] == self.name
            return {"id": self.name, "stored": True}

    async def call(name):
        instance = ScopedService(name)
        principal = {"id": name, "email": name + "@example.test", "role": "member"}
        with bind_service(instance, principal=principal):
            result = await server.mcp.call_tool("engraphis_remember", {"content": "test"})
            assert json.loads(result[0].text)["id"] == name
            assert bound_service() is instance
            assert current_user()["id"] == name

    async def exercise():
        await asyncio.gather(call("member_a"), call("member_b"))

    asyncio.run(exercise())
    assert bound_service() is None
    assert current_user() is None


def test_concurrent_capability_pruning_issue_and_resolve(monkeypatch):
    pytest.importorskip("mcp")
    from engraphis import mcp_server as server

    class YieldingIndex(OrderedDict):
        def items(self):
            for item in super().items():
                time.sleep(0.00001)
                yield item

    monkeypatch.setattr(server, "_CAPABILITY_INDEX", YieldingIndex())
    specs = list(server.ACTION_SPECS.values())[:8]

    def exercise(spec):
        for _ in range(15):
            identifier = server._issue_capability(spec)
            assert server._resolve_capability(identifier, spec.schema_digest) is spec
            server._prune_capabilities(time.time())

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(exercise, specs))
    assert len(server._CAPABILITY_INDEX) <= server._CAPABILITY_MAX_ENTRIES


@pytest.mark.parametrize("terminal", ["result", "error"])
def test_bridge_returns_completed_sse_result_before_stream_closes(terminal, monkeypatch):
    from engraphis.mcp_stdio_bridge import LocalBridge

    # The offline suite replaces every DNS result with a public fixture address.
    # This protocol test connects only to its own numeric loopback server.
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, *_a, **_k: [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", port)),
    ])
    release = threading.Event()
    expected = {"jsonrpc": "2.0", "id": 7, terminal: {"value": "copper lantern"}}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.end_headers()
            self.wfile.write(b": keepalive\n\nevent: message\n")
            self.wfile.write(("data: " + json.dumps(expected) + "\n\n").encode())
            self.wfile.flush()
            release.wait(3)
            self.close_connection = True

        def log_message(self, *_args):
            pass

    with HTTPServer(("127.0.0.1", 0), Handler) as http:
        worker = threading.Thread(target=http.serve_forever, daemon=True)
        worker.start()
        try:
            bridge = LocalBridge(f"http://127.0.0.1:{http.server_port}/mcp", timeout=0.5)
            assert bridge.forward({"jsonrpc": "2.0", "id": 7, "method": "tools/list"}) == expected
            assert not release.is_set()
        finally:
            release.set()
            http.shutdown()
            worker.join(timeout=3)


def test_sse_stream_limit_includes_ignored_events(monkeypatch):
    from engraphis import mcp_stdio_bridge as bridge

    monkeypatch.setattr(bridge, "MAX_MESSAGE_BYTES", 40)
    response = io.BytesIO(b": " + b"x" * 60 + b"\n\n")
    response.headers = {"Content-Type": "text/event-stream"}
    with pytest.raises(ValueError, match="message limit"):
        bridge.read_response(response, 7)
