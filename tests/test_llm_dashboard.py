"""Dashboard controls and audit view for the optional LLM extractor."""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi", reason="full-stack extra not installed")
pytest.importorskip("httpx", reason="httpx not installed")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from engraphis.config import settings  # noqa: E402
from engraphis.service import MemoryService  # noqa: E402


class _WorkingLLM:
    provider = "openrouter"
    model = "test/structured-model"

    def __init__(self, *args, **kwargs):
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        self.closed = True

    def ping(self):
        return {"ok": True, "reply": "ok", "error": "",
                "provider": self.provider, "model": self.model}

    def extract_json(self, prompt, schema):
        assert "facts" in schema.get("properties", {})
        return {"facts": [
            {
                "content": "Engraphis stores durable memories in SQLite.",
                "title": "Durable storage",
                "mtype": "semantic",
                "importance": 0.8,
                "keywords": ["Engraphis", "SQLite"],
                "entities": ["Engraphis", "SQLite"],
                "relations": [
                    {"source": "Engraphis", "relation": "stores in", "target": "SQLite"},
                ],
            },
            {
                "content": "Structured extraction writes individually recallable facts.",
                "title": "Structured extraction",
                "mtype": "semantic",
                "importance": 0.7,
                "keywords": ["extraction", "recall"],
                "entities": ["Structured extraction"],
                "relations": [],
            },
        ]}


def _client(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "llm_provider", _WorkingLLM.provider)
    monkeypatch.setattr(settings, "llm_model", _WorkingLLM.model)
    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    monkeypatch.setattr(settings, "llm_base_url", "")
    monkeypatch.setattr(settings, "extractor", "none")
    monkeypatch.setattr(settings, "llm_auto_extract", True)
    monkeypatch.setenv("ENGRAPHIS_EXTRACTOR", "none")
    monkeypatch.setenv("ENGRAPHIS_LLM_AUTO_EXTRACT", "1")
    from engraphis.llm import client as llm_client
    monkeypatch.setattr(llm_client, "LLMClient", _WorkingLLM)
    from engraphis.routes import v2_api
    for key, value in {"ok": False, "provider": "", "model": "", "tested_at": 0.0}.items():
        monkeypatch.setitem(v2_api._llm_connection_state, key, value)
    svc = MemoryService.create(":memory:", embed_model="", extractor="none",
                               graph_extractor="none")
    v2_api.set_service(svc)
    app = FastAPI()
    app.include_router(v2_api.router)
    return TestClient(app), svc


def test_successful_connection_auto_enables_extractor_and_activity_is_explainable(
        monkeypatch, tmp_path):
    client, svc = _client(monkeypatch, tmp_path)

    tested = client.post("/api/llm/test")
    assert tested.status_code == 200
    assert tested.json()["ok"] is True
    assert "reply" not in tested.json()
    assert tested.json()["extractor_enabled"] is True
    assert tested.json()["auto_enabled"] is True
    assert svc.engine.extractor is not None
    status = client.get("/api/llm/status").json()
    assert status["working"] is True
    assert status["extractor"] == "llm_structured"
    assert "base_url" not in status

    persisted = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "ENGRAPHIS_EXTRACTOR=llm_structured" in persisted
    assert "ENGRAPHIS_LLM_AUTO_EXTRACT=1" in persisted
    assert "test-key" not in persisted

    result = svc.ingest(
        "A long source note about SQLite and structured extraction.",
        workspace="demo",
        scope="workspace",
        source="human",
    )
    assert result["extracted"] is True
    assert result["count"] == 2

    activity = client.get("/api/llm/activity?workspace=demo").json()
    assert activity["count"] == 2
    assert {item["action"] for item in activity["activities"]} == {"extracted"}
    first = activity["activities"][0]
    assert first["provider"] == _WorkingLLM.provider
    assert first["model"] == _WorkingLLM.model
    assert first["fact_count"] == 2
    assert first["fact_index"] in (1, 2)
    assert any("SQLite" in item["entities"] for item in activity["activities"])


def test_manual_off_prevents_reenable_until_user_turns_extractor_back_on(
        monkeypatch, tmp_path):
    client, svc = _client(monkeypatch, tmp_path)
    assert client.post("/api/llm/test").json()["extractor_enabled"] is True

    disabled = client.post("/api/llm/extractor", json={"enabled": False})
    assert disabled.status_code == 200
    assert disabled.json()["extractor_enabled"] is False
    assert svc.engine.extractor.extract("disabled") == []
    assert settings.llm_auto_extract is False
    persisted = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "ENGRAPHIS_EXTRACTOR=none" in persisted
    assert "ENGRAPHIS_LLM_AUTO_EXTRACT=0" in persisted

    # A later health check still verifies the provider, but respects the explicit off.
    retested = client.post("/api/llm/test").json()
    assert retested["ok"] is True
    assert retested["extractor_enabled"] is False
    assert retested["auto_enabled"] is False

    enabled = client.post("/api/llm/extractor", json={"enabled": True})
    assert enabled.status_code == 200
    assert enabled.json()["extractor_enabled"] is True
    assert enabled.json()["persisted"] is True
    assert svc.engine.extractor is not None
    assert settings.llm_auto_extract is True


def test_extractor_cannot_be_enabled_without_an_api_key(monkeypatch, tmp_path):
    client, svc = _client(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "llm_api_key", "")

    response = client.post("/api/llm/extractor", json={"enabled": True})
    assert response.status_code == 400
    assert "API key" in response.json()["detail"]["error"]
    assert svc.engine.extractor is None


def test_extractor_enable_maps_provider_constructor_failure_to_safe_400(monkeypatch, tmp_path):
    client, svc = _client(monkeypatch, tmp_path)
    from engraphis.llm import client as llm_client

    class _BrokenLLM:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("provider-internal-secret")

    monkeypatch.setattr(llm_client, "LLMClient", _BrokenLLM)
    response = client.post("/api/llm/extractor", json={"enabled": True})

    assert response.status_code == 400
    assert "could not be verified" in response.json()["detail"]["error"]
    assert "provider-internal-secret" not in response.text
    assert client.get("/api/llm/status").json()["working"] is False
    assert svc.engine.extractor is None


def test_dashboard_warns_when_auto_enabled_extractor_cannot_persist():
    from pathlib import Path

    dashboard = (Path(__file__).resolve().parents[1]
                 / "engraphis" / "static" / "dashboard.js").read_text(encoding="utf-8")
    assert "d.auto_enabled&&d.persisted===false" in dashboard
    assert "could not be saved for restart" in dashboard
    assert "ENGRAPHIS_EXTRACTOR=llm_structured" in dashboard
    assert "ENGRAPHIS_LLM_AUTO_EXTRACT=1" in dashboard
    assert "d.reply" not in dashboard
    assert "replied:" not in dashboard


def test_llm_http_status_and_test_allowlist_hide_endpoint_and_provider_payload(
        monkeypatch, tmp_path):
    client, _svc = _client(monkeypatch, tmp_path)
    marker = "private-provider-route-and-reply-secret"
    monkeypatch.setattr(
        settings, "llm_base_url", "https://provider.example/%s" % marker
    )
    monkeypatch.setattr(settings, "llm_auto_extract", False)

    from engraphis.llm import client as llm_client

    class _HostilePing(_WorkingLLM):
        def ping(self):
            return {
                "ok": True,
                "reply": marker,
                "base_url": "https://user:%s@provider.example" % marker,
                "unexpected": marker,
                "provider": marker,
                "model": marker,
            }

    monkeypatch.setattr(llm_client, "LLMClient", _HostilePing)
    tested = client.post("/api/llm/test")
    status = client.get("/api/llm/status")

    assert tested.status_code == 200
    assert set(tested.json()) == {
        "ok", "provider", "model", "extractor", "extractor_enabled",
        "auto_extract", "auto_enabled",
    }
    assert tested.json()["provider"] == _WorkingLLM.provider
    assert tested.json()["model"] == _WorkingLLM.model
    assert status.json()["custom_base_url_configured"] is True
    assert "base_url" not in status.json()
    assert marker not in tested.text
    assert marker not in status.text

    class _HostileFailure(_WorkingLLM):
        def ping(self):
            return {
                "ok": False,
                "error": marker,
                "reply": marker,
                "base_url": marker,
                "provider": marker,
                "model": marker,
            }

    monkeypatch.setattr(llm_client, "LLMClient", _HostileFailure)
    failed = client.post("/api/llm/test")
    assert failed.status_code == 200
    assert set(failed.json()) == {
        "ok", "provider", "model", "extractor", "extractor_enabled",
        "auto_extract", "auto_enabled", "error",
    }
    assert failed.json()["error"].startswith("The provider test failed")
    assert marker not in failed.text
