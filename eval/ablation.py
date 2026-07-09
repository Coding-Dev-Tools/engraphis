"""Ablation: vector-only vs full hybrid recall.

Demonstrates that the eval harness can attribute quality to each part of the
pipeline. Runs offline with the deterministic embedder. Real datasets (LoCoMo,
LongMemEval) and a real embedder make the gap meaningful; on the tiny fixture
both modes may saturate — the point is the measurement scaffold.

    python -m eval.ablation
"""
from __future__ import annotations

from pathlib import Path

from engraphis.backends import DeterministicEmbedder, NumpyVectorIndex
from engraphis.backends.reranker import IdentityReranker
from engraphis.core.interfaces import MemoryRecord, MemoryType, Scope, SearchFilter
from engraphis.core.recall import RecallEngine
from engraphis.core.store import Store
from eval import metrics
from eval.harness import load_dataset


def _score(dataset: list[dict], *, k: int, hybrid: bool, graph_mode: str = "ppr") -> float:
    emb = DeterministicEmbedder(256)
    per = []
    for case in dataset:
        store = Store(":memory:")
        wid = store.get_or_create_workspace("eval")
        rid = store.get_or_create_repo(wid, case.get("id", "c"))
        index = NumpyVectorIndex(store)
        engine = RecallEngine(store, emb, index, IdentityReranker(), graph_mode=graph_mode)
        tag_by_id = {}
        for m in case["memories"]:
            mid = store.add_memory(MemoryRecord(
                id="", content=m["text"], mtype=MemoryType.EPISODIC, scope=Scope.REPO,
                workspace_id=wid, repo_id=rid, embedding=emb.embed([m["text"]])[0]))
            tag_by_id[mid] = m.get("tag")
        for q in case["questions"]:
            if hybrid:
                ids = [c["id"] for c in engine.recall(q["q"], SearchFilter(workspace_id=wid), k=k).chunks]
            else:
                ids = [i for i, _ in index.search(emb.embed([q["q"]])[0], k,
                                                   filter=SearchFilter(workspace_id=wid))]
            per.append(metrics.recall_at_k([tag_by_id.get(i) for i in ids], q.get("supporting", [])))
        store.close()
    return round(sum(per) / max(len(per), 1), 4)


def main() -> None:
    ds = load_dataset(str(Path(__file__).resolve().parent / "datasets" / "sample.jsonl"))
    print("Engraphis ablation — recall@5")
    print(f"  vector-only  : {_score(ds, k=5, hybrid=False)}")
    print(f"  hybrid-1hop  : {_score(ds, k=5, hybrid=True, graph_mode='1hop')}")
    print(f"  hybrid-ppr   : {_score(ds, k=5, hybrid=True, graph_mode='ppr')}")


if __name__ == "__main__":
    main()
