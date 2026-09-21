"""Private per-case checkpoints for complete external retrieval diagnostics.

No model reader is called here. Source, model and configuration drift fail closed;
interrupted local-only cases can be explicitly restarted with their attempt retained.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
import json
import math
import os
import re
from pathlib import Path
import stat
import sys
import time
from typing import Callable, Optional

try:  # pragma: no cover - the Windows branch is exercised on release hosts.
    import fcntl  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

from engraphis.core.interfaces import embedding_space_fingerprint
from eval.benchmark import canonical_json, environment_provenance, sha256_file, sha256_text
from eval.harness import run


SCHEMA = "engraphis-external-checkpoints/v2"
RUNNER_LOCK_MARKER = b"engraphis-external-runner-lock/v1\n"


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(canonical_json(payload) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _read(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("checkpoint must contain an object")
    return value


@contextmanager
def _runner_lock(path: Path):
    """Hold an OS lock whose release is automatic if the process dies.

    The fixed marker is initialized once under the lock; a prefix left by an
    interrupted first write can be completed without truncating the file.
    Existing empty files, legacy PID markers and other unrecognized bytes
    require manual inspection.
    Cooperating runners must keep the lock file in place, even after exiting.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        try:
            handle = path.open("x+b")
            created = True
        except FileExistsError:
            handle = path.open("r+b")
    except OSError as exc:
        raise ValueError("external diagnostic runner lock is unsafe or unavailable") from exc
    acquired = False
    try:
        handle.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                if fcntl is None:
                    raise OSError("POSIX file locking is unavailable")
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            raise ValueError("external diagnostic runner already owns this directory") from exc
        acquired = True

        opened, named = os.fstat(handle.fileno()), path.lstat()
        if (not stat.S_ISREG(named.st_mode) or opened.st_nlink != 1
                or not os.path.samestat(opened, named)):
            raise ValueError("external diagnostic runner lock is unsafe or changed")
        handle.seek(0)
        raw = handle.read(len(RUNNER_LOCK_MARKER) + 1)
        if (not raw and not created) or not RUNNER_LOCK_MARKER.startswith(raw):
            raise ValueError("legacy or unrecognized external runner lock requires manual inspection")
        if raw != RUNNER_LOCK_MARKER:
            handle.write(RUNNER_LOCK_MARKER[len(raw):])
            handle.flush()
            os.fsync(handle.fileno())
        yield
    finally:
        active_error = sys.exc_info()[1]
        try:
            try:
                if acquired:
                    handle.seek(0)
                    if sys.platform == "win32":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    elif fcntl is not None:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
        except OSError:
            if active_error is None:
                raise


def producer_snapshot() -> dict:
    root = Path(__file__).resolve().parents[1]
    paths = [path for folder in ("engraphis/core", "engraphis/backends")
             for path in (root / folder).rglob("*.py")]
    paths += [root / name for name in ("engraphis/factory.py", "eval/external.py", "eval/harness.py",
                                      "eval/metrics.py", "eval/benchmark.py", "eval/external_checkpoints.py")]
    return {path.relative_to(root).as_posix(): sha256_file(path) for path in sorted(paths)}


def _runtime_identity(embedder: object) -> dict:
    fingerprint = embedding_space_fingerprint(embedder)
    if not fingerprint:
        raise ValueError("external checkpoints require a durable embedder fingerprint")
    return {"embedder": fingerprint, "environment": environment_provenance()}


def _validate_case_report(report: object, case: dict) -> None:
    rows = report.get("detail") if isinstance(report, dict) else None
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("external case did not retain exact question coverage")
    expected = [str(question.get("id") or f"{case['id']}:{i}")
                for i, question in enumerate(case["questions"])]
    if [row.get("question_id") for row in rows] != expected:
        raise ValueError("external case did not retain exact question coverage")
    for row, question in zip(rows, case["questions"]):
        answer = question.get("answer_variants") or question.get("answer") or question.get("evidence") or ""
        supporting = question.get("supporting", [])
        if (row.get("retrieval_scored") is not bool(supporting)
                or row.get("answer_scored") is not (question.get("answerable") is not False and bool(answer))
                or row.get("category") != str(question.get("category") or "unknown")):
            raise ValueError("external case has invalid scored detail")
        for name in ("recall_at_k", "hit_at_k", "mrr_at_k", "ndcg_at_k", "packed_recall_at_k",
                     "packed_hit_at_k", "packed_mrr_at_k", "packed_ndcg_at_k",
                     "answer_token_recall", "packed_answer_token_recall"):
            value = row.get(name)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or not 0 <= value <= 1):
                raise ValueError("external case has invalid scored detail")


def aggregate(reports: list[dict]) -> dict:
    rows = [row for report in reports for row in report["detail"]]
    retrieval = [row for row in rows if row.get("retrieval_scored")]
    answers = [row for row in rows if row.get("answer_scored")]
    result = {"questions": len(rows), "scored_questions": len(retrieval),
              "answer_scored_questions": len(answers), "detail": rows,
              "exclusions": [row["excluded"] for row in rows if row.get("excluded")]}
    rank_names = ("recall_at_k", "hit_at_k", "mrr_at_k", "ndcg_at_k", "packed_recall_at_k",
                  "packed_hit_at_k", "packed_mrr_at_k", "packed_ndcg_at_k")
    for name in rank_names + ("answer_token_recall", "packed_answer_token_recall"):
        eligible = answers if "answer_token" in name else retrieval
        result[name] = round(sum(row[name] for row in eligible) / len(eligible), 6) if eligible else None
    categories: dict[str, list] = defaultdict(list)
    for row in rows:
        categories[str(row.get("category", "unknown"))].append(row)
    result["category_metrics"] = {}
    for category, items in categories.items():
        scored = [row for row in items if row.get("retrieval_scored")]
        result["category_metrics"][category] = {
            "questions": len(items), "retrieval_scored_questions": len(scored),
            **{name: sum(row[name] for row in scored) / len(scored) if scored else None
               for name in ("recall_at_k", "packed_recall_at_k")},
        }
    result["case_wall_seconds"] = sum(report.get("case_wall_seconds", 0) for report in reports)
    result["query_latency_ms_sum"] = sum(row.get("latency_ms", 0) for row in rows)
    result["latency_boundary"] = "query latency excludes ingestion; case wall time includes ingestion and cleanup"
    return result


def _validate_start_receipt(path: Path, ordinal: int, case_hash: str) -> None:
    try:
        receipt = _read(path)
    except (OSError, ValueError) as exc:
        raise ValueError("invalid external case start receipt") from exc
    if receipt != {"case_sha256": case_hash, "ordinal": ordinal} or type(receipt.get("ordinal")) is not int:
        raise ValueError("external case start receipt binding changed")


def _retained_restart_counts(directory: Path, case_hashes: list[str]) -> list[int]:
    attempts: dict[int, list[int]] = defaultdict(list)
    for path in directory.glob("*.retry-*"):
        match = re.fullmatch(r"case-(\d+)[.]retry-(\d+)", path.name)
        if match is None:
            raise ValueError("invalid external restart receipt name")
        ordinal, attempt = map(int, match.groups())
        if ordinal >= len(case_hashes) or path.name != f"case-{ordinal:05d}.retry-{attempt:03d}":
            raise ValueError("external restart receipt case binding changed")
        try:
            receipt = _read(path)
        except (OSError, ValueError) as exc:
            raise ValueError("invalid external restart receipt") from exc
        if receipt != {"reason": "explicit local-only interrupted-case restart", "case_sha256": case_hashes[ordinal]}:
            raise ValueError("external restart receipt content changed")
        attempts[ordinal].append(attempt)
    counts = [0] * len(case_hashes)
    for ordinal, indices in attempts.items():
        if sorted(indices) != list(range(len(indices))):
            raise ValueError("external restart receipt sequence changed")
        _validate_start_receipt(directory / f"case-{ordinal:05d}.started", ordinal, case_hashes[ordinal])
        counts[ordinal] = len(indices)
    return counts


def run_resumable(cases: list[dict], *, directory: Path, binding: dict, embedder: object,
                  k: int = 10, token_budget: int = 1500, resolve_conflicts: bool = False,
                  restart_interrupted: bool = False, runner: Callable = run,
                  snapshot: Callable[[], dict] = producer_snapshot,
                  maximum_cases: Optional[int] = None) -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    lock = directory / ".runner.lock"
    with _runner_lock(lock):
        identity = {"schema": SCHEMA, **binding, "producer": snapshot(), "k": k,
                    "runtime": _runtime_identity(embedder),
                    "token_budget": token_budget, "resolve_conflicts": resolve_conflicts,
                    "normalized_cases_sha256": sha256_text(canonical_json(cases))}
        header = directory / "manifest.json"
        if header.exists():
            if _read(header) != identity:
                raise ValueError("external checkpoint source/model/configuration drift")
        else:
            _write(header, identity)
        reports, executed = [], 0
        case_hashes = [sha256_text(canonical_json(case)) for case in cases]
        retries = _retained_restart_counts(directory, case_hashes)
        for ordinal, case in enumerate(cases):
            case_path = directory / f"case-{ordinal:05d}.json"
            case_hash = case_hashes[ordinal]
            start_path = directory / f"case-{ordinal:05d}.started"
            if start_path.exists():
                _validate_start_receipt(start_path, ordinal, case_hash)
            if case_path.exists():
                checkpoint = _read(case_path)
                cached_report = checkpoint.get("report")
                _validate_case_report(cached_report, case)
                if (checkpoint.get("case_sha256") != case_hash
                        or checkpoint.get("report_sha256") != sha256_text(canonical_json(cached_report))):
                    raise ValueError("external case checkpoint content changed")
                reports.append(cached_report)
                continue
            if start_path.exists():
                if not restart_interrupted:
                    raise ValueError("interrupted local case; use explicit restart flag after inspecting retained attempt")
                retry_path = directory / f"case-{ordinal:05d}.retry-{retries[ordinal]:03d}"
                _write(retry_path, {"reason": "explicit local-only interrupted-case restart", "case_sha256": case_hash})
                retries[ordinal] += 1
            else:
                _write(start_path, {"case_sha256": case_hash, "ordinal": ordinal})
            started = time.perf_counter()
            report = runner([case], k=k, token_budget=token_budget, embedder=embedder,
                            resolve_conflicts=resolve_conflicts)
            _validate_case_report(report, case)
            report["case_wall_seconds"] = time.perf_counter() - started
            if snapshot() != identity["producer"] or _runtime_identity(embedder) != identity["runtime"]:
                raise ValueError("external producer or runtime changed during a case")
            _write(case_path, {"case_sha256": case_hash, "report": report,
                               "report_sha256": sha256_text(canonical_json(report))})
            reports.append(report)
            executed += 1
            print(f"external checkpoints: {len(reports)}/{len(cases)} cases complete", flush=True)
            if maximum_cases is not None and executed >= maximum_cases:
                break
        result = aggregate(reports)
        result["checkpoint_status"] = "COMPLETE" if len(reports) == len(cases) else "PARTIAL"
        result["completed_cases"] = len(reports)
        result["expected_cases"] = len(cases)
        if _retained_restart_counts(directory, case_hashes) != retries:
            raise ValueError("external restart receipts changed during aggregation")
        result["explicit_local_restarts"] = sum(retries)
        if snapshot() != identity["producer"] or _runtime_identity(embedder) != identity["runtime"]:
            raise ValueError("external producer or runtime changed during checkpoint aggregation")
        return result
