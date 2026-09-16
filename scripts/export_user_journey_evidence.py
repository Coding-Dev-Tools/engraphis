"""Run disposable user journeys and export a source-bound public evidence envelope."""
from __future__ import annotations

import argparse
from pathlib import Path

from eval.benchmark import report_envelope, sha256_file, write_canonical_artifact
from eval.user_journeys import AVAILABLE_JOURNEYS, run_journeys, verify_envelope


ROOT = Path(__file__).resolve().parents[1]


def export(output: Path) -> dict:
    sources = [*ROOT.joinpath("engraphis").rglob("*.py"),
               ROOT / "eval/user_journeys.py", ROOT / "eval/benchmark.py", Path(__file__)]
    before = {str(path): sha256_file(path) for path in sources}
    observed = run_journeys()
    if not verify_envelope(observed) or before != {str(path): sha256_file(path) for path in sources}:
        raise ValueError("journey evidence or producer source changed during execution")
    payload = observed["payload"]
    report = report_envelope(
        suite="Engraphis local user journeys", dataset_path=ROOT / "eval/user_journeys.py",
        config={"journeys": list(AVAILABLE_JOURNEYS), "store": "disposable_local",
                "evidence_kind": "functional_regression", "repetitions": 1},
        records=[{"question_id": row["journey_id"], "category": row["journey_id"],
                  "qa_correct": row["status"] == "passed", "latency_ms": row["duration_ms"]}
                 for row in payload["journeys"]],
        metrics={**payload, "status": "COMPLETE" if payload["failed"] == 0 else "BLOCKED",
                 "source_stable": True, "independent_acceptance_eligible": False,
                 "leadership_eligible": False},
        source_paths=sorted(sources),
        models={"embedding": {"identity": "deterministic hashing", "semantic": False}},
        token_accounting={"identity": "engraphis.regex.v1", "revision": None,
                          "scope": "journey context checks only", "method": "not provider billing"},
        command=["python", "-m", "scripts.export_user_journey_evidence", "--output", "<new-artifact>"],
    )
    write_canonical_artifact(report, output)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists() or args.output.with_suffix(args.output.suffix + ".sha256").exists():
        parser.error("existing evidence is immutable; choose a new path")
    report = export(args.output)
    print(f"{report['metrics']['passed']}/{report['metrics']['journey_count']} journeys passed")
    return int(report["metrics"]["failed"] != 0)


if __name__ == "__main__":
    raise SystemExit(main())
