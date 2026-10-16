"""Integration tests for spec crawl service and REST API endpoints."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="spec crawl HTTP route needs the server extra")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from engraphis.routes import v2_api
from engraphis.service import MemoryService
from engraphis.service import ValidationError


def test_service_spec_crawl_and_answer():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test_spec.db")
        svc = MemoryService.create(db_path)
        try:
            ws = "default"

            # 1. Spec crawl direct text
            spec = """# 01 ROLE
Senior maintainer.

# 02 RULES
We must verify all unit tests before release.
"""
            report = svc.spec_crawl(spec, workspace=ws, trace_claims=True)
            assert report["score"] > 0
            assert len(report["sections"]) == 2
            assert any(f["token"] == "all" for f in report["flags"])

            vague_flag = next(f for f in report["flags"] if f["token"] == "all")

            # 2. Spec crawl answer: clarify vague flag and save as procedural memory
            ans_res = svc.spec_crawl_answer(
                vague_flag,
                "100% of",
                spec_text=spec,
                workspace=ws,
                save_as_memory=True,
            )

            assert "100% of" in ans_res["updated_text"]
            assert ans_res["memory_id"] is not None
            assert ans_res["new_report"] is not None

            # 3. Procedural memories crawl
            proc_report = svc.spec_crawl(workspace=ws, crawl_procedural=True)
            assert proc_report["score"] > 0
            assert len(proc_report["sections"]) >= 1
        finally:
            svc.close()


@pytest.fixture
def crawl_service():
    svc = MemoryService.create(":memory:")
    try:
        yield svc
    finally:
        svc.close()


def test_service_boolean_controls_do_not_coerce_strings(crawl_service):
    svc = crawl_service
    text = "# Rules\nVerify all tests."
    flag = svc.spec_crawl(text)["flags"][0]
    before = svc.store.conn.total_changes
    with pytest.raises(ValidationError, match="save_as_memory"):
        svc.spec_crawl_answer(flag, "12", spec_text=text, save_as_memory="false")
    for control in ("crawl_procedural", "trace_claims", "include_trace"):
        with pytest.raises(ValidationError, match=control):
            svc.spec_crawl(text, **{control: "false"})
    assert svc.store.conn.total_changes == before


@pytest.mark.parametrize("explicit_selection", [False, True])
def test_service_preserves_memory_word_truncation_status(crawl_service, explicit_selection):
    from engraphis.core.spec_crawl import MAX_SPEC_WORDS

    svc = crawl_service
    memory_id = svc.remember(
        "neutral " * MAX_SPEC_WORDS + "every result.", workspace="alpha",
        resolve_conflicts=False,
    )["id"]
    before = svc.store.conn.total_changes
    report = svc.spec_crawl_memories(
        workspace="alpha", memory_ids=[memory_id] if explicit_selection else None,
    )
    assert svc.store.conn.total_changes == before
    assert report["node_count"] == report["total_eligible_node_count"] == 1
    assert report["node_selection_truncated"] is False
    assert report["analysis_truncated"] is report["truncated"] is True
    assert report["word_analysis_truncated_node_count"] == 1
    assert report["nodes"][0]["word_count_is_lower_bound"] is True
    assert report["score_breakdown"]["quality_points"] == 0


def test_clarification_persistence_is_opt_in_and_keeps_requested_session(crawl_service):
    svc = crawl_service
    session_id = svc.start_session("alpha", repo="app", agent="codex")["session_id"]
    text = "# Rules\nVerify all tests."
    flag = svc.spec_crawl(text, workspace="alpha")["flags"][0]
    before = svc.store.conn.total_changes
    result = svc.spec_crawl_answer(flag, "12", spec_text=text, workspace="alpha",
                                   repo="app", session_id=session_id)
    assert result["memory_id"] is None
    assert svc.store.conn.total_changes == before
    saved = svc.spec_crawl_answer(flag, "12", spec_text=text, workspace="alpha",
                                 repo="app", session_id=session_id, save_as_memory=True)
    record = svc.store.get_memory(saved["memory_id"])
    assert record.scope.value == "session"
    assert record.session_id == session_id
    from engraphis.core.poisoning import prompt_eligible
    assert not prompt_eligible(record.provenance, record.metadata)


def test_http_spec_schemas_reject_incomplete_or_oversized_inputs_before_writing(crawl_service):
    svc = crawl_service
    v2_api.set_service(svc)
    app = FastAPI()
    app.include_router(v2_api.router)
    before = svc.store.conn.total_changes
    with TestClient(app) as client:
        for node_b in ({}, {"node_b": None}, {"node_b": ""}):
            result = client.post("/api/spec/crawl/memories/resolve", json={
                "action": "link", "node_a": "first", **node_b,
            })
            assert result.status_code == 422
        for values in ({"memory_ids": ["mem"] * 151}, {"mtypes": ["procedural"] * 5}):
            assert client.post("/api/spec/crawl/memories", json=values).status_code == 422
    assert svc.store.conn.total_changes == before


@pytest.mark.parametrize("include_smaller_memory", [False, True])
def test_procedural_crawl_cannot_fabricate_empty_scope_when_source_exceeds_budget(
    crawl_service, monkeypatch, include_smaller_memory,
):
    svc = crawl_service
    records = []
    oversized = svc.remember(
        "Verify approved source evidence before release.\n" + "x" * 66_000,
        workspace="alpha", title="Oversized rules", mtype="procedural", trusted=True,
    )
    records.append(svc.store.get_memory(oversized["id"]))
    if include_smaller_memory:
        smaller = svc.remember(
            "Run Ruff before release.", workspace="alpha", title="Lint rule",
            mtype="procedural", trusted=True,
        )
        records.append(svc.store.get_memory(smaller["id"]))
    # A newest/first oversized record exhausts the whole-memory composition budget.
    monkeypatch.setattr(svc.store, "list_memories", lambda *args, **kwargs: records)
    with pytest.raises(ValidationError, match="64000-character audit limit"):
        svc.spec_crawl(workspace="alpha", crawl_procedural=True, trace_claims=False)


def test_memory_crawl_never_broadens_unknown_or_explicit_scopes(crawl_service):
    svc = crawl_service
    alpha = svc.remember("Alpha configuration port: 5432", workspace="alpha", repo="one", trusted=True)
    beta = svc.remember("Beta configuration port: 8000", workspace="beta", trusted=True)
    assert svc.spec_crawl_memories(workspace="alpha", memory_ids=[beta["id"]])["node_count"] == 0
    assert svc.spec_crawl_memories(workspace="alpha", memory_ids=[])["node_count"] == 0
    assert svc.spec_crawl_memories(workspace="alpha", memory_ids=[alpha["id"]])["node_count"] == 1
    for method in (svc.spec_crawl_memories, lambda **kw: svc.spec_crawl(crawl_procedural=True, **kw)):
        with pytest.raises(ValidationError):
            method(workspace="missing")
        with pytest.raises(ValidationError):
            method(workspace="alpha", repo="missing")


def test_memory_crawl_excludes_retired_and_pending_records(crawl_service):
    svc = crawl_service
    approved = svc.remember("Approved configuration", workspace="alpha", trusted=True)
    pending = svc.remember("Pending imported rules", workspace="alpha", source="spec_crawl")
    svc.retire(approved["id"], workspace="alpha")
    assert svc.spec_crawl_memories(workspace="alpha", memory_ids=[approved["id"], pending["id"]])["node_count"] == 0


def test_spec_crawl_preserves_session_owner_boundary(crawl_service, monkeypatch):
    svc = crawl_service
    svc.create_workspace("team", visibility="shared", confirmed=True)
    principal = {"id": "usr_alice", "email": "alice@example.test", "role": "member"}
    monkeypatch.setattr("engraphis.service._authenticated_principal", lambda: principal)
    session_id = svc.start_session("team", repo="app", agent="codex")["session_id"]
    private = svc.remember("Alice private procedure", workspace="team", repo="app",
                           session_id=session_id, scope="session", mtype="procedural", trusted=True)
    principal = {"id": "usr_bob", "email": "bob@example.test", "role": "member"}
    assert svc.spec_crawl_memories(workspace="team", memory_ids=[private["id"]])["node_count"] == 0
    for method in (svc.spec_crawl_memories, lambda **kw: svc.spec_crawl(crawl_procedural=True, **kw)):
        with pytest.raises(ValidationError, match="another user"):
            method(workspace="team", session_id=session_id)


def test_resolution_requires_confirmation_and_ownership_before_writing(crawl_service):
    svc = crawl_service
    a = svc.remember("Alpha database port: 5432", workspace="alpha", trusted=True)["id"]
    b = svc.remember("Beta database port: 8000", workspace="beta", trusted=True)["id"]
    with pytest.raises(ValidationError, match="confirmation"):
        svc.spec_crawl_resolve(action="supersede", node_a=a, node_b=b, workspace="alpha")
    with pytest.raises(ValidationError):
        svc.spec_crawl_resolve(action="supersede", node_a=a, node_b=b, workspace="alpha", confirmed=True)
    assert svc.store.get_memory(b).valid_to is None
    with pytest.raises(ValidationError):
        svc.spec_crawl_resolve(action="supersede", node_a="missing", node_b=a, workspace="alpha", confirmed=True)
    assert svc.store.get_memory(a).valid_to is None


def test_resolution_rolls_back_all_changes_on_lifecycle_failure(crawl_service, monkeypatch):
    svc = crawl_service
    a = svc.remember("New database port: 5433", workspace="alpha", trusted=True)["id"]
    b = svc.remember("Old database port: 5432", workspace="alpha", trusted=True)["id"]
    def unavailable(*args, **kwargs):
        raise RuntimeError("retire unavailable")
    monkeypatch.setattr(svc, "retire", unavailable)
    with pytest.raises(RuntimeError):
        svc.spec_crawl_resolve(action="supersede", node_a=a, node_b=b, workspace="alpha", confirmed=True)
    assert svc.store.get_memory(b).valid_to is None
    assert not any(link["relation"] == "supersedes" for link in svc.store.get_links(a))


def test_read_only_crawl_leaves_database_unchanged(crawl_service):
    svc = crawl_service
    svc.remember("SQLite stores repository memories.", workspace="alpha", trusted=True)
    before = svc.store.conn.total_changes
    svc.spec_crawl("# Context\nSQLite stores repository memories.", workspace="alpha", trace_claims=True)
    svc.spec_crawl_memories(workspace="alpha")
    assert svc.store.conn.total_changes == before


def test_invalid_or_oversized_clarification_writes_nothing(crawl_service):
    svc = crawl_service
    spec = "# RULES\nall " + "x" * 63_988
    flag = next(f for f in svc.spec_crawl(spec, trace_claims=False)["flags"] if f["token"] == "all")
    before = svc.store.conn.total_changes
    with pytest.raises(ValidationError):
        svc.spec_crawl_answer(flag, "the 15 required smoke modules", spec_text=spec, workspace="default")
    with pytest.raises(ValidationError):
        svc.spec_crawl_answer({**flag, "token": "fake"}, "15 modules", spec_text=spec)
    assert svc.store.conn.total_changes == before


def test_source_files_require_local_operator_and_approved_root(crawl_service, tmp_path, monkeypatch):
    svc = crawl_service
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    source = allowed / "AGENTS.md"
    source.write_text("# Rules\nRun 15 tests.", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("Private sentinel", encoding="utf-8")
    monkeypatch.setenv("ENGRAPHIS_INDEX_ROOTS", str(allowed))
    assert svc.spec_crawl(source_id=str(source), trace_claims=False)["word_count"] > 0
    with pytest.raises(ValidationError):
        svc.spec_crawl(source_id=str(outside))
    monkeypatch.setattr("engraphis.service._authenticated_principal", lambda: {"id": "member"})
    with pytest.raises(ValidationError, match="local operator"):
        svc.spec_crawl(source_id=str(source), workspace="default")


def test_api_spec_crawl_endpoints():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test_api_spec.db")
        svc = MemoryService.create(db_path)
        v2_api.set_service(svc)
        app = FastAPI()
        app.include_router(v2_api.router)
        client = TestClient(app)

        try:
            # POST /api/spec/crawl
            rejected_source = client.post("/api/spec/crawl", json={"source_id": "AGENTS.md"})
            assert rejected_source.status_code == 422
            rejected_with_text = client.post("/api/spec/crawl", json={
                "source_id": "AGENTS.md", "text": "# Rules\nRun 15 tests.",
            })
            assert rejected_with_text.status_code == 422
            resp = client.post(
                "/api/spec/crawl",
                json={
                    "text": "# 01 ROLE\nDeveloper.\n# 02 RULES\nTest everything.\n",
                    "workspace": "default",
                    "trace_claims": False,
                    "include_trace": True,
                },
            )
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["score"] > 0
            assert len(data["sections"]) == 2
            assert len(data["flags"]) >= 1

            flag = data["flags"][0]

            # POST /api/spec/crawl/answer
            ans_resp = client.post(
                "/api/spec/crawl/answer",
                json={
                    "flag": flag,
                    "answer": "all 15 modules",
                    "spec_text": "# 01 ROLE\nDeveloper.\n# 02 RULES\nTest everything.\n",
                    "workspace": "default",
                    "save_as_memory": True,
                },
            )
            assert ans_resp.status_code == 200, ans_resp.text
            ans_data = ans_resp.json()
            assert "all 15 modules" in ans_data["updated_text"]
            assert ans_data["memory_id"] is not None
        finally:
            v2_api._service = None
            svc.close()


def test_api_spec_crawl_memories_and_resolve():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test_cluster_api.db")
        svc = MemoryService.create(db_path)
        v2_api.set_service(svc)
        app = FastAPI()
        app.include_router(v2_api.router)
        client = TestClient(app)

        try:
            # Seed 2 conflicting memories and 1 orphan
            m1 = svc.remember(
                "Legacy service uses database port: 5432", title="Legacy DB Config",
                workspace="default", subject_key="db.primary", claim_kind="port",
                valid_from=1000.0, resolve_conflicts=False,
            )
            m2 = svc.remember(
                "Primary service uses database port: 5433", title="Primary DB Config",
                workspace="default", subject_key="db.primary", claim_kind="port",
                valid_from=2000.0, resolve_conflicts=False,
            )
            m3 = svc.remember("SOC2 compliance policy requires audit logging", title="Audit Policy", workspace="default")

            # 1. Audit cluster
            resp = client.post("/api/spec/crawl/memories", json={"workspace": "default", "include_trace": True})
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["node_count"] == 3
            assert len(data["conflicts"]) == 1
            assert data["conflicts"][0]["subject"] == "port"
            assert len(data["orphans"]) >= 1

            # 2. Resolve conflict (supersede m1 with newer m2)
            res_conflict = client.post(
                "/api/spec/crawl/memories/resolve",
                json={
                    "action": "supersede",
                    "confirmed": True,
                    "node_a": m2["id"],
                    "node_b": m1["id"],
                    "workspace": "default",
                },
            )
            assert res_conflict.status_code == 200, res_conflict.text
            c_data = res_conflict.json()
            assert c_data["ok"] is True
            assert c_data["action"] == "supersede"
            assert "Superseded" in c_data["message"]

            # 3. Resolve orphan (link m2 to m3)
            res_link = client.post(
                "/api/spec/crawl/memories/resolve",
                json={
                    "action": "link",
                    "node_a": m2["id"],
                    "node_b": m3["id"],
                    "workspace": "default",
                },
            )
            assert res_link.status_code == 200, res_link.text
            l_data = res_link.json()
            assert l_data["ok"] is True
            assert l_data["action"] == "link"
            assert "Linked" in l_data["message"]

            # 4. Re-audit cluster
            re_resp = client.post("/api/spec/crawl/memories", json={"workspace": "default"})
            assert re_resp.status_code == 200
            re_data = re_resp.json()
            # m1 was superseded (validity closed), so live active nodes are 2 and 0 conflicts
            assert re_data["node_count"] == 2
            assert len(re_data["conflicts"]) == 0
        finally:
            v2_api._service = None
            svc.close()


@pytest.mark.parametrize("failed", [True, False], ids=["backend_failure", "successful_abstention"])
def test_claim_lookup_failures_remain_unchecked(crawl_service, monkeypatch, failed):
    from engraphis.core.grounded import GroundedAnswer
    from engraphis.core.spec_crawl import MAX_TRACED_CLAIMS

    svc = crawl_service
    svc.create_workspace("alpha")
    text = "# Context\n" + "The server has port 123.\n" * 100
    baseline = svc.spec_crawl(text, workspace="alpha", trace_claims=False)
    before = svc.store.conn.total_changes
    attempts = []

    def lookup(query, **kwargs):
        attempts.append(kwargs)
        if failed:
            raise RuntimeError("index unavailable")
        return GroundedAnswer()

    monkeypatch.setattr(svc.engine, "grounded_recall", lookup)
    report = svc.spec_crawl(text, workspace="alpha", trace_claims=True)
    assert len(attempts) == MAX_TRACED_CLAIMS
    assert all(kwargs["reinforce"] is False for kwargs in attempts)
    assert report["counts"]["claim_lookup_attempts"] == MAX_TRACED_CLAIMS
    assert report["counts"]["claims_traced"] == 0
    assert svc.store.conn.total_changes == before
    if failed:
        assert report["counts"]["claims_checked"] == 0
        assert all(claim["status"] == "unchecked" for claim in report["claims"])
        assert not any(flag["kind"] == "untraced" for flag in report["flags"])
        assert report["score"] == baseline["score"]
        assert report["score_breakdown"] == baseline["score_breakdown"]
    else:
        assert report["counts"]["claims_checked"] == MAX_TRACED_CLAIMS
        assert sum(claim["status"] == "untraced" for claim in report["claims"]) == MAX_TRACED_CLAIMS
        assert report["score"] < baseline["score"]


def test_backfilled_recommendation_preserves_current_fact_and_history(crawl_service, monkeypatch):
    svc = crawl_service
    current = svc.remember(
        "port: 5433", workspace="alpha", trusted=True, subject_key="db.primary",
        claim_kind="port", valid_from=2000.0, resolve_conflicts=False,
    )["id"]
    historical = svc.remember(
        "port: 5432", workspace="alpha", trusted=True, subject_key="db.primary",
        claim_kind="port", valid_from=1000.0, resolve_conflicts=False,
    )["id"]
    assert svc.store.get_memory(historical).ingested_at >= svc.store.get_memory(current).ingested_at
    remedy = svc.spec_crawl_memories(workspace="alpha")["conflicts"][0]["remedy"]
    assert remedy["action"] == "supersede"
    assert remedy["ordering_basis"] == "valid_from"
    assert remedy["keep_node"] == current
    assert remedy["retire_node"] == historical
    recording_time = max(svc.store.get_memory(mid).ingested_at for mid in (current, historical)) + 2
    monkeypatch.setattr("engraphis.core.store.now_ts", lambda: recording_time)
    resolved = svc.spec_crawl_resolve(
        action=remedy["action"], node_a=remedy["keep_node"], node_b=remedy["retire_node"],
        workspace="alpha", confirmed=True,
    )
    assert resolved["retained_node"] == current
    assert resolved["superseded_node"] == historical
    assert svc.store.get_memory(current).valid_to is None
    retired = svc.store.get_memory(historical)
    assert retired.valid_to == 2000.0
    from engraphis.core.interfaces import SearchFilter
    wid = svc.store.get_memory(current).workspace_id
    assert svc.store.visible_memory_ids([historical, current], SearchFilter(
        workspace_id=wid, valid_at=1500.0,
    )) == {historical}
    assert svc.store.visible_memory_ids([historical, current], SearchFilter(
        workspace_id=wid, valid_at=2500.0,
    )) == {current}
    assert svc.store.visible_memory_ids([historical, current], SearchFilter(
        workspace_id=wid, valid_at=2500.0, known_at=recording_time - 1,
    )) == {historical, current}
    assert svc.store.get_memory(historical).content == "port: 5432"
    assert svc.store.get_memory(current).content == "port: 5433"
    # Supersession is lifecycle lineage and remains queryable after retiring its predecessor.
    historical_link = svc.store.conn.execute(
        "SELECT a,b,valid_to FROM mem_links WHERE a=? AND b=? AND relation=?",
        (current, historical, "supersedes"),
    ).fetchone()
    assert historical_link is not None
    assert any(link["relation"] == "supersedes" and link["b"] == historical
               for link in svc.store.get_links(current))
    assert [entry["id"] for entry in svc.inspect(current, workspace="alpha")["chain"]] == [historical, current]


def test_supersession_lineage_preserves_system_time_and_excludes_associative_links(crawl_service, monkeypatch):
    from engraphis.core.interfaces import SearchFilter
    svc = crawl_service
    old = svc.remember("Historical policy", workspace="alpha", trusted=True, valid_from=1000)["id"]
    current = svc.remember("Current policy", workspace="alpha", trusted=True, valid_from=2000)["id"]
    unrelated = svc.remember("Other procedure", workspace="alpha", trusted=True)["id"]
    stamp = max(svc.store.get_memory(mid).ingested_at for mid in (old, current, unrelated)) + 10
    monkeypatch.setattr("engraphis.core.store.now_ts", lambda: stamp)
    svc.link(current, unrelated, workspace="alpha", relation="related_to")
    svc.spec_crawl_resolve(action="supersede", node_a=current, node_b=old, workspace="alpha", confirmed=True)
    wid = svc.store.get_memory(current).workspace_id
    before = svc.store.get_links(current, flt=SearchFilter(workspace_id=wid, valid_at=stamp, known_at=stamp - 1))
    assert not any(link["relation"] == "supersedes" for link in before)
    after = svc.store.get_links(current, flt=SearchFilter(workspace_id=wid, valid_at=stamp, known_at=stamp))
    assert any(link["relation"] == "supersedes" for link in after)
    assert {entry["id"] for entry in svc.inspect(current, workspace="alpha")["chain"]} == {old, current}
    assert {entry["id"] for entry in svc.inspect(old, workspace="alpha")["chain"]} == {old, current}
    svc.store.conn.execute("UPDATE memories SET expired_at=? WHERE id=?", (stamp - 1, old))
    assert not any(link["relation"] == "supersedes" for link in svc.store.get_links(current))


@pytest.mark.parametrize("eligible_count", [150, 151])
def test_cluster_sample_reports_truncation_without_counting_pending_memories(crawl_service, monkeypatch, eligible_count):
    svc = crawl_service
    monkeypatch.setattr(svc.engine, "_evolve", lambda *args, **kwargs: None)
    ids = [svc.remember(f"Procedure number {i}", workspace="alpha", trusted=True,
                        resolve_conflicts=False)["id"] for i in range(eligible_count)]
    svc.remember("Pending procedure", workspace="alpha", source="spec_crawl", resolve_conflicts=False)
    report = svc.spec_crawl_memories(workspace="alpha", include_trace=False)
    assert report["node_count"] == 150
    assert report["truncated"] is (eligible_count > 150)
    assert report["node_limit"] == 150
    assert report["count_semantics"] == "bounded_scope_sample"
    selected = svc.spec_crawl_memories(workspace="alpha", memory_ids=ids[:2])
    assert selected["node_count"] == 2 and selected["truncated"] is False
    assert selected["count_semantics"] == "requested_selection"
    assert report["total_eligible_node_count"] == eligible_count
    assert report["analysis_truncated"] is report["truncated"]


def test_lineage_chain_preserves_zero_effective_dates_and_terminates_cycles(crawl_service):
    svc = crawl_service
    ids = [svc.remember(f"Policy at effective time {stamp}", workspace="alpha", trusted=True,
                        valid_from=stamp, resolve_conflicts=False)["id"] for stamp in (0, 1000, 2000)]
    for retained, retired in ((ids[1], ids[0]), (ids[2], ids[1])):
        svc.spec_crawl_resolve(action="supersede", node_a=retained, node_b=retired,
                               workspace="alpha", confirmed=True)
    svc.link(ids[0], ids[2], workspace="alpha", relation="supersedes")
    assert [entry["id"] for entry in svc.inspect(ids[2], workspace="alpha")["chain"]] == ids


def test_remediation_requires_one_explicit_session_scope(crawl_service):
    svc = crawl_service
    first = svc.start_session("alpha", repo="app", agent="codex")["session_id"]
    second = svc.start_session("alpha", repo="app", agent="codex", force_new=True)["session_id"]
    ids = [svc.remember(f"Private procedure {i}", workspace="alpha", repo="app", session_id=sid,
                        scope="session", trusted=True, resolve_conflicts=False)["id"]
           for i, sid in enumerate((first, first, second))]
    for scope in ({"workspace": "alpha", "repo": "app"}, {"session_id": first}):
        with pytest.raises(ValidationError):
            svc.spec_crawl_resolve(action="link", node_a=ids[0], node_b=ids[2], **scope)
    assert svc.spec_crawl_resolve(action="link", node_a=ids[0], node_b=ids[1], session_id=first)["ok"]


@pytest.mark.parametrize("action", ["supersede", "link"])
@pytest.mark.parametrize("ancestor_is_retained", [True, False])
def test_repo_resolution_accepts_visible_workspace_ancestors(crawl_service, action, ancestor_is_retained):
    svc = crawl_service
    ancestor = svc.remember("Workspace policy port: 5433", workspace="alpha", trusted=True,
                            valid_from=2000 if ancestor_is_retained else 1000,
                            resolve_conflicts=False)["id"]
    local = svc.remember("Repository policy port: 5432", workspace="alpha", repo="one", trusted=True,
                         valid_from=1000 if ancestor_is_retained else 2000,
                         resolve_conflicts=False)["id"]
    assert {node["id"] for node in svc.spec_crawl_memories(workspace="alpha", repo="one")["nodes"]} == {ancestor, local}
    retained, retired = (ancestor, local) if ancestor_is_retained else (local, ancestor)
    result = svc.spec_crawl_resolve(action=action, node_a=retained, node_b=retired,
                                   workspace="alpha", repo="one", confirmed=True)
    assert result["ok"] is True
    if action == "supersede":
        assert svc.store.get_memory(retired).valid_to == 2000
    else:
        assert any(link["a"] == retained and link["b"] == retired for link in svc.store.get_links(retained))


@pytest.mark.parametrize("action", ["supersede", "link"])
def test_repo_resolution_rejects_other_repositories_atomically(crawl_service, action):
    svc = crawl_service
    local = svc.remember("Local policy", workspace="alpha", repo="one", trusted=True)["id"]
    other = svc.remember("Other policy", workspace="alpha", repo="two", trusted=True)["id"]
    with pytest.raises(ValidationError):
        svc.spec_crawl_resolve(action=action, node_a=local, node_b=other,
                               workspace="alpha", repo="one", confirmed=True)
    assert svc.store.get_memory(other).valid_to is None
    assert not any(link["relation"] in {"supersedes", "related_to"} for link in svc.store.get_links(local))


def test_reversed_effective_time_resolution_rolls_back_link_and_receipt(crawl_service):
    svc = crawl_service
    earlier = svc.remember("Earlier policy", workspace="alpha", trusted=True, valid_from=1000)["id"]
    later = svc.remember("Later policy", workspace="alpha", trusted=True, valid_from=2000)["id"]
    receipts = svc.store.conn.execute("SELECT COUNT(*) FROM operation_receipts").fetchone()[0]
    with pytest.raises(ValidationError, match="predate"):
        svc.spec_crawl_resolve(action="supersede", node_a=earlier, node_b=later,
                               workspace="alpha", confirmed=True)
    assert svc.store.get_memory(later).valid_to is None
    assert not any(link["relation"] == "supersedes" for link in svc.store.get_links(earlier))
    assert svc.store.conn.execute("SELECT COUNT(*) FROM operation_receipts").fetchone()[0] == receipts


def test_cluster_audit_deduplicates_persisted_links_before_inference(crawl_service, monkeypatch):
    svc = crawl_service
    monkeypatch.setattr(svc.engine, "_evolve", lambda *args, **kwargs: None)
    ids = [svc.remember(f"Deployment procedure for service {i}", workspace="alpha", trusted=True,
                        resolve_conflicts=False)["id"] for i in range(4)]
    svc.link(ids[0], ids[1], workspace="alpha", relation="related_to")
    svc.link(ids[2], ids[3], workspace="alpha", relation="related_to")
    report = svc.spec_crawl_memories(workspace="alpha", memory_ids=ids)
    declared = [link for link in report["links"] if not link["inferred"]]
    assert len(declared) == 2
    assert len({(link["a"], link["b"], link["relation"]) for link in declared}) == 2
    assert any(link["inferred"] for link in report["links"])
