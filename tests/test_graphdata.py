"""Unit tests for engraphis.graphdata — the pure row-shaping logic shared by the
dashboard's and Inspector's Graph tab endpoints (no I/O, no MemoryService).

Entity rows carry an ``id`` distinct from their display ``name`` (the real schema:
``entities.id`` is an opaque ``ent_<ulid>``), and edges reference entities by that
``id`` — never by name (see ``backends.graph_extractor.feed``, which always writes
``Edge(src=<entity id>, dst=<entity id>)``). Every fixture here uses ids that differ
from the display name specifically to catch a regression of the bug fixed 2026-07-11:
``build_graph_payload`` used to key its name/etype lookup by entity *name* while node
degree/edges were keyed by entity *id*, silently doubling every connected entity into
a correctly-named-but-falsely-isolated node plus a correctly-connected-but-id-labeled
phantom (surfaced as stray "ent_..."-named nodes on the Graph tab).
"""
from engraphis.graphdata import build_graph_payload, empty_graph


def test_empty_graph_shape():
    g = empty_graph("acme")
    assert g == {"workspace": "acme", "nodes": [], "edges": [], "types": [],
                 "layers": [], "top": [],
                 "stats": {"entities": 0, "edges": 0, "connected": 0, "isolated": 0}}


def test_build_graph_payload_basic_shape():
    ents = [{"id": "e1", "name": "Alice", "etype": "person_or_concept"},
            {"id": "e2", "name": "Acme Corp", "etype": "organization"},
            {"id": "e3", "name": "Isolated Co", "etype": "organization"}]
    edges = [{"src": "e1", "dst": "e2", "relation": "works_at", "layer": "entity"}]
    g = build_graph_payload("acme", ents, edges)

    # node identity is the entity id, not the display name
    ids = {n["id"] for n in g["nodes"]}
    assert ids == {"e1", "e2", "e3"}
    assert g["edges"] == [
        {"from": "e1", "to": "e2", "label": "works_at", "layer": "entity"}
    ]
    assert g["layers"] == [{"layer": "entity", "count": 1}]

    by_id = {n["id"]: n for n in g["nodes"]}
    assert by_id["e1"]["label"] == "Alice" and by_id["e1"]["degree"] == 1
    assert by_id["e2"]["label"] == "Acme Corp" and by_id["e2"]["degree"] == 1
    assert by_id["e3"]["label"] == "Isolated Co" and by_id["e3"]["degree"] == 0

    types = {t["etype"]: t["count"] for t in g["types"]}
    assert types == {"person_or_concept": 1, "organization": 2}

    # "top" is a human-readable ranking: id for focusing the graph, name for display
    assert g["top"] == [{"id": "e1", "name": "Alice", "degree": 1},
                         {"id": "e2", "name": "Acme Corp", "degree": 1}] or \
           g["top"] == [{"id": "e2", "name": "Acme Corp", "degree": 1},
                         {"id": "e1", "name": "Alice", "degree": 1}]

    assert g["stats"] == {"entities": 3, "edges": 1, "connected": 2, "isolated": 1}


def test_build_graph_payload_does_not_duplicate_connected_entities():
    """Regression guard for the 2026-07-11 bug: a connected entity must appear as
    exactly one node (keyed by id), not split into an isolated name-node plus a
    connected id-node."""
    ents = [{"id": "e1", "name": "Alice", "etype": "person_or_concept"},
            {"id": "e2", "name": "Acme Corp", "etype": "organization"}]
    edges = [{"src": "e1", "dst": "e2", "relation": "works_at"}]
    g = build_graph_payload("acme", ents, edges)
    assert len(g["nodes"]) == 2                       # not 4 (one real + one phantom each)
    assert g["stats"]["isolated"] == 0                 # both are connected, neither is stray
    labels = {n["label"] for n in g["nodes"]}
    assert labels == {"Alice", "Acme Corp"}             # no raw "e1"/"e2" ids leaking as labels


def test_build_graph_payload_defaults_missing_etype_and_skips_dangling_edges():
    ents = [{"id": "e1", "name": "Mystery", "etype": None}]
    edges = [{"src": "e1", "dst": None, "relation": "mentions"},
             {"src": None, "dst": "e1", "relation": "mentions"}]
    g = build_graph_payload("acme", ents, edges)
    assert g["edges"] == []                      # dangling edges (missing src/dst) dropped
    assert g["nodes"] == [{"id": "e1", "label": "Mystery",
                           "etype": "person_or_concept", "degree": 0}]


def test_build_graph_payload_falls_back_to_id_when_entity_row_missing():
    """Defensive path: an edge referencing an id with no matching entity row (should
    never happen, but the schema doesn't enforce it) still renders — label falls back
    to the raw id rather than crashing or vanishing."""
    edges = [{"src": "e1", "dst": "e2", "relation": "related"}]
    g = build_graph_payload("acme", [], edges)
    labels = {n["id"]: n["label"] for n in g["nodes"]}
    assert labels == {"e1": "e1", "e2": "e2"}
    assert g["edges"][0]["layer"] == "semantic"


def test_build_graph_payload_top_connected_capped_at_twelve():
    ents = [{"id": f"e{i}", "name": f"n{i}", "etype": "person_or_concept"} for i in range(20)]
    edges = [{"src": f"e{i}", "dst": f"e{i+1}", "relation": "related"} for i in range(19)]
    g = build_graph_payload("acme", ents, edges)
    assert len(g["top"]) == 12
    degrees = [t["degree"] for t in g["top"]]
    assert degrees == sorted(degrees, reverse=True)
    # "top" names are the human-readable names, not the raw ids
    assert all(t["name"] == f"n{int(t['id'][1:])}" for t in g["top"])
