"""Run the official LongMemEval-V2 harness with Engraphis registered.

The upstream harness uses an import-time memory registry and does not discover
third-party backends. This entry point registers Engraphis first, then delegates
the unchanged command line to ``evaluation.harness``.
"""
from __future__ import annotations

import importlib
import runpy
import subprocess
from pathlib import Path
from typing import Callable


PINNED_LONGMEMEVAL_V2_REVISION = "6f020ac2fc3275e46c706d3406e02c3ed79b7be2"
PINNED_READER_MODEL = "Qwen/Qwen3.5-9B"
PINNED_READER_REVISION = "c202236235762e1c871ad0ccb60c8ee5ba337b9a"


def verify_official_checkout(memory_module: object) -> None:
    """Require the exact audited upstream revision before delegating a run.

    ``PYTHONPATH`` alone is not provenance: it can point at an arbitrary local
    fork with a compatible registry.  The wrapper is reserved for official V2
    execution, so fail before any data/model work if the imported memory module
    is not inside the pinned upstream checkout.
    """
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
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(
            "LongMemEval-V2 must be an exact pinned Git checkout; could not verify its revision."
        ) from exc
    if revision != PINNED_LONGMEMEVAL_V2_REVISION:
        raise SystemExit(
            "LongMemEval-V2 checkout revision mismatch: expected "
            f"{PINNED_LONGMEMEVAL_V2_REVISION}, found {revision or 'unknown'}."
        )


def pin_official_reader_processor() -> Callable[[], None]:
    """Force the official harness reader processor onto the audited revision.

    The pinned upstream harness loads ``AutoProcessor`` without a revision.
    This canonical Engraphis wrapper replaces that mutable default for the
    complete delegated run and restores the global method afterwards.
    """
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


def main() -> None:
    try:
        memory_module = importlib.import_module("memory_modules.memory")
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "LongMemEval-V2 is not importable. Add the pinned official checkout "
            "to PYTHONPATH before running this module."
        ) from exc
    verify_official_checkout(memory_module)
    importlib.import_module("eval.longmemeval_v2")
    restore_processor = pin_official_reader_processor()
    try:
        runpy.run_module("evaluation.harness", run_name="__main__")
    finally:
        restore_processor()


if __name__ == "__main__":
    main()
