"""Opt-in Jev planning and dashboard review stay bounded and read-only."""
from __future__ import annotations

import time
import json
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi", reason="full-stack extra not installed")
pytest.importorskip("httpx", reason="httpx not installed")

from fastapi.testclient import TestClient  # noqa: E402

from engraphis.backends.jev_decision import JevDecisionBackend  # noqa: E402
from engraphis.backends.jev_query_planner import JevAssistedQueryPlanner  # noqa: E402
from engraphis.config import settings  # noqa: E402
from engraphis.core.interfaces import (  # noqa: E402
    GraphLayer,
    MemoryType,
    PlannedQuery,
    RetrievalPlan,
    Scope,
    SearchFilter,
)
from engraphis.service import MemoryService  # noqa: E402


class _Batch:
    is_fallback = False

    def __init__(self, *, confidence=0.9, malformed=False):
        self.confidence = confidence
        self.malformed = malformed

    def get_choice(self, question_id):
        if self.malformed:
            return SimpleNamespace(selected="unlisted-route", confidence=0.99)
        selected = "route_2" if question_id == "route" else "reinforces"
        return SimpleNamespace(selected=selected, confidence=self.confidence)

    def get_noul(self, _question_id):
        return SimpleNamespace(probability=0.9, confidence=self.confidence)


class _DecisionClient:
    is_configured = True
    allow_fallback = False

    def __init__(self, *, confidence=0.9, malformed=False, fail=False):
        self.calls = []
        self.confidence = confidence
        self.malformed = malformed
        self.fail = fail

    def evaluate(
        self, state, questions, *, model, allow_remote=False, purpose="custom",
        data_classification="internal", timeout_s=None,
    ):
        self.calls.append({
            "state": state,
            "questions": list(questions),
            "model": model,
            "allow_remote": allow_remote,
            "purpose": purpose,
            "data_classification": data_classification,
            "timeout_s": timeout_s,
        })
        if self.fail:
            raise RuntimeError("private synthetic failure")
        return _Batch(confidence=self.confidence, malformed=self.malformed)


class _FixedDeterministicPlanner:
    def __init__(self):
        self.filters = []
        self.filter_snapshots = []
        self.delay_s = 0.0

    def plan(self, query, *, filter=None, timeout_s=None, mode="auto"):
        if self.delay_s:
            time.sleep(self.delay_s)
        self.filters.append(filter)
        if filter is not None:
            self.filter_snapshots.append({
                "workspace_id": filter.workspace_id,
                "repo_id": filter.repo_id,
                "session_id": filter.session_id,
                "scopes": list(filter.scopes) if filter.scopes is not None else None,
                "mtypes": list(filter.mtypes) if filter.mtypes is not None else None,
                "graph_layers": list(filter.graph_layers) if filter.graph_layers is not None else None,
                "as_of": filter.as_of,
                "valid_at": filter.valid_at,
                "known_at": filter.known_at,
                "modified_since": filter.modified_since,
                "include_ancestors": filter.include_ancestors,
            })
            # A planner bug must not mutate the real retrieval boundary.
            if filter.scopes is not None:
                filter.scopes.clear()
            if filter.mtypes is not None:
                filter.mtypes.clear()
            if filter.graph_layers is not None:
                filter.graph_layers.clear()
            filter.repo_id = "repo_foreign"
            filter.valid_at = 999.0
            filter.known_at = 999.0
        assert mode == "auto"
        return RetrievalPlan((
            PlannedQuery(query.strip(), 1, "balanced"),
            PlannedQuery("CACHE.get()", 2, "lexical", (MemoryType.PROCEDURAL,)),
            PlannedQuery("cache restart path", 3, "graph"),
        ), reason_codes=("fixture_routes",))


def _backend(client):
    return JevDecisionBackend(client=client, model="test-model-1.0")


def test_route_choice_is_limited_to_deterministic_routes_and_keeps_exact_query():
    original = '  Why does CACHE.get() fail after restart?  '
    client = _DecisionClient()
    deterministic = _FixedDeterministicPlanner()
    planner = JevAssistedQueryPlanner(_backend(client), deterministic)

    baseline = planner.plan_with_jev(original, allow_remote=False)
    selected = planner.plan_with_jev(
        original, allow_remote=True, data_classification="public", timeout_s=0.8,
    )

    assert client.calls[0]["allow_remote"] is True
    assert client.calls[0]["data_classification"] == "public"
    assert 0 < client.calls[0]["timeout_s"] <= 0.8
    assert client.calls[0]["questions"][0].options == ("route_1", "route_2")
    assert f"ORIGINAL QUERY:\n{original}" in client.calls[0]["state"]
    assert "CACHE.get()" in client.calls[0]["state"]
    assert baseline.queries[0].text == original
    assert baseline.reason_codes[-1] == "jev_remote_consent_required"
    assert [route.text for route in selected.queries] == [
        original, "cache restart path", "CACHE.get()",
    ]
    assert len(selected.queries) <= 3
    assert selected.reason_codes[-1] == "jev_route_selected"


def test_single_alternate_route_skips_jev_without_provider_call():
    class SingleAlternativePlanner:
        identity = "fixture.single-alternative"

        def plan(self, query, *, filter=None, timeout_s=None, mode="auto"):
            del filter, timeout_s, mode
            return RetrievalPlan((
                PlannedQuery(query, 1, "balanced"),
                PlannedQuery("one deterministic alternate", 2, "lexical"),
            ))

    client = _DecisionClient()
    planner = JevAssistedQueryPlanner(_backend(client), SingleAlternativePlanner())
    plan = planner.plan_with_jev(
        "original query", allow_remote=True, data_classification="public",
    )

    assert plan.reason_codes[-1] == "jev_no_route_choice"
    assert [route.text for route in plan.queries] == [
        "original query", "one deterministic alternate",
    ]
    assert client.calls == []


def test_normal_planner_interface_remains_deterministic_and_never_calls_jev():
    client = _DecisionClient()
    deterministic = _FixedDeterministicPlanner()
    planner = JevAssistedQueryPlanner(_backend(client), deterministic)

    plan = planner.plan("Why does CACHE.get() fail?")

    assert [route.text for route in plan.queries] == [
        "Why does CACHE.get() fail?", "CACHE.get()", "cache restart path",
    ]
    assert client.calls == []


@pytest.mark.parametrize(
    ("client", "reason"),
    [
        (_DecisionClient(confidence=0.5), "jev_uncertain"),
        (_DecisionClient(malformed=True), "jev_malformed_response"),
        (_DecisionClient(fail=True), "jev_remote_unavailable"),
    ],
)
def test_uncertain_malformed_and_failed_jev_results_keep_deterministic_plan(client, reason):
    deterministic = _FixedDeterministicPlanner()
    planner = JevAssistedQueryPlanner(_backend(client), deterministic)
    original = "Why does CACHE.get() fail after restart?"

    fallback = planner.plan_with_jev(
        original, allow_remote=True, data_classification="internal", timeout_s=1.0,
    )
    expected = planner.plan(original)

    assert [route.text for route in fallback.queries] == [route.text for route in expected.queries]
    assert fallback.reason_codes[-1] == reason


def test_remote_route_call_requires_consent_classification_and_deadline_support():
    client = _DecisionClient()
    planner = JevAssistedQueryPlanner(_backend(client), _FixedDeterministicPlanner())

    denied = planner.plan_with_jev("Why does CACHE.get() fail?", allow_remote=False)
    invalid = planner.plan_with_jev(
        "Why does CACHE.get() fail?", allow_remote=True,
        data_classification="secret",
    )
    assert denied.reason_codes[-1] == "jev_remote_consent_required"
    assert invalid.reason_codes[-1] == "jev_invalid_classification"
    assert client.calls == []

    class LegacyClient:
        is_configured = True
        allow_fallback = False

        def evaluate(self, state, questions, *, model):
            pytest.fail("deadline-unsupported clients must not be invoked")

    legacy_planner = JevAssistedQueryPlanner(
        _backend(LegacyClient()), _FixedDeterministicPlanner(),
    )
    unsupported = legacy_planner.plan_with_jev(
        "Why does CACHE.get() fail?", allow_remote=True,
        data_classification="internal", timeout_s=0.5,
    )
    assert unsupported.reason_codes[-1] == "jev_client_deadline_unsupported"


def test_route_transport_timeout_subtracts_local_planner_time():
    client = _DecisionClient()
    deterministic = _FixedDeterministicPlanner()
    deterministic.delay_s = 0.05
    planner = JevAssistedQueryPlanner(_backend(client), deterministic)

    planner.plan_with_jev(
        "Why does CACHE.get() fail?", allow_remote=True,
        data_classification="internal", timeout_s=0.5,
    )

    assert len(client.calls) == 1
    assert 0 < client.calls[0]["timeout_s"] < 0.48


def test_core_planner_gets_cloned_scope_and_time_filters_without_trust_authority():
    client = _DecisionClient()
    deterministic = _FixedDeterministicPlanner()
    planner = JevAssistedQueryPlanner(_backend(client), deterministic)
    service = MemoryService.create(":memory:", embed_model="", embed_dim=16)
    try:
        engine = service.engine.recall_engine
        engine.query_planner = planner
        engine.planner_timeout_s = 0.8
        original_filter = SearchFilter(
            workspace_id="ws_demo",
            repo_id="repo_demo",
            session_id="ses_demo",
            scopes=[Scope.REPO, Scope.WORKSPACE],
            mtypes=[MemoryType.SEMANTIC, MemoryType.PROCEDURAL],
            graph_layers=[GraphLayer.CAUSAL, GraphLayer.SEMANTIC],
            as_of=101.0,
            valid_at=101.0,
            known_at=102.0,
            modified_since=103.0,
            include_ancestors=True,
        )
        original_values = {
            "workspace_id": original_filter.workspace_id,
            "repo_id": original_filter.repo_id,
            "session_id": original_filter.session_id,
            "scopes": list(original_filter.scopes),
            "mtypes": list(original_filter.mtypes),
            "graph_layers": list(original_filter.graph_layers),
            "as_of": original_filter.as_of,
            "valid_at": original_filter.valid_at,
            "known_at": original_filter.known_at,
            "modified_since": original_filter.modified_since,
            "include_ancestors": original_filter.include_ancestors,
        }

        plan, fallback = engine._plan_queries(
            "Why does CACHE.get() fail?",
            original_filter,
            selected_profile="balanced",
            planning_mode="auto",
            jev_assisted=True,
            allow_remote=True,
            data_classification="internal",
        )

        assert fallback == ""
        assert plan.queries[0].text == "Why does CACHE.get() fail?"
        assert original_filter.workspace_id == original_values["workspace_id"]
        assert original_filter.repo_id == original_values["repo_id"]
        assert original_filter.session_id == original_values["session_id"]
        assert original_filter.scopes == original_values["scopes"]
        assert original_filter.mtypes == original_values["mtypes"]
        assert original_filter.graph_layers == original_values["graph_layers"]
        assert original_filter.as_of == original_values["as_of"]
        assert original_filter.valid_at == original_values["valid_at"]
        assert original_filter.known_at == original_values["known_at"]
        assert original_filter.modified_since == original_values["modified_since"]
        assert original_filter.include_ancestors is original_values["include_ancestors"]
        assert deterministic.filters[-1] is not original_filter
        assert deterministic.filters[-1].scopes is not original_filter.scopes
        assert deterministic.filter_snapshots[-1] == original_values
        assert client.calls[0]["timeout_s"] <= 0.801
        # Trust controls are applied by recall after route generation; they are not
        # passed to the advisory client or exposed as Jev-controlled planner input.
        assert "include_untrusted" not in client.calls[0]["state"]
    finally:
        service.close()


def test_managed_decision_client_is_resolved_lazily_after_login(monkeypatch):
    from engraphis import cloud_session
    from engraphis.backends.jev_transport import EngraphisCloudDecisionClient

    logged_in = [False]
    monkeypatch.setattr(settings, "decision_backend", "managed")
    monkeypatch.setattr(settings, "decision_model", "test-model-1.0")
    monkeypatch.setattr(
        cloud_session, "configured", lambda **_kwargs: logged_in[0],
    )
    monkeypatch.setattr(
        cloud_session, "credential_bound_control_url",
        lambda: "https://control.example.invalid" if logged_in[0] else "",
    )
    service = MemoryService.create(":memory:", embed_model="", embed_dim=16)
    try:
        backend = service.engine.recall_engine.query_planner.decision_backend
        assert isinstance(backend.client, EngraphisCloudDecisionClient)
        assert backend.is_available is False
        logged_in[0] = True
        assert backend.is_available is True
    finally:
        service.close()


@pytest.fixture
def dashboard(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "jev-dashboard.db"))
    monkeypatch.setattr(settings, "embed_model", "")
    monkeypatch.setattr(settings, "embed_dim", 384)
    monkeypatch.setattr(settings, "allowed_workspaces", [])
    monkeypatch.setattr(settings, "api_token", "")
    seeded = MemoryService.create(settings.db_path)
    workspace_id = seeded.store.get_or_create_workspace("demo")
    first_id = seeded.engine.remember(
        "Postgres 16 is the primary application database.",
        workspace_id=workspace_id,
        scope=Scope.WORKSPACE,
        title="Primary database",
    )
    second_id = seeded.engine.remember(
        "SQLite stores local test fixtures.",
        workspace_id=workspace_id,
        scope=Scope.WORKSPACE,
        title="Fixture database",
    )
    seeded.close()

    from engraphis.dashboard_app import create_app

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        yield client, client.app.state.service, first_id, second_id


def _review_backend(client):
    return _backend(client)


def test_smart_mcp_recall_exposes_opt_in_consent_and_falls_back_without_it(
    dashboard, monkeypatch,
):
    _client, service, _first_id, _second_id = dashboard
    from engraphis import mcp_server

    monkeypatch.setattr(mcp_server, "_service", service)
    decision_client = _DecisionClient()
    service.engine.recall_engine.query_planner = JevAssistedQueryPlanner(
        _backend(decision_client), _FixedDeterministicPlanner(),
    )

    local = json.loads(mcp_server.smart_recall_context(
        query="Why does CACHE.get() fail after restart?",
        workspace="demo",
    ))
    assert "planning_advisory" not in local
    assert decision_client.calls == []

    opted_in = json.loads(mcp_server.smart_recall_context(
        query="Why does CACHE.get() fail after restart?",
        workspace="demo",
        allow_remote=True,
        data_classification="internal",
    ))
    assert opted_in["planning_advisory"] == {
        "status": "decision", "reason": "route_selected",
    }
    assert len(decision_client.calls) == 1
    assert decision_client.calls[0]["allow_remote"] is True
    assert decision_client.calls[0]["data_classification"] == "internal"
    assert "SELECTED MEMORY" not in decision_client.calls[0]["state"]


def test_dashboard_jev_review_requires_consent_and_does_not_write_memory(
    dashboard, monkeypatch,
):
    client, service, first_id, second_id = dashboard
    from engraphis.routes import v2_api

    hidden_tail = "UNBOUNDED-SECOND-MEMORY-END-MARKER"
    service.store.conn.execute(
        "UPDATE memories SET content=? WHERE id=?",
        ("SQLite stores local test fixtures. " + ("x" * 8_000) + hidden_tail, second_id),
    )
    service.store.conn.commit()

    decision_client = _DecisionClient()
    monkeypatch.setattr(v2_api, "_dashboard_jev_backend", lambda: _review_backend(decision_client))
    before_count = service.store.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    before_audit = service.store.conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
    before_access = {
        memory_id: service.store.get_memory(memory_id).access_count
        for memory_id in (first_id, second_id)
    }

    denied = client.post("/api/jev/review", json={
        "workspace": "demo",
        "memory_ids": [first_id],
        "claim": "Which database is primary?",
        "allow_remote": False,
        "data_classification": "internal",
    })
    assert denied.status_code == 200
    assert denied.json()["remote_consent_granted"] is False
    assert denied.json()["support"]["fallback_reason"] == "remote_not_authorized"
    assert decision_client.calls == []

    invalid_classification = client.post("/api/jev/review", json={
        "workspace": "demo",
        "memory_ids": [first_id],
        "claim": "Which database is primary?",
        "allow_remote": True,
        "data_classification": "secret",
    })
    assert invalid_classification.status_code == 422
    assert decision_client.calls == []

    authorized = client.post("/api/jev/review", json={
        "workspace": "demo",
        "memory_ids": [first_id, second_id],
        "claim": "Which database is primary?",
        "allow_remote": True,
        "data_classification": "public",
    })
    assert authorized.status_code == 200
    body = authorized.json()
    assert body["advisory_only"] is True
    assert body["read_only"] is True
    assert body["remote_consent_granted"] is True
    assert body["remote_blocked_reason"] is None
    assert body["data_classification"] == "public"
    assert body["support"]["status"] == "decision"
    assert body["support"]["probability"] == 0.9
    assert body["contradiction"]["status"] == "decision"
    assert len(decision_client.calls) == 2
    assert all(call["allow_remote"] is True for call in decision_client.calls)
    assert all(call["data_classification"] == "public" for call in decision_client.calls)
    assert all(len(call["state"]) < 8_000 for call in decision_client.calls)
    assert all(hidden_tail not in call["state"] for call in decision_client.calls)
    assert "SQLite stores local test fixtures." in decision_client.calls[1]["state"]
    assert "Primary database" in decision_client.calls[0]["state"]
    assert "Fixture database" in decision_client.calls[0]["state"]
    assert "provenance" not in decision_client.calls[0]["state"]
    assert service.store.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == before_count
    assert service.store.conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0] == before_audit
    assert {
        memory_id: service.store.get_memory(memory_id).access_count
        for memory_id in (first_id, second_id)
    } == before_access


def test_dashboard_jev_review_blocks_secret_memory_even_after_consent(dashboard, monkeypatch):
    client, service, first_id, _second_id = dashboard
    from engraphis.routes import v2_api

    service.store.conn.execute(
        "UPDATE memories SET sensitivity='secret' WHERE id=?", (first_id,),
    )
    service.store.conn.commit()
    decision_client = _DecisionClient()
    monkeypatch.setattr(v2_api, "_dashboard_jev_backend", lambda: _review_backend(decision_client))

    response = client.post("/api/jev/review", json={
        "workspace": "demo",
        "memory_ids": [first_id],
        "claim": "Which database is primary?",
        "allow_remote": True,
        "data_classification": "internal",
    })

    assert response.status_code == 200
    body = response.json()
    assert body["remote_consent_granted"] is True
    assert body["remote_blocked_reason"] == "sensitive_memory"
    assert body["support"]["fallback_reason"] == "sensitive_memory"
    assert decision_client.calls == []
    assert service.store.get_memory(first_id).sensitivity == "secret"


def test_jev_route_choice_cannot_bypass_grounded_abstention(dashboard, monkeypatch):
    client, service, _first_id, _second_id = dashboard
    from engraphis.backends.jev_query_planner import JevAssistedQueryPlanner

    decision_client = _DecisionClient()
    deterministic = _FixedDeterministicPlanner()
    service.engine.recall_engine.query_planner = JevAssistedQueryPlanner(
        _backend(decision_client), deterministic,
    )

    response = client.post("/api/answer", json={
        "workspace": "demo",
        "query": "Why does CACHE.get() fail after restart?",
        "planning": "auto",
        "jev_assisted": True,
        "allow_remote": True,
        "data_classification": "internal",
    })

    assert response.status_code == 200
    body = response.json()
    assert body["planning_advisory"] == {
        "status": "decision", "reason": "route_selected",
    }
    assert body["grounded"] is False
    assert body["abstained"] is True
    assert body["citations"] == []
    assert len(decision_client.calls) == 1
    assert decision_client.calls[0]["questions"][0].id == "route"


def test_dashboard_shows_jev_controls_and_never_claims_consent_proves_transmission(dashboard):
    client, _service, _first_id, _second_id = dashboard
    page = client.get("/")
    script = client.get("/v2-assets/ledger.js")
    assert page.status_code == script.status_code == 200
    assert 'id="ask-jev-assisted" type="checkbox"' in page.text
    assert 'id="ask-jev-remote" type="checkbox" disabled' in page.text
    assert 'id="ask-jev-classification" disabled' in page.text
    assert "Data classification for this action" in script.text
    assert "Remote consent was granted for this action only" in script.text
    assert "Jev received only this action" not in script.text
    assert "jev_assisted: jevAssisted" in script.text
    assert "query: question" in script.text
