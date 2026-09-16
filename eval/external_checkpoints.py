"""Private per-case checkpoints for complete external retrieval diagnostics.

No model reader is called here. Source, model and configuration drift fail closed;
interrupted local-only cases can be explicitly restarted with their attempt retained.
"""
from __future__ import annotations

from collections import defaultdict
import json
import os
from pathlib import Path
import time
from typing import Callable, Optional

from eval.benchmark import canonical_json, sha256_file, sha256_text
from eval.harness import run


SCHEMA = "engraphis-external-checkpoints/v1"


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


def producer_snapshot() -> dict:
    root = Path(__file__).resolve().parents[1]
    paths = [path for folder in ("engraphis/core", "engraphis/backends")
             for path in (root / folder).rglob("*.py")]
    paths += [root / name for name in ("engraphis/factory.py", "eval/external.py", "eval/harness.py",
                                      "eval/metrics.py", "eval/benchmark.py", "eval/external_checkpoints.py")]
    return {path.relative_to(root).as_posix(): sha256_file(path) for path in sorted(paths)}


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
        result[name] = round(sum(row[name] for row in eligible) / max(len(eligible), 1), 6)
    categories: dict[str, list] = defaultdict(list)
    for row in rows:
        categories[str(row.get("category", "unknown"))].append(row)
    result["category_metrics"] = {
        category: {"questions": len(items), "retrieval_scored_questions": sum(bool(row.get("retrieval_scored")) for row in items),
                   **{name: (sum(row[name] for row in items if row.get("retrieval_scored")) /
                              max(sum(bool(row.get("retrieval_scored")) for row in items), 1))
                      for name in ("recall_at_k", "packed_recall_at_k")}}
        for category, items in categories.items()
    }
    result["case_wall_seconds"] = sum(report.get("case_wall_seconds", 0) for report in reports)
    result["query_latency_ms_sum"] = sum(row.get("latency_ms", 0) for row in rows)
    result["latency_boundary"] = "query latency excludes ingestion; case wall time includes ingestion and cleanup"
    return result


def run_resumable(cases: list[dict], *, directory: Path, binding: dict, embedder: object,
                  k: int = 10, token_budget: int = 1500, resolve_conflicts: bool = False,
                  restart_interrupted: bool = False, runner: Callable = run,
                  snapshot: Callable[[], dict] = producer_snapshot,
                  maximum_cases: Optional[int] = None) -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    identity = {"schema": SCHEMA, **binding, "producer": snapshot(), "k": k,
                "token_budget": token_budget, "resolve_conflicts": resolve_conflicts,
                "normalized_cases_sha256": sha256_text(canonical_json(cases))}
    header = directory / "manifest.json"
    if header.exists():
        if _read(header) != identity:
            raise ValueError("external checkpoint source/model/configuration drift")
    else:
        _write(header, identity)
    lock = directory / ".runner.lock"
    try:
        handle = lock.open("x", encoding="utf-8")
    except FileExistsError as exc:
        raise ValueError("external diagnostic runner already owns this directory") from exc
    reports, retries, executed = [], 0, 0
    try:
        with handle:
            handle.write(str(os.getpid()))
            handle.flush()
            os.fsync(handle.fileno())
        for ordinal, case in enumerate(cases):
            case_path = directory / f"case-{ordinal:05d}.json"
            case_hash = sha256_text(canonical_json(case))
            if case_path.exists():
                checkpoint = _read(case_path)
                if (checkpoint.get("case_sha256") != case_hash
                        or checkpoint.get("report_sha256") != sha256_text(canonical_json(checkpoint["report"]))):
                    raise ValueError("external case checkpoint content changed")
                reports.append(checkpoint["report"])
                continue
            start_path = directory / f"case-{ordinal:05d}.started"
            if start_path.exists():
                if not restart_interrupted:
                    raise ValueError("interrupted local case; use explicit restart flag after inspecting retained attempt")
                retries += 1
                retry_path = directory / f"case-{ordinal:05d}.retry-{len(list(directory.glob(f'case-{ordinal:05d}.retry-*'))):03d}"
                _write(retry_path, {"reason": "explicit local-only interrupted-case restart", "case_sha256": case_hash})
            else:
                _write(start_path, {"case_sha256": case_hash, "ordinal": ordinal})
            started = time.perf_counter()
            report = runner([case], k=k, token_budget=token_budget, embedder=embedder,
                            resolve_conflicts=resolve_conflicts)
            report["case_wall_seconds"] = time.perf_counter() - started
            if snapshot() != identity["producer"]:
                raise ValueError("external producer changed during a case")
            expected_ids = [str(question.get("id") or f"{case['id']}:{i}")
                            for i, question in enumerate(case["questions"])]
            observed_ids = [row["question_id"] for row in report["detail"]]
            if observed_ids != expected_ids:
                raise ValueError("external case did not retain exact question coverage")
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
        result["explicit_local_restarts"] = retries
        return result
    finally:
        lock.unlink(missing_ok=True)
