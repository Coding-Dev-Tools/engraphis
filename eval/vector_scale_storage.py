"""File-backed exact-index measurements; no semantic or agent-quality claims.

Extends ``eval.vector_scale`` with bounded corpus construction, scoped exact
search, restart, native mirror rebuild and concurrent canonical/index writes.
This bypasses extraction, conflict resolution, embeddings and the recall packer.
All database state is synthetic and belongs to a disposable temporary directory.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import ctypes
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import sqlite3
import subprocess
import tempfile
import threading
import time
from typing import Callable, Optional

import numpy as np

from engraphis.backends.vector_numpy import NumpyVectorIndex
from engraphis.core.interfaces import MemoryRecord, MemoryType, Scope, SearchFilter
from engraphis.core.store import Store
from eval.benchmark import report_envelope, sha256_file
from eval.vector_scale import (
    BACKENDS, _latency_ms, _normalized_random, _result_hash, get_vector_index, parse_sizes,
)

_ROOT = Path(__file__).resolve().parents[1]
_SOURCES = (
    "eval/vector_scale.py", "eval/vector_scale_storage.py", "eval/benchmark.py",
    "engraphis/backends/vector_numpy.py", "engraphis/backends/vector_sqlitevec.py",
    "engraphis/core/vector_search.py", "engraphis/core/store.py",
    "engraphis/core/schema.py", "engraphis/core/interfaces.py",
)
_GENERATOR = "numpy.PCG64.standard_normal.float32.normalized.v1"


def write_scale_checkpoint(output: Path, payload: dict) -> None:
    """Atomically replace a redacted checkpoint, outside timed search sections."""
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=output.parent,
            prefix=output.name + ".", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"), allow_nan=False)
            handle.write("\n")
        os.replace(temporary, output)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _source_snapshot() -> dict:
    paths = [name for name in _SOURCES if (_ROOT / name).is_file()]
    hashes = {name: sha256_file(_ROOT / name) for name in paths}
    try:
        diff = subprocess.check_output(
            ["git", "diff", "HEAD", "--", *paths], cwd=_ROOT,
            stderr=subprocess.DEVNULL,
        )
        diff_hash = hashlib.sha256(diff).hexdigest()
    except (OSError, subprocess.CalledProcessError):
        diff_hash = None
    return {"files": hashes, "tracked_diff_sha256": diff_hash}


def _memory() -> dict:
    """Current RSS and process-lifetime peak; Windows does not expose resource."""
    if os.name == "nt":
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in (
                    "peak_working", "working", "peak_paged", "paged",
                    "peak_nonpaged", "nonpaged", "pagefile", "peak_pagefile",
                )
            ]

        values = Counters()
        values.cb = ctypes.sizeof(values)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = (
            wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD,
        )
        if psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(values), values.cb):
            return {"rss_bytes": int(values.working),
                    "process_lifetime_peak_rss_bytes": int(values.peak_working)}
    else:
        try:
            import resource

            peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            return {"rss_bytes": None,
                    "process_lifetime_peak_rss_bytes": int(peak if platform.system() == "Darwin"
                                                           else peak * 1024)}
        except (ImportError, OSError):
            pass
    return {"rss_bytes": None, "process_lifetime_peak_rss_bytes": None}


def _hardware() -> dict:
    cpu = platform.processor()
    physical_ram = None
    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
                cpu = str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
        except OSError:
            pass
        from ctypes import wintypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", wintypes.DWORD), ("load", wintypes.DWORD)] + [
                (name, ctypes.c_ulonglong) for name in (
                    "total_physical", "available_physical", "total_pagefile",
                    "available_pagefile", "total_virtual", "available_virtual",
                    "available_extended_virtual",
                )
            ]

        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        if ctypes.WinDLL("kernel32").GlobalMemoryStatusEx(ctypes.byref(status)):
            physical_ram = int(status.total_physical)
    return {"cpu": cpu, "logical_cpus": os.cpu_count(),
            "physical_ram_bytes": physical_ram,
            "architecture": platform.machine(), "sqlite": sqlite3.sqlite_version,
            "blas_thread_limits": {name: os.environ.get(name) for name in (
                "OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS",
            )}}


def _disk(path: Path) -> dict:
    values = {label: Path(str(path) + suffix).stat().st_size
              if Path(str(path) + suffix).exists() else 0
              for label, suffix in (("database_bytes", ""), ("wal_bytes", "-wal"),
                                    ("shared_memory_bytes", "-shm"))}
    return {**values, "total_bytes": sum(values.values())}


def _insert(store, index, numbers, vectors, workspace_id, repo_ids) -> None:
    """One bounded atomic canonical/native batch, using production storage APIs."""
    store.conn.execute("BEGIN IMMEDIATE")
    try:
        ids = []
        for number, vector in zip(numbers, vectors):
            memory_id = f"mem_scale_{number:09d}"
            ids.append(memory_id)
            store.add_memory(MemoryRecord(
                id=memory_id, content=f"Synthetic exact-index record {number}.",
                mtype=MemoryType.EPISODIC, scope=Scope.REPO,
                workspace_id=workspace_id, repo_id=repo_ids[number % len(repo_ids)],
            ), audit=False, commit=False)
            if not getattr(index, "shares_store_vector_table", False):
                store.put_vector(memory_id, vector, model=_GENERATOR)
        index.upsert(ids, vectors, [{"model": _GENERATOR} for _ in ids], commit=False)
        store.conn.commit()
    except BaseException:
        if store.conn.transaction_owned_by_current_thread():
            store.conn.rollback()
        raise


def _read(index, query, k, flt):
    started = time.perf_counter()
    result = index.search(query, k, filter=flt)
    elapsed = (time.perf_counter() - started) * 1000
    if len({memory_id for memory_id, _ in result}) != len(result):
        raise RuntimeError("exact index returned duplicate IDs")
    if any(not np.isfinite(score) for _, score in result):
        raise RuntimeError("exact index returned a non-finite score")
    return elapsed, result


def _reads(index, query_vectors, k, flt, concurrency, iterations) -> dict:
    began = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(_read, index, query, k, flt)
                   for _ in range(iterations) for query in query_vectors]
        samples = [future.result() for future in futures]
    seconds = time.perf_counter() - began
    return {"timed_searches": len(samples),
            "latency_ms": _latency_ms([latency for latency, _ in samples]),
            "wall_seconds": round(seconds, 6),
            "searches_per_second": round(len(samples) / max(seconds, 1e-9), 3),
            "result_ids_sha256": _result_hash([result for _, result in samples]),
            "result_counts": sorted({len(result) for _, result in samples}),
            "memory": _memory()}


def _mixed(store, index, query_vectors, k, flt, concurrency, count,
           offset, workspace_id, repo_ids, rng) -> dict:
    if count == 0:
        return {"measured": False, "reason": "mixed_writes=0"}
    gate = threading.Event()

    def writes():
        gate.wait()
        samples = []
        for number in range(offset, offset + count):
            vectors = _normalized_random(rng, 1, len(query_vectors[0]))
            started = time.perf_counter()
            _insert(store, index, [number], vectors, workspace_id, repo_ids)
            samples.append((time.perf_counter() - started) * 1000)
        return samples

    def reads():
        gate.wait()
        return _reads(index, query_vectors, k, flt, concurrency, 1)

    with ThreadPoolExecutor(max_workers=2) as pool:
        writer, reader = pool.submit(writes), pool.submit(reads)
        started = time.perf_counter()
        gate.set()
        write_latencies, read_result = writer.result(), reader.result()
    return {"measured": True, "starting_corpus_size": offset, "reader_concurrency": concurrency,
            "writer_concurrency": 1, "committed_writes": len(write_latencies),
            "write_latency_ms": _latency_ms(write_latencies), "reads": read_result,
            "wall_seconds": round(time.perf_counter() - started, 6)}


def _native_rebuild(store, index, dim, batch_size) -> dict:
    if isinstance(index, NumpyVectorIndex):
        return {"measured": False, "reason": "NumPy reads canonical vectors; no separate mirror"}
    # Fault injection affects only the disposable derived mirror. Canonical rows
    # remain intact, and the backend's normal stale-index constructor recreates it.
    store.conn.execute("UPDATE mem_vec_ann_state SET format_version=0 WHERE singleton=1")
    store.conn.commit()
    started = time.perf_counter()
    repaired = get_vector_index(store, dim=dim, prefer="sqlite-vec")
    ids, vectors = [], []
    copied = 0
    for memory_id, vector in store.iter_vectors(include_invalid=True, dim=dim):
        ids.append(memory_id)
        vectors.append(vector)
        if len(ids) == batch_size:
            repaired.upsert(ids, np.asarray(vectors, dtype=np.float32))
            copied += len(ids)
            ids, vectors = [], []
    if ids:
        repaired.upsert(ids, np.asarray(vectors, dtype=np.float32))
        copied += len(ids)
    repaired.mark_rebuild_complete()
    return {"measured": True, "kind": "native_mirror_replay_from_canonical_vectors",
            "records_replayed": copied, "seconds": round(time.perf_counter() - started, 6)}


def run_file_backed(sizes: list[int], *, dim: int = 256, queries: int = 20,
                    iterations: int = 3, warmups: int = 1, k: int = 10,
                    seed: int = 20260731, backend: str = "numpy",
                    concurrencies: Optional[list[int]] = None, mixed_writes: int = 4,
                    batch_size: int = 500, tenants: int = 4,
                    progress: Optional[Callable[[dict], None]] = None,
                    checkpoint: Optional[Callable[[dict], None]] = None) -> dict:
    """Return redacted reproducible evidence; large cells run only when requested."""
    sizes = parse_sizes(",".join(str(value) for value in sizes))
    concurrencies = [1, 4, 16] if concurrencies is None else list(concurrencies)
    if not concurrencies or len(set(concurrencies)) != len(concurrencies) or any(
        value not in (1, 4, 16) for value in concurrencies
    ):
        raise ValueError("concurrencies must be distinct choices from 1, 4, 16")
    if backend not in BACKENDS:
        raise ValueError("backend must be numpy or sqlite-vec")
    if min(dim, queries, iterations, k, batch_size, tenants) < 1 or min(warmups, mixed_writes) < 0:
        raise ValueError("dimensions/counts must be positive; warmups and mixed_writes nonnegative")
    source_before = _source_snapshot()
    inputs, cells, storage = [], [], []
    query_vectors = _normalized_random(np.random.default_rng(seed + 1), queries, dim)
    query_hash = hashlib.sha256(query_vectors.tobytes()).hexdigest()
    backend_class = ""
    config = {"sizes": sizes, "dimension": dim, "queries": queries, "iterations": iterations,
              "warmups": warmups, "k": k, "seed": seed, "backend": backend,
              "concurrencies": concurrencies, "mixed_writes_per_cell": mixed_writes,
              "batch_size": batch_size, "tenant_scopes": tenants, "file_backed": True,
              "vector_generator": _GENERATOR, "inputs": inputs, "queries_sha256": query_hash}

    def checkpoint_phase(phase: str, size: int, **details) -> None:
        if checkpoint:
            checkpoint({"schema": "engraphis-scale-checkpoint/v1", "status": "incomplete",
                        "phase": phase, "corpus_size": size, "config": config,
                        "source_before": source_before, "backend_class": backend_class,
                        "completed_cells": cells, "completed_storage": storage, **details})
        if progress:
            progress({"backend": backend, "corpus_size": size, "stage": phase,
                      **({"concurrency": details["concurrency"]} if "concurrency" in details else {})})

    for size in sizes:
        checkpoint_phase("population", size)
        # TemporaryDirectory owns exactly this newly created directory; no caller
        # path is recursively removed, and every Store closes before cleanup.
        with tempfile.TemporaryDirectory(prefix="egr-scale-") as folder:
            path = Path(folder) / "corpus.db"
            started = time.perf_counter()
            store = Store(str(path))
            try:
                index = get_vector_index(store, dim=dim, prefer=backend)
                backend_class = type(index).__name__
                initial_startup_ms = (time.perf_counter() - started) * 1000
                workspace_id = store.get_or_create_workspace("vector-scale")
                repo_ids = [store.get_or_create_repo(workspace_id, f"scope-{number}")
                            for number in range(tenants)]
                rng = np.random.default_rng(seed)
                digest = hashlib.sha256()
                began = time.perf_counter()
                batch_latencies = []
                for offset in range(0, size, batch_size):
                    stop = min(size, offset + batch_size)
                    vectors = _normalized_random(rng, stop - offset, dim)
                    digest.update(vectors.tobytes())
                    batch_started = time.perf_counter()
                    _insert(store, index, range(offset, stop), vectors, workspace_id, repo_ids)
                    batch_latencies.append((time.perf_counter() - batch_started) * 1000)
                    if progress and (stop == size or stop % 10_000 == 0):
                        progress({"backend": backend, "corpus_size": size,
                                  "records_written": stop, "stage": "population"})
                if hasattr(index, "mark_rebuild_complete"):
                    checkpoint_phase("native_publication", size)
                    index.mark_rebuild_complete()
                population_seconds = time.perf_counter() - began
                inputs.append({"corpus_size": size, "vectors_sha256": digest.hexdigest()})
                populated_disk = _disk(path)
                store.close()
                checkpoint_phase("restart", size, population_seconds=population_seconds)
                began = time.perf_counter()
                store = Store(str(path))
                index = get_vector_index(store, dim=dim, prefer=backend)
                restart_ms = (time.perf_counter() - began) * 1000
                if getattr(index, "requires_rebuild", False):
                    raise RuntimeError("completed native mirror was not ready after restart")
                flt = SearchFilter(workspace_id=workspace_id, repo_id=repo_ids[0])
                cold = _reads(index, query_vectors, k, flt, 1, 1)
                # Reference parity is measured outside the latency cells. This is
                # NumPy/native agreement, not an independent retrieval-quality eval.
                reference = NumpyVectorIndex(store, dim=dim)
                reference_results = [reference.search(query, k, filter=flt)
                                     for query in query_vectors]
                expected_hash = _result_hash(reference_results * iterations)
                expected_count = min(k, (size + tenants - 1) // tenants)
                for concurrency in concurrencies:
                    if warmups:
                        _reads(index, query_vectors, k, flt, concurrency, warmups)
                    measured = _reads(index, query_vectors, k, flt, concurrency, iterations)
                    parity = measured["result_ids_sha256"] == expected_hash
                    if not parity or measured["result_counts"] != [expected_count]:
                        raise RuntimeError("exact-index results differ from the NumPy reference")
                    cells.append({"corpus_size": size, "concurrency": concurrency,
                                  "status": "complete", "numpy_reference_parity": parity,
                                  **measured})
                    checkpoint_phase("reads_complete", size, concurrency=concurrency)
                mixed = []
                for position, concurrency in enumerate(concurrencies):
                    mixed.append(_mixed(store, index, query_vectors, k, flt, concurrency,
                                        mixed_writes, size + position * mixed_writes,
                                        workspace_id, repo_ids, rng))
                    checkpoint_phase("mixed_cell_complete", size, current_mixed=mixed)
                checkpoint_phase("native_rebuild", size, current_mixed=mixed)
                rebuilt = _native_rebuild(store, index, dim, batch_size)
                checkpoint_phase("mixed_and_rebuild_complete", size,
                                 current_mixed=mixed, rebuild=rebuilt)
                expected_rows = size + len(concurrencies) * mixed_writes
                store.close()
                store = Store(str(path))
                reopened = get_vector_index(store, dim=dim, prefer=backend)
                actual_rows = int(store.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
                vector_rows = int(store.conn.execute("SELECT COUNT(*) FROM mem_vectors").fetchone()[0])
                if (actual_rows != expected_rows or vector_rows != expected_rows
                        or getattr(reopened, "requires_rebuild", False)):
                    raise RuntimeError("mixed writes or native rebuild did not survive restart")
                storage.append({"corpus_size": size, "initial_startup_ms": initial_startup_ms,
                                "restart_ms": restart_ms, "population_seconds": population_seconds,
                                "population_records_per_second": size / max(population_seconds, 1e-9),
                                "population_batch_latency_ms": _latency_ms(batch_latencies),
                                "connection_cold_reads": cold, "mixed": mixed, "rebuild": rebuilt,
                                "durable_memory_rows": actual_rows, "durable_vector_rows": vector_rows,
                                "populated_disk": populated_disk, "final_disk": _disk(path),
                                "memory": _memory()})
                checkpoint_phase("corpus_complete", size)
            finally:
                store.close()
    source_after = _source_snapshot()
    try:
        native_version = importlib.metadata.version("sqlite-vec") if backend == "sqlite-vec" else None
    except importlib.metadata.PackageNotFoundError:
        native_version = "unavailable"
    return report_envelope(
        suite="file-backed-exact-index-scale/v1", dataset_path=Path(__file__), config=config,
        records=[{"question_id": f"n{cell['corpus_size']}-c{cell['concurrency']}",
                  "category": "exact_index_throughput", "latency_ms": cell["latency_ms"]["p50"]}
                 for cell in cells],
        metrics={"cells": cells, "storage": storage, "hardware": _hardware(),
                 "source_before": source_before, "source_after": source_after,
                 "source_stable": source_before == source_after,
                 "measurement_scope": "synthetic scoped exact-index and storage operations; not end-to-end recall",
                 "timing_scope": "latency is execution including storage locks, excludes executor queue; throughput includes queue",
                 "cold_scope": "new connection after population; operating-system disk cache is not flushed",
                 "unmeasured": ["embedding/model latency", "extraction and conflict resolution",
                                "agent task quality", "full recall and context packing",
                                "multi-process contention", "independent repeated processes"],
                 "percentile_scope": "descriptive samples, not tail-SLO confidence bounds"},
        source_paths=[_ROOT / name for name in _SOURCES if (_ROOT / name).is_file()],
        models={"embedding": {"identity": "none; precomputed synthetic vectors"},
                "vector_backend": {"identity": backend_class, "native_version": native_version},
                "tokenizer": {"identity": "not applicable; no reader context"}},
        token_accounting={"identity": "not_applicable", "revision": None,
                          "scope": "no reader context", "method": "not_measured"},
        command=["python", "-m", "eval.vector_scale", "--file-backed", "--backend", backend,
                 "--sizes", ",".join(map(str, sizes)), "--dim", str(dim),
                 "--queries", str(queries), "--iterations", str(iterations),
                 "--warmups", str(warmups), "--k", str(k), "--seed", str(seed),
                 "--concurrencies", ",".join(map(str, concurrencies)),
                 "--mixed-writes", str(mixed_writes), "--batch-size", str(batch_size),
                 "--tenants", str(tenants), *(["--progress"] if progress else [])],
    )
