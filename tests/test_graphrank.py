import numpy as np  # noqa: F401  (asserts numpy-only dependency stays importable)

from engraphis.backends import DeterministicEmbedder, NumpyVectorIndex
from engraphis.backends.reranker import IdentityReranker
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


def test_recall_end_to_end_with_ppr_default():
    store, wid, emb, index, ids = _graph_fixture()
    eng = RecallEngine(store, emb, index, IdentityReranker())
    assert eng.graph_mode == "ppr"
    res = eng.recall("alphasvc login", SearchFilter(workspace_id=wid), k=5)
    assert res.count >= 1
    assert any(c["id"] == ids["m1"] for c in res.chunks)
    store.close()
