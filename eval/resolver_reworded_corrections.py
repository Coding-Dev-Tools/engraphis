"""Reproducible eval for the reworded-correction resolver.

Drives ``engraphis.core.resolve.resolve()`` over a labeled JSONL corpus
and reports the number of true-positives superseded and the number of
false-positives (distinct facts the resolver incorrectly merged). The
corpus and this script together protect the quality claim made in
``CHANGELOG.md``; rerun with:

    python -m eval.resolver_reworded_corrections

The dataset ships at ``eval/datasets/resolver_reworded_corrections.jsonl``
and contains 38 positive (reworded-correction) pairs and 6 negative
(distinct-fact / env-conflict) pairs. Each row is::

    {"id", "neighbor", "candidate", "expected", "subject_hint"}

``expected`` is one of ``"invalidate"`` (the resolver should mark the
candidate as a correction of the neighbour) or ``"add"`` (the resolver
should add it as a distinct fact).

The default is a resolver-unit fixture with injected similarity. ``--end-to-end``
instead measures candidate discovery and both writes through the production
engine with its deterministic offline embedder; it does not inject similarity.
Neither mode calls an external service. Distinct-fact negatives must survive
both false invalidation and false NOOP decisions.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from engraphis.core.interfaces import MemoryRecord
from engraphis.core.resolve import resolve

DATASET = Path(__file__).resolve().parent / "datasets" / "resolver_reworded_corrections.jsonl"


def _memory_record(text: str, record_id: str, *, title: str = "") -> MemoryRecord:
    return MemoryRecord(
        id=record_id, workspace_id="w", repo_id=None, session_id=None,
        title=title, content=text, mtype="semantic", scope="workspace",
        importance=0.0, confidence=1.0, valid_from=0.0, valid_to=None,
        ingested_at=0.0, expired_at=None,
        subject_key="", claim_kind="", keywords=(), metadata={},
    )


def _write_pair(row: dict, engine) -> tuple[str, str, bool, bool]:
    """Return real write outcome and survival without replacing candidate lookup."""
    workspace_id = engine.store.get_or_create_workspace("resolver-acceptance")
    repo_id = engine.store.get_or_create_repo(workspace_id, str(row["id"]))
    shared = {"workspace_id": workspace_id, "repo_id": repo_id}
    shared.update({key: row[key] for key in ("subject_key", "claim_kind") if key in row})
    before = engine.remember_with_resolution(
        row["neighbor"], title=row.get("neighbor_title", ""), **shared,
    )
    after = engine.remember_with_resolution(
        row["candidate"], title=row.get("candidate_title", ""), **shared,
    )
    old_record = engine.store.get_memory(before["id"])
    new_record = engine.store.get_memory(after["id"])
    old_survives = bool(old_record and old_record.valid_to is None)
    new_survives = bool(
        new_record and new_record.content == row["candidate"]
        and after["id"] != before["id"] and new_record.valid_to is None
    )
    return str(after["op"]), str(after.get("reason", "")), old_survives, new_survives


def evaluate(dataset: Path = DATASET, *, end_to_end: bool = False) -> dict[str, Any]:
    positives = 0
    positives_superseded = 0
    negatives = 0
    false_invalidations: list[dict[str, Any]] = []
    false_noops: list[dict[str, Any]] = []
    lost_distinct_facts: list[str] = []
    missed_corrections: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    total = 0
    seen_ids: set[str] = set()
    with dataset.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            case_id = row.get("id")
            if not isinstance(case_id, str) or not case_id or case_id in seen_ids:
                raise ValueError("resolver cases require distinct non-empty string ids")
            seen_ids.add(case_id)
            total += 1
            expected = row["expected"]
            neighbor = _memory_record(
                row["neighbor"], f"mem_{row['id']}_n", title=row.get("neighbor_title", ""),
            )
            # Use a high similarity so the resolver's strong/rewrite gates
            # are exercised for every row. The labeled ground truth tells
            # us whether the resolver should INVALIDATE or ADD.
            if expected not in {"invalidate", "add"}:
                raise ValueError("expected must be invalidate or add")
            old_survives = new_survives = True
            if end_to_end:
                from engraphis.core.engine import MemoryEngine

                engine = MemoryEngine.create(":memory:")
                try:
                    actual, reason, old_survives, new_survives = _write_pair(row, engine)
                finally:
                    engine.store.close()
            else:
                candidate = row["candidate"]
                if row.get("candidate_title"):
                    candidate = f"{row['candidate_title']}\n{candidate}"
                resolution = resolve(
                    candidate,
                    [(0.9, neighbor)],
                    candidate_content=row["candidate"],
                )
                actual, reason = resolution.op.value, resolution.reason
            if expected == "invalidate":
                positives += 1
                if actual == "invalidate":
                    positives_superseded += 1
                else:
                    missed_corrections.append({
                        "id": row["id"],
                        "expected": "invalidate",
                        "actual": actual,
                        "reason": reason,
                        "subject_hint": row.get("subject_hint", ""),
                    })
            else:
                negatives += 1
                if actual == "invalidate":
                    false_invalidations.append({
                        "id": row["id"],
                        "expected": "add",
                        "actual": actual,
                        "reason": reason,
                        "subject_hint": row.get("subject_hint", ""),
                    })
                if actual == "noop":
                    false_noops.append({"id": row["id"], "reason": reason})
                if not old_survives or not new_survives:
                    lost_distinct_facts.append(row["id"])
            record = {
                "question_id": row["id"], "expected_operation": expected,
                "actual_operation": actual,
            }
            if end_to_end:
                record.update({"old_fact_survives": old_survives,
                               "new_fact_survives": new_survives})
            records.append(record)
    summary = {
        "dataset": str(dataset),
        "total": total,
        "positives": positives,
        "negatives": negatives,
        "positives_superseded": positives_superseded,
        "false_invalidations": len(false_invalidations),
        "false_noops": len(false_noops),
        "false_noop_ids": [item["id"] for item in false_noops],
        "lost_distinct_facts": len(lost_distinct_facts),
        "lost_distinct_fact_ids": lost_distinct_facts,
        "missed_corrections": len(missed_corrections),
        "missed_correction_ids": [m["id"] for m in missed_corrections],
        "false_invalidation_ids": [f["id"] for f in false_invalidations],
        "execution": "production_write_path" if end_to_end else "resolver_unit",
        "similarity_injected": not end_to_end,
        "correction_recall": positives_superseded / positives if positives else None,
        "correction_precision": (
            positives_superseded / (positives_superseded + len(false_invalidations))
            if positives_superseded + len(false_invalidations) else None
        ),
        "distinct_fact_error_rate": (
            len(set([f["id"] for f in false_invalidations]
                    + [f["id"] for f in false_noops] + lost_distinct_facts)) / negatives
            if negatives else None
        ),
        "records": records,
    }
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--end-to-end", action="store_true",
        help="Exercise both writes and candidate discovery with the offline production engine.",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit the existing provenance/redaction envelope with aggregate measurements.",
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=DATASET,
        help="Path to the labeled JSONL corpus (default: %(default)s).",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="(Deprecated, now the default.) Exit non-zero if any positive is "
        "missed or any negative is false-invalidated. The default mode is "
        "strict so the eval can be run in CI as an audit log without flaking "
        "on regressions.",
    )
    parser.add_argument(
        "--audit-only",
        action="store_true",
        help="Report and exit 0 even on labeled regressions. Use this only "
        "for ad-hoc inspection where the eval is the audit log; CI must "
        "not pass --audit-only.",
    )
    args = parser.parse_args(argv)

    if not args.dataset.exists():
        print(f"error: dataset not found at {args.dataset}", file=sys.stderr)
        return 2

    summary = evaluate(args.dataset, end_to_end=args.end_to_end)
    positives = summary["positives"]
    superseded = summary["positives_superseded"]
    negatives = summary["negatives"]
    false_inv = summary["false_invalidations"]
    failed = bool(
        superseded < positives or false_inv > 0 or summary["false_noops"] > 0
        or summary["lost_distinct_facts"] > 0
    )
    if args.json:
        from eval.benchmark import report_envelope

        root = Path(__file__).resolve().parents[1]
        try:
            public_dataset = args.dataset.resolve().relative_to(root).as_posix()
        except ValueError:
            # External paths may identify an owner's private directory. The
            # basename plus suite digest identifies the input without that path.
            public_dataset = args.dataset.name
        command = ["python", "-m", "eval.resolver_reworded_corrections",
                   "--dataset", public_dataset, "--json"]
        if args.end_to_end:
            command.append("--end-to-end")
        if args.audit_only:
            command.append("--audit-only")
        sources = [Path(__file__), root / "engraphis/core/resolve.py"]
        if args.end_to_end:
            sources.extend(root / path for path in (
                "engraphis/factory.py", "engraphis/core/engine.py",
                "engraphis/core/store.py", "engraphis/core/schema.py",
                "engraphis/core/interfaces.py", "engraphis/core/vector_search.py",
                "engraphis/core/vector_repair.py", "engraphis/backends/vector_numpy.py",
                "engraphis/backends/embedder_deterministic.py",
            ))
        report = report_envelope(
            suite="resolver-write-acceptance" if args.end_to_end else "resolver-unit",
            dataset_path=args.dataset,
            config={"end_to_end": args.end_to_end, "offline": True,
                    "evidence_scope": "authored regression corpus; not an independent quality benchmark"},
            records=summary["records"],
            metrics={key: value for key, value in summary.items()
                     if key not in {"dataset", "records"}},
            source_paths=sources,
            command=command,
        )
        print(json.dumps(report, sort_keys=True))
        return 0 if args.audit_only or not failed else 1
    print(
        f"resolver reworded-correction eval: "
        f"{superseded}/{positives} positives superseded, "
        f"{false_inv}/{negatives} false invalidations, "
        f"{summary['false_noops']}/{negatives} false NOOPs, "
        f"{summary['total']} pairs total"
    )
    if summary["missed_correction_ids"]:
        print(f"  missed corrections:  {summary['missed_correction_ids']}")
    if summary["false_invalidation_ids"]:
        print(f"  false invalidations: {summary['false_invalidation_ids']}")
    if summary["false_noop_ids"]:
        print(f"  false NOOPs: {summary['false_noop_ids']}")
    if summary["lost_distinct_fact_ids"]:
        print(f"  lost distinct facts: {summary['lost_distinct_fact_ids']}")
    # Default: strict — labeled regressions fail the run. CI must invoke
    # this script with no flags so the build gates on labeled quality.
    if args.audit_only:
        return 0
    if failed:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
