"""Entity resolution: canonical aliases + query-time alias seeding for the graph arm."""
from engraphis.core.interfaces import MemoryRecord, Node, SearchFilter
from engraphis.core.resolve import ResolutionOp, resolve
from engraphis.core.store import Store, normalize_entity_name


def _store_with_canonical_entities():
    """Seed two entities that normalize to one canonical (exact-normalized pass)."""
    store = Store(":memory:")
    wid = store.get_or_create_workspace("w")
    rid = store.get_or_create_repo(wid, "r")
    a = store.upsert_entity(Node(id="", name="OpenAI", ntype="org",
                                 workspace_id=wid, repo_id=rid))
    b = store.upsert_entity(Node(id="", name="Open AI", ntype="org",
                                 workspace_id=wid, repo_id=rid))
    store._backfill_entity_canonicalization()
    return store, wid, rid, a, b


def test_normalize_entity_name_keeps_cpp_and_csharp_distinct():
    assert normalize_entity_name("C++") == "c++"
    assert normalize_entity_name("C#") == "c#"
    assert normalize_entity_name("C++") != normalize_entity_name("C#")


def test_exact_normalized_entities_share_a_canonical():
    store, wid, _rid, a, b = _store_with_canonical_entities()
    rows = {r["id"]: r for r in store.conn.execute(
        "SELECT id, canonical_id, canonical_method FROM entities").fetchall()}
    assert rows[a]["canonical_id"] == rows[b]["canonical_id"]
    assert rows[a]["canonical_method"] == "token_overlap"
    store.close()


def test_query_seeds_canonical_group_via_alias_member():
    """A query mentioning the member spelling seeds both members via canonical join."""
    store, wid, rid, a, b = _store_with_canonical_entities()
    from engraphis.core.recall import RecallEngine
    from engraphis.backends.embedder_deterministic import DeterministicEmbedder
    from engraphis.backends.vector_numpy import NumpyVectorIndex

    engine = RecallEngine(store, DeterministicEmbedder(dim=64), NumpyVectorIndex(store))
    flt = SearchFilter(workspace_id=wid, repo_id=rid)
    # Direct name match: "OpenAI" in query seeds "OpenAI".
    direct = engine._seed_entity_map("OpenAI pricing", flt)
    assert a in direct
    # Alias member: "Open AI" spelling is NOT a direct match for stored "OpenAI", but
    # the canonical fallback joins the group and seeds both members.
    aliased = engine._seed_entity_map("Open AI pricing", flt)
    assert a in aliased
    assert b in aliased
    store.close()


def test_query_seeds_canonical_representative_name():
    store, wid, rid, a, b = _store_with_canonical_entities()
    from engraphis.core.recall import RecallEngine
    from engraphis.backends.embedder_deterministic import DeterministicEmbedder
    from engraphis.backends.vector_numpy import NumpyVectorIndex

    engine = RecallEngine(store, DeterministicEmbedder(dim=64), NumpyVectorIndex(store))
    flt = SearchFilter(workspace_id=wid, repo_id=rid)
    # "OpenAI" appears in the query; the canonical representative is "OpenAI", so the
    # alias member "Open AI" must also seed through the canonical join.
    seeded = engine._seed_entity_map("OpenAI roadmap", flt)
    assert a in seeded
    assert b in seeded
    store.close()


def test_canonicalization_stays_within_workspace_and_entity_type():
    """A spelling alias must not merge entities across isolation boundaries."""
    store = Store(":memory:")
    first_workspace = store.get_or_create_workspace("first")
    second_workspace = store.get_or_create_workspace("second")
    first_repo = store.get_or_create_repo(first_workspace, "repo")
    second_repo = store.get_or_create_repo(second_workspace, "repo")
    org = store.upsert_entity(Node(
        id="", name="Open AI", ntype="org",
        workspace_id=first_workspace, repo_id=first_repo,
    ))
    person = store.upsert_entity(Node(
        id="", name="OpenAI", ntype="person",
        workspace_id=first_workspace, repo_id=first_repo,
    ))
    other_workspace = store.upsert_entity(Node(
        id="", name="OpenAI", ntype="org",
        workspace_id=second_workspace, repo_id=second_repo,
    ))
    store._backfill_entity_canonicalization()
    rows = {
        row["id"]: row["canonical_id"]
        for row in store.conn.execute(
            "SELECT id, canonical_id FROM entities WHERE id IN (?,?,?)",
            (org, person, other_workspace),
        ).fetchall()
    }

    assert rows[org] != rows[person]
    assert rows[org] != rows[other_workspace]
    store.close()


def _memory(memory_id, content, *, subject_key="", claim_kind="", valid_from=None):
    return MemoryRecord(
        id=memory_id,
        content=content,
        subject_key=subject_key,
        claim_kind=claim_kind,
        valid_from=valid_from,
    )


def test_keyed_exact_claim_is_idempotent_even_with_low_similarity():
    """A durable claim identity outranks vector rank and presentation punctuation."""
    existing = _memory(
        "old",
        "The API uses PASETO tokens for authentication.",
        subject_key="api.auth",
        claim_kind="mechanism",
    )
    decision = resolve(
        "unrelated display title",
        [(0.0, existing)],
        subject_key="api.auth",
        claim_kind="mechanism",
        candidate_content="The API uses PASETO tokens for authentication!",
    )

    assert decision.op is ResolutionOp.NOOP
    assert decision.target_id == "old"


def test_keyed_claim_kind_prevents_cross_predicate_supersession():
    """Same subject with a different predicate is not the same durable claim."""
    existing = _memory(
        "old",
        "The API uses PASETO tokens.",
        subject_key="api.auth",
        claim_kind="mechanism",
    )
    decision = resolve(
        "The API rotates keys hourly.",
        [(1.0, existing)],
        subject_key="api.auth",
        claim_kind="rotation",
        candidate_content="The API rotates keys hourly.",
    )

    assert decision.op is ResolutionOp.ADD
    assert decision.target_id is None


def test_ambiguous_unkeyed_supersession_keeps_all_candidates_live():
    """Near-equal strong matches must not retire an arbitrary unrelated neighbor."""
    neighbors = [
        (0.80, _memory("b", "The API uses PASETO tokens with hourly rotation.")),
        (0.80, _memory("a", "The API uses PASETO tokens with daily rotation.")),
    ]
    decision = resolve(
        "The API uses PASETO tokens with weekly rotation.",
        neighbors,
    )

    assert decision.op is ResolutionOp.RELATE
    assert decision.target_id is None
    assert "ambiguous" in decision.reason


def test_keyed_supersession_target_is_stable_across_neighbor_order():
    """History updates are deterministic and follow the latest world-time version."""
    candidate = "The service timeout is 30 seconds."
    first = _memory("z", "The service timeout is 60 seconds.",
                    subject_key="service.timeout", claim_kind="configured_value",
                    valid_from=20.0)
    second = _memory("a", "The service timeout is 90 seconds.",
                     subject_key="service.timeout", claim_kind="configured_value",
                     valid_from=10.0)

    left = resolve(candidate, [(0.7, first), (0.7, second)],
                   subject_key="service.timeout", claim_kind="configured_value",
                   candidate_content=candidate)
    right = resolve(candidate, [(0.7, second), (0.7, first)],
                    subject_key="service.timeout", claim_kind="configured_value",
                    candidate_content=candidate)

    assert left.op is right.op is ResolutionOp.INVALIDATE
    assert left.target_id == right.target_id == "z"
