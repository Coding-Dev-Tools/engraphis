"""Real governed transitions across independent connections and spawned processes."""
import multiprocessing
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from engraphis.core.interfaces import Scope, SearchFilter
from engraphis.core.mutations import MemoryConflict
from engraphis.factory import create_memory_engine


def _prepare(path, kind):
    engine = create_memory_engine(path, auto_evolve=False)
    workspace = engine.store.get_or_create_workspace("governance")
    repo = engine.store.get_or_create_repo(workspace, "project")
    source = engine.remember(
        "The cache expires after 30 days.", workspace_id=workspace, repo_id=repo,
        scope=Scope.REPO, resolve_conflicts=False,
        metadata={"provenance": {"trusted": kind != "approve", "source": "fixture"}},
    )
    other = engine.remember("Backups are encrypted.", workspace_id=workspace,
                            repo_id=repo, scope=Scope.REPO, resolve_conflicts=False)
    engine.close()
    return workspace, [source, other]


def _operate(engine, kind, sources, value):
    try:
        if kind == "correct":
            result = engine.correct(sources[0], f"The cache expires after {value} days.")
        elif kind == "approve":
            result = engine.approve_for_prompt(
                sources[0], reviewer="owner", reason="verified",
                replacement_content=f"The cache expires after {value} days.",
            )
        elif kind == "promote":
            result = engine.promote(sources[0], Scope.WORKSPACE)
        else:
            result = engine.merge(sources, f"Encrypted backups expire after {value} days.")
        return ("ok", result["id"])
    except MemoryConflict:
        return ("conflict", "")


def _worker(path, kind, sources, value, barrier, output):
    engine = create_memory_engine(path, auto_evolve=False)
    original = engine.embedder.embed

    def embed(texts, *, kind="text"):
        assert not engine.store.conn.transaction_owned_by_current_thread()
        vectors = original(texts, kind=kind)
        barrier.wait(timeout=30)
        return vectors

    engine.embedder.embed = embed
    try:
        output.put(_operate(engine, kind, sources, value))
    finally:
        engine.close()


@pytest.mark.parametrize("kind", ["correct", "approve", "promote", "merge"])
@pytest.mark.parametrize("contradictory", [False, True])
def test_governed_writes_across_instances(tmp_path, monkeypatch, kind, contradictory):
    path = str(tmp_path / "instances.db")
    workspace, sources = _prepare(path, kind)
    engines = [create_memory_engine(path, auto_evolve=False) for _ in range(2)]
    barrier = threading.Barrier(2, timeout=15)
    for engine in engines:
        original = engine.embedder.embed

        def embed(texts, *, kind="text", engine=engine, original=original):
            assert not engine.store.conn.transaction_owned_by_current_thread()
            vectors = original(texts, kind=kind)
            barrier.wait()
            return vectors

        monkeypatch.setattr(engine.embedder, "embed", embed)
    values = [60, 90 if contradictory else 60]
    try:
        with ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(_operate, engine, kind, sources, value)
                       for engine, value in zip(engines, values)]
            results = [future.result(timeout=25) for future in futures]
        if contradictory and kind != "promote":
            assert sorted(row[0] for row in results) == ["conflict", "ok"]
        else:
            assert len(set(results)) == 1
            assert results[0][0] == "ok"
        live = engines[0].store.list_memories(SearchFilter(workspace_id=workspace))
        successors = [record for record in live if record.id not in sources]
        assert len(successors) == 1
    finally:
        for engine in engines:
            engine.close()


@pytest.mark.parametrize("kind", ["correct", "approve", "promote", "merge"])
def test_governed_writes_across_processes(tmp_path, kind):
    path = str(tmp_path / "processes.db")
    _, sources = _prepare(path, kind)
    context = multiprocessing.get_context("spawn")
    barrier, output = context.Barrier(2), context.Queue()
    workers = [context.Process(target=_worker,
                               args=(path, kind, sources, value, barrier, output))
               for value in (60, 90)]
    try:
        for worker in workers:
            worker.start()
        results = [output.get(timeout=45) for _ in workers]
        if kind == "promote":
            assert results[0] == results[1]
        else:
            assert sorted(row[0] for row in results) == ["conflict", "ok"]
        for worker in workers:
            worker.join(timeout=10)
            assert worker.exitcode == 0
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=10)
        output.close()


def test_failed_transition_rolls_back_receipt_and_source_claim(tmp_path, monkeypatch):
    path = str(tmp_path / "rollback.db")
    _, sources = _prepare(path, "correct")
    engine = create_memory_engine(path, auto_evolve=False)
    try:
        original = engine.store.close_validity

        def fail(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("interrupted correction")

        with monkeypatch.context() as patch:
            patch.setattr(engine.store, "close_validity", fail)
            with pytest.raises(RuntimeError, match="interrupted"):
                engine.correct(sources[0], "The cache expires after 60 days.")
        assert engine.store.conn.execute("SELECT COUNT(*) FROM memory_commands").fetchone()[0] == 0
        assert engine.store.get_memory(sources[0]).valid_to is None
        result = engine.correct(sources[0], "The cache expires after 60 days.")
        assert result["id"] != sources[0]
    finally:
        engine.close()


@pytest.mark.parametrize("reviewer", ["original-owner", "owner-" + "a" * 220])
def test_approval_retry_reports_original_reviewer_after_reopening(tmp_path, monkeypatch, reviewer):
    path = str(tmp_path / "reviewer.db")
    _, sources = _prepare(path, "approve")
    engine = create_memory_engine(path, auto_evolve=False)
    first = engine.approve_for_prompt(sources[0], reviewer=reviewer, reason="verified")
    engine.close()
    engine = create_memory_engine(path, auto_evolve=False)
    try:
        def unavailable(*args, **kwargs):
            raise RuntimeError("embedding provider unavailable after approval")

        monkeypatch.setattr(engine.embedder, "embed", unavailable)
        retry = engine.approve_for_prompt(sources[0], reviewer="different-owner", reason="lost response")
        direct = engine.approve_for_prompt(first["id"], reviewer="third-owner", reason="repeat")
        stored = engine.store.get_memory(first["id"]).metadata["approval"]["reviewer"]
        assert first == retry == direct
        assert retry["reviewer"] == stored == reviewer[:200]
        rows = engine.store.conn.execute(
            "SELECT detail FROM audit WHERE action='approve' AND target=?", (first["id"],)
        ).fetchall()
        assert len(rows) == 1
        assert f"reviewer={stored}" in rows[0]["detail"]
    finally:
        engine.close()


@pytest.mark.parametrize("removal", [None, "retire", "secure_erase"])
def test_approval_retry_preserves_result_guards_without_embeddings(tmp_path, monkeypatch, removal):
    path = str(tmp_path / "approval-outage.db")
    _, sources = _prepare(path, "approve")
    engine = create_memory_engine(path, auto_evolve=False)
    try:
        command = {"reviewer": "owner", "reason": "verified"}
        result = engine.approve_for_prompt(sources[0], **command)
        if removal:
            getattr(engine, removal)(result["id"])

        def unavailable(*args, **kwargs):
            raise RuntimeError("embedding provider unavailable after approval")

        monkeypatch.setattr(engine.embedder, "embed", unavailable)
        before = engine.store.conn.total_changes
        if removal:
            with pytest.raises(ValueError, match="retired|erased"):
                engine.approve_for_prompt(sources[0], **command)
        else:
            assert engine.approve_for_prompt(sources[0], **command) == result
            with pytest.raises(MemoryConflict, match="different content"):
                engine.approve_for_prompt(
                    sources[0], **command, replacement_content="A contradictory approval.",
                )
        assert engine.store.conn.total_changes == before
    finally:
        engine.close()


@pytest.mark.parametrize("approval", [None, 1, "legacy", {"reviewer": None}])
def test_approval_without_recorded_reviewer_does_not_invent_one(tmp_path, approval):
    engine = create_memory_engine(str(tmp_path / "legacy-reviewer.db"), auto_evolve=False)
    try:
        wid = engine.store.get_or_create_workspace("governance")
        mid = engine.remember("A local fact.", workspace_id=wid, metadata={"approval": approval})
        result = engine.approve_for_prompt(mid, reviewer="later-owner", reason="repeat")
        assert result["id"] == mid
        assert result["reviewer"] == ""
    finally:
        engine.close()


def _session_sources(engine):
    workspace = engine.store.get_or_create_workspace("governance")
    repo = engine.store.get_or_create_repo(workspace, "project")
    session = engine.start_session(workspace, repo)
    sources = [engine.remember(
        content, workspace_id=workspace, repo_id=repo, session_id=session,
        scope=Scope.SESSION, resolve_conflicts=False,
    ) for content in ("The cache expires after 30 days.", "Backups are encrypted.")]
    return session, sources


def _session_operation(engine, kind, sources, *, reason="verified"):
    if kind == "promote":
        return engine.promote(sources[0], Scope.REPO, reason=reason)
    return engine.merge(sources, "Encrypted backups expire after 30 days.", reason=reason)


@pytest.mark.parametrize("kind", ["promote", "merge"])
def test_session_transition_replays_after_close_and_restart(tmp_path, monkeypatch, kind):
    path = str(tmp_path / "session-retry.db")
    engine = create_memory_engine(path, auto_evolve=False)
    session, sources = _session_sources(engine)
    first = _session_operation(engine, kind, sources)
    engine.end_session(session, summary="Work complete.")
    engine.close()
    engine = create_memory_engine(path, auto_evolve=False)
    try:
        before = engine.store.conn.total_changes

        def unavailable(*args, **kwargs):
            raise RuntimeError("embedding provider is unavailable")

        monkeypatch.setattr(engine.embedder, "embed", unavailable)
        replay = _session_operation(engine, kind, sources)
        assert replay["id"] == first["id"]
        assert {k: v for k, v in replay.items() if k != "op"} == {
            k: v for k, v in first.items() if k != "op"
        }
        assert engine.store.conn.total_changes == before
        assert engine.store.conn.execute("SELECT COUNT(*) FROM memory_commands").fetchone()[0] == 1
        with pytest.raises(ValueError, match="closed session|active session"):
            _session_operation(engine, kind, sources, reason="a different operation")
        assert engine.store.conn.total_changes == before
    finally:
        engine.close()


@pytest.mark.parametrize("kind", ["promote", "merge"])
@pytest.mark.parametrize("removal", ["retire", "secure_erase"])
def test_closed_session_retry_does_not_recreate_removed_successor(tmp_path, kind, removal):
    engine = create_memory_engine(str(tmp_path / "removed-result.db"), auto_evolve=False)
    try:
        session, sources = _session_sources(engine)
        first = _session_operation(engine, kind, sources)
        engine.end_session(session)
        getattr(engine, removal)(first["id"])
        before = engine.store.conn.total_changes
        with pytest.raises(MemoryConflict) as caught:
            _session_operation(engine, kind, sources)
        assert caught.value.code == "result_unavailable"
        assert engine.store.conn.total_changes == before
    finally:
        engine.close()


@pytest.mark.parametrize("kind", ["promote", "merge"])
def test_new_session_transition_rechecks_close_during_preparation(tmp_path, monkeypatch, kind):
    path = str(tmp_path / "session-race.db")
    engine = create_memory_engine(path, auto_evolve=False)
    session, sources = _session_sources(engine)
    closer = create_memory_engine(path, auto_evolve=False)
    original = engine.embedder.embed

    def close_during_embed(texts, *, kind="text"):
        assert not engine.store.conn.transaction_owned_by_current_thread()
        closer.end_session(session)
        return original(texts, kind=kind)

    monkeypatch.setattr(engine.embedder, "embed", close_during_embed)
    try:
        with pytest.raises(ValueError, match="closed session|not active"):
            _session_operation(engine, kind, sources)
        assert engine.store.conn.execute("SELECT COUNT(*) FROM memory_commands").fetchone()[0] == 0
        assert engine.store.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 2
        assert all(engine.store.get_memory(mid).valid_to is None for mid in sources)
    finally:
        closer.close()
        engine.close()
