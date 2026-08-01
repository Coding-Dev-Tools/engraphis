"""Unified local dashboard tests for the public open-core boundary."""
import ast
import io
import threading
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="full-stack extra not installed")
pytest.importorskip("httpx", reason="httpx not installed")

from fastapi.testclient import TestClient  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from engraphis import cloud_features  # noqa: E402
from engraphis.config import settings  # noqa: E402
from engraphis.cloud_features import CloudFeatureError  # noqa: E402
from engraphis.routes import v2_api  # noqa: E402
from engraphis.service import MemoryService, ValidationError  # noqa: E402


def _client(monkeypatch, tmp_path):
    db_path = str(tmp_path / "dashboard.db")
    monkeypatch.setattr(settings, "db_path", db_path)
    monkeypatch.setattr(settings, "embed_model", "")
    monkeypatch.setattr(settings, "embed_dim", 384)
    monkeypatch.setattr(settings, "allowed_workspaces", [])
    monkeypatch.setattr(settings, "api_token", "")
    seeded = MemoryService.create(db_path)
    seeded.remember(
        "Postgres 16 is the main database.",
        workspace="demo",
        scope="workspace",
        title="Database",
    )
    seeded.remember(
        "A second workspace must stay isolated.",
        workspace="beta",
        scope="workspace",
        title="Isolation",
    )
    seeded.store.close()
    from engraphis.dashboard_app import create_app
    return TestClient(create_app(), client=("127.0.0.1", 50000))


def test_dashboard_serves_and_bootstraps_local_core(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "<title>Engraphis Ledger</title>" in page.text
        assert 'class="sidebar"' in page.text
        for area in ("Today", "Ask", "Library", "Graph &amp; Relations", "Provenance", "Manage"):
            assert f">{area}<" in page.text
        assert 'value="matrix">Matrix' in page.text
        assert 'class="dashboard-switcher" aria-label="Dashboard interface"' in page.text
        assert 'id="sidebar-theme-select" aria-label="Dashboard theme"' in page.text
        assert 'value="classic">Classic<' in page.text
        assert 'href="/classic">Classic<' in page.text
        assert 'Ledger (primary)' not in page.text
        assert 'Classic (alternate)' not in page.text
        assert '/v2-assets/vendor/d3.min.js' in page.text
        assert '/v2-assets/vendor/force-graph.min.js' not in page.text
        assert '/v2-assets/engraphis-graph.js' not in page.text
        classic = client.get("/classic")
        assert classic.status_code == 200
        assert '/classic-assets/dashboard.css' in classic.text
        assert 'class="dashboard-switcher" aria-label="Dashboard interface"' in classic.text
        assert 'href="/"' in classic.text
        assert 'href="/classic" aria-current="page">Classic (alternate)<' in classic.text
        assert 'value="classic" selected>Classic dashboard (alternate)<' in classic.text
        assert 'id="graph-show-all"' not in classic.text
        assert client.get("/v2-assets/ledger.css").status_code == 200
        ledger_js = client.get("/v2-assets/ledger.js")
        assert ledger_js.status_code == 200
        assert "'/v2-assets/vendor/force-graph.min.js?v=20260727-final'" in ledger_js.text
        assert "'/v2-assets/engraphis-graph.js?v=20260730-drag-stability'" in ledger_js.text
        assert "/v2-assets/ledger.css?v=20260728-connected-memories" in page.text
        assert "/v2-assets/ledger.js?v=20260728-connected-memories" in page.text
        classic_js = client.get("/classic-assets/dashboard.js")
        assert classic_js.status_code == 200
        assert "/static/vendor/force-graph.min.js" in classic_js.text
        assert "/v2-assets/engraphis-graph.js?v=20260730-drag-stability" in classic_js.text
        assert "graphLimit=GRAPH_FULL?20000:320" in classic_js.text
        assert "graphScope=GRAPH_FULL?'&full=true':(showUnlinked?'':'&connected_only=true')" in classic_js.text
        bootstrap = client.get("/api/bootstrap")
        assert bootstrap.status_code == 200
        assert bootstrap.json()["stats"]["memories"] >= 1
        savings = client.get("/api/context-savings", params={"workspace": "demo"})
        assert savings.status_code == 200
        assert savings.json()["format"] == "engraphis-context-savings/1"


def test_dashboard_assets_revalidate_instead_of_pinning_old_visuals(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        for path in (
            "/v2-assets/engraphis-graph.js?v=20260730-drag-stability",
            "/v2-assets/ledger.js?v=20260728-connected-memories",
            "/v2-assets/ledger.css?v=20260728-connected-memories",
            "/classic-assets/dashboard.js?v=20260728-reference-materials",
        ):
            response = client.get(path)
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-cache, must-revalidate"


def test_classic_dashboard_script_mirrors_the_static_compatibility_asset():
    root = Path(__file__).parents[1] / "engraphis"
    assert (root / "classic_assets" / "dashboard.js").read_bytes() == (
        root / "static" / "dashboard.js"
    ).read_bytes()


def test_dashboard_and_mcp_recall_share_the_v2_service(monkeypatch, tmp_path):
    pytest.importorskip("mcp", reason="MCP extra not installed")
    import json

    from engraphis import mcp_server

    with _client(monkeypatch, tmp_path) as client:
        assert mcp_server.service() is client.app.state.service
        response = client.get(
            "/api/recall",
            params={"q": "which database do we use", "workspace": "demo", "k": 3},
        )
        assert response.status_code == 200
        dashboard = response.json()
        mcp = json.loads(mcp_server.engraphis_recall(
            query="which database do we use", workspace="demo", k=3,
        ))
        assert [memory["id"] for memory in dashboard["memories"]] == [
            memory["id"] for memory in mcp["memories"]
        ]
        assert [memory["retention"] for memory in dashboard["memories"]] == [
            memory["retention"] for memory in mcp["memories"]
        ]
        assert [memory["relative_score"] for memory in dashboard["memories"]] == [
            memory["relative_score"] for memory in mcp["memories"]
        ]
        assert [memory["absolute_support"] for memory in dashboard["memories"]] == [
            memory["absolute_support"] for memory in mcp["memories"]
        ]
        assert dashboard["score_semantics"] == mcp["score_semantics"]


def test_dashboard_keyword_fallback_reports_truthful_lexical_scores(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        def mismatched_embedder(*_args, **_kwargs):
            raise ValueError("shapes (1,256) and (384,1) not aligned")

        monkeypatch.setattr(client.app.state.service, "recall", mismatched_embedder)
        response = client.get(
            "/api/recall",
            params={
                "q": "which database do we use",
                "workspace": "demo",
                "k": 3,
                "response_mode": "compact",
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["mode"] == "keyword"
        assert "lexical Jaccard" in payload["score_semantics"]["relative_score"]
        assert "Semantic support is unavailable" in (
            payload["score_semantics"]["absolute_support"]
        )
        memory = payload["memories"][0]
        assert memory["score"] == memory["relative_score"] == 1.0
        assert 0.0 < memory["absolute_support"] < 1.0
        assert memory["arm"] == "lexical"
        assert "content" not in memory


def test_dashboard_serves_the_graph_engine_from_its_v2_asset_surface(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        asset = client.get("/v2-assets/engraphis-graph.js")
        assert asset.status_code == 200
        assert "window.EngraphisGraph =" in asset.text
        compat = client.get("/v2-assets/engraphis-graph-compat.js")
        assert compat.status_code == 200
        assert "window.EngraphisGraphCompat =" in compat.text
        assert client.get("/v2-assets/vendor/d3.min.js").status_code == 200
        assert client.get("/v2-assets/vendor/force-graph.min.js").status_code == 200


def test_graph_load_is_bounded_single_flight_and_retryable(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        page = client.get("/")
        script = client.get("/v2-assets/ledger.js")
        assert 'id="graph-retry"' in page.text
        assert 'id="graph-full"' not in page.text
        assert '>Show all nodes<' not in page.text
        assert 'id="graph-show-unlinked"' in page.text
        assert 'id="graph-unlinked"' not in page.text
        assert 'id="graph-tune-unlinked"' not in page.text
        assert 'id="graph-style" type="hidden" value="cyber"' in page.text
        assert "const GRAPH_INITIAL_NODE_LIMIT = 320;" in script.text
        assert "const GRAPH_FULL_NODE_LIMIT = 20_000;" in script.text
        assert "const GRAPH_LOAD_TIMEOUT_MS = 12_000;" in script.text
        assert "AbortController" in script.text
        assert "state.graphLoadPromise" in script.text
        assert "&full=true" in script.text
        assert "&connected_only=true" in script.text
        assert "style: 'cyber'" in script.text
        assert "renderMode: targetMode" in script.text
        assert "loadGraph({ force: true })" in script.text


def test_graph_motion_saved_views_and_tuning_controls_are_wired(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        page = client.get("/")
        script = client.get("/v2-assets/ledger.js")
        for control in (
            'id="graph-flow-speed"', 'data-graph-saved-view="operations"',
            'data-graph-saved-view="schema"', 'data-graph-saved-view="people"',
            'data-graph-saved-view="code"', 'id="graph-save-view"',
            'id="graph-repel"', 'id="graph-depth"', 'id="graph-reset-tuning"',
            'data-graph-layer="code"',
        ):
            assert control in page.text
        for behavior in (
            "function applyGraphView(id)", "function resetGraphTuning()",
            "function saveCurrentGraphView()", "function graphTuningSettings()",
            "&include_code=true", "graph.setLayers(graphLayerState())",
            "setSettings({ flowSpeed: speed })",
        ):
            assert behavior in script.text


def test_graph_palette_recolors_every_colour_mode(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        engine = client.get("/v2-assets/engraphis-graph.js")
        ledger = client.get("/v2-assets/ledger.js")
        assert engine.status_code == 200
        assert "function selectedPalette()" in engine.text
        assert "function commPal() { return selectedPalette()" in engine.text
        assert "const colors = selectedPalette() || GRAPH_HEAT;" in engine.text
        # Palettes still recolor every identity mode, but material families stay stable:
        # semantic color belongs to the slim identity ring rather than rotating the whole
        # Cyber film into arbitrary green/yellow alloys.
        assert "function iridescentTint(c)" not in engine.text
        assert "fixedPalette" in engine.text
        assert "function identityRing(" in engine.text
        assert "identity: rgbString(identity)" in engine.text
        assert "function graphThemeColors()" in ledger.text
        assert "graph.setThemeColors(graphThemeColors());" in ledger.text
        assert "state.graphEngine.setThemeColors(graphThemeColors());" in ledger.text
        assert "renderMode: opts.renderMode === 'full' ? 'full' : 'overview'" in engine.text
        assert "function pinFullGraphLayout(data)" in engine.text


def test_graph_facts_and_search_use_the_atomic_node_reveal(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        page = client.get("/")
        ledger = client.get("/v2-assets/ledger.js")
        engine = client.get("/v2-assets/engraphis-graph.js")
        assert 'id="graph-connections-dialog"' in page.text
        assert "function revealGraphNode(id, label = 'Selected entity')" in ledger.text
        assert "revealGraphNode(item.id, item.name)" in ledger.text
        assert "function openGraphConnections(item)" in ledger.text
        assert "function showGraphConnectionMemories(item)" in ledger.text
        assert "onNodeClick: item => openGraphConnections(item)" in ledger.text
        assert "api.reveal = id =>" in engine.text
        assert "function centerRenderedNode(id)" in engine.text
        assert "suppressNodeClickAfterDrag" in engine.text
        assert "render(true, true);" not in engine.text[engine.text.index("api.focus = id =>"):engine.text.index("api.clearFocus")]


def test_library_editor_stacks_directly_below_the_selected_memory_panel(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert '<div class="library-detail-stack">' in page.text
        assert page.text.index('id="memory-detail"') < page.text.index('id="memory-editor"')
        stylesheet = client.get("/v2-assets/ledger.css")
        assert ".library-detail-stack { display: grid; gap: 12px; align-content: start; }" in stylesheet.text


def test_workspace_switcher_uses_the_active_ledger_theme_for_native_dropdowns(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        stylesheet = client.get("/v2-assets/ledger.css")
        assert stylesheet.status_code == 200
        css = stylesheet.text
        assert ".workspace-switcher select {" in css
        assert "background: var(--c-inset);" in css
        assert "color-scheme: dark;" in css
        assert 'body[data-theme="paper"] .workspace-switcher select { color-scheme: light; }' in css
        assert ".workspace-switcher select option { background: var(--c-inset); color: var(--c-fg); }" in css
        assert ".workspace-switcher select option:checked { background: var(--c-acc); color: var(--c-bg); }" in css


def test_sidebar_keeps_manage_and_compare_plans_in_separate_flex_rows(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        stylesheet = client.get("/v2-assets/ledger.css")
        assert stylesheet.status_code == 200
        css = stylesheet.text
        sidebar = css[css.index(".sidebar {"):css.index(".brand-row {")]
        assert "display: flex;" in sidebar
        assert "flex-direction: column;" in sidebar
        assert "grid-template-rows" not in sidebar
        assert ".primary-nav { flex: 1 0 auto; }" in css
        assert ".manage-nav { flex: 0 0 auto; }" in css
        assert ".sidebar-promo {\n  flex: 0 0 auto;" in css


def test_dashboard_grounded_answer_route_cites_or_abstains(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        grounded = client.post(
            "/api/answer",
            json={
                "query": "Which database is the main database?",
                "workspace": "demo",
                "k": 8,
                "max_citations": 5,
                "candidate_depth": "adaptive",
            },
        )
        assert grounded.status_code == 200
        body = grounded.json()
        assert body["query"] == "Which database is the main database?"
        assert body["grounded"] is True
        assert body["abstained"] is False
        assert body["citations"]
        assert body["sources"] == body["citations"]
        assert "[1]" in body["answer"]
        assert body["candidate_depth"] == "adaptive"
        assert body["candidate_k_used"] < body["candidate_k_requested"]

        abstained = client.post(
            "/api/answer",
            json={
                "query": "How should I bake a sourdough loaf?",
                "workspace": "demo",
            },
        )
        assert abstained.status_code == 200
        assert abstained.json()["grounded"] is False
        assert abstained.json()["abstained"] is True


def test_dashboard_grounded_answer_route_bounds_and_redacts(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        assert client.post("/api/answer", json={"query": "", "workspace": "demo"}).status_code == 422
        assert client.post(
            "/api/answer",
            json={"query": "database", "workspace": "demo", "k": 51},
        ).status_code == 422


def test_team_account_routes_are_not_in_public_runtime(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        assert client.post("/api/auth/setup", json={}).status_code == 404
        assert client.get("/api/auth/users").status_code == 404
        state = client.get("/api/auth/state").json()
        assert state["enabled"] is False
        assert state["hosted_team"] is True


def test_local_agent_write_has_no_client_side_team_paywall(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        response = client.post(
            "/api/remember",
            json={"workspace": "demo", "content": "Queues use at-least-once delivery."},
        )
        assert response.status_code == 200


def test_http_memory_api_round_trips_world_time(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        old = client.post(
            "/api/remember",
            json={
                "workspace": "demo",
                    "content": "The API rate limit is 100 requests per minute.",
                    "valid_from": 1_000.0,
                    "subject_key": "api.rate_limit",
                    "claim_kind": "configured_value",
            },
        ).json()
        new = client.post(
            "/api/intent/remember",
            json={
                "workspace": "demo",
                    "text": "The API rate limit is 500 requests per minute.",
                    "valid_from": 2_000.0,
                    "subject_key": "api.rate_limit",
                    "claim_kind": "configured_value",
            },
        ).json()

        before = client.get(
            "/api/recall",
            params={
                "workspace": "demo",
                "q": "What is the API rate limit?",
                "as_of": 1_500.0,
            },
        )
        after = client.post(
            "/api/answer",
            json={
                "workspace": "demo",
                "query": "What is the API rate limit?",
                "as_of": 2_500.0,
                "min_support": 0.0,
            },
        )

        assert before.status_code == 200
        assert [memory["id"] for memory in before.json()["memories"]] == [old["id"]]
        assert after.status_code == 200
        assert [source["id"] for source in after.json()["sources"]] == [new["id"]]


def test_keyword_recall_fallback_keeps_bitemporal_visibility(monkeypatch, tmp_path):
    """A semantic-backend failure must not leak current facts into historical views."""
    with _client(monkeypatch, tmp_path) as client:
        svc = v2_api.service()
        old = svc.remember(
            "The fallback retention setting was ten days.", workspace="demo",
            valid_from=1_000.0,
        )
        new = svc.remember(
            "The fallback retention setting was thirty days.", workspace="demo",
            valid_from=2_000.0,
        )
        # The writes happened during this test, but the fixture models facts learned
        # before the requested historical system-time anchors.
        svc.store.conn.execute(
            "UPDATE memories SET ingested_at=100 WHERE id=?", (old["id"],)
        )
        svc.store.conn.execute(
            "UPDATE memories SET ingested_at=200 WHERE id=?", (new["id"],)
        )
        svc.store.conn.execute(
            "UPDATE memories SET valid_to_recorded_at=200, "
            "subject_key='retention.days', claim_kind='configured_value' "
            "WHERE id=?",
            (old["id"],),
        )
        svc.store.conn.commit()
        old_before = v2_api._keyword_search(
            "demo", "fallback retention", valid_at=1_500.0, known_at=3_000.0
        )
        old_known = v2_api._keyword_search(
            "demo", "fallback retention", valid_at=1_500.0, known_at=50.0
        )
        current = v2_api._keyword_search(
            "demo", "fallback retention", valid_at=2_500.0, known_at=3_000.0
        )
        closure_unknown = v2_api._keyword_search(
            "demo", "fallback retention", valid_at=2_500.0, known_at=150.0
        )

        assert [memory["id"] for memory in old_before] == [old["id"]]
        assert old_known == []
        assert [memory["id"] for memory in current] == [new["id"]]
        assert [memory["id"] for memory in closure_unknown] == [old["id"]]
        assert closure_unknown[0]["valid_to_recorded_at"] == 200.0
        assert closure_unknown[0]["subject_key"] == "retention.days"
        assert closure_unknown[0]["claim_kind"] == "configured_value"

        def incompatible_embedder(*_args, **_kwargs):
            raise ValueError("shapes (256,) and (384,) not aligned")

        monkeypatch.setattr(svc, "recall", incompatible_embedder)
        fallback = client.get(
            "/api/recall",
            params={
                "workspace": "demo", "q": "fallback retention",
                "valid_at": 2_500.0, "known_at": 150.0,
            },
        )
        assert fallback.status_code == 200
        assert fallback.json()["mode"] == "keyword"
        assert [item["id"] for item in fallback.json()["memories"]] == [old["id"]]

        compact_fallback = client.get(
            "/api/recall",
            params={
                "workspace": "demo", "q": "fallback retention", "response_mode": "compact",
                "token_budget": 0,
            },
        )
        payload = compact_fallback.json()
        assert compact_fallback.status_code == 200
        assert payload["mode"] == "keyword"
        assert payload["response_mode"] == "compact"
        assert payload["usage"]["budget_tokens"] == 0
        assert payload["usage"]["context_tokens"] == 0
        assert payload["memories"] and "content" not in payload["memories"][0]


def test_keyword_recall_fallback_excludes_untrusted_memories(monkeypatch, tmp_path):
    """A degraded HTTP recall must enforce the same prompt eligibility boundary."""
    with _client(monkeypatch, tmp_path) as client:
        svc = v2_api.service()
        trusted = svc.remember(
            "Fallback visibility trusted candidate.",
            workspace="demo",
            source="human",
            trusted=True,
        )
        untrusted = svc.remember(
            "Fallback visibility untrusted candidate.",
            workspace="demo",
            source="sync",
            trusted=False,
        )

        def incompatible_embedder(*_args, **_kwargs):
            raise ValueError("shapes (256,) and (384,) not aligned")

        monkeypatch.setattr(svc, "recall", incompatible_embedder)
        response = client.get(
            "/api/recall",
            params={"workspace": "demo", "q": "fallback visibility candidate", "k": 1},
        )

    payload = response.json()
    assert response.status_code == 200
    assert payload["mode"] == "keyword"
    assert [memory["id"] for memory in payload["memories"]] == [trusted["id"]]
    assert untrusted["id"] not in {memory["id"] for memory in payload["memories"]}
    assert "untrusted candidate" not in repr(payload)


def test_http_memory_api_rejects_backdated_supersession_without_partial_write(
    monkeypatch, tmp_path
):
    with _client(monkeypatch, tmp_path) as client:
        original = client.post(
            "/api/remember",
            json={
                "workspace": "demo",
                "content": "The deployment window is Friday afternoon.",
                "valid_from": 2_000.0,
            },
        ).json()
        service = v2_api.service()
        count_before = len(service.store.list_memories(include_invalid=True))
        rejected = client.post(
            "/api/remember",
            json={
                "workspace": "demo",
                "content": "The deployment window is Thursday afternoon.",
                "valid_from": 1_000.0,
            },
        )

        assert rejected.status_code == 400
        assert service.store.get_memory(original["id"]).valid_to is None
        assert len(service.store.list_memories(include_invalid=True)) == count_before


def test_manual_consolidation_stays_local_but_dreaming_is_cloud_only(
    monkeypatch, tmp_path
):
    with _client(monkeypatch, tmp_path) as client:
        manual = client.post(
            "/api/consolidate",
            json={"workspace": "demo", "dry_run": True, "infer": False},
        )
        assert manual.status_code == 200
        dream = client.post(
            "/api/consolidate",
            json={"workspace": "demo", "dry_run": True, "infer": True},
        )
        assert dream.status_code == 501
        assert dream.json()["detail"]["cloud_only"] is True


def test_analytics_route_delegates_to_managed_compute(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "engraphis.cloud_features.run_managed_job",
        lambda service, workspace, kind: {
            "result": {
                "kind": kind,
                "generation": 4,
                "totals": {"live": 1},
            }
        },
    )
    with _client(monkeypatch, tmp_path) as client:
        response = client.get("/api/analytics?workspace=demo")
        assert response.status_code == 200
        assert response.json()["kind"] == "analytics"
        assert response.json()["generation"] == 4


def test_unconnected_automation_returns_a_structured_auth_error(monkeypatch, tmp_path):
    for name in (
        "ENGRAPHIS_CLOUD_ACCESS_TOKEN",
        "ENGRAPHIS_CLOUD_ORGANIZATION_ID",
        "ENGRAPHIS_CLOUD_COMPUTE_URL",
        "ENGRAPHIS_CLOUD_REFRESH_CREDENTIAL",
        "ENGRAPHIS_CLOUD_CONTROL_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ENGRAPHIS_STATE_DIR", str(tmp_path / "unconnected-state"))

    with _client(monkeypatch, tmp_path) as client:
        response = client.get("/api/automation?workspace=demo")

    assert response.status_code == 401
    # The copy is ``_public_session_error(401)``: fixed, status-keyed, and actionable. The
    # generic placeholder told an unconnected customer nothing they could act on.
    assert response.json()["detail"] == {
        "error": "Connect this installation to Engraphis Cloud to use hosted features.",
        "managed_cloud": True,
        "transient": False,
        "code": "cloud_unconfigured",
    }


def test_hosted_automation_accepts_the_cloud_policy_field(monkeypatch, tmp_path):
    saved = {}

    class _Cloud:
        def upload_snapshot(self, workspace_id, snapshot):
            return {"generation": snapshot["generation"]}

        def get_policy(self, workspace_id):
            return {"enabled": False, "cadence_minutes": 1440, "dream_enabled": False}

        def save_policy(self, workspace_id, policy):
            saved.update(policy)
            return {"version": 2}

    monkeypatch.setattr(
        "engraphis.cloud_features.build_managed_snapshot",
        lambda service, workspace: ("ws_cloud", {"generation": 1}),
    )
    monkeypatch.setattr(
        "engraphis.cloud_features.CloudFeatureClient.from_environment",
        lambda workspace_id=None: _Cloud(),
    )
    with _client(monkeypatch, tmp_path) as client:
        response = client.post(
            "/api/automation",
            json={"enabled": True, "dream_enabled": True, "cadence_hours": 12},
        )
        assert response.status_code == 200
        assert response.json()["dream_enabled"] is True
        assert saved["dream_enabled"] is True


def test_first_hosted_automation_view_bootstraps_the_recommended_policy(
    monkeypatch, tmp_path
):
    """A connected Pro/Team workspace starts maintaining itself without a toggle."""

    uploaded = []
    saved = []

    class _Cloud:
        organization_id = "org_test"

        def get_policy(self, workspace_id):
            # Version zero is the private Cloud's documented no-policy sentinel.
            return {"enabled": False, "cadence_minutes": 1440, "version": 0}

        def upload_snapshot(self, workspace_id, snapshot):
            uploaded.append((workspace_id, snapshot))
            return {"generation": snapshot["generation"]}

        def save_policy(self, workspace_id, policy):
            saved.append((workspace_id, policy))
            return {"version": 1}

        def list_jobs(self, workspace_id, *, limit=10):
            return {"jobs": []}

    monkeypatch.setattr(
        "engraphis.cloud_features.build_managed_snapshot",
        lambda service, workspace: ("ws_cloud", {"generation": 7}),
    )
    monkeypatch.setattr(
        "engraphis.cloud_features.CloudFeatureClient.from_environment",
        lambda workspace_id=None: _Cloud(),
    )
    with _client(monkeypatch, tmp_path) as client:
        response = client.get("/api/automation")

    assert response.status_code == 200
    assert response.json()["enabled"] is True
    assert response.json()["dream"] is True
    assert uploaded == [("ws_cloud", {"generation": 7})]
    assert saved == [("ws_cloud", {
        "enabled": True,
        "cadence_minutes": 1440,
        "dream_enabled": True,
        "dream_min_new": 25,
        "dream_idle_minutes": 15,
        "infer": False,
    })]


def test_first_automation_policy_retry_does_not_upload_the_snapshot_twice(
    monkeypatch, tmp_path
):
    """A failed policy write resumes after the already successful private upload."""

    from engraphis.cloud_features import CloudFeatureError

    uploaded = []
    saved = []
    builds = []

    class _Cloud:
        organization_id = "org_test"

        def get_policy(self, workspace_id):
            return {"enabled": False, "cadence_minutes": 1440, "version": 0}

        def upload_snapshot(self, workspace_id, snapshot):
            uploaded.append((workspace_id, snapshot))
            return {"generation": snapshot["generation"]}

        def save_policy(self, workspace_id, policy):
            saved.append((workspace_id, policy))
            if len(saved) == 1:
                raise CloudFeatureError(
                    "Engraphis Cloud is temporarily unavailable.",
                    status=503,
                    transient=True,
                )
            return {"version": 1}

        def list_jobs(self, workspace_id, *, limit=10):
            return {"jobs": []}

    def _snapshot(service, workspace):
        builds.append(workspace)
        return "ws_cloud", {"generation": 7}

    monkeypatch.setattr("engraphis.cloud_features.build_managed_snapshot", _snapshot)
    monkeypatch.setattr(
        "engraphis.cloud_features.CloudFeatureClient.from_environment",
        lambda workspace_id=None: _Cloud(),
    )
    with _client(monkeypatch, tmp_path) as client:
        first = client.get("/api/automation")
        second = client.get("/api/automation")

    assert first.status_code == 503
    assert second.status_code == 200
    assert len(builds) == 1
    assert uploaded == [("ws_cloud", {"generation": 7})]
    assert len(saved) == 2


def test_concurrent_first_automation_views_upload_one_snapshot(monkeypatch, tmp_path):
    """Parallel dashboard reads serialize the sensitive first-bootstrap upload."""

    uploaded = []
    saved = []
    started = threading.Event()
    release_upload = threading.Event()

    class _Cloud:
        organization_id = "org_concurrent"

        def get_policy(self, workspace_id):
            return {"enabled": False, "cadence_minutes": 1440, "version": 0}

        def upload_snapshot(self, workspace_id, snapshot):
            uploaded.append((workspace_id, snapshot))
            started.set()
            assert release_upload.wait(timeout=5)
            return {"generation": snapshot["generation"]}

        def save_policy(self, workspace_id, policy):
            saved.append((workspace_id, policy))
            return {"version": 1}

        def list_jobs(self, workspace_id, *, limit=10):
            return {"jobs": []}

    monkeypatch.setattr(
        "engraphis.cloud_features.build_managed_snapshot",
        lambda service, workspace: ("ws_cloud", {"generation": 7}),
    )
    monkeypatch.setattr(
        "engraphis.cloud_features.CloudFeatureClient.from_environment",
        lambda workspace_id=None: _Cloud(),
    )
    with _client(monkeypatch, tmp_path):
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(v2_api.automation_get)
            assert started.wait(timeout=5)
            second = pool.submit(v2_api.automation_get)
            release_upload.set()
            assert first.result(timeout=5)["enabled"] is True
            follower = second.result(timeout=5)
            assert follower["enabled"] is True
            assert follower["version"] == 1

    assert uploaded == [("ws_cloud", {"generation": 7})]
    assert len(saved) == 1


def test_reading_or_disabling_automation_never_uploads_memory_content(
    monkeypatch, tmp_path
):
    saved = {}

    class _Cloud:
        def get_policy(self, workspace_id):
            return {"enabled": True, "cadence_minutes": 60, "dream_enabled": True}

        def list_jobs(self, workspace_id, *, limit=10):
            return {"jobs": []}

        def save_policy(self, workspace_id, policy):
            saved.update(policy)
            return {"version": 3}

    def _unexpected_upload(*args, **kwargs):
        raise AssertionError("policy inspection must not build or upload a snapshot")

    monkeypatch.setattr(
        "engraphis.cloud_features.build_managed_snapshot",
        _unexpected_upload,
    )
    monkeypatch.setattr(
        "engraphis.cloud_features.CloudFeatureClient.from_environment",
        lambda workspace_id=None: _Cloud(),
    )
    with _client(monkeypatch, tmp_path) as client:
        assert client.get("/api/automation").status_code == 200
        response = client.post("/api/automation", json={"enabled": False})
        assert response.status_code == 200
        assert saved["enabled"] is False


def test_automation_and_maintenance_use_the_selected_workspace(monkeypatch, tmp_path):
    policy_workspaces = []
    snapshot_workspaces = []
    maintenance_workspaces = []

    class _Cloud:
        def get_policy(self, workspace_id):
            policy_workspaces.append(workspace_id)
            return {"enabled": False, "cadence_minutes": 60, "dream_enabled": True}

        def list_jobs(self, workspace_id, *, limit=10):
            policy_workspaces.append(workspace_id)
            return {"jobs": []}

        def upload_snapshot(self, workspace_id, snapshot):
            snapshot_workspaces.append(workspace_id)
            return {"generation": snapshot["generation"]}

        def save_policy(self, workspace_id, policy):
            policy_workspaces.append(workspace_id)
            return {"version": 1}

    def snapshot(service, workspace):
        snapshot_workspaces.append(workspace)
        return service._lookup_workspace(workspace), {"generation": 1}

    def managed_job(service, workspace, kind):
        maintenance_workspaces.append((workspace, kind))
        return {"result": {"kind": kind}}

    monkeypatch.setattr("engraphis.cloud_features.build_managed_snapshot", snapshot)
    monkeypatch.setattr("engraphis.cloud_features.run_managed_job", managed_job)
    monkeypatch.setattr(
        "engraphis.cloud_features.CloudFeatureClient.from_environment",
        lambda workspace_id=None: _Cloud(),
    )
    with _client(monkeypatch, tmp_path) as client:
        beta_id = client.app.state.service._lookup_workspace("beta")
        demo_id = client.app.state.service._lookup_workspace("demo")
        assert client.get("/api/automation?workspace=beta").status_code == 200
        assert client.post(
            "/api/automation?workspace=beta", json={"enabled": True}
        ).status_code == 200
        assert client.post(
            "/api/maintenance/run?workspace=beta", json={"dry_run": True}
        ).status_code == 200

    assert beta_id in policy_workspaces
    assert demo_id not in policy_workspaces
    assert "beta" in snapshot_workspaces
    assert maintenance_workspaces == [("beta", "consolidate")]


def test_automation_workspace_query_unknown_is_not_replaced_by_legacy_default(
    monkeypatch, tmp_path
):
    with _client(monkeypatch, tmp_path) as client:
        for method, path, payload in (
            (client.get, "/api/automation?workspace=missing", None),
            (client.post, "/api/automation?workspace=missing", {"enabled": False}),
            (client.post, "/api/maintenance/run?workspace=missing", {"dry_run": True}),
        ):
            response = method(path, json=payload) if payload is not None else method(path)
            assert response.status_code == 404


def test_dashboard_automation_uses_active_workspace_and_discloses_upload_boundary():
    source = Path(__file__).parents[1] / "engraphis" / "static" / "dashboard.js"
    source = source.read_text(encoding="utf-8")
    assert "/automation?workspace=" in source
    assert "/maintenance/run?workspace=" in source
    assert "Preview snapshot" not in source
    assert "uploads the selected workspace’s normal and sensitive memory content" in source
    # The upload boundary is still disclosed, but consent now travels with the cloud
    # account: the dashboard must not name the operator override anywhere.
    assert "ENGRAPHIS_MANAGED_COMPUTE_CONSENT" not in source
    assert "Hosted work is automatic with Pro." in source


def test_portfolio_and_report_analytics_are_hosted_only(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        assert client.get("/api/analytics/portfolio").status_code == 501
        assert client.get("/api/analytics/export?workspace=demo").status_code == 501


def test_raw_owner_export_is_free_and_signed_export_is_honestly_unimplemented(
    monkeypatch, tmp_path
):
    """The signed variant must not claim to exist somewhere else.

    It previously answered ``cloud_only: True`` — but Engraphis Cloud has no export route,
    no supported hosted export capability, so that pointed a customer at a
    product that does not exist. The 501 now says the capability is unimplemented and names
    the working unsigned export instead.
    """

    with _client(monkeypatch, tmp_path) as client:
        raw = client.get("/api/export?workspace=demo")
        assert raw.status_code == 200
        assert raw.json()["counts"]["memories"] >= 1
        signed = client.get("/api/export?workspace=demo&signed=true")
        assert signed.status_code == 501
        detail = signed.json()["detail"]
        assert detail["implemented"] is False
        assert detail["alternative"] == "/export"
        assert "cloud_only" not in detail
        assert "Engraphis Cloud" not in detail["error"]


def test_health_and_readiness_remain_public(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/ready").status_code == 200


def test_dashboard_exception_responses_do_not_echo_untrusted_exception_text():
    secret = "https://provider.example/?api_key=do-not-return-this"

    def fail_with(exc):
        raise exc

    with pytest.raises(HTTPException) as internal:
        v2_api._run(fail_with, RuntimeError(secret))
    assert internal.value.status_code == 500
    assert internal.value.detail == {"error": "internal server error"}
    assert secret not in repr(internal.value.detail)

    with pytest.raises(HTTPException) as validation:
        v2_api._run(fail_with, ValidationError(secret))
    assert validation.value.status_code == 400
    assert validation.value.detail == {"error": "invalid request"}
    assert secret not in repr(validation.value.detail)

    with pytest.raises(HTTPException) as downstream:
        v2_api._run(fail_with, HTTPException(status_code=418, detail={"error": secret}))
    assert downstream.value.status_code == 418
    assert downstream.value.detail == {"error": "request rejected"}
    assert secret not in repr(downstream.value.detail)

    with pytest.raises(HTTPException) as invalid_status:
        v2_api._run(fail_with, HTTPException(status_code=999, detail={"error": secret}))
    assert invalid_status.value.status_code == 500
    assert invalid_status.value.detail == {"error": "internal server error"}
    assert secret not in repr(invalid_status.value.detail)

    with pytest.raises(HTTPException) as mismatch:
        v2_api._run(fail_with, ValueError(f"{secret}: shapes 256 and 384 are not aligned"))
    assert mismatch.value.status_code == 409
    assert mismatch.value.detail["embedder"] is True
    assert secret not in repr(mismatch.value.detail)

    with pytest.raises(HTTPException) as ordinary_value_error:
        v2_api._run(fail_with, ValueError(secret))
    assert ordinary_value_error.value.status_code == 500
    assert ordinary_value_error.value.detail == {"error": "internal server error"}
    assert secret not in repr(ordinary_value_error.value.detail)


def test_managed_cloud_errors_forward_only_bounded_public_copy():
    """``_managed_call`` forwards the message; the bound is the boundary's own check.

    ``CloudFeatureError`` is the already-redacted form -- every raise site builds it from
    fixed, status-keyed copy -- so its text is what the customer should read. The bound
    here is not the redaction, it is the guard for a message that is *not* that fixed copy:
    anything oversized, empty, or carrying control characters is dropped for the generic
    placeholder rather than rendered into a JSON error body.
    """

    def fail_with(exc):
        raise exc

    for message in ("x" * 301, "", "connection\x00reset", "trace\x1b[31m"):
        with pytest.raises(HTTPException) as caught:
            v2_api._managed_call(fail_with, CloudFeatureError(message, status=502))
        assert caught.value.status_code == 502
        assert caught.value.detail == {
            "error": v2_api._MANAGED_ERROR_FALLBACK, "managed_cloud": True,
            "transient": False,
        }

    with pytest.raises(HTTPException) as consent:
        v2_api._managed_call(
            fail_with,
            CloudFeatureError(
                "Managed compute is turned off for this installation.",
                status=409, code="consent_required",
            ),
        )
    assert consent.value.status_code == 409
    assert consent.value.detail == {
        "error": "Managed compute is turned off for this installation.",
        "managed_cloud": True,
        "transient": False,
        "code": "consent_required",
    }

    with pytest.raises(HTTPException) as unconfigured:
        v2_api._managed_call(
            fail_with,
            CloudFeatureError(
                "Connect this installation to Engraphis Cloud to use hosted features.",
                status=401, code="cloud_unconfigured",
            ),
        )
    assert unconfigured.value.status_code == 401
    assert unconfigured.value.detail == {
        "error": "Connect this installation to Engraphis Cloud to use hosted features.",
        "managed_cloud": True,
        "transient": False,
        "code": "cloud_unconfigured",
    }


def _managed_http_failure(monkeypatch, status: int) -> HTTPException:
    """Drive one real hosted request against a control plane that answers ``status``."""

    class _Opener:
        def open(self, request, timeout=None):
            raise urllib.error.HTTPError(
                "https://compute.example.test/private", status, "failure", {},
                io.BytesIO(b'{"detail": "provider-internals https://backend.invalid"}'),
            )

    monkeypatch.setattr(
        cloud_features, "build_pinned_https_opener", lambda *handlers: _Opener()
    )
    client = cloud_features.CloudFeatureClient(
        "https://compute.example.test", "org_1", "token"
    )
    with pytest.raises(HTTPException) as caught:
        v2_api._managed_call(client._request, "GET", "/private")
    return caught.value


def test_a_managed_outage_is_distinguishable_from_a_workspace_conflict(monkeypatch):
    """The defect: every hosted failure rendered as one fixed, unactionable string.

    ``cloud_features._public_http_error`` already produces redacted, status-keyed copy that
    tells a retryable outage apart from a conflict the customer has to fix -- and
    ``_managed_call`` threw all of it away, so the dashboard's error branch could only ever
    show "managed cloud operation failed" for a 429, a 5xx and a 409 alike.
    """

    busy = _managed_http_failure(monkeypatch, 429)
    down = _managed_http_failure(monkeypatch, 503)
    conflict = _managed_http_failure(monkeypatch, 409)

    assert busy.status_code == 429
    assert busy.detail["transient"] is True
    assert "temporarily busy" in busy.detail["error"], busy.detail["error"]

    assert down.status_code == 503
    assert down.detail["transient"] is True
    assert "temporarily unavailable" in down.detail["error"], down.detail["error"]

    assert conflict.status_code == 409
    assert conflict.detail["transient"] is False
    assert "workspace state" in conflict.detail["error"], conflict.detail["error"]

    messages = {busy.detail["error"], down.detail["error"], conflict.detail["error"]}
    assert len(messages) == 3, "the dashboard still cannot tell these three apart"
    assert v2_api._MANAGED_ERROR_FALLBACK not in messages
    # Forwarding the public copy must not forward the provider's body with it.
    assert all("provider-internals" not in text for text in messages)
    assert all("backend.invalid" not in text for text in messages)


def test_every_managed_cloud_error_message_is_fixed_local_copy():
    """The invariant that makes forwarding safe, pinned against future raise sites.

    ``_managed_call`` may forward a ``CloudFeatureError`` message only because every one of
    them is built from a literal in this repository -- never from a provider body, a
    ``CloudSessionError``, or a local path. A raise site that interpolated a runtime value
    would silently turn this boundary into a reflection point, so the shape is asserted
    rather than trusted.

    Three forms are accepted: a string literal; a name bound from ``_public_http_error`` /
    ``_public_session_error`` (both of which switch on a bare integer status and return
    fixed copy); and the one audited ``%`` template, below.
    """

    source = Path(cloud_features.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    public_copy = {"_public_http_error", "_public_session_error"}
    from_public_copy = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        called = node.value.func
        if not isinstance(called, ast.Name) or called.id not in public_copy:
            continue
        for target in node.targets:
            elements = target.elts if isinstance(target, ast.Tuple) else [target]
            from_public_copy.update(
                item.id for item in elements if isinstance(item, ast.Name)
            )
    assert from_public_copy, "the fixed-copy helpers are no longer bound to a name"

    interpolated = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.id if isinstance(node.func, ast.Name) else None
        if name != "CloudFeatureError" or not node.args:
            continue
        message = node.args[0]
        if isinstance(message, ast.Constant) and isinstance(message.value, str):
            continue
        if isinstance(message, ast.Name) and message.id in from_public_copy:
            continue
        # ``"literal %s" % (...)`` is allowed only where the substituted values are
        # themselves constrained to local literals; ``run_job`` is the single such site
        # and its ``status`` is guarded by an ``in {"failed", "canceled"}`` membership
        # test one line above. Anything else -- an f-string, a bare name, a concatenated
        # response field -- is a reflection risk and fails here.
        if (isinstance(message, ast.BinOp) and isinstance(message.op, ast.Mod)
                and isinstance(message.left, ast.Constant)
                and message.left.value == "Managed %s did not complete (%s)."):
            continue
        interpolated.append((node.lineno, ast.dump(message)[:120]))

    assert interpolated == [], (
        "a CloudFeatureError message is no longer fixed local copy; _managed_call "
        "forwards it to the customer: %r" % (interpolated,)
    )
