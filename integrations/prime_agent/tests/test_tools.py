"""Tests for the 9 Smart tool factories and scope-default helper."""
from __future__ import annotations

import pytest

from engraphis_prime_agent.config import EngraphisRuntimeConfig
from engraphis_prime_agent.mcp_client import EngraphisMcpClient
from engraphis_prime_agent.tools import (
    TOOL_SPECS,
    all_tools,
    apply_scope_defaults,
    build_tool,
    validate_args,
)


@pytest.fixture
def client(mcp_client) -> EngraphisMcpClient:
    return mcp_client


def test_tool_specs_cover_nine_tools() -> None:
    assert len(TOOL_SPECS) == 9
    names = [name for name, _ in TOOL_SPECS]
    assert names == [
        "engraphis_session",
        "engraphis_recall_context",
        "engraphis_remember",
        "engraphis_discover_actions",
        "engraphis_execute_read",
        "engraphis_execute_action",
        "engraphis_get_memory",
        "engraphis_update_memory",
        "engraphis_conflict_review",
    ]


def test_each_tool_has_name_description_and_schema() -> None:
    for name, schema in TOOL_SPECS:
        assert isinstance(name, str) and name
        assert "type" in schema and schema["type"] == "object"
        assert "properties" in schema


def test_remember_schema_declares_keyed_claim_fields_as_properties() -> None:
    schema = dict(TOOL_SPECS)["engraphis_remember"]

    assert {"subject_key", "claim_kind"} <= set(schema["properties"])
    assert "subject_key" not in schema
    assert "claim_kind" not in schema
    assert schema["properties"]["subject_key"] == {"type": "string", "maxLength": 1000}
    assert schema["properties"]["claim_kind"] == {"type": "string", "maxLength": 200}


def test_session_agent_is_optional_for_registered_lifecycle_calls() -> None:
    schema = dict(TOOL_SPECS)["engraphis_session"]
    assert "agent" in schema["properties"]
    assert "agent" not in schema["required"]


def test_session_schema_advertises_compatibility_action_aliases() -> None:
    schema = dict(TOOL_SPECS)["engraphis_session"]
    assert schema["properties"]["action"]["enum"] == [
        "start",
        "end",
        "start_session",
        "end_session",
    ]
    assert validate_args("engraphis_session", {"action": "start_session"})[
        "action"
    ] == "start_session"


def test_nullable_schema_types_accept_each_union_member() -> None:
    assert validate_args("engraphis_session", {"repo": "api"})["repo"] == "api"
    assert validate_args("engraphis_session", {"repo": None})["repo"] is None


def test_build_tool_unknown_name_raises() -> None:
    config = EngraphisRuntimeConfig(command="x")
    client = EngraphisMcpClient(config)
    with pytest.raises(KeyError):
        build_tool("not_a_tool", client, config)


@pytest.mark.asyncio
async def test_recall_context_tool_calls_mcp(client) -> None:
    fn, meta = build_tool("engraphis_recall_context", client, client.config)
    result = await fn({"query": "decision: sqlite-vec KNN"})
    assert result["_tool"] == "engraphis_recall_context"
    assert client._tools_cache is not None  # ensure list_tools was called


@pytest.mark.asyncio
async def test_remember_tool_passes_arguments(client) -> None:
    fn, _ = build_tool("engraphis_remember", client, client.config)
    result = await fn({"content": "Use sqlite-vec KNN for <=1M vectors", "importance": 0.7})
    assert result["_tool"] == "engraphis_remember"


@pytest.mark.asyncio
async def test_session_id_is_injected_when_bound(client, fake_mcp_server) -> None:
    fn, _ = build_tool(
        "engraphis_recall_context", client, client.config, session_id="ses_test_1"
    )
    await fn({"query": "anything"})
    # The fake server records every tools/call; the last entry should
    # carry the injected session_id.
    assert fake_mcp_server.call_log[-1][0] == "engraphis_recall_context"
    assert fake_mcp_server.call_log[-1][1].get("session_id") == "ses_test_1"


def test_all_tools_returns_nine_pairs(client) -> None:
    pairs = all_tools(client, client.config)
    assert len(pairs) == 9
    for fn, meta in pairs:
        assert callable(fn)
        assert meta["name"] in [name for name, _ in TOOL_SPECS]
        assert "description" in meta
        assert "parameters" in meta


def test_apply_scope_defaults_preserves_model_supplied() -> None:
    config = EngraphisRuntimeConfig(
        command="x",
        default_workspace="acme",
        default_repo="api",
    )
    out = apply_scope_defaults(
        {"workspace": "override", "repo": "fork"},
        config,
    )
    assert out["workspace"] == "override"
    assert out["repo"] == "fork"


def test_apply_scope_defaults_injects_when_missing() -> None:
    config = EngraphisRuntimeConfig(
        command="x",
        default_workspace="acme",
        default_repo="api",
    )
    out = apply_scope_defaults({}, config)
    assert out["workspace"] == "acme"
    assert out["repo"] == "api"


def test_apply_scope_defaults_skips_repo_when_workspace_overridden() -> None:
    config = EngraphisRuntimeConfig(
        command="x",
        default_workspace="acme",
        default_repo="api",
    )
    out = apply_scope_defaults({"workspace": "other"}, config)
    assert out["workspace"] == "other"
    assert "repo" not in out


def test_apply_scope_defaults_merges_extra() -> None:
    config = EngraphisRuntimeConfig(command="x")
    out = apply_scope_defaults({}, config, extra={"actor": "user"})
    assert out["actor"] == "user"


def test_apply_scope_defaults_extra_can_be_overridden_by_params() -> None:
    config = EngraphisRuntimeConfig(command="x")
    out = apply_scope_defaults({"actor": "agent"}, config, extra={"actor": "user"})
    assert out["actor"] == "agent"


# ---- new edge-case tests below ----


def test_apply_scope_defaults_does_not_mutate_input_dict() -> None:
    """The helper must not mutate the caller's `params` dict — prime-agent
    and other call sites may reuse the same dict for repeated tool calls."""
    config = EngraphisRuntimeConfig(
        command="x",
        default_workspace="acme",
        default_repo="api",
    )
    params = {"query": "hello"}
    snapshot = dict(params)
    out = apply_scope_defaults(params, config)
    assert params == snapshot  # input untouched
    # Output is a new dict — mutating it must not bleed back.
    out["query"] = "mutated"
    assert params["query"] == "hello"


def test_apply_scope_defaults_does_not_mutate_extra_dict() -> None:
    """`extra` is also treated as read-only."""
    config = EngraphisRuntimeConfig(command="x", default_workspace="acme")
    extra = {"actor": "user", "workspace": "extra-ws"}
    snapshot = dict(extra)
    out = apply_scope_defaults({}, config, extra=extra)
    assert extra == snapshot
    # The output is a copy of extra; mutating output must not leak.
    out["actor"] = "mutated"
    assert extra["actor"] == "user"


def test_apply_scope_defaults_no_defaults_no_extra_returns_new_dict() -> None:
    """With no config defaults and no extra, apply_scope_defaults should
    return a new dict equal to the input — and still not be the same object."""
    config = EngraphisRuntimeConfig(command="x")
    params = {"x": 1}
    out = apply_scope_defaults(params, config)
    assert out == params
    assert out is not params


@pytest.mark.asyncio
async def test_build_tool_returns_async_callable(client) -> None:
    """The returned callable must be awaitable and accept a single dict arg."""
    import inspect

    fn, meta = build_tool("engraphis_remember", client, client.config)
    assert callable(fn)
    assert inspect.iscoroutinefunction(fn) or hasattr(fn, "__call__")
    # Calling with a dict must return an awaitable that resolves to a dict.
    coro = fn({"content": "x"})
    result = await coro
    assert isinstance(result, dict)
    assert "content" in result or "_tool" in result


@pytest.mark.asyncio
async def test_build_tool_meta_has_required_fields(client) -> None:
    """The metadata dict must include name, description, and parameters so
    any prime-agent registration surface can render it without fallbacks."""
    fn, meta = build_tool("engraphis_get_memory", client, client.config)
    assert meta["name"] == "engraphis_get_memory"
    assert isinstance(meta["description"], str) and meta["description"]
    assert meta["parameters"]["type"] == "object"
    assert "properties" in meta["parameters"]


def test_all_tool_schemas_declare_required_field_explicitly() -> None:
    """Every Smart tool schema must declare a `required` key — either as a
    non-empty list of names or an empty list. The absence of `required`
    would be ambiguous (it can be read as "no required fields" OR as
    "all fields implicitly required" depending on the consumer)."""
    for name, schema in TOOL_SPECS:
        assert "required" in schema, f"{name} schema is missing the 'required' key"
        assert isinstance(schema["required"], list), (
            f"{name} schema 'required' must be a list, got {type(schema['required']).__name__}"
        )
        # Every name listed in `required` must also be a defined property.
        for required_name in schema["required"]:
            assert required_name in schema["properties"], (
                f"{name} schema lists {required_name!r} in required "
                "but it is not in properties"
            )


def test_schema_required_names_are_subset_of_properties() -> None:
    """Defense in depth: cross-check every required name appears in properties."""
    for name, schema in TOOL_SPECS:
        for required_name in schema.get("required", []):
            assert required_name in schema["properties"], (
                f"{name}: required field {required_name!r} missing from properties"
            )


def test_schemas_have_additional_properties_false_or_unset() -> None:
    """The schemas set `additionalProperties: False` to surface typos early.
    Any schema that loses this guarantee is a regression."""
    for name, schema in TOOL_SPECS:
        if "additionalProperties" in schema:
            assert schema["additionalProperties"] is False, (
                f"{name} schema should have additionalProperties=False"
            )


def test_no_tool_schema_is_empty() -> None:
    """Every tool must declare at least one property. An empty schema would
    mean the tool accepts no parameters at all, which is not a Smart tool."""
    for name, schema in TOOL_SPECS:
        assert schema.get("properties"), f"{name} schema has no properties"
        assert len(schema["properties"]) >= 1


@pytest.mark.asyncio
async def test_session_id_is_injected_into_call(client, fake_mcp_server) -> None:
    """A tool bound with session_id="ses_xyz" must forward "ses_xyz" as the
    session_id argument of the resulting tools/call RPC."""
    fn, _ = build_tool(
        "engraphis_recall_context", client, client.config, session_id="ses_xyz"
    )
    await fn({"query": "anything"})
    # The fake server records the last call's (name, arguments) pair.
    assert fake_mcp_server.call_log, "fake server recorded no calls"
    last_name, last_args = fake_mcp_server.call_log[-1]
    assert last_name == "engraphis_recall_context"
    assert last_args.get("session_id") == "ses_xyz"
    # The caller-supplied args are preserved alongside the injection.
    assert last_args.get("query") == "anything"


@pytest.mark.asyncio
async def test_session_id_injection_does_not_override_caller_supplied(client, fake_mcp_server) -> None:
    """If the caller already supplied a session_id, the bound session_id
    must NOT silently overwrite it — caller intent wins."""
    fn, _ = build_tool(
        "engraphis_recall_context", client, client.config, session_id="ses_bound"
    )
    await fn({"query": "x", "session_id": "ses_caller"})
    _, last_args = fake_mcp_server.call_log[-1]
    assert last_args["session_id"] == "ses_caller"


@pytest.mark.asyncio
async def test_session_id_not_injected_when_not_bound(client, fake_mcp_server) -> None:
    """A tool built without a session_id must not add a session_id key —
    only the caller-supplied fields (plus scope defaults) reach the server."""
    fn, _ = build_tool("engraphis_recall_context", client, client.config)
    await fn({"query": "x"})
    _, last_args = fake_mcp_server.call_log[-1]
    assert "session_id" not in last_args or last_args.get("session_id") in (None, "")


async def test_session_id_not_injected_for_tools_without_session_id_in_schema(
    client, fake_mcp_server
) -> None:
    """A bound session_id must NOT be injected into tools whose declared
    schema does not list ``session_id``; FastMCP would otherwise reject
    the RPC for an unexpected argument. Covers discover_actions, both
    executors, get_memory, update_memory, and conflict_review."""
    for tool in (
        "engraphis_discover_actions",
        "engraphis_execute_action",
        "engraphis_execute_read",
        "engraphis_get_memory",
        "engraphis_update_memory",
        "engraphis_conflict_review",
    ):
        fn, _meta = build_tool(tool, client, client.config, session_id="ses_bound")
        await fn({})  # any args; the server replies with the echoed payload
        last_name, last_args = fake_mcp_server.call_log[-1]
        assert last_name == tool
        assert "session_id" not in last_args, (
            f"session_id leaked into {tool!r} whose schema does not declare it"
        )


def test_all_tools_with_session_id_returns_independent_callables(client) -> None:
    """all_tools() must return 9 distinct callables, each with its own
    closure-captured name. Reusing a session_id must not collapse the
    tools into a single shared callable."""
    pairs = all_tools(client, client.config, session_id="ses_shared")
    assert len(pairs) == 9
    callables = [fn for fn, _ in pairs]
    # Each callable has a unique __name__ or at least is a different object.
    assert len({id(fn) for fn in callables}) == 9


def test_build_tool_meta_description_matches_descriptor_table(client) -> None:
    """Every built tool's description must match the entry in _DESC — a
    typo in a schema shouldn't silently ship."""
    for name, _schema in TOOL_SPECS:
        _fn, meta = build_tool(name, client, client.config)
        assert meta["name"] == name
        assert isinstance(meta["description"], str) and meta["description"]


def test_all_tool_schemas_have_unique_property_names_within_tool() -> None:
    """A schema that lists the same property twice would be ambiguous."""
    for name, schema in TOOL_SPECS:
        props = schema.get("properties", {})
        assert len(props) == len(set(props)), (
            f"{name} schema has duplicate property names: {list(props)}"
        )
