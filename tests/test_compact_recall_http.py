"""Compact REST candidates cannot detach exact bindings from packed evidence."""
import pytest

from engraphis.service import MemoryService


@pytest.fixture
def service(tmp_path):
    result = MemoryService.create(str(tmp_path / "compact-http.db"), graph_extractor="none")
    yield result
    result.close()


@pytest.fixture
def client(service, monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from engraphis.routes import v2_api

    monkeypatch.setattr(v2_api, "service", lambda: service)
    app = FastAPI()
    app.include_router(v2_api.router)
    with TestClient(app) as result:
        yield result


@pytest.mark.parametrize("route", ["recall", "intent/recall"])
@pytest.mark.parametrize("budget", [0, 2, 128])
def test_compact_http_bindings_appear_only_in_admitted_packed_sources(client, service, route, budget):
    content = "Use ALPHA only in production; never in staging."
    stored = service.remember(content, workspace="w", repo="api", exact_value="ALPHA",
                              exact_value_type="identifier")
    before = service.store.get_memory(stored["id"])
    params = {"workspace": "w", "token_budget": budget, "response_mode": "compact", "k": 1}
    if route == "recall":
        response = client.get("/api/recall", params={**params, "q": "production ALPHA"})
    else:
        response = client.post("/api/intent/recall", json={**params, "query": "production ALPHA", "repo": "api"})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["memories"] and payload["memories"][0]["id"] == stored["id"]
    assert all("exact_value" not in row for row in payload["memories"])
    assert all(not row.get("content") for row in payload["memories"])
    assert payload["usage"]["context_tokens"] <= budget
    if budget == 128:
        source = payload["packed_sources"][0]
        assert source["id"] == stored["id"]
        assert source["exact_value"] == before.metadata["exact_value"]
        assert source["source_span"] == [content.index("ALPHA"), content.index("ALPHA") + 5]
        assert content in payload["context"]
    else:
        assert payload["context"] == ""
        assert payload["packed_sources"] == []
    after = service.store.get_memory(stored["id"])
    assert after.content == before.content
    assert after.metadata["exact_value"] == before.metadata["exact_value"]


@pytest.mark.parametrize("route", ["recall", "intent/recall"])
def test_compact_http_legacy_withholds_binding_when_restriction_is_truncated(
    client, service, route,
):
    content = (
        "Credential is ALPHA only in production. "
        "Never use this credential in staging environments under any circumstances whatsoever."
    )
    stored = service.remember(
        content,
        workspace="w",
        repo="api",
        exact_value="ALPHA",
        exact_value_type="identifier",
    )
    params = {
        "workspace": "w",
        "repo": "api",
        "token_budget": 13,
        "response_mode": "compact",
        "k": 1,
    }
    if route == "recall":
        response = client.get(
            "/api/recall", params={**params, "q": "ALPHA production"}
        )
    else:
        response = client.post(
            "/api/intent/recall",
            json={**params, "query": "ALPHA production"},
        )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["memories"] and payload["memories"][0]["id"] == stored["id"]
    assert "Credential is ALPHA only in production." in payload["context"]
    assert "Never use this credential" not in payload["context"]
    source = payload["packed_sources"][0]
    assert "exact_value" not in source
    assert "source_span" not in source
    assert source["evidence_unit"]["value"] is None
