"""Rerun the three public offline fixtures into a new immutable evidence artifact.

This exports aggregate fixture evidence only. It makes no capacity, provider-cost,
MCP-transport or competitive coding-task claim. Existing artifacts are never replaced.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def source_manifest() -> dict[str, str]:
    paths = {path for folder in ("engraphis", "eval")
             for path in (ROOT / folder).rglob("*.py")}
    paths.update(ROOT / path for path in (
        "eval/datasets/longdoc.jsonl", "eval/datasets/codemem.jsonl",
        "scripts/export_offline_evidence.py",
    ))
    return {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(paths)}


def generate() -> dict:
    from eval.chunking_eval import compare, load
    from eval.grounded import run as grounded_run
    from eval.harness import load_dataset
    from eval.performance import run as run_performance

    before = source_manifest()
    documents = load(str(ROOT / "eval/datasets/longdoc.jsonl"))
    chunking = compare(documents, k=5, embed_model=None)
    performance = run_performance(load_dataset(str(ROOT / "eval/datasets/codemem.jsonl")),
                                  k=5, iterations=10)
    grounded = grounded_run()
    if before != source_manifest():
        raise RuntimeError("source changed during fixture execution")
    chunked = {"documents": len(documents), "questions": chunking["reports"]["whole"]["questions"],
               "k": 5, "token_counter": chunking["reports"]["chunked"]["token_counter"],
               "context_reduction_pct": chunking["context_reduction_pct"]}
    for mode in ("whole", "chunked"):
        result = chunking["reports"][mode]
        chunked[mode] = {key: result[key] for key in (
            "recall_at_k", "mean_context_tokens", "mean_evidence_tokens", "max_stored_tokens")}
        chunked[mode]["memories"] = result["memories_stored"]
    measured = {**performance["quality"], "k": 5, "token_budget": 1500,
                "dataset_cases": performance["corpus"]["dataset_cases"],
                "memories": performance["corpus"]["memories"],
                "questions": performance["corpus"]["questions"],
                "timed_recalls": performance["run"]["timed_recalls"]}
    context = performance["context"]
    for target, source in (("mean_context_tokens", "mean_tokens"),
                           ("max_context_tokens", "max_tokens")):
        measured[target] = context[source]
    measured.update({key: context[key] for key in (
        "token_counter", "full_serialized_payload_tokens", "compact_serialized_payload_tokens",
        "saved_serialized_payload_tokens", "serialized_payload_savings_ratio")})
    grounded_result = {target: grounded[source] for target, source in (
        ("answerable", "n_answerable"), ("grounded", "grounded_hits"),
        ("off_topic", "n_unanswerable"), ("quarantined", "n_quarantine"),
        ("abstained", "abstain_hits"), ("quarantine_hits", "quarantine_hits"),
        ("decision_accuracy", "accuracy"))}
    runs = []
    for name, command, boundary, result in (
        ("chunking", "python -m eval.chunking_eval --dataset eval/datasets/longdoc.jsonl --k 5",
         "Deterministic offline retrieval fixture; normalized-character token estimator; not external QA or provider billing.", chunked),
        ("performance", "python -m eval.performance --dataset eval/datasets/codemem.jsonl --k 5 --iterations 10 --json",
         "Deterministic offline CodeMem fixture; serialized JSON-shape payload proxies, not MCP transport responses, provider billing, or latency claims.", measured),
        ("grounded", "python -m eval.grounded",
         "Deterministic offline support/abstention fixture; not a frontier-model answer-quality score.", grounded_result),
    ):
        runs.append({"id": "offline-" + name, "command": command, "boundary": boundary,
                     "config_digest": hashlib.sha256(command.encode()).hexdigest(),
                     "config_digest_method": "sha256(UTF-8 exact command)", "result": result})
    return {
        "schema": "engraphis-public-offline-fixtures/v1",
        "generated_on": datetime.now(timezone.utc).date().isoformat(),
        "privacy": {name: False for name in ("contains_answers", "contains_customer_data",
                    "contains_per_record_fingerprints", "contains_prompts", "contains_raw_questions")},
        "environment": {"python": platform.python_version(), "platform": sys.platform,
                        "numpy": importlib.metadata.version("numpy"),
                        "embedding": "deterministic", "vector_backend": "numpy"},
        "suite": {"files": before,
                  "digest": hashlib.sha256(json.dumps(before, sort_keys=True,
                                           separators=(",", ":")).encode()).hexdigest(),
                  "digest_method": "sha256(canonical compact JSON mapping each sorted path to its file SHA-256)"},
        "runs": runs,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    sidecar = args.output.with_suffix(args.output.suffix + ".sha256")
    if args.output.exists() or sidecar.exists():
        parser.error("choose a new artifact path; existing evidence is immutable")
    report = generate()
    payload = (json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
    digest = hashlib.sha256(payload).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("xb") as output:
        output.write(payload)
    with sidecar.open("xb") as checksum:
        checksum.write(f"{digest}  {args.output.name}\n".encode("ascii"))
    print(json.dumps({"artifact": str(args.output), "sha256": digest}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
