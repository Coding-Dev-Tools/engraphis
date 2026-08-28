"""Async stdio client for the local Engraphis MCP gateway.

Translates integrations/pi/src/mcp-client.ts to the Python `mcp` SDK:
 - one shared subprocess (StdioClientTransport from mcp.client.stdio)
 - generation counter so a close-during-connect cannot leave a stale Client
 - bounded 4 KiB stderr buffer for diagnosis
 - retry-on-read-only up to 2 attempts with backoff
 - 60s connect / 5 min tool timeouts
 - two distinct exception classes for tool-level vs. compatibility errors
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
from contextlib import AsyncExitStack
from typing import Any, TextIO

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import Implementation

from .config import CORE_DIRECT_TOOLS, EXTENSION_VERSION, EngraphisRuntimeConfig

_logger = logging.getLogger("engraphis_prime_agent.mcp_client")

TOOL_REQUEST_TIMEOUT_S = 5 * 60
CONNECT_TIMEOUT_S = 60
STDERR_BUFFER_BYTES = 4 * 1024

# Tools whose server-side contract is idempotent. A transport failure
# can be safely retried because the server will produce the same result.
# ``engraphis_recall_context`` is intentionally NOT in this set: the
# Smart gateway appends a receipt on each successful call, so retrying
# after a transport-level failure would create duplicate accounting
# records for one logical user request.
READ_ONLY_TOOLS = frozenset({
    "engraphis_get_memory",
    "engraphis_conflict_review",
    "engraphis_discover_actions",
    "engraphis_execute_read",
})


class EngraphisMcpToolError(RuntimeError):
    """Semantic rejection returned by the MCP server (e.g. invalid args)."""


class EngraphisCompatibilityError(RuntimeError):
    """Gateway is reachable but does not expose the Smart 9-tool surface."""


class EngraphisMcpClient:
    """Lazy async stdio client. Safe to share across coroutines.

    Concurrent tool calls are serialized through a single asyncio.Lock; the
    stdio transport is one connection, so the upstream SDK cannot interleave
    JSON-RPC frames safely. Framework-level concurrency (e.g. 8 sub-agents
    reasoning in parallel and then each issuing a tool call) is unaffected.
    """

    def __init__(self, config: EngraphisRuntimeConfig) -> None:
        self._config = config
        self._lifecycle = 0
        self._session: ClientSession | None = None
        self._stack: AsyncExitStack | None = None
        self._connect_lock = asyncio.Lock()
        self._call_lock = asyncio.Lock()
        self._tools_cache: list[dict[str, Any]] | None = None
        self._diagnostic = ""
        self._client_name = f"engraphis-prime-agent/{EXTENSION_VERSION}"
        # A real temp file is the only cross-platform `errlog` that Windows
        # subprocess.Popen accepts. The file is read on demand to fill the
        # bounded diagnostic buffer; it's never persisted.
        self._stderr_file: TextIO | None = None
        self._stderr_path: str | None = None

    # --- lifecycle -------------------------------------------------------

    def generation(self) -> int:
        return self._lifecycle

    @property
    def config(self) -> EngraphisRuntimeConfig:
        return self._config

    def diagnostic_hint(self) -> str | None:
        d = self._diagnostic
        if re.search(r"python 3\.10|requires python 3\.10", d, re.I):
            return "The Engraphis MCP server requires Python 3.10 or later."
        if re.search(r"no module named ['\"]?mcp", d, re.I):
            return "The Engraphis MCP dependency is missing. Install `engraphis[mcp]>=1.5,<2`."
        if re.search(r"no module named ['\"]?engraphis", d, re.I):
            return "Engraphis is not installed for the configured MCP command."
        return None

    def _refresh_diagnostic_from_file(self) -> None:
        path = self._stderr_path
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                data = f.read(STDERR_BUFFER_BYTES * 4)
        except OSError:
            return
        self._diagnostic = data[-STDERR_BUFFER_BYTES:]

    async def connect(self) -> ClientSession:
        async with self._connect_lock:
            if self._session is not None:
                return self._session
            # Capture the generation so a concurrent close() (which bumps
            # _lifecycle) invalidates this connect. The post-await check
            # below closes the freshly-opened stack and discards the session
            # instead of publishing a live subprocess after shutdown.
            generation = self._lifecycle
            self._diagnostic = ""
            stack = AsyncExitStack()
            try:
                params = StdioServerParameters(
                    command=self._config.command,
                    args=list(self._config.args),
                    cwd=self._config.cwd,
                    env=dict(self._config.environment),
                )
                # Open a real temp file for stderr so Windows subprocess.Popen
                # can take its fileno. The file is closed and unlinked after
                # the session is torn down.
                err_fd, err_path = tempfile.mkstemp(prefix="engraphis-prime-agent-", suffix=".err")
                err_file = os.fdopen(err_fd, mode="w", encoding="utf-8", buffering=1)
                # Register the unlink BEFORE the close. AsyncExitStack
                # runs callbacks in LIFO order, so the first-registered
                # (unlink) fires last, after the file handle has been
                # closed — required on Windows where an open file cannot
                # be unlinked.
                stack.callback(self._safe_unlink, err_path)
                stack.callback(err_file.close)
                self._stderr_file = err_file
                self._stderr_path = err_path
                read, write = await asyncio.wait_for(
                    stack.enter_async_context(stdio_client(params, errlog=err_file)),
                    timeout=CONNECT_TIMEOUT_S,
                )
                session = await stack.enter_async_context(
                    ClientSession(
                        read,
                        write,
                        client_info=Implementation(name=self._client_name, version=EXTENSION_VERSION),
                    )
                )
                await asyncio.wait_for(session.initialize(), timeout=CONNECT_TIMEOUT_S)
                tools = await self._list_tools(session)
                available = {t["name"] for t in tools}
                missing = [n for n in CORE_DIRECT_TOOLS if n not in available]
                if missing:
                    self._refresh_diagnostic_from_file()
                    raise EngraphisCompatibilityError(
                        "Engraphis 1.5.x Smart MCP is required; the server is "
                        f"missing: {', '.join(missing)}."
                    )
                # If close() ran while we were awaiting, abort — don't
                # publish a session that the caller has already decided to
                # discard. The local stack is closed before the raise so the
                # subprocess is reaped.
                if self._lifecycle != generation:
                    await stack.aclose()
                    self._stderr_file = None
                    self._stderr_path = None
                    raise EngraphisMcpToolError(
                        "Engraphis client was closed before the connect completed."
                    )
                self._session = session
                self._stack = stack
                self._tools_cache = tools
                return session
            except BaseException:
                self._refresh_diagnostic_from_file()
                await stack.aclose()
                self._session = None
                self._stack = None
                self._tools_cache = None
                self._stderr_file = None
                self._stderr_path = None
                raise

    @staticmethod
    def _safe_unlink(path: str) -> None:
        try:
            os.unlink(path)
        except OSError:
            pass

    async def close(self) -> None:
        # Hold the connect lock so any in-flight connect() either completes
        # before us (and is then torn down) or aborts via the post-await
        # generation check. Without this, a concurrent close() can return
        # while a connect() is still mid-await, leaving a live subprocess.
        async with self._connect_lock:
            self._lifecycle += 1
            stack = self._stack
            self._stack = None
            self._session = None
            self._tools_cache = None
            # Reset stderr-temp-file handles. The actual file close + unlink are
            # registered as AsyncExitStack callbacks in connect(), so they fire
            # when `stack.aclose()` runs below. We just need to drop the Python
            # references so a subsequent connect() can recreate them cleanly.
            self._stderr_file = None
            self._stderr_path = None
            if stack is not None:
                try:
                    await stack.aclose()
                except Exception:  # noqa: BLE001 — best-effort teardown
                    _logger.debug("ignored error while closing MCP stack", exc_info=True)

    async def __aenter__(self) -> "EngraphisMcpClient":
        await self.connect()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    # --- tool surface ----------------------------------------------------

    async def list_tools(self) -> list[dict[str, Any]]:
        if self._tools_cache is not None:
            return list(self._tools_cache)
        session = await self.connect()
        tools = await self._list_tools(session)
        self._tools_cache = tools
        return list(tools)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in CORE_DIRECT_TOOLS:
            raise EngraphisMcpToolError(f"Unknown Engraphis tool: {name}")
        last_error: BaseException | None = None
        retry = name in READ_ONLY_TOOLS
        max_attempts = 3 if retry else 1
        for attempt in range(max_attempts):
            try:
                async with self._call_lock:
                    session = await self.connect()
                    response = await asyncio.wait_for(
                        session.call_tool(name, arguments),
                        timeout=TOOL_REQUEST_TIMEOUT_S,
                    )
                return self._format_result(name, response)
            except EngraphisMcpToolError:
                raise
            except EngraphisCompatibilityError:
                raise
            except asyncio.TimeoutError:
                raise
            except asyncio.CancelledError:
                raise
            except (BrokenPipeError, ConnectionError, OSError, EOFError) as exc:
                # Standard transport / stdio-pipe failure: log distinctly
                # at DEBUG (per-attempt noise is already covered by the
                # WARNING below on the terminal failure).
                last_error = exc
                _logger.debug(
                    "MCP transport failure for %s (attempt %d): %s",
                    name, attempt + 1, exc,
                )
                self._refresh_diagnostic_from_file()
                await self.close()
                if attempt + 1 >= max_attempts:
                    break
                # Linear backoff: attempt 0 -> 1.0s, attempt 1 -> 2.2s.
                # Formula: base * (attempt + 1) + jitter * attempt.
                await asyncio.sleep((attempt + 1) * 1.0 + attempt * 0.2)
            except Exception as exc:  # unexpected transport failure
                last_error = exc
                _logger.debug(
                    "MCP unexpected failure for %s (attempt %d): %s",
                    name, attempt + 1, exc,
                )
                self._refresh_diagnostic_from_file()
                await self.close()
                if attempt + 1 >= max_attempts:
                    break
                await asyncio.sleep((attempt + 1) * 1.0 + attempt * 0.2)
        assert last_error is not None
        _logger.warning(
            "MCP call %s failed after %d attempt(s): %s",
            name, max_attempts, last_error,
        )
        raise last_error

    # --- helpers ---------------------------------------------------------

    async def _list_tools(self, session: ClientSession) -> list[dict[str, Any]]:
        all_tools: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            page = await session.list_tools(cursor=cursor)
            for tool in page.tools:
                all_tools.append(
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "inputSchema": tool.inputSchema,
                    }
                )
            cursor = page.nextCursor
            if not cursor:
                break
        return all_tools

    @staticmethod
    def _format_result(name: str, response: Any) -> dict[str, Any]:
        is_error = bool(getattr(response, "isError", False))
        content: list[dict[str, Any]] = []
        for block in getattr(response, "content", []) or []:
            text = getattr(block, "text", None)
            content.append({"type": getattr(block, "type", "text"), "text": text})
        text = "\n\n".join(
            b["text"] for b in content if b.get("type") == "text" and b.get("text")
        ).strip()
        declared_error = re.match(r"^Error:\s*([a-z0-9_]+)\s*$", text, re.I)
        server_error = text.lower().startswith("error:")
        # The Smart gateway emits a structured JSON envelope on tool
        # validation / scope / not-found failures (``engraphis/mcp_server.py::
        # _smart_error``): ``{"code": "...", "message": "...", "retryable": false}``.
        # Detect that envelope inside any text block and forward its code,
        # message, and retryable flag so agent hosts can distinguish caller
        # errors from retryable/internal failures as the Smart contract
        # intends, rather than collapsing every error to the same generic
        # message.
        envelope: dict[str, Any] | None = None
        for block in content:
            if block.get("type") != "text" or not isinstance(block.get("text"), str):
                continue
            try:
                parsed = json.loads(block["text"])
            except (ValueError, TypeError):
                continue
            if (
                isinstance(parsed, dict)
                and isinstance(parsed.get("code"), str)
                and isinstance(parsed.get("message"), str)
            ):
                envelope = parsed
                break
        if is_error or server_error:
            if envelope is not None:
                msg = (
                    f"Engraphis rejected the request: "
                    f"{envelope.get('code', 'unknown')}: {envelope.get('message', '')}"
                )
            elif declared_error:
                msg = f"Engraphis rejected the request: {declared_error.group(1)}."
            else:
                msg = (
                    "Engraphis rejected the request. Verify the parameters and "
                    "inspect the local Engraphis logs."
                )
            raise EngraphisMcpToolError(msg)
        return {"_tool": name, "isError": is_error, "content": content}

    # --- status ----------------------------------------------------------

    async def status(self) -> dict[str, Any]:
        tools = await self.list_tools()
        return {
            "connected": True,
            "server": "engraphis",
            "toolCount": len(tools),
            "diagnosticHint": self.diagnostic_hint(),
        }


def format_mcp_payload(payload: dict[str, Any]) -> str:
    """Return the joined text content of a tool result, falling back to JSON."""
    parts: list[str] = []
    for block in payload.get("content", []) or []:
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
    joined = "\n\n".join(parts).strip()
    return joined or json.dumps(payload, indent=2, default=str)
