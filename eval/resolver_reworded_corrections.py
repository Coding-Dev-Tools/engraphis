"""Reproducible eval for the reworded-correction resolver.

Drives ``engraphis.core.resolve.resolve()`` over a labeled JSONL corpus
and reports the number of true-positives superseded and the number of
false-positives (distinct facts the resolver incorrectly merged). The
corpus and this script together protect the quality claim made in
``CHANGELOG.md``; rerun with:

    python -m eval.resolver_reworded_corrections

The dataset ships at ``eval/datasets/resolver_reworded_corrections.jsonl``
and contains 36 positive (reworded-correction) pairs and 8 negative
(distinct-fact / env-conflict) pairs. Each row is::

    {"id", "neighbor", "candidate", "expected", "subject_hint"}

``expected`` is one of ``"invalidate"`` (the resolver should mark the
candidate as a correction of the neighbour) or ``"add"`` (the resolver
should add it as a distinct fact).

This is an offline-only evaluation: it does not require an embedder,
``engraphis-mcp``, or any external service. The resolver's only
configuration is its importable constants.
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


def _memory_record(text: str, record_id: str) -> MemoryRecord:
    return MemoryRecord(
        id=record_id, workspace_id="w", repo_id=None, session_id=None,
        title="", content=text, mtype="semantic", scope="workspace",
        importance=0.0, confidence=1.0, valid_from=0.0, valid_to=None,
        ingested_at=0.0, expired_at=None,
        subject_key="", claim_kind="", keywords=(), metadata={},
    )


def evaluate(dataset: Path = DATASET) -> dict[str, Any]:
    positives = 0
    positives_superseded = 0
    negatives = 0
    false_invalidations: list[dict[str, Any]] = []
    missed_corrections: list[dict[str, Any]] = []
    total = 0
    with dataset.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            total += 1
            expected = row["expected"]
            neighbor = _memory_record(row["neighbor"], f"mem_{row['id']}_n")
            # Use a high similarity so the resolver's strong/rewrite gates
            # are exercised for every row. The labeled ground truth tells
            # us whether the resolver should INVALIDATE or ADD.
            resolution = resolve(
                row["candidate"],
                [(0.9, neighbor)],
            )
            actual = resolution.op.value
            if expected == "invalidate":
                positives += 1
                if actual == "invalidate":
                    positives_superseded += 1
                else:
                    missed_corrections.append({
                        "id": row["id"],
                        "expected": "invalidate",
                        "actual": actual,
                        "reason": resolution.reason,
                        "subject_hint": row.get("subject_hint", ""),
                    })
            else:
                negatives += 1
                if actual == "invalidate":
                    false_invalidations.append({
                        "id": row["id"],
                        "expected": "add",
                        "actual": actual,
                        "reason": resolution.reason,
                        "subject_hint": row.get("subject_hint", ""),
                    })
    summary = {
        "dataset": str(dataset),
        "total": total,
        "positives": positives,
        "negatives": negatives,
        "positives_superseded": positives_superseded,
        "false_invalidations": len(false_invalidations),
        "missed_corrections": len(missed_corrections),
        "missed_correction_ids": [m["id"] for m in missed_corrections],
        "false_invalidation_ids": [f["id"] for f in false_invalidations],
    }
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--dataset",
        type=Path,
        default=DATASET,
        help="Path to the labeled JSONL corpus (default: %(default)s).",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero if any positive is missed or any negative is "
        "false-invalidated. The default is to report and exit 0 so this "
        "script can be run in CI as an audit log without flaking on "
        "regressions; use --strict to gate the build.",
    )
    args = parser.parse_args(argv)

    if not args.dataset.exists():
        print(f"error: dataset not found at {args.dataset}", file=sys.stderr)
        return 2

    summary = evaluate(args.dataset)
    positives = summary["positives"]
    superseded = summary["positives_superseded"]
    negatives = summary["negatives"]
    false_inv = summary["false_invalidations"]
    print(
        f"resolver reworded-correction eval: "
        f"{superseded}/{positives} positives superseded, "
        f"{false_inv}/{negatives} false invalidations, "
        f"{summary['total']} pairs total"
    )
    if summary["missed_correction_ids"]:
        print(f"  missed corrections:  {summary['missed_correction_ids']}")
    if summary["false_invalidation_ids"]:
        print(f"  false invalidations: {summary['false_invalidation_ids']}")
    if args.strict and (superseded < positives or false_inv > 0):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
