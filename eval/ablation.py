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
from engraphis.core.interfaces import Edge, MemoryRecord, MemoryType, Node, Scope, SearchFilter
from engraphis.core.recall import RecallEngine
from engraphis.core.store import Store
from eval import metrics
from eval.harness import load_dataset


def _seed_graph(store: Store, *, workspace_id: str, repo_id: str, case: dict) -> None:
    """Persist readable dataset edges with the entity IDs returned by the store."""
    entity_ids: dict[str, str] = {}
    for entity in case.get("entities", []):
        name = entity[0]
        entity_ids[name] = store.upsert_entity(Node(
            id="", name=name,
            ntype=(entity[1] if len(entity) > 1 else "concept"),
            workspace_id=workspace_id, repo_id=repo_id,
        ))
    for edge in case.get("edges", []):
        src = entity_ids.get(edge[0])
        dst = entity_ids.get(edge[1])
        if src is None or dst is None:
            raise ValueError(
                f"eval edge references an unknown entity: {edge[0]!r} -> {edge[1]!r}"
            )
        store.upsert_edge(Edge(
            id="", src=src, dst=dst,
            relation=(edge[2] if len(edge) > 2 else "rel"),
            workspace_id=workspace_id, repo_id=repo_id,
        ))


def _score(dataset: list[dict], *, k: int, hybrid: bool, graph_mode: str = "ppr") -> float:
    emb = DeterministicEmbedder(256)
    per = []
    for case in dataset:
        store = Store(":memory:")
        wid = store.get_or_create_workspace("eval")
        rid = store.get_or_create_repo(wid, case.get("id", "c"))
        index = NumpyVectorIndex(store)
        # Seed the entity graph when the case provides one (optional keys), so the graph
        # arm has something to walk — mirrors what production extraction populates.
        _seed_graph(store, workspace_id=wid, repo_id=rid, case=case)
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


def _arm_recall(dataset: list[dict], *, k: int, arm: str) -> float:
    """Arm-level recall@k: can a SINGLE retrieval arm reach the supporting memory?

    ``arm``: "vector" (dense only), "graph1hop" or "graphppr" (that graph arm alone).
    This isolates the graph machinery from score fusion — on the multi-hop set the answer
    sits two entity-hops from the query, so the vector arm and 1-hop expansion can't reach
    it but Personalized PageRank can. That's the ablation signal the saturated full-recall
    numbers hide."""
    from engraphis.core.store import now_ts
    emb = DeterministicEmbedder(256)
    per = []
    for case in dataset:
        store = Store(":memory:")
        wid = store.get_or_create_workspace("eval")
        rid = store.get_or_create_repo(wid, case.get("id", "c"))
        index = NumpyVectorIndex(store)
        _seed_graph(store, workspace_id=wid, repo_id=rid, case=case)
        mode = "1hop" if arm == "graph1hop" else "ppr"
        engine = RecallEngine(store, emb, index, IdentityReranker(), graph_mode=mode)
        tag_by_id = {}
        for m in case["memories"]:
            mid = store.add_memory(MemoryRecord(
                id="", content=m["text"], mtype=MemoryType.EPISODIC, scope=Scope.REPO,
                workspace_id=wid, repo_id=rid, embedding=emb.embed([m["text"]])[0]))
            tag_by_id[mid] = m.get("tag")
        for q in case["questions"]:
            if arm == "vector":
                ids = [i for i, _ in index.search(emb.embed([q["q"]])[0], k,
                                                  filter=SearchFilter(workspace_id=wid))]
            else:
                ranked = sorted(engine._graph_arm(q["q"], SearchFilter(workspace_id=wid),
                                                  now_ts()).items(),
                                key=lambda kv: kv[1], reverse=True)
                ids = [i for i, _ in ranked[:k]]
            per.append(metrics.recall_at_k([tag_by_id.get(i) for i in ids], q.get("supporting", [])))
        store.close()
    return round(sum(per) / max(len(per), 1), 4)


def main() -> None:
    ds = load_dataset(str(Path(__file__).resolve().parent / "datasets" / "sample.jsonl"))
    print("Engraphis ablation — recall@5")
    print(f"  vector-only  : {_score(ds, k=5, hybrid=False)}")
    print(f"  hybrid-1hop  : {_score(ds, k=5, hybrid=True, graph_mode='1hop')}")
    print(f"  hybrid-ppr   : {_score(ds, k=5, hybrid=True, graph_mode='ppr')}")

    mh_path = Path(__file__).resolve().parent / "datasets" / "graph_multihop.jsonl"
    if mh_path.exists():
        mh = load_dataset(str(mh_path))
        print("\nEngraphis ablation (multi-hop graph dataset) — arm-level recall@5")
        print("  (answers sit 2 entity-hops from the query; which arm can REACH them?)")
        print(f"  vector arm   : {_arm_recall(mh, k=5, arm='vector')}")
        print(f"  graph 1-hop  : {_arm_recall(mh, k=5, arm='graph1hop')}   (reaches 1 hop only)")
        print(f"  graph PPR    : {_arm_recall(mh, k=5, arm='graphppr')}   (multi-hop walk)")


if __name__ == "__main__":
    main()
