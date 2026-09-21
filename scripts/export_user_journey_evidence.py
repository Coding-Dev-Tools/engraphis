"""Run disposable user journeys and export a source-bound public evidence envelope."""
from __future__ import annotations

import argparse
from pathlib import Path

from eval.benchmark import report_envelope, sha256_file, write_canonical_artifact
from eval.user_journeys import AVAILABLE_JOURNEYS, run_journeys, verify_envelope


ROOT = Path(__file__).resolve().parents[1]


def _source_label(path: Path) -> str:
    """Return a stable public repo-relative label for one producer source."""
    root = ROOT.resolve()
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("journey producer source is outside the repository root") from exc
    label = relative.as_posix()
    if not label or label == "." or label.startswith("../"):
        raise ValueError("journey producer source has no stable relative label")
    return label


def _producer_sources() -> tuple[list[Path], list[str]]:
    paths = [
        *ROOT.joinpath("engraphis").rglob("*.py"),
        ROOT / "eval/user_journeys.py",
        ROOT / "eval/benchmark.py",
        Path(__file__),
    ]
    labeled = sorted(((_source_label(path), path) for path in paths), key=lambda item: item[0])
    labels = [label for label, _ in labeled]
    if len(labels) != len(set(labels)):
        raise ValueError("journey producer source labels are not unique")
    return [path for _, path in labeled], labels


def export(output: Path) -> dict:
    sources, labels = _producer_sources()
    before = {str(path): sha256_file(path) for path in sources}
    observed = run_journeys()
    after_sources, after_labels = _producer_sources()
    after = {str(path): sha256_file(path) for path in after_sources}
    if (not verify_envelope(observed) or labels != after_labels or before != after):
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
        source_paths=sources, source_names=labels,
        models={"embedding": {"identity": "deterministic hashing", "semantic": False}},
        token_accounting={"identity": "engraphis.regex.v1", "revision": None,
                          "scope": "journey context checks only", "method": "not provider billing"},
        command=["python", "-m", "scripts.export_user_journey_evidence", "--output", "<new-artifact>"],
    )
    expected_sources = list(zip(labels, (before[str(path)] for path in sources)))
    observed_sources = [
        (item.get("name"), item.get("sha256"))
        for item in report["suite"]["sources"]
    ]
    if (report["suite"]["sha256"] != before[str(ROOT / "eval/user_journeys.py")]
            or observed_sources != expected_sources):
        raise ValueError("journey artifact does not match the evaluated source snapshot")
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
