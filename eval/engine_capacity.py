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
import platform
import queue
import re
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
SQLITE_DURABILITY = "durable"
RESOURCE_PHASES = ("seeding", "startup", "workload", "teardown")
RSS_INTERVAL_S = 0.05
BACKLOG_INTERVAL_S = 1.0


def acceptance_policy() -> dict:
    """Existing release criteria, bound before execution rather than chosen from results."""
    return {
        "schema": "engraphis-capacity-acceptance/v1", "resource_observation_version": 2,
        "resource_phases": list(RESOURCE_PHASES), "rss_fraction_of_physical_ram_exclusive": 0.75,
        "sqlite_durability": {"policy": SQLITE_DURABILITY, "journal_mode": "wal", "synchronous": "FULL"},
        "recall_p95_ms": [
            {"hardware": "laptop16", "size": 100_000, "concurrency": 4, "maximum": 1000},
            {"hardware": "shared32", "size": 100_000, "concurrency": 16, "maximum": 2000},
        ],
        "latency_boundary": "queue-inclusive recall p95 in every repeat, both backends and workloads",
        "backlog": {"version": 1, "active_windows": 5, "minimum_active_seconds": 10,
                    "minimum_distinct_samples_per_window": 2,
                    "growth_rule": "every successive mean grows; net growth exceeds max(1, offered operations per second)"},
    }


def _identity_digest(value) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def validate_reference_hosts(value: dict) -> None:
    if (not isinstance(value, dict) or value.get("schema") != "engraphis-capacity-reference-hosts/v1"
            or value.get("policy_sha256") != _identity_digest(acceptance_policy())
            or not isinstance(value.get("hosts"), dict) or set(value["hosts"]) != set(HARDWARE)):
        raise ValueError("reference hosts must bind both declared profiles and the current acceptance policy")
    identities = []
    for host in value["hosts"].values():
        if (not isinstance(host, dict) or set(host) != {"host_identity_sha256", "hardware_sha256"}
                or any(not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{64}", item)
                       for item in host.values())):
            raise ValueError("reference host requires observed host and hardware SHA-256 identities")
        identities.append(host["host_identity_sha256"])
    if len(set(identities)) != len(HARDWARE):
        raise ValueError("reference profiles require distinct hosts")


def host_observation() -> dict:
    """Read-only host inventory; hashes keep the machine name out of public artifacts."""
    hardware = _hardware()
    if hardware.get("physical_ram_bytes") is None and importlib.util.find_spec("psutil"):
        import psutil

        hardware["physical_ram_bytes"] = psutil.virtual_memory().total
    node = platform.node().strip()
    return {"hardware": hardware, "hardware_sha256": _identity_digest(hardware),
            "host_identity_sha256": _identity_digest({"node": node,
                "system": platform.system(), "architecture": platform.machine()}) if node else None,
            "host_identity_boundary": "hash of locally observed hostname, OS and architecture; not attestation",
            "policy_sha256": _identity_digest(acceptance_policy())}


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
            "sqlite_durability": {"policy": SQLITE_DURABILITY, "journal_mode": "wal", "synchronous": "FULL"},
            "acceptance_policy": acceptance_policy(),
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
    paths.append(ROOT / "eval/capacity_matrix.py")
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
                                 sqlite_durability=SQLITE_DURABILITY,
                                 require_exact_backends=True, extractor="none", graph_extractor="none")
    if (engine.embedder.dim != cell.dimension
            or (model is not None and not engine.embedder.supports_semantic_search)):
        engine.close()
        raise ValueError("observed embedding dimension/capability differs from the declared cell")
    durability = engine.store.durability_health()
    if durability["synchronous"] != "FULL" or durability["journal_mode"] != "wal":
        engine.close()
        raise RuntimeError("capacity engine did not establish the declared WAL/FULL policy")
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
        engine.close()
    return targets, (time.perf_counter() - started) * 1000


def operation_plan(cell: Cell, targets: Optional[list[dict]] = None) -> list[dict]:
    """Stable schedules have exact ratios and never race erasure against a gold read."""
    import random

    kinds = (["recall"] * 100 if cell.workload == "read" else
             ["recall"] * 80 + ["remember"] * 15 + ["correct"] * 4 + ["erase"])
    kinds *= cell.operations // 100
    random.Random(cell.seed).shuffle(kinds)
    mutable_count = sum(kind in {"correct", "erase"} for kind in kinds)
    immutable_count = (len(targets) if targets is not None else cell.size) - mutable_count
    mutation = immutable_count
    operations = []
    for index, kind in enumerate(kinds):
        if kind in {"correct", "erase"}:
            target_index = mutation
            mutation += 1
        else:
            target_index = (index * 17 + cell.seed) % immutable_count
        target = targets[target_index] if targets is not None else {"index": target_index}
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
                      "sqlite_durability": engine.store.durability_health(),
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
            try:
                engine.close()
            except Exception as exc:
                outgoing.put({"kind": "teardown_error", "pid": os.getpid(),
                              "error_type": type(exc).__name__})


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
    total, observed = 0, 0
    for process in processes.values():
        try:
            total += process.memory_info().rss
            observed += 1
        except psutil.Error:
            continue
    return total if observed else None


class _LifecycleObserver:
    """Sample RSS independently of blocked seeding/startup/operation calls."""

    def __init__(self, cell: Cell, *, clock=None, rss_reader=None):
        self.cell = cell
        self.clock = clock or time.perf_counter
        self.rss_reader = rss_reader or _tree_rss
        self.origin = self.clock()
        self.lock = threading.Lock()
        self.sample_lock = threading.Lock()
        self.stop = threading.Event()
        self.thread = None
        self.phase = "seeding"
        self.pids = []
        self.phases = {phase: {
            "sampling_attempts": 0, "sample_count": 0, "unavailable_samples": 0,
            "observed_peak_rss_bytes": None, "first_sample_elapsed_s": None,
            "last_sample_elapsed_s": None, "max_sample_gap_s": None,
        } for phase in RESOURCE_PHASES}
        self.epoch = None
        self.load_end = None
        self.submitted = 0
        self.received = 0
        self.backlog = []
        self.sampling_started = False

    def start(self):
        self.sampling_started = True
        self.sample(force_backlog=True)
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self):
        while not self.stop.wait(RSS_INTERVAL_S):
            self.sample()

    def add_pid(self, pid):
        if pid is not None:
            with self.lock:
                self.pids.append(pid)

    def set_phase(self, phase):
        if phase not in RESOURCE_PHASES:
            raise ValueError("unknown resource observation phase")
        self.sample(force_backlog=True)
        with self.lock:
            self.phase = phase
        self.sample(force_backlog=True)

    def begin_load(self, epoch):
        with self.lock:
            self.epoch = epoch
        self.sample(force_backlog=True)

    def end_load(self):
        with self.lock:
            if self.epoch is not None and self.load_end is None:
                self.load_end = self.clock()
        self.sample(force_backlog=True)

    def record_submission(self):
        with self.lock:
            self.submitted += 1

    def record_receipt(self):
        with self.lock:
            self.received += 1

    def sample(self, *, force_backlog=False):
        # Serialize boundary/background collection so time-series order is stable.
        with self.sample_lock:
            now = self.clock()
            with self.lock:
                phase, pids = self.phase, list(self.pids)
            try:
                rss = self.rss_reader(pids)
            except Exception:
                rss = None
            with self.lock:
                row = self.phases[phase]
                row["sampling_attempts"] += 1
                elapsed = now - self.origin
                if row["last_sample_elapsed_s"] is not None:
                    gap = elapsed - row["last_sample_elapsed_s"]
                    row["max_sample_gap_s"] = max(row["max_sample_gap_s"] or 0, gap)
                if row["first_sample_elapsed_s"] is None:
                    row["first_sample_elapsed_s"] = elapsed
                row["last_sample_elapsed_s"] = elapsed
                if type(rss) is int and rss > 0:
                    row["sample_count"] += 1
                    row["observed_peak_rss_bytes"] = max(row["observed_peak_rss_bytes"] or 0, rss)
                else:
                    row["unavailable_samples"] += 1
                if self.epoch is None:
                    return
                # Queue counters and their timestamp are captured together, after
                # RSS collection, which may itself take appreciable time.
                queue_now = self.clock()
                load_elapsed = max(0, queue_now - self.epoch)
                if (not force_backlog and self.backlog
                        and load_elapsed - self.backlog[-1]["elapsed_s"] < BACKLOG_INTERVAL_S):
                    return
                offered_until = min(queue_now, self.load_end) if self.load_end is not None else queue_now
                due = (min(self.cell.operations, math.floor(
                    max(0, offered_until - self.epoch) * self.cell.arrival_rate) + 1)
                       if self.cell.arrival_rate else self.cell.operations)
                self.backlog.append({
                    "elapsed_s": round(load_elapsed, 6), "phase": self.phase,
                    "scheduled_due": due, "submitted": self.submitted, "received": self.received,
                    "scheduled_outstanding": max(0, due - self.received),
                    "dispatch_pending": max(0, due - self.submitted),
                    "submitted_unreceived": max(0, self.submitted - self.received),
                })

    def close(self):
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=2)
        self.sample(force_backlog=True)

    def report(self):
        with self.lock:
            phases = {key: dict(value) for key, value in self.phases.items()}
            peaks = [row["observed_peak_rss_bytes"] for row in phases.values()
                     if row["observed_peak_rss_bytes"] is not None]
            observed_until = (max(0, (self.load_end if self.load_end is not None else self.clock()) - self.epoch)
                              if self.epoch is not None else None)
            return {
                "observed_process_tree_peak_rss_bytes": max(peaks, default=None),
                "memory_samples": sum(row["sample_count"] for row in phases.values()),
                "resource_observations": {
                    "version": 2, "requested_sample_interval_s": RSS_INTERVAL_S,
                    "started_before_seeding": self.sampling_started,
                    "sampler_thread_stopped": self.thread is None or not self.thread.is_alive(),
                    "phases": phases,
                    "observation_limits": [
                        "sampled RSS peaks can miss transients between observations",
                        "shared resident pages may be counted in more than one process",
                        "exited or inaccessible descendants can be omitted from a sample",
                        "phase is captured at sample start; collection can cross a phase boundary",
                        "runner Python/model allocator retention contributes to later phase RSS",
                        "GPU memory and cold OS cache are not measured",
                    ],
                },
                "backlog_observations": {
                    "version": 1, "requested_sample_interval_s": BACKLOG_INTERVAL_S,
                    "offered_operations_per_second": self.cell.arrival_rate,
                    "offering_observed_until_s": observed_until,
                    "series": [dict(row) for row in self.backlog],
                    "boundary": "scheduled-due minus parent-received operations; includes dispatch, IPC, running work and verification",
                },
            }


def _backlog_assessment(cell: Cell, observations: dict, *, execution_complete: bool) -> dict:
    """A predeclared finite-window observation, never a capacity/stability proof."""
    result = {
        "available": False, "sustained_growth_observed": None,
        "offered_operations_per_second": cell.arrival_rate,
        "execution_complete": execution_complete, "general_capacity_proof": False,
        "rule": "five active-arrival windows with at least two samples each; every successive mean grows and net growth exceeds one second of offered arrivals",
    }
    if cell.arrival_rate <= 0:
        return {**result, "reason": "burst workload has no sustained offered rate"}
    until = observations.get("offering_observed_until_s")
    if until is None:
        return {**result, "reason": "offered-load execution never started"}
    duration = min(float(until), (cell.operations - 1) / cell.arrival_rate)
    # Forced boundary observations at the same timestamp add no independent
    # sampling coverage; use their last recorded counter snapshot.
    active = list({row["elapsed_s"]: row for row in observations["series"]
                   if 0 <= row["elapsed_s"] <= duration}.values())
    bins = [[] for _ in range(5)]
    if duration < 10:
        return {**result, "reason": "fewer than ten seconds of active offered-load observations"}
    for row in active:
        bins[min(4, int(row["elapsed_s"] / duration * 5))].append(row["scheduled_outstanding"])
    if any(len(window) < 2 for window in bins):
        return {**result, "reason": "insufficient sampling across the five arrival windows"}
    means = [sum(window) / len(window) for window in bins]
    mean_time = sum(row["elapsed_s"] for row in active) / len(active)
    mean_backlog = sum(row["scheduled_outstanding"] for row in active) / len(active)
    time_variance = sum((row["elapsed_s"] - mean_time) ** 2 for row in active)
    slope = (sum((row["elapsed_s"] - mean_time) * (row["scheduled_outstanding"] - mean_backlog)
                 for row in active) / time_variance if time_variance else None)
    growth = all(right > left for left, right in zip(means, means[1:]))
    growth = growth and means[-1] - means[0] > max(1.0, cell.arrival_rate)
    return {
        **result, "available": True, "sustained_growth_observed": growth,
        "assessment_duration_s": duration, "active_samples": len(active),
        "window_sample_counts": [len(window) for window in bins],
        "window_mean_outstanding": means, "observed_slope_operations_per_second": slope,
        "peak_scheduled_outstanding": max(row["scheduled_outstanding"] for row in active),
        "interpretation": "growth observed under this finite load" if growth else "no sustained growth observed in this finite load; stability is unproven",
    }


def _repeat(cell: Cell, model: Optional[str]) -> dict:
    observer = _LifecycleObserver(cell)
    observer.start()
    result = None
    try:
        with tempfile.TemporaryDirectory(prefix="engraphis-capacity-") as scratch:
            result = _repeat_database(str(Path(scratch) / "capacity.db"), cell, model, observer)
    except Exception as exc:
        if result is None:
            raise
        if result["status"] == "complete":
            result["status"] = "worker_error"
        result["lifecycle_errors"].append({"phase": "teardown", "error_type": type(exc).__name__})
    finally:
        # Keep observing worker/queue shutdown and temporary database cleanup.
        observer.close()
    observations = observer.report()
    result.update(observations)
    result["backlog_assessment"] = _backlog_assessment(
        cell, observations["backlog_observations"], execution_complete=result["status"] == "complete",
    )
    return result


def _repeat_database(database: str, cell: Cell, model: Optional[str], observer) -> dict:
    # An unseeded schedule still preserves the complete denominator on seed failure.
    jobs = operation_plan(cell)
    ready, rows, submitted, errors = [], [], {}, []
    workers, incoming, outgoing = [], None, None
    stop = threading.Event()
    dispatcher = None
    dispatch_errors = []
    started = time.perf_counter()
    seed_ms, epoch, status, phase = 0.0, None, "complete", "seeding"
    late_results = 0
    try:
        targets, seed_ms = _seed(database, cell, model)
        jobs = operation_plan(cell, targets)
        phase = "startup"
        observer.set_phase(phase)
        ctx = multiprocessing.get_context("spawn")
        incoming, outgoing = ctx.Queue(), ctx.Queue()
        workers = [ctx.Process(target=_worker, args=(database, cell, model, incoming, outgoing))
                   for _ in range(cell.concurrency)]
        startup_started = time.perf_counter()
        for worker in workers:
            worker.start()
            observer.add_pid(worker.pid)
        while len(ready) < cell.concurrency:
            remaining = cell.timeout_s - (time.perf_counter() - startup_started)
            if remaining <= 0:
                raise TimeoutError("worker startup deadline")
            try:
                item = outgoing.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                if any(worker.is_alive() for worker in workers):
                    continue
                raise RuntimeError("workers exited before readiness")
            if item["kind"] != "ready":
                errors.append({"phase": "startup", "error_type": item.get("error_type", "unknown")})
                raise RuntimeError("worker startup failed")
            ready.append(item)
        phase = "workload"
        observer.set_phase(phase)
        epoch = time.perf_counter()
        observer.begin_load(epoch)

        def dispatch():
            try:
                for job in jobs:
                    scheduled = epoch + (job["number"] / cell.arrival_rate if cell.arrival_rate else 0)
                    if stop.wait(max(0, scheduled - time.perf_counter())):
                        return
                    submitted[job["number"]] = (scheduled, time.perf_counter())
                    observer.record_submission()
                    incoming.put(job)
                for _ in workers:
                    incoming.put(None)
            except Exception as exc:
                dispatch_errors.append(type(exc).__name__)
                stop.set()

        dispatcher = threading.Thread(target=dispatch, daemon=True)
        dispatcher.start()
        seen = set()
        while len(rows) < len(jobs):
            if time.perf_counter() - epoch > cell.timeout_s:
                status = "timeout"
                break
            if dispatch_errors:
                status = "worker_error"
                break
            try:
                item = outgoing.get(timeout=0.05)
            except queue.Empty:
                if not any(worker.is_alive() for worker in workers):
                    status = "worker_exit"
                    break
                continue
            received = time.perf_counter()
            if received - epoch > cell.timeout_s:
                status = "timeout"
                late_results += int(item.get("kind") == "result")
                break
            if item["kind"] != "result" or item.get("number") not in submitted or item["number"] in seen:
                status = "worker_error"
                errors.append({"phase": "workload", "error_type": item.get("error_type", "UnexpectedResult")})
                break
            seen.add(item["number"])
            observer.record_receipt()
            scheduled, enqueued = submitted[item["number"]]
            wall = (received - scheduled) * 1000
            item.update({"wall_ms": wall, "dispatch_lag_ms": (enqueued - scheduled) * 1000,
                         "queue_ipc_ms": max(0, wall - item.get("operation_ms", 0)
                                             - item.get("verification_ms", 0))})
            rows.append(item)
        elapsed = time.perf_counter() - epoch
    except Exception as exc:
        status = "startup_failed" if phase in {"seeding", "startup"} else "worker_error"
        errors.append({"phase": phase, "error_type": type(exc).__name__})
        elapsed = time.perf_counter() - (epoch if epoch is not None else started)
        if phase == "seeding":
            seed_ms = (time.perf_counter() - started) * 1000
    finally:
        stop.set()
        observer.end_load()
        observer.set_phase("teardown")
        if dispatcher is not None:
            dispatcher.join(timeout=1)
            if dispatcher.is_alive():
                errors.append({"phase": "dispatch", "error_type": "DispatcherStillRunning"})
                if status == "complete":
                    status = "worker_error"
        for worker in workers:
            if worker.pid is not None:
                worker.join(timeout=2)
                if worker.is_alive():
                    worker.terminate()  # only this runner's disposable worker processes
                    worker.join(timeout=2)
                    errors.append({"phase": "teardown", "error_type": "WorkerTerminated"})
                    if status == "complete":
                        status = "worker_error"
                if worker.exitcode not in {0, None} and status == "complete":
                    status = "worker_error"
        if outgoing is not None:
            while True:
                try:
                    item = outgoing.get_nowait()
                except queue.Empty:
                    break
                except (OSError, ValueError) as exc:
                    errors.append({"phase": "teardown", "error_type": type(exc).__name__})
                    if status == "complete":
                        status = "worker_error"
                    break
                if item.get("kind") == "result":
                    late_results += 1
                elif item.get("kind") in {"teardown_error", "startup_error"}:
                    errors.append({"phase": "teardown", "error_type": item.get("error_type", "unknown")})
                    if status == "complete":
                        status = "worker_error"
        errors.extend({"phase": "dispatch", "error_type": error} for error in dispatch_errors)
        if incoming is not None:
            incoming.cancel_join_thread()
            incoming.close()
        if outgoing is not None:
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
            "by_operation": by_operation, "lifecycle_errors": errors,
            "late_result_count": late_results,
            "worker_exitcodes": [worker.exitcode for worker in workers if worker.pid is not None],
            "received_operations_per_second": len(completed) / elapsed if elapsed else None,
            "operation_counts": dict(Counter(job["kind"] for job in jobs)),
            "disk": _disk(Path(database)), "input_sha256": hashlib.sha256(
                canonical_json([{"number": job["number"], "kind": job["kind"],
                                 "target_index": job["target"]["index"]} for job in jobs]).encode()
            ).hexdigest()}


def run_cell(cell: Cell, *, model_dir: Optional[str] = None,
             model_sha256: Optional[str] = None, reference_hosts: Optional[dict] = None) -> dict:
    cell.validate()
    if reference_hosts is not None:
        validate_reference_hosts(reference_hosts)
    host_before = host_observation()
    identity = _local_model(model_dir, model_sha256)
    if not cell.smoke and not identity["semantic"]:
        raise ValueError("protocol cells require a pinned existing local semantic model")
    if not cell.smoke and importlib.util.find_spec("psutil") is None:
        raise ValueError("protocol cells require psutil process-tree memory sampling")
    if cell.backend == "sqlite-vec" and importlib.util.find_spec("sqlite_vec") is None:
        raise ModuleNotFoundError("explicit sqlite-vec backend requires installed sqlite_vec")
    before = _snapshot()
    repeats = [{**_repeat(cell, model_dir), "repeat_number": number}
               for number in range(cell.repeats)]
    after = _snapshot()
    try:
        model_stable = identity == _local_model(model_dir, model_sha256)
    except (OSError, ValueError):
        model_stable = False
    hardware = host_before["hardware"]
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
                 "resource_observation_version": 2,
                 "acceptance_policy": acceptance_policy(),
                 "acceptance_policy_sha256": _identity_digest(acceptance_policy()),
                 "reference_hosts_sha256": _identity_digest(reference_hosts) if reference_hosts is not None else None,
                 "host_identity_sha256": host_before["host_identity_sha256"],
                 "host_identity_boundary": host_before["host_identity_boundary"],
                 "host_identity_stable": host_before == host_observation(),
                 "sqlite_durability": {"policy": SQLITE_DURABILITY, "journal_mode": "wal", "synchronous": "FULL"},
                 "recall_diagnostics_enabled": True, "source_before": before,
                 "source_after": after, "source_stable": before == after, "model_stable": model_stable,
                 "wall_latency_ms": _latency_ms(wall) if wall else None,
                 "correctness_failures": sum(not row["correct"] for row in rows),
                 "target_capacity_verified": False, "primary_matrix_complete": False,
                 "hardware_matches_declared_target": hardware_matches,
                 "dataset_origin": "synthetic_generator", "independent_task_quality": False,
                 "measurement_boundary": "scheduled arrival to parent receipt; includes dispatch, "
                     "IPC, queue, engine call and canonical verification; excludes startup/seeding",
                 "memory_boundary": "sampled simultaneous RSS sum of runner and descendants from "
                     "before seeding through engine startup, workload and teardown; shared pages "
                     "may be counted more than once; not an allocation high-water mark",
                 "startup_boundary": "fresh process and connection with warm OS page cache",
                 "unmeasured": ["phase-level embedding/ranking/packing timings", "agent task success",
                                "production workload representativeness", "cold OS cache", "restore drills",
                                "unsampled transient allocation peaks", "seeding hard deadline",
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
    mode.add_argument("--host-identity", action="store_true", help="read-only reference host inventory")
    parser.add_argument("--backend", choices=("numpy", "sqlite-vec"), default="numpy")
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--model-dir")
    parser.add_argument("--model-sha256")
    parser.add_argument("--reference-hosts", type=Path, help="manifest frozen before the primary matrix")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.host_identity:
        print(json.dumps(host_observation(), indent=2))
        return 0
    if not args.smoke and args.run_cell is None:
        print(json.dumps(protocol(), indent=2))
        return 0
    cell = (Cell(**json.loads(args.run_cell.read_text(encoding="utf-8"))) if args.run_cell else
            Cell(backend=args.backend, concurrency=args.concurrency))
    if args.run_cell is not None and cell.smoke:
        raise ValueError("--run-cell requires smoke=false")
    references = json.loads(args.reference_hosts.read_text(encoding="utf-8")) if args.reference_hosts else None
    report = run_cell(cell, model_dir=args.model_dir, model_sha256=args.model_sha256, reference_hosts=references)
    if args.output:
        print(json.dumps(write_canonical_artifact(report, args.output)))
    else:
        print(json.dumps(report, indent=2))
    return int(bool(report["metrics"]["correctness_failures"])
               or not report["metrics"]["source_stable"] or not report["metrics"]["model_stable"]
               or any(repeat["status"] != "complete" for repeat in report["metrics"]["repeats"]))


if __name__ == "__main__":
    raise SystemExit(main())
