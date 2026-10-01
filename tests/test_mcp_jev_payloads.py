"""Registered MCP decisions reach the real managed/BYOK validation boundary.

Only credential acquisition and HTTP are synthetic; client evaluation and wire
validation remain real. No provider request or private session discovery occurs.
"""
import asyncio
import json

import pytest

pytest.importorskip("mcp")

from engraphis import cloud_session, mcp_server
from engraphis.backends import jev_transport
from engraphis.service import MemoryService


@pytest.fixture(params=["managed", "byok"])
def wire_client(request, monkeypatch):
    backend = request.param
    monkeypatch.setenv("ENGRAPHIS_DECISION_BACKEND", backend)
    monkeypatch.setenv("ENGRAPHIS_DECISION_MODEL", jev_transport.MODEL)
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-personal-key")
    monkeypatch.setenv("TYPESAFE_BASE_URL", "https://api.typesafe.ai")
    monkeypatch.setattr(cloud_session, "configured", lambda **kwargs: True)
    monkeypatch.setattr(cloud_session, "credential_bound_control_url",
                        lambda: "https://control.example.invalid")
    calls = {"backend": backend, "refresh": [], "http": []}

    def access(workspace, **kwargs):
        calls["refresh"].append((workspace, kwargs))
        return "synthetic-access", "org_synthetic", ""

    def post(url, token, payload, timeout_s, **kwargs):
        normalized = backend == "managed"
        assert url == ("https://control.example.invalid/v1/jev/decide" if normalized
                       else "https://api.typesafe.ai/v1/systemone")
        assert token == ("synthetic-access" if normalized else "synthetic-personal-key")
        calls["http"].append(payload)
        questions = ([(item["id"], item) for item in payload["questions"]] if normalized
                     else list(payload["questions"].items()))
        answers = {}
        for name, question in questions:
            kind = question["type"]
            if kind == "choice":
                options = question["options"] if normalized else list(question["criteria"])
                answer = {"type": "choice", "confidence": 0.9,
                          "probabilities": {item: float(index == 0)
                                            for index, item in enumerate(options)},
                          "selected" if normalized else "choice": options[0]}
                if normalized:
                    answer["confidence_source"] = "provider"
            else:
                assert kind == "noul"
                answer = {"type": "noul", "probability" if normalized else "noul": 0.9}
                if normalized:
                    answer.update(confidence=0.8, confidence_source="derived_decisiveness")
            answers[name] = answer
        return {"model": jev_transport.MODEL, "is_fallback": False,
                "decisions" if normalized else "answers": answers}

    monkeypatch.setattr(cloud_session, "access_for_workspace", access)
    monkeypatch.setattr(jev_transport, "_post_json", post)
    service = MemoryService.create(":memory:")
    monkeypatch.setattr(mcp_server, "_service", service)
    try:
        yield calls
    finally:
        service.store.close()


@pytest.fixture(params=["classic", "smart"])
def dispatch(request):
    def call(arguments, *, raw=False):
        if request.param == "classic":
            target = mcp_server.classic_mcp
            tool_name = "engraphis_decide"
            tool_arguments = arguments
        else:
            target = mcp_server.smart_mcp
            action = mcp_server._action_payload(mcp_server.ACTION_SPECS["decide"])
            tool_name = "engraphis_execute_action"
            tool_arguments = {"capability_id": action["capability_id"],
                              "schema_digest": action["schema_digest"],
                              "arguments": arguments}
        response = asyncio.run(target.call_tool(tool_name, tool_arguments))
        if raw:
            return response
        content = response[0] if isinstance(response, tuple) else response
        payload = json.loads(content[0].text)
        return payload["result"] if request.param == "smart" else payload

    call.surface = request.param
    return call


@pytest.mark.parametrize("options", [None, ["yes", "no"]], ids=["noul", "choice"])
@pytest.mark.parametrize("state_args", [
    {}, {"state": ""}, {"state": " \n\t"}, {"state": "  Explicit synthetic evidence.  "},
], ids=["omitted", "empty", "whitespace", "explicit"])
def test_custom_question_only_uses_real_client_validation(
    wire_client, dispatch, options, state_args,
):
    question = "Does the synthetic fixture contain evidence?"
    arguments = {"kind": "custom", "question": question, "allow_remote": True,
                 "data_classification": "public", **state_args}
    if options is not None:
        arguments["options"] = options
    result = dispatch(arguments)
    assert result["is_fallback"] is False
    assert result["decision_status"] == "decision"
    assert len(wire_client["http"]) == 1
    payload = wire_client["http"][0]
    state = state_args.get("state", "")
    assert payload["state"] == (state if state.strip() else question)
    if wire_client["backend"] == "managed":
        assert len(wire_client["refresh"]) == 1
        expected = {"id": "custom", "prompt": question, "type": "choice" if options else "noul"}
        if options:
            expected["options"] = options
        assert payload["questions"] == [expected]
        assert payload["purpose"] == "custom"
    else:
        assert wire_client["refresh"] == []
        expected = {"type": "choice" if options else "noul", "instructions": question}
        if options:
            expected["criteria"] = {item: item for item in options}
        assert payload["questions"] == {"custom": expected}
    if options:
        assert result["selected"] == "yes"
    else:
        assert result["probability"] == 0.9


@pytest.mark.parametrize("consent,reason", [
    ({}, "remote_not_authorized"),
    ({"allow_remote": False}, "remote_not_authorized"),
    ({"allow_remote": True, "offline_mode": True}, "offline"),
])
def test_question_only_still_requires_consent_before_backend_selection(
    monkeypatch, wire_client, dispatch, consent, reason,
):
    def forbidden(*args, **kwargs):
        pytest.fail("question-only input bypassed remote consent")

    monkeypatch.setattr(jev_transport, "select_decision_client", forbidden)
    result = dispatch({"kind": "custom", "question": "Synthetic question?", **consent})
    assert result["is_fallback"] is True and result["fallback_reason"] == reason
    assert wire_client["http"] == wire_client["refresh"] == []


@pytest.mark.parametrize("arguments,reason", [
    ({}, "invalid_request"),
    ({"state": " \n", "question": " \t"}, "invalid_request"),
    ({"question": "sk-" + "s" * 24}, "sensitive_content"),
    ({"state": "Synthetic evidence", "question": "sk-" + "s" * 24}, "sensitive_content"),
    ({"question": "Choose a synthetic option", "options": ["safe", "sk-" + "s" * 24]},
     "sensitive_content"),
])
def test_invalid_or_sensitive_custom_input_cannot_refresh_or_send(
    wire_client, dispatch, arguments, reason,
):
    result = dispatch({"kind": "custom", "allow_remote": True, **arguments})
    assert result["is_fallback"] is True and result["fallback_reason"] == reason
    assert wire_client["http"] == wire_client["refresh"] == []


@pytest.mark.parametrize("question_args", [{}, {"question": ""}, {"question": " \n\t"}],
                         ids=["omitted", "empty", "whitespace"])
@pytest.mark.parametrize("options", [None, ["yes", "no"]], ids=["noul", "choice"])
def test_blank_optional_question_uses_default_with_meaningful_state(
    wire_client, dispatch, question_args, options,
):
    arguments = {"kind": "custom", "state": "  Keep this evidence unchanged.  ",
                 "allow_remote": True, **question_args}
    if options:
        arguments["options"] = options
    result = dispatch(arguments)
    assert result["is_fallback"] is False
    assert len(wire_client["http"]) == 1
    payload = wire_client["http"][0]
    assert payload["state"] == arguments["state"]
    prompt = (payload["questions"][0]["prompt"] if wire_client["backend"] == "managed"
              else payload["questions"]["custom"]["instructions"])
    assert prompt == "Evaluate state"


@pytest.mark.parametrize("kind", [
    "guard_command", "classify_contradiction", "verify_support", "verify_completion", "custom",
])
@pytest.mark.parametrize("blank", ["", " \n\t"])
def test_all_blank_semantic_input_is_rejected_before_backend_selection(
    monkeypatch, wire_client, dispatch, kind, blank,
):
    def forbidden(*args, **kwargs):
        pytest.fail("blank semantic input must not inspect backend credentials")

    monkeypatch.setattr(jev_transport, "select_decision_client", forbidden)
    result = dispatch({"kind": kind, "allow_remote": True, **{
        field: blank for field in ("state", "question", "query", "existing_content",
                                   "goal", "recent_actions")
    }})
    assert result["is_fallback"] is True and result["fallback_reason"] == "invalid_request"
    assert wire_client["http"] == wire_client["refresh"] == []


@pytest.mark.parametrize("length", [1024, 1025])
def test_custom_question_schema_matches_real_client_limit(
    monkeypatch, wire_client, dispatch, length,
):
    from mcp.server.fastmcp.exceptions import ToolError

    arguments = {"kind": "custom", "question": "q" * length, "allow_remote": True}
    if length == 1024:
        result = dispatch(arguments)
        assert result["is_fallback"] is False
        assert len(wire_client["http"]) == 1
        assert wire_client["http"][0]["state"] == arguments["question"]
        return

    def forbidden(*args, **kwargs):
        pytest.fail("schema-invalid question must not inspect backend credentials")

    monkeypatch.setattr(jev_transport, "select_decision_client", forbidden)
    if dispatch.surface == "classic":
        with pytest.raises(ToolError, match="1024"):
            dispatch(arguments)
    else:
        response = dispatch(arguments, raw=True)
        content = response.content if hasattr(response, "content") else response
        error = json.loads(content[0].text)["error"]
        assert error["code"] == "E_VALIDATION" and error["message"] == "invalid_arguments"
    assert wire_client["http"] == wire_client["refresh"] == []


def test_combined_context_limit_rejects_without_truncation_or_network(wire_client, dispatch):
    result = dispatch({"kind": "verify_support", "state": "x" * 16000,
                       "query": "Synthetic query", "allow_remote": True})
    assert result["is_fallback"] is True and result["fallback_reason"] == "invalid_request"
    assert wire_client["http"] == wire_client["refresh"] == []


@pytest.mark.parametrize("arguments,expected_state,expected_questions", [
    ({"kind": "guard_command", "state": "git status"}, "git status", {"is_safe", "category"}),
    ({"kind": "classify_contradiction", "state": "Now use SQLite", "existing_content": "Use CSV"},
     "EXISTING FACT: Use CSV\nNEW CANDIDATE FACT: Now use SQLite", {"verdict"}),
    ({"kind": "verify_support", "state": "SQLite is used", "query": "Which database?"},
     "QUERY: Which database?\nEVIDENCE: SQLite is used", {"has_support"}),
    ({"kind": "verify_completion", "state": "Completed", "goal": "Run the fixture",
      "recent_actions": "Fixture passed"},
     "GOAL: Run the fixture\nACTIONS: Fixture passed\nOUTPUT: Completed", {"is_complete"}),
    ({"kind": "verify_completion", "state": "Fixture passed", "goal": "Run the fixture"},
     "GOAL: Run the fixture\nACTIONS: \nOUTPUT: Fixture passed", {"is_complete"}),
])
def test_other_kind_mappings_reach_real_client_validation(
    wire_client, dispatch, arguments, expected_state, expected_questions,
):
    result = dispatch({"allow_remote": True, **arguments})
    assert result["is_fallback"] is False
    assert len(wire_client["http"]) == 1
    payload = wire_client["http"][0]
    assert payload["state"] == expected_state
    questions = payload["questions"]
    actual = ({item["id"] for item in questions} if wire_client["backend"] == "managed"
              else set(questions))
    assert actual == expected_questions
    if arguments["kind"] == "guard_command":
        assert result["category"] == "read_only" and result["safety_probability"] == 0.9
        assert result["allow_auto"] is False and result["escalate_to_user"] is True
        assert result["advisory_only"] is True


@pytest.mark.parametrize("arguments,result_key", [
    ({"kind": "classify_contradiction", "existing_content": "Use SQLite"}, "verdict"),
    ({"kind": "verify_support", "query": "Which database?"}, "supported"),
    ({"kind": "verify_completion", "goal": "Run the fixture"}, "is_complete"),
])
def test_partial_kind_inputs_cannot_refresh_or_send(wire_client, dispatch, arguments, result_key):
    result = dispatch({"allow_remote": True, **arguments})
    assert result["is_fallback"] is True and result["fallback_reason"] == "invalid_request"
    assert result[result_key] is None
    assert wire_client["http"] == wire_client["refresh"] == []


@pytest.mark.parametrize("kind,field", [
    ("guard_command", "state"),
    ("classify_contradiction", "existing_content"),
    ("verify_support", "query"),
    ("verify_completion", "goal"),
    ("verify_completion", "recent_actions"),
    ("custom", "question"),
    ("custom", "options"),
])
def test_sensitive_raw_mcp_fields_never_refresh_or_send(
    wire_client, dispatch, caplog, kind, field,
):
    private = json.dumps({"DB_PASSWORD": 'synthetic" phrase'})
    arguments = {"kind": kind, "state": "Synthetic evidence", "goal": "Check the fixture",
                 "allow_remote": True,
                 field: ["safe", private] if field == "options" else private}
    result = dispatch(arguments)
    assert result["is_fallback"] is True
    assert result["fallback_reason"] == "sensitive_content"
    assert wire_client["http"] == wire_client["refresh"] == []
    assert "DB_PASSWORD" not in json.dumps(result) + caplog.text
    assert "synthetic" not in json.dumps(result) + caplog.text
