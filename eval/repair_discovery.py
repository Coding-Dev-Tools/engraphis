"""Offline repair-only contention probe; not an engine-capacity or quality result.

Setup bulk-seeds synthetic canonical rows on a disposable file-backed database.
The measured operation repairs one erased external ID after a queue of updates,
with embedding-space readiness deliberately unavailable. The adapter is a local
synchronous fixture, so no model, network, provider latency or paid calls occur.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import tempfile
import time

import numpy as np

import engraphis
from engraphis.core.interfaces import MemoryRecord, Scope
from engraphis.core import vector_repair
from engraphis.factory import create_memory_engine
from eval.benchmark import (
    canonical_json, environment_provenance, git_provenance, sha256_file,
)
from eval.vector_scale_storage import _disk, _hardware


class _Index:
    index_identity = "repair-discovery-probe-v1"

    def __init__(self):
        self.present = {"mem_probe_erased"}
        self.calls = 0

    def upsert(self, *args, **kwargs):
        raise AssertionError("this probe must only publish cleanup")

    def delete(self, ids, **kwargs):
        self.calls += 1
        self.present.difference_update(ids)


def run_probe(backlog: int, repetition: int, *, directory=None) -> dict:
    """Measure one warm, uncontented invocation, including queue scans and counts."""
    if type(backlog) is not int or backlog < 1:
        raise ValueError("backlog must be a positive integer")
    with tempfile.TemporaryDirectory(prefix="repair-probe-", dir=directory) as temporary:
        path = Path(temporary) / "probe.db"
        engine = create_memory_engine(
            str(path), embed_dim=32, auto_evolve=False, extractor="none",
            graph_extractor="none", require_exact_backends=True,
        )
        try:
            store = engine.store
            workspace = store.get_or_create_workspace("repair-probe")
            index = _Index()
            target = vector_repair.index_repair_identity(index, store)
            assert target is not None
            store.register_vector_index(target)
            vector = np.zeros(32, dtype=np.float32)
            vector[0] = 1.0
            with store.write_transaction():
                for number in range(backlog + 1):
                    mid = f"mem_probe_{number:08d}" if number < backlog else "mem_probe_erased"
                    store.add_memory(MemoryRecord(
                        id=mid, content=f"Synthetic record {number}.", workspace_id=workspace,
                        scope=Scope.WORKSPACE, provenance={"source": "offline-repair-probe"},
                    ), audit=False, commit=False)
                    store.put_vector(mid, vector, model=engine.embedding_space)
                # A disposable fixture deletion creates real trigger-maintained debt.
                store.conn.execute("DELETE FROM memories WHERE id=?", ("mem_probe_erased",))
            assert store.vector_index_pending(target) == backlog + 1
            last = store.conn.execute(
                "SELECT memory_id FROM vector_index_repairs WHERE identity=? "
                "ORDER BY generation DESC,memory_id DESC LIMIT 1", (target,),
            ).fetchone()
            assert last[0] == "mem_probe_erased"
            pragmas = {name: store.conn.execute(f"PRAGMA {name}").fetchone()[0]
                       for name in ("journal_mode", "synchronous", "page_size")}
            disk_before = _disk(path)
            original = store.write_transaction
            measured = {"writer_reservations": 0, "writer_acquisition_ms": 0.0,
                        "writer_reserved_ms": 0.0}

            @contextmanager
            def timed_writer():
                # Nested acknowledgement shares the reservation; never count it twice.
                if store.conn.transaction_owned_by_current_thread():
                    with original():
                        yield
                    return
                started = time.perf_counter()
                acquired = None
                try:
                    with original():
                        acquired = time.perf_counter()
                        measured["writer_reservations"] += 1
                        measured["writer_acquisition_ms"] += (acquired - started) * 1000
                        yield
                finally:
                    if acquired is not None:
                        # Includes commit/rollback and the wrapper's release overhead.
                        measured["writer_reserved_ms"] += (time.perf_counter() - acquired) * 1000

            store.write_transaction = timed_writer
            try:
                started = time.perf_counter()
                result = vector_repair.repair_vector_index(
                    store, index, embedding_space="unavailable-space", dim=0, limit=1,
                )
                elapsed_ms = (time.perf_counter() - started) * 1000
            finally:
                store.write_transaction = original
            assert result == {"attempted": 1, "repaired": 1, "pending": backlog}
            assert index.calls == 1 and not index.present
            assert store.get_memory("mem_probe_erased") is None
            assert store.vector_index_repair_generations(target, ["mem_probe_erased"]) == {}
            return {"backlog_updates": backlog, "repetition": repetition,
                    "elapsed_ms": elapsed_ms, **measured, "result": result,
                    "provider_calls": index.calls, "erasure_verified": True,
                    "sqlite_pragmas": pragmas, "disk_before_bytes": disk_before}
        finally:
            engine.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backlog", type=int, default=1000)
    parser.add_argument("--repetition", type=int, default=1)
    parser.add_argument("--temp-root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(engraphis.__file__).resolve().parents[1]
    sources = {path.relative_to(root).as_posix(): sha256_file(path) for path in sorted([
        *root.joinpath("engraphis/core").glob("*.py"),
        *root.joinpath("engraphis/backends").glob("*.py"),
        root / "engraphis/factory.py", root / "engraphis/__init__.py",
        root / "eval/benchmark.py", root / "eval/vector_scale_storage.py",
    ])}
    payload = {
        "schema": "engraphis-repair-discovery-probe/v1",
        "configuration": {"backlog": args.backlog, "repetition": args.repetition},
        "source": git_provenance(root), "source_files": sources,
        "driver_sha256": sha256_file(__file__), "environment": environment_provenance(),
        "sqlite_version": sqlite3.sqlite_version, "hardware": _hardware(),
        "boundary": {
            "operation": "one repair(limit=1), erasure after queued updates",
            "storage": "file-backed SQLite", "external_index": "synchronous fixture",
            "vector": "fixed one-hot float32, dimension 32, bulk-seeded",
            "embedding": "none measured; readiness deliberately unavailable",
            "tokenizer": "not used", "concurrency": 1, "temperature": "warm after setup",
            "includes": ["discovery", "writer acquisition", "publication", "pending count",
                         "same transaction-timing instrumentation on both sources"],
            "excludes": ["setup", "embedding", "recall", "network", "assertions", "teardown"],
            "limitations": ["not target hardware qualification", "not a contention load test",
                            "not a total scan or provider latency bound", "synthetic data"],
        },
    }
    try:
        payload["measurement"] = run_probe(
            args.backlog, args.repetition, directory=args.temp_root,
        )
        payload["status"] = "ok"
    except (Exception, KeyboardInterrupt) as exc:
        # Retain attempted-cell identity even when setup or verification fails.
        # Do not represent missing measurements as successful zero-cost work.
        payload["status"] = "failed"
        payload["failure"] = {"type": type(exc).__name__}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(canonical_json(payload) + "\n", encoding="utf-8")
    if payload["status"] != "ok":
        print(canonical_json(payload["failure"]))
        raise SystemExit(1)
    print(canonical_json(payload["measurement"]))


if __name__ == "__main__":
    main()
