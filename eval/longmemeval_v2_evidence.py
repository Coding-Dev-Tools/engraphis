"""Convert an attested official LongMemEval-V2 run into redacted evidence.

Official per-question logs contain prompts, gold answers, reader output, and retrieved
context. Those remain private. This module exports only audited controls, aggregate
measurements, stable source IDs, and whole-file provenance digests.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Optional, Sequence

from eval.benchmark import report_envelope, write_canonical_artifact
from eval.run_longmemeval_v2 import PINNED_READER_MODEL, PINNED_READER_REVISION


_EXECUTION_MANIFEST_SCHEMA = "engraphis-longmemeval-v2-execution/v1"
_MEMORY_TYPES = frozenset({"working", "episodic", "semantic", "procedural"})
_CLEAN_DIRTY_STATE_SHA256 = hashlib.sha256(b"").hexdigest()


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"per-question record {line_number} must be an object")
        question_id = value.get("question_id")
        if not isinstance(question_id, str) or not question_id:
            raise ValueError(f"per-question record {line_number} has no question_id")
        records.append(value)
    if not records:
        raise ValueError("per-question output contains no records")
    if len({record["question_id"] for record in records}) != len(records):
        raise ValueError("per-question output has duplicate question_id values")
    return records


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_question_ids(path: Path) -> list[str]:
    value = json.loads(path.read_text(encoding="utf-8"))
    rows = value if isinstance(value, list) else (
        value.get("questions") if isinstance(value, dict) else None
    )
    if not isinstance(rows, list) or not rows:
        raise ValueError("questions source must contain a non-empty question list")
    question_ids = []
    for number, row in enumerate(rows, start=1):
        question_id = row.get("question_id") if isinstance(row, dict) else None
        if not isinstance(question_id, str) or not question_id:
            raise ValueError(f"source question {number} has no question_id")
        question_ids.append(question_id)
    if len(set(question_ids)) != len(question_ids):
        raise ValueError("questions source has duplicate question_id values")
    return question_ids


def _finite_number(value: Any, label: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
    ):
        raise ValueError(f"official per-question {label} must be a finite number")
    return float(value)


def _nonnegative_integer(value: Any, label: str) -> int:
    number = _finite_number(value, label)
    if number < 0 or not number.is_integer():
        raise ValueError(f"official per-question {label} must be a non-negative integer")
    return int(number)


def _required_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"official per-question {label} must be boolean")
    return value


def _count_map(value: Any, label: str) -> dict[str, int]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a memory-type count object")
    result: dict[str, int] = {}
    for key, count in value.items():
        if key not in _MEMORY_TYPES:
            raise ValueError(f"{label} contains an unknown memory type")
        result[key] = _nonnegative_integer(count, f"{label}.{key}")
    return dict(sorted(result.items()))


def _verify_execution_manifest(
    path: Path,
    *,
    per_question: Path,
    questions: Path,
    haystack: Path,
    trajectories: Path,
    memory_config: Path,
    matrix_manifest: Path,
    upstream_revision: str,
    seed: int,
    source_questions: int,
    output_rows: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("execution manifest must be an object")
    environment = manifest.get("environment")
    if (
        not isinstance(environment, dict)
        or any(
            not isinstance(environment.get(field), str) or not environment[field]
            for field in ("python", "implementation", "platform", "machine")
        )
        or not isinstance(environment.get("packages"), dict)
    ):
        raise ValueError(
            "execution manifest must record the official run environment"
        )
    delegated_argv = manifest.get("delegated_argv")
    if (
        not isinstance(delegated_argv, list)
        or any(not isinstance(value, str) for value in delegated_argv)
    ):
        raise ValueError("execution manifest must record delegated_argv")
    delegated_argv_sha256 = hashlib.sha256(
        json.dumps(delegated_argv, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if manifest.get("delegated_argv_sha256") != delegated_argv_sha256:
        raise ValueError("execution manifest delegated_argv_sha256 does not match")
    expected = {
        "schema": _EXECUTION_MANIFEST_SCHEMA,
        "status": "complete",
        "upstream_revision": upstream_revision,
        "seed": seed,
        "questions_sha256": _sha256_file(questions),
        "haystack_sha256": _sha256_file(haystack),
        "trajectories_sha256": _sha256_file(trajectories),
        "memory_config_sha256": _sha256_file(memory_config),
        "matrix_manifest_sha256": _sha256_file(matrix_manifest),
        "per_question_sha256": _sha256_file(per_question),
        "source_question_count": source_questions,
        "output_row_count": output_rows,
    }
    for field, expected_value in expected.items():
        if manifest.get(field) != expected_value:
            raise ValueError(f"execution manifest {field} does not match the completed run")
    checkout = manifest.get("official_checkout")
    if (
        not isinstance(checkout, dict)
        or checkout.get("revision") != upstream_revision
        or checkout.get("dirty") is not False
        or checkout.get("dirty_state_sha256") != _CLEAN_DIRTY_STATE_SHA256
    ):
        raise ValueError("execution manifest does not attest a clean official checkout")
    binding = {
        "verified": True,
        "schema": _EXECUTION_MANIFEST_SCHEMA,
        "manifest_sha256": _sha256_file(path),
        "status": "complete",
        "source_questions": source_questions,
        "output_rows": output_rows,
        "upstream_revision": upstream_revision,
        "clean_checkout": True,
        "delegated_argv_sha256": delegated_argv_sha256,
    }
    return binding, dict(environment)


def _normalized_record(
    row: dict[str, Any],
    *,
    expected_tokenizer: str,
    retrieval_profile: str,
    planning: str,
    mtype_limits: dict[str, int],
    token_budget: int,
) -> dict[str, Any]:
    """Project one private official row after verifying its executed controls."""
    metadata = row.get("memory_post_query_metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    adapter_usage = metadata.get("usage")
    adapter_usage = adapter_usage if isinstance(adapter_usage, dict) else {}
    reader_usage = row.get("usage")
    reader_usage = reader_usage if isinstance(reader_usage, dict) else {}
    tokenizer = metadata.get("tokenizer")
    if tokenizer != expected_tokenizer:
        raise ValueError(
            "official per-question metadata does not prove the pinned reader tokenizer: "
            f"expected {expected_tokenizer!r}, found {tokenizer!r}"
        )
    if metadata.get("token_budget_method") != "pinned_reader_content_tokenizer":
        raise ValueError("official per-question metadata does not prove exact token accounting")
    if metadata.get("retrieval_profile") != retrieval_profile:
        raise ValueError(
            "official per-question retrieval_profile does not match the matrix cell"
        )
    if metadata.get("planning") != planning:
        raise ValueError("official per-question planning does not match the matrix cell")
    if metadata.get("mtype_limits") != mtype_limits:
        raise ValueError("official per-question mtype_limits do not match the matrix cell")
    source_ids_value = metadata.get("source_ids")
    if not isinstance(source_ids_value, list) or any(
        not isinstance(item, str) or not item for item in source_ids_value
    ):
        raise ValueError("official per-question source_ids must be a string array")
    source_ids = list(source_ids_value)
    if len(source_ids) != len(set(source_ids)):
        raise ValueError("official per-question source_ids must be unique")
    context_tokens = _nonnegative_integer(
        row.get("memory_context_token_count"), "memory_context_token_count"
    )
    returned_context_tokens = _nonnegative_integer(
        metadata.get("returned_context_tokens"), "returned_context_tokens"
    )
    if context_tokens > token_budget or returned_context_tokens > token_budget:
        raise ValueError("official per-question context tokens exceed the matrix token budget")
    adapter_context_tokens = adapter_usage.get("context_tokens")
    if adapter_context_tokens is not None and (
        _nonnegative_integer(adapter_context_tokens, "adapter usage.context_tokens")
        > token_budget
    ):
        raise ValueError(
            "official per-question adapter context tokens exceed the matrix token budget"
        )
    inserted_counts = _count_map(
        metadata.get("inserted_memory_type_counts"), "inserted_memory_type_counts"
    )
    retrieved_counts = _count_map(
        metadata.get("retrieved_memory_type_counts"), "retrieved_memory_type_counts"
    )
    for memory_type, limit in mtype_limits.items():
        if retrieved_counts.get(memory_type, 0) > limit:
            raise ValueError(
                "official per-question retrieved memory-type count exceeds "
                f"mtype_limits[{memory_type!r}]"
            )
    if not any(count > 0 for count in inserted_counts.values()):
        raise ValueError(
            "official per-question inserted memory-type counts must be non-empty"
        )
    if sum(retrieved_counts.values()) != len(source_ids):
        raise ValueError(
            "official per-question retrieved memory-type counts must match source_ids"
        )
    is_abstention = _required_bool(
        row.get("is_abstention_problem"), "is_abstention_problem"
    )
    is_unknown = _required_bool(row.get("is_unknown"), "is_unknown")
    score = _finite_number(row.get("score"), "score")
    score_bool = _required_bool(row.get("score_bool"), "score_bool")
    latency_seconds = _finite_number(
        row.get("memory_query_duration_seconds"), "memory_query_duration_seconds"
    )
    if latency_seconds < 0:
        raise ValueError(
            "official per-question memory_query_duration_seconds must be non-negative"
        )
    raw = {
        "question_id": row["question_id"],
        "category": str(row.get("category") or "unknown"),
        "question_text": row.get("question_text", ""),
        "answer_gold": row.get("answer_gold", ""),
        "response_raw": row.get("response_raw", ""),
        "response_parsed_boxed": row.get("response_parsed_boxed", ""),
        "memory_context": row.get("memory_context", []),
        "prompt_messages": row.get("prompt_messages", []),
        "retrieved_ids": source_ids,
        "supporting_ids": [],
        "answerable": not is_abstention,
        "abstained": is_unknown,
        "qa_score": score,
        "qa_correct": score_bool,
        "latency_ms": round(latency_seconds * 1000, 6),
        "context_tokens": context_tokens,
        "context_token_method": "official_harness_reader_memory_context_tokens",
        "context_tokenizer_identity": tokenizer,
        "inserted_memory_type_counts": inserted_counts,
        "retrieved_memory_type_counts": retrieved_counts,
        "usage": {
            "memory_context_tokens": context_tokens,
            "token_counter": tokenizer,
        },
    }
    optional_usage = (
        ("memory_context_original_tokens", row.get("memory_context_original_token_count")),
        ("reader_prompt_tokens", reader_usage.get("prompt_tokens")),
        ("reader_completion_tokens", reader_usage.get("completion_tokens")),
        ("adapter_reported_context_tokens", adapter_context_tokens),
    )
    for key, value in optional_usage:
        if value is not None:
            raw["usage"][key] = _nonnegative_integer(value, key)
    return raw


def _qa_metrics(records: Sequence[dict[str, Any]]) -> dict[str, Any]:
    scores = [float(record["qa_score"]) for record in records]
    abstentions = [record for record in records if not record["answerable"]]
    answered = [record for record in records if record["answerable"]]
    return {
        "official_qa": {
            "available": True,
            "metric": "official_harness_score",
            "mean_score": sum(scores) / len(scores),
            "n": len(records),
            "n_answerable": len(answered),
            "n_abstention": len(abstentions),
            "unknown_rate": (
                sum(bool(record["abstained"]) for record in records) / len(records)
            ),
        },
        "context_measurements": {
            "mean_final_tokens": (
                sum(record["context_tokens"] for record in records) / len(records)
            ),
            "mean_query_latency_ms": (
                sum(record["latency_ms"] for record in records) / len(records)
            ),
        },
    }


def build_evidence_report(
    *,
    per_question_path: str | Path,
    questions_path: str | Path,
    haystack_path: str | Path,
    trajectories_path: str | Path,
    memory_config_path: str | Path,
    execution_manifest_path: str | Path,
    upstream_revision: str,
    matrix_manifest_path: str | Path,
    ablation: str,
    token_budget: int,
    seed: int,
    reader_model: str = PINNED_READER_MODEL,
    reader_revision: str = PINNED_READER_REVISION,
    evaluator_model: Optional[str] = None,
    evaluator_revision: Optional[str] = None,
    command: Optional[Sequence[str]] = None,
) -> dict[str, Any]:
    """Build public evidence only from a complete, attested official V2 run."""
    per_question = Path(per_question_path)
    questions_file = Path(questions_path)
    haystack_file = Path(haystack_path)
    trajectories_file = Path(trajectories_path)
    memory_config_file = Path(memory_config_path)
    manifest_file = Path(matrix_manifest_path)
    execution_manifest_file = Path(execution_manifest_path)
    if re.fullmatch(r"[0-9a-f]{40}", reader_revision) is None:
        raise ValueError(
            "reader_revision must be an immutable lowercase 40-character commit"
        )
    if re.fullmatch(r"[0-9a-f]{40}", upstream_revision) is None:
        raise ValueError(
            "upstream_revision must be an immutable lowercase 40-character commit"
        )
    if bool(evaluator_model) != bool(evaluator_revision):
        raise ValueError("evaluator_model and evaluator_revision must be used together")
    if evaluator_revision and re.fullmatch(r"[0-9a-f]{40}", evaluator_revision) is None:
        raise ValueError(
            "evaluator_revision must be an immutable lowercase 40-character commit"
        )
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("seed must be a non-negative integer")
    if (
        isinstance(token_budget, bool)
        or not isinstance(token_budget, int)
        or token_budget <= 0
    ):
        raise ValueError("token_budget must be a positive integer")
    if not isinstance(ablation, str) or not ablation:
        raise ValueError("ablation must be a non-empty string")

    memory_config_bytes = memory_config_file.read_bytes()
    memory_config = json.loads(memory_config_bytes)
    if not isinstance(memory_config, dict):
        raise ValueError("memory config must be an object")
    memory_params = memory_config.get("memory_params")
    if not isinstance(memory_params, dict):
        raise ValueError("memory config must contain memory_params")
    if (
        memory_params.get("reader_tokenizer_model") != reader_model
        or memory_params.get("reader_tokenizer_revision") != reader_revision
    ):
        raise ValueError("memory config does not match the pinned reader")
    if memory_params.get("max_context_tokens") != token_budget:
        raise ValueError("memory config token budget does not match the matrix cell")
    retrieval_profile = memory_params.get("retrieval_profile")
    if not isinstance(retrieval_profile, str) or not retrieval_profile:
        raise ValueError("memory config must declare retrieval_profile")
    planning = memory_params.get("planning", "off")
    if planning not in {"off", "auto"}:
        raise ValueError("memory config planning must be off or auto")
    expected_limits = _count_map(
        memory_params.get("mtype_limits", {}), "memory config mtype_limits"
    )
    config_sha256 = hashlib.sha256(memory_config_bytes).hexdigest()

    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or not isinstance(manifest.get("runs"), list):
        raise ValueError("matrix manifest must contain a runs list")
    if (
        manifest.get("reader_model") != reader_model
        or manifest.get("reader_revision") != reader_revision
    ):
        raise ValueError("matrix manifest does not match the pinned reader")
    matches = [
        row
        for row in manifest["runs"]
        if isinstance(row, dict)
        and row.get("ablation") == ablation
        and row.get("token_budget") == token_budget
    ]
    if len(matches) != 1 or matches[0].get("sha256") != config_sha256:
        raise ValueError(
            "memory config does not match the requested matrix manifest cell"
        )
    matrix_binding = {
        "verified": True,
        "manifest_name": str(manifest.get("name") or ""),
        "manifest_sha256": _sha256_file(manifest_file),
        "ablation": ablation,
        "token_budget": token_budget,
        "config_sha256": config_sha256,
    }

    private_rows = _load_jsonl(per_question)
    expected_question_ids = _source_question_ids(questions_file)
    output_ids = [row["question_id"] for row in private_rows]
    if (
        set(output_ids) != set(expected_question_ids)
        or len(output_ids) != len(expected_question_ids)
    ):
        missing = len(set(expected_question_ids) - set(output_ids))
        unknown = len(set(output_ids) - set(expected_question_ids))
        raise ValueError(
            "per-question output must cover the source question IDs exactly "
            f"(missing={missing}, unknown={unknown})"
        )
    execution_binding, execution_environment = _verify_execution_manifest(
        execution_manifest_file,
        per_question=per_question,
        questions=questions_file,
        haystack=haystack_file,
        trajectories=trajectories_file,
        memory_config=memory_config_file,
        matrix_manifest=manifest_file,
        upstream_revision=upstream_revision,
        seed=seed,
        source_questions=len(expected_question_ids),
        output_rows=len(private_rows),
    )
    tokenizer_identity = f"{reader_model}@{reader_revision}"
    records = [
        _normalized_record(
            row,
            expected_tokenizer=tokenizer_identity,
            retrieval_profile=retrieval_profile,
            planning=planning,
            mtype_limits=expected_limits,
            token_budget=token_budget,
        )
        for row in private_rows
    ]
    if expected_limits and len({
        mtype
        for record in records
        for mtype, count in record["inserted_memory_type_counts"].items()
        if count > 0
    }) < 2:
        raise ValueError(
            "memory-type cap evidence requires at least two populated memory types"
        )

    report = report_envelope(
        suite="LongMemEval-V2",
        dataset_path=questions_file,
        source_paths=[
            per_question,
            haystack_file,
            trajectories_file,
            memory_config_file,
            manifest_file,
            execution_manifest_file,
        ],
        config={
            "measurement_scope": "end_to_end",
            "claim_boundary": (
                "Official reader QA plus observed Engraphis retrieval context under "
                "the bound matrix cell"
            ),
            "official_harness": "LongMemEval-V2",
            "reader_model": reader_model,
            "reader_revision": reader_revision,
            "evaluator_model": evaluator_model,
            "evaluator_revision": evaluator_revision,
            "upstream_revision": upstream_revision,
            "seed": seed,
            "matrix_binding": matrix_binding,
            "execution_binding": execution_binding,
            "memory_config": {
                "sha256": config_sha256,
                "memory_type": memory_config.get("memory_type"),
                "planning": planning,
                "mtype_limits": expected_limits,
                "max_context_tokens": token_budget,
                "embed_model": memory_params.get("embed_model"),
                "embed_revision": memory_params.get("embed_revision"),
                "vector_backend": memory_params.get("vector_backend"),
            },
            "per_question_schema": "official_harness/per_question.jsonl",
        },
        command=command
        or ("python", "-m", "eval.run_longmemeval_v2", "<official_args_redacted>"),
        token_accounting={
            "identity": tokenizer_identity,
            "revision": reader_revision,
            "scope": "official_harness_memory_context_item_content_excluding_prompt_framing",
            "method": "official_harness_count_memory_context_tokens",
        },
        models={
            "reader": {"model_id": reader_model, "revision": reader_revision},
            "embedder": {
                "model_id": memory_params.get("embed_model") or "not_recorded",
                "revision": memory_params.get("embed_revision"),
            },
            "evaluator": {
                "model_id": evaluator_model or "not_recorded",
                "revision": evaluator_revision,
            },
        },
        records=records,
        metrics=_qa_metrics(records),
    )
    report["environment"] = execution_environment
    report["protocol"]["source_questions"] = len(expected_question_ids)
    return report


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Redact attested LongMemEval-V2 output into immutable Engraphis evidence."
        )
    )
    parser.add_argument("--per-question", required=True)
    parser.add_argument("--questions", required=True)
    parser.add_argument("--haystack", required=True)
    parser.add_argument("--trajectories", required=True)
    parser.add_argument("--memory-config", required=True)
    parser.add_argument("--execution-manifest", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--reader-model", default=PINNED_READER_MODEL)
    parser.add_argument("--reader-revision", default=PINNED_READER_REVISION)
    parser.add_argument("--evaluator-model", default=None)
    parser.add_argument("--evaluator-revision", default=None)
    parser.add_argument("--upstream-revision", required=True)
    parser.add_argument("--matrix-manifest", required=True)
    parser.add_argument("--ablation", required=True)
    parser.add_argument("--token-budget", required=True, type=int)
    parser.add_argument("--seed", required=True, type=int)
    args = parser.parse_args(argv)
    try:
        report = build_evidence_report(
            per_question_path=args.per_question,
            questions_path=args.questions,
            haystack_path=args.haystack,
            trajectories_path=args.trajectories,
            memory_config_path=args.memory_config,
            execution_manifest_path=args.execution_manifest,
            reader_model=args.reader_model,
            reader_revision=args.reader_revision,
            evaluator_model=args.evaluator_model,
            evaluator_revision=args.evaluator_revision,
            upstream_revision=args.upstream_revision,
            matrix_manifest_path=args.matrix_manifest,
            ablation=args.ablation,
            token_budget=args.token_budget,
            seed=args.seed,
        )
        result = write_canonical_artifact(report, args.output)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
