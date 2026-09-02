"""Pytest fixtures: in-process fake MCP server + live-gated real client.

The fake server monkey-patches `mcp.client.stdio.stdio_client` so the real
`ClientSession` runs over an `anyio` memory-stream transport. Tests then
exercise the full JSON-RPC framing without an `engraphis-mcp` subprocess.

Set `ENGRAPHIS_INTEGRATION_LIVE=1` to skip the fake and boot a real
`engraphis-mcp` subprocess for the live integration tests.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import AsyncIterator
from typing import Any

import anyio
import pytest
import pytest_asyncio

from engraphis_prime_agent.config import EngraphisRuntimeConfig
from engraphis_prime_agent.mcp_client import EngraphisMcpClient

__all__ = ["FakeMcpServer", "live_mcp_client", "mcp_client"]


CORE_TOOL_NAMES = (
    "engraphis_session",
    "engraphis_recall_context",
    "engraphis_remember",
    "engraphis_discover_actions",
    "engraphis_execute_read",
    "engraphis_execute_action",
    "engraphis_get_memory",
    "engraphis_update_memory",
    "engraphis_conflict_review",
)


class FakeMcpServer:
    """In-process stand-in for the Engraphis MCP gateway.

    The patched `stdio_client` returns ``(read_stream, write_stream)`` over an
    anyio memory channel pair. The server task drains requests, calls the
    provided handler, and writes back responses.
    """

    def __init__(self, tool_names: tuple[str, ...] = CORE_TOOL_NAMES) -> None:
        async def _default(name: str, args: dict[str, Any]) -> dict[str, Any]:
            if name == "engraphis_session":
                # Pretend a session was created and echo the request back.
                payload = {
                    "session_id": f"ses_fake_{next(self._session_counter):04d}",
                    "agent": args.get("agent", "unknown"),
                    "workspace": args.get("workspace"),
                    "repo": args.get("repo"),
                    "action": args.get("action", "start"),
                }
                if args.get("action") == "end":
                    payload["status"] = "closed"
                return {
                    "_tool": name,
                    "content": [{"type": "text", "text": json.dumps(payload)}],
                }
            return {"_tool": name, "content": [{"type": "text", "text": json.dumps(args)}]}

        self.tool_handler = _default
        self._session_counter = iter(range(1, 10_000))
        self.tool_names = tool_names
        self.call_log: list[tuple[str, dict[str, Any]]] = []
        self.fail_next: Exception | None = None
        self.crash_on_next: bool = False
        # Shared streams so the test can restart the server task while the
        # client keeps the same transport alive.
        self._shared_server_to_client_send: Any = None
        self._shared_client_to_server_send: Any = None
        self._server_task: asyncio.Task[None] | None = None
        self._original_stdio_client: Any = None
        self._installed = False

    def install(self) -> None:
        from mcp.client import stdio as stdio_mod

        self._original_stdio_client = stdio_mod.stdio_client

        @contextlib.asynccontextmanager
        async def _fake_stdio(_params, errlog=None):  # type: ignore[no-untyped-def]
            # If streams haven't been allocated yet (first call), create them.
            if self._shared_client_to_server_send is None:
                # anyio.create_memory_object_stream returns (send, receive).
                s2c_send, c_read = anyio.create_memory_object_stream(max_buffer_size=4096)
                c2s_send, s_read = anyio.create_memory_object_stream(max_buffer_size=4096)
                self._shared_server_to_client_send = s2c_send
                self._shared_client_to_server_send = c2s_send
                self._server_read = s_read
                self._client_read = c_read
                self._start_server()
            elif self._server_task is None or self._server_task.done():
                # Re-entry after a transport failure: spin a fresh server.
                self._start_server()
            try:
                yield (self._client_read, self._shared_client_to_server_send)
            finally:
                if self._server_task and not self._server_task.done():
                    self._server_task.cancel()
                    with contextlib.suppress(BaseException):
                        await self._server_task

        # Patch both the source module AND the binding used by the client.
        stdio_mod.stdio_client = _fake_stdio  # type: ignore[assignment]
        import engraphis_prime_agent.mcp_client as _client_mod

        self._original_client_binding = _client_mod.stdio_client
        _client_mod.stdio_client = _fake_stdio  # type: ignore[assignment]
        self._installed = True

    def _start_server(self) -> None:
        from mcp.shared.message import SessionMessage

        self._server_task = asyncio.create_task(
            self._serve(self._server_read, self._shared_server_to_client_send, SessionMessage)
        )

    async def restart_server(self) -> None:
        """Kill the server task and start a fresh one on the same streams.

        Used to simulate a transport failure (server crash) followed by the
        client successfully reconnecting.
        """
        if self._server_task and not self._server_task.done():
            self._server_task.cancel()
            with contextlib.suppress(BaseException):
                await self._server_task
        self._start_server()

    def restore(self) -> None:
        from mcp.client import stdio as stdio_mod
        import engraphis_prime_agent.mcp_client as _client_mod

        if self._installed and self._original_stdio_client is not None:
            stdio_mod.stdio_client = self._original_stdio_client  # type: ignore[assignment]
            if getattr(self, "_original_client_binding", None) is not None:
                _client_mod.stdio_client = self._original_client_binding  # type: ignore[assignment]
            self._installed = False
        if self._server_task and not self._server_task.done():
            self._server_task.cancel()

    async def _serve(self, read_stream, write_stream, SessionMessage) -> None:  # type: ignore[no-untyped-def]
        """Minimal MCP server. Handles initialize / notifications / tools/list / tools/call."""
        from mcp.shared.message import JSONRPCMessage
        from mcp.types import (
            CallToolResult,
            InitializeResult,
            JSONRPCError,
            JSONRPCResponse,
            ListToolsResult,
            TextContent,
            Tool,
        )

        async def reply_ok(req_id: Any, result: Any) -> None:
            # JSONRPCResponse.result is typed as a dict; dump pydantic models.
            payload_dict = (
                result.model_dump(by_alias=True, mode="json", exclude_none=True)
                if hasattr(result, "model_dump")
                else result
            )
            payload = JSONRPCResponse(jsonrpc="2.0", id=req_id, result=payload_dict)
            await write_stream.send(SessionMessage(message=JSONRPCMessage(payload)))

        async def reply_error(req_id: Any, message: str) -> None:
            err = JSONRPCError(
                jsonrpc="2.0",
                id=req_id,
                error={"code": -32601, "message": message},
            )
            await write_stream.send(SessionMessage(message=JSONRPCMessage(err)))

        while True:
            try:
                message: Any = await read_stream.receive()
            except (anyio.EndOfStream, asyncio.CancelledError):
                return
            # `message` is a SessionMessage; `.message` is a JSONRPCMessage;
            # `.root` is the actual JSONRPCRequest / JSONRPCNotification.
            jsonrpc = getattr(message, "message", message)
            request = getattr(jsonrpc, "root", jsonrpc)
            method = getattr(request, "method", None)
            request_id = getattr(request, "id", None)
            params = getattr(request, "params", None) or {}
            # If the tool handler itself raises (e.g. a transport-failure
            # simulation), we let the exception propagate so the server task
            # exits. The client will see a closed receive stream and treat it
            # as a transport failure, exercising the retry path.
            if method == "tools/call":
                name = params.get("name", "")
                arguments = params.get("arguments") or {}
                self.call_log.append((name, arguments))
                if self.fail_next is not None:
                    exc = self.fail_next
                    self.fail_next = None
                    raise exc
                if self.crash_on_next:
                    self.crash_on_next = False
                    return
                result = await self.tool_handler(name, arguments)
                content = [
                    TextContent(type="text", text=block.get("text", ""))
                    for block in (result.get("content", []) or [])
                ]
                await reply_ok(
                    request_id,
                    CallToolResult(content=content, isError=bool(result.get("isError"))),
                )
                continue
            try:
                if method == "initialize":
                    await reply_ok(
                        request_id,
                        InitializeResult(
                            protocolVersion="2025-03-26",
                            capabilities={},
                            serverInfo=ServerInfo(name="fake-engraphis", version="0.0.0"),
                        ),
                    )
                elif method == "notifications/initialized":
                    continue
                elif method == "tools/list":
                    tools = [
                        Tool(
                            name=n,
                            description=f"fake {n}",
                            inputSchema={"type": "object", "properties": {}},
                        )
                        for n in self.tool_names
                    ]
                    await reply_ok(
                        request_id, ListToolsResult(tools=tools, nextCursor=None)
                    )
                else:
                    await reply_error(request_id, f"Method not found: {method}")
            except Exception as exc:  # noqa: BLE001 — surface as tool error
                try:
                    await reply_ok(
                        request_id,
                        CallToolResult(
                            content=[TextContent(type="text", text=f"Error: {exc}")],
                            isError=True,
                        ),
                    )
                except Exception:
                    return


def ServerInfo(name: str, version: str) -> Any:  # noqa: N802 — helper
    from mcp.types import Implementation

    return Implementation(name=name, version=version)


@pytest_asyncio.fixture
async def fake_mcp_server() -> AsyncIterator[FakeMcpServer]:
    server = FakeMcpServer()
    server.install()
    try:
        yield server
    finally:
        server.restore()


@pytest_asyncio.fixture
async def mcp_client(fake_mcp_server: FakeMcpServer) -> AsyncIterator[EngraphisMcpClient]:
    """Return a connected `EngraphisMcpClient` backed by the fake server."""
    config = EngraphisRuntimeConfig(command="ignored", environment={})
    client = EngraphisMcpClient(config)
    await client.connect()
    try:
        yield client
    finally:
        await client.close()


@pytest_asyncio.fixture
async def live_mcp_client() -> AsyncIterator[EngraphisMcpClient]:
    """Yield a real EngraphisMcpClient against `engraphis-mcp` if available."""
    if not os.environ.get("ENGRAPHIS_INTEGRATION_LIVE"):
        pytest.skip("set ENGRAPHIS_INTEGRATION_LIVE=1 to run live integration tests")
    config = EngraphisRuntimeConfig(command="engraphis-mcp", environment={})
    client = EngraphisMcpClient(config)
    await client.connect()
    try:
        yield client
    finally:
        await client.close()
