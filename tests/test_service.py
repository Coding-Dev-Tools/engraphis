"""Offline tests for the MemoryService facade (numpy-only, no model download, no mcp).

Covers the validated write/read path the MCP server delegates to: round-trip recall,
scope isolation, session lifecycle, input validation, and untrusted-content sanitization
(the memory-poisoning guard), plus conflict resolution, governance, and the bi-temporal
why/timeline/proactive tools.
"""
import pytest

from engraphis.service import MemoryService, ValidationError


def _svc() -> MemoryService:
    return MemoryService.create(":memory:")


def test_remember_then_recall_roundtrip():
    s = _svc()
    out = s.remember("We use pnpm as the package manager for all frontend repos.",
                     workspace="acme", repo="web")
    assert out["stored"] is True
    assert out["id"].startswith("mem_")
    assert out["scope"] == "repo" and out["mtype"] == "semantic"

    r = s.recall("which package manager for the frontend?", workspace="acme", repo="web")
    assert r["count"] >= 1
    assert "pnpm" in r["context"]
    assert any("pnpm" in m["content"] for m in r["memories"])


def test_recall_distinguishes_query_relative_rank_from_absolute_support():
    s = _svc()
    s.remember("Frontend repositories use pnpm for package management.",
               workspace="acme", repo="web")

    full = s.recall("which package manager do frontend repositories use?",
                    workspace="acme", repo="web")
    memory = full["memories"][0]
    assert memory["score"] == memory["relative_score"]  # compatibility alias
    assert 0.0 <= memory["absolute_support"] <= 1.0
    assert "Query-relative" in full["score_semantics"]["relative_score"]
    assert "[0, 1]" in full["score_semantics"]["absolute_support"]

    compact = s.recall("which package manager do frontend repositories use?",
                       workspace="acme", repo="web", response_mode="compact")
    compact_memory = compact["memories"][0]
    assert compact_memory["relative_score"] == compact_memory["score"]
    assert 0.0 <= compact_memory["absolute_support"] <= 1.0
    assert compact["score_semantics"] == full["score_semantics"]


def test_recall_support_reuses_vector_arm_without_a_second_embedding_batch():
    s = _svc()
    s.remember("Frontend repositories use pnpm for package management.",
               workspace="acme", repo="web")

    class CountingEmbedder:
        def __init__(self, wrapped):
            self.wrapped = wrapped
            self.batches = []

        def embed(self, texts):
            self.batches.append(list(texts))
            return self.wrapped.embed(texts)

    counter = CountingEmbedder(s.engine.recall_engine.embedder)
    s.engine.recall_engine.embedder = counter
    result = s.recall("which package manager do frontend repositories use?",
                      workspace="acme", repo="web")

    assert len(counter.batches) == 1
    assert counter.batches[0] == ["which package manager do frontend repositories use?"]
    assert result["score_semantics"]["version"] == "retrieval-support-v1"


def test_recall_absolute_support_stays_low_for_a_weak_one_item_pool():
    s = _svc()
    s.remember("Production deploys to AWS ECS after approval.",
               workspace="acme", repo="web")

    result = s.recall("What sourdough hydration ratio should I use?",
                      workspace="acme", repo="web")
    memory = result["memories"][0]

    assert memory["relative_score"] > 0.5
    assert memory["absolute_support"] < 0.15


def test_reworded_rate_limit_requires_claim_key_to_supersede_offline():
    s = _svc()
    old_text = "The API rate limit is one hundred requests every sixty seconds."
    new_text = "Calls are capped at 500 per minute for each key."

    unkeyed_old = s.remember(old_text, workspace="unkeyed", repo="api")
    unkeyed_new = s.remember(new_text, workspace="unkeyed", repo="api")
    assert unkeyed_new["op"] == "add"
    assert s.store.get_memory(unkeyed_old["id"]).valid_to is None

    keyed_old = s.remember(
        old_text, workspace="keyed", repo="api", subject_key="api-rate-limit",
        claim_kind="configured_value",
    )
    keyed_new = s.remember(
        new_text, workspace="keyed", repo="api", subject_key="api-rate-limit",
        claim_kind="configured_value",
    )
    assert keyed_new["op"] == "invalidate"
    assert keyed_new["superseded"] == [keyed_old["id"]]
    assert s.store.get_memory(keyed_old["id"]).valid_to is not None


@pytest.mark.parametrize("method", ("remember", "ingest"))
def test_invalid_trust_label_is_rejected_before_scope_creation(method):
    s = _svc()

    with pytest.raises(ValidationError, match="trusted must be a boolean"):
        getattr(s, method)("untrusted input", workspace="must-not-exist", trusted="false")

    assert s.list_workspaces()["workspaces"] == []


def test_service_recall_does_not_reinforce_weak_results_by_default():
    s = _svc()
    stored = s.remember("The deployment target is AWS ECS.", workspace="acme", repo="web")
    before = s.store.get_memory(stored["id"]).access_count

    s.recall("unrelated lunch menu", workspace="acme", repo="web", k=1)
    assert s.store.get_memory(stored["id"]).access_count == before

    s.recall("deployment target", workspace="acme", repo="web", k=1, reinforce=True)
    assert s.store.get_memory(stored["id"]).access_count > before


def test_scope_isolation_by_workspace():
    s = _svc()
    s.remember("Secret alpha fact about widgets.", workspace="alpha")
    s.remember("Secret beta fact about gadgets.", workspace="beta")
    r = s.recall("fact", workspace="alpha")
    assert r["count"] >= 1
    assert all(m["content"] != "Secret beta fact about gadgets." for m in r["memories"])


def test_repo_recall_inherits_workspace_memories():
    s = _svc()
    workspace = s.remember(
        "Scopeprobe: every repository must use signed commits.",
        workspace="acme", scope="workspace",
    )
    repo = s.remember(
        "Scopeprobe: the web repository deploys on tags.",
        workspace="acme", repo="web", scope="repo",
    )

    recalled = s.recall("scopeprobe", workspace="acme", repo="web", k=10)
    ids = {memory["id"] for memory in recalled["memories"]}

    assert {workspace["id"], repo["id"]} <= ids


def test_session_recall_is_exact_and_inherits_ancestors():
    s = _svc()
    workspace = s.remember(
        "Scopeprobe workspace convention.", workspace="acme", scope="workspace"
    )
    repo = s.remember(
        "Scopeprobe repo convention.", workspace="acme", repo="web", scope="repo"
    )
    first_session = s.start_session("acme", repo="web", goal="first", force_new=True)
    second_session = s.start_session("acme", repo="web", goal="second", force_new=True)
    first = s.remember(
        "Scopeprobe private first-session state.", workspace="acme", repo="web",
        session_id=first_session["session_id"], scope="session",
    )
    second = s.remember(
        "Scopeprobe private second-session state.", workspace="acme", repo="web",
        session_id=second_session["session_id"], scope="session",
    )

    repo_recall = s.recall("scopeprobe", workspace="acme", repo="web", k=10)
    repo_ids = {memory["id"] for memory in repo_recall["memories"]}
    assert {workspace["id"], repo["id"]} <= repo_ids
    assert first["id"] not in repo_ids and second["id"] not in repo_ids

    session_recall = s.recall(
        "scopeprobe", workspace="acme", repo="web",
        session_id=first_session["session_id"], k=10,
    )
    session_ids = {memory["id"] for memory in session_recall["memories"]}
    assert {workspace["id"], repo["id"], first["id"]} <= session_ids
    assert second["id"] not in session_ids


def test_write_scope_defaults_and_parent_validation():
    s = _svc()
    workspace = s.remember("Workspace default.", workspace="acme")
    repo = s.remember("Repo default.", workspace="acme", repo="web")
    assert workspace["scope"] == "workspace"
    assert repo["scope"] == "repo"

    session = s.start_session("acme", repo="web", force_new=True)
    session_grouped_repo = s.remember(
        "Session-grouped durable repo fact.", workspace="acme", repo="web",
        session_id=session["session_id"],
    )
    session_private = s.remember(
        "Session-private working state.", workspace="acme", repo="web",
        session_id=session["session_id"], scope="session",
    )
    assert session_grouped_repo["scope"] == "repo"
    assert session_private["scope"] == "session"

    workspace_session = s.start_session("acme", force_new=True)
    workspace_session_default = s.remember(
        "Workspace-session grouped fact.", workspace="acme",
        session_id=workspace_session["session_id"],
    )
    assert workspace_session_default["scope"] == "workspace"

    with pytest.raises(ValidationError, match="repo scope requires"):
        s.remember("broken", workspace="acme", scope="repo")
    with pytest.raises(ValidationError, match="session scope requires"):
        s.remember("broken", workspace="acme", repo="web", scope="session")
    with pytest.raises(ValidationError, match="workspace scope requires repo"):
        s.remember("broken", workspace="acme", repo="web", scope="workspace")


def test_recall_unknown_workspace_is_empty_not_error():
    s = _svc()
    r = s.recall("anything", workspace="does-not-exist")
    assert r["count"] == 0
    assert "note" in r


def test_session_lifecycle():
    s = _svc()
    started = s.start_session("acme", repo="web", agent="claude-code", goal="ship auth")
    sid = started["session_id"]
    assert sid.startswith("ses_") and started["status"] == "active"

    s.remember("Decided to use PASETO over JWT.", workspace="acme", repo="web",
               session_id=sid, mtype="episodic")
    ended = s.end_session(sid, summary="Auth migrated to PASETO.", outcome="shipped")
    assert ended["status"] == "summarized"

    with pytest.raises(ValidationError):
        s.end_session("ses_does_not_exist")


def test_stats_counts():
    s = _svc()
    s.remember("one", workspace="acme", mtype="semantic")
    s.remember("two", workspace="acme", mtype="procedural")
    st = s.stats(workspace="acme")
    assert st["memories"] == 2
    assert st["by_type"].get("procedural") == 1
    assert st["schema_version"] >= 2


@pytest.mark.parametrize("kwargs", [
    {"content": "", "workspace": "acme"},                       # empty content
    {"content": "x", "workspace": ""},                          # empty workspace
    {"content": "x", "workspace": "bad;name"},                  # illegal name char
    {"content": "x", "workspace": "acme", "mtype": "bogus"},    # bad enum
    {"content": "x", "workspace": "acme", "scope": "bogus"},    # bad enum
    {"content": "x" * 100_001, "workspace": "acme"},            # oversized content
])
def test_remember_validation_rejects_bad_input(kwargs):
    s = _svc()
    with pytest.raises(ValidationError):
        s.remember(**kwargs)


def test_control_characters_are_stripped():
    s = _svc()
    out = s.remember("hello\x00\x07world", workspace="acme")  # NUL + BEL injected
    rec = s.store.get_memory(out["id"])
    assert "\x00" not in rec.content and "\x07" not in rec.content
    assert rec.content == "helloworld"


def test_importance_is_clamped():
    s = _svc()
    out = s.remember("important", workspace="acme", importance=9.0)
    rec = s.store.get_memory(out["id"])
    assert rec.importance == 1.0


def test_update_memory_preserves_metadata_changes_on_a_correction_replacement():
    s = _svc()
    original = s.remember(
        "The original deployment runbook.", workspace="acme", title="Runbook",
        mtype="semantic", importance=0.2,
    )
    replacement = s.correct(
        original["id"], "The revised deployment runbook.", workspace="acme",
    )

    out = s.update_memory(
        replacement["id"], workspace="acme", title="Deployment runbook",
        mtype="procedural", importance=0.9,
    )
    saved = s.store.get_memory(replacement["id"])

    assert out["updated"] == ["title", "type=procedural", "importance"]
    assert (saved.title, saved.mtype.value, saved.importance) == (
        "Deployment runbook", "procedural", 0.9,
    )


def test_provenance_recorded():
    s = _svc()
    out = s.remember("traceable fact", workspace="acme", source="unit-test")
    rec = s.store.get_memory(out["id"])
    assert rec.metadata.get("provenance", {}).get("source") == "unit-test"


# ── conflict resolution on the write path ───────────────────────────────────────

def test_remember_reports_add_op():
    s = _svc()
    out = s.remember("We use pnpm.", workspace="acme", repo="web")
    assert out["op"] == "add"


def test_remember_noop_on_duplicate_reports_op():
    s = _svc()
    text = "We standardized on pnpm as the package manager for all frontend repos."
    s.remember(text, workspace="acme", repo="web")
    out = s.remember(text, workspace="acme", repo="web")
    assert out["op"] == "noop"
    assert "resolution" in out


def test_remember_invalidate_reports_superseded():
    s = _svc()
    first = s.remember("Until 2026-01 the rate limit was 100 requests per minute per API key.",
                       workspace="acme", repo="web")
    second = s.remember(
        "As of 2026-02 the rate limit was raised to 500 requests per minute per API key.",
        workspace="acme", repo="web")
    assert second["op"] == "invalidate"
    assert second["superseded"] == [first["id"]]


def test_remember_resolve_conflicts_false_keeps_both():
    s = _svc()
    text = "Build failed again."
    a = s.remember(text, workspace="acme", repo="web", mtype="episodic", resolve_conflicts=False)
    b = s.remember(text, workspace="acme", repo="web", mtype="episodic", resolve_conflicts=False)
    assert a["op"] == "add" and b["op"] == "add" and a["id"] != b["id"]


# ── session continuity (cross-session handoff) ───────────────────────────────────

def test_start_session_bootstraps_from_prior_session():
    s = _svc()
    first = s.start_session("acme", repo="web", goal="refactor auth")
    assert first["bootstrap"] == {}
    s.end_session(first["session_id"], summary="mid-refactor", outcome="blocked",
                  open_threads=["tests 3-5 failing"])
    second = s.start_session("acme", repo="web", goal="finish refactor")
    assert second["bootstrap"]["summary"] == "mid-refactor"
    assert second["bootstrap"]["open_threads"] == ["tests 3-5 failing"]
    assert second["bootstrap"]["outcome"] == "blocked"


# ── governance: forget / pin / correct ──────────────────────────────────────────

def test_forget_then_recall_excludes_it():
    s = _svc()
    out = s.remember("A fact to forget.", workspace="acme", repo="web")
    s.forget(out["id"], workspace="acme", repo="web", reason="no longer relevant")
    r = s.recall("fact to forget", workspace="acme", repo="web")
    assert all(m["id"] != out["id"] for m in r["memories"])


def test_forget_unknown_id_raises_validation_error():
    s = _svc()
    s.remember("anchor", workspace="acme")   # workspace must exist for _require_scope
    with pytest.raises(ValidationError):
        s.forget("mem_does_not_exist", workspace="acme")


def test_forget_wrong_workspace_raises_validation_error():
    s = _svc()
    out = s.remember("Alpha's private fact.", workspace="alpha")
    s.remember("anchor", workspace="beta")
    with pytest.raises(ValidationError):
        s.forget(out["id"], workspace="beta")          # beta doesn't own alpha's memory
    r = s.recall("private fact", workspace="alpha")
    assert any(m["id"] == out["id"] for m in r["memories"])   # untouched


def test_pin_roundtrip():
    s = _svc()
    out = s.remember("Pin me.", workspace="acme")
    pinned = s.pin(out["id"], workspace="acme")
    assert pinned["pinned"] is True
    unpinned = s.pin(out["id"], workspace="acme", pinned=False)
    assert unpinned["pinned"] is False


def test_pin_wrong_workspace_raises_validation_error():
    s = _svc()
    out = s.remember("Alpha's private fact.", workspace="alpha")
    s.remember("anchor", workspace="beta")
    with pytest.raises(ValidationError):
        s.pin(out["id"], workspace="beta")


def test_correct_supersedes():
    s = _svc()
    out = s.remember("The API key header is X-Auth-Key.", workspace="acme")
    corrected = s.correct(out["id"], "The API key header is X-Api-Key.", workspace="acme",
                          reason="typo")
    assert corrected["superseded"] == [out["id"]]
    r = s.recall("API key header", workspace="acme")
    assert any("X-Api-Key" in m["content"] for m in r["memories"])


def test_correct_wrong_workspace_raises_validation_error():
    s = _svc()
    out = s.remember("Alpha's private fact.", workspace="alpha")
    s.remember("anchor", workspace="beta")
    with pytest.raises(ValidationError):
        s.correct(out["id"], "tampered", workspace="beta")


def test_promote_repo_memory_to_workspace():
    s = _svc()
    source = s.remember(
        "Every service uses structured JSON logs.",
        workspace="acme", repo="api", scope="repo",
    )

    promoted = s.promote(
        source["id"], "workspace", workspace="acme", repo="api",
        reason="confirmed across repositories",
    )

    assert promoted["scope"] == "workspace"
    assert promoted["promoted_from"] == source["id"]
    assert promoted["receipt"]["operation"] == "promote"
    rec = s.store.get_memory(promoted["id"])
    assert rec.repo_id is None
    assert s.store.get_memory(source["id"]).valid_to is not None


def test_promote_rejects_wrong_workspace_and_non_widening_scope():
    s = _svc()
    source = s.remember("Alpha convention.", workspace="alpha", repo="api")
    s.remember("Beta anchor.", workspace="beta")

    with pytest.raises(ValidationError):
        s.promote(source["id"], "workspace", workspace="beta")
    with pytest.raises(ValidationError, match="must widen"):
        s.promote(source["id"], "repo", workspace="alpha", repo="api")


def test_promote_session_to_repo_infers_repo_name():
    s = _svc()
    session = s.start_session("acme", repo="api", force_new=True)
    source = s.remember(
        "This session finding is now a repo convention.",
        workspace="acme", repo="api", session_id=session["session_id"],
        scope="session",
    )

    promoted = s.promote(source["id"], "repo", workspace="acme")

    assert promoted["scope"] == "repo" and promoted["repo"] == "api"


# ── bi-temporal: why / timeline / recall_proactive ───────────────────────────────

def test_why_returns_answer_and_history():
    s = _svc()
    s.remember("Until 2026-01 the rate limit was 100 requests per minute per API key.",
              workspace="acme", repo="web")
    s.remember("As of 2026-02 the rate limit was raised to 500 requests per minute per API key.",
              workspace="acme", repo="web")
    out = s.why("what is the rate limit", workspace="acme", repo="web")
    assert any("500" in m["content"] for m in out["answer"])
    assert any("100" in m["content"] for m in out["supersedes"])


def test_why_unknown_workspace_raises():
    s = _svc()
    with pytest.raises(ValidationError):
        s.why("anything", workspace="does-not-exist")


def test_timeline_orders_chronologically():
    s = _svc()
    s.remember("Until 2026-01 the rate limit was 100 requests per minute per API key.",
              workspace="acme", repo="web")
    s.remember("As of 2026-02 the rate limit was raised to 500 requests per minute per API key.",
              workspace="acme", repo="web")
    out = s.timeline("rate limit", workspace="acme", repo="web")
    assert len(out["history"]) == 2
    assert out["history"][0]["valid_from"] <= out["history"][1]["valid_from"]


@pytest.mark.parametrize(
    ("intent", "result_key", "engine_method"),
    [
        ("why", "explanation", "why"),
        ("timeline", "history", "timeline"),
    ],
)
def test_intent_recall_forwards_temporal_anchors_to_secondary_reads(
        monkeypatch, intent, result_key, engine_method):
    s = _svc()
    s.remember("Temporal intent anchor regression fixture.", workspace="acme", repo="web")
    observed = {}

    def observe(*args, **kwargs):
        observed.update(kwargs)
        return {"answer": [], "supersedes": []} if engine_method == "why" else []

    monkeypatch.setattr(s.engine, engine_method, observe)
    out = s.intent_recall(
        "Temporal intent anchor", intent=intent, workspace="acme", repo="web",
        as_of=10.0, valid_at=10.0, known_at=20.0,
    )

    assert result_key in out
    assert observed["valid_at"] == 10.0
    assert observed["known_at"] == 20.0


def test_service_exposes_world_time_writes_and_point_in_time_recall():
    s = _svc()
    old = s.remember(
        "The API rate limit is 100 requests per minute.",
        workspace="acme",
        repo="web",
        valid_from=1_000.0,
    )
    new = s.remember(
        "The API rate limit is 500 requests per minute.",
        workspace="acme",
        repo="web",
        valid_from=2_000.0,
    )

    before = s.recall(
        "What is the API rate limit?",
        workspace="acme",
        repo="web",
        as_of=1_500.0,
        reinforce=False,
    )
    after = s.recall(
        "What is the API rate limit?",
        workspace="acme",
        repo="web",
        as_of=2_500.0,
        reinforce=False,
    )
    assert [memory["id"] for memory in before["memories"]] == [old["id"]]
    assert [memory["id"] for memory in after["memories"]] == [new["id"]]


@pytest.mark.parametrize(
    ("method", "kwargs"),
    [
        ("remember", {"content": "A fact.", "workspace": "acme", "valid_from": float("nan")}),
        ("remember", {"content": "A fact.", "workspace": "acme", "valid_from": True}),
        ("recall", {"query": "A fact.", "workspace": "acme", "as_of": float("inf")}),
        (
            "grounded_recall",
            {"query": "A fact.", "workspace": "acme", "as_of": "not-a-time"},
        ),
    ],
)
def test_service_rejects_invalid_temporal_anchors(method, kwargs):
    s = _svc()
    with pytest.raises(ValidationError, match="finite timestamp"):
        getattr(s, method)(**kwargs)


def test_service_rejects_backdated_supersession_as_validation_error():
    s = _svc()
    original = s.remember(
        "The deployment window is Friday afternoon.",
        workspace="acme",
        valid_from=2_000.0,
    )

    with pytest.raises(ValidationError, match="cannot predate"):
        s.remember(
            "The deployment window is Thursday afternoon.",
            workspace="acme",
            valid_from=1_000.0,
        )

    assert s.store.get_memory(original["id"]).valid_to is None


def test_recall_proactive_includes_last_session():
    s = _svc()
    s.remember("High importance convention.", workspace="acme", repo="web", importance=0.9)
    started = s.start_session("acme", repo="web")
    s.end_session(started["session_id"], summary="mid-work", open_threads=["thing left undone"])
    out = s.recall_proactive(workspace="acme", repo="web")
    assert out["memories"]
    assert out["last_session"]["open_threads"] == ["thing left undone"]


def test_recall_proactive_filters_untrusted_before_applying_k():
    s = _svc()
    s.remember(
        "A trusted project convention.", workspace="acme", repo="web",
        importance=0.1,
    )
    untrusted = s.remember(
        "A high-priority imported instruction.", workspace="acme", repo="web",
        importance=1.0, source="import", trusted=False,
    )

    out = s.recall_proactive(workspace="acme", repo="web", k=1)

    assert len(out["memories"]) == 1
    assert out["memories"][0]["id"] != untrusted["id"]


# ── linking & events ─────────────────────────────────────────────────────────────

def test_record_event_and_link():
    s = _svc()
    a = s.remember("Memory A.", workspace="acme", repo="web")
    b = s.remember("Memory B.", workspace="acme", repo="web")
    ev = s.record_event("decision", "Chose PASETO over JWT.", workspace="acme", repo="web")
    assert ev["id"].startswith("evt_")
    link = s.link(a["id"], b["id"], workspace="acme", repo="web", relation="related")
    assert link["linked"] is True


def test_link_unknown_id_raises():
    s = _svc()
    a = s.remember("Memory A.", workspace="acme")
    with pytest.raises(ValidationError):
        s.link(a["id"], "mem_nope", workspace="acme")


def test_link_wrong_workspace_raises_validation_error():
    s = _svc()
    a = s.remember("Alpha's fact.", workspace="alpha")
    b = s.remember("Beta's fact.", workspace="beta")
    with pytest.raises(ValidationError):
        s.link(a["id"], b["id"], workspace="alpha")    # b isn't alpha's to link


# ── code-symbol graph ─────────────────────────────────────────────────────────────

def test_index_repo_and_search_code(tmp_path):
    (tmp_path / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n"
    )
    s = _svc()
    report = s.index_repo(workspace="acme", repo="sample", root_path=str(tmp_path))
    assert report["files_indexed"] == 1
    out = s.search_code("add", workspace="acme", repo="sample")
    assert any(sym["name"] == "add" for sym in out["symbols"])


def test_search_code_requires_repo():
    s = _svc()
    s.remember("x", workspace="acme")
    with pytest.raises(ValidationError):
        s.search_code("add", workspace="acme", repo="")


def test_service_code_search_honors_bitemporal_anchors():
    """The public service must not append present-day code to historic recall."""
    from engraphis.core.interfaces import MemoryRecord, Scope

    s = _svc()
    workspace_id = s.store.get_or_create_workspace("acme")
    repo_id = s.store.get_or_create_repo(workspace_id, "api")
    symbol_id = s.store.upsert_symbol(
        repo_id=repo_id, kind="function", name="legacy_route", fqname="legacy_route",
        file="legacy.py", span="1-1",
    )
    memory_id = s.store.add_memory(MemoryRecord(
        id="", content="legacy_route handled historic requests", title="legacy route",
        workspace_id=workspace_id, repo_id=repo_id, scope=Scope.REPO,
        valid_from=10.0, ingested_at=10.0,
    ))
    s.store.link_memory_symbol(repo_id=repo_id, symbol_id=symbol_id, memory_id=memory_id)
    for table in ("symbols", "code_memory_links"):
        s.store.conn.execute(
            f"UPDATE {table} SET valid_from=10, ingested_at=10 WHERE repo_id=?", (repo_id,)
        )
    s.store.conn.commit()
    s.store.close_validity(memory_id, at=20.0)
    s.store.clear_symbols_for_file(repo_id, "legacy.py")
    closed_at = s.store.conn.execute(
        "SELECT valid_to FROM symbols WHERE id=?", (symbol_id,)
    ).fetchone()["valid_to"]

    current = s.search_code("legacy_route", workspace="acme", repo="api")
    historic = s.search_code(
        "legacy_route", workspace="acme", repo="api", valid_at=15.0,
        known_at=float(closed_at) + 1.0,
    )

    assert current["symbols"] == []
    assert [symbol["id"] for symbol in historic["symbols"]] == [symbol_id]
    with pytest.raises(ValidationError, match="as_of and valid_at"):
        s.search_code(
            "legacy_route", workspace="acme", repo="api", as_of=14.0, valid_at=15.0
        )


def test_service_code_export_honors_bitemporal_anchors():
    """Every export companion must be rendered from one anchored graph payload."""
    from engraphis.core.interfaces import MemoryRecord, Scope

    s = _svc()
    workspace_id = s.store.get_or_create_workspace("acme")
    repo_id = s.store.get_or_create_repo(workspace_id, "api")
    symbol_id = s.store.upsert_symbol(
        repo_id=repo_id, kind="function", name="legacy_route", fqname="legacy_route",
        file="legacy.py", span="1-1",
    )
    memory_id = s.store.add_memory(MemoryRecord(
        id="", content="legacy_route handled historic requests", title="legacy route",
        workspace_id=workspace_id, repo_id=repo_id, scope=Scope.REPO,
        valid_from=10.0, ingested_at=10.0,
    ))
    s.store.link_memory_symbol(
        repo_id=repo_id, symbol_id=symbol_id, memory_id=memory_id
    )
    for table in ("symbols", "code_memory_links"):
        s.store.conn.execute(
            f"UPDATE {table} SET valid_from=10, ingested_at=10 WHERE repo_id=?",
            (repo_id,),
        )
    s.store.conn.commit()
    s.store.close_validity(memory_id, at=20.0)
    s.store.clear_symbols_for_file(repo_id, "legacy.py")
    learned_close = s.store.conn.execute(
        "SELECT valid_to FROM symbols WHERE id=?", (symbol_id,)
    ).fetchone()["valid_to"]

    current = s.export_code_graph(workspace="acme", repo="api")
    before_ingestion = s.export_code_graph(
        workspace="acme", repo="api", valid_at=15.0, known_at=9.0,
    )
    historical = s.export_code_graph(
        workspace="acme", repo="api", as_of=15.0, valid_at=15.0,
        known_at=float(learned_close) + 1.0,
    )

    assert current["graph"]["nodes"] == []
    assert before_ingestion["graph"]["nodes"] == []
    assert {row["id"] for row in historical["graph"]["nodes"]} == {symbol_id}
    assert {row["memory_id"] for row in historical["graph"]["memory_links"]} == {
        memory_id
    }
    assert "- Symbols: 1" in historical["report_markdown"]
    assert "legacy_route" in historical["graph_html"]
    assert historical["valid_at"] == 15.0
    assert historical["known_at"] == float(learned_close) + 1.0
    assert historical["historical"] is True
    with pytest.raises(ValidationError, match="as_of and valid_at"):
        s.export_code_graph(
            workspace="acme", repo="api", as_of=14.0, valid_at=15.0
        )


# ── folder / file import (dashboard "Import files & folders" section, SECURITY.md §5) ─

def test_import_folder_success(tmp_path, monkeypatch):
    (tmp_path / "notes.md").write_text("# DB choice\nWe use Postgres 16.\n")
    (tmp_path / "empty.md").write_text("   \n")
    (tmp_path / "skip.txt").write_text("wrong pattern, not imported")
    monkeypatch.setenv("ENGRAPHIS_IMPORT_ROOTS", str(tmp_path))
    s = _svc()
    report = s.import_folder(workspace="acme", path=str(tmp_path))
    assert report["scanned"] == 2          # only *.md matched skip.txt is excluded
    assert report["imported"] == 1
    assert report["skipped"] == 1          # empty.md
    r = s.recall("Postgres", workspace="acme", include_untrusted=True)
    assert any("Postgres" in m["content"] for m in r["memories"])


def test_import_folder_marks_untrusted(tmp_path, monkeypatch):
    (tmp_path / "a.md").write_text("An imported fact about narwhals.")
    monkeypatch.setenv("ENGRAPHIS_IMPORT_ROOTS", str(tmp_path))
    s = _svc()
    s.import_folder(workspace="acme", path=str(tmp_path))
    r = s.recall("narwhals", workspace="acme", include_untrusted=True)
    assert r["memories"], "expected the imported memory to be recallable"
    prov = r["memories"][0]["provenance"]
    assert prov["source"] == "import" and prov["trusted"] is False
    assert prov["kind"] == "file_import"


def test_import_folder_respects_file_pattern(tmp_path, monkeypatch):
    (tmp_path / "a.md").write_text("markdown note")
    (tmp_path / "b.txt").write_text("text note")
    monkeypatch.setenv("ENGRAPHIS_IMPORT_ROOTS", str(tmp_path))
    s = _svc()
    report = s.import_folder(workspace="acme", path=str(tmp_path), file_pattern="*.txt")
    assert report["scanned"] == 1 and report["imported"] == 1
    r = s.recall("text note", workspace="acme", include_untrusted=True)
    assert any("text note" in m["content"] for m in r["memories"])


def test_import_folder_missing_path_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("ENGRAPHIS_IMPORT_ROOTS", str(tmp_path))
    s = _svc()
    with pytest.raises(ValidationError):
        s.import_folder(workspace="acme", path=str(tmp_path / "does-not-exist"))


def test_import_folder_path_traversal_blocked(tmp_path, monkeypatch):
    """A path outside the allowed roots (home dir / ENGRAPHIS_IMPORT_ROOTS) must be
    refused before anything under it is read — SECURITY.md §5's threat model treats the
    path as attacker-controlled (any team member who can reach the dashboard, or a
    prompt-injected agent calling through it)."""
    import pathlib
    decoy_home = tmp_path / "decoy-home"
    decoy_home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("do not import me")
    monkeypatch.delenv("ENGRAPHIS_IMPORT_ROOTS", raising=False)
    monkeypatch.setattr(pathlib.Path, "home", lambda: decoy_home)
    s = _svc()
    with pytest.raises(ValidationError):
        s.import_folder(workspace="acme", path=str(outside))


def test_import_folder_allows_home_directory(tmp_path, monkeypatch):
    """A path *under* the (possibly faked) home directory is allowed without needing
    ENGRAPHIS_IMPORT_ROOTS — the default, no-config case."""
    import pathlib
    home = tmp_path / "home"
    sub = home / "notes"
    sub.mkdir(parents=True)
    (sub / "a.md").write_text("fact under home")
    monkeypatch.delenv("ENGRAPHIS_IMPORT_ROOTS", raising=False)
    monkeypatch.setattr(pathlib.Path, "home", lambda: home)
    s = _svc()
    report = s.import_folder(workspace="acme", path=str(sub))
    assert report["imported"] == 1


def test_import_folder_symlink_escape_blocked(tmp_path, monkeypatch):
    """A symlink *inside* an allowed root that points *outside* it must not let
    ``import_folder`` read the target — ``rglob`` follows symlinked directories, so
    ``_resolve_import_root``'s containment check on the root alone isn't enough; each
    candidate file is re-resolved and re-contained in ``_iter_import_files``."""
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside-secret"
    outside.mkdir()
    (outside / "secret.md").write_text("classified narwhal launch codes")
    try:
        (allowed / "escape").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported in this environment")
    monkeypatch.setenv("ENGRAPHIS_IMPORT_ROOTS", str(allowed))
    s = _svc()
    report = s.import_folder(workspace="acme", path=str(allowed))
    assert report["imported"] == 0 and report["scanned"] == 0
    r = s.recall("narwhal launch codes", workspace="acme")
    assert not any("launch codes" in m["content"] for m in r["memories"])


def test_import_files_success():
    s = _svc()
    report = s.import_files(workspace="acme", files=[
        {"name": "one.md", "content": "# Title\nA fact about pangolins."},
        {"name": "two.md", "content": ""},
    ])
    assert report["imported"] == 1
    assert report["skipped"] == 1
    r = s.recall("pangolins", workspace="acme", include_untrusted=True)
    assert any("pangolins" in m["content"] for m in r["memories"])


def test_import_files_marks_untrusted_with_upload_kind():
    s = _svc()
    s.import_files(workspace="acme", files=[
        {"name": "x.md", "content": "A fact about uploaded quokkas."}])
    r = s.recall("quokkas", workspace="acme", include_untrusted=True)
    prov = r["memories"][0]["provenance"]
    assert prov["source"] == "import" and prov["trusted"] is False
    assert prov["kind"] == "file_upload"


def test_import_files_caps_count():
    s = _svc()
    too_many = [{"name": f"f{i}.md", "content": "x"} for i in range(600)]
    with pytest.raises(ValidationError):
        s.import_files(workspace="acme", files=too_many)


def test_import_files_rejects_non_list():
    s = _svc()
    with pytest.raises(ValidationError):
        s.import_files(workspace="acme", files={"name": "a.md", "content": "x"})
