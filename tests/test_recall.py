from engraphis.backends import DeterministicEmbedder, NumpyVectorIndex
from engraphis.backends.reranker import IdentityReranker
from engraphis.core.interfaces import MemoryRecord, Scope, SearchFilter
from engraphis.core.recall import RecallEngine
from engraphis.core.retrieval_policy import ProfileConfig
from engraphis.core.store import Store


def _engine():
    store = Store(":memory:")
    emb = DeterministicEmbedder(256)
    eng = RecallEngine(store, emb, NumpyVectorIndex(store), IdentityReranker())
    return store, emb, eng


def _add(store, emb, wid, rid, text, **kw):
    return store.add_memory(MemoryRecord(id="", content=text, workspace_id=wid, repo_id=rid,
                                         embedding=emb.embed([text])[0], **kw))


class _OrderedIndex:
    """Minimal index double which keeps untrusted candidates ahead of trusted ones."""

    def __init__(self, ids):
        self.ids = ids

    def search(self, query, k, *, filter=None):
        return [
            (memory_id, float(len(self.ids) - position))
            for position, memory_id in enumerate(self.ids[:k])
        ]


class _RecordingOrderedIndex(_OrderedIndex):
    def __init__(self, ids):
        super().__init__(ids)
        self.requested: list[int] = []

    def search(self, query, k, *, filter=None):
        self.requested.append(k)
        return super().search(query, k, filter=filter)


def test_recall_returns_relevant_first():
    store, emb, eng = _engine()
    wid = store.get_or_create_workspace("w")
    rid = store.get_or_create_repo(wid, "r")
    _add(store, emb, wid, rid, "We standardized on pnpm as the package manager.")
    _add(store, emb, wid, rid, "The sky over the harbor was a pale shade of blue.")
    res = eng.recall("which package manager do we use?", SearchFilter(workspace_id=wid), k=2)
    assert res.count >= 1
    assert "pnpm" in res.context.lower()


def test_lexical_absolute_support_includes_title_text():
    store, emb, eng = _engine()
    wid = store.get_or_create_workspace("w")
    rid = store.get_or_create_repo(wid, "r")
    memory_id = _add(
        store,
        emb,
        wid,
        rid,
        "Rotate it every 30 days.",
        title="OAUTH_TOKEN_ROTATION",
    )

    result = eng.recall(
        "OAUTH_TOKEN_ROTATION",
        SearchFilter(workspace_id=wid, repo_id=rid),
        k=1,
        retrieval_profile="lexical",
    )

    assert [chunk["id"] for chunk in result.chunks] == [memory_id]
    assert result.chunks[0]["absolute_support"] > 0.0


def test_prompt_only_recall_continues_past_untrusted_arm_candidates():
    store = Store(":memory:")
    emb = DeterministicEmbedder(256)
    wid = store.get_or_create_workspace("w")
    rid = store.get_or_create_repo(wid, "r")
    untrusted_ids = [
        _add(
            store, emb, wid, rid, f"Untrusted candidate {index}.",
            provenance={"source": "import", "trusted": False},
        )
        for index in range(201)
    ]
    trusted_id = _add(
        store, emb, wid, rid, "Trusted project evidence.",
        provenance={"source": "agent", "trusted": True},
    )
    eng = RecallEngine(
        store, emb, _OrderedIndex([*untrusted_ids, trusted_id]), IdentityReranker(),
    )

    result = eng.recall(
        "project evidence", SearchFilter(workspace_id=wid, repo_id=rid), k=1,
        prompt_only=True,
        arm_config=ProfileConfig("vector_only", True, False, False, False),
    )

    assert [chunk["id"] for chunk in result.chunks] == [trusted_id]


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


def test_recall_is_observational_by_default():
    store, emb, eng = _engine()
    wid = store.get_or_create_workspace("w")
    rid = store.get_or_create_repo(wid, "r")
    mid = _add(store, emb, wid, rid, "pnpm is our package manager.")
    before = store.get_memory(mid).access_count
    eng.recall("package manager", SearchFilter(workspace_id=wid), k=1)
    assert store.get_memory(mid).access_count == before


def test_recall_can_reinforce_when_use_is_explicit():
    store, emb, eng = _engine()
    wid = store.get_or_create_workspace("w")
    rid = store.get_or_create_repo(wid, "r")
    mid = _add(store, emb, wid, rid, "pnpm is our package manager.")
    before = store.get_memory(mid).access_count
    eng.recall(
        "package manager",
        SearchFilter(workspace_id=wid),
        k=1,
        reinforce=True,
    )
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


def test_graph_arm_selects_seed_frontier_edges_before_global_edge_cap(monkeypatch):
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
    memory_id = _add(store, emb, wid, rid, "Checkout uses the Redis cache.")
    store.link_memory_entity(
        memory_id=memory_id, entity_id=checkout, workspace_id=wid, repo_id=rid,
        source_kind="test", confidence=1.0,
    )
    def no_global_edges(*_args, **_kwargs):
        raise AssertionError("PPR must traverse from query seeds, not global edges")

    monkeypatch.setattr(store, "edges_in_scope", no_global_edges)

    scores = eng._graph_arm_ppr(
        "How does Redis relate to the checkout service?",
        SearchFilter(workspace_id=wid, repo_id=rid), now=10**12,
    )

    assert memory_id in scores


def test_graph_arm_filters_incidence_to_ppr_frontier_before_cap(monkeypatch):
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
    memory_id = _add(store, emb, wid, rid, "Checkout uses the Redis cache.")
    store.link_memory_entity(
        memory_id=memory_id, entity_id=checkout, workspace_id=wid, repo_id=rid,
        source_kind="test", confidence=1.0,
    )

    real_list_memory_entities = store.list_memory_entities
    global_prefix = [
        {"id": f"inc_{index}", "memory_id": f"mem_{index}",
         "entity_id": f"ent_{index}", "confidence": 1.0}
        for index in range(12_000)
    ]

    def list_memory_entities(flt, *, entity_ids=None, memory_ids=None, limit=None):
        # This models a crowded global prefix which does not contain checkout's
        # incidence. The real target remains available when constrained first.
        if entity_ids is None:
            return global_prefix[:limit]
        return real_list_memory_entities(
            flt, entity_ids=entity_ids, memory_ids=memory_ids, limit=limit,
        )

    monkeypatch.setattr(store, "list_memory_entities", list_memory_entities)

    scores = eng._graph_arm_ppr(
        "How does Redis relate to the checkout service?",
        SearchFilter(workspace_id=wid, repo_id=rid), now=10**12,
    )

    assert memory_id in scores


def test_graph_arm_backfills_text_memory_when_its_entity_is_added_later():
    from engraphis.core.interfaces import Edge, Node

    store, emb, eng = _engine()
    wid = store.get_or_create_workspace("w")
    rid = store.get_or_create_repo(wid, "r")
    related = _add(
        store, emb, wid, rid, "The checkout service had a race condition.")
    redis = store.upsert_entity(Node(
        id="", name="Redis", ntype="tech", workspace_id=wid, repo_id=rid))
    checkout = store.upsert_entity(Node(
        id="", name="checkout", ntype="module", workspace_id=wid, repo_id=rid))
    store.upsert_edge(Edge(
        id="", src=redis, dst=checkout, relation="used_by",
        workspace_id=wid, repo_id=rid))

    scores = eng._graph_arm_ppr(
        "How does Redis relate to things?",
        SearchFilter(workspace_id=wid, repo_id=rid), now=10**12,
    )

    assert related in scores


def test_graph_arm_traverses_links_to_memories_without_entity_incidence():
    from engraphis.core.interfaces import Node

    store, emb, eng = _engine()
    wid = store.get_or_create_workspace("w")
    rid = store.get_or_create_repo(wid, "r")
    redis = store.upsert_entity(Node(
        id="", name="Redis", ntype="tech", workspace_id=wid, repo_id=rid,
    ))
    attached = _add(store, emb, wid, rid, "Redis owns the cache migration.")
    linked_only = _add(store, emb, wid, rid, "The migration requires a staged rollout.")
    store.link_memory_entity(
        memory_id=attached, entity_id=redis, workspace_id=wid, repo_id=rid,
        source_kind="test", confidence=1.0,
    )
    store.add_link(attached, linked_only, relation="supports")

    scores = eng._graph_arm_ppr(
        "What does Redis own?", SearchFilter(workspace_id=wid, repo_id=rid), now=10**12,
    )

    assert linked_only in scores


def test_graph_arm_backfills_workspace_mentions_for_a_later_repo_entity():
    from engraphis.core.interfaces import Edge, Node

    store, emb, eng = _engine()
    wid = store.get_or_create_workspace("w")
    rid = store.get_or_create_repo(wid, "r")
    related = _add(
        store, emb, wid, None, "The checkout service had a race condition.",
        scope=Scope.WORKSPACE,
    )
    redis = store.upsert_entity(Node(
        id="", name="Redis", ntype="tech", workspace_id=wid, repo_id=rid))
    checkout = store.upsert_entity(Node(
        id="", name="checkout", ntype="module", workspace_id=wid, repo_id=rid))
    store.upsert_edge(Edge(
        id="", src=redis, dst=checkout, relation="used_by",
        workspace_id=wid, repo_id=rid))
    flt = SearchFilter(workspace_id=wid, repo_id=rid, include_ancestors=True)

    incidence = store.list_memory_entities(flt, entity_ids=[checkout])
    assert [(row["memory_id"], row["repo_id"]) for row in incidence] == [(related, None)]
    assert related in eng._graph_arm_ppr(
        "How does Redis relate to things?", flt, now=10**12,
    )


def test_graph_arm_expands_an_older_unmentioned_link_endpoint_from_incidence(monkeypatch):
    from engraphis.core.interfaces import Node

    store, emb, eng = _engine()
    wid = store.get_or_create_workspace("w")
    rid = store.get_or_create_repo(wid, "r")
    redis = store.upsert_entity(Node(
        id="", name="Redis", ntype="technology", workspace_id=wid, repo_id=rid,
    ))
    attached = _add(store, emb, wid, rid, "The cache migration is attached evidence.")
    older_unmentioned = _add(store, emb, wid, rid, "The old rollout required a staged cutover.")
    store.link_memory_entity(
        memory_id=attached, entity_id=redis, workspace_id=wid, repo_id=rid,
        source_kind="test", confidence=1.0,
    )
    store.add_link(attached, older_unmentioned, relation="supports")

    # Simulate a full scope whose bounded newest-memory window excludes the older
    # endpoint. The incidence frontier still contains ``attached``.
    monkeypatch.setattr(
        store,
        "list_memories",
        lambda *_args, **_kwargs: [
            MemoryRecord(id=f"mem_new_{i}", content="") for i in range(12_000)
        ],
    )

    scores = eng._graph_arm_ppr(
        "How does Redis relate to the rollout?",
        SearchFilter(workspace_id=wid, repo_id=rid), now=10**12,
    )

    assert older_unmentioned in scores


def test_entity_backfill_preserves_closed_workspace_memory_history():
    from engraphis.core.interfaces import Node

    store, _emb, _eng = _engine()
    wid = store.get_or_create_workspace("w")
    rid = store.get_or_create_repo(wid, "r")
    historical = store.add_memory(MemoryRecord(
        id="", content="The checkout service had a race condition.",
        workspace_id=wid, scope=Scope.WORKSPACE, valid_from=100.0, valid_to=200.0,
        valid_to_recorded_at=300.0, ingested_at=100.0,
    ))
    checkout = store.upsert_entity(Node(
        id="", name="checkout", ntype="module", workspace_id=wid, repo_id=rid))

    visible = store.list_memory_entities(SearchFilter(
        workspace_id=wid, repo_id=rid, include_ancestors=True,
        valid_at=150.0, known_at=250.0,
    ), entity_ids=[checkout])
    assert [(row["memory_id"], row["repo_id"]) for row in visible] == [(historical, None)]



def test_lexical_recall_is_filtered_before_candidate_limit():
    store, emb, eng = _engine()
    target = store.get_or_create_workspace("target")
    other = store.get_or_create_workspace("other")
    for i in range(60):
        _add(store, emb, other, None, f"needle belongs elsewhere {i}")
    wanted = _add(store, emb, target, None, "needle belongs in the target workspace")

    res = eng.recall("needle", SearchFilter(workspace_id=target), k=3, candidate_k=10)
    assert [c["id"] for c in res.chunks] == [wanted]


def test_prompt_overfetch_never_reduces_the_requested_candidate_depth():
    store = Store(":memory:")
    emb = DeterministicEmbedder(256)
    index = NumpyVectorIndex(store)
    requested: list[int] = []
    original_search = index.search

    def recording_search(query, k, filter=None):
        requested.append(k)
        return original_search(query, k, filter=filter)

    index.search = recording_search
    eng = RecallEngine(store, emb, index, IdentityReranker())
    wid = store.get_or_create_workspace("w")
    _add(store, emb, wid, None, "A sufficiently deep candidate set remains available.")

    result = eng.recall(
        "candidate depth", SearchFilter(workspace_id=wid), k=1, candidate_k=500,
    )

    assert result.candidate_k_requested == 500
    assert result.candidate_k_used == 500
    assert requested[0] == 750


def test_prompt_only_overfetch_stays_bounded_for_large_untrusted_scopes():
    store = Store(":memory:")
    emb = DeterministicEmbedder(256)
    wid = store.get_or_create_workspace("w")
    untrusted_ids = [
        _add(
            store,
            emb,
            wid,
            None,
            f"untrusted imported evidence {index}",
            metadata={"provenance": {"source": "web", "trusted": False}},
        )
        for index in range(300)
    ]
    index = _RecordingOrderedIndex(untrusted_ids)
    eng = RecallEngine(store, emb, index, IdentityReranker())

    result = eng.recall(
        "project evidence",
        SearchFilter(workspace_id=wid),
        k=1,
        candidate_k=1,
        prompt_only=True,
        arm_config=ProfileConfig("vector_only", True, False, False, False),
    )

    assert result.chunks == []
    assert index.requested == [4, 256]
    assert max(index.requested) < len(untrusted_ids)


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

# ── regression: batched candidate lookup + deterministic tie ordering ─────────

def test_recall_resolves_candidates_in_one_batched_lookup(monkeypatch):
    """Candidates used to be resolved with a get_memory() per unique id across the
    vec/lex/graph arms — ~150 single-row queries per recall."""
    store, emb, eng = _engine()
    wid = store.get_or_create_workspace("w")
    rid = store.get_or_create_repo(wid, "r")
    for i in range(12):
        _add(store, emb, wid, rid, "deployment note number %d about caching" % i)

    single = []
    monkeypatch.setattr(store, "get_memory", lambda mid: single.append(mid))
    batched = []
    real_get_memories = store.get_memories
    monkeypatch.setattr(store, "get_memories",
                        lambda ids: (batched.append(list(ids)), real_get_memories(ids))[1])

    res = eng.recall("caching", SearchFilter(workspace_id=wid), k=5, reinforce=False)

    assert res.count >= 1
    assert single == []                       # no per-id query on the recall path
    assert len(batched) == 1                  # exactly one batched resolve


def test_recall_tie_order_is_deterministic():
    """Candidates come from set(vec) | set(lex) | set(graph); set iteration order varies
    with PYTHONHASHSEED, so equal-scored results reordered across processes."""
    store, emb, eng = _engine()
    wid = store.get_or_create_workspace("w")
    rid = store.get_or_create_repo(wid, "r")
    # Identical content => identical scores => ordering is decided purely by the
    # tiebreak, which must be the id and not set/hash iteration order.
    for _ in range(8):
        _add(store, emb, wid, rid, "the release checklist is in the runbook")

    flt = SearchFilter(workspace_id=wid)
    runs = [[c["id"] for c in eng.recall("release checklist runbook", flt, k=8,
                                         reinforce=False).chunks]
            for _ in range(5)]

    assert all(r == runs[0] for r in runs)
    tied = eng.recall("release checklist runbook", flt, k=8, reinforce=False).chunks
    top = max(c["score"] for c in tied)
    tied_ids = [c["id"] for c in tied if c["score"] == top]
    assert tied_ids == sorted(tied_ids)       # equal scores order by id, ascending
