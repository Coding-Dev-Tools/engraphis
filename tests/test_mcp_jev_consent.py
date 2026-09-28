"""MCP decisions preserve consent, uncertainty, fallback, and private error boundaries."""
import asyncio
import json
from types import SimpleNamespace

import pytest

pytest.importorskip("mcp")
from engraphis import mcp_server as server
from engraphis.backends import jev_transport as transport


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
        kind=kind, state="Synthetic evidence", allow_remote=True, data_classification="public",
    ))
    assert calls[0][2] == {"model": transport.MODEL, "allow_remote": True,
                            "purpose": kind, "data_classification": "public"}
    assert result["is_fallback"] is False
    assert result["decision_status"] == "uncertain"
    assert result["confidence"] == 0.0
    assert result["confidence_source"] == "derived_decisiveness"
    assert result[result_key] is None


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


def test_remote_advice_cannot_override_local_destructive_veto(monkeypatch):
    batch = transport.CloudDecisionBatch(False, {
        "category": transport.SimpleChoiceDecision("read_only", 0.99),
    }, {"is_safe": transport.SimpleSupportDecision(0.99, 0.98)})
    _client(monkeypatch, batch)
    result = json.loads(server.engraphis_decide(
        kind="guard_command", state="rm -rf / --no-preserve-root", allow_remote=True,
    ))
    assert result["allow_auto"] is False and result["escalate_to_user"] is True
    assert result["advisory_only"] is True


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
