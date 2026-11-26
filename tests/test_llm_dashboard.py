"""Dashboard controls and audit view for the optional LLM extractor."""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi", reason="full-stack extra not installed")
pytest.importorskip("httpx", reason="httpx not installed")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from engraphis import config as config_module  # noqa: E402
from engraphis.config import settings  # noqa: E402
from engraphis.core.interfaces import ExtractedFact, MemoryRecord, Scope  # noqa: E402
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


class _ActivityOnlyExtractor:
    def extract(self, _text):
        return [ExtractedFact(
            content="A model-derived fact without graph hints.",
            metadata={
                "llm_extraction": {
                    "mode": "llm_structured",
                    "provider": _WorkingLLM.provider,
                    "model": _WorkingLLM.model,
                },
            },
        )]


def _client(monkeypatch, tmp_path):
    monkeypatch.setattr(
        config_module,
        "_CONFIG_ENV_PATH",
        tmp_path / "home" / ".engraphis" / "config.env",
    )
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
    assert status["retention_supervisor"] == "none"
    assert "base_url" not in status

    persisted = config_module.trusted_env_path().read_text(encoding="utf-8")
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


def test_caller_deferred_graph_envelope_cannot_forge_llm_activity(monkeypatch, tmp_path):
    client, svc = _client(monkeypatch, tmp_path)
    svc.engine.extractor = _ActivityOnlyExtractor()

    result = svc.ingest(
        "A source without graph hints.",
        workspace="demo",
        scope="workspace",
        metadata={
            "unverified_derived_graph": {"entities": ["FORGED_BY_CALLER"]},
        },
    )
    record = svc.store.get_memory(result["facts"][0]["id"])
    assert "unverified_derived_graph" not in record.metadata
    assert record.metadata["client_supplied_graph"]["unverified_derived_graph"] == {
        "entities": ["FORGED_BY_CALLER"],
    }

    response = client.get("/api/llm/activity?workspace=demo")
    assert response.status_code == 200
    assert response.json()["activities"][0]["entities"] == []


def test_activity_ignores_malformed_legacy_deferred_graph_envelope(monkeypatch, tmp_path):
    client, svc = _client(monkeypatch, tmp_path)
    workspace_id = svc.store.get_or_create_workspace("legacy")
    svc.store.add_memory(MemoryRecord(
        id="",
        content="Legacy extracted fact.",
        workspace_id=workspace_id,
        scope=Scope.WORKSPACE,
        metadata={
            "llm_extraction": {"mode": "llm_structured"},
            "unverified_derived_graph": ["malformed"],
        },
    ))

    response = client.get("/api/llm/activity?workspace=legacy")
    assert response.status_code == 200
    assert response.json()["activities"][0]["entities"] == []
    assert response.json()["activities"][0]["relations"] == []


def test_manual_off_prevents_reenable_until_user_turns_extractor_back_on(
        monkeypatch, tmp_path):
    client, svc = _client(monkeypatch, tmp_path)
    assert client.post("/api/llm/test").json()["extractor_enabled"] is True

    disabled = client.post("/api/llm/extractor", json={"enabled": False})
    assert disabled.status_code == 200
    assert disabled.json()["extractor_enabled"] is False
    assert svc.engine.extractor.extract("disabled") == []
    assert settings.llm_auto_extract is False
    persisted = config_module.trusted_env_path().read_text(encoding="utf-8")
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
                 / "engraphis" / "dashboard_assets" / "ledger.js").read_text(encoding="utf-8")
    test_llm = dashboard[dashboard.index("  async function testLlm()"):
                         dashboard.index("  function switchView(")]
    assert "result.auto_enabled && result.persisted === false" in test_llm
    assert "could not be saved for restart" in test_llm
    assert "ENGRAPHIS_EXTRACTOR=llm_structured" in test_llm
    assert "ENGRAPHIS_LLM_AUTO_EXTRACT=1" in test_llm
    assert "transient ? 'error' : 'ready'" in test_llm
    # The provider's own reply is never echoed into the page.
    assert "result.reply" not in dashboard
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


def test_managed_cloud_session_provides_zero_key_llm_for_pro_and_team(monkeypatch, tmp_path):
    client, _svc = _client(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "llm_provider", "openai")
    monkeypatch.setattr(settings, "llm_model", "gpt-4o-mini")
    monkeypatch.setattr(settings, "llm_api_key", "")
    monkeypatch.setattr(settings, "llm_base_url", "")

    from engraphis.llm import client as llm_client

    monkeypatch.setattr(llm_client, "managed_cloud_llm_available", lambda: True)

    class _ManagedLLM(_WorkingLLM):
        provider = "managed"
        model = llm_client.DEFAULT_MANAGED_MODEL

    monkeypatch.setattr(llm_client, "LLMClient", _ManagedLLM)

    status = client.get("/api/llm/status").json()
    assert status["provider"] == "managed"
    assert status["model"] == llm_client.DEFAULT_MANAGED_MODEL
    assert status["key_set"] is True
    assert status["configured"] is True
    assert "ENGRAPHIS_LLM_PROVIDER=managed" in status["env_snippet"]
    assert "ENGRAPHIS_LLM_API_KEY=" not in status["env_snippet"]

    tested = client.post("/api/llm/test")
    assert tested.status_code == 200
    assert tested.json()["ok"] is True
    assert tested.json()["provider"] == "managed"
    assert tested.json()["model"] == llm_client.DEFAULT_MANAGED_MODEL
    assert tested.json()["extractor_enabled"] is True


def test_managed_llm_client_authenticates_with_rotating_cloud_session(monkeypatch):
    from engraphis import cloud_session, hosted_client
    from engraphis.llm import client as llm_client

    monkeypatch.setattr(cloud_session, "configured", lambda require_compute=False: True)
    monkeypatch.setattr(
        cloud_session,
        "credential_bound_control_url",
        lambda: "https://api.engraphis.com",
    )
    monkeypatch.setattr(
        cloud_session,
        "saved_entitlement",
        lambda: {"plan": "pro", "status": "active", "cloud_access_active": True},
    )
    monkeypatch.setattr(
        hosted_client,
        "validate_cloud_base_url",
        lambda url: url,
    )
    monkeypatch.setattr(
        cloud_session,
        "access_for_workspace",
        lambda ws, require_compute=False, deadline=None: (
            "rotating-cloud-token",
            "org_pro",
            None,
        ),
    )

    captured = {}

    def _fake_post(self, url, body, headers, *, timeout=None):
        captured["url"] = url
        captured["body"] = body
        captured["auth"] = headers.get("Authorization")
        return {"choices": [{"message": {"content": "managed-reply"}}]}

    monkeypatch.setattr(llm_client.LLMClient, "_post_json", _fake_post)

    with llm_client.LLMClient(provider="managed", api_key="") as llm:
        assert llm.provider == "managed"
        assert llm.model == llm_client.DEFAULT_MANAGED_MODEL
        reply = llm.chat([{"role": "user", "content": "Hello"}])
        assert reply == "managed-reply"
        assert captured["url"] == "https://api.engraphis.com/v1/llm/chat/completions"
        assert captured["auth"] == "Bearer rotating-cloud-token"
        assert captured["body"]["model"] == llm_client.DEFAULT_MANAGED_MODEL


def test_pro_and_team_paid_or_trial_auto_activates_structured_extractor_proactive_and_jev(
    monkeypatch, tmp_path
):
    from engraphis import cloud_session
    from engraphis.backends.extractor import StructuredLLMExtractor, get_extractor
    from engraphis.backends.jev_transport import EngraphisCloudDecisionClient, select_decision_client
    from engraphis.llm import client as llm_client
    from engraphis.service import MemoryService

    monkeypatch.delenv("ENGRAPHIS_EXTRACTOR", raising=False)
    monkeypatch.delenv("ENGRAPHIS_DECISION_BACKEND", raising=False)
    monkeypatch.setattr(settings, "llm_api_key", "")
    monkeypatch.setattr(cloud_session, "configured", lambda require_compute=False: True)
    monkeypatch.setattr(
        cloud_session,
        "credential_bound_control_url",
        lambda: "https://api.engraphis.com",
    )
    monkeypatch.setattr(
        cloud_session,
        "saved_entitlement",
        lambda: {"plan": "pro", "status": "trialing", "cloud_access_active": True},
    )

    assert llm_client.managed_cloud_llm_available() is True

    # 1. Engine lazily activates managed StructuredLLMExtractor on ingest for Pro/Team trial/paid,
    #    and proactive_context(synthesize=True) uses the managed LLM with zero API key.
    extracted_calls = []
    chat_calls = []

    class _ManagedExtractLLM(_WorkingLLM):
        provider = "managed"
        model = llm_client.DEFAULT_MANAGED_MODEL
        api_key = ""
        has_credentials = True

        def extract_json(self, prompt, schema, *, timeout=None):
            extracted_calls.append(prompt)
            return {
                "facts": [
                    {
                        "content": "Billing ledger uses PostgreSQL 16.",
                        "title": "Billing DB",
                        "mtype": "semantic",
                        "importance": 0.9,
                        "keywords": ["postgresql", "billing"],
                        "entities": ["PostgreSQL 16", "billing ledger"],
                        "relations": [
                            {
                                "source": "billing ledger",
                                "relation": "uses",
                                "target": "PostgreSQL 16",
                            }
                        ],
                    }
                ]
            }

        def chat(self, messages, *, system=None, temperature=None, max_tokens=None, timeout=None):
            chat_calls.append(messages)
            return "Use PostgreSQL 16 for the billing ledger [1]."

    monkeypatch.setattr(llm_client, "LLMClient", _ManagedExtractLLM)
    svc = MemoryService.create(str(tmp_path / "auto_trial.db"), embed_model="", extractor="none")
    try:
        assert svc.engine.extractor is None  # No eager credential inspection at create time.
        ingested = svc.ingest(
            "Team decided to run PostgreSQL 16 for the billing ledger.",
            workspace="ws_trial",
        )
        assert len(extracted_calls) == 1
        assert ingested["extracted"] is True
        assert ingested["count"] == 1
        stored_id = ingested["facts"][0]["id"]
        stored_rec = svc.store.get_memory(stored_id)
        assert stored_rec is not None and stored_rec.title == "Billing DB"

        svc.remember("Billing ledger runs on PostgreSQL 16.", workspace="ws_trial")
        briefing = svc.proactive_context(
            workspace="ws_trial", task="Check billing database choice", synthesize=True
        )
        assert len(chat_calls) == 1
        assert briefing["synthesized"] is True
        assert "[1]" in briefing["context_summary"]
    finally:
        svc.close()

    # 2. Strict require_exact mode accepts zero-key managed LLM.
    strict_ext = get_extractor("llm_structured", require_exact=True)
    assert isinstance(strict_ext, StructuredLLMExtractor)
    assert strict_ext.llm.provider == "managed"
    assert strict_ext.llm.has_credentials is True
    strict_ext.llm.close()

    # 3. Managed Jev auto-selects without manual ENGRAPHIS_DECISION_BACKEND env var.
    jev_client, backend_name = select_decision_client()
    assert isinstance(jev_client, EngraphisCloudDecisionClient)
    assert backend_name == "engraphis_cloud"

    # 4. Expired or free entitlement disables automatic activation cleanly.
    monkeypatch.setattr(
        cloud_session,
        "saved_entitlement",
        lambda: {"plan": "pro", "status": "expired", "cloud_access_active": False},
    )
    assert llm_client.managed_cloud_llm_available() is False
    assert select_decision_client() == (None, "local_heuristic")


def test_byok_key_suppresses_auto_managed_extractor_when_extractor_unset(monkeypatch, tmp_path):
    """A connected Pro/Team user with a personal BYOK key and unset ENGRAPHIS_EXTRACTOR must not bill BYOK on ingest."""
    from engraphis import cloud_session
    from engraphis.llm import client as llm_client
    from engraphis.service import MemoryService

    monkeypatch.delenv("ENGRAPHIS_EXTRACTOR", raising=False)
    monkeypatch.setattr(settings, "llm_provider", "openai")
    monkeypatch.setattr(settings, "llm_api_key", "sk-personal-byok-key")
    monkeypatch.setattr(settings, "llm_base_url", "")
    monkeypatch.setattr(cloud_session, "configured", lambda *, require_compute=True: True)
    monkeypatch.setattr(
        cloud_session,
        "credential_bound_control_url",
        lambda: "https://api.engraphis.com",
    )
    monkeypatch.setattr(
        cloud_session,
        "saved_entitlement",
        lambda: {"plan": "pro", "status": "trialing", "cloud_access_active": True},
    )

    assert llm_client.managed_cloud_llm_available() is True
    assert llm_client.auto_managed_llm_selected() is False

    def _forbidden_llm(*_args, **_kwargs):
        raise AssertionError("BYOK LLMClient must not be constructed automatically during ingest")

    monkeypatch.setattr(llm_client, "LLMClient", _forbidden_llm)
    svc = MemoryService.create(str(tmp_path / "byok_isolated.db"), embed_model="", extractor="none")
    try:
        ingested = svc.ingest("Keep this local unless ENGRAPHIS_EXTRACTOR is set.", workspace="ws_byok")
        assert ingested["extracted"] is False
    finally:
        svc.close()


def test_managed_provider_never_sends_byok_api_key_to_cloud(monkeypatch):
    """Explicit provider=managed must ignore ENGRAPHIS_LLM_API_KEY and always use Cloud session auth."""
    from engraphis import cloud_session
    from engraphis.llm import client as llm_client

    monkeypatch.setattr(settings, "llm_provider", "managed")
    monkeypatch.setattr(settings, "llm_api_key", "sk-personal-secret-never-send")
    monkeypatch.setattr(settings, "llm_base_url", "https://custom.byok.example/v1")
    monkeypatch.setattr(
        settings,
        "llm_extra_headers",
        {"X-API-Key": "sk-header-secret", "Authorization": "Bearer sk-override"},
    )
    monkeypatch.setattr(cloud_session, "configured", lambda *, require_compute=True: True)
    monkeypatch.setattr(
        cloud_session,
        "credential_bound_control_url",
        lambda: "https://api.engraphis.com",
    )
    monkeypatch.setattr(
        cloud_session,
        "saved_entitlement",
        lambda: {"plan": "pro", "status": "active", "cloud_access_active": True},
    )

    captured: dict = {}

    def _fake_managed_auth(self, timeout=None):
        return ("cloud-rotating-token", "https://api.engraphis.com/v1/llm")

    def _fake_compat(
        self,
        messages,
        system=None,
        temperature=None,
        max_tokens=None,
        timeout=None,
        *,
        api_key_override=None,
        base_url_override=None,
    ):
        captured["api_key_override"] = api_key_override
        captured["base_url_override"] = base_url_override
        captured["self_api_key"] = self.api_key
        return "managed-ok"

    monkeypatch.setattr(llm_client.LLMClient, "_resolve_managed_auth", _fake_managed_auth)
    monkeypatch.setattr(llm_client.LLMClient, "_chat_openai_compat", _fake_compat)

    with llm_client.LLMClient() as llm:
        assert llm.provider == "managed"
        assert llm.api_key == ""
        assert llm.extra_headers == {}
        assert llm.has_credentials is True
        reply = llm.chat([{"role": "user", "content": "ping"}])

    assert reply == "managed-ok"
    assert captured["self_api_key"] == ""
    assert captured["api_key_override"] == "cloud-rotating-token"
    assert captured["base_url_override"] == "https://api.engraphis.com/v1/llm"


@pytest.mark.parametrize("flag_value", ["0", "false", "no", "off"])
def test_auto_extract_env_opt_out_disables_managed_ingest_extraction(
    monkeypatch, tmp_path, flag_value
):
    """Explicit ENGRAPHIS_LLM_AUTO_EXTRACT=0/false/no/off opts out even when ENGRAPHIS_EXTRACTOR is unset."""
    from engraphis import cloud_session
    from engraphis.llm import client as llm_client
    from engraphis.service import MemoryService

    monkeypatch.delenv("ENGRAPHIS_EXTRACTOR", raising=False)
    monkeypatch.setenv("ENGRAPHIS_LLM_AUTO_EXTRACT", flag_value)
    monkeypatch.setattr(settings, "llm_provider", "openai")
    monkeypatch.setattr(settings, "llm_api_key", "")
    monkeypatch.setattr(settings, "llm_base_url", "")
    monkeypatch.setattr(cloud_session, "configured", lambda *, require_compute=True: True)
    monkeypatch.setattr(
        cloud_session,
        "credential_bound_control_url",
        lambda: "https://api.engraphis.com",
    )
    monkeypatch.setattr(
        cloud_session,
        "saved_entitlement",
        lambda: {"plan": "pro", "status": "trialing", "cloud_access_active": True},
    )

    assert llm_client.auto_managed_llm_selected() is True
    assert llm_client.auto_managed_extractor_allowed() is False

    def _forbidden_llm(*_args, **_kwargs):
        raise AssertionError("Managed LLMClient must not run on ingest when ENGRAPHIS_LLM_AUTO_EXTRACT opts out")

    monkeypatch.setattr(llm_client, "LLMClient", _forbidden_llm)
    svc = MemoryService.create(str(tmp_path / "opt_out.db"), embed_model="", extractor="none")
    try:
        ingested = svc.ingest("Respect explicit opt-out on ingest.", workspace="ws_optout")
        assert ingested["extracted"] is False
    finally:
        svc.close()


@pytest.mark.parametrize(
    "entitlement",
    [
        {},
        {"plan": "free", "status": "active", "cloud_access_active": True},
        {"plan": "starter", "status": "active", "cloud_access_active": True},
        {"plan": "pro", "status": "active", "cloud_access_active": False},
        {"plan": "pro", "status": "past_due", "cloud_access_active": True},
        {"plan": "team", "status": "revoked", "cloud_access_active": True},
    ],
)
def test_managed_llm_and_jev_fail_closed_without_positive_paid_entitlement(
    monkeypatch, entitlement
):
    """Auto-activation requires a positively verified active/trialing Pro or Team entitlement."""
    from engraphis import cloud_session
    from engraphis.backends.jev_transport import select_decision_client
    from engraphis.llm import client as llm_client

    monkeypatch.delenv("ENGRAPHIS_DECISION_BACKEND", raising=False)
    monkeypatch.setattr(cloud_session, "configured", lambda *, require_compute=True: True)
    monkeypatch.setattr(
        cloud_session,
        "credential_bound_control_url",
        lambda: "https://api.engraphis.com",
    )
    monkeypatch.setattr(cloud_session, "saved_entitlement", lambda: entitlement)

    assert llm_client.managed_cloud_llm_available() is False
    assert select_decision_client() == (None, "local_heuristic")


def test_extractor_enabled_is_false_in_strict_exact_mode(monkeypatch, tmp_path):
    """When require_exact_backends=True disables auto_extractor_factory, _extractor_enabled() reports False."""
    from engraphis import cloud_session
    from engraphis.routes import v2_api
    from engraphis.service import MemoryService

    monkeypatch.delenv("ENGRAPHIS_EXTRACTOR", raising=False)
    monkeypatch.delenv("ENGRAPHIS_LLM_AUTO_EXTRACT", raising=False)
    monkeypatch.setattr(settings, "llm_provider", "openai")
    monkeypatch.setattr(settings, "llm_api_key", "")
    monkeypatch.setattr(settings, "llm_base_url", "")
    monkeypatch.setattr(cloud_session, "configured", lambda *, require_compute=True: True)
    monkeypatch.setattr(
        cloud_session,
        "credential_bound_control_url",
        lambda: "https://api.engraphis.com",
    )
    monkeypatch.setattr(
        cloud_session,
        "saved_entitlement",
        lambda: {"plan": "pro", "status": "active", "cloud_access_active": True},
    )

    svc = MemoryService.create(
        str(tmp_path / "strict_exact.db"),
        embed_model="",
        extractor="none",
        require_exact_backends=True,
    )
    try:
        monkeypatch.setattr(v2_api, "service", lambda: svc)
        assert svc.engine._auto_extractor_factory is None
        assert v2_api._extractor_enabled() is False
    finally:
        svc.close()


@pytest.mark.parametrize(
    "stale_entitlement",
    [
        {},
        {"plan": "pro", "status": "expired", "cloud_access_active": False},
        {"plan": "pro", "status": "past_due", "cloud_access_active": True},
    ],
)
def test_explicit_managed_provider_recovers_from_stale_or_empty_entitlement_cache(
    monkeypatch, stale_entitlement
):
    """Explicit provider=managed must allow live Cloud token refresh on stale/empty cache while auto-activation stays fail-closed."""
    from engraphis import cloud_session, hosted_client
    from engraphis.llm import client as llm_client
    from engraphis.routes import v2_api

    monkeypatch.setattr(settings, "llm_provider", "managed")
    monkeypatch.setattr(settings, "llm_api_key", "")
    monkeypatch.setattr(settings, "llm_base_url", "")
    monkeypatch.setattr(hosted_client, "validate_cloud_base_url", lambda url: url.rstrip("/"))
    monkeypatch.setattr(cloud_session, "configured", lambda *, require_compute=True: True)
    monkeypatch.setattr(
        cloud_session,
        "credential_bound_control_url",
        lambda: "https://api.engraphis.com",
    )
    monkeypatch.setattr(cloud_session, "saved_entitlement", lambda: stale_entitlement)

    calls: list[bool] = []

    def _fake_access(workspace, *, require_compute=True, deadline=None):
        calls.append(require_compute)
        return ("fresh-cloud-token", "org_1", "")

    monkeypatch.setattr(cloud_session, "access_for_workspace", _fake_access)

    # Implicit auto-activation remains fail-closed on stale/empty cache.
    assert llm_client.managed_cloud_llm_available() is False
    assert llm_client.auto_managed_llm_selected() is False
    assert llm_client.auto_managed_extractor_allowed() is False

    # Explicit provider=managed allows live token exchange to refresh entitlement.
    assert llm_client.managed_cloud_session_configured() is True
    provider, model, key_set, is_managed = v2_api._effective_llm_config()
    assert (provider, model, key_set, is_managed) == (
        "managed",
        llm_client.DEFAULT_MANAGED_MODEL,
        True,
        True,
    )

    # Works both with explicit provider= kwarg and default from settings.llm_provider.
    with llm_client.LLMClient() as llm:
        assert llm.provider == "managed"
        assert llm.has_credentials is True
        token, url = llm._resolve_managed_auth(timeout=5.0)
        assert token == "fresh-cloud-token"
        assert url == "https://api.engraphis.com/v1/llm"

    # Implicit provider=openai stays non-managed on stale/empty cache.
    monkeypatch.setattr(settings, "llm_provider", "openai")
    monkeypatch.setattr(settings, "llm_model", "gpt-4o-mini")
    assert v2_api._effective_llm_config() == ("openai", "gpt-4o-mini", False, False)

    assert calls == [False]


@pytest.mark.parametrize(
    ("is_configured", "control_url"),
    [
        (False, ""),
        (True, ""),
        (False, "https://api.engraphis.com"),
    ],
)
def test_explicit_managed_provider_fails_when_cloud_session_unconfigured(
    monkeypatch, tmp_path, is_configured, control_url
):
    """Explicit provider=managed rejects partial/missing Cloud sessions and ignores BYOK keys."""
    from engraphis import cloud_session
    from engraphis.llm import client as llm_client
    from engraphis.routes import v2_api

    real_llm_cls = llm_client.LLMClient
    client, _svc = _client(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "llm_provider", "managed")
    monkeypatch.setattr(settings, "llm_api_key", "sk-byok-must-not-satisfy-managed")
    monkeypatch.setattr(cloud_session, "configured", lambda *, require_compute=True: is_configured)
    monkeypatch.setattr(cloud_session, "credential_bound_control_url", lambda: control_url)

    assert llm_client.managed_cloud_session_configured() is False
    assert v2_api._effective_llm_config()[2] is False

    resp = client.post("/api/llm/extractor", json={"enabled": True})
    assert resp.status_code == 400
    assert "engraphis-connect" in resp.json()["detail"]["error"]

    with real_llm_cls(provider="managed") as llm:
        assert llm.has_credentials is False
        with pytest.raises(ValueError, match="connected Engraphis Cloud session"):
            llm._resolve_managed_auth(timeout=5.0)


def test_dashboard_http_routes_recover_or_deny_explicit_managed_provider_on_stale_cache(
    monkeypatch, tmp_path
):
    """HTTP /api/llm/status, /api/llm/test, and /api/llm/extractor honor live Cloud recovery and 402 denial."""
    from engraphis import cloud_session, hosted_client
    from engraphis.llm import client as llm_client
    from engraphis.routes import v2_api

    real_llm_cls = llm_client.LLMClient
    monkeypatch.setattr(hosted_client, "validate_cloud_base_url", lambda url: url.rstrip("/"))

    monkeypatch.setattr(
        config_module,
        "_CONFIG_ENV_PATH",
        tmp_path / "home" / ".engraphis" / "config.env",
    )
    monkeypatch.setattr(settings, "llm_provider", "managed")
    monkeypatch.setattr(settings, "llm_model", "gpt-4o-mini")
    monkeypatch.setattr(settings, "llm_api_key", "")
    monkeypatch.setattr(settings, "llm_base_url", "https://custom.byok.example/v1")
    monkeypatch.setattr(settings, "extractor", "none")
    monkeypatch.setattr(settings, "llm_auto_extract", True)
    monkeypatch.setenv("ENGRAPHIS_EXTRACTOR", "none")
    monkeypatch.setenv("ENGRAPHIS_LLM_AUTO_EXTRACT", "1")
    for key, value in {"ok": False, "provider": "", "model": "", "tested_at": 0.0}.items():
        monkeypatch.setitem(v2_api._llm_connection_state, key, value)
    svc = MemoryService.create(":memory:", embed_model="", extractor="none", graph_extractor="none")
    try:
        v2_api.set_service(svc)
        app = FastAPI()
        app.include_router(v2_api.router)
        http = TestClient(app)

        monkeypatch.setattr(cloud_session, "configured", lambda *, require_compute=True: True)
        monkeypatch.setattr(
            cloud_session,
            "credential_bound_control_url",
            lambda: "https://api.engraphis.com",
        )
        monkeypatch.setattr(
            cloud_session,
            "saved_entitlement",
            lambda: {"plan": "pro", "status": "expired", "cloud_access_active": False},
        )
        denial_recorded: list[bool] = []
        monkeypatch.setattr(
            cloud_session,
            "record_billing_denial",
            lambda: (denial_recorded.append(True) or True),
        )

        # 1. Status reflects configured managed provider without auto-enabling extraction or claiming custom URL.
        status_before = http.get("/api/llm/status").json()
        assert status_before["provider"] == "managed"
        assert status_before["key_set"] is True
        assert status_before["custom_base_url_configured"] is False
        assert status_before["working"] is False
        assert status_before["extractor_enabled"] is False

        # 2. Live 402 denial fails closed on both /api/llm/test and /api/llm/extractor and records denial.
        class _Denied402(RuntimeError):
            status = 402

        monkeypatch.setattr(
            cloud_session,
            "access_for_workspace",
            lambda *_a, **_kw: (_ for _ in ()).throw(_Denied402("payment required")),
        )
        denied_test = http.post("/api/llm/test").json()
        assert denied_test["ok"] is False
        assert denied_test["error"]
        assert "payment required" not in denied_test["error"]
        assert denied_test["extractor_enabled"] is False
        assert len(denial_recorded) >= 1

        denied_enable = http.post("/api/llm/extractor", json={"enabled": True})
        assert denied_enable.status_code == 400
        assert svc.engine.extractor is None

        # 3. Live refresh recovery succeeds after paid upgrade and enables extraction.
        monkeypatch.setattr(
            cloud_session,
            "access_for_workspace",
            lambda *_a, **_kw: ("recovered-token", "org_1", ""),
        )
        monkeypatch.setattr(
            real_llm_cls,
            "_chat_openai_compat",
            lambda self, *a, **kw: "ok",
        )
        recovered = http.post("/api/llm/test").json()
        assert recovered["ok"] is True
        assert recovered["provider"] == "managed"
        assert recovered["extractor_enabled"] is True

        enabled = http.post("/api/llm/extractor", json={"enabled": True})
        assert enabled.status_code == 200
        assert enabled.json()["ok"] is True
    finally:
        svc.close()


def test_explicit_managed_provider_rejects_control_url_drift_and_runs_proactive_synthesis(
    monkeypatch, tmp_path
):
    """Control-URL drift raises 401 and proactive_context(synthesize=True) honors explicit managed recovery."""
    from engraphis import ai_context, cloud_session, hosted_client
    from engraphis.llm import client as llm_client

    monkeypatch.setattr(settings, "llm_provider", "managed")
    monkeypatch.setattr(settings, "llm_api_key", "")
    monkeypatch.setattr(hosted_client, "validate_cloud_base_url", lambda url: url.rstrip("/"))
    monkeypatch.setattr(cloud_session, "configured", lambda *, require_compute=True: True)
    monkeypatch.setattr(
        cloud_session,
        "saved_entitlement",
        lambda: {"plan": "pro", "status": "expired", "cloud_access_active": False},
    )

    urls = iter(["https://api.engraphis.com", "https://api.engraphis.com", "https://evil.example"])
    monkeypatch.setattr(cloud_session, "credential_bound_control_url", lambda: next(urls))
    access_calls: list[int] = []

    def _fake_access(*_a, **_kw):
        access_calls.append(1)
        return ("tok", "org_1", "")

    monkeypatch.setattr(cloud_session, "access_for_workspace", _fake_access)
    with llm_client.LLMClient(provider="managed") as llm:
        with pytest.raises(llm_client._LLMProviderError) as exc_info:
            llm._resolve_managed_auth(timeout=5.0)
        assert exc_info.value.status == 401
    assert access_calls == [1]

    # Verify MemoryService.proactive_context(synthesize=True) passes an LLMClient on explicit managed.
    monkeypatch.setattr(
        cloud_session,
        "credential_bound_control_url",
        lambda: "https://api.engraphis.com",
    )
    seen_llm: list[object] = []
    orig_build = ai_context.build_proactive_context

    def _spy_build(**kwargs):
        seen_llm.append(kwargs.get("llm"))
        return orig_build(**{**kwargs, "llm": None, "synthesize": False})

    monkeypatch.setattr(ai_context, "build_proactive_context", _spy_build)
    svc = MemoryService.create(str(tmp_path / "proactive_managed.db"), embed_model="", extractor="none")
    try:
        svc.remember("Migrated auth tokens to PASETO.", workspace="demo")
        svc.proactive_context(task="check auth migration", workspace="demo", synthesize=True)
    finally:
        svc.close()
    assert len(seen_llm) == 1
    assert isinstance(seen_llm[0], llm_client.LLMClient)
    assert seen_llm[0].provider == "managed"

    # Negative branch: explicit managed with unconfigured session + BYOK key must NOT construct LLMClient.
    seen_llm.clear()
    monkeypatch.setattr(settings, "llm_api_key", "sk-byok-must-not-run")
    monkeypatch.setattr(cloud_session, "configured", lambda *, require_compute=True: False)
    svc2 = MemoryService.create(str(tmp_path / "proactive_unconfigured.db"), embed_model="", extractor="none")
    try:
        svc2.remember("Keep BYOK isolated.", workspace="demo")
        svc2.proactive_context(task="check isolation", workspace="demo", synthesize=True)
    finally:
        svc2.close()
    assert seen_llm == [None]


def test_managed_auth_classifies_offline_timeout_401_and_persists_chat_402_denial(monkeypatch):
    """Offline DNS failures report unreachable (not 401), TimeoutError/401 fail closed, and chat 402 records denial."""
    from engraphis import cloud_session, hosted_client
    from engraphis.llm import client as llm_client

    monkeypatch.setattr(settings, "llm_provider", "   ")
    monkeypatch.setattr(settings, "llm_api_key", "")
    monkeypatch.setattr(settings, "llm_base_url", "")
    monkeypatch.setattr(cloud_session, "configured", lambda *, require_compute=True: False)
    with llm_client.LLMClient(provider="   ") as ws_llm:
        assert ws_llm.provider == "openai"

    monkeypatch.setattr(settings, "llm_provider", "managed")
    monkeypatch.setattr(cloud_session, "configured", lambda *, require_compute=True: True)
    monkeypatch.setattr(
        cloud_session,
        "credential_bound_control_url",
        lambda: "https://api.engraphis.com",
    )

    # 1. Offline CloudUrlUnresolved maps to unreachable=True (not HTTP 401).
    def _dns_fail(_url: str) -> str:
        raise hosted_client.CloudUrlUnresolved("offline")

    monkeypatch.setattr(hosted_client, "validate_cloud_base_url", _dns_fail)
    with llm_client.LLMClient(provider="managed") as llm:
        with pytest.raises(llm_client._LLMProviderError) as exc_info:
            llm._resolve_managed_auth(timeout=5.0)
        assert exc_info.value.unreachable is True
        assert exc_info.value.status is None
        ping_res = llm.ping()
        assert ping_res["ok"] is False
        assert "Could not reach" in ping_res["error"]

    # 2. TimeoutError propagates cleanly.
    monkeypatch.setattr(hosted_client, "validate_cloud_base_url", lambda url: url.rstrip("/"))
    monkeypatch.setattr(
        cloud_session,
        "access_for_workspace",
        lambda *_a, **_kw: (_ for _ in ()).throw(TimeoutError("deadline exceeded")),
    )
    with llm_client.LLMClient(provider="managed") as llm:
        with pytest.raises(TimeoutError):
            llm._resolve_managed_auth(timeout=5.0)

    # 3. 401 refresh failure maps to status=401 without recording billing denial.
    denials: list[bool] = []
    monkeypatch.setattr(cloud_session, "record_billing_denial", lambda: (denials.append(True) or True))

    class _Unauthorized401(RuntimeError):
        status = 401

    monkeypatch.setattr(
        cloud_session,
        "access_for_workspace",
        lambda *_a, **_kw: (_ for _ in ()).throw(_Unauthorized401("revoked")),
    )
    with llm_client.LLMClient(provider="managed") as llm:
        with pytest.raises(llm_client._LLMProviderError) as exc_info:
            llm._resolve_managed_auth(timeout=5.0)
        assert exc_info.value.status == 401
    assert denials == []

    # 4. Managed chat() HTTP 402 records billing denial so cached entitlement flips inactive.
    monkeypatch.setattr(
        cloud_session,
        "access_for_workspace",
        lambda *_a, **_kw: ("tok", "org_1", ""),
    )
    monkeypatch.setattr(
        llm_client.LLMClient,
        "_chat_openai_compat",
        lambda self, *a, **kw: (_ for _ in ()).throw(llm_client._LLMProviderError(status=402)),
    )
    with llm_client.LLMClient(provider="managed") as llm:
        with pytest.raises(llm_client._LLMProviderError) as exc_info:
            llm.chat([{"role": "user", "content": "hi"}])
        assert exc_info.value.status == 402
    assert denials == [True]


