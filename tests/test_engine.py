import sqlite3

import pytest

from engraphis.backends.vector_numpy import NumpyVectorIndex
from engraphis.core.engine import MemoryEngine
from engraphis.core.interfaces import MemoryType, Scope, SearchFilter


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


def test_engine_falls_back_to_numpy_index_offline():
    eng = MemoryEngine.create(":memory:")
    # sqlite-vec is unavailable in the sandbox → factory falls back to NumPy.
    assert isinstance(eng.index, NumpyVectorIndex)


def test_engine_respects_memory_type_and_scope():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    mid = eng.remember("How to add a migration: edit models, run alembic revision.",
                       workspace_id=wid, repo_id=rid, mtype=MemoryType.PROCEDURAL, scope=Scope.REPO)
    rec = eng.store.get_memory(mid)
    assert rec.mtype == MemoryType.PROCEDURAL and rec.scope == Scope.REPO


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
