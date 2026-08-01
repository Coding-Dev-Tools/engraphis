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

from engraphis.backends.extractor import ChunkingExtractor, get_extractor
from engraphis.core.interfaces import MemoryType
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
             embed_model: Optional[str] = None, embed_dim: int = 256,
             chunk_extractor: Optional[ChunkingExtractor] = None) -> dict:
    """Ingest the corpus in one workspace under ``mode`` and score its questions."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of: {', '.join(MODES)}")
    selected_chunker = chunk_extractor or get_extractor("chunk")
    if not isinstance(selected_chunker, ChunkingExtractor):
        raise TypeError("chunk_extractor must be a ChunkingExtractor")
    count_tokens = selected_chunker.count_tokens
    svc = MemoryService.create(":memory:", embed_model=embed_model, embed_dim=embed_dim,
                               extractor=("chunk" if mode == "chunked" else "none"))
    if mode == "chunked":
        svc.engine.extractor = selected_chunker
    # The checked-in fixture is trusted test data. Raw service ingest correctly marks
    # arbitrary imports untrusted, which normal recall excludes from agent context; use
    # the core ingest path with explicit eval provenance so this benchmark isolates the
    # chunking/retrieval effect instead of measuring the trust gate.
    workspace_id = svc.store.get_or_create_workspace("corpus")
    fixture_metadata = {
        "provenance": {
            "source": "eval:checked-in-fixture",
            "trusted": True,
            "trust_origin": "offline_eval",
        }
    }
    memories = 0
    stored_tokens: list[int] = []
    for c in cases:
        out = svc.engine.ingest(
            c["document"],
            workspace_id=workspace_id,
            default_mtype=MemoryType.SEMANTIC,
            metadata=fixture_metadata,
        )
        memories += out["count"]
        for fact in out["facts"]:
            record = svc.store.get_memory(fact["id"])
            if record is not None:
                stored_tokens.append(count_tokens(record.content))

    nq = hits = 0
    ctx_tokens = evidence_tokens = 0
    for c in cases:
        for q in c["questions"]:
            nq += 1
            results = svc.recall(q["q"], workspace="corpus", k=k).get("memories") or []
            ctx_tokens += sum(count_tokens(m.get("content") or "") for m in results)
            holding = [m for m in results if q["evidence"] in (m.get("content") or "")]
            if holding:
                hits += 1
                evidence_tokens += min(count_tokens(m["content"]) for m in holding)
    return {
        "mode": mode, "memories_stored": memories, "questions": nq,
        "recall_at_k": round(hits / nq, 3) if nq else 0.0,
        "mean_context_tokens": round(ctx_tokens / nq, 1) if nq else 0.0,
        "mean_evidence_tokens": round(evidence_tokens / hits, 1) if hits else 0.0,
        "max_stored_tokens": max(stored_tokens, default=0),
        "token_counter": selected_chunker.token_counter_identity,
    }


def compare(cases: list[dict], *, k: int, embed_model: Optional[str]) -> dict:
    chunker = get_extractor("chunk")
    reports = {
        mode: run_eval(
            cases,
            mode=mode,
            k=k,
            embed_model=embed_model,
            chunk_extractor=chunker,
        )
        for mode in MODES
    }
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
          "evidence_tokens={mean_evidence_tokens:<7} max_stored={max_stored_tokens:<6} " \
          "(memories={memories_stored})"
    for mode in MODES:
        print(row.format(**result["reports"][mode]))
    print(f"  token counter: {result['reports']['chunked']['token_counter']}")
    print(f"\n  context reduction (chunked vs whole): {result['context_reduction_pct']}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
