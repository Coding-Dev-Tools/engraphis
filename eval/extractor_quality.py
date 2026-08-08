"""Extractor quality eval — compare extraction modes on fact-level retrieval.

Measures how well each extractor mode (``none``, ``chunk``, ``llm``,
``llm_structured``) preserves retrievable facts from the same corpus. For each
mode we ingest the dataset into a fresh workspace, run the gold-standard
queries, and report:

* ``fact_count``            — number of memories stored after ingestion
* ``precision``             — fraction of top-k results that contain the evidence
* ``recall``                — fraction of questions where at least one top-k result
                              contains the evidence
* ``f1``                    — harmonic mean of precision and recall
* ``mean_tokens_per_fact``  — average token count of stored memories

The offline modes (``none``, ``chunk``) run deterministically with no API key.
The LLM modes run only with explicit ``--include-llm`` opt-in because they may
make network requests and incur provider cost. ``--embed-model`` independently
selects a real sentence-transformers embedding model.

Usage::

    python -m eval.extractor_quality --dataset eval/datasets/longdoc.jsonl
    python -m eval.extractor_quality --dataset eval/datasets/longdoc.jsonl --json
    python -m eval.extractor_quality --dataset eval/datasets/longdoc.jsonl \
        --include-llm --embed-model sentence-transformers/all-MiniLM-L6-v2
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Optional

from engraphis.backends.extractor import ChunkingExtractor, get_extractor
from engraphis.core.interfaces import MemoryType
from engraphis.core.textutil import estimate_tokens
from engraphis.service import MemoryService

OFFLINE_MODES = ("none", "chunk")
LLM_MODES = ("llm", "llm_structured")
ALL_MODES = OFFLINE_MODES + LLM_MODES


def load(path: str) -> list[dict]:
    cases = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            cases.append(json.loads(line))
    return cases


def run_eval(cases: list[dict], *, mode: str, k: int = 5,
             embed_model: Optional[str] = None, embed_dim: int = 256) -> dict:
    """Ingest the corpus under ``mode`` and score queries against gold evidence."""
    if mode not in ALL_MODES:
        raise ValueError(f"unsupported extractor mode: {mode}")
    if k <= 0:
        raise ValueError("k must be positive")
    svc = MemoryService.create(
        ":memory:",
        embed_model=embed_model,
        embed_dim=embed_dim,
        extractor=mode,
    )
    try:
        if mode == "chunk":
            chunker = get_extractor("chunk")
            if isinstance(chunker, ChunkingExtractor):
                svc.engine.extractor = chunker

        workspace_id = svc.store.get_or_create_workspace("corpus")
        fixture_metadata = {
            "provenance": {
                "source": "eval:checked-in-fixture",
                "trusted": True,
                "trust_origin": "offline_eval",
            }
        }

        total_facts = 0
        model_backed_facts = 0
        stored_tokens: list[int] = []
        for case in cases:
            out = svc.engine.ingest(
                case["document"],
                workspace_id=workspace_id,
                default_mtype=MemoryType.SEMANTIC,
                metadata=fixture_metadata,
            )
            total_facts += out["count"]
            for fact in out["facts"]:
                record = svc.store.get_memory(fact["id"])
                if record is not None:
                    stored_tokens.append(estimate_tokens(record.content))
                    if isinstance(record.metadata.get("llm_extraction"), dict):
                        model_backed_facts += 1

        if mode in LLM_MODES and model_backed_facts == 0:
            raise RuntimeError("LLM extractor produced no model-backed facts")

        question_count = 0
        hits = 0
        total_precision_sum = 0.0
        for case in cases:
            for question in case["questions"]:
                question_count += 1
                results = svc.recall(
                    question["q"], workspace="corpus", k=k
                ).get("memories") or []
                evidence = question["evidence"]
                holding = [m for m in results if evidence in (m.get("content") or "")]
                if holding:
                    hits += 1
                if results:
                    total_precision_sum += len(holding) / len(results)

        recall_val = hits / question_count if question_count else 0.0
        precision_val = total_precision_sum / question_count if question_count else 0.0
        f1_val = (
            2 * precision_val * recall_val / (precision_val + recall_val)
            if (precision_val + recall_val) > 0 else 0.0
        )
        mean_tokens = sum(stored_tokens) / len(stored_tokens) if stored_tokens else 0.0
        return {
            "mode": mode,
            "fact_count": total_facts,
            "model_backed_fact_count": model_backed_facts,
            "precision": round(precision_val, 3),
            "recall": round(recall_val, 3),
            "f1": round(f1_val, 3),
            "mean_tokens_per_fact": round(mean_tokens, 1),
            "questions": question_count,
        }
    finally:
        extractor = svc.engine.extractor
        close_llm = getattr(getattr(extractor, "llm", None), "close", None)
        if callable(close_llm):
            close_llm()
        svc.store.close()


def evaluate_all(cases: list[dict], *, k: int, embed_model: Optional[str],
                 include_llm: bool = False) -> dict:
    """Run offline modes and any explicitly opted-in LLM modes."""
    reports: dict[str, dict] = {}
    skipped: list[dict[str, str]] = []

    for mode in OFFLINE_MODES:
        reports[mode] = run_eval(cases, mode=mode, k=k, embed_model=embed_model)

    if include_llm:
        for mode in LLM_MODES:
            try:
                reports[mode] = run_eval(cases, mode=mode, k=k, embed_model=embed_model)
            except Exception as exc:
                skipped.append({
                    "mode": mode,
                    "reason": f"skipped ({type(exc).__name__})",
                })
    else:
        for mode in LLM_MODES:
            skipped.append({
                "mode": mode,
                "reason": "skipped (requires explicit --include-llm)",
            })

    return {"reports": reports, "skipped": skipped, "k": k}


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Extractor quality eval: compare none/chunk/llm/llm_structured."
    )
    ap.add_argument("--dataset", default="eval/datasets/longdoc.jsonl")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--embed-model", default=None,
                    help="sentence-transformers model; omit for offline-only eval.")
    ap.add_argument("--include-llm", action="store_true",
                    help="run provider-backed modes; may use network and incur cost.")
    ap.add_argument("--json", action="store_true", dest="json_output",
                    help="emit JSON instead of human-readable table.")
    args = ap.parse_args()

    cases = load(args.dataset)
    result = evaluate_all(
        cases,
        k=args.k,
        embed_model=args.embed_model,
        include_llm=args.include_llm,
    )

    if args.json_output:
        print(json.dumps(result, indent=2))
        return 0

    embedder = args.embed_model or "DeterministicEmbedder (offline)"
    print(f"extractor quality eval — {len(cases)} docs · "
          f"{result['reports'].get('none', {}).get('questions', 0)} questions "
          f"@ k={args.k} · embedder={embedder}\n")

    row = ("  {mode:<16} facts={fact_count:<6} precision={precision:<6} "
           "recall={recall:<6} f1={f1:<6} mean_tokens={mean_tokens_per_fact:<8}")
    for mode in ALL_MODES:
        if mode in result["reports"]:
            print(row.format(**result["reports"][mode]))

    if result["skipped"]:
        print()
        for s in result["skipped"]:
            print(f"  {s['mode']:<16} {s['reason']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
