"""Ablation: vector-only vs full hybrid recall.

Demonstrates that the eval harness can attribute quality to each part of the
pipeline. Runs offline with the deterministic embedder. Real datasets (LoCoMo,
LongMemEval) and a real embedder make the gap meaningful; on the tiny fixture
both modes may saturate — the point is the measurement scaffold.

    python -m eval.ablation
"""
from __future__ import annotations

from pathlib import Path
import re

from engraphis.backends import DeterministicEmbedder, NumpyVectorIndex
from engraphis.backends.reranker import IdentityReranker
from engraphis.core import scoring
from engraphis.core.interfaces import Edge, MemoryRecord, MemoryType, Node, Scope, SearchFilter
from engraphis.core.recall import RecallEngine
from engraphis.core.retrieval_policy import ProfileConfig
from engraphis.core.store import Store
from eval import metrics
from eval.harness import load_dataset


def _seed_graph(
    store: Store, *, workspace_id: str, repo_id: str, case: dict,
) -> dict[str, str]:
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
    return entity_ids


def _link_fixture_mentions(
    store: Store,
    *,
    memory_id: str,
    text: str,
    entity_ids: dict[str, str],
    workspace_id: str,
    repo_id: str,
) -> None:
    """Materialize the same exact-mention incidence used by production writes."""
    for name, entity_id in entity_ids.items():
        if re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", text, re.IGNORECASE):
            store.link_memory_entity(
                memory_id=memory_id,
                entity_id=entity_id,
                workspace_id=workspace_id,
                repo_id=repo_id,
                source_kind="eval_fixture",
                confidence=1.0,
            )


def _score(
    dataset: list[dict],
    *,
    k: int,
    hybrid: bool,
    graph_mode: str = "ppr",
    retrieval_profile: str = "balanced",
) -> float:
    emb = DeterministicEmbedder(256)
    per = []
    for case in dataset:
        store = Store(":memory:")
        wid = store.get_or_create_workspace("eval")
        rid = store.get_or_create_repo(wid, case.get("id", "c"))
        index = NumpyVectorIndex(store)
        # Seed the entity graph when the case provides one (optional keys), so the graph
        # arm has something to walk — mirrors what production extraction populates.
        entity_ids = _seed_graph(store, workspace_id=wid, repo_id=rid, case=case)
        engine = RecallEngine(store, emb, index, IdentityReranker(), graph_mode=graph_mode)
        tag_by_id = {}
        for m in case["memories"]:
            mid = store.add_memory(MemoryRecord(
                id="", content=m["text"], mtype=MemoryType.EPISODIC, scope=Scope.REPO,
                workspace_id=wid, repo_id=rid, embedding=emb.embed([m["text"]])[0]))
            _link_fixture_mentions(
                store,
                memory_id=mid,
                text=m["text"],
                entity_ids=entity_ids,
                workspace_id=wid,
                repo_id=rid,
            )
            tag_by_id[mid] = m.get("tag")
        for q in case["questions"]:
            if hybrid:
                ids = [
                    c["id"] for c in engine.recall(
                        q["q"],
                        SearchFilter(workspace_id=wid),
                        k=k,
                        retrieval_profile=retrieval_profile,
                        # Fixtures seed Store directly and therefore carry no
                        # write-time prompt approval.  This is a retrieval-arm
                        # ablation, so request inspection visibility explicitly
                        # instead of accidentally measuring provenance gating.
                        include_untrusted=True,
                    ).chunks
                ]
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
        entity_ids = _seed_graph(store, workspace_id=wid, repo_id=rid, case=case)
        mode = "1hop" if arm == "graph1hop" else "ppr"
        engine = RecallEngine(store, emb, index, IdentityReranker(), graph_mode=mode)
        tag_by_id = {}
        for m in case["memories"]:
            mid = store.add_memory(MemoryRecord(
                id="", content=m["text"], mtype=MemoryType.EPISODIC, scope=Scope.REPO,
                workspace_id=wid, repo_id=rid, embedding=emb.embed([m["text"]])[0]))
            _link_fixture_mentions(
                store,
                memory_id=mid,
                text=m["text"],
                entity_ids=entity_ids,
                workspace_id=wid,
                repo_id=rid,
            )
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


def _ordinary_recall_age_delta() -> float:
    """Measure age-only bias in ordinary recall (must remain zero).

    The two records have equal retrieval evidence and equal reinforcement history;
    only their validity/ingestion time differs. A non-zero value would show that
    query recall is applying fact age in addition to Ebbinghaus retention.
    """
    now = 1_000_000.0
    common = dict(
        content="same evidence", mtype=MemoryType.SEMANTIC,
        stability=4.0, last_access=now - 86_400, importance=0.4,
    )
    recent = MemoryRecord(id="recent", ingested_at=now, valid_from=now, **common)
    old = MemoryRecord(
        id="old", ingested_at=now - 365 * 86_400,
        valid_from=now - 365 * 86_400, **common,
    )
    weights = scoring.weights_for(MemoryType.SEMANTIC)
    return round(
        scoring.score_memory(recent, now=now, weights=weights, semantic=0.7)
        - scoring.score_memory(old, now=now, weights=weights, semantic=0.7),
        8,
    )


class _SemanticEvalEmbedder(DeterministicEmbedder):
    """Offline test embedder that declares the vector arm semantic."""

    supports_semantic_search = True
    embedding_mode = "semantic"


class _FixedScoreIndex:
    """One weak vector candidate for the semantic-confidence micro-ablation."""

    def __init__(self, scores: list[tuple[str, float]]) -> None:
        self.scores = scores

    def search(self, _query, k: int, *, filter=None) -> list[tuple[str, float]]:
        del filter
        return self.scores[:k]


def _semantic_confidence_calibration_contrast() -> tuple[bool, bool]:
    """Check the known weak-singleton mode without claiming benchmark gain.

    The contrast is deterministic: a 0.01 cosine vector distractor competes
    with exact lexical evidence. It demonstrates only the opt-in score-control
    invariant; external semantic benchmarks remain the quality authority.
    """
    store = Store(":memory:")
    try:
        embedder = _SemanticEvalEmbedder(256)
        workspace_id = store.get_or_create_workspace("semantic-calibration")
        weak_id = store.add_memory(MemoryRecord(
            id="",
            content="The parking garage closes at dusk.",
            workspace_id=workspace_id,
            scope=Scope.WORKSPACE,
            embedding=embedder.embed(["The parking garage closes at dusk."])[0],
        ))
        lexical_id = store.add_memory(MemoryRecord(
            id="",
            content="PASETO is the approved token format.",
            workspace_id=workspace_id,
            scope=Scope.WORKSPACE,
            embedding=embedder.embed(["PASETO is the approved token format."])[0],
        ))
        engine = RecallEngine(
            store,
            embedder,
            _FixedScoreIndex([(weak_id, 0.01)]),
            IdentityReranker(),
        )
        base_config = ProfileConfig("vector_lexical", True, True, False, False)
        default_id = engine.recall(
            "PASETO",
            SearchFilter(workspace_id=workspace_id),
            k=1,
            include_untrusted=True,
            arm_config=base_config,
        ).chunks[0]["id"]
        calibrated_id = engine.recall(
            "PASETO",
            SearchFilter(workspace_id=workspace_id),
            k=1,
            include_untrusted=True,
            arm_config=ProfileConfig(
                "vector_lexical_calibrated",
                True,
                True,
                False,
                False,
                semantic_confidence_calibration=True,
            ),
        ).chunks[0]["id"]
        return default_id == weak_id, calibrated_id == lexical_id
    finally:
        store.close()


def main() -> None:
    ds = load_dataset(str(Path(__file__).resolve().parent / "datasets" / "sample.jsonl"))
    print("Engraphis ablation — recall@5")
    print(f"  vector-only  : {_score(ds, k=5, hybrid=False)}")
    print(f"  hybrid-1hop  : {_score(ds, k=5, hybrid=True, graph_mode='1hop')}")
    print(f"  hybrid-ppr   : {_score(ds, k=5, hybrid=True, graph_mode='ppr')}")
    print("\nEngraphis ordinary-recall age ablation")
    print(
        "  equal-reinforcement score delta (recent - 1y old): "
        f"{_ordinary_recall_age_delta():.8f}  (expected 0.00000000)"
    )
    default_weak_first, calibrated_lexical_first = _semantic_confidence_calibration_contrast()
    print("\nEngraphis semantic-confidence micro-ablation (not a benchmark)")
    print(f"  default weak singleton wins : {default_weak_first}")
    print(f"  opt-in calibrated lexical wins: {calibrated_lexical_first}")

    mh_path = Path(__file__).resolve().parent / "datasets" / "graph_multihop.jsonl"
    if mh_path.exists():
        mh = load_dataset(str(mh_path))
        print("\nEngraphis ablation (multi-hop graph dataset) — arm-level recall@5")
        print("  (answers sit 2 entity-hops from the query; which arm can REACH them?)")
        print(f"  vector arm   : {_arm_recall(mh, k=5, arm='vector')}")
        print(f"  graph 1-hop  : {_arm_recall(mh, k=5, arm='graph1hop')}   (reaches 1 hop only)")
        print(f"  graph PPR    : {_arm_recall(mh, k=5, arm='graphppr')}   (multi-hop walk)")
        print("\nEngraphis retrieval-policy fixture — recall@5")
        print(f"  balanced     : {_score(mh, k=5, hybrid=True)}")
        print(
            "  auto         : "
            f"{_score(mh, k=5, hybrid=True, retrieval_profile='auto')} "
            "(opt-in graph specialization)"
        )


if __name__ == "__main__":
    main()
