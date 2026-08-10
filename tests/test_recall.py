from types import SimpleNamespace

from engraphis.backends import DeterministicEmbedder, NumpyVectorIndex
from engraphis.backends.reranker import IdentityReranker
from engraphis.core.interfaces import MemoryRecord, MemoryType, Scope, SearchFilter
from engraphis.core.recall import (
    RecallEngine,
    _absolute_retrieval_support,
    _fuse_query_runs,
    _mtype_limits_can_fill,
    _ranked,
)
from engraphis.core.retrieval_policy import ProfileConfig
from engraphis.core.store import Store


class _SemanticTestEmbedder(DeterministicEmbedder):
    """Test double that opts into vector semantics without a model download."""

    supports_semantic_search = True
    embedding_mode = "semantic"


def test_prompt_candidate_expansion_accounts_for_memory_type_caps():
    semantic = MemoryRecord(id="mem_semantic", content="", mtype=MemoryType.SEMANTIC)
    procedural = MemoryRecord(id="mem_procedural", content="", mtype=MemoryType.PROCEDURAL)
    limits = {MemoryType.SEMANTIC: 0}

    assert not _mtype_limits_can_fill({semantic.id: semantic}, limits, 1)
    assert _mtype_limits_can_fill(
        {semantic.id: semantic, procedural.id: procedural}, limits, 1,
    )


def _engine():
    store = Store(":memory:")
    emb = DeterministicEmbedder(256)
    eng = RecallEngine(store, emb, NumpyVectorIndex(store), IdentityReranker())
    return store, emb, eng


def _add(store, emb, wid, rid, text, **kw):
    # These direct Store fixtures model locally approved test data. Public ingress
    # coverage uses MemoryService and must remain pending until review.
    provenance = dict(kw.get("provenance") or {
        "source": "test", "trusted": True, "review_state": "approved",
    })
    if provenance.get("trusted") is True:
        provenance.setdefault("review_state", "approved")
    kw["provenance"] = provenance
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


class _FixedScoreIndex:
    """Semantic-index double for calibration regressions."""

    def __init__(self, scores):
        self.scores = list(scores)

    def search(self, query, k, *, filter=None):
        return self.scores[:k]


class _FailingIndex:
    """Proves degraded recall never reaches the semantic vector backend."""

    def __init__(self):
        self.calls = 0

    def search(self, query, k, *, filter=None):
        self.calls += 1
        raise AssertionError("degraded recall must not query the vector index")


class _RuntimeFailingIndex:
    def search(self, query, k, *, filter=None):
        raise RuntimeError("credentialed-provider-detail")


def test_ranked_drops_nonfinite_and_malformed_arm_evidence():
    recs = {memory_id: object() for memory_id in ("good", "nan", "inf", "bad")}
    assert _ranked({
        "nan": float("nan"),
        "inf": float("inf"),
        "bad": "not-a-score",
        "good": 0.5,
    }, recs) == ["good"]
    assert _ranked({"nan": float("-inf"), "bad": None}, recs) == []

def test_recall_returns_relevant_first():
    store, emb, eng = _engine()
    wid = store.get_or_create_workspace("w")
    rid = store.get_or_create_repo(wid, "r")
    _add(store, emb, wid, rid, "We standardized on pnpm as the package manager.")
    _add(store, emb, wid, rid, "The sky over the harbor was a pale shade of blue.")
    res = eng.recall("which package manager do we use?", SearchFilter(workspace_id=wid), k=2)
    assert res.count >= 1
    assert "pnpm" in res.context.lower()


def test_degraded_recall_skips_vector_arm_and_uses_lexical_fallback():
    store = Store(":memory:")
    emb = DeterministicEmbedder(256)
    index = _FailingIndex()
    eng = RecallEngine(store, emb, index, IdentityReranker())
    wid = store.get_or_create_workspace("w")
    _add(store, emb, wid, None, "pnpm is the package manager for frontend projects.")

    result = eng.recall(
        "package manager", SearchFilter(workspace_id=wid), k=1, diagnostics=True,
    )

    assert index.calls == 0
    assert result.degraded_mode is True
    assert result.semantic_support is False
    assert result.chunks[0]["arm"] == "lexical"
    assert result.retrieval_trace[0]["raw"]["semantic"] is None


def test_semantic_index_runtime_failure_preserves_lexical_recall_and_is_redacted(caplog):
    store = Store(":memory:")
    emb = _SemanticTestEmbedder(256)
    eng = RecallEngine(store, emb, _RuntimeFailingIndex(), IdentityReranker())
    wid = store.get_or_create_workspace("w")
    memory_id = _add(
        store, emb, wid, None,
        "pnpm is the package manager for frontend projects.",
    )

    with caplog.at_level("WARNING", logger="engraphis.core.recall"):
        result = eng.recall(
            "package manager", SearchFilter(workspace_id=wid), k=1,
            diagnostics=True,
        )

    assert result.chunks[0]["id"] == memory_id
    assert result.degraded_mode is True
    # The semantic embedder remains usable for exact support scoring; only the
    # retrieval index failed for this request.
    assert result.semantic_support is True
    assert result.vector_search_ready is False
    assert result.retrieval_trace[0]["raw"]["semantic"] is None
    assert "RuntimeError" in caplog.text
    assert "credentialed-provider-detail" not in caplog.text


def test_semantic_query_embedding_failure_preserves_lexical_recall_and_is_redacted(caplog):
    class RuntimeFailingSemanticEmbedder(_SemanticTestEmbedder):
        def embed(self, texts, **kwargs):
            if len(texts) == 1 and texts[0] == "package manager":
                raise RuntimeError("embedding-provider-secret")
            return super().embed(texts, **kwargs)

    store = Store(":memory:")
    emb = RuntimeFailingSemanticEmbedder(256)
    wid = store.get_or_create_workspace("w")
    memory_id = _add(
        store, emb, wid, None,
        "pnpm is the package manager for frontend projects.",
    )
    eng = RecallEngine(
        store,
        emb,
        NumpyVectorIndex(store),
        IdentityReranker(),
    )

    with caplog.at_level("WARNING", logger="engraphis.core.recall"):
        result = eng.recall(
            "package manager",
            SearchFilter(workspace_id=wid),
            k=1,
            diagnostics=True,
        )

    assert result.chunks[0]["id"] == memory_id
    assert result.degraded_mode is True
    assert result.semantic_support is False
    assert result.vector_search_ready is False
    assert result.retrieval_trace is not None
    assert result.retrieval_trace[0]["raw"]["semantic"] is None
    assert "RuntimeError" in caplog.text
    assert "embedding-provider-secret" not in caplog.text


def test_reranker_mutate_then_raise_uses_pristine_fused_fallback(caplog):
    class MutatingFailingReranker:
        def rerank(self, query, candidates, k):
            for candidate in candidates:
                candidate.score = 999_999.0
            raise RuntimeError("private-reranker-detail")

    store = Store(":memory:")
    emb = DeterministicEmbedder(256)
    eng = RecallEngine(store, emb, NumpyVectorIndex(store), MutatingFailingReranker())
    wid = store.get_or_create_workspace("w")
    memory_id = _add(
        store, emb, wid, None,
        "pnpm is the package manager for frontend projects.",
    )

    with caplog.at_level("WARNING", logger="engraphis.core.recall"):
        result = eng.recall(
            "package manager", SearchFilter(workspace_id=wid), k=1,
            diagnostics=True,
        )

    assert result.chunks[0]["id"] == memory_id
    assert result.chunks[0]["score"] < 999_999.0
    assert result.retrieval_trace[0]["rerank_score"] is None
    assert "RuntimeError" in caplog.text
    assert "private-reranker-detail" not in caplog.text


def test_reranker_malformed_output_uses_pristine_fused_fallback(caplog):
    class MutatingMalformedReranker:
        def rerank(self, query, candidates, k):
            for candidate in candidates:
                candidate.score = 888_888.0
            return [object()]

    store = Store(":memory:")
    emb = DeterministicEmbedder(256)
    eng = RecallEngine(store, emb, NumpyVectorIndex(store), MutatingMalformedReranker())
    wid = store.get_or_create_workspace("w")
    memory_id = _add(
        store, emb, wid, None,
        "Poetry manages dependencies for backend projects.",
    )

    with caplog.at_level("WARNING", logger="engraphis.core.recall"):
        result = eng.recall(
            "backend dependencies", SearchFilter(workspace_id=wid), k=1,
            diagnostics=True,
        )

    assert result.chunks[0]["id"] == memory_id
    assert result.chunks[0]["score"] < 888_888.0
    assert result.retrieval_trace[0]["rerank_score"] is None
    assert "reranker returned no valid candidates" in caplog.text


def test_degraded_recall_uses_inflection_aware_like_fallback_without_fts5():
    store = Store(":memory:")
    store.has_fts5 = False
    emb = DeterministicEmbedder(256)
    eng = RecallEngine(store, emb, _FailingIndex(), IdentityReranker())
    wid = store.get_or_create_workspace("w")
    _add(store, emb, wid, None, "The service authenticates API requests with PASETO.")

    result = eng.recall("authentication", SearchFilter(workspace_id=wid), k=1)

    assert result.count == 1
    assert "paseto" in result.context.lower()


def test_lexical_absolute_support_does_not_allow_title_only_evidence():
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
    assert result.chunks[0]["absolute_support"] == 0.0


def test_absolute_support_treats_non_finite_cosine_as_no_evidence():
    assert _absolute_retrieval_support(
        "credential rotation", "unrelated prose", title="credential rotation",
        semantic_cosine=float("nan"),
    ) == 0.0
    assert _absolute_retrieval_support(
        "credential rotation", "unrelated prose", title="credential rotation",
        semantic_cosine=10 ** 1000,
    ) == 0.0


def test_opt_in_semantic_confidence_calibration_rejects_weak_singleton_distractor():
    """Default rank fusion is unchanged; the explicit calibration is safer.

    A single vector hit normally min-max normalizes to 1.0. Its raw cosine is
    nevertheless only 0.01 here, while the other record has exact lexical
    support. The controlled flag must use the former as weak evidence without
    changing the established default profile behavior.
    """
    store = Store(":memory:")
    emb = _SemanticTestEmbedder(256)
    wid = store.get_or_create_workspace("w")
    weak_id = _add(store, emb, wid, None, "The parking garage closes at dusk.")
    lexical_id = _add(store, emb, wid, None, "PASETO is the approved token format.")
    engine = RecallEngine(
        store,
        emb,
        _FixedScoreIndex([(weak_id, 0.01)]),
        IdentityReranker(),
    )
    base_config = ProfileConfig("vector_lexical", True, True, False, False)

    default_result = engine.recall(
        "PASETO", SearchFilter(workspace_id=wid), k=1, arm_config=base_config,
    )
    calibrated_result = engine.recall(
        "PASETO",
        SearchFilter(workspace_id=wid),
        k=1,
        arm_config=ProfileConfig(
            "vector_lexical_calibrated",
            True,
            True,
            False,
            False,
            semantic_confidence_calibration=True,
        ),
    )

    assert [chunk["id"] for chunk in default_result.chunks] == [weak_id]
    assert [chunk["id"] for chunk in calibrated_result.chunks] == [lexical_id]


def test_semantic_confidence_calibration_leaves_presence_bonus_explicit():
    """Future semantic bonuses are not silently attenuated by cosine confidence."""
    config = SimpleNamespace(
        semantic_scale=1.0,
        semantic_presence_bonus=0.4,
        semantic_confidence_calibration=True,
    )
    run = {
        "query": SimpleNamespace(priority=1),
        "config": config,
        "vector": {"mem_weak": 0.1},
    }
    recs = {"mem_weak": MemoryRecord(id="mem_weak", content="weak")}

    state, _rrf = _fuse_query_runs([run], recs)

    # The rank contribution is calibrated (1.0 * 0.1); the explicit bonus is
    # then added as a distinct piece of configuration intent.
    assert state["adjusted"]["semantic"]["mem_weak"] == 0.5


def test_prompt_only_recall_continues_past_untrusted_arm_candidates():
    store = Store(":memory:")
    emb = _SemanticTestEmbedder(256)
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
    checkout_memory = _add(store, emb, wid, rid, "The checkout service had a race condition.")
    store.link_memory_entity(
        memory_id=checkout_memory,
        entity_id=checkout, workspace_id=wid, repo_id=rid,
        source_kind="test", confidence=1.0,
    )
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

    def list_memory_entities(
        flt, *, entity_ids=None, memory_ids=None, limit=None, prompt_only=False,
    ):
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


def test_graph_arm_excludes_pending_edge_support_bridges_before_ppr():
    from engraphis.core.interfaces import Edge, Node

    store, emb, eng = _engine()
    wid = store.get_or_create_workspace("w")
    rid = store.get_or_create_repo(wid, "r")
    redis = store.upsert_entity(Node(
        id="", name="Redis", ntype="tech", workspace_id=wid, repo_id=rid,
    ))
    checkout = store.upsert_entity(Node(
        id="", name="checkout", ntype="module", workspace_id=wid, repo_id=rid,
    ))
    pending = _add(
        store, emb, wid, rid, "Pending import says Redis reaches checkout.",
        provenance={"source": "import", "trusted": False, "review_state": "pending"},
    )
    approved = _add(store, emb, wid, rid, "Checkout has approved deployment evidence.")
    store.upsert_edge(Edge(
        id="", src=redis, dst=checkout, relation="used_by", workspace_id=wid,
        repo_id=rid, provenance={"memory_id": pending},
    ))
    store.link_memory_entity(
        memory_id=approved, entity_id=checkout, workspace_id=wid, repo_id=rid,
        source_kind="test", confidence=1.0,
    )

    scores = eng._graph_arm_ppr(
        "What does Redis use?", SearchFilter(workspace_id=wid, repo_id=rid),
        now=10**12, prompt_only=True,
    )

    assert pending not in scores
    assert approved not in scores


def test_recall_edge_filter_rejects_untrusted_source_less_edges():
    from engraphis.core.interfaces import Edge

    store, _emb, eng = _engine()
    wid = store.get_or_create_workspace("w")
    edges = [
        Edge(id="legacy", src="a", dst="b", relation="uses", workspace_id=wid),
        Edge(
            id="approved", src="a", dst="c", relation="uses", workspace_id=wid,
            provenance={"trusted": True, "review_state": "approved"},
        ),
        Edge(
            id="pending", src="a", dst="d", relation="uses", workspace_id=wid,
            provenance={"trusted": True, "review_state": "pending"},
        ),
        Edge(
            id="untrusted", src="a", dst="e", relation="uses", workspace_id=wid,
            provenance={"trusted": False},
        ),
    ]

    assert {edge.id for edge in eng._prompt_eligible_edges(edges)} == {
        "legacy", "approved",
    }


def test_prompt_edge_support_must_match_active_scope_and_validity():
    from engraphis.core.interfaces import Edge

    store, emb, eng = _engine()
    allowed = store.get_or_create_workspace("allowed")
    foreign = store.get_or_create_workspace("foreign")
    foreign_memory = _add(store, emb, foreign, None, "Foreign approved support.")
    expired_memory = _add(
        store, emb, allowed, None, "Expired approved support.",
        valid_from=0.0, valid_to=10.0,
    )
    edges = [
        Edge(
            id="foreign-support", src="a", dst="b", relation="supports",
            workspace_id=allowed,
            provenance={
                "trusted": True, "review_state": "approved",
                "memory_id": foreign_memory,
            },
        ),
        Edge(
            id="expired-support", src="a", dst="c", relation="supports",
            workspace_id=allowed,
            provenance={
                "trusted": True, "review_state": "approved",
                "memory_id": expired_memory,
            },
        ),
    ]

    flt = SearchFilter(workspace_id=allowed, valid_at=20.0)
    assert eng._prompt_eligible_edges(edges, flt) == []


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
    emb = _SemanticTestEmbedder(256)
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
    # Diagnostics expose the actual post-overfetch page depth, not the policy
    # starting depth, so operators can distinguish an ordinary recall from one
    # that searched further for approved evidence.
    assert result.candidate_k_used == 750
    assert requested[0] == 750


def test_prompt_only_overfetch_stays_bounded_for_large_untrusted_scopes():
    store = Store(":memory:")
    emb = _SemanticTestEmbedder(256)
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
    assert result.candidate_k_used == 256
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


def test_graph_arms_drop_zero_weight_edges_and_zero_confidence_incidence():
    from engraphis.core.interfaces import Edge, Node

    store, emb, eng = _engine()
    wid = store.get_or_create_workspace("w")
    rid = store.get_or_create_repo(wid, "r")
    atlas = store.upsert_entity(Node(
        id="", name="Atlas", ntype="service", workspace_id=wid, repo_id=rid,
    ))
    beacon = store.upsert_entity(Node(
        id="", name="Beacon", ntype="service", workspace_id=wid, repo_id=rid,
    ))
    store.upsert_edge(Edge(
        id="", src=atlas, dst=beacon, relation="related",
        weight=0.0, workspace_id=wid, repo_id=rid,
    ))
    behind_zero_edge = _add(
        store, emb, wid, rid, "Beacon owns an unrelated archive.",
    )
    direct_zero = _add(
        store, emb, wid, rid, "An unrelated Atlas note.",
    )
    store.link_memory_entity(
        memory_id=behind_zero_edge,
        entity_id=beacon,
        workspace_id=wid,
        repo_id=rid,
        source_kind="test",
        confidence=1.0,
    )
    store.link_memory_entity(
        memory_id=direct_zero,
        entity_id=atlas,
        workspace_id=wid,
        repo_id=rid,
        source_kind="test",
        confidence=0.0,
    )
    flt = SearchFilter(workspace_id=wid, repo_id=rid)

    for graph_arm in (eng._graph_arm_ppr, eng._graph_arm_1hop):
        scores = graph_arm("Atlas", flt, now=10**12)
        assert behind_zero_edge not in scores
        assert direct_zero not in scores


def test_entity_seed_cap_prioritizes_exact_names_deterministically(tmp_path):
    from engraphis.core.interfaces import Node

    store = Store(str(tmp_path / "bounded-entity-seeds.db"))
    emb = DeterministicEmbedder(256)
    eng = RecallEngine(store, emb, NumpyVectorIndex(store), IdentityReranker())
    wid = store.get_or_create_workspace("w")
    rid = store.get_or_create_repo(wid, "r")
    for index in range(2048):
        store.upsert_entity(Node(
            id="",
            name=f"Atlas Archive {index:04d}",
            ntype="document",
            workspace_id=wid,
            repo_id=rid,
        ))
    target = store.upsert_entity(Node(
        id="", name="Atlas", ntype="service", workspace_id=wid, repo_id=rid,
    ))
    flt = SearchFilter(workspace_id=wid, repo_id=rid)

    first = eng._seed_entity_map("What is Atlas?", flt)
    second = eng._seed_entity_map("What is Atlas?", flt)

    assert first == second == {target: "Atlas"}
    store.close()

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
