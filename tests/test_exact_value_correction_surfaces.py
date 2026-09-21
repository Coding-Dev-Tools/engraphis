"""Public corrections cannot silently keep a negated or detached action literal."""
import asyncio
import json

import pytest

from engraphis.core.evidence import exact_value_binding
from engraphis.core.mutations import memory_version
from engraphis.service import MemoryService


@pytest.fixture
def svc(tmp_path):
    service = MemoryService.create(str(tmp_path / "correction.db"), extractor="none")
    yield service
    service.close()


@pytest.fixture
def client(svc, monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from engraphis.routes import v2_api

    monkeypatch.setattr(v2_api, "service", lambda: svc)
    app = FastAPI()
    app.include_router(v2_api.router)
    with TestClient(app) as result:
        yield result


def seed(svc):
    first = svc.remember(
        "Deploy channel is ALPHA.", workspace="w", repo="api",
        exact_value="ALPHA", exact_value_type="identifier",
    )
    return svc.store.get_memory(first["id"])


def body_for(old, route, **controls):
    body = {
        "id": old.id, "workspace": "w", "repo": "api",
        "content": "Deploy channel is now BETA, not ALPHA.", **controls,
    }
    if route == "memory/revise":
        body.update(expected_version=memory_version(old), operation_id="replace-channel")
    return body


def check_successor(svc, old, result, expected):
    successor = svc.store.get_memory(result["id"])
    binding = exact_value_binding(successor.metadata, content=successor.content)
    if expected is None:
        assert "exact_value" not in successor.metadata
    else:
        assert binding["value"] == expected
        assert binding["type"] == "identifier"
        assert successor.content[binding["start"]:binding["end"]] == expected
    predecessor = svc.store.get_memory(old.id)
    assert predecessor.valid_to is not None
    assert predecessor.content == old.content
    assert predecessor.metadata["exact_value"] == old.metadata["exact_value"]


@pytest.mark.parametrize("route", ["correct", "memory/revise"])
@pytest.mark.parametrize(("controls", "expected"), [
    ({}, None),
    ({"exact_value": "BETA", "exact_value_type": "identifier"}, "BETA"),
    ({"clear_exact_value": True}, None),
])
def test_http_correction_binding_intent_and_retry(client, svc, route, controls, expected):
    old = seed(svc)
    body = body_for(old, route, **controls)
    result = client.post("/api/" + route, json=body)
    assert result.status_code == 200, result.text
    check_successor(svc, old, result.json(), expected)
    assert client.post("/api/" + route, json=body).json() == result.json()


@pytest.mark.parametrize("route", ["correct", "memory/revise"])
@pytest.mark.parametrize("controls", [
    {"exact_value": "BETA", "exact_value_span": [True, 5]},
    {"clear_exact_value": "false"},
])
def test_http_binding_controls_reject_coercion_before_mutation(client, svc, route, controls):
    old = seed(svc)
    response = client.post("/api/" + route, json=body_for(old, route, **controls))
    assert response.status_code == 422
    assert svc.store.get_memory(old.id).valid_to is None
    assert svc.store.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1


@pytest.mark.parametrize("route", ["correct", "memory/revise"])
def test_http_invalid_replacement_is_a_validation_error_without_partial_write(client, svc, route):
    old = seed(svc)
    response = client.post("/api/" + route, json=body_for(old, route, exact_value="MISSING"))
    assert response.status_code == 400, response.text
    assert response.json() == {"detail": {"error": "invalid request"}}
    assert "MISSING" not in response.text
    assert svc.store.get_memory(old.id).valid_to is None
    assert svc.store.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1


def test_http_binding_only_revision_has_replay_identity(client, svc):
    old = seed(svc)
    body = body_for(old, "memory/revise", clear_exact_value=True)
    body.pop("content")
    response = client.post("/api/memory/revise", json=body)
    assert response.status_code == 200, response.text
    check_successor(svc, old, response.json(), None)
    assert svc.store.get_memory(response.json()["id"]).content == old.content
    conflict = client.post("/api/memory/revise", json={
        **body, "clear_exact_value": False, "exact_value": "ALPHA",
    })
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["detail"]["code"] == "operation_conflict"


@pytest.mark.parametrize("route", ["correct", "memory/revise"])
def test_http_replacement_selects_an_explicit_repeated_occurrence(client, svc, route):
    old = seed(svc)
    content = "BETA is staged; deploy BETA after approval."
    start = content.rindex("BETA")
    body = body_for(old, route, exact_value="BETA", exact_value_type="identifier",
                    exact_value_span=[start, start + 4])
    response = client.post("/api/" + route, json={**body, "content": content})
    assert response.status_code == 200, response.text
    check_successor(svc, old, response.json(), "BETA")
    assert svc.store.get_memory(response.json()["id"]).metadata["exact_value"]["start"] == start


def test_compatibility_inspector_forwards_replacement_and_clear(svc, monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from engraphis.config import settings
    from engraphis.inspector import create_app

    monkeypatch.setattr(settings, "api_token", "")
    old = seed(svc)
    content = "BETA is staged; deploy BETA after approval."
    start = content.rindex("BETA")
    with TestClient(create_app(svc)) as inspector:
        response = inspector.post("/api/correct", json={
            "memory_id": old.id, "workspace": "w", "repo": "api", "new_content": content,
            "exact_value": "BETA", "exact_value_type": "identifier",
            "exact_value_span": [start, start + 4],
        })
        assert response.status_code == 200, response.text
        check_successor(svc, old, response.json(), "BETA")
        current = svc.store.get_memory(response.json()["id"])
        cleared = inspector.post("/api/correct", json={
            "memory_id": current.id, "workspace": "w", "repo": "api", "new_content": content,
            "clear_exact_value": True,
        })
        assert cleared.status_code == 200, cleared.text
        check_successor(svc, current, cleared.json(), None)


@pytest.mark.parametrize("surface", ["classic", "smart"])
@pytest.mark.parametrize(("controls", "expected"), [
    ({}, None),
    ({"exact_value": "BETA", "exact_value_type": "identifier"}, "BETA"),
    ({"clear_exact_value": True}, None),
])
def test_mcp_correction_replaces_or_clears_the_binding(svc, monkeypatch, surface, controls, expected):
    pytest.importorskip("mcp")
    from engraphis import mcp_server

    monkeypatch.setattr(mcp_server, "service", lambda: svc)
    old = seed(svc)
    arguments = {
        "memory_id": old.id, "workspace": "w", "repo": "api",
        "new_content": "Deploy channel is now BETA, not ALPHA.", **controls,
    }
    if surface == "classic":
        tool = mcp_server.classic_mcp._tool_manager._tools["engraphis_correct"]
        result = json.loads(asyncio.run(tool.run(arguments)))
    else:
        action = mcp_server._action_payload(mcp_server.ACTION_SPECS["correct"])
        assert {"exact_value", "exact_value_span", "clear_exact_value"} <= set(
            action["input_schema"]["properties"]
        )
        response = mcp_server.engraphis_execute_action(
            capability_id=action["capability_id"], schema_digest=action["schema_digest"],
            arguments=arguments,
        )
        assert isinstance(response, str), response
        result = json.loads(response)["result"]
    check_successor(svc, old, result, expected)


@pytest.mark.parametrize("controls", [
    {"exact_value": "BETA", "exact_value_span": [True, 5]},
    {"clear_exact_value": "false"},
])
def test_registered_mcp_rejects_coerced_binding_controls(svc, monkeypatch, controls):
    pytest.importorskip("mcp")
    from mcp.server.fastmcp.exceptions import ToolError
    from engraphis import mcp_server

    monkeypatch.setattr(mcp_server, "service", lambda: svc)
    old = seed(svc)
    tool = mcp_server.classic_mcp._tool_manager._tools["engraphis_correct"]
    with pytest.raises(ToolError, match="validation error"):
        asyncio.run(tool.run({
            "memory_id": old.id, "workspace": "w", "repo": "api",
            "new_content": "Deploy channel is BETA.", **controls,
        }))
    assert svc.store.get_memory(old.id).valid_to is None
