"""Tests for EngraphisPrimeAgent and PrimeAgentFleet."""
from __future__ import annotations

import asyncio
import json

import pytest

from engraphis_prime_agent.agent import EngraphisPrimeAgent, PrimeAgentFleet
from engraphis_prime_agent.config import (
    DEFAULT_AGENT_NAMES,
    EngraphisRuntimeConfig,
)
from engraphis_prime_agent.mcp_client import EngraphisMcpClient, EngraphisMcpToolError
from engraphis_prime_agent.tools import TOOL_SPECS


# Auto-use the fake MCP server for every test in this module so that any
# test which constructs an EngraphisMcpClient (directly or via the fleet)
# gets the in-process fake transport, not a real subprocess.
@pytest.fixture(autouse=True)
def _install_fake(fake_mcp_server) -> None:
    return None


@pytest.fixture
async def fleet() -> PrimeAgentFleet:
    f = PrimeAgentFleet(
        workspace="test",
        config=EngraphisRuntimeConfig(command="ignored", environment={}),
    )
    await f.client.connect()
    try:
        yield f
    finally:
        await f.aclose()


@pytest.mark.asyncio
async def test_fleet_default_names_is_eight() -> None:
    f = PrimeAgentFleet(workspace="x")
    assert len(f) == 8
    assert f.names() == DEFAULT_AGENT_NAMES


@pytest.mark.asyncio
async def test_fleet_custom_agent_names() -> None:
    custom = ("a", "b", "c", "d", "e", "f", "g", "h")
    f = PrimeAgentFleet(workspace="x", agent_names=custom)
    assert f.names() == custom


@pytest.mark.asyncio
async def test_subagent_repr_and_contains() -> None:
    f = PrimeAgentFleet(workspace="x")
    assert "researcher" in f
    assert f["researcher"].name == "researcher"


@pytest.mark.asyncio
async def test_subagent_rejects_blank_name() -> None:
    config = EngraphisRuntimeConfig(command="ignored", environment={})
    client = EngraphisMcpClient(config)
    with pytest.raises(ValueError):
        EngraphisPrimeAgent("   ", client, config)
    with pytest.raises(ValueError):
        EngraphisPrimeAgent("", client, config)


@pytest.mark.asyncio
async def test_status_reports_workspace_and_agents() -> None:
    f = PrimeAgentFleet(workspace="demo")
    status = f.status()
    assert status["workspace"] == "demo"
    assert len(status["agents"]) == 8
    for entry in status["agents"]:
        assert "name" in entry
        assert "session_id" in entry


@pytest.mark.asyncio
async def test_start_session_returns_session_id_and_caches_it(fleet) -> None:
    agent = fleet["researcher"]
    sid = await agent.start_session()
    assert isinstance(sid, str) and sid
    # Second call is a no-op.
    sid2 = await agent.start_session()
    assert sid2 == sid
    assert agent.session_id == sid


@pytest.mark.asyncio
async def test_force_new_starts_a_fresh_session(fleet) -> None:
    agent = fleet["researcher"]
    sid1 = await agent.start_session()
    sid2 = await agent.start_session(force_new=True)
    assert sid1 != sid2


@pytest.mark.asyncio
async def test_explicit_null_repo_clears_cached_repo(fleet) -> None:
    agent = fleet["researcher"]
    await agent.start_session()
    await agent.start_session(force_new=True, repo=None)
    assert agent.repo is None


@pytest.mark.asyncio
async def test_call_lazy_starts_session(fleet) -> None:
    agent = fleet["researcher"]
    assert agent.session_id is None
    await agent.call("engraphis_recall_context", {"query": "anything"})
    assert agent.session_id is not None


@pytest.mark.asyncio
async def test_call_injects_session_id_into_subsequent_calls(fleet) -> None:
    agent = fleet["researcher"]
    await agent.call("engraphis_recall_context", {"query": "warm up"})
    # The client we drive is the one used by the fleet.
    # We can verify the call succeeded and returned the tool name.
    result = await agent.call("engraphis_recall_context", {"query": "next"})
    assert result["_tool"] == "engraphis_recall_context"


@pytest.mark.asyncio
async def test_end_session_clears_cached_id(fleet) -> None:
    agent = fleet["researcher"]
    await agent.start_session()
    assert agent.session_id is not None
    await agent.end_session(summary="done", outcome="shipped")
    assert agent.session_id is None


@pytest.mark.asyncio
async def test_end_session_is_idempotent_when_no_session(fleet) -> None:
    agent = fleet["researcher"]
    await agent.end_session()  # no-op


@pytest.mark.asyncio
async def test_fan_out_runs_concurrently(fleet) -> None:
    args = {
        "researcher": {"query": "researcher query"},
        "coder": {"query": "coder query"},
    }
    out = await fleet.fan_out("engraphis_recall_context", args)
    assert set(out.keys()) == {"researcher", "coder"}
    for value in out.values():
        assert value["_tool"] == "engraphis_recall_context"


@pytest.mark.asyncio
async def test_fan_out_raises_for_unknown_agent(fleet) -> None:
    with pytest.raises(KeyError):
        await fleet.fan_out("engraphis_recall_context", {"ghost": {}})


@pytest.mark.asyncio
async def test_start_all_sessions_warms_every_agent(fleet) -> None:
    out = await fleet.start_all_sessions()
    # New structured return: {"sessions": {name: sid}, "errors": {name: exc}}.
    assert set(out.keys()) == {"sessions", "errors"}
    sessions = out["sessions"]
    errors = out["errors"]
    assert isinstance(sessions, dict) and isinstance(errors, dict)
    assert set(sessions.keys()) == set(fleet.names())
    assert errors == {}
    for sid in sessions.values():
        assert isinstance(sid, str) and sid


@pytest.mark.asyncio
async def test_register_requires_register_tool() -> None:
    fleet = PrimeAgentFleet(workspace="x")
    with pytest.raises(TypeError) as exc:
        fleet["researcher"].register(object())
    assert "register_tool" in str(exc.value)


@pytest.mark.asyncio
async def test_register_registers_all_nine_tools() -> None:
    fleet = PrimeAgentFleet(workspace="x")
    registered: list[tuple[str, dict]] = []

    class _Target:
        def register_tool(self, name: str, fn, schema: dict) -> None:
            registered.append((name, schema))

    target = _Target()
    fleet["researcher"].register(target)
    assert len(registered) == 9
    for name, schema in registered:
        assert name.startswith("engraphis_")
        assert "parameters" in schema


@pytest.mark.asyncio
async def test_aclose_ends_sessions_and_closes_client() -> None:
    fleet = PrimeAgentFleet(workspace="x")
    await fleet.client.connect()
    await fleet["researcher"].start_session()
    await fleet["coder"].start_session()
    await fleet.aclose()
    assert fleet["researcher"].session_id is None
    assert fleet["coder"].session_id is None
    assert fleet._closed is True


@pytest.mark.asyncio
async def test_agents_reject_calls_after_fleet_close() -> None:
    fleet = PrimeAgentFleet(workspace="x")
    await fleet.client.connect()
    agent = fleet["researcher"]
    await agent.start_session()
    await fleet.aclose()

    with pytest.raises(RuntimeError, match="is closed"):
        await agent.call("engraphis_recall_context", {"query": "after close"})
    with pytest.raises(RuntimeError, match="is closed"):
        await agent.start_session()


@pytest.mark.asyncio
async def test_lifecycle_rejects_unknown_action(fleet) -> None:
    with pytest.raises(EngraphisMcpToolError, match="must be 'start' or 'end'"):
        await fleet["researcher"].call("engraphis_session", {"action": "resume"})


@pytest.mark.asyncio
async def test_lifecycle_accepts_compatibility_action_aliases(fleet) -> None:
    agent = fleet["researcher"]
    await agent.call("engraphis_session", {"action": "start_session"})
    assert agent.session_id is not None
    await agent.call("engraphis_session", {"action": "end_session"})
    assert agent.session_id is None


@pytest.mark.asyncio
async def test_lifecycle_rejects_non_boolean_force_new(fleet) -> None:
    with pytest.raises(EngraphisMcpToolError, match="force_new must be a boolean"):
        await fleet["researcher"].call(
            "engraphis_session", {"force_new": "false"}
        )


@pytest.mark.asyncio
async def test_aexit_via_context_manager() -> None:
    async with PrimeAgentFleet(workspace="x") as fleet:
        await fleet["researcher"].start_session()
    assert fleet._closed is True


@pytest.mark.asyncio
async def test_context_manager_rejects_reentry_after_close() -> None:
    fleet = PrimeAgentFleet(workspace="x")
    async with fleet:
        pass

    with pytest.raises(RuntimeError, match="is closed"):
        async with fleet:
            pass


# ---- new edge-case tests below ----


@pytest.mark.asyncio
async def test_aclose_is_idempotent() -> None:
    """`aclose()` (and therefore `__aexit__`) must be safe to call twice.
    The second call is a no-op because the fleet has already torn down."""
    f = PrimeAgentFleet(workspace="x")
    await f.client.connect()
    await f["researcher"].start_session()
    await f.aclose()
    assert f._closed is True
    # Second call must not raise.
    await f.aclose()
    assert f._closed is True


@pytest.mark.asyncio
async def test_aclose_before_any_session_is_safe() -> None:
    """A fresh fleet that has never connected must close cleanly without
    requiring a prior `start_session` or `connect`."""
    f = PrimeAgentFleet(workspace="x")
    await f.aclose()
    assert f._closed is True


@pytest.mark.asyncio
async def test_fan_out_with_single_sub_agent() -> None:
    """fan_out() with exactly one agent must return a one-entry dict and
    must not raise. The framework-level concurrency path should still work
    for a single coroutine."""
    f = PrimeAgentFleet(workspace="x")
    await f.client.connect()
    try:
        out = await f.fan_out(
            "engraphis_recall_context",
            {"researcher": {"query": "single-agent query"}},
        )
        assert set(out.keys()) == {"researcher"}
        result = out["researcher"]
        assert result["_tool"] == "engraphis_recall_context"
    finally:
        await f.aclose()


@pytest.mark.asyncio
async def test_fan_out_with_empty_args_raises_value_error() -> None:
    """fan_out() with an empty mapping must raise ValueError so a misnamed
    variable at the call site surfaces immediately rather than silently
    producing an empty result dict."""
    f = PrimeAgentFleet(workspace="x")
    await f.client.connect()
    try:
        with pytest.raises(ValueError) as exc:
            await f.fan_out("engraphis_recall_context", {})
        assert "non-empty" in str(exc.value).lower() or "empty" in str(exc.value).lower()
    finally:
        await f.aclose()


@pytest.mark.asyncio
async def test_status_before_any_session_started() -> None:
    """`status()` is a sync method — it must work without any prior connect,
    start_session, or call. It should report the configured workspace, the
    full agent roster, and a None session_id for every agent."""
    f = PrimeAgentFleet(workspace="demo")
    s = f.status()
    assert s["workspace"] == "demo"
    assert len(s["agents"]) == 8
    for entry in s["agents"]:
        assert entry["session_id"] is None
        assert "name" in entry
        assert "workspace" in entry
        assert "repo" in entry


def test_status_before_connect_does_not_require_async() -> None:
    """`status()` is intentionally sync (status snapshot, not a live call).
    It must be callable from a non-async context without a runtime error."""
    f = PrimeAgentFleet(workspace="x")
    s = f.status()
    assert s["workspace"] == "x"
    assert isinstance(s["agents"], list)
    assert isinstance(s["clientGeneration"], int)
    # Generation starts at 0.
    assert s["clientGeneration"] == 0


@pytest.mark.asyncio
async def test_register_calls_register_tool_exactly_n_times() -> None:
    """`register()` must invoke `register_tool` exactly once per tool —
    not zero, not twice, not conditional on the tool name. We assert this
    by counting invocations against the number of tools in TOOL_SPECS."""
    f = PrimeAgentFleet(workspace="x")
    invocations: list[tuple[str, object]] = []

    class _Target:
        def register_tool(self, name: str, fn, schema: dict) -> None:
            invocations.append((name, fn))

    target = _Target()
    f["researcher"].register(target)
    expected_count = len(TOOL_SPECS)
    assert len(invocations) == expected_count
    # Every tool name from TOOL_SPECS must appear exactly once.
    seen = [name for name, _fn in invocations]
    assert seen == [n for n, _ in TOOL_SPECS]
    # Each call's `fn` is callable and distinct from the others.
    fns = [fn for _name, fn in invocations]
    assert all(callable(fn) for fn in fns)
    assert len({id(fn) for fn in fns}) == expected_count


@pytest.mark.asyncio
async def test_register_invokes_for_each_agent_independently() -> None:
    """Each sub-agent's register() registers its OWN 9 tools. Registering
    one agent must not bleed into another agent's binding."""
    f = PrimeAgentFleet(workspace="x")
    researcher_calls: list[str] = []
    coder_calls: list[str] = []

    class _T:
        def __init__(self, sink: list[str]) -> None:
            self._sink = sink

        def register_tool(self, name: str, fn, schema: dict) -> None:
            self._sink.append(name)

    f["researcher"].register(_T(researcher_calls))
    f["coder"].register(_T(coder_calls))
    assert len(researcher_calls) == 9
    assert len(coder_calls) == 9
    assert researcher_calls == coder_calls  # same tool surface


@pytest.mark.asyncio
async def test_fleet_iter_and_len_match() -> None:
    """`len(fleet)` and `for a in fleet` must agree — they both read from
    the same internal agent dict."""
    f = PrimeAgentFleet(workspace="x")
    assert len(f) == 8
    names_via_iter = [a.name for a in f]
    assert names_via_iter == list(f.names())


@pytest.mark.asyncio
async def test_fleet_unknown_name_raises_keyerror() -> None:
    """`__getitem__` for an unknown agent must raise KeyError, not silently
    return None or a default — fan_out already raises KeyError, and direct
    indexing must behave consistently."""
    f = PrimeAgentFleet(workspace="x")
    with pytest.raises(KeyError):
        _ = f["nonexistent_agent"]


@pytest.mark.asyncio
async def test_fleet_contains_is_consistent_with_iter() -> None:
    f = PrimeAgentFleet(workspace="x")
    for name in f.names():
        assert name in f
    assert "definitely_not_an_agent" not in f
    assert None not in f
    assert 42 not in f


@pytest.mark.asyncio
async def test_fleet_workspace_override_sets_every_agent() -> None:
    """When the fleet is constructed with `workspace=...`, every sub-agent
    inherits that workspace. Individual sub-agents have no way to opt out
    (they can only set their own workspace via the EngraphisPrimeAgent
    constructor, which the fleet does not expose)."""
    f = PrimeAgentFleet(workspace="shared-ws")
    for agent in f:
        assert agent.workspace == "shared-ws"


@pytest.mark.asyncio
async def test_start_all_sessions_is_idempotent_per_agent() -> None:
    """Calling start_all_sessions() twice must not spawn extra sessions.
    Each agent should keep its first session id."""
    f = PrimeAgentFleet(workspace="x")
    await f.client.connect()
    try:
        first = await f.start_all_sessions()
        second = await f.start_all_sessions()
        assert first == second
    finally:
        await f.aclose()


@pytest.mark.asyncio
async def test_subagent_status_reflects_session_lifecycle() -> None:
    """`subagent.status()` should reflect the current session state — None
    before start, populated after start, None again after end."""
    f = PrimeAgentFleet(workspace="x")
    await f.client.connect()
    try:
        agent = f["researcher"]
        assert agent.status()["session_id"] is None
        await agent.start_session()
        s = agent.status()
        assert isinstance(s["session_id"], str) and s["session_id"]
        await agent.end_session()
        assert agent.status()["session_id"] is None
    finally:
        await f.aclose()


@pytest.mark.asyncio
async def test_end_session_forwards_open_threads_to_mcp_call(fake_mcp_server) -> None:
    """`end_session(open_threads=[...])` must include the open_threads list
    in the underlying MCP call_tool so the server can persist the
    next-session handoff. Dropping the argument would silently strand
    advertised follow-ups on the server side."""
    f = PrimeAgentFleet(workspace="x")
    await f.client.connect()
    try:
        agent = f["researcher"]
        await agent.start_session()
        thread = "follow up on the caching decision"
        await agent.end_session(summary="done", outcome="ok",
                                open_threads=[thread])
        # Locate the engraphis_session/end RPC in the call log.
        end_calls = [
            (name, args) for name, args in fake_mcp_server.call_log
            if name == "engraphis_session" and args.get("action") == "end"
        ]
        assert end_calls, "expected an engraphis_session/end MCP call"
        # The most recent end call should carry the open_threads payload.
        _name, end_args = end_calls[-1]
        assert end_args.get("open_threads") == [thread]
    finally:
        await f.aclose()


@pytest.mark.asyncio
async def test_end_session_holds_lock_until_close_rpc_finishes() -> None:
    """A replacement start must wait until the previous close is complete."""
    close_started = asyncio.Event()
    release_close = asyncio.Event()
    calls: list[dict[str, object]] = []
    session_number = 0

    class _BlockingClient:
        async def call_tool(
            self, _name: str, args: dict[str, object]
        ) -> dict[str, object]:
            nonlocal session_number
            calls.append(args)
            if args.get("action") == "end":
                close_started.set()
                await release_close.wait()
            else:
                session_number += 1
            session_id = f"ses_blocking_{session_number:04d}"
            return {
                "content": [{"text": json.dumps({"session_id": session_id})}]
            }

    config = EngraphisRuntimeConfig(command="ignored", environment={})
    agent = EngraphisPrimeAgent("researcher", _BlockingClient(), config, workspace="x")
    await agent.start_session()

    end_task = asyncio.create_task(agent.end_session())
    await close_started.wait()
    start_task = asyncio.create_task(agent.start_session())
    await asyncio.sleep(0)
    assert not start_task.done()

    release_close.set()
    await end_task
    await start_task
    assert [call["action"] for call in calls] == ["start", "end", "start"]


@pytest.mark.asyncio
async def test_dispatch_lifecycle_forwards_advertised_arguments(fake_mcp_server) -> None:
    f = PrimeAgentFleet(workspace="initial")
    await f.client.connect()
    try:
        agent = f["researcher"]
        await agent.call(
            "engraphis_session",
            {
                "action": "start",
                "agent": "custom-role",
                "workspace": "override",
                "repo": "project",
                "goal": "inspect integration",
                "force_new": True,
                "token_budget": 2048,
            },
        )
        start_args = fake_mcp_server.call_log[-1][1]
        assert start_args == {
            "action": "start",
            "agent": "custom-role",
            "workspace": "override",
            "repo": "project",
            "goal": "inspect integration",
            "force_new": True,
            "token_budget": 2048,
        }
        assert agent.status()["workspace"] == "override"
        assert agent.status()["repo"] == "project"
        assert agent.status()["goal"] == "inspect integration"
        assert agent.token_budget == 2048

        await agent.end_session()
        assert fake_mcp_server.call_log[-1][1]["agent"] == "custom-role"
        await agent.call(
            "engraphis_session",
            {
                "action": "start",
                "agent": "custom-role",
                "workspace": "override",
                "repo": "project",
                "goal": "inspect integration",
                "force_new": True,
                "token_budget": 2048,
            },
        )
        session_id = agent.status()["session_id"]
        await agent.call(
            "engraphis_session",
            {
                "action": "end",
                "session_id": session_id,
                "agent": "custom-role",
                "workspace": "override",
                "repo": "project",
                "summary": "done",
                "outcome": "shipped",
                "open_threads": ["none"],
            },
        )
        end_args = fake_mcp_server.call_log[-1][1]
        assert end_args == {
            "action": "end",
            "agent": "custom-role",
            "session_id": session_id,
            "workspace": "override",
            "repo": "project",
            "summary": "done",
            "outcome": "shipped",
            "open_threads": ["none"],
        }
    finally:
        await f.aclose()


@pytest.mark.asyncio
async def test_dispatch_session_lifecycle_end_routes_through_state_machine(fake_mcp_server) -> None:
    """`agent.call("engraphis_session", {"action": "end"})` must clear the
    cached session id so subsequent memory calls do not re-inject a
    closed id. Without the lifecycle routing, the agent would still
    hold the prior id after the server closed the session."""
    f = PrimeAgentFleet(workspace="x")
    await f.client.connect()
    try:
        agent = f["researcher"]
        await agent.start_session()
        prior = agent.status()["session_id"]
        assert prior
        await agent.call("engraphis_session", {"action": "end",
                                                "summary": "shutdown",
                                                "outcome": "complete"})
        assert agent.status()["session_id"] is None
    finally:
        await f.aclose()
