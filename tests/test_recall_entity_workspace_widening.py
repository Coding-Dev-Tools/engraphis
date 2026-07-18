"""Entity ancestor-widening treats ``workspace_id`` like ``repo_id`` (0.9.7 change).

``SearchFilter.include_ancestors`` promises that contextual reads also see records
stored without the narrower scope; before 0.9.7 the entity map applied that to
``repo_id`` but not ``workspace_id``, hiding user-scope/global entities from every
workspace-scoped graph recall. Retrieval-behavior change → pinned by test (house rule:
"better" needs a number — here, exact visibility semantics).
"""
from engraphis.core.engine import MemoryEngine
from engraphis.core.interfaces import Node, SearchFilter


def _seed(engine, wid):
    s = engine.store
    s.upsert_entity(Node(id="", name="scoped-entity", ntype="service",
                         workspace_id=wid, repo_id=None, canonical_id=None))
    s.upsert_entity(Node(id="", name="global-entity", ntype="service",
                         workspace_id=None, repo_id=None, canonical_id=None))


def test_ancestor_widening_includes_null_workspace_entities():
    engine = MemoryEngine.create(":memory:")
    wid = engine.store.get_or_create_workspace("w")
    _seed(engine, wid)
    names = set(engine.recall_engine._entity_map(
        SearchFilter(workspace_id=wid, include_ancestors=True)).values())
    assert {"scoped-entity", "global-entity"} <= names


def test_strict_filter_still_excludes_null_workspace_entities():
    engine = MemoryEngine.create(":memory:")
    wid = engine.store.get_or_create_workspace("w")
    _seed(engine, wid)
    names = set(engine.recall_engine._entity_map(
        SearchFilter(workspace_id=wid, include_ancestors=False)).values())
    assert "scoped-entity" in names and "global-entity" not in names


def test_other_workspaces_never_leak_either_way():
    engine = MemoryEngine.create(":memory:")
    wid = engine.store.get_or_create_workspace("w")
    other = engine.store.get_or_create_workspace("other")
    engine.store.upsert_entity(Node(id="", name="foreign-entity", ntype="service",
                                    workspace_id=other, repo_id=None, canonical_id=None))
    for widen in (True, False):
        names = set(engine.recall_engine._entity_map(
            SearchFilter(workspace_id=wid, include_ancestors=widen)).values())
        assert "foreign-entity" not in names
