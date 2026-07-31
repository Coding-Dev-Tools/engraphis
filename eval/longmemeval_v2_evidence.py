"""Convert official LongMemEval-V2 output into a redacted evidence artifact.

The official harness writes rich per-question logs containing prompts, gold
answers, and reader output. Those files remain private run material. This
module extracts only scores, timings, token counts, stable IDs, and digests,
then uses :mod:`eval.benchmark` to make an immutable public artifact.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import re
from typing import Any, Optional, Sequence

from eval.benchmark import report_envelope, write_canonical_artifact
from eval.run_longmemeval_v2 import PINNED_READER_MODEL, PINNED_READER_REVISION


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
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


def _finite_number(value: Any, label: str) -> float:
    """Validate an official numeric field before it enters public evidence."""
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


def _normalized_record(row: dict[str, Any], *, expected_tokenizer: str) -> dict[str, Any]:
    """Project one private official-harness row into a public-safe row."""
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
    source_ids = metadata.get("source_ids")
    source_ids = [str(item) for item in source_ids] if isinstance(source_ids, list) else []
    context_tokens = _nonnegative_integer(
        row.get("memory_context_token_count"), "memory_context_token_count"
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
        raise ValueError("official per-question memory_query_duration_seconds must be non-negative")
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
        "usage": {
            "memory_context_tokens": context_tokens,
            "token_counter": tokenizer,
        },
    }
    optional_usage = (
        ("memory_context_original_tokens", row.get("memory_context_original_token_count")),
        ("reader_prompt_tokens", reader_usage.get("prompt_tokens")),
        ("reader_completion_tokens", reader_usage.get("completion_tokens")),
        ("adapter_reported_context_tokens", adapter_usage.get("context_tokens")),
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
            "unknown_rate": sum(bool(record["abstained"]) for record in records) / len(records),
        },
        "memory_context": {
            "mean_final_tokens": sum(record["context_tokens"] for record in records) / len(records),
            "mean_query_latency_ms": sum(record["latency_ms"] for record in records) / len(records),
        },
    }


def build_evidence_report(
    *,
    per_question_path: str | Path,
    questions_path: str | Path,
    haystack_path: str | Path,
    trajectories_path: str | Path,
    memory_config_path: str | Path,
    reader_model: str = PINNED_READER_MODEL,
    reader_revision: str = PINNED_READER_REVISION,
    evaluator_model: Optional[str] = None,
    evaluator_revision: Optional[str] = None,
    command: Optional[Sequence[str]] = None,
) -> dict[str, Any]:
    """Build a public-safe artifact from one completed official V2 run.

    This reports official QA scores but deliberately does not mark the result
    as ``canonical``. The canonical Engraphis contract also requires a complete
    five-budget retrieval curve, which an individual official reader run does
    not produce.
    """
    per_question = Path(per_question_path)
    if re.fullmatch(r"[0-9a-f]{40}", reader_revision) is None:
        raise ValueError("reader_revision must be an immutable lowercase 40-character commit")
    if bool(evaluator_model) != bool(evaluator_revision):
        raise ValueError("evaluator_model and evaluator_revision must be used together")
    if evaluator_revision and re.fullmatch(r"[0-9a-f]{40}", evaluator_revision) is None:
        raise ValueError("evaluator_revision must be an immutable lowercase 40-character commit")
    source_paths = [
        per_question,
        Path(haystack_path),
        Path(trajectories_path),
        Path(memory_config_path),
    ]
    private_rows = _load_jsonl(per_question)
    tokenizer_identity = f"{reader_model}@{reader_revision}"
    records = [
        _normalized_record(row, expected_tokenizer=tokenizer_identity)
        for row in private_rows
    ]
    return report_envelope(
        suite="LongMemEval-V2",
        dataset_path=questions_path,
        source_paths=source_paths,
        config={
            "official_harness": "LongMemEval-V2",
            "reader_model": reader_model,
            "reader_revision": reader_revision,
            "evaluator_model": evaluator_model,
            "evaluator_revision": evaluator_revision,
            "per_question_schema": "official_harness/per_question.jsonl",
        },
        command=command or ("python", "-m", "eval.run_longmemeval_v2", "<official_args_redacted>"),
        token_accounting={
            "identity": tokenizer_identity,
            "revision": reader_revision,
            "scope": "official_harness_memory_context_item_content_excluding_prompt_framing",
            "method": "official_harness_count_memory_context_tokens",
        },
        models={
            "reader": {"model_id": reader_model, "revision": reader_revision},
            "evaluator": {
                "model_id": evaluator_model or "not_recorded",
                "revision": evaluator_revision,
            },
        },
        records=records,
        metrics=_qa_metrics(records),
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Redact official LongMemEval-V2 output into an immutable Engraphis evidence artifact."
    )
    parser.add_argument("--per-question", required=True)
    parser.add_argument("--questions", required=True)
    parser.add_argument("--haystack", required=True)
    parser.add_argument("--trajectories", required=True)
    parser.add_argument("--memory-config", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--reader-model", default=PINNED_READER_MODEL)
    parser.add_argument("--reader-revision", default=PINNED_READER_REVISION)
    parser.add_argument("--evaluator-model", default=None)
    parser.add_argument("--evaluator-revision", default=None)
    args = parser.parse_args(argv)
    try:
        report = build_evidence_report(
            per_question_path=args.per_question,
            questions_path=args.questions,
            haystack_path=args.haystack,
            trajectories_path=args.trajectories,
            memory_config_path=args.memory_config,
            reader_model=args.reader_model,
            reader_revision=args.reader_revision,
            evaluator_model=args.evaluator_model,
            evaluator_revision=args.evaluator_revision,
        )
        result = write_canonical_artifact(report, args.output)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
