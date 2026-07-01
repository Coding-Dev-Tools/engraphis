"""Eval runner: ingest fixture memories, query, score retrieval.

Routes both ingestion and querying through ``MemoryEngine`` — the same hybrid
vector+lexical+graph recall, six-term scoring, RRF fusion, and deterministic
conflict resolution that ships in production — not a bare vector-index lookup.
(Earlier versions of this harness called the vector index directly, which meant
the CI gate measured plumbing but never exercised the actual recall pipeline or
the write-path resolver; AGENTS.md §3.7 — "prove better with a number" — only
means something if the number is about what ships.)

Runs fully offline with the deterministic embedder + NumPy index, so it executes
anywhere (including CI) with no model download. The same harness will drive the
real backends — just pass a different ``Embedder`` in.

    python -m eval.harness --dataset eval/datasets/sample.jsonl --k 5

Dataset format (JSONL, one object per line):
    {
      "id": "case-1",
      "memories": [{"tag": "f1", "text": "..."}, ...],
      "questions": [{"q": "...", "answer": "...", "supporting": ["f1"]}]
    }

A memory's tag may be absent from the retrieved set without being "wrong": if its
text was resolved as a near-duplicate or superseded by a later memory in the same
case (conflict resolution — see ``core.resolve``), its tag now maps to whichever
memory *is* live, and that is what gets credited. This is intentional: the
"temporal-update" style fixtures rely on exactly this to test that superseded
facts stop being treated as current.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Optional

from engraphis.backends import DeterministicEmbedder, NumpyVectorIndex
from engraphis.backends.reranker import IdentityReranker
from engraphis.core.engine import MemoryEngine
from engraphis.core.interfaces import MemoryType, Scope
from engraphis.core.store import Store
from eval import metrics


def load_dataset(path: str) -> list[dict]:
    items = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            items.append(json.loads(line))
    return items


def run(dataset: list[dict], *, k: int = 5, dim: int = 256,
        embedder: Optional[DeterministicEmbedder] = None,
        resolve_conflicts: bool = True) -> dict:
    embedder = embedder or DeterministicEmbedder(dim=dim)
    per_q = []

    for case in dataset:
        store = Store(":memory:")
        wid = store.get_or_create_workspace("eval")
        rid = store.get_or_create_repo(wid, case.get("id", "case"))
        index = NumpyVectorIndex(store)
        engine = MemoryEngine(store, embedder, index, IdentityReranker())

        tag_to_id: dict[str, str] = {}
        id_to_tags: dict[str, list[str]] = {}
        id_to_text: dict[str, str] = {}
        for m in case["memories"]:
            mid = engine.remember(
                m["text"], workspace_id=wid, repo_id=rid, mtype=MemoryType.EPISODIC,
                scope=Scope.REPO, resolve_conflicts=resolve_conflicts,
            )
            tag = m.get("tag")
            tag_to_id[tag] = mid
            id_to_tags.setdefault(mid, []).append(tag)
            id_to_text[mid] = m["text"]

        for q in case["questions"]:
            res = engine.recall(q["q"], workspace_id=wid, k=k)
            retrieved_ids = [c["id"] for c in res.chunks]
            retrieved_tags = [t for i in retrieved_ids for t in id_to_tags.get(i, [None])]
            retrieved_texts = [id_to_text.get(i, "") for i in retrieved_ids]
            supporting = q.get("supporting", [])
            per_q.append({
                "case": case.get("id"),
                "q": q["q"],
                "recall_at_k": metrics.recall_at_k(retrieved_tags, supporting),
                "hit_at_k": metrics.hit_at_k(retrieved_tags, supporting),
                "answer_token_recall": metrics.answer_token_recall(retrieved_texts, q.get("answer", "")),
            })
        store.close()

    n = max(len(per_q), 1)
    report = {
        "questions": len(per_q),
        "recall_at_k": round(sum(x["recall_at_k"] for x in per_q) / n, 4),
        "hit_at_k": round(sum(x["hit_at_k"] for x in per_q) / n, 4),
        "answer_token_recall": round(sum(x["answer_token_recall"] for x in per_q) / n, 4),
        "k": k,
        "detail": per_q,
    }
    return report


def _print(report: dict) -> None:
    print(f"\nEngraphis eval — {report['questions']} questions @ k={report['k']}")
    print(f"  recall@k            : {report['recall_at_k']:.3f}")
    print(f"  hit@k               : {report['hit_at_k']:.3f}")
    print(f"  answer_token_recall : {report['answer_token_recall']:.3f}\n")


def main() -> None:
    ap = argparse.ArgumentParser(description="Run the Engraphis retrieval eval.")
    ap.add_argument("--dataset", default=str(Path(__file__).resolve().parent / "datasets" / "sample.jsonl"))
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--dim", type=int, default=256)
    ap.add_argument("--json", action="store_true", help="print full JSON report")
    args = ap.parse_args()

    report = run(load_dataset(args.dataset), k=args.k, dim=args.dim)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print(report)


if __name__ == "__main__":
    main()
