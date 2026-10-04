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
from engraphis.backends.jev_transport import DecisionClientError  # noqa: E402
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

    def __init__(self, *, confidence=0.9, malformed=False, fail=False, error_code=None):
        self.calls = []
        self.confidence = confidence
        self.malformed = malformed
        self.fail = fail
        self.error_code = error_code

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
        if self.error_code is not None:
            raise DecisionClientError(self.error_code)
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


def test_local_planning_diagnostics_report_deterministic_planner():
    client = _DecisionClient()
    planner = JevAssistedQueryPlanner(_backend(client))
    service = MemoryService.create(":memory:", embed_model="", embed_dim=16)
    try:
        engine = service.engine.recall_engine
        engine.query_planner = planner

        result = engine.recall(
            "Why does CACHE.get() fail after restart?",
            SearchFilter(),
            planning="auto",
            diagnostics=True,
        )

        assert result.planning_details is not None
        assert result.planning_details["planner"] == planner.local_identity
        assert result.planning_details["planner"] == "engraphis.query-planner.deterministic.v1"
        assert client.calls == []
    finally:
        service.close()


@pytest.mark.parametrize("caller", ["engine", "service", "classic_mcp"])
@pytest.mark.parametrize("planning", [None, "off"])
def test_planning_off_exposes_public_advisory_reason_without_remote_work(
    dashboard, monkeypatch, caller, planning,
):
    _client, service, _first_id, _second_id = dashboard
    from engraphis import mcp_server

    monkeypatch.setattr(mcp_server, "_service", service)
    decision_client = _DecisionClient()
    deterministic = _FixedDeterministicPlanner()
    service.engine.recall_engine.query_planner = JevAssistedQueryPlanner(
        _backend(decision_client), deterministic,
    )
    kwargs = {"jev_assisted": True, "allow_remote": True,
              "data_classification": "internal"}
    if planning is not None:
        kwargs["planning"] = planning
    query = "Which database is primary?"
    if caller == "engine":
        result = service.engine.recall_engine.recall(query, SearchFilter(), **kwargs)
        advisory = result.planning_advisory
    elif caller == "service":
        advisory = service.recall(
            query, workspace="demo", record_receipt=False, **kwargs,
        )["planning_advisory"]
    else:
        advisory = json.loads(mcp_server.engraphis_recall_context(
            query=query, workspace="demo", **kwargs,
        ))["planning_advisory"]
    assert advisory == {"status": "fallback", "reason": "planning_disabled"}
    assert deterministic.filters == []
    assert decision_client.calls == []


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


@pytest.mark.parametrize(("error_code", "expected_reason"), [
    ("allowance_exhausted", "allowance_exhausted"),
    ("remote_timeout", "remote_timeout"),
    ("session_changed", "session_changed"),
    ("private-provider-detail", "remote_unavailable"),
])
def test_recall_surfaces_safe_jev_transport_state_and_keeps_deterministic_routes(
    dashboard, monkeypatch, error_code, expected_reason,
):
    _client, service, _first_id, _second_id = dashboard
    from engraphis import mcp_server

    monkeypatch.setattr(mcp_server, "_service", service)
    decision_client = _DecisionClient(error_code=error_code)
    deterministic = _FixedDeterministicPlanner()
    planner = JevAssistedQueryPlanner(_backend(decision_client), deterministic)
    service.engine.recall_engine.query_planner = planner
    query = "Why does CACHE.get() fail after restart?"

    expected = planner.plan(query)
    fallback_plan = planner.plan_with_advisory(
        query, allow_remote=True, data_classification="internal",
    )
    assert [route.text for route in fallback_plan.queries] == [
        route.text for route in expected.queries
    ]
    assert fallback_plan.reason_codes[-1] == f"jev_{expected_reason}"
    response = json.loads(mcp_server.smart_recall_context(
        query=query,
        workspace="demo",
        allow_remote=True,
        data_classification="internal",
    ))

    assert response["planning_advisory"] == {
        "status": "fallback", "reason": expected_reason,
    }
    assert len(decision_client.calls) == 2
    # The failed or unavailable advisory leaves local deterministic routes intact.
    assert [route.text for route in expected.queries] == [
        query, "CACHE.get()", "cache restart path",
    ]


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


@pytest.mark.parametrize(("sensitivity", "metadata_sensitivity"), [
    ("normal", "secret"), ("normal", " SeCrEt "), ("normal", "unrecognized"),
    ("normal", True), ("normal", False), ("normal", {"label": "normal"}),
    ("secret", "normal"),
])
@pytest.mark.parametrize("secret_position", [0, 1])
def test_review_secret_or_unknown_metadata_never_reaches_either_provider_check(
    dashboard, monkeypatch, sensitivity, metadata_sensitivity, secret_position,
):
    from engraphis.routes import v2_api

    client, service, first_id, second_id = dashboard
    memory_ids = [first_id, second_id]
    selected_id = memory_ids[secret_position]
    service.store.conn.execute(
        "UPDATE memories SET sensitivity=?, metadata=? WHERE id=?",
        (sensitivity, json.dumps({"sensitivity": metadata_sensitivity}), selected_id),
    )
    service.store.conn.commit()
    decision_client = _DecisionClient()
    backend_resolutions = []

    def resolve_backend():
        backend_resolutions.append(True)
        return _review_backend(decision_client)

    monkeypatch.setattr(v2_api, "_dashboard_jev_backend", resolve_backend)
    before = list(service.store.conn.iterdump())
    response = client.post("/api/jev/review", json={
        "workspace": "demo", "memory_ids": memory_ids, "claim": "Which database is primary?",
        "allow_remote": True, "data_classification": "internal",
    })
    assert response.status_code == 200
    body = response.json()
    assert body["remote_consent_granted"] is True
    assert body["remote_blocked_reason"] == "sensitive_memory"
    assert body["support"]["fallback_reason"] == "sensitive_memory"
    assert body["contradiction"]["fallback_reason"] == "sensitive_memory"
    assert decision_client.calls == []
    assert backend_resolutions == []
    assert list(service.store.conn.iterdump()) == before


@pytest.mark.parametrize("metadata_sensitivity", ["normal", " Sensitive ", None])
def test_review_recognized_nonsecret_metadata_retains_consented_advisory(
    dashboard, monkeypatch, metadata_sensitivity,
):
    import json
    from engraphis.routes import v2_api

    client, service, first_id, second_id = dashboard
    service.store.conn.execute("UPDATE memories SET metadata=? WHERE id=?", (
        json.dumps({"sensitivity": metadata_sensitivity}), first_id,
    ))
    service.store.conn.commit()
    decision_client = _DecisionClient()
    monkeypatch.setattr(v2_api, "_dashboard_jev_backend", lambda: _review_backend(decision_client))
    response = client.post("/api/jev/review", json={
        "workspace": "demo", "memory_ids": [first_id, second_id],
        "claim": "Which database is primary?", "allow_remote": True,
        "data_classification": "internal",
    })
    assert response.status_code == 200
    assert response.json()["remote_blocked_reason"] is None
    assert len(decision_client.calls) == 2


@pytest.mark.parametrize(("scope", "stored_repo"), [
    (Scope.REPO, "selected"),
    (Scope.WORKSPACE, None),
    (Scope.USER, None),
    (Scope.WORKSPACE, "sibling"),
    (Scope.USER, "sibling"),
])
def test_project_jev_review_accepts_visible_ancestors_without_inspection_or_mutation(
    dashboard, monkeypatch, scope, stored_repo,
):
    from engraphis.routes import v2_api
    from engraphis.service import ValidationError
    from engraphis.service_context import bind_service

    client, service, _first_id, _second_id = dashboard
    wid = service.store.get_or_create_workspace("demo")
    selected_rid = service.store.get_or_create_repo(wid, "selected")
    sibling_rid = service.store.get_or_create_repo(wid, "sibling")
    first_id = service.engine.remember(
        "The selected application uses Postgres.", workspace_id=wid,
        repo_id=selected_rid, scope=Scope.REPO, title="Selected repository",
    )
    code = "def configure():\n    return {'database': 'Postgres'}\n"
    peer_id = service.engine.remember(
        code, workspace_id=wid, scope=Scope.REPO if scope == Scope.REPO else Scope.WORKSPACE,
        repo_id=selected_rid if scope == Scope.REPO else None,
        title="Selected review evidence",
    )
    if scope == Scope.USER or stored_repo == "sibling":
        # Legacy/promoted rows can retain a repo id; new user-scope writes are unsupported.
        service.store.conn.execute(
            "UPDATE memories SET scope=?, repo_id=? WHERE id=?",
            (scope.value, sibling_rid if stored_repo == "sibling" else None, peer_id),
        )
        service.store.conn.commit()
    visible = service.list_memories(workspace="demo", repo="selected")["memories"]
    assert {first_id, peer_id} <= {memory["id"] for memory in visible}
    if scope in {Scope.WORKSPACE, Scope.USER}:
        # Broader review visibility never broadens inspection/governance ownership.
        with pytest.raises(ValidationError, match="does not belong"):
            service.inspect(peer_id, workspace="demo", repo="selected")

    def forbidden_inspection(*_args, **_kwargs):
        pytest.fail("advisory review read inspector links, audit, or lineage")

    monkeypatch.setattr(service, "inspect", forbidden_inspection)
    decision_client = _DecisionClient()
    monkeypatch.setattr(v2_api, "_dashboard_jev_backend", lambda: _review_backend(decision_client))
    before = list(service.store.conn.iterdump())
    with bind_service(service, principal={
        "id": "usr_reviewer", "email": "reviewer@example.test", "role": "viewer",
    }):
        response = client.post("/api/jev/review", json={
            "workspace": "demo", "repo": "selected", "memory_ids": [first_id, peer_id],
            "claim": "Which database is configured?", "allow_remote": True,
            "data_classification": "internal",
        })

    assert response.status_code == 200
    assert response.json()["support"]["status"] == "decision"
    assert response.json()["contradiction"]["status"] == "decision"
    assert len(decision_client.calls) == 2
    assert all(code in call["state"] for call in decision_client.calls)
    assert list(service.store.conn.iterdump()) == before


@pytest.mark.parametrize("denied_scope", [
    "foreign_workspace", "sibling_repo", "foreign_session", "own_session",
])
def test_project_jev_review_denies_out_of_scope_records_before_provider_or_mutation(
    dashboard, monkeypatch, denied_scope,
):
    from engraphis.routes import v2_api
    from engraphis.service import ValidationError
    from engraphis.service_context import bind_service

    client, service, first_id, _second_id = dashboard
    wid = service.store.get_or_create_workspace("demo")
    selected_rid = service.store.get_or_create_repo(wid, "selected")
    memory_wid, memory_rid, scope, session_id = wid, selected_rid, Scope.REPO, None
    if denied_scope == "foreign_workspace":
        memory_wid = service.store.get_or_create_workspace("foreign")
        memory_rid, scope = None, Scope.WORKSPACE
    elif denied_scope == "sibling_repo":
        memory_rid = service.store.get_or_create_repo(wid, "sibling")
    else:
        session_owner = "usr_reviewer" if denied_scope == "own_session" else "usr_other"
        session_id = service.store.start_session(wid, selected_rid, user_id=session_owner)
        scope = Scope.SESSION
    denied_id = service.engine.remember(
        "PRIVATE-DENIED-REVIEW-EVIDENCE", workspace_id=memory_wid, repo_id=memory_rid,
        scope=scope, session_id=session_id,
    )
    decision_client = _DecisionClient()
    monkeypatch.setattr(v2_api, "_dashboard_jev_backend", lambda: _review_backend(decision_client))
    before = list(service.store.conn.iterdump())
    with bind_service(service, principal={
        "id": "usr_reviewer", "email": "reviewer@example.test", "role": "viewer",
    }):
        with pytest.raises(ValidationError, match="not visible"):
            service.read_memory_for_review(denied_id, workspace="demo", repo="selected")
        response = client.post("/api/jev/review", json={
            "workspace": "demo", "repo": "selected", "memory_ids": [first_id, denied_id],
            "claim": "Is there selected evidence?", "allow_remote": True,
            "data_classification": "internal",
        })

    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "invalid request"
    assert decision_client.calls == []
    assert "PRIVATE-DENIED-REVIEW-EVIDENCE" not in response.text
    assert list(service.store.conn.iterdump()) == before


@pytest.mark.parametrize(("state", "visible"), [
    ("current", True),
    ("low_retention", True),
    ("future_invalidation", True),
    ("not_yet_known_invalidation", True),
    ("valid_from_boundary", True),
    ("known_from_boundary", True),
    ("invalidated", False),
    ("valid_to_boundary", False),
    ("expired", False),
    ("expired_boundary", False),
    ("future_valid", False),
    ("not_yet_known", False),
    ("forgotten", False),
    ("retired", False),
])
def test_jev_review_selected_record_matches_current_browse_visibility(
    dashboard, monkeypatch, state, visible,
):
    from engraphis.routes import v2_api
    from engraphis.service import ValidationError
    from engraphis.service_context import bind_service

    client, service, first_id, second_id = dashboard
    now = time.time() + 1
    monkeypatch.setattr(time, "time", lambda: now)
    changes = {
        "low_retention": {"stability": 0},
        "future_invalidation": {"valid_to": now + 60, "valid_to_recorded_at": now},
        "not_yet_known_invalidation": {
            "valid_to": now - 60, "valid_to_recorded_at": now + 60,
        },
        "valid_from_boundary": {"valid_from": now},
        "known_from_boundary": {"ingested_at": now},
        "invalidated": {"valid_to": now - 60, "valid_to_recorded_at": now},
        "valid_to_boundary": {"valid_to": now, "valid_to_recorded_at": now},
        "expired": {"expired_at": now - 60},
        "expired_boundary": {"expired_at": now},
        "future_valid": {"valid_from": now + 60},
        "not_yet_known": {"ingested_at": now + 60},
    }.get(state, {})
    if changes:
        service.store.conn.execute(
            "UPDATE memories SET " + ",".join(f"{field}=?" for field in changes) + " WHERE id=?",
            [*changes.values(), second_id],
        )
        service.store.conn.commit()
    elif state == "forgotten":
        service.forget(second_id, workspace="demo")
    elif state == "retired":
        service.retire(second_id, workspace="demo")

    decision_client = _DecisionClient()
    monkeypatch.setattr(v2_api, "_dashboard_jev_backend", lambda: _review_backend(decision_client))
    before = list(service.store.conn.iterdump())
    with bind_service(service, principal={
        "id": "usr_reviewer", "email": "reviewer@example.test", "role": "viewer",
    }):
        listing = service.list_memories(workspace="demo")["memories"]
        assert (second_id in {memory["id"] for memory in listing}) is visible
        if visible:
            assert service.read_memory_for_review(second_id, workspace="demo").id == second_id
        else:
            with pytest.raises(ValidationError, match="not visible"):
                service.read_memory_for_review(second_id, workspace="demo")
        response = client.post("/api/jev/review", json={
            "workspace": "demo", "memory_ids": [first_id, second_id],
            "claim": "Which database is primary?", "allow_remote": True,
            "data_classification": "internal",
        })

    assert response.status_code == (200 if visible else 400)
    assert len(decision_client.calls) == (2 if visible else 0)
    if not visible:
        assert response.json()["detail"]["error"] == "invalid request"
        assert "SQLite stores local test fixtures." not in response.text
    assert list(service.store.conn.iterdump()) == before


def test_jev_review_projection_preserves_structural_whitespace():
    from engraphis.routes.v2_api import _jev_review_projection_parts

    source = "def configure():\n    settings = {\n        'port': 443,\n    }\n    return settings\n"
    title, content = _jev_review_projection_parts({"title": "  Setup  ", "content": source})

    assert title == "Setup"
    assert content == source
    assert _jev_review_projection_parts({"content": source + "x" * 4_000})[1] == (
        source + "x" * 4_000
    )[:3_500]


def test_jev_route_choice_cannot_bypass_grounded_abstention(dashboard, monkeypatch):
    client, service, _first_id, _second_id = dashboard
    from engraphis.backends.jev_query_planner import JevAssistedQueryPlanner

    decision_client = _DecisionClient()
    deterministic = _FixedDeterministicPlanner()
    service.engine.recall_engine.query_planner = JevAssistedQueryPlanner(
        _backend(decision_client), deterministic,
    )
    retrieved = []
    recall = service.engine.recall_engine.recall

    def record_recall(*args, **kwargs):
        result = recall(*args, **kwargs)
        retrieved.append(result)
        return result

    monkeypatch.setattr(service.engine.recall_engine, "recall", record_recall)

    response = client.post("/api/answer", json={
        "workspace": "demo",
        "query": "Why does CACHE.get() fail after restart?",
        "planning": "auto",
        "jev_assisted": True,
        "allow_remote": True,
        "data_classification": "internal",
        "include_retrieval_preview": True,
    })

    assert response.status_code == 200
    body = response.json()
    assert body["planning_advisory"] == {
        "status": "decision", "reason": "route_selected",
    }
    assert body["grounded"] is False
    assert body["abstained"] is True
    assert body["citations"] == []
    assert body["retrieval_preview"] == retrieved[0].chunks
    assert len(decision_client.calls) == 1
    assert len(retrieved) == 1
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
    assert "allowance_exhausted:" in script.text
    assert "fallbackLabels[item.fallback_reason]" in script.text
    assert "Remote consent was granted for this action only" in script.text
    assert "Jev received only this action" not in script.text
    assert "jev_assisted: jevAssisted" in script.text
    assert "query: question" in script.text
