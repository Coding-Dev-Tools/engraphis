"""MCP handlers isolate synchronous work without losing scoped request authority."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import replace
import inspect
import json
import threading
import time

import pytest

pytest.importorskip("mcp", reason="optional 'mcp' extra not installed")

from engraphis import mcp_server as server
from engraphis.service import MemoryService, current_user
from engraphis.service_context import (
    bind_service, bound_service, http_transport_context, local_source_access_allowed,
)


@pytest.fixture(autouse=True)
def _isolated_capability_state():
    lock = getattr(server, "_CAPABILITY_LOCK", nullcontext())
    with lock:
        index = server._CAPABILITY_INDEX.copy()
        catalog = dict(server.ACTION_SPECS)
    previous = server._service
    try:
        yield
    finally:
        with lock:
            server._CAPABILITY_INDEX.clear()
            server._CAPABILITY_INDEX.update(index)
            server.ACTION_SPECS.clear()
            server.ACTION_SPECS.update(catalog)
        server._service = previous


@pytest.fixture
def service(tmp_path):
    svc = MemoryService.create(str(tmp_path / "synthetic.db"), embed_model=None, vector_backend="numpy",
                               extractor="none", graph_extractor="none", retention_supervisor="none",
                               allow_automatic_critical_retention=False, sqlite_durability="durable")
    svc.remember("Synthetic concurrency audit fixture.", workspace="synthetic")
    previous = server._service
    server.set_service(svc)
    try:
        yield svc
    finally:
        server._service = previous
        svc.close()


def payload(result):
    content = result[0] if isinstance(result, tuple) else result
    return json.loads(content[0].text)


@pytest.mark.parametrize("surface", ["classic", "gateway"])
def test_blocking_tool_keeps_event_loop_peer_responsive(service, surface):
    entered, release, peer_done, rescued = (threading.Event() for _ in range(4))
    observations = {}
    stats = service.stats

    def blocking_stats(*args, **kwargs):
        observations["handler_thread"] = threading.get_ident()
        observations["bound_service_preserved"] = bound_service() is service
        observations["principal_preserved"] = current_user() == {
            "id": "synthetic-audit-member", "email": "synthetic@example.invalid", "role": "admin"}
        observations["http_source_denial_preserved"] = local_source_access_allowed() is False
        entered.set()
        assert release.wait(4), "Synthetic blocking handler rescue failed"
        observations["peer_completed_before_handler_return"] = peer_done.is_set()
        return stats(*args, **kwargs)

    service.stats = blocking_stats
    chosen = server.classic_mcp if surface == "classic" else server.smart_mcp
    if surface == "classic":
        name, arguments = "engraphis_stats", {"workspace": "synthetic"}
    else:
        discovery = json.loads(server.engraphis_discover_actions("Show memory store statistics."))["actions"][0]
        assert discovery["canonical_action"] == "stats"
        name = "engraphis_execute_read"
        arguments = {"capability_id": discovery["capability_id"], "schema_digest": discovery["schema_digest"],
                     "arguments": {"workspace": "synthetic"}}

    def rescue():
        if entered.wait(4) and not release.wait(1):
            rescued.set()
            release.set()

    watcher = threading.Thread(target=rescue, name="synthetic-mcp-rescue", daemon=True)
    watcher.start()

    async def exercise():
        observations["loop_thread"] = threading.get_ident()

        async def peer():
            while not entered.is_set():
                await asyncio.sleep(0)
            await chosen.list_tools()
            peer_done.set()
            release.set()

        principal = {"id": "synthetic-audit-member", "email": "synthetic@example.invalid", "role": "admin"}
        with bind_service(service, principal=principal), http_transport_context():
            slow = asyncio.create_task(chosen.call_tool(name, arguments))
            other = asyncio.create_task(peer())
            result, _ = await asyncio.gather(slow, other)
            assert result

    try:
        asyncio.run(exercise())
    finally:
        release.set()
        watcher.join(5)
        service.stats = stats
    observations["thread_rescue_was_required"] = rescued.is_set()
    assert not watcher.is_alive()
    assert observations["bound_service_preserved"] is True
    assert observations["principal_preserved"] is True
    assert observations["http_source_denial_preserved"] is True
    assert observations["peer_completed_before_handler_return"] is True
    assert observations["thread_rescue_was_required"] is False
    assert observations["handler_thread"] != observations["loop_thread"]


def test_original_globals_and_smart_read_write_dispatch_stay_synchronous(service):
    direct = server.engraphis_stats(workspace="synthetic")
    assert isinstance(direct, str) and not inspect.isawaitable(direct)
    discovery = json.loads(server.engraphis_discover_actions("Show memory store statistics."))["actions"][0]
    read = server.engraphis_execute_read(discovery["capability_id"], discovery["schema_digest"], {"workspace": "synthetic"})
    assert isinstance(read, str) and json.loads(read)["result"]["workspace"] == "synthetic"
    spec = server.ACTION_SPECS["record_event"]
    cap = server._issue_capability(spec)
    before = service.store.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    result = asyncio.run(server.smart_mcp.call_tool("engraphis_execute_action", {
        "capability_id": cap, "schema_digest": spec.schema_digest,
        "arguments": {"kind": "synthetic.audit", "content": "Synthetic event.", "workspace": "synthetic"}}))
    assert payload(result)["canonical_action"] == "record_event"
    assert service.store.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before + 1


def test_native_async_tools_and_sync_validation_errors_keep_sdk_contract():
    instance = type(server.classic_mcp)("synthetic-contract")

    async def native(value: int) -> str:
        return str(value)

    def synchronous(value: int) -> str:
        return str(value)

    instance.add_tool(native)
    instance.add_tool(synchronous, "synchronous", "Positional title", "Positional description")
    assert instance._tool_manager.get_tool("native").fn is native
    assert instance._tool_manager.get_tool("synchronous").title == "Positional title"
    assert instance._tool_manager.get_tool("synchronous").description == "Positional description"
    assert payload(asyncio.run(instance.call_tool("native", {"value": 2}))) in {2, "2"}
    with pytest.raises(Exception):
        asyncio.run(instance.call_tool("synchronous", {"value": ["invalid"]}))


@pytest.mark.parametrize("served", [False, True])
def test_supported_named_async_callable_preserves_native_registration(served):
    from mcp.server.fastmcp import FastMCP

    class NativeAsyncCallable:
        # MCP1.30's function metadata requires __name__ even for explicitly named
        # callable objects. Exercise the actually supported SDK contract.
        __name__ = "synthetic_native_async_callable"

        async def __call__(self, value: int) -> str:
            return str(value)

    instance = (type(server.classic_mcp) if served else FastMCP)("synthetic-callable")
    handler = NativeAsyncCallable()
    instance.add_tool(handler, name="synthetic_native_async_callable", structured_output=False)
    tool = instance._tool_manager.get_tool("synthetic_native_async_callable")
    assert tool.fn is handler and tool.is_async is True
    result = asyncio.run(instance.call_tool("synthetic_native_async_callable", {"value": 7}))
    assert payload(result) in {7, "7"}


def test_cancelled_write_is_awaited_exactly_once_not_abandoned():
    import anyio

    instance = type(server.classic_mcp)("synthetic-cancellation")
    entered, release, finished = (threading.Event() for _ in range(3))
    writes = []
    scopes = []

    def write() -> str:
        entered.set()
        assert release.wait(4)
        writes.append("synthetic-write-once")
        return "done"

    instance.add_tool(write)
    rescue = threading.Thread(target=lambda: (entered.wait(4), release.wait(1), release.set()), daemon=True)
    rescue.start()

    async def exercise():
        async def invoke():
            with anyio.CancelScope() as scope:
                scopes.append(scope)
                await instance.call_tool("write", {})
            finished.set()

        async with anyio.create_task_group() as group:
            group.start_soon(invoke)
            while not entered.is_set():
                await anyio.sleep(0)
            scopes[0].cancel()
            await anyio.sleep(0)
            assert finished.is_set() is False
            assert writes == []
            release.set()

    try:
        anyio.run(exercise)
    finally:
        release.set()
        rescue.join(5)
    assert finished.is_set() and writes == ["synthetic-write-once"]


def test_capability_disappearing_during_availability_is_stale_not_keyerror():
    original = server.ACTION_SPECS["stats"]

    def unavailable_after_prune():
        server._prune_capabilities(time.time() + server._CAPABILITY_TTL_SECONDS + 1)
        return True

    modified = replace(original, availability_predicate=unavailable_after_prune)
    server.ACTION_SPECS["stats"] = modified
    try:
        cap = server._issue_capability(modified)
        assert server._resolve_capability(cap, modified.schema_digest) is None
    finally:
        server.ACTION_SPECS["stats"] = original


def test_availability_callback_does_not_hold_registry_lock():
    original = server.ACTION_SPECS["stats"]
    completed = threading.Event()
    workers = []

    def predicate():
        worker = threading.Thread(target=lambda: (server._prune_capabilities(time.time()), completed.set()))
        workers.append(worker)
        worker.start()
        return completed.wait(2)

    modified = replace(original, availability_predicate=predicate)
    server.ACTION_SPECS["stats"] = modified
    try:
        cap = server._issue_capability(modified)
        assert server._resolve_capability(cap, modified.schema_digest) is modified
    finally:
        server.ACTION_SPECS["stats"] = original
        for worker in workers:
            worker.join(3)
    assert completed.is_set() and not any(worker.is_alive() for worker in workers)


def test_parallel_capability_issues_pruning_and_resolution_remain_bounded():
    specs = list(server.ACTION_SPECS.values())

    def operate(worker):
        for index in range(100):
            spec = specs[(worker + index) % len(specs)]
            cap = server._issue_capability(spec)
            resolved = server._resolve_capability(cap, spec.schema_digest)
            assert resolved is None or resolved is spec
            server._prune_capabilities(time.time())

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(operate, range(8)))
    assert len(server._CAPABILITY_INDEX) <= server._CAPABILITY_MAX_ENTRIES


@pytest.mark.parametrize("kind", ["expired", "policy", "schema", "evicted"])
def test_capability_ttl_policy_schema_and_lru_still_fail_closed(monkeypatch, kind):
    server._CAPABILITY_INDEX.clear()
    spec = server.ACTION_SPECS["stats"]
    cap = server._issue_capability(spec)
    digest = spec.schema_digest
    if kind == "expired":
        entry = server._CAPABILITY_INDEX[cap]
        server._CAPABILITY_INDEX[cap] = (*entry[:4], 0.0)
    elif kind == "policy":
        monkeypatch.setattr(server, "_DEPLOYMENT_POLICY", "synthetic-changed-policy")
    elif kind == "schema":
        digest = "0" * 16
    else:
        monkeypatch.setattr(server, "_CAPABILITY_MAX_ENTRIES", 1)
        other = next(item for item in server.ACTION_SPECS.values() if item.canonical_id != "stats")
        server._issue_capability(other)
    assert server._resolve_capability(cap, digest) is None
