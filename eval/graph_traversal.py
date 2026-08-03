"""Offline regression eval for deterministic intent-layered graph traversal.

Each fixture gives the uniform PPR arm a slightly heavier wrong-layer distractor.
The opt-in policy must recover the relation-aligned target while retaining normal
scope and graph-layer filtering in the production recall implementation.  This is
a small synthetic regression fixture, not a claim about external benchmark gains.

    python -m eval.graph_traversal
"""
from __future__ import annotations

import json
from pathlib import Path

from engraphis.backends import DeterministicEmbedder, NumpyVectorIndex
from engraphis.backends.reranker import IdentityReranker
from engraphis.core.graph_layers import infer_graph_layer
from engraphis.core.graph_policy import DeterministicIntentGraphTraversalPolicy
from engraphis.core.interfaces import Edge, MemoryRecord, MemoryType, Node, Scope, SearchFilter
from engraphis.core.recall import RecallEngine
from engraphis.core.store import Store, now_ts


DATASET = Path(__file__).resolve().parent / "datasets" / "graph_layer_routing.jsonl"


def _load_fixture(path: Path = DATASET) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _top_id(scores: dict[str, float]) -> str:
    return sorted(scores, key=lambda memory_id: (-scores[memory_id], memory_id))[0]


def _run_case(case: dict) -> dict:
    store = Store(":memory:")
    try:
        workspace_id = store.get_or_create_workspace("graph-layer-routing")
        embedder = DeterministicEmbedder(dim=64)
        index = NumpyVectorIndex(store)
        entities = {
            name: store.upsert_entity(Node(
                id="", name=name, ntype="service", workspace_id=workspace_id,
            ))
            for name in ("alphasvc", "targetsvc", "distractorsvc")
        }
        target_layer = infer_graph_layer(case["target_relation"])
        store.upsert_edge(Edge(
            id="", src=entities["alphasvc"], dst=entities["targetsvc"],
            relation=case["target_relation"], layer=target_layer,
            weight=1.0, workspace_id=workspace_id,
        ))
        store.upsert_edge(Edge(
            id="", src=entities["alphasvc"], dst=entities["distractorsvc"],
            relation=case["distractor_relation"],
            layer=infer_graph_layer(case["distractor_relation"]),
            weight=1.2, workspace_id=workspace_id,
        ))
        memory_ids = {}
        for tag, entity, text in (
            ("target", "targetsvc", "target evidence"),
            ("distractor", "distractorsvc", "distractor evidence"),
        ):
            memory_ids[tag] = store.add_memory(MemoryRecord(
                id="", content=text, mtype=MemoryType.SEMANTIC,
                scope=Scope.WORKSPACE, workspace_id=workspace_id,
                embedding=embedder.embed([text])[0],
            ))
            store.link_memory_entity(
                memory_id=memory_ids[tag], entity_id=entities[entity],
                workspace_id=workspace_id, repo_id=None, source_kind="eval",
            )
        uniform = RecallEngine(store, embedder, index, IdentityReranker())
        layered = RecallEngine(
            store,
            embedder,
            index,
            IdentityReranker(),
            graph_traversal_policy=DeterministicIntentGraphTraversalPolicy(),
        )
        flt = SearchFilter(workspace_id=workspace_id)
        now = now_ts()
        baseline_top = _top_id(uniform._graph_arm(case["query"], flt, now))
        layered_top = _top_id(layered._graph_arm(case["query"], flt, now))
        return {
            "id": case["id"],
            "preferred_layer": case["preferred_layer"],
            "baseline_top": baseline_top,
            "layered_top": layered_top,
            "target": memory_ids["target"],
            "baseline_correct": baseline_top == memory_ids["target"],
            "layered_correct": layered_top == memory_ids["target"],
        }
    finally:
        store.close()


def run(path: Path = DATASET) -> dict:
    rows = [_run_case(case) for case in _load_fixture(path)]
    count = len(rows)
    return {
        "benchmark": {
            "name": "engraphis-graph-layer-routing/v1",
            "offline": True,
            "scope": "synthetic regression fixture; not an external benchmark",
        },
        "tasks": count,
        "uniform_recall_at_1": sum(row["baseline_correct"] for row in rows) / max(1, count),
        "intent_layered_recall_at_1": sum(row["layered_correct"] for row in rows) / max(1, count),
        "rows": rows,
    }


def main() -> None:
    print(json.dumps(run(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
