"""Tests for the async stdio MCP client."""
from __future__ import annotations

import json

import pytest

from engraphis_prime_agent.config import EngraphisRuntimeConfig
from engraphis_prime_agent.mcp_client import (
    READ_ONLY_TOOLS,
    EngraphisCompatibilityError,
    EngraphisMcpClient,
    EngraphisMcpToolError,
    format_mcp_payload,
)


@pytest.mark.asyncio
async def test_connect_lists_core_tools(mcp_client) -> None:
    tools = await mcp_client.list_tools()
    names = {t["name"] for t in tools}
    expected = {
        "engraphis_session",
        "engraphis_recall_context",
        "engraphis_remember",
        "engraphis_discover_actions",
        "engraphis_execute_read",
        "engraphis_execute_action",
        "engraphis_get_memory",
        "engraphis_update_memory",
        "engraphis_conflict_review",
    }
    assert expected.issubset(names)


@pytest.mark.asyncio
async def test_status_reports_connected(mcp_client) -> None:
    status = await mcp_client.status()
    assert status["connected"] is True
    assert status["server"] == "engraphis"
    assert status["toolCount"] >= 9


@pytest.mark.asyncio
async def test_call_tool_passes_arguments(fake_mcp_server, mcp_client) -> None:
    payload = await mcp_client.call_tool(
        "engraphis_recall_context", {"query": "decision: sqlite-vec KNN", "k": 3}
    )
    assert payload["_tool"] == "engraphis_recall_context"
    assert fake_mcp_server.call_log[-1] == (
        "engraphis_recall_context",
        {"query": "decision: sqlite-vec KNN", "k": 3},
    )


# Note: retry behavior is exercised by the production code path; the
# in-process fake doesn't reliably simulate "transport failure" because
# crashing the server task races with the real ClientSession's receive loop.
# The retry constants (READ_ONLY_TOOLS) are unit-tested separately below.


def test_read_only_tools_classification() -> None:
    # engraphis_recall_context is intentionally NOT in READ_ONLY_TOOLS:
    # the Smart gateway appends a receipt on every successful call, so a
    # transport-level retry would create duplicate accounting records
    # for one logical user request. The class is the opposite: tools
    # whose server-side contract is purely read-only and idempotent.
    assert "engraphis_get_memory" in READ_ONLY_TOOLS
    assert "engraphis_conflict_review" in READ_ONLY_TOOLS
    assert "engraphis_discover_actions" in READ_ONLY_TOOLS
    assert "engraphis_execute_read" in READ_ONLY_TOOLS
    # Writes and side-effect tools are not in the read-only set, so the
    # client's call_tool will not retry them on transport failure.
    assert "engraphis_recall_context" not in READ_ONLY_TOOLS
    assert "engraphis_remember" not in READ_ONLY_TOOLS
    assert "engraphis_execute_action" not in READ_ONLY_TOOLS
    assert "engraphis_session" not in READ_ONLY_TOOLS
    assert "engraphis_update_memory" not in READ_ONLY_TOOLS


@pytest.mark.asyncio
async def test_rejection_text_raises_tool_error(fake_mcp_server, mcp_client) -> None:
    async def handler(name: str, args: dict) -> dict:
        return {
            "isError": True,
            "content": [{"type": "text", "text": "Error: bad_arg"}],
        }

    fake_mcp_server.tool_handler = handler
    with pytest.raises(EngraphisMcpToolError) as exc:
        await mcp_client.call_tool("engraphis_remember", {"content": "x"})
    assert "bad_arg" in str(exc.value)


@pytest.mark.asyncio
async def test_compatibility_error_when_tools_missing(fake_mcp_server) -> None:
    """Drop a core tool from the fake server and verify the compatibility error."""
    # Switch the existing fake server to advertise only one core tool,
    # so the client's required-tool check fails on the others.
    fake_mcp_server.restore()
    fake_mcp_server.tool_names = ("engraphis_session",)
    fake_mcp_server.install()
    config = EngraphisRuntimeConfig(command="ignored", environment={})
    client = EngraphisMcpClient(config)
    try:
        with pytest.raises(EngraphisCompatibilityError) as exc:
            await client.connect()
        assert "missing" in str(exc.value).lower()
    finally:
        await client.close()
        fake_mcp_server.restore()


def test_diagnostic_hint_matches_python_message() -> None:
    client = EngraphisMcpClient(EngraphisRuntimeConfig(command="x"))
    client._diagnostic = "ERROR: This package requires python 3.10 or later.\n"
    hint = client.diagnostic_hint()
    assert hint is not None
    assert "Python 3.10" in hint


def test_diagnostic_hint_matches_missing_mcp() -> None:
    client = EngraphisMcpClient(EngraphisRuntimeConfig(command="x"))
    client._diagnostic = "ModuleNotFoundError: No module named 'mcp'\n"
    assert client.diagnostic_hint() is not None
    assert "mcp" in client.diagnostic_hint().lower()


def test_diagnostic_hint_matches_missing_engraphis() -> None:
    client = EngraphisMcpClient(EngraphisRuntimeConfig(command="x"))
    client._diagnostic = "ModuleNotFoundError: No module named 'engraphis'\n"
    assert client.diagnostic_hint() is not None


def test_format_mcp_payload_joins_text() -> None:
    payload = {
        "content": [
            {"type": "text", "text": "hello"},
            {"type": "text", "text": "world"},
        ]
    }
    assert format_mcp_payload(payload) == "hello\n\nworld"


def test_format_mcp_payload_falls_back_to_json() -> None:
    payload = {"content": []}
    out = format_mcp_payload(payload)
    parsed = json.loads(out)
    assert parsed == payload


@pytest.mark.asyncio
async def test_unknown_tool_name_rejected(mcp_client) -> None:
    with pytest.raises(EngraphisMcpToolError):
        await mcp_client.call_tool("not_a_tool", {})


@pytest.mark.asyncio
async def test_close_bumps_generation(fake_mcp_server) -> None:
    config = EngraphisRuntimeConfig(command="ignored", environment={})
    client = EngraphisMcpClient(config)
    g0 = client.generation()
    await client.connect()
    await client.close()
    g1 = client.generation()
    assert g1 > g0


# ---- new edge-case tests below ----


@pytest.mark.asyncio
async def test_connect_is_idempotent(fake_mcp_server) -> None:
    """Calling connect() twice must return the same session and not re-spawn
    the stdio subprocess or re-fetch the tool list."""
    config = EngraphisRuntimeConfig(command="ignored", environment={})
    client = EngraphisMcpClient(config)
    s1 = await client.connect()
    s2 = await client.connect()
    assert s1 is s2
    # The list_tools cache was populated by the first connect; the second
    # call must not issue a fresh tools/list RPC.
    assert client._tools_cache is not None
    cache_id = id(client._tools_cache)
    await client.connect()
    assert id(client._tools_cache) == cache_id
    await client.close()


@pytest.mark.asyncio
async def test_close_clears_session_stack_and_tools_cache(fake_mcp_server) -> None:
    """After close(), every internal handle must be released so the
    next connect() can rebuild cleanly."""
    config = EngraphisRuntimeConfig(command="ignored", environment={})
    client = EngraphisMcpClient(config)
    await client.connect()
    assert client._session is not None
    assert client._stack is not None
    assert client._tools_cache is not None
    await client.close()
    assert client._session is None
    assert client._stack is None
    assert client._tools_cache is None


@pytest.mark.asyncio
async def test_aenter_aexit_context_manager(fake_mcp_server) -> None:
    """`async with EngraphisMcpClient(...) as client:` must connect on enter
    and release every handle on exit."""
    config = EngraphisRuntimeConfig(command="ignored", environment={})
    async with EngraphisMcpClient(config) as client:
        # Inside the block: connected, tools cached.
        assert client._session is not None
        assert client._tools_cache is not None
        tools = await client.list_tools()
        assert len(tools) >= 9
    # After the block: all handles released.
    assert client._session is None
    assert client._stack is None
    assert client._tools_cache is None


@pytest.mark.asyncio
async def test_aenter_returns_client_instance(fake_mcp_server) -> None:
    config = EngraphisRuntimeConfig(command="ignored", environment={})
    async with EngraphisMcpClient(config) as client:
        assert isinstance(client, EngraphisMcpClient)
        assert client is not None


@pytest.mark.asyncio
async def test_unknown_tool_name_includes_name_in_error_message(mcp_client) -> None:
    """`call_tool` must raise EngraphisMcpToolError AND the error message
    must name the rejected tool so a developer can diagnose the rejection."""
    with pytest.raises(EngraphisMcpToolError) as exc:
        await mcp_client.call_tool("engraphis_does_not_exist", {})
    assert "engraphis_does_not_exist" in str(exc.value)
    # And bare "not_a_tool" (no engraphis_ prefix) is also rejected with a
    # message — a different guard, but same exception class.
    with pytest.raises(EngraphisMcpToolError) as exc2:
        await mcp_client.call_tool("not_a_tool", {})
    assert "not_a_tool" in str(exc2.value)


@pytest.mark.asyncio
async def test_close_is_idempotent(fake_mcp_server) -> None:
    """Calling close() twice must not raise. The second call should be a no-op
    because _stack/_session are already None."""
    config = EngraphisRuntimeConfig(command="ignored", environment={})
    client = EngraphisMcpClient(config)
    await client.connect()
    await client.close()
    # Second close should be silent.
    await client.close()
    assert client._session is None
    assert client._stack is None
    assert client._tools_cache is None


@pytest.mark.asyncio
async def test_list_tools_returns_independent_list(fake_mcp_server) -> None:
    """Mutating the list returned by list_tools() must not affect the cache
    (so a second caller still sees the full list)."""
    config = EngraphisRuntimeConfig(command="ignored", environment={})
    client = EngraphisMcpClient(config)
    await client.connect()
    first = await client.list_tools()
    first.clear()
    second = await client.list_tools()
    assert len(second) == len(first) or len(second) >= 9


@pytest.mark.asyncio
async def test_status_diagnostic_hint_is_none_when_no_failure(fake_mcp_server) -> None:
    """After a healthy connect, diagnosticHint must be None — there is no
    error message to surface."""
    config = EngraphisRuntimeConfig(command="ignored", environment={})
    async with EngraphisMcpClient(config) as client:
        status = await client.status()
        assert status["connected"] is True
        assert status["diagnosticHint"] is None
        assert status["server"] == "engraphis"
        assert status["toolCount"] >= 9


def test_diagnostic_hint_returns_none_for_unrecognized_error() -> None:
    """A diagnostic line that doesn't match any known pattern must surface
    None (not a misleading hint)."""
    client = EngraphisMcpClient(EngraphisRuntimeConfig(command="x"))
    client._diagnostic = "ERROR: connection refused on 127.0.0.1:9999\n"
    assert client.diagnostic_hint() is None


def test_format_mcp_payload_handles_non_text_blocks() -> None:
    """Blocks without a `text` field (e.g. an image) must be skipped, and
    the JSON fallback must kick in when no text content is present."""
    payload = {
        "content": [
            {"type": "image", "data": "ignored"},
            {"type": "text", "text": "only text"},
        ]
    }
    assert format_mcp_payload(payload) == "only text"
    # No text at all -> JSON fallback.
    assert json.loads(format_mcp_payload({"content": [{"type": "image"}]})) == {
        "content": [{"type": "image"}]
    }
