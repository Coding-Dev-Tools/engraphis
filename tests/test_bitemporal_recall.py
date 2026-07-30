import pytest

from engraphis.core.engine import MemoryEngine
from engraphis.core.interfaces import Edge, Node, SearchFilter


def _engine_with_historical_memory():
    engine = MemoryEngine.create(":memory:")
    workspace_id = engine.store.get_or_create_workspace("w")
    repo_id = engine.store.get_or_create_repo(workspace_id, "r")
    memory_id = engine.remember(
        "The service uses a TLS certificate issued by Aurora.",
        workspace_id=workspace_id,
        repo_id=repo_id,
        valid_from=100.0,
        resolve_conflicts=False,
    )
    engine.store.conn.execute(
        "UPDATE memories SET ingested_at=? WHERE id=?", (200.0, memory_id)
    )
    engine.store.conn.commit()
    return engine, workspace_id, repo_id, memory_id


def test_search_filter_preserves_legacy_positional_include_ancestors():
    flt = SearchFilter("w", "r", None, None, None, None, 123.0, True)
    assert flt.as_of == flt.valid_at == 123.0
    assert flt.include_ancestors is True
    assert flt.known_at is None


def test_ordinary_recall_uses_one_effective_snapshot_without_becoming_historical(
    monkeypatch,
):
    engine = MemoryEngine.create(":memory:")
    workspace_id = engine.store.get_or_create_workspace("w")
    memory_id = engine.remember(
        "The snapshot marker is blue.",
        workspace_id=workspace_id,
        resolve_conflicts=False,
    )
    engine.store.conn.execute(
        "UPDATE memories SET valid_from=100, ingested_at=100 WHERE id=?",
        (memory_id,),
    )
    engine.store.conn.commit()
    monkeypatch.setattr("engraphis.core.recall.now_ts", lambda: 123.0)

    result = engine.recall(
        "snapshot marker",
        workspace_id=workspace_id,
        reinforce=True,
    )

    assert result.valid_at == result.known_at == 123.0
    assert result.historical is False
    assert engine.store.get_memory(memory_id).access_count == 1


def test_valid_at_and_known_at_keep_future_knowledge_out_of_recall():
    engine, workspace_id, repo_id, memory_id = _engine_with_historical_memory()

    before_known = engine.recall(
        "Which certificate issuer does the service use?",
        workspace_id=workspace_id,
        repo_id=repo_id,
        valid_at=150.0,
        known_at=199.0,
    )
    once_known = engine.recall(
        "Which certificate issuer does the service use?",
        workspace_id=workspace_id,
        repo_id=repo_id,
        valid_at=150.0,
        known_at=200.0,
    )

    assert before_known.chunks == []
    assert [chunk["id"] for chunk in once_known.chunks] == [memory_id]


def test_historical_recall_is_observational():
    engine, workspace_id, repo_id, memory_id = _engine_with_historical_memory()
    before = engine.store.get_memory(memory_id)

    result = engine.recall(
        "Which certificate issuer does the service use?",
        workspace_id=workspace_id,
        repo_id=repo_id,
        valid_at=150.0,
        known_at=200.0,
    )
    after = engine.store.get_memory(memory_id)

    assert [chunk["id"] for chunk in result.chunks] == [memory_id]
    assert after.access_count == before.access_count
    assert after.last_access == before.last_access
    assert after.stability == before.stability


def test_system_expiry_is_evaluated_at_known_at_not_the_present():
    engine, workspace_id, repo_id, memory_id = _engine_with_historical_memory()
    engine.store.conn.execute(
        "UPDATE memories SET expired_at=? WHERE id=?", (300.0, memory_id)
    )
    engine.store.conn.commit()

    before_expiry = engine.store.list_memories(SearchFilter(
        workspace_id=workspace_id, valid_at=150.0, known_at=299.0,
    ))
    after_expiry = engine.store.list_memories(SearchFilter(
        workspace_id=workspace_id, valid_at=150.0, known_at=300.0,
    ))

    assert [record.id for record in before_expiry] == [memory_id]
    assert after_expiry == []


def test_retroactive_supersession_is_visible_only_after_it_was_learned():
    engine = MemoryEngine.create(":memory:")
    workspace_id = engine.store.get_or_create_workspace("w")
    repo_id = engine.store.get_or_create_repo(workspace_id, "r")
    old_id = engine.remember(
        "The production endpoint was alpha.",
        workspace_id=workspace_id,
        repo_id=repo_id,
        valid_from=100.0,
        resolve_conflicts=False,
    )
    engine.store.conn.execute(
        "UPDATE memories SET ingested_at=100 WHERE id=?", (old_id,)
    )
    engine.store.conn.commit()
    engine.store.close_validity(old_id, at=200.0)
    engine.store.conn.execute(
        "UPDATE memories SET valid_to_recorded_at=300 WHERE id=?", (old_id,)
    )
    new_id = engine.remember(
        "The production endpoint was beta.",
        workspace_id=workspace_id,
        repo_id=repo_id,
        valid_from=200.0,
        resolve_conflicts=False,
    )
    engine.store.conn.execute(
        "UPDATE memories SET ingested_at=300 WHERE id=?", (new_id,)
    )
    engine.store.conn.commit()

    believed_before_correction = engine.recall(
        "What was the production endpoint?",
        workspace_id=workspace_id,
        repo_id=repo_id,
        valid_at=250.0,
        known_at=250.0,
    )
    corrected_view = engine.recall(
        "What was the production endpoint?",
        workspace_id=workspace_id,
        repo_id=repo_id,
        valid_at=250.0,
        known_at=350.0,
    )
    past_world_after_correction = engine.recall(
        "What was the production endpoint?",
        workspace_id=workspace_id,
        repo_id=repo_id,
        valid_at=150.0,
        known_at=350.0,
    )

    assert [chunk["id"] for chunk in believed_before_correction.chunks] == [old_id]
    assert [chunk["id"] for chunk in corrected_view.chunks] == [new_id]
    assert [chunk["id"] for chunk in past_world_after_correction.chunks] == [old_id]


def test_graph_edges_neighbors_and_supports_share_bitemporal_visibility():
    engine, workspace_id, repo_id, memory_id = _engine_with_historical_memory()
    store = engine.store
    alpha = store.upsert_entity(Node(
        id="", name="alpha", workspace_id=workspace_id, repo_id=repo_id,
    ))
    beta = store.upsert_entity(Node(
        id="", name="beta", workspace_id=workspace_id, repo_id=repo_id,
    ))
    edge_id = store.upsert_edge(Edge(
        id="", src=alpha, dst=beta, relation="depends_on",
        workspace_id=workspace_id, repo_id=repo_id,
        valid_from=100.0, ingested_at=200.0, expired_at=300.0,
        provenance={"memory_id": memory_id},
    ))
    before_known = SearchFilter(
        workspace_id=workspace_id, repo_id=repo_id, valid_at=150.0, known_at=199.0,
    )
    visible = SearchFilter(
        workspace_id=workspace_id, repo_id=repo_id, valid_at=150.0, known_at=200.0,
    )
    expired = SearchFilter(
        workspace_id=workspace_id, repo_id=repo_id, valid_at=150.0, known_at=300.0,
    )

    assert store.edges_in_scope(before_known) == []
    assert [edge.id for edge in store.edges_in_scope(visible)] == [edge_id]
    assert store.edges_in_scope(expired) == []
    assert store.neighbors([alpha], flt=before_known) == []
    assert [edge.id for edge in store.neighbors([alpha], flt=visible)] == [edge_id]
    assert store.neighbors([alpha], flt=expired) == []
    assert store.edge_supports_in_scope([edge_id], flt=before_known) == []
    assert len(store.edge_supports_in_scope([edge_id], flt=visible)) == 1
    assert store.edge_supports_in_scope([edge_id], flt=expired) == []


def test_retroactive_edge_closure_does_not_leak_before_it_was_known():
    engine, workspace_id, repo_id, memory_id = _engine_with_historical_memory()
    store = engine.store
    edge_id = store.upsert_edge(Edge(
        id="edge_history",
        src="alpha",
        dst="beta",
        relation="depends_on",
        workspace_id=workspace_id,
        repo_id=repo_id,
        valid_from=100.0,
        ingested_at=100.0,
        provenance={"memory_id": memory_id},
    ))
    store.invalidate_edge(edge_id, at=200.0)
    store.conn.execute(
        "UPDATE edges SET valid_to_recorded_at=300 WHERE id=?", (edge_id,)
    )
    store.conn.execute(
        "UPDATE edge_supports SET valid_to_recorded_at=300 WHERE edge_id=?", (edge_id,)
    )
    store.conn.commit()

    before_correction = SearchFilter(
        workspace_id=workspace_id, repo_id=repo_id, valid_at=250.0, known_at=250.0,
    )
    after_correction = SearchFilter(
        workspace_id=workspace_id, repo_id=repo_id, valid_at=250.0, known_at=350.0,
    )
    earlier_world = SearchFilter(
        workspace_id=workspace_id, repo_id=repo_id, valid_at=150.0, known_at=350.0,
    )

    assert [edge.id for edge in store.edges_in_scope(before_correction)] == [edge_id]
    assert store.edges_in_scope(after_correction) == []
    assert [edge.id for edge in store.edges_in_scope(earlier_world)] == [edge_id]
    assert store.edge_supports_in_scope([edge_id], flt=before_correction)
    assert store.edge_supports_in_scope([edge_id], flt=after_correction) == []


def test_memory_links_share_bitemporal_visibility_and_do_not_leak_future_associations():
    """A late direct-link must not change a historical graph walk."""
    engine, workspace_id, repo_id, memory_id = _engine_with_historical_memory()
    other_id = engine.remember(
        "Aurora certificate operations have a migration runbook.",
        workspace_id=workspace_id,
        repo_id=repo_id,
        valid_from=100.0,
        resolve_conflicts=False,
    )
    engine.store.conn.execute(
        "UPDATE memories SET ingested_at=100 WHERE id=?", (other_id,)
    )
    engine.store.add_link(
        memory_id, other_id, "related", valid_from=100.0, ingested_at=200.0,
    )
    engine.store.conn.commit()

    before_known = SearchFilter(
        workspace_id=workspace_id, repo_id=repo_id, valid_at=150.0, known_at=199.0,
    )
    visible = SearchFilter(
        workspace_id=workspace_id, repo_id=repo_id, valid_at=150.0, known_at=200.0,
    )
    assert engine.store.links_among([memory_id, other_id], flt=before_known) == []
    assert [link["relation"] for link in engine.store.links_among(
        [memory_id, other_id], flt=visible,
    )] == ["related"]

    engine.store.conn.execute(
        "UPDATE mem_links SET valid_to=200, valid_to_recorded_at=300 "
        "WHERE a=? AND b=?", (memory_id, other_id),
    )
    engine.store.conn.commit()
    believed_before_closure = SearchFilter(
        workspace_id=workspace_id, repo_id=repo_id, valid_at=250.0, known_at=250.0,
    )
    corrected_view = SearchFilter(
        workspace_id=workspace_id, repo_id=repo_id, valid_at=250.0, known_at=350.0,
    )
    earlier_world = SearchFilter(
        workspace_id=workspace_id, repo_id=repo_id, valid_at=150.0, known_at=350.0,
    )
    assert engine.store.links_among([memory_id, other_id], flt=believed_before_closure)
    assert engine.store.links_among([memory_id, other_id], flt=corrected_view) == []
    assert engine.store.links_among([memory_id, other_id], flt=earlier_world)


def test_as_of_is_the_valid_at_compatibility_alias_and_conflicts_are_rejected():
    compatible = SearchFilter(as_of=150.0)
    assert compatible.valid_at == compatible.as_of == 150.0
    assert compatible.historical

    with pytest.raises(ValueError, match="as_of and valid_at must match"):
        SearchFilter(as_of=100.0, valid_at=101.0)


@pytest.mark.parametrize("field", ["as_of", "valid_at", "known_at"])
def test_temporal_filter_anchors_must_be_finite(field):
    for invalid in (float("nan"), True):
        with pytest.raises(ValueError, match=field + " must be a finite timestamp"):
            SearchFilter(**{field: invalid})
