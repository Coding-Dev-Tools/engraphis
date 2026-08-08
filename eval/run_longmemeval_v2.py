"""Run the pinned official LongMemEval-V2 harness and attest completed output.

The upstream harness uses an import-time memory registry and does not discover
third-party backends. This entry point registers Engraphis, strips its optional
receipt arguments, then delegates the remaining command line unchanged.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import runpy
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional, Sequence

from eval.benchmark import environment_provenance


PINNED_LONGMEMEVAL_V2_REVISION = "6f020ac2fc3275e46c706d3406e02c3ed79b7be2"
PINNED_READER_MODEL = "Qwen/Qwen3.5-9B"
PINNED_READER_REVISION = "c202236235762e1c871ad0ccb60c8ee5ba337b9a"
EXECUTION_MANIFEST_SCHEMA = "engraphis-longmemeval-v2-execution/v1"


def verify_official_checkout(memory_module: object) -> dict[str, object]:
    """Require and attest the exact clean upstream revision before delegation."""
    module_path = getattr(memory_module, "__file__", None)
    if not isinstance(module_path, str) or not module_path:
        raise SystemExit("LongMemEval-V2 memory module has no verifiable source path.")
    try:
        root = subprocess.check_output(
            ["git", "-C", str(Path(module_path).resolve().parent), "rev-parse", "--show-toplevel"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        revision = subprocess.check_output(
            ["git", "-C", root, "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        dirty_state = subprocess.check_output(
            ["git", "-C", root, "status", "--porcelain=v1", "--untracked-files=all"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(
            "LongMemEval-V2 must be an exact pinned Git checkout; could not verify its revision."
        ) from exc
    if revision != PINNED_LONGMEMEVAL_V2_REVISION:
        raise SystemExit(
            "LongMemEval-V2 checkout revision mismatch: expected "
            f"{PINNED_LONGMEMEVAL_V2_REVISION}, found {revision or 'unknown'}."
        )
    if dirty_state.strip():
        raise SystemExit(
            "LongMemEval-V2 checkout must be clean; tracked or untracked changes were found."
        )
    return {
        "revision": revision,
        "dirty": False,
        "dirty_state_sha256": hashlib.sha256(dirty_state.encode("utf-8")).hexdigest(),
    }


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _question_ids(path: Path, *, jsonl: bool) -> list[str]:
    if jsonl:
        values = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    else:
        value = json.loads(path.read_text(encoding="utf-8"))
        values = value if isinstance(value, list) else (
            value.get("questions") if isinstance(value, dict) else None
        )
    if not isinstance(values, list) or not values:
        raise ValueError(f"{path.name} must contain a non-empty question list")
    result = []
    for number, value in enumerate(values, start=1):
        question_id = value.get("question_id") if isinstance(value, dict) else None
        if not isinstance(question_id, str) or not question_id:
            raise ValueError(f"{path.name} question {number} has no question_id")
        result.append(question_id)
    if len(set(result)) != len(result):
        raise ValueError(f"{path.name} contains duplicate question_id values")
    return result


def write_execution_manifest(
    output: str | Path,
    *,
    checkout: dict[str, object],
    per_question: str | Path,
    questions: str | Path,
    haystack: str | Path,
    trajectories: str | Path,
    memory_config: str | Path,
    matrix_manifest: str | Path,
    seed: int,
    delegated_argv: Sequence[str],
) -> dict[str, object]:
    """Write an immutable completion receipt after exact source coverage succeeds."""
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("seed must be a non-negative integer")
    if (
        checkout.get("revision") != PINNED_LONGMEMEVAL_V2_REVISION
        or checkout.get("dirty") is not False
        or checkout.get("dirty_state_sha256") != hashlib.sha256(b"").hexdigest()
    ):
        raise ValueError("checkout must attest the exact clean official revision")
    if isinstance(delegated_argv, (str, bytes)) or any(
        not isinstance(value, str) for value in delegated_argv
    ):
        raise ValueError("delegated_argv must be a sequence of strings")
    paths = {
        "per_question": Path(per_question),
        "questions": Path(questions),
        "haystack": Path(haystack),
        "trajectories": Path(trajectories),
        "memory_config": Path(memory_config),
        "matrix_manifest": Path(matrix_manifest),
    }
    source_ids = _question_ids(paths["questions"], jsonl=False)
    output_ids = _question_ids(paths["per_question"], jsonl=True)
    if len(output_ids) != len(source_ids) or set(output_ids) != set(source_ids):
        raise ValueError("official output does not exactly cover the source question IDs")
    payload: dict[str, object] = {
        "schema": EXECUTION_MANIFEST_SCHEMA,
        "status": "complete",
        "upstream_revision": checkout["revision"],
        "official_checkout": checkout,
        "environment": environment_provenance(),
        "seed": seed,
        "questions_sha256": _sha256_file(paths["questions"]),
        "haystack_sha256": _sha256_file(paths["haystack"]),
        "trajectories_sha256": _sha256_file(paths["trajectories"]),
        "memory_config_sha256": _sha256_file(paths["memory_config"]),
        "matrix_manifest_sha256": _sha256_file(paths["matrix_manifest"]),
        "per_question_sha256": _sha256_file(paths["per_question"]),
        "source_question_count": len(source_ids),
        "output_row_count": len(output_ids),
        "delegated_argv": list(delegated_argv),
        "delegated_argv_sha256": hashlib.sha256(
            json.dumps(list(delegated_argv), separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    }
    content = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and target.read_text(encoding="utf-8") != content:
        raise ValueError(f"refusing to replace different execution manifest: {target}")
    target.write_text(content, encoding="utf-8")
    return payload


def _parse_receipt_options(
    argv: Sequence[str],
) -> tuple[Optional[argparse.Namespace], list[str]]:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--engraphis-execution-manifest")
    parser.add_argument("--engraphis-per-question")
    parser.add_argument("--engraphis-questions")
    parser.add_argument("--engraphis-haystack")
    parser.add_argument("--engraphis-trajectories")
    parser.add_argument("--engraphis-memory-config")
    parser.add_argument("--engraphis-matrix-manifest")
    parser.add_argument("--engraphis-seed", type=int)
    options, delegated = parser.parse_known_args(list(argv))
    fields = (
        "engraphis_execution_manifest",
        "engraphis_per_question",
        "engraphis_questions",
        "engraphis_haystack",
        "engraphis_trajectories",
        "engraphis_memory_config",
        "engraphis_matrix_manifest",
        "engraphis_seed",
    )
    supplied = [getattr(options, field) is not None for field in fields]
    if any(supplied) and not all(supplied):
        parser.error("all --engraphis-* completion-receipt arguments must be supplied together")
    if options.engraphis_seed is not None and options.engraphis_seed < 0:
        parser.error("--engraphis-seed must be non-negative")
    return (options if all(supplied) else None), delegated


def pin_official_reader_processor() -> Callable[[], None]:
    """Force the official harness reader processor onto the audited revision."""
    try:
        from transformers import AutoProcessor
    except ImportError as exc:  # pragma: no cover - optional official-run dependency
        raise SystemExit(
            "canonical LongMemEval-V2 execution requires transformers and the pinned reader processor."
        ) from exc
    original = AutoProcessor.from_pretrained

    @classmethod
    def pinned_from_pretrained(
        cls: object, pretrained_model_name_or_path: object, *args: object, **kwargs: object
    ) -> object:
        del cls
        if str(pretrained_model_name_or_path) != PINNED_READER_MODEL:
            raise RuntimeError(
                "canonical LongMemEval-V2 execution permits only the configured reader processor"
            )
        requested_revision = kwargs.get("revision")
        if requested_revision not in (None, PINNED_READER_REVISION):
            raise RuntimeError("official harness requested a reader revision outside the canonical profile")
        kwargs["revision"] = PINNED_READER_REVISION
        return original(pretrained_model_name_or_path, *args, **kwargs)

    AutoProcessor.from_pretrained = pinned_from_pretrained

    def restore() -> None:
        AutoProcessor.from_pretrained = original

    return restore


def main(argv: Optional[list[str]] = None) -> None:
    receipt_options, delegated_argv = _parse_receipt_options(
        sys.argv[1:] if argv is None else argv
    )
    try:
        memory_module = importlib.import_module("memory_modules.memory")
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "LongMemEval-V2 is not importable. Add the pinned official checkout "
            "to PYTHONPATH before running this module."
        ) from exc
    checkout = verify_official_checkout(memory_module)
    importlib.import_module("eval.longmemeval_v2")
    restore_processor = pin_official_reader_processor()
    original_argv = sys.argv
    sys.argv = [original_argv[0], *delegated_argv]
    try:
        try:
            runpy.run_module("evaluation.harness", run_name="__main__")
        except SystemExit as exc:
            if exc.code not in (None, 0):
                raise
    finally:
        sys.argv = original_argv
        restore_processor()
    if receipt_options is not None:
        write_execution_manifest(
            receipt_options.engraphis_execution_manifest,
            checkout=checkout,
            per_question=receipt_options.engraphis_per_question,
            questions=receipt_options.engraphis_questions,
            haystack=receipt_options.engraphis_haystack,
            trajectories=receipt_options.engraphis_trajectories,
            memory_config=receipt_options.engraphis_memory_config,
            matrix_manifest=receipt_options.engraphis_matrix_manifest,
            seed=receipt_options.engraphis_seed,
            delegated_argv=delegated_argv,
        )


if __name__ == "__main__":
    main()
