"""Chunking eval — whole-file vs sub-file chunked ingestion, same recall pipeline.

Measures the payoff of ``ENGRAPHIS_EXTRACTOR=chunk`` on long, multi-topic documents:
the same corpus is ingested twice — once as one memory per document (``whole``), once
through the deterministic ``ChunkingExtractor`` (``chunked``) — and queried through the
*real* ``MemoryEngine`` hybrid recall. For each mode we report

* ``recall_at_k``          — did a top-k memory actually contain the evidence, and
* ``mean_context_tokens``  — how many context tokens the agent must carry for those
  top-k memories (``core.textutil.estimate_tokens``), i.e. the cost of the answer, and
* ``mean_evidence_tokens`` — tokens of the smallest top-k memory that holds the evidence
  (tokens-to-evidence).

The headline is **context reduction**: chunking returns the relevant *passage* instead
of the whole document, so recall holds while the context cost collapses — the
"quality per token" metric in ``BENCHMARKS.md``. Runs offline on the deterministic
embedder for a stable plumbing/regression number; pass ``--embed-model`` (a real
sentence-transformers model) for a publishable retrieval number.

Usage::

    python -m eval.chunking_eval --dataset eval/datasets/longdoc.jsonl --k 5           # offline
    python -m eval.chunking_eval --dataset eval/datasets/longdoc.jsonl --k 5 \
        --embed-model sentence-transformers/all-MiniLM-L6-v2                            # real
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Optional

from engraphis.core.textutil import estimate_tokens
from engraphis.service import MemoryService

MODES = ("whole", "chunked")


def load(path: str) -> list[dict]:
    cases = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            cases.append(json.loads(line))
    return cases


def run_eval(cases: list[dict], *, mode: str, k: int = 5,
             embed_model: Optional[str] = None, embed_dim: int = 256) -> dict:
    """Ingest the corpus in one workspace under ``mode`` and score its questions."""
    svc = MemoryService.create(":memory:", embed_model=embed_model, embed_dim=embed_dim,
                               extractor=("chunk" if mode == "chunked" else "none"))
    memories = 0
    for c in cases:
        out = svc.ingest(c["document"], workspace="corpus", mtype="semantic")
        memories += out["count"]

    nq = hits = 0
    ctx_tokens = evidence_tokens = 0
    for c in cases:
        for q in c["questions"]:
            nq += 1
            results = svc.recall(q["q"], workspace="corpus", k=k).get("memories") or []
            ctx_tokens += sum(estimate_tokens(m.get("content") or "") for m in results)
            holding = [m for m in results if q["evidence"] in (m.get("content") or "")]
            if holding:
                hits += 1
                evidence_tokens += min(estimate_tokens(m["content"]) for m in holding)
    return {
        "mode": mode, "memories_stored": memories, "questions": nq,
        "recall_at_k": round(hits / nq, 3) if nq else 0.0,
        "mean_context_tokens": round(ctx_tokens / nq, 1) if nq else 0.0,
        "mean_evidence_tokens": round(evidence_tokens / hits, 1) if hits else 0.0,
    }


def compare(cases: list[dict], *, k: int, embed_model: Optional[str]) -> dict:
    reports = {m: run_eval(cases, mode=m, k=k, embed_model=embed_model) for m in MODES}
    whole, chunked = reports["whole"], reports["chunked"]
    reduction = 0.0
    if whole["mean_context_tokens"]:
        reduction = round(100.0 * (1 - chunked["mean_context_tokens"]
                                   / whole["mean_context_tokens"]), 1)
    return {"reports": reports, "context_reduction_pct": reduction, "k": k}


def main() -> int:
    ap = argparse.ArgumentParser(description="Whole vs chunked ingestion eval.")
    ap.add_argument("--dataset", default="eval/datasets/longdoc.jsonl")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--embed-model", default=None,
                    help="sentence-transformers model for a real number; omit for the "
                         "deterministic (offline) embedder.")
    args = ap.parse_args()

    cases = load(args.dataset)
    result = compare(cases, k=args.k, embed_model=args.embed_model)
    embedder = args.embed_model or "DeterministicEmbedder (offline — plumbing number)"
    print(f"chunking eval — {len(cases)} docs · {result['reports']['whole']['questions']} "
          f"questions @ k={args.k} · embedder={embedder}\n")
    row = "  {mode:<8} recall@k={recall_at_k:<6} ctx_tokens={mean_context_tokens:<8} " \
          "evidence_tokens={mean_evidence_tokens:<7} (memories={memories_stored})"
    for mode in MODES:
        print(row.format(**result["reports"][mode]))
    print(f"\n  context reduction (chunked vs whole): {result['context_reduction_pct']}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
