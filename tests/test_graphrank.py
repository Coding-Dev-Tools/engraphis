import numpy as np  # noqa: F401  (asserts numpy-only dependency stays importable)
import pytest

from engraphis.backends import DeterministicEmbedder, NumpyVectorIndex
from engraphis.backends.reranker import IdentityReranker
from engraphis.core import graphrank
from engraphis.core.graphrank import personalized_pagerank
from engraphis.core.interfaces import Edge, MemoryRecord, MemoryType, Node, Scope, SearchFilter
from engraphis.core.recall import RecallEngine
from engraphis.core.store import Store


# ── pure PPR function ─────────────────────────────────────────────────────────────

def test_ppr_empty_inputs():
    assert personalized_pagerank({}, ["a"]) == {}
    assert personalized_pagerank({"a": [("b", 1.0)]}, []) == {}
    assert personalized_pagerank({"a": [("b", 1.0)]}, ["zzz"]) == {}


def test_ppr_reaches_multi_hop_neighbors():
    adj = {"a": [("b", 1.0)], "b": [("a", 1.0), ("c", 1.0)], "c": [("b", 1.0)], "d": []}
    r = personalized_pagerank(adj, ["a"])
    assert r["c"] > 0.0                    # two hops from the seed still gets mass
    assert "d" not in r                    # disconnected gets none
    assert r["a"] > r["c"]                 # seed locality preserved


def test_ppr_scores_sum_to_one():
    adj = {"a": [("b", 1.0)], "b": [("a", 1.0), ("c", 2.0)], "c": [("b", 2.0)]}
    r = personalized_pagerank(adj, ["a", "b"])
    assert abs(sum(r.values()) - 1.0) < 1e-6


def test_ppr_weight_influences_ranking():
    # b splits mass between c (heavy) and d (light) → c should outrank d.
    adj = {"a": [("b", 1.0)], "b": [("a", 1.0), ("c", 10.0), ("d", 0.1)],
           "c": [("b", 10.0)], "d": [("b", 0.1)]}
    r = personalized_pagerank(adj, ["a"])
    assert r["c"] > r["d"]


def _dense_reference(adjacency, seeds, *, damping=0.85, iterations=30, tol=1e-9):
    """Pre-sparse implementation retained only as a numerical oracle for this test."""
    nodes = sorted(set(adjacency) | {dst for edges in adjacency.values() for dst, _ in edges}
                   | set(seeds))
    index = {node: position for position, node in enumerate(nodes)}
    seed_ids = [index[seed] for seed in seeds if seed in index]
    if not seed_ids or not [seed for seed in seeds if seed in adjacency and adjacency[seed]]:
        return {}
    matrix = np.zeros((len(nodes), len(nodes)), dtype=np.float64)
    for source, edges in adjacency.items():
        total = float(sum(max(weight, 0.0) for _, weight in edges))
        if total <= 0.0:
            continue
        for destination, weight in edges:
            if weight > 0.0:
                matrix[index[destination], index[source]] += weight / total
    restart = np.zeros(len(nodes), dtype=np.float64)
    restart[seed_ids] = 1.0 / len(seed_ids)
    dangling = matrix.sum(axis=0) == 0.0
    probability = restart.copy()
    for _ in range(iterations):
        next_probability = (1.0 - damping) * restart + damping * (
            matrix @ probability + probability[dangling].sum() * restart
        )
        if float(np.abs(next_probability - probability).sum()) < tol:
            probability = next_probability
            break
        probability = next_probability
    return {nodes[index]: float(score) for index, score in enumerate(probability) if score > 0.0}


def test_sparse_ppr_matches_prior_dense_iteration_within_tight_tolerance():
    adj = {
        "a": [("b", 2.0), ("b", 1.0), ("c", 0.5)],
        "b": [("a", 1.0), ("d", 3.0)],
        "c": [],
        "d": [("a", 1.0)],
    }
    expected = _dense_reference(adj, ["a", "missing"], iterations=50)
    actual = personalized_pagerank(adj, ["a", "missing"], iterations=50)
    assert actual.keys() == expected.keys()
    for node in actual:
        assert actual[node] == pytest.approx(expected[node], abs=1e-12)


def test_sparse_ppr_handles_several_thousand_node_graph_without_dense_matrix():
    # A bidirectional chain has ~12k directed edges. A dense 6k × 6k float64
    # matrix would require ~275 MiB before iteration buffers; sparse iteration
    # remains proportional to the chain itself.
    size = 6_000
    adjacency = {f"n{index}": [] for index in range(size)}
    for index in range(size - 1):
        adjacency[f"n{index}"].append((f"n{index + 1}", 1.0))
        adjacency[f"n{index + 1}"].append((f"n{index}", 1.0))
    result = personalized_pagerank(adjacency, ["n0"])
    assert result["n0"] > result["n10"] > 0.0
    assert abs(sum(result.values()) - 1.0) < 1e-9


def test_sparse_ppr_refuses_oversized_direct_input_deterministically(monkeypatch):
    monkeypatch.setattr(graphrank, "MAX_NODES", 2)
    assert personalized_pagerank({"a": [("b", 1.0)], "b": [("c", 1.0)]}, ["a"]) == {}


# ── PPR retrieval arm inside RecallEngine ─────────────────────────────────────────

def _graph_fixture():
    """alpha—beta—gamma entity chain; M1 mentions alpha, M3 mentions only gamma."""
    store = Store(":memory:")
    wid = store.get_or_create_workspace("w")
    rid = store.get_or_create_repo(wid, "r")
    emb = DeterministicEmbedder(dim=64)
    index = NumpyVectorIndex(store)

    entity_ids = {}
    for name in ("alphasvc", "betasvc", "gammasvc"):
        entity_ids[name] = store.upsert_entity(Node(id="", name=name, ntype="service",
                                                    workspace_id=wid, repo_id=rid))
    store.upsert_edge(Edge(id="", src=entity_ids["alphasvc"], dst=entity_ids["betasvc"],
                           relation="calls", workspace_id=wid, repo_id=rid))
    store.upsert_edge(Edge(id="", src=entity_ids["betasvc"], dst=entity_ids["gammasvc"],
                           relation="calls", workspace_id=wid, repo_id=rid))

    texts = {
        "m1": "alphasvc handles the login flow.",
        "m3": "gammasvc owns the billing ledger reconciliation.",
    }
    ids = {}
    for tag, text in texts.items():
        rec = MemoryRecord(id="", content=text, mtype=MemoryType.SEMANTIC, scope=Scope.REPO,
                           workspace_id=wid, repo_id=rid, embedding=emb.embed([text])[0])
        ids[tag] = store.add_memory(rec)
        entity = entity_ids["alphasvc" if tag == "m1" else "gammasvc"]
        store.link_memory_entity(
            memory_id=ids[tag],
            entity_id=entity,
            workspace_id=wid,
            repo_id=rid,
            source_kind="test",
        )
    return store, wid, emb, index, ids


def test_ppr_arm_surfaces_multi_hop_memory():
    store, wid, emb, index, ids = _graph_fixture()
    flt = SearchFilter(workspace_id=wid)
    eng_ppr = RecallEngine(store, emb, index, IdentityReranker(), graph_mode="ppr")
    eng_1hop = RecallEngine(store, emb, index, IdentityReranker(), graph_mode="1hop")
    from engraphis.core.store import now_ts
    now = now_ts()

    ppr_scores = eng_ppr._graph_arm("what does alphasvc depend on", flt, now)
    hop_scores = eng_1hop._graph_arm("what does alphasvc depend on", flt, now)

    # gammasvc is two hops from the seed: PPR sees the memory that mentions it,
    # 1-hop expansion cannot.
    assert ids["m3"] in ppr_scores
    assert ids["m3"] not in hop_scores
    # And the directly-mentioning memory outranks the associative one.
    assert ppr_scores[ids["m1"]] > ppr_scores[ids["m3"]]
    store.close()


def test_ppr_arm_returns_empty_without_seed_entities():
    store, wid, emb, index, _ = _graph_fixture()
    eng = RecallEngine(store, emb, index, IdentityReranker())
    from engraphis.core.store import now_ts
    assert eng._graph_arm("nothing here matches", SearchFilter(workspace_id=wid), now_ts()) == {}
    store.close()


def test_1hop_arm_honors_graph_layer_filter():
    """`graph_mode="1hop"` (also the PPR big-graph fallback) must respect
    `SearchFilter.graph_layers` like the PPR arm does: a temporal-only intent
    may not expand the seed through entity/causal edges (PR #19 follow-up)."""
    from engraphis.core.interfaces import GraphLayer
    from engraphis.core.store import now_ts

    store = Store(":memory:")
    wid = store.get_or_create_workspace("w")
    emb = DeterministicEmbedder(dim=64)
    index = NumpyVectorIndex(store)
    a = store.upsert_entity(Node(id="", name="alphasvc", ntype="service", workspace_id=wid))
    b = store.upsert_entity(Node(id="", name="betasvc", ntype="service", workspace_id=wid))
    # "calls" classifies as the ENTITY overlay.
    store.upsert_edge(Edge(id="", src=a, dst=b, relation="calls", workspace_id=wid))
    text = "betasvc publishes the audit events."
    mid = store.add_memory(MemoryRecord(
        id="", content=text, mtype=MemoryType.SEMANTIC, scope=Scope.WORKSPACE,
        workspace_id=wid, embedding=emb.embed([text])[0],
    ))
    store.link_memory_entity(
        memory_id=mid, entity_id=b, workspace_id=wid, repo_id=None,
        source_kind="test",
    )
    eng = RecallEngine(store, emb, index, IdentityReranker(), graph_mode="1hop")
    now = now_ts()

    unrestricted = eng._graph_arm("alphasvc status", SearchFilter(workspace_id=wid), now)
    assert mid in unrestricted  # entity edge expands alphasvc → betasvc

    temporal_only = eng._graph_arm(
        "alphasvc status",
        SearchFilter(workspace_id=wid, graph_layers=[GraphLayer.TEMPORAL]), now,
    )
    assert mid not in temporal_only  # the entity edge is outside the overlay
    store.close()


def test_ppr_mem_links_obey_known_at_and_empty_layer_filter():
    store = Store(":memory:")
    wid = store.get_or_create_workspace("w")
    emb = DeterministicEmbedder(dim=64)
    index = NumpyVectorIndex(store)
    alpha = store.upsert_entity(Node(
        id="", name="alphasvc", ntype="service", workspace_id=wid
    ))
    beta = store.upsert_entity(Node(
        id="", name="betasvc", ntype="service", workspace_id=wid
    ))
    first = store.add_memory(MemoryRecord(
        id="", content="alphasvc entry point", workspace_id=wid,
        scope=Scope.WORKSPACE, valid_from=10.0, ingested_at=10.0,
    ))
    second = store.add_memory(MemoryRecord(
        id="", content="betasvc ledger", workspace_id=wid,
        scope=Scope.WORKSPACE, valid_from=10.0, ingested_at=10.0,
    ))
    store.link_memory_entity(
        memory_id=first, entity_id=alpha, workspace_id=wid, repo_id=None,
        valid_from=10.0, ingested_at=10.0,
    )
    store.link_memory_entity(
        memory_id=second, entity_id=beta, workspace_id=wid, repo_id=None,
        valid_from=10.0, ingested_at=10.0,
    )
    store.add_link(first, second)
    store.conn.execute(
        "UPDATE mem_links SET created_at=100, valid_from=100, ingested_at=100 "
        "WHERE a=? AND b=?", (first, second)
    )
    store.conn.commit()
    engine = RecallEngine(store, emb, index, IdentityReranker())

    before = engine._graph_arm(
        "alphasvc",
        SearchFilter(workspace_id=wid, valid_at=50.0, known_at=50.0),
        50.0,
    )
    after = engine._graph_arm(
        "alphasvc",
        SearchFilter(workspace_id=wid, valid_at=150.0, known_at=150.0),
        150.0,
    )
    disabled = engine._graph_arm(
        "alphasvc",
        SearchFilter(workspace_id=wid, graph_layers=[]),
        150.0,
    )

    assert first in before and second not in before
    assert second in after
    assert disabled == {}
    store.close()


def test_recall_end_to_end_with_ppr_default():
    store, wid, emb, index, ids = _graph_fixture()
    eng = RecallEngine(store, emb, index, IdentityReranker())
    assert eng.graph_mode == "ppr"
    res = eng.recall("alphasvc login", SearchFilter(workspace_id=wid), k=5)
    assert res.count >= 1
    assert any(c["id"] == ids["m1"] for c in res.chunks)
    store.close()
