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
