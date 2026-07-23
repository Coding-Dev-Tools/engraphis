import os
import sqlite3

import pytest

from engraphis.backends.vector_numpy import NumpyVectorIndex
from engraphis.core.engine import MemoryEngine
from engraphis.core.interfaces import MemoryRecord, MemoryType, Scope, SearchFilter


def test_engine_remember_and_recall():
    eng = MemoryEngine.create(":memory:")          # offline defaults
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    eng.remember("We deploy with GitHub Actions to AWS ECS.", workspace_id=wid, repo_id=rid,
                 title="deployment", importance=0.8)
    eng.remember("Lunch is usually around noon.", workspace_id=wid, repo_id=rid)
    res = eng.recall("how do we deploy?", workspace_id=wid, k=2)
    assert res.count >= 1
    assert "actions" in res.context.lower() or "aws" in res.context.lower()


def test_index_upsert_failure_preserves_memory_and_audits(caplog):
    class BrokenIndex:
        def search(self, _vec, _k, *, filter=None):
            return []

        def upsert(self, _ids, _vecs, meta=None):
            raise RuntimeError("simulated index outage")

    eng = MemoryEngine.create(":memory:", vector_backend="numpy", auto_evolve=False)
    eng.index = BrokenIndex()
    eng.recall_engine.index = eng.index
    wid = eng.store.get_or_create_workspace("w")
    with caplog.at_level("WARNING"):
        out = eng.remember_with_resolution("Durable fact.", workspace_id=wid)
    assert eng.store.get_memory(out["id"]).content == "Durable fact."
    row = eng.store.conn.execute(
        "SELECT action, target, detail FROM audit WHERE action='index_upsert_failed'"
    ).fetchone()
    assert dict(row) == {
        "action": "index_upsert_failed",
        "target": out["id"],
        "detail": "failure_type=RuntimeError",
    }
    assert "simulated index outage" not in caplog.text
    assert "RuntimeError" in caplog.text


def test_engine_infers_scope_and_rejects_impossible_parents():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")

    workspace = eng.remember("Workspace fact.", workspace_id=wid)
    repo = eng.remember("Repo fact.", workspace_id=wid, repo_id=rid)
    assert eng.store.get_memory(workspace).scope == Scope.WORKSPACE
    assert eng.store.get_memory(repo).scope == Scope.REPO

    session_id = eng.start_session(wid, rid)
    session_grouped = eng.remember(
        "Session-grouped repo fact.", workspace_id=wid, session_id=session_id
    )
    grouped = eng.store.get_memory(session_grouped)
    assert grouped.scope == Scope.REPO and grouped.repo_id == rid

    workspace_session = eng.start_session(wid)
    workspace_grouped = eng.remember(
        "Workspace-session grouped fact.", workspace_id=wid,
        session_id=workspace_session,
    )
    assert eng.store.get_memory(workspace_grouped).scope == Scope.WORKSPACE

    with pytest.raises(ValueError, match="repo scope requires"):
        eng.remember("broken", workspace_id=wid, scope=Scope.REPO)
    with pytest.raises(ValueError, match="workspace scope requires"):
        eng.remember("broken", workspace_id=wid, repo_id=rid, scope=Scope.WORKSPACE)


def test_engine_falls_back_to_numpy_index_offline(monkeypatch):
    """The factory's fallback CONTRACT, independent of what this environment happens to
    have installed (sqlite-vec is now a [test] dependency, so simulate its absence):
    sqlite-vec unavailable → NumPy reference index, never an error."""
    import engraphis.backends.vector_sqlitevec as vs

    class _Unavailable:
        def __init__(self, *a, **k):
            raise ImportError("sqlite_vec not installed (simulated)")

    monkeypatch.setattr(vs, "SqliteVecVectorIndex", _Unavailable)
    eng = MemoryEngine.create(":memory:")
    assert isinstance(eng.index, NumpyVectorIndex)


def test_engine_prefers_sqlitevec_index_when_available():
    pytest.importorskip("sqlite_vec", reason="sqlite-vec extra not installed")
    from engraphis.backends.vector_sqlitevec import SqliteVecVectorIndex
    eng = MemoryEngine.create(":memory:")
    assert isinstance(eng.index, SqliteVecVectorIndex)


def test_engine_respects_memory_type_and_scope():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    mid = eng.remember("How to add a migration: edit models, run alembic revision.",
                       workspace_id=wid, repo_id=rid, mtype=MemoryType.PROCEDURAL, scope=Scope.REPO)
    rec = eng.store.get_memory(mid)
    assert rec.mtype == MemoryType.PROCEDURAL and rec.scope == Scope.REPO


def test_engine_session_recall_infers_parent_repo():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    sid = eng.start_session(wid, rid)
    workspace = eng.remember(
        "Scopeprobe workspace ancestor.", workspace_id=wid, scope=Scope.WORKSPACE
    )
    repo = eng.remember(
        "Scopeprobe repo ancestor.", workspace_id=wid, repo_id=rid, scope=Scope.REPO
    )
    session = eng.remember(
        "Scopeprobe exact session.", workspace_id=wid, session_id=sid,
        scope=Scope.SESSION,
    )

    recalled = eng.recall("scopeprobe", workspace_id=wid, session_id=sid, k=10)
    ids = {chunk["id"] for chunk in recalled.chunks}

    assert {workspace, repo, session} <= ids


# ── conflict resolution on the write path ───────────────────────────────────────

def test_remember_adds_unrelated_facts():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    out1 = eng.remember_with_resolution("We standardized on pnpm for frontend repos.",
                                        workspace_id=wid, repo_id=rid)
    out2 = eng.remember_with_resolution("The design team prefers Figma for mockups.",
                                        workspace_id=wid, repo_id=rid)
    assert out1["op"] == "add" and out2["op"] == "add"
    assert out1["id"] != out2["id"]


def test_remember_noops_on_near_duplicate():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    text = "We standardized on pnpm as the package manager for all frontend repositories."
    first = eng.remember_with_resolution(text, workspace_id=wid, repo_id=rid)
    before = eng.store.get_memory(first["id"])
    second = eng.remember_with_resolution(text, workspace_id=wid, repo_id=rid)
    assert second["op"] == "noop"
    assert second["id"] == first["id"]
    after = eng.store.get_memory(first["id"])
    assert after.stability > before.stability        # reinforced, not duplicated
    assert len(eng.store.list_memories(SearchFilter(workspace_id=wid, repo_id=rid))) == 1


def test_remember_invalidates_superseded_fact():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    old = eng.remember_with_resolution(
        "Until 2026-01 the rate limit was 100 requests per minute per API key.",
        workspace_id=wid, repo_id=rid)
    new = eng.remember_with_resolution(
        "As of 2026-02 the rate limit was raised to 500 requests per minute per API key.",
        workspace_id=wid, repo_id=rid)
    assert new["op"] == "invalidate"
    assert new["superseded"] == [old["id"]]
    live_ids = [m.id for m in eng.store.list_memories(SearchFilter(workspace_id=wid, repo_id=rid))]
    assert old["id"] not in live_ids and new["id"] in live_ids


def test_remember_keeps_related_but_complementary_facts():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    cause = eng.remember_with_resolution(
        "The bug in checkout was caused by a race condition in the inventory service.",
        workspace_id=wid, repo_id=rid, mtype=MemoryType.EPISODIC)
    fix = eng.remember_with_resolution(
        "We fixed the checkout race condition by adding a Redis lock around the stock decrement.",
        workspace_id=wid, repo_id=rid, mtype=MemoryType.EPISODIC)
    assert cause["op"] == "add" and fix["op"] == "add"


def test_remember_resolve_conflicts_false_keeps_duplicates():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    text = "Build failed again on the flaky network test."
    out1 = eng.remember_with_resolution(text, workspace_id=wid, repo_id=rid,
                                        mtype=MemoryType.EPISODIC, resolve_conflicts=False)
    out2 = eng.remember_with_resolution(text, workspace_id=wid, repo_id=rid,
                                        mtype=MemoryType.EPISODIC, resolve_conflicts=False)
    assert out1["op"] == "add" and out2["op"] == "add"
    assert out1["id"] != out2["id"]


# ── memory evolution (A-MEM-style auto-linking on write) ────────────────────────

def test_remember_auto_links_related_memories():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    cause = eng.remember_with_resolution(
        "The bug in checkout was caused by a race condition in the inventory service.",
        workspace_id=wid, repo_id=rid, mtype=MemoryType.EPISODIC)
    fix = eng.remember_with_resolution(
        "We fixed the checkout race condition by adding a Redis lock around the stock decrement.",
        workspace_id=wid, repo_id=rid, mtype=MemoryType.EPISODIC)
    assert fix["op"] == "add"
    assert cause["id"] in fix.get("linked", [])
    assert eng.store.has_link(fix["id"], cause["id"])


def test_evolution_reinforces_linked_neighbor():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    cause = eng.remember_with_resolution(
        "The bug in checkout was caused by a race condition in the inventory service.",
        workspace_id=wid, repo_id=rid, mtype=MemoryType.EPISODIC)
    before = eng.store.get_memory(cause["id"]).stability
    eng.remember_with_resolution(
        "We fixed the checkout race condition by adding a Redis lock around the stock decrement.",
        workspace_id=wid, repo_id=rid, mtype=MemoryType.EPISODIC)
    after = eng.store.get_memory(cause["id"]).stability
    assert after > before                          # old note strengthened by new arrival


def test_evolution_does_not_link_unrelated_memories():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    a = eng.remember_with_resolution("We deploy with GitHub Actions to AWS ECS.",
                                     workspace_id=wid, repo_id=rid)
    b = eng.remember_with_resolution("Lunch is usually around noon.",
                                     workspace_id=wid, repo_id=rid)
    assert a["id"] not in b.get("linked", [])
    assert not eng.store.has_link(b["id"], a["id"])


def test_evolution_links_are_idempotent():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    a = eng.remember(
        "The bug in checkout was caused by a race condition in the inventory service.",
        workspace_id=wid, repo_id=rid, mtype=MemoryType.EPISODIC)
    b = eng.remember(
        "We fixed the checkout race condition by adding a Redis lock around the stock decrement.",
        workspace_id=wid, repo_id=rid, mtype=MemoryType.EPISODIC)
    eng.store.add_link(a, b, "related")            # explicit re-link of the auto link
    rows = [link for link in eng.store.get_links(a)
            if {link["a"], link["b"]} == {a, b} and link["relation"] == "related"]
    assert len(rows) == 1                          # deduped in either direction


def test_evolution_can_be_disabled():
    eng = MemoryEngine.create(":memory:")
    eng.auto_evolve = False
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    eng.remember_with_resolution(
        "The bug in checkout was caused by a race condition in the inventory service.",
        workspace_id=wid, repo_id=rid, mtype=MemoryType.EPISODIC)
    fix = eng.remember_with_resolution(
        "We fixed the checkout race condition by adding a Redis lock around the stock decrement.",
        workspace_id=wid, repo_id=rid, mtype=MemoryType.EPISODIC)
    assert "linked" not in fix


def test_invalidate_records_supersedes_metadata():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    old = eng.remember_with_resolution(
        "Until 2026-01 the rate limit was 100 requests per minute per API key.",
        workspace_id=wid, repo_id=rid)
    new = eng.remember_with_resolution(
        "As of 2026-02 the rate limit was raised to 500 requests per minute per API key.",
        workspace_id=wid, repo_id=rid)
    assert new["op"] == "invalidate"
    rec = eng.store.get_memory(new["id"])
    assert rec.metadata.get("supersedes") == [old["id"]]   # chain queryable, not audit-only


# ── governance: forget / pin / correct ──────────────────────────────────────────

def test_forget_invalidates_without_deleting():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    mid = eng.remember("A fact to forget.", workspace_id=wid, repo_id=rid)
    eng.forget(mid, reason="no longer true")
    assert mid not in [m.id for m in eng.store.list_memories(SearchFilter(workspace_id=wid))]
    assert eng.store.get_memory(mid) is not None      # not hard-deleted


def test_forget_unknown_id_raises():
    eng = MemoryEngine.create(":memory:")
    with pytest.raises(KeyError):
        eng.forget("mem_does_not_exist")


def test_pin_sets_flag_and_audits():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    mid = eng.remember("Pin me.", workspace_id=wid, repo_id=rid)
    eng.pin(mid)
    assert eng.store.get_memory(mid).pinned is True
    eng.pin(mid, pinned=False)
    assert eng.store.get_memory(mid).pinned is False


def test_audit_rows_are_durable_without_a_later_write(tmp_path):
    db = tmp_path / "audit.db"
    eng = MemoryEngine.create(str(db))
    eng.store.audit("test", "standalone", "target")

    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM audit WHERE action='standalone'").fetchone()[0] == 1


def test_correct_supersedes_without_deleting():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    mid = eng.remember("The API key header is X-Auth-Key.", workspace_id=wid, repo_id=rid)
    out = eng.correct(mid, "The API key header is X-Api-Key.", reason="typo in the original")
    assert out["superseded"] == [mid]
    new_rec = eng.store.get_memory(out["id"])
    assert "X-Api-Key" in new_rec.content
    assert new_rec.metadata.get("corrects") == mid
    live_ids = [m.id for m in eng.store.list_memories(SearchFilter(workspace_id=wid))]
    assert mid not in live_ids and out["id"] in live_ids


def test_promote_widens_scope_and_preserves_source_history_and_safety():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    sid = eng.start_session(wid, rid)
    source = eng.remember(
        "All release tags must be signed.", workspace_id=wid, repo_id=rid,
        session_id=sid, scope=Scope.SESSION,
    )
    eng.store.set_pinned(source, True)
    eng.store.conn.execute(
        "UPDATE memories SET sensitivity='secret', stability=9.0, access_count=4 WHERE id=?",
        (source,),
    )
    eng.store.conn.commit()

    out = eng.promote(source, Scope.REPO, reason="confirmed repo convention")

    old = eng.store.get_memory(source)
    promoted = eng.store.get_memory(out["id"])
    assert old is not None and old.valid_to is not None
    assert promoted.scope == Scope.REPO and promoted.repo_id == rid
    assert promoted.pinned is True and promoted.sensitivity == "secret"
    assert promoted.stability >= 9.0 and promoted.access_count >= 4
    assert promoted.metadata["promoted_from"] == [source]
    assert eng.store.has_link(promoted.id, source, relation="promotes")


def test_promote_deduplicates_into_existing_wider_memory():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    text = "The organization requires signed release tags."
    wider = eng.remember(
        text, workspace_id=wid, scope=Scope.WORKSPACE,
        metadata={"provenance": {"source": "agent", "trusted": True}},
    )
    source = eng.remember(
        text, workspace_id=wid, repo_id=rid, scope=Scope.REPO,
        metadata={"provenance": {"source": "web", "trusted": False}},
    )

    out = eng.promote(source, Scope.WORKSPACE)

    assert out["id"] == wider and out["op"] == "noop"
    assert eng.store.get_memory(source).valid_to is not None
    assert eng.store.has_link(wider, source, relation="promotes")
    promoted = eng.store.get_memory(wider)
    assert promoted.metadata["promoted_from"] == [source]
    assert promoted.provenance["trusted"] is False


def test_promote_rejects_same_or_narrower_scope():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    source = eng.remember("Repo fact.", workspace_id=wid, repo_id=rid, scope=Scope.REPO)

    with pytest.raises(ValueError, match="must widen"):
        eng.promote(source, Scope.REPO)
    with pytest.raises(ValueError, match="must widen"):
        eng.promote(source, Scope.SESSION)
    with pytest.raises(ValueError, match="user scope is not supported"):
        eng.promote(source, Scope.USER)


# ── why / timeline / recall_proactive ────────────────────────────────────────────

def test_why_surfaces_live_answer_and_superseded_history():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    eng.remember("Until 2026-01 the rate limit was 100 requests per minute per API key.",
                workspace_id=wid, repo_id=rid)
    eng.remember("As of 2026-02 the rate limit was raised to 500 requests per minute per API key.",
                workspace_id=wid, repo_id=rid)
    out = eng.why("what is the rate limit", workspace_id=wid, repo_id=rid)
    assert any("500" in r.content for r in out["answer"])
    assert any("100" in r.content for r in out["supersedes"])


def test_timeline_orders_history_chronologically():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    eng.remember("Until 2026-01 the rate limit was 100 requests per minute per API key.",
                workspace_id=wid, repo_id=rid, valid_from=1_000.0)
    eng.remember("As of 2026-02 the rate limit was raised to 500 requests per minute per API key.",
                workspace_id=wid, repo_id=rid, valid_from=2_000.0)
    hist = eng.timeline("rate limit", workspace_id=wid, repo_id=rid)
    assert len(hist) == 2
    assert hist[0].valid_from < hist[1].valid_from


def test_recall_proactive_includes_last_session_handoff():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    eng.remember("High importance convention.", workspace_id=wid, repo_id=rid, importance=0.9)
    sid = eng.start_session(wid, rid, goal="refactor auth")
    eng.end_session(sid, summary="mid-refactor", open_threads=["tests 3-5 failing"])
    out = eng.recall_proactive(workspace_id=wid, repo_id=rid)
    assert out["memories"]
    assert out["last_session"]["open_threads"] == ["tests 3-5 failing"]
    assert out["last_session"]["summary"] == "mid-refactor"


# ── linking & events ─────────────────────────────────────────────────────────────

def test_link_connects_two_memories():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    a = eng.remember("Memory A.", workspace_id=wid, repo_id=rid)
    b = eng.remember("Memory B.", workspace_id=wid, repo_id=rid)
    eng.link(a, b, relation="related")
    links = eng.store.get_links(a)
    assert any(link["a"] == a and link["b"] == b for link in links)


def test_link_unknown_id_raises():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    a = eng.remember("Memory A.", workspace_id=wid, repo_id=rid)
    with pytest.raises(KeyError):
        eng.link(a, "mem_nope")


def test_record_event_persists():
    eng = MemoryEngine.create(":memory:")
    eid = eng.record_event("decision", "Chose PASETO over JWT.", workspace_id="ws_x")
    assert eid.startswith("evt_")


# ── code-symbol graph ─────────────────────────────────────────────────────────────

def _write_sample_repo(tmp_path):
    (tmp_path / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\n"
        "class Calculator:\n"
        "    def add(self, x):\n        return add(x, 1)\n"
    )
    return tmp_path


def test_index_repo_and_search_code(tmp_path):
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "sample")
    _write_sample_repo(tmp_path)

    report = eng.index_repo(rid, str(tmp_path))
    assert report["files_indexed"] >= 1
    assert report["symbols"] >= 1

    out = eng.search_code("add", repo_id=rid)
    names = {s["name"] for s in out["symbols"]}
    assert "add" in names


def test_index_repo_allows_selected_root_only_within_approved_local_roots(tmp_path, monkeypatch):
    from engraphis.core import engine as engine_module

    allowed = tmp_path / "allowed"
    selected_repo = allowed / "chosen-project"
    selected_repo.mkdir(parents=True)
    (selected_repo / "module.py").write_text("def selected(): pass\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "module.py").write_text("def rejected(): pass\n", encoding="utf-8")
    monkeypatch.setattr(engine_module, "_approved_local_index_roots", lambda: (str(allowed),))

    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "sample")

    report = eng.index_repo(rid, str(selected_repo), prefer="regex")
    assert report["files_indexed"] == 1
    with pytest.raises(ValueError, match="outside approved local roots"):
        eng.index_repo(rid, str(outside), prefer="regex")


def test_index_repo_rejects_normalized_escape_from_approved_local_root(tmp_path, monkeypatch):
    from engraphis.core import engine as engine_module

    allowed = tmp_path / "allowed"
    selected_repo = allowed / "selected-project"
    selected_repo.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setattr(engine_module, "_approved_local_index_roots", lambda: (str(allowed),))

    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "sample")

    escaped = selected_repo / ".." / ".." / "outside"
    with pytest.raises(ValueError, match="outside approved local roots"):
        eng.index_repo(rid, str(escaped), prefer="regex")


def test_index_repo_accepts_the_approved_root_itself(tmp_path, monkeypatch):
    from engraphis.core import engine as engine_module

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    (allowed / "module.py").write_text("def selected(): pass\n", encoding="utf-8")
    monkeypatch.setattr(engine_module, "_approved_local_index_roots", lambda: (str(allowed),))

    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "sample")

    report = eng.index_repo(rid, str(allowed), prefer="regex")
    assert report["files_indexed"] == 1


def test_index_repo_rejects_root_symlink_that_resolves_outside_approved_root(tmp_path, monkeypatch):
    from engraphis.core import engine as engine_module

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = allowed / "outside-link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported in this environment")
    monkeypatch.setattr(engine_module, "_approved_local_index_roots", lambda: (str(allowed),))

    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "sample")

    with pytest.raises(ValueError, match="outside approved local roots"):
        eng.index_repo(rid, str(link), prefer="regex")


def test_index_repo_operator_roots_replace_local_defaults(tmp_path, monkeypatch):
    from engraphis.core import engine as engine_module

    first = tmp_path / "first"
    second = tmp_path / "second"
    allowed_repo = first / "selected"
    allowed_repo.mkdir(parents=True)
    (allowed_repo / "module.py").write_text("def selected(): pass\n", encoding="utf-8")
    default_only = tmp_path / "outside-configured-roots"
    default_only.mkdir()
    monkeypatch.setenv("ENGRAPHIS_INDEX_ROOTS", os.pathsep.join((str(first), str(second))))

    assert engine_module._approved_local_index_roots() == (
        os.path.realpath(first),
        os.path.realpath(second),
    )

    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "sample")
    assert eng.index_repo(rid, str(allowed_repo), prefer="regex")["files_indexed"] == 1
    with pytest.raises(ValueError, match="outside approved local roots"):
        eng.index_repo(rid, str(default_only), prefer="regex")


def test_index_repo_is_idempotent_per_file(tmp_path):
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "sample")
    _write_sample_repo(tmp_path)

    first = eng.index_repo(rid, str(tmp_path))
    second = eng.index_repo(rid, str(tmp_path))
    assert first["symbols"] == second["symbols"]   # replaced, not accumulated
    assert second["files_indexed"] == 0
    assert second["files_unchanged"] == 1
    assert eng.store.count_symbols(rid) == first["symbols"]


def test_truncated_directory_walk_never_removes_unvisited_index_state(
        tmp_path, monkeypatch):
    from engraphis.backends import codegraph

    (tmp_path / "a.py").write_text("def root_symbol():\n    pass\n", encoding="utf-8")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "b.py").write_text("def nested_symbol():\n    pass\n", encoding="utf-8")
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "sample")
    assert eng.index_repo(rid, str(tmp_path), prefer="regex")["symbols"] == 2

    monkeypatch.setattr(codegraph, "_MAX_WALK_DIRS", 1)
    report = eng.index_repo(rid, str(tmp_path), prefer="regex")

    assert report["scan_complete"] is False
    assert report["files_removed"] == 0
    assert {symbol["name"] for symbol in eng.store.list_symbols(rid)} == {
        "root_symbol", "nested_symbol",
    }


def test_index_repo_skips_unsupported_files(tmp_path):
    (tmp_path / "readme.md").write_text("not code")
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "sample")
    report = eng.index_repo(rid, str(tmp_path))
    assert report["files_indexed"] == 0


def test_index_repo_never_reads_a_symlink_that_escapes_root(tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "-outside-indexed-source.py")
    outside.write_text("def leaked_secret(): pass\n", encoding="utf-8")
    link = tmp_path / "escape.py"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported in this environment")
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "sample")

    report = eng.index_repo(rid, str(tmp_path), prefer="regex")

    assert report["files_indexed"] == 0
    assert eng.search_code("leaked_secret", repo_id=rid)["symbols"] == []


def test_truncated_incremental_scan_does_not_delete_unseen_files(tmp_path):
    (tmp_path / "a.py").write_text("def alpha(): pass\n")
    (tmp_path / "b.py").write_text("def beta(): pass\n")
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "sample")
    first = eng.index_repo(rid, str(tmp_path), prefer="regex")
    assert first["symbols"] == 2

    limited = eng.index_repo(rid, str(tmp_path), prefer="regex", max_files=1)
    assert limited["scan_complete"] is False
    assert limited["files_removed"] == 0
    assert eng.store.count_symbols(rid) == 2


def test_complete_incremental_scan_removes_deleted_files(tmp_path):
    (tmp_path / "a.py").write_text("def alpha(): pass\n")
    doomed = tmp_path / "b.py"
    doomed.write_text("def beta(): pass\n")
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "sample")
    eng.index_repo(rid, str(tmp_path), prefer="regex")

    doomed.unlink()
    report = eng.index_repo(rid, str(tmp_path), prefer="regex")
    assert report["scan_complete"] is True
    assert report["files_removed"] == 1
    assert {row["name"] for row in eng.store.list_symbols(rid)} == {"alpha"}


def test_incremental_scan_preserves_last_good_index_for_unreadable_file(
        tmp_path, monkeypatch):
    source = tmp_path / "a.py"
    source.write_text("def alpha(): pass\n")
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "sample")
    eng.index_repo(rid, str(tmp_path), prefer="regex")

    original_read_bytes = type(source).read_bytes

    def fail_target(path):
        if path.resolve() == source.resolve():
            raise OSError("temporary read failure")
        return original_read_bytes(path)

    monkeypatch.setattr(type(source), "read_bytes", fail_target)
    report = eng.index_repo(rid, str(tmp_path), prefer="regex")

    assert report["scan_complete"] is True
    assert report["files_failed"] == 1
    assert report["files_removed"] == 0
    assert {row["name"] for row in eng.store.list_symbols(rid)} == {"alpha"}


def test_code_path_and_impact_preserve_hidden_repo_paths(tmp_path):
    hidden = tmp_path / ".github"
    hidden.mkdir()
    (hidden / "workflow.py").write_text("def deploy(): pass\n")
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "sample")
    eng.index_repo(rid, str(tmp_path), prefer="regex")

    path = eng.code_path(".github/workflow.py", "deploy", repo_id=rid)
    assert path["found"] is True and path["hops"] == 1
    impact = eng.analyze_impact([".github/workflow.py"], repo_id=rid)
    assert impact["changed_files"] == [".github/workflow.py"]
    assert {row["name"] for row in impact["symbols"]} == {"deploy"}


def test_code_memory_paths_hide_forgotten_memories(tmp_path):
    (tmp_path / "deploy.py").write_text("def deploy(): pass\n")
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "sample")
    eng.index_repo(rid, str(tmp_path), prefer="regex")
    mid = eng.remember(
        "The deploy procedure requires a signed release tag.",
        workspace_id=wid,
        repo_id=rid,
    )
    assert eng.code_path("deploy", mid, repo_id=rid)["found"] is True
    assert eng.analyze_impact(["deploy.py"], repo_id=rid)["memory_mentions"]

    eng.forget(mid)
    assert eng.code_path("deploy", mid, repo_id=rid)["found"] is False
    assert eng.analyze_impact(["deploy.py"], repo_id=rid)["memory_mentions"] == []


def test_code_reads_apply_session_visibility_to_every_memory_surface():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "sample")
    session_id = eng.store.start_session(wid, rid)
    symbol_id = eng.store.upsert_symbol(
        repo_id=rid, kind="function", name="deploy", fqname="deploy",
        file="deploy.py", span="1-1",
    )
    repo_memory = eng.store.add_memory(MemoryRecord(
        id="", content="deploy uses the public release process", title="repo deploy",
        workspace_id=wid, repo_id=rid, scope=Scope.REPO,
    ))
    session_memory = eng.store.add_memory(MemoryRecord(
        id="", content="deploy uses a private session token", title="session deploy secret",
        workspace_id=wid, repo_id=rid, session_id=session_id, scope=Scope.SESSION,
    ))
    for memory_id in (repo_memory, session_memory):
        eng.store.link_memory_symbol(
            repo_id=rid, symbol_id=symbol_id, memory_id=memory_id,
        )

    repo_filter = SearchFilter(
        workspace_id=wid, repo_id=rid, include_ancestors=True,
    )
    search = eng.search_code("deploy", repo_id=rid, flt=repo_filter)
    assert {row["id"] for row in search["symbols"][0]["linked_memories"]} == {
        repo_memory
    }
    assert eng.code_path("deploy", repo_memory, repo_id=rid, flt=repo_filter)["found"]
    assert not eng.code_path(
        "deploy", session_memory, repo_id=rid, flt=repo_filter,
    )["found"]
    impact = eng.analyze_impact(["deploy.py"], repo_id=rid, flt=repo_filter)
    assert {row["id"] for row in impact["memory_mentions"]} == {repo_memory}
    exported = eng.export_code_graph(repo_id=rid, flt=repo_filter)
    assert {row["memory_id"] for row in exported["memory_links"]} == {repo_memory}
    assert session_memory not in eng.code_graph_html(repo_id=rid, flt=repo_filter)

    session_filter = SearchFilter(
        workspace_id=wid, repo_id=rid, session_id=session_id,
        include_ancestors=True,
    )
    session_search = eng.search_code("deploy", repo_id=rid, flt=session_filter)
    assert {row["id"] for row in session_search["symbols"][0]["linked_memories"]} == {
        repo_memory, session_memory
    }
    assert eng.code_path(
        "deploy", session_memory, repo_id=rid, flt=session_filter,
    )["found"]


def test_rebuild_code_memory_links_keysets_past_five_thousand_session_records():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "sample")
    session_id = eng.store.start_session(wid, rid)
    symbol_id = eng.store.upsert_symbol(
        repo_id=rid, kind="function", name="deploy", fqname="deploy",
        file="deploy.py", span="1-1",
    )
    target_id = "mem_00000"
    rows = [
        (
            target_id, wid, rid, session_id, "session", "semantic",
            "oldest", "deploy remains linked", 0.0, 0.0,
        )
    ]
    rows.extend(
        (
            f"mem_{i:05d}", wid, rid, None, "repo", "semantic",
            "", "unrelated filler", float(i), float(i),
        )
        for i in range(1, 5001)
    )
    eng.store.conn.executemany(
        "INSERT INTO memories("
        "id, workspace_id, repo_id, session_id, scope, mtype, title, content, "
        "valid_from, ingested_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    eng.store.link_memory_symbol(
        repo_id=rid, symbol_id=symbol_id, memory_id=target_id,
    )

    eng.rebuild_code_memory_links(repo_id=rid)

    assert {
        row["memory_id"] for row in eng.store.list_code_memory_links(rid)
    } == {target_id}


def test_code_graph_html_escapes_embedded_graph_data():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "sample")
    eng.store.upsert_symbol(
        repo_id=rid,
        kind="function",
        name="run",
        fqname="run",
        file="</script><script>alert(1)</script>.py",
        span="1-1",
    )
    html = eng.code_graph_html(repo_id=rid)
    assert '<svg id="graph"' in html
    assert "Scroll to zoom" in html
    assert "</script><script>alert(1)</script>.py" not in html
    assert "\\u003c/script>" in html
    assert "&lt;/script&gt;&lt;script&gt;alert(1)&lt;/script&gt;.py" in html


# ── correct(): write the replacement before retiring the original ───────────────────

def test_correct_leaves_the_original_live_when_the_replacement_write_fails():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    mid = eng.remember("Old fact.", workspace_id=wid, resolve_conflicts=False)

    def boom(*_args, **_kw):
        raise RuntimeError("simulated write failure")

    eng.remember = boom
    with pytest.raises(RuntimeError):
        eng.correct(mid, "New fact.")

    rec = eng.store.get_memory(mid)
    assert rec.valid_to is None and rec.content == "Old fact."


def test_correct_repairs_a_repo_scoped_row_that_has_no_repo():
    """The sync apply path can persist a scope/repo_id combination ``remember`` rejects.
    Correcting one used to retire it and *then* raise, leaving nothing behind."""
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    mid = eng.remember("Synced fact.", workspace_id=wid, resolve_conflicts=False)
    eng.store.conn.execute(
        "UPDATE memories SET scope='repo', repo_id=NULL WHERE id=?", (mid,))
    eng.store.conn.commit()

    out = eng.correct(mid, "Corrected fact.")

    new = eng.store.get_memory(out["id"])
    assert new.content == "Corrected fact." and new.scope == Scope.WORKSPACE
    assert new.valid_to is None
    assert eng.store.get_memory(mid).valid_to is not None   # retired, not deleted


# ── export_code_graph is bounded (viewer-reachable payload) ─────────────────────────

def test_export_code_graph_is_bounded_and_flags_truncation(monkeypatch):
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "sample")
    for n in range(12):
        eng.store.upsert_symbol(repo_id=rid, kind="function", name=f"fn_{n:03d}",
                                fqname=f"mod{n}.fn_{n:03d}", file=f"mod{n}.py", span="1-1")
        eng.store.upsert_code_file(repo_id=rid, file=f"mod{n}.py", lang="python",
                                   content_hash=f"h{n}", size_bytes=1, mtime_ns=0,
                                   backend="test")

    requested_limits = []
    list_code_files = eng.store.list_code_files

    def tracked_list_code_files(repo_id, **kwargs):
        requested_limits.append(kwargs.get("limit"))
        return list_code_files(repo_id, **kwargs)

    monkeypatch.setattr(eng.store, "list_code_files", tracked_list_code_files)

    capped = eng.export_code_graph(repo_id=rid, limit=5)
    assert capped["limit"] == 5
    assert len(capped["nodes"]) == 5 and len(capped["files"]) == 5
    assert capped["truncated"] is True
    assert requested_limits == [6]  # five payload rows plus one truncation sentinel

    full = eng.export_code_graph(repo_id=rid)
    assert len(full["nodes"]) == 12 and len(full["files"]) == 12
    assert full["truncated"] is False
    # Bogus limits are clamped, never passed through to SQL.
    assert eng.export_code_graph(repo_id=rid, limit=-7)["limit"] == 1


# ── code↔memory linking: the compiled matcher must reproduce the old links exactly ──

_OVERLAPPING_SYMBOLS = [
    # (kind, name, fqname, file) — deliberately overlapping/substring names.
    ("class", "Engine", "engraphis.core.engine.Engine", "engine.py"),
    ("function", "engine", "engraphis.core.engine", "engine.py"),
    ("function", "engine_v2", "engraphis.core.engine_v2", "engine.py"),
    ("function", "run", "run", "run.py"),
    ("function", "run_all", "run.run_all", "run.py"),
    ("function", "ru", "ru", "run.py"),                 # < 3 chars: always skipped
    ("class", "Store", "engraphis.core.store.Store", "store.py"),
    ("function", "store", "store", "store.py"),
    ("function", "add", "Calculator.add", "calc.py"),
    ("class", "Calculator", "Calculator", "calc.py"),
]

_LINK_TEXTS = [
    "engraphis.core.engine wraps engine and engine_v2 for the migration.",
    "See engraphis.core.store.Store; the store module also exports Store.",
    "Calculator.add is the only caller of add() in calc.py.",
    "run_all invokes run, but run_allocation is unrelated.",
    "The engine_v2 rewrite lives beside engraphis.core.engine.Engine.",
    "Nothing here mentions any indexed symbol at all.",
]


def _legacy_links(symbols, content):
    """The pre-optimization per-symbol matcher, reproduced verbatim as the oracle."""
    import re

    from engraphis.core.textutil import tokenize

    hay = str(content or "")
    hay_lower = hay.lower()
    hay_tokens = tokenize(hay)
    out = []
    for symbol in symbols:
        name = str(symbol.get("name") or "").strip()
        fqname = str(symbol.get("fqname") or "").strip()
        if len(name) < 3:
            continue
        confidence = 0.0
        fqname_lower = fqname.lower()
        name_lower = name.lower()
        if fqname and len(fqname) >= 3 and fqname_lower in hay_lower and re.search(
            r"(?<!\w)" + re.escape(fqname.lower()) + r"(?!\w)", hay_lower
        ):
            confidence = 1.0
        elif name_lower in hay_lower and re.search(
            r"(?<!\w)" + re.escape(name_lower) + r"(?!\w)", hay_lower
        ):
            confidence = 0.9
        else:
            name_tokens = tokenize(name)
            if name_tokens and name_tokens <= hay_tokens:
                confidence = 0.75
        if confidence <= 0.0:
            continue
        out.append((symbol["id"], confidence))
        if len(out) >= 200:
            break
    return out


def test_compiled_symbol_matcher_reproduces_the_legacy_links_exactly():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "sample")
    for kind, name, fqname, file in _OVERLAPPING_SYMBOLS:
        eng.store.upsert_symbol(repo_id=rid, kind=kind, name=name, fqname=fqname,
                                file=file, span="1-1")
    symbols = eng.store.list_symbols(rid)

    for text in _LINK_TEXTS:
        mid = eng.remember(text, workspace_id=wid, repo_id=rid, resolve_conflicts=False)
        actual = sorted(
            (row["symbol_id"], row["confidence"])
            for row in eng.store.list_code_memory_links(rid)
            if row["memory_id"] == mid
        )
        assert actual == sorted(_legacy_links(symbols, text)), text


def test_symbol_matcher_still_sees_a_name_nested_inside_a_longer_fqname():
    """A plain non-overlapping ``finditer`` over the alternation would let
    ``engraphis.core.engine`` swallow ``engine`` and silently downgrade its confidence
    from 0.9 to the 0.75 token fallback. Candidate offsets must stay overlapping."""
    from engraphis.core.engine import _CodeSymbolMatcher

    symbols = [
        {"id": "sym_long", "name": "engine", "fqname": "engraphis.core.engine"},
        {"id": "sym_short", "name": "engine", "fqname": "engine"},
    ]
    matcher = _CodeSymbolMatcher(symbols)
    matched, positions = matcher.match("see engraphis.core.engine for details", set())
    assert matched == {"engraphis.core.engine", "engine"}
    assert positions == [0, 1], "candidates must come back in store order for the cap"


def test_code_matcher_cache_is_invalidated_when_symbols_change():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "sample")
    first = eng.remember("The deployer handles rollout.", workspace_id=wid, repo_id=rid,
                         resolve_conflicts=False)
    assert [r for r in eng.store.list_code_memory_links(rid)
            if r["memory_id"] == first] == []

    eng.store.upsert_symbol(repo_id=rid, kind="function", name="deployer",
                            fqname="deployer", file="d.py", span="1-1")
    second = eng.remember("The deployer also signs the release.", workspace_id=wid,
                          repo_id=rid, resolve_conflicts=False)

    assert [r["symbol_id"] for r in eng.store.list_code_memory_links(rid)
            if r["memory_id"] == second], "a new symbol must invalidate the cached matcher"
