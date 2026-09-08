"""File-backed, process-owned engine workload evidence; never an agent-quality claim."""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, dataclass
import hashlib
import importlib.metadata
import importlib.util
import json
import math
import multiprocessing
import os
from pathlib import Path
import queue
import tempfile
import threading
import time
from typing import Optional
import uuid

from engraphis.core.engine import MemoryEngine
from engraphis.core.interfaces import Scope
from eval.benchmark import canonical_json, report_envelope, sha256_file, write_canonical_artifact
from eval.vector_scale import _latency_ms
from eval.vector_scale_storage import _disk, _hardware


ROOT = Path(__file__).resolve().parents[1]
HARDWARE = {"laptop16": 16, "shared32": 32}
SCHEMA = "engraphis-engine-capacity/v1"


def protocol() -> dict:
    cells = [
        {"hardware": hardware, "size": size, "backend": backend,
         "concurrency": concurrency, "workload": workload}
        for hardware in HARDWARE for size in (10_000, 100_000)
        for backend in ("numpy", "sqlite-vec") for concurrency in (1, 4, 16)
        for workload in ("read", "mixed")
    ]
    return {"schema": SCHEMA, "primary_cells": cells, "primary_cell_count": 48,
            "repeats": 5, "operations_per_repeat": 2000,
            "mixed_percent": {"recall": 80, "remember": 15, "correct": 4, "erase": 1},
            "arrival_rate": "one operation per agent per second; recorded in every cell",
            "stress_sizes": [1_000_000], "target_capacity_verified": False}


@dataclass(frozen=True)
class Cell:
    size: int = 16
    concurrency: int = 1
    operations: int = 100
    repeats: int = 1
    backend: str = "numpy"
    workload: str = "mixed"
    hardware: str = "laptop16"
    dimension: int = 32
    token_budget: int = 1500
    seed: int = 20260905
    arrival_rate: float = 0.0
    timeout_s: float = 7200.0
    smoke: bool = True

    def validate(self) -> None:
        if self.backend not in {"numpy", "sqlite-vec"}:
            raise ValueError("backend must be numpy or sqlite-vec")
        if self.hardware not in HARDWARE or self.workload not in {"read", "mixed"}:
            raise ValueError("invalid hardware or workload")
        if type(self.concurrency) is not int or self.concurrency not in {1, 4, 16}:
            raise ValueError("concurrency must be 1, 4 or 16")
        if type(self.seed) is not int or type(self.smoke) is not bool:
            raise ValueError("seed and smoke must be an integer and boolean")
        for name in ("size", "operations", "repeats", "dimension", "token_budget"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.operations % 100:
            raise ValueError("operations must be a multiple of 100 for an exact mixed ratio")
        if (type(self.arrival_rate) not in {int, float} or type(self.timeout_s) not in {int, float}
                or not 0 <= self.arrival_rate < float("inf") or not 0 < self.timeout_s < float("inf")):
            raise ValueError("arrival_rate and timeout_s must be finite and nonnegative/positive")
        if self.arrival_rate and self.timeout_s <= (self.operations - 1) / self.arrival_rate:
            raise ValueError("timeout must exceed the declared arrival schedule")
        if self.size <= self.operations // 20:
            raise ValueError("size must leave immutable reads and distinct mutation targets")
        if not self.smoke and (
            self.size not in {10_000, 100_000, 1_000_000}
            or self.repeats < 5 or self.operations < 2000
            or self.arrival_rate != self.concurrency
        ):
            raise ValueError("protocol runs require a declared size, 5 repeats, 2000 operations "
                             "and one operation per agent per second")


def _snapshot() -> dict:
    paths = [*ROOT.joinpath("engraphis/core").glob("*.py"),
             *ROOT.joinpath("engraphis/backends").glob("*.py"),
             ROOT / "engraphis/factory.py", ROOT / "engraphis/__init__.py",
             Path(__file__), ROOT / "eval/benchmark.py", ROOT / "eval/vector_scale.py",
             ROOT / "eval/vector_scale_storage.py"]
    return {path.relative_to(ROOT).as_posix(): sha256_file(path) for path in sorted(paths)}


def _local_model(path: Optional[str], expected_digest: Optional[str]) -> dict:
    if path is None:
        if expected_digest is not None:
            raise ValueError("model digest requires a local model directory")
        return {"identity": "deterministic_hashing", "semantic": False}
    directory = Path(path).resolve(strict=True)
    if not directory.is_dir():
        raise ValueError("model must be an existing local directory")
    files = {}
    for item in sorted(directory.rglob("*")):
        if item.is_symlink():
            raise ValueError("model directory must be self-contained, without symlinks")
        if item.is_file():
            files[item.relative_to(directory).as_posix()] = sha256_file(item)
    actual = hashlib.sha256(canonical_json(files).encode()).hexdigest()
    if not files or expected_digest != actual:
        raise ValueError("local model directory digest does not match the frozen identity")
    return {"identity": "local_directory", "sha256": actual, "semantic": True}


def _engine(path: str, cell: Cell, model: Optional[str]) -> MemoryEngine:
    # No implicit downloads, extraction, or provider calls in a benchmark worker.
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["ENGRAPHIS_EXTRACTOR"] = "none"
    engine = MemoryEngine.create(path, embed_model="local:" + model if model else None,
                                 embed_dim=cell.dimension, vector_backend=cell.backend,
                                 require_exact_backends=True, extractor="none", graph_extractor="none")
    if (engine.embedder.dim != cell.dimension
            or (model is not None and not engine.embedder.supports_semantic_search)):
        engine.store.close()
        raise ValueError("observed embedding dimension/capability differs from the declared cell")
    return engine


def _text(index: int) -> str:
    return f"Service marker_{index:06d} retains deployment logs for {30 + index % 61} days."


def _seed(path: str, cell: Cell, model: Optional[str]) -> tuple[list[dict], float]:
    started = time.perf_counter()
    engine = _engine(path, cell, model)
    targets = []
    try:
        workspace = engine.store.get_or_create_workspace("capacity")
        repos = [engine.store.get_or_create_repo(workspace, f"project-{i}")
                 for i in range(min(40, cell.size))]
        for index in range(cell.size):
            repo = repos[index % len(repos)]
            result = engine.remember_with_resolution(
                _text(index), workspace_id=workspace, repo_id=repo, scope=Scope.REPO,
                subject_key=f"capacity.service.{index}", claim_kind="retention",
            )
            if result["op"] != "add":
                raise RuntimeError("synthetic seed lost a distinct fact")
            targets.append({"id": result["id"], "workspace": workspace, "repo": repo,
                            "index": index})
    finally:
        engine.store.close()
    return targets, (time.perf_counter() - started) * 1000


def operation_plan(cell: Cell, targets: list[dict]) -> list[dict]:
    """Stable schedules have exact ratios and never race erasure against a gold read."""
    import random

    kinds = (["recall"] * 100 if cell.workload == "read" else
             ["recall"] * 80 + ["remember"] * 15 + ["correct"] * 4 + ["erase"])
    kinds *= cell.operations // 100
    random.Random(cell.seed).shuffle(kinds)
    mutable_count = sum(kind in {"correct", "erase"} for kind in kinds)
    immutable_count = len(targets) - mutable_count
    mutation = immutable_count
    operations = []
    for index, kind in enumerate(kinds):
        if kind in {"correct", "erase"}:
            target = targets[mutation]
            mutation += 1
        else:
            target = targets[(index * 17 + cell.seed) % immutable_count]
        operations.append({"number": index, "kind": kind, "target": target})
    return operations


def _operate(engine: MemoryEngine, job: dict, cell: Cell) -> dict:
    target, kind, number = job["target"], job["kind"], job["number"]
    started = time.perf_counter()
    if kind == "recall":
        result = engine.recall(f"marker_{target['index']:06d}",
                               workspace_id=target["workspace"], repo_id=target["repo"],
                               token_budget=cell.token_budget, diagnostics=True)
    elif kind == "remember":
        result = engine.remember_with_resolution(
            f"New capacity setting new_{number:06d} is {number + 7}.",
            workspace_id=target["workspace"], repo_id=target["repo"],
            subject_key=f"capacity.new.{number}", claim_kind="setting",
        )
    elif kind == "correct":
        result = engine.correct(target["id"], f"Corrected marker_{target['index']:06d} "
                                f"retains deployment logs for {number + 100} days.")
    else:
        result = engine.secure_erase(target["id"], actor="capacity_benchmark")
    operation_ms = (time.perf_counter() - started) * 1000
    verify = time.perf_counter()
    if kind == "recall":
        correct = target["id"] in {chunk.id for chunk in result.packed_chunks}
        tokens = result.usage.context_tokens
    elif kind == "remember":
        correct = result["op"] == "add" and engine.store.get_memory(result["id"]) is not None
        tokens = 0
    elif kind == "correct":
        old = engine.store.get_memory(target["id"])
        new = engine.store.get_memory(result["id"])
        correct = (old is not None and old.valid_to is not None and result["id"] != target["id"]
                   and new is not None and new.valid_to is None
                   and new.content == f"Corrected marker_{target['index']:06d} "
                   f"retains deployment logs for {number + 100} days.")
        tokens = 0
    else:
        correct = engine.store.get_memory(target["id"]) is None
        tokens = 0
    phases = {}
    if kind == "recall":
        raw = (getattr(result, "diagnostics_v1", None) or {}).get("phase_ms", {})
        # Persist only the known numeric observation; retrieval traces may contain source text.
        value = raw.get("engine_recall")
        if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0:
            phases["engine_recall"] = value
    return {"correct": correct, "context_tokens": tokens, "operation_ms": operation_ms,
            "verification_ms": (time.perf_counter() - verify) * 1000, "phase_ms": phases}


def _worker(database: str, cell: Cell, model: Optional[str], incoming, outgoing) -> None:
    engine = None
    try:
        start = time.perf_counter()
        engine = _engine(database, cell, model)
        outgoing.put({"kind": "ready", "pid": os.getpid(),
                      "startup_ms": (time.perf_counter() - start) * 1000,
                      "backend": type(engine.index).__name__, "embedding_dimension": engine.embedder.dim,
                      "embedding_semantic": engine.embedder.supports_semantic_search})
        while True:
            job = incoming.get()
            if job is None:
                break
            try:
                outcome = _operate(engine, job, cell)
            except Exception as exc:
                outcome = {"correct": False, "error_type": type(exc).__name__}
            outgoing.put({"kind": "result", "pid": os.getpid(),
                          "number": job["number"], "operation": job["kind"], **outcome})
    except Exception as exc:
        outgoing.put({"kind": "startup_error", "pid": os.getpid(),
                      "error_type": type(exc).__name__})
    finally:
        if engine is not None:
            engine.store.close()


def _tree_rss(pids: list[int]) -> Optional[int]:
    try:
        import psutil
    except ImportError:
        return None
    processes = {}
    for pid in [os.getpid(), *pids]:
        try:
            process = psutil.Process(pid)
            processes[pid] = process
            for child in process.children(recursive=True):
                processes[child.pid] = child
        except psutil.Error:
            continue
    total = 0
    for process in processes.values():
        try:
            total += process.memory_info().rss
        except psutil.Error:
            continue
    return total


def _repeat(cell: Cell, model: Optional[str]) -> dict:
    with tempfile.TemporaryDirectory(prefix="engraphis-capacity-") as scratch:
        database = str(Path(scratch) / "capacity.db")
        targets, seed_ms = _seed(database, cell, model)
        jobs = operation_plan(cell, targets)
        ctx = multiprocessing.get_context("spawn")
        incoming, outgoing = ctx.Queue(), ctx.Queue()
        workers = [ctx.Process(target=_worker, args=(database, cell, model, incoming, outgoing))
                   for _ in range(cell.concurrency)]
        ready, rows, submitted = [], [], {}
        peak, samples = None, 0
        stop = threading.Event()
        dispatcher = None
        started = time.perf_counter()
        status = "complete"
        try:
            for worker in workers:
                worker.start()
            while len(ready) < cell.concurrency:
                remaining = cell.timeout_s - (time.perf_counter() - started)
                if remaining <= 0:
                    raise TimeoutError("worker startup deadline")
                try:
                    item = outgoing.get(timeout=min(remaining, 1.0))
                except queue.Empty:
                    if any(worker.is_alive() for worker in workers):
                        continue
                    raise RuntimeError("workers exited before readiness")
                if item["kind"] != "ready":
                    raise RuntimeError("worker startup failed: " + item.get("error_type", "unknown"))
                ready.append(item)
            epoch = time.perf_counter()

            def dispatch():
                for job in jobs:
                    scheduled = epoch + (job["number"] / cell.arrival_rate if cell.arrival_rate else 0)
                    if stop.wait(max(0, scheduled - time.perf_counter())):
                        return
                    submitted[job["number"]] = (scheduled, time.perf_counter())
                    incoming.put(job)
                for _ in workers:
                    incoming.put(None)

            dispatcher = threading.Thread(target=dispatch, daemon=True)
            dispatcher.start()
            while len(rows) < len(jobs):
                if time.perf_counter() - epoch > cell.timeout_s:
                    status = "timeout"
                    break
                rss = _tree_rss([item["pid"] for item in ready])
                if rss is not None:
                    peak = max(peak or 0, rss)
                    samples += 1
                try:
                    item = outgoing.get(timeout=0.05)
                except queue.Empty:
                    if not any(worker.is_alive() for worker in workers):
                        status = "worker_exit"
                        break
                    continue
                if item["kind"] != "result":
                    status = "worker_error"
                    break
                received = time.perf_counter()
                scheduled, enqueued = submitted[item["number"]]
                wall = (received - scheduled) * 1000
                item.update({"wall_ms": wall, "dispatch_lag_ms": (enqueued - scheduled) * 1000,
                             "queue_ipc_ms": max(0, wall - item.get("operation_ms", 0)
                                                 - item.get("verification_ms", 0))})
                rows.append(item)
            elapsed = time.perf_counter() - epoch
        except (TimeoutError, queue.Empty, RuntimeError):
            status, elapsed = "startup_failed", time.perf_counter() - started
        finally:
            stop.set()
            if dispatcher is not None:
                dispatcher.join(timeout=1)
            for worker in workers:
                if worker.pid is not None:
                    worker.join(timeout=2)
                    if worker.is_alive():
                        worker.terminate()  # only this runner's disposable worker processes
                        worker.join(timeout=2)
            incoming.cancel_join_thread()
            incoming.close()
            outgoing.close()
        completed = {row["number"] for row in rows}
        for job in jobs:
            if job["number"] not in completed:
                rows.append({"number": job["number"], "operation": job["kind"],
                             "correct": False, "error_type": status})
        by_operation = {}
        for kind in sorted({job["kind"] for job in jobs}):
            selected = [row for row in rows if row["operation"] == kind]
            measured = [row["wall_ms"] for row in selected if "wall_ms" in row]
            by_operation[kind] = {"scheduled": len(selected), "measured": len(measured),
                                  "failures": sum(not row["correct"] for row in selected),
                                  "wall_latency_ms": _latency_ms(measured) if measured else None}
        return {"execution_id": uuid.uuid4().hex, "status": status, "seed_ms": seed_ms, "startup": ready,
                "elapsed_s": elapsed, "operations": sorted(rows, key=lambda row: row["number"]),
                "by_operation": by_operation,
                "received_operations_per_second": len(completed) / elapsed if elapsed else None,
                "operation_counts": dict(Counter(job["kind"] for job in jobs)),
                "observed_process_tree_peak_rss_bytes": peak, "memory_samples": samples,
                "disk": _disk(Path(database)), "input_sha256": hashlib.sha256(
                    canonical_json([{"number": j["number"], "kind": j["kind"],
                                     "target_index": j["target"]["index"]} for j in jobs]).encode()
                ).hexdigest()}


def run_cell(cell: Cell, *, model_dir: Optional[str] = None,
             model_sha256: Optional[str] = None) -> dict:
    cell.validate()
    identity = _local_model(model_dir, model_sha256)
    if not cell.smoke and not identity["semantic"]:
        raise ValueError("protocol cells require a pinned existing local semantic model")
    if not cell.smoke and importlib.util.find_spec("psutil") is None:
        raise ValueError("protocol cells require psutil process-tree memory sampling")
    before = _snapshot()
    repeats = [{**_repeat(cell, model_dir), "repeat_number": number}
               for number in range(cell.repeats)]
    after = _snapshot()
    try:
        model_stable = identity == _local_model(model_dir, model_sha256)
    except (OSError, ValueError):
        model_stable = False
    hardware = _hardware()
    if hardware.get("physical_ram_bytes") is None and importlib.util.find_spec("psutil"):
        import psutil

        hardware["physical_ram_bytes"] = psutil.virtual_memory().total
    dependencies = {}
    for distribution in ("sqlite-vec", "psutil"):
        try:
            dependencies[distribution] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            dependencies[distribution] = None
    ram = hardware.get("physical_ram_bytes")
    hardware_matches = (ram is not None and
                        abs(ram / (HARDWARE[cell.hardware] * 1024 ** 3) - 1) <= 0.125)
    rows = [row for repeat in repeats for row in repeat["operations"]]
    wall = [row["wall_ms"] for row in rows if "wall_ms" in row]
    return report_envelope(
        suite=SCHEMA, dataset_path=Path(__file__), config=asdict(cell),
        records=[{"question_id": f"r{r}-op{row['number']}", "category": row["operation"],
                  "qa_correct": row["correct"],
                  **({"latency_ms": row["wall_ms"]} if "wall_ms" in row else {})}
                 for r, repeat in enumerate(repeats) for row in repeat["operations"]],
        metrics={"repeats": repeats, "hardware": hardware, "runner_dependencies": dependencies,
                 "measurement_origin": "observed_local_engine", "measurement_version": 1,
                 "recall_diagnostics_enabled": True, "source_before": before,
                 "source_after": after, "source_stable": before == after, "model_stable": model_stable,
                 "wall_latency_ms": _latency_ms(wall) if wall else None,
                 "correctness_failures": sum(not row["correct"] for row in rows),
                 "target_capacity_verified": False, "primary_matrix_complete": False,
                 "hardware_matches_declared_target": hardware_matches,
                 "dataset_origin": "synthetic_generator", "independent_task_quality": False,
                 "measurement_boundary": "scheduled arrival to parent receipt; includes dispatch, "
                     "IPC, queue, engine call and canonical verification; excludes startup/seeding",
                 "memory_boundary": "sampled simultaneous RSS sum of runner and descendants during "
                     "operations; excludes seeding/startup; shared pages may be counted more than "
                     "once; not an allocation high-water mark",
                 "startup_boundary": "fresh process and connection with warm OS page cache",
                 "unmeasured": ["phase-level embedding/ranking/packing timings", "agent task success",
                                "production workload representativeness", "cold OS cache", "restore drills",
                                "full 48-cell paired matrix and confidence intervals"]},
        source_paths=[ROOT / name for name in before], models={"embedding": identity,
                    "vector_backend": {"identity": cell.backend}},
        token_accounting={"identity": "engraphis.regex.v1",
                          "revision": before["engraphis/core/context.py"], "scope": "packed context",
                          "method": "named regex counter; not provider billing"},
        command=(["python", "-m", "eval.engine_capacity", "--smoke", "--backend", cell.backend,
                  "--concurrency", str(cell.concurrency)] if cell.smoke else
                 ["python", "-m", "eval.engine_capacity", "--run-cell", "<saved-cell-config.json>",
                  "--model-dir", "<existing-local-model>", "--model-sha256", str(model_sha256)]),
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--smoke", action="store_true")
    mode.add_argument("--run-cell", type=Path, help="JSON Cell configuration; smoke must be false")
    parser.add_argument("--backend", choices=("numpy", "sqlite-vec"), default="numpy")
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--model-dir")
    parser.add_argument("--model-sha256")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if not args.smoke and args.run_cell is None:
        print(json.dumps(protocol(), indent=2))
        return 0
    cell = (Cell(**json.loads(args.run_cell.read_text(encoding="utf-8"))) if args.run_cell else
            Cell(backend=args.backend, concurrency=args.concurrency))
    if args.run_cell is not None and cell.smoke:
        raise ValueError("--run-cell requires smoke=false")
    report = run_cell(cell, model_dir=args.model_dir, model_sha256=args.model_sha256)
    if args.output:
        print(json.dumps(write_canonical_artifact(report, args.output)))
    else:
        print(json.dumps(report, indent=2))
    return int(bool(report["metrics"]["correctness_failures"])
               or not report["metrics"]["source_stable"] or not report["metrics"]["model_stable"])


if __name__ == "__main__":
    raise SystemExit(main())
