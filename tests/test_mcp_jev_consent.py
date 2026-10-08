"""MCP decisions preserve consent, uncertainty, fallback, and private error boundaries."""
import asyncio
import json
from types import SimpleNamespace

import pytest

pytest.importorskip("mcp")
from engraphis import mcp_server as server
from engraphis.backends import jev_transport as transport


@pytest.mark.parametrize("dispatch", ["direct", "classic", "smart"])
@pytest.mark.parametrize("mode", ["managed", "auto", " MANAGED "])
def test_managed_custom_is_rejected_before_client_or_credential_lookup(monkeypatch, dispatch, mode):
    from engraphis import cloud_session
    from engraphis.backends.jev_decision import select_decision_client

    # Load re-exported selectors before patching the transport module; otherwise
    # a later factory import would retain the temporary forbidden test function.
    assert select_decision_client is transport.select_decision_client

    def forbidden(*_args, **_kwargs):
        pytest.fail("unsupported managed custom calls must not inspect credentials or select a client")

    monkeypatch.setenv("ENGRAPHIS_DECISION_BACKEND", mode)
    monkeypatch.setenv("TYPESAFE_API_KEY", "ambient-key-must-not-select-byok")
    monkeypatch.setattr(transport, "select_decision_client", forbidden)
    monkeypatch.setattr(cloud_session, "configured", forbidden)
    monkeypatch.setattr(cloud_session, "credential_bound_control_url", forbidden)
    monkeypatch.setattr(cloud_session, "access_for_workspace", forbidden)
    arguments = {"kind": "custom", "question": "Does the synthetic fixture contain evidence?",
                 "allow_remote": True, "data_classification": "public"}
    if dispatch == "direct":
        raw = server.engraphis_decide(**arguments)
    elif dispatch == "classic":
        response = asyncio.run(server.classic_mcp.call_tool("engraphis_decide", arguments))
        content = response[0] if isinstance(response, tuple) else response
        raw = content[0].text
    else:
        action = server._action_payload(server.ACTION_SPECS["decide"])
        response = server.engraphis_execute_action(
            capability_id=action["capability_id"], schema_digest=action["schema_digest"],
            arguments=arguments,
        )
        raw = response if isinstance(response, str) else response.content[0].text
    result = json.loads(raw)
    if dispatch == "smart":
        result = result["result"]
    assert result["fallback_reason"] == "managed_operation_unsupported"
    assert result["is_fallback"] is True
    assert result["selected"] is None
    assert result["confidence"] is None
    assert result["advisory_only"] is True


@pytest.mark.parametrize("dispatch", ["direct", "classic", "smart"])
@pytest.mark.parametrize("consent", [{}, {"offline_mode": True, "allow_remote": True},
                                     {"allow_remote": True}])
def test_invalid_kind_never_fabricates_a_decision_or_inspects_credentials(
    monkeypatch, dispatch, consent,
):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid requests must not inspect credentials or call a backend")

    monkeypatch.setattr(transport, "select_decision_client", forbidden)
    arguments = {"kind": "unknown", "state": "Synthetic", **consent}
    if dispatch == "direct":
        raw = server.engraphis_decide(**arguments)
    elif dispatch == "classic":
        response = asyncio.run(server.classic_mcp.call_tool("engraphis_decide", arguments))
        content = response[0] if isinstance(response, tuple) else response
        raw = content[0].text
    else:
        action = server._action_payload(server.ACTION_SPECS["decide"])
        response = server.engraphis_execute_action(
            capability_id=action["capability_id"], schema_digest=action["schema_digest"],
            arguments=arguments,
        )
        raw = response if isinstance(response, str) else response.content[0].text
    result = json.loads(raw)
    if dispatch == "smart":
        result = result["result"]
    assert result["fallback_reason"] == "invalid_request"
    assert result["selected"] is None
    assert result["confidence"] is None
    assert result["is_fallback"] is True


@pytest.mark.parametrize("kwargs", ({}, {"allow_remote": False},
                                    {"offline_mode": True, "allow_remote": True}))
def test_unapproved_or_offline_mcp_never_discovers_credentials(monkeypatch, kwargs):
    def forbidden(*args, **kw):
        pytest.fail("backend configuration must not be inspected without call permission")
    monkeypatch.setattr(transport, "select_decision_client", forbidden)
    result = json.loads(server.engraphis_decide(kind="custom", state="Synthetic", **kwargs))
    assert result["is_fallback"] is True
    assert result["confidence"] is None
    assert result["selected"] is None
    assert result["advisory_only"] is True


@pytest.mark.parametrize("dispatch", ["direct", "classic", "smart"])
@pytest.mark.parametrize("consent", [1, 0, 1.0, "true", "yes", "false", None])
def test_remote_consent_requires_a_literal_boolean(monkeypatch, dispatch, consent):
    from mcp.server.fastmcp.exceptions import ToolError

    def forbidden(*args, **kwargs):
        pytest.fail("malformed consent must not inspect credentials or call a backend")

    monkeypatch.setattr(transport, "select_decision_client", forbidden)
    arguments = {"kind": "custom", "state": "Synthetic", "allow_remote": consent}
    if dispatch == "direct":
        result = json.loads(server.engraphis_decide(**arguments))
        assert result["fallback_reason"] == "remote_not_authorized"
    elif dispatch == "classic":
        with pytest.raises(ToolError, match="valid boolean"):
            asyncio.run(server.classic_mcp.call_tool("engraphis_decide", arguments))
    else:
        action = server._action_payload(server.ACTION_SPECS["decide"])
        response = server.engraphis_execute_action(
            capability_id=action["capability_id"], schema_digest=action["schema_digest"],
            arguments=arguments,
        )
        assert response.isError is True
        assert "E_VALIDATION" in response.content[0].text


def test_smart_read_refuses_remote_decision_before_backend_lookup(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("a Smart read must not inspect credentials or consume decision allowance")

    monkeypatch.setattr(transport, "select_decision_client", forbidden)
    action = server._action_payload(server.ACTION_SPECS["decide"])
    rejected = server.engraphis_execute_read(
        capability_id=action["capability_id"], schema_digest=action["schema_digest"],
        arguments={"kind": "custom", "state": "Synthetic", "allow_remote": True},
    )
    assert rejected.isError is True
    assert "action_requires_execute_action" in rejected.content[0].text


def test_discovered_decision_example_executes_as_offline_advice(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("the advertised example must not inspect remote credentials")

    monkeypatch.setattr(transport, "select_decision_client", forbidden)
    discovered = json.loads(server.engraphis_discover_actions(task="guard command safety"))
    action = next(item for item in discovered["actions"] if item["canonical_action"] == "decide")
    response = server.engraphis_execute_action(
        capability_id=action["capability_id"], schema_digest=action["schema_digest"],
        arguments=action["example"],
    )
    raw = response if isinstance(response, str) else response.content[0].text
    result = json.loads(raw)["result"]
    assert result["decision_status"] == "local_fallback"
    assert result["fallback_reason"] == "offline"
    assert result["advisory_only"] is True
    assert result["allow_auto"] is False


def test_classic_dispatch_retains_local_owner_access_and_requires_call_consent(monkeypatch):
    batch = transport.CloudDecisionBatch(False, {}, {
        "custom": transport.SimpleSupportDecision(probability=0.9, confidence=0.8),
    })
    calls = _client(monkeypatch, batch)
    arguments = {"kind": "custom", "state": "Synthetic", "data_classification": "public"}
    # The same registered dispatch serves stdio. Its local owner has no hosted
    # viewer/member identity, and default consent must still suppress remote work.
    local = asyncio.run(server.classic_mcp.call_tool("engraphis_decide", arguments))
    local_content = local[0] if isinstance(local, tuple) else local
    assert json.loads(local_content[0].text)["fallback_reason"] == "remote_not_authorized"
    assert calls == []
    approved = asyncio.run(server.classic_mcp.call_tool(
        "engraphis_decide", {**arguments, "allow_remote": True},
    ))
    approved_content = approved[0] if isinstance(approved, tuple) else approved
    assert json.loads(approved_content[0].text)["is_fallback"] is False
    assert len(calls) == 1


def test_local_http_smart_dispatch_auth_and_consent_precede_remote_work(monkeypatch, tmp_path):
    """Exercise the real single-principal HTTP mount, without inventing Team roles."""
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from engraphis.config import settings
    from engraphis.dashboard_app import create_app

    monkeypatch.setattr(settings, "db_path", str(tmp_path / "local-mcp.db"))
    monkeypatch.setattr(settings, "embed_model", "")
    monkeypatch.setattr(settings, "api_token", "synthetic-local-deployment-token")
    monkeypatch.setattr(server, "_service", None)
    monkeypatch.setattr(server.mcp.settings, "json_response", True)
    batch = transport.CloudDecisionBatch(False, {}, {
        "custom": transport.SimpleSupportDecision(probability=0.9, confidence=0.8),
    })
    calls = _client(monkeypatch, batch)
    select_client = transport.select_decision_client
    lookups = []

    def inspected():
        lookups.append(True)
        return select_client()

    monkeypatch.setattr(transport, "select_decision_client", inspected)
    headers = {"Authorization": "Bearer synthetic-local-deployment-token",
               "Accept": "application/json, text/event-stream"}
    with TestClient(create_app(), base_url="http://127.0.0.1:8700",
                    client=("127.0.0.1", 50000)) as client:
        def rpc(name, arguments, *, authenticated=True):
            return client.post("/mcp/", json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }, headers=headers if authenticated else {"Accept": headers["Accept"]})

        discovered = rpc("engraphis_discover_actions", {"task": "guard command safety"})
        assert discovered.status_code == 200
        payload = json.loads(discovered.json()["result"]["content"][0]["text"])
        action = next(item for item in payload["actions"] if item["canonical_action"] == "decide")
        arguments = {"capability_id": action["capability_id"],
                     "schema_digest": action["schema_digest"],
                     "arguments": {"kind": "custom", "state": "Synthetic",
                                   "allow_remote": True, "data_classification": "public"}}

        unauthenticated = rpc("engraphis_execute_action", arguments, authenticated=False)
        assert unauthenticated.status_code == 401
        read = rpc("engraphis_execute_read", arguments)
        assert read.status_code == 200 and read.json()["result"]["isError"] is True
        assert "action_requires_execute_action" in read.text
        assert lookups == calls == []

        local_arguments = {**arguments, "arguments": dict(arguments["arguments"])}
        local_arguments["arguments"].pop("allow_remote")
        local = rpc("engraphis_execute_action", local_arguments)
        assert local.status_code == 200
        assert "remote_not_authorized" in local.text
        assert lookups == calls == []

        approved = rpc("engraphis_execute_action", arguments)
        assert approved.status_code == 200 and not approved.json()["result"].get("isError")
        result = json.loads(approved.json()["result"]["content"][0]["text"])
        assert result["result"]["is_fallback"] is False
        assert lookups == [True] and len(calls) == 1


def _client(monkeypatch, batch=None, error=None):
    calls = []
    def evaluate(state, questions, **kwargs):
        calls.append((state, questions, kwargs))
        if error is not None:
            raise error
        return batch
    monkeypatch.setattr(transport, "select_decision_client", lambda: (
        SimpleNamespace(evaluate=evaluate), "engraphis_cloud",
    ))
    return calls


@pytest.fixture(params=["direct", "classic", "smart"])
def dispatch_decision(request):
    def call(arguments):
        if request.param == "direct":
            return json.loads(server.engraphis_decide(**arguments))
        target = server.classic_mcp
        name = "engraphis_decide"
        if request.param == "smart":
            target = server.smart_mcp
            name = "engraphis_execute_action"
            action = server._action_payload(server.ACTION_SPECS["decide"])
            arguments = {"capability_id": action["capability_id"],
                         "schema_digest": action["schema_digest"], "arguments": arguments}
        response = asyncio.run(target.call_tool(name, arguments))
        content = response[0] if isinstance(response, tuple) else response
        result = json.loads(content[0].text)
        return result["result"] if request.param == "smart" else result

    return call


@pytest.mark.parametrize("kind,required,missing", [
    ("classify_contradiction", {"state": "Use SQLite", "existing_content": "Use CSV"}, "state"),
    ("classify_contradiction", {"state": "Use SQLite", "existing_content": "Use CSV"}, "existing_content"),
    ("verify_support", {"state": "SQLite is used", "query": "Which database?"}, "state"),
    ("verify_support", {"state": "SQLite is used", "query": "Which database?"}, "query"),
    ("verify_completion", {"state": "Tests passed", "goal": "Run the tests"}, "state"),
    ("verify_completion", {"state": "Tests passed", "goal": "Run the tests"}, "goal"),
])
@pytest.mark.parametrize("blank", [None, "", " \n\t"], ids=["omitted", "empty", "whitespace"])
def test_missing_required_input_is_unknown_before_backend_selection(
    monkeypatch, dispatch_decision, kind, required, missing, blank,
):
    def forbidden(*args, **kwargs):
        pytest.fail("incomplete decisions must not inspect credentials or consume allowance")

    monkeypatch.setattr(transport, "select_decision_client", forbidden)
    # Unrelated context, including optional action history, cannot replace a fact,
    # a query, a goal, or the output evidence required for this decision kind.
    arguments = dict.fromkeys(
        ("query", "existing_content", "goal", "recent_actions", "question"), "Other context",
    )
    arguments.update(kind=kind, allow_remote=True, **required)
    arguments.pop(missing)
    if blank is not None:
        arguments[missing] = blank
    result = dispatch_decision(arguments)
    assert result["is_fallback"] is True
    assert result["fallback_reason"] == "invalid_request"
    assert result["advisory_only"] is True and result["confidence"] is None
    for field in ("supported", "probability", "is_complete", "completion_probability", "verdict", "selected"):
        if field in result:
            assert result[field] is None


@pytest.mark.parametrize("kind,field", [
    ("classify_contradiction", "verdict"), ("verify_support", "supported"),
    ("verify_completion", "is_complete"),
])
def test_client_rejected_input_cannot_fall_back_to_a_definitive_conclusion(
    monkeypatch, dispatch_decision, kind, field,
):
    calls = _client(monkeypatch, error=transport.DecisionClientError("invalid_request"))
    result = dispatch_decision({
        "kind": kind, "state": "Tests passed successfully", "query": "Tests passed",
        "existing_content": "Tests passed successfully", "goal": "Run tests", "allow_remote": True,
    })
    assert len(calls) == 1
    assert result["fallback_reason"] == "invalid_request"
    assert result[field] is None
    assert result["confidence"] is None and result["advisory_only"] is True
    for probability in ("probability", "completion_probability"):
        if probability in result:
            assert result[probability] is None


@pytest.mark.parametrize("kind", ["classify_contradiction", "verify_support", "verify_completion"])
@pytest.mark.parametrize("consent", [{}, {"allow_remote": True, "offline_mode": True}])
def test_incomplete_local_input_is_unknown_without_backend_selection(
    monkeypatch, dispatch_decision, kind, consent,
):
    def forbidden(*args, **kwargs):
        pytest.fail("local advice must not inspect credentials")

    monkeypatch.setattr(transport, "select_decision_client", forbidden)
    result = dispatch_decision({"kind": kind, "state": "Tests passed", **consent})
    assert result["fallback_reason"] == "invalid_request"
    assert result["is_fallback"] is True and result["advisory_only"] is True
    assert result["confidence"] is None
    for field in ("verdict", "supported", "probability", "is_complete", "completion_probability"):
        if field in result:
            assert result[field] is None


@pytest.mark.parametrize("kind", ["classify_contradiction", "verify_support", "verify_completion"])
@pytest.mark.parametrize("consent,reason", [
    ({}, "remote_not_authorized"), ({"allow_remote": True, "offline_mode": True}, "offline"),
])
def test_valid_local_input_preserves_offline_and_consent_boundary(
    monkeypatch, dispatch_decision, kind, consent, reason,
):
    def forbidden(*args, **kwargs):
        pytest.fail("valid local advice must not inspect credentials")

    monkeypatch.setattr(transport, "select_decision_client", forbidden)
    result = dispatch_decision({
        "kind": kind, "state": "Tests passed", "query": "Tests passed?",
        "existing_content": "Tests pending", "goal": "Run the tests", **consent,
    })
    assert result["fallback_reason"] == reason
    assert result["is_fallback"] is True and result["advisory_only"] is True
    assert result["confidence"] is None


@pytest.mark.parametrize("kind,key,result_key", (
    ("verify_support", "has_support", "supported"),
    ("verify_completion", "is_complete", "is_complete"),
))
def test_uncertain_remote_result_is_neither_success_nor_failure(monkeypatch, kind, key, result_key):
    batch = transport.CloudDecisionBatch(False, {}, {
        key: transport.SimpleSupportDecision(probability=0.5, confidence=0.0),
    })
    calls = _client(monkeypatch, batch)
    result = json.loads(server.engraphis_decide(
        kind=kind, state="Synthetic evidence", query="Synthetic query", goal="Check the fixture",
        allow_remote=True, data_classification="public",
    ))
    assert calls[0][2] == {"model": transport.MODEL, "allow_remote": True,
                            "purpose": kind, "data_classification": "public"}
    assert result["is_fallback"] is False
    assert result["decision_status"] == "uncertain"
    assert result["confidence"] == 0.0
    assert result["confidence_source"] == "derived_decisiveness"
    assert result[result_key] is None


@pytest.mark.parametrize("probability,status,complete", (
    (0.95, "decision", True),
    (0.85, "decision", True),
    (0.8, "uncertain", None),
    (0.6, "uncertain", None),
    (0.2, "decision", False),
))
def test_remote_completion_requires_the_completion_bar(monkeypatch, probability, status, complete):
    decision = transport.SimpleSupportDecision(probability=probability,
                                               confidence=abs(2 * probability - 1))
    _client(monkeypatch, transport.CloudDecisionBatch(False, {}, {"is_complete": decision}))
    result = json.loads(server.engraphis_decide(
        kind="verify_completion", state="Synthetic evidence", goal="Check the fixture",
        allow_remote=True, data_classification="public",
    ))
    assert result["is_fallback"] is False
    assert result["decision_status"] == status
    assert result["is_complete"] is complete
    assert result["completion_probability"] == probability


@pytest.mark.parametrize("batch,error,reason", (
    (transport.CloudDecisionBatch(True, {}, {}), None, "provider_fallback"),
    (transport.CloudDecisionBatch(False, {}, {}), None, "malformed_response"),
    (None, RuntimeError("private request and synthetic credential"), "remote_unavailable"),
    (None, transport.DecisionClientError("allowance_exhausted"), "allowance_exhausted"),
))
def test_remote_failures_cannot_look_like_verified_success(monkeypatch, batch, error, reason):
    _client(monkeypatch, batch, error)
    raw = server.engraphis_decide(kind="verify_support", state="Synthetic evidence",
                                 query="Synthetic", allow_remote=True)
    result = json.loads(raw)
    assert result["is_fallback"] is True
    assert result["decision_status"] == "local_fallback"
    assert result["fallback_reason"] == reason
    assert result["confidence"] is None
    assert "private request" not in raw and "synthetic credential" not in raw


_REMOVAL_COMMANDS = (
    "rm -rf / --no-preserve-root",
    "rm -rf .",
    "rm -rf *",
    "rm -fr ./build",
    "rm -rf build",
    "rm -r -f ./build",
    "rm -f -r build",
    "rm -Rf ./build",
    "rm --recursive --force ./build",
    "rm --force --recursive build",
    "rm -r --force ./build",
    "rm --recursive -f ./build",
)


@pytest.mark.parametrize("command", _REMOVAL_COMMANDS + (
    "git status; rm -rf .",
    "git status && rm -rf ./build",
    "echo $(rm -fr ./build)",
    "sh -c 'rm -rf *'",
    'cmd /c "del /s /q build"',
    "Remove-Item -Recurse -Force ./build",
    "git status",
    "echo 'rm -rf . is an example, not a command to run'",
))
def test_remote_guard_advice_never_authorizes_commands(monkeypatch, dispatch_decision, command):
    batch = transport.CloudDecisionBatch(False, {
        "category": transport.SimpleChoiceDecision("read_only", 0.99),
    }, {"is_safe": transport.SimpleSupportDecision(0.99, 0.98)})
    calls = _client(monkeypatch, batch)
    # These are inert strings: neither this test nor the guard executes a command.
    result = dispatch_decision({"kind": "guard_command", "state": command, "allow_remote": True})
    assert len(calls) == 1
    assert result["is_fallback"] is False and result["decision_status"] == "decision"
    assert result["allow_auto"] is False and result["escalate_to_user"] is True
    assert result["advisory_only"] is True
    # Preserve the provider's advice without elevating it to shell authority.
    assert result["category"] == "read_only" and result["category_confidence"] == 0.99
    assert result["safety_probability"] == 0.99 and result["confidence"] == 0.98


@pytest.mark.parametrize("command", _REMOVAL_COMMANDS)
def test_local_removal_advice_recognizes_relative_targets_and_flag_order(monkeypatch, command):
    def forbidden(*args, **kwargs):
        pytest.fail("local command advice must not inspect credentials")

    monkeypatch.setattr(transport, "select_decision_client", forbidden)
    result = json.loads(server.engraphis_decide(kind="guard_command", state=command))
    assert result["category"] == "destructive_or_leak"
    assert result["allow_auto"] is False and result["escalate_to_user"] is True
    assert result["is_fallback"] is True and result["confidence"] is None


@pytest.mark.parametrize("command", (
    "git status",
    "git branch -D feature",
    "echo text > tracked-file.txt",
    "git status; Remove-Item -Recurse project",
))
@pytest.mark.parametrize("remote", (False, True))
def test_unmeasured_command_fallback_never_recommends_autoexecution(monkeypatch, command, remote):
    _client(monkeypatch, error=transport.DecisionClientError("remote_timeout"))
    result = json.loads(server.engraphis_decide(
        kind="guard_command", state=command, allow_remote=remote,
    ))
    assert result["is_fallback"] is True
    assert result["allow_auto"] is False
    assert result["escalate_to_user"] is True
    assert result["confidence"] is None
