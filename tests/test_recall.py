from engraphis.backends import DeterministicEmbedder, NumpyVectorIndex
from engraphis.backends.reranker import IdentityReranker
from engraphis.core.interfaces import MemoryRecord, SearchFilter
from engraphis.core.recall import RecallEngine
from engraphis.core.store import Store


def _engine():
    store = Store(":memory:")
    emb = DeterministicEmbedder(256)
    eng = RecallEngine(store, emb, NumpyVectorIndex(store), IdentityReranker())
    return store, emb, eng


def _add(store, emb, wid, rid, text, **kw):
    return store.add_memory(MemoryRecord(id="", content=text, workspace_id=wid, repo_id=rid,
                                         embedding=emb.embed([text])[0], **kw))


def test_recall_returns_relevant_first():
    store, emb, eng = _engine()
    wid = store.get_or_create_workspace("w")
    rid = store.get_or_create_repo(wid, "r")
    _add(store, emb, wid, rid, "We standardized on pnpm as the package manager.")
    _add(store, emb, wid, rid, "The sky over the harbor was a pale shade of blue.")
    res = eng.recall("which package manager do we use?", SearchFilter(workspace_id=wid), k=2)
    assert res.count >= 1
    assert "pnpm" in res.context.lower()


def test_recall_scope_isolation():
    store, emb, eng = _engine()
    wid = store.get_or_create_workspace("w")
    r1 = store.get_or_create_repo(wid, "repo1")
    r2 = store.get_or_create_repo(wid, "repo2")
    _add(store, emb, wid, r1, "repo1 authenticates with PASETO.")
    _add(store, emb, wid, r2, "repo2 authenticates with JWT.")
    res = eng.recall("authentication", SearchFilter(workspace_id=wid, repo_id=r1), k=5)
    assert res.count >= 1
    assert all(c["repo_id"] == r1 for c in res.chunks)


def test_recall_bitemporal_excludes_invalidated_fact():
    store, emb, eng = _engine()
    wid = store.get_or_create_workspace("w")
    rid = store.get_or_create_repo(wid, "r")
    old = _add(store, emb, wid, rid, "We use JWT for authentication.")
    store.close_validity(old)  # contradicted by new info
    _add(store, emb, wid, rid, "We use PASETO for authentication.")
    res = eng.recall("what do we use for authentication?", SearchFilter(workspace_id=wid), k=5)
    assert old not in [c["id"] for c in res.chunks]


def test_recall_reinforces_returned_memories():
    store, emb, eng = _engine()
    wid = store.get_or_create_workspace("w")
    rid = store.get_or_create_repo(wid, "r")
    mid = _add(store, emb, wid, rid, "pnpm is our package manager.")
    before = store.get_memory(mid).access_count
    eng.recall("package manager", SearchFilter(workspace_id=wid), k=1)
    assert store.get_memory(mid).access_count > before


def test_graph_arm_pulls_related_via_entities():
    from engraphis.core.interfaces import Edge, Node
    store, emb, eng = _engine()
    wid = store.get_or_create_workspace("w")
    rid = store.get_or_create_repo(wid, "r")
    # Entity graph: Redis —used_by→ checkout
    redis = store.upsert_entity(Node(id="", name="Redis", ntype="tech",
                                     workspace_id=wid, repo_id=rid))
    checkout = store.upsert_entity(Node(id="", name="checkout", ntype="module",
                                        workspace_id=wid, repo_id=rid))
    store.upsert_edge(Edge(id="", src=redis, dst=checkout, relation="used_by",
                           workspace_id=wid, repo_id=rid))
    _add(store, emb, wid, rid, "The checkout service had a race condition.")
    _add(store, emb, wid, rid, "Totally unrelated note about office plants.")
    # Query mentions Redis; graph arm should surface the checkout memory.
    res = eng.recall("how does Redis relate to things?", SearchFilter(workspace_id=wid), k=3)
    assert any("checkout" in c["content"].lower() for c in res.chunks)



def test_lexical_recall_is_filtered_before_candidate_limit():
    store, emb, eng = _engine()
    target = store.get_or_create_workspace("target")
    other = store.get_or_create_workspace("other")
    for i in range(60):
        _add(store, emb, other, None, f"needle belongs elsewhere {i}")
    wanted = _add(store, emb, target, None, "needle belongs in the target workspace")

    res = eng.recall("needle", SearchFilter(workspace_id=target), k=3, candidate_k=10)
    assert [c["id"] for c in res.chunks] == [wanted]


def test_graph_arm_does_not_match_entity_names_inside_other_words():
    from engraphis.core.interfaces import Edge, Node

    store, emb, eng = _engine()
    wid = store.get_or_create_workspace("w")
    rid = store.get_or_create_repo(wid, "r")
    redis = store.upsert_entity(Node(
        id="", name="Redis", ntype="tech", workspace_id=wid, repo_id=rid))
    checkout = store.upsert_entity(Node(
        id="", name="checkout", ntype="module", workspace_id=wid, repo_id=rid))
    store.upsert_edge(Edge(
        id="", src=redis, dst=checkout, relation="used_by",
        workspace_id=wid, repo_id=rid))
    related = _add(
        store, emb, wid, rid, "The checkout service had a race condition.")

    scores = eng._graph_arm_ppr(
        "we rediscovered an old archive",
        SearchFilter(workspace_id=wid, repo_id=rid),
        now=10**12)

    assert related not in scores