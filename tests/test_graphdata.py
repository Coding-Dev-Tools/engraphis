"""Unit tests for engraphis.graphdata — the pure row-shaping logic shared by the
dashboard's and Inspector's Graph tab endpoints (no I/O, no MemoryService).
"""
from engraphis.graphdata import build_graph_payload, empty_graph


def test_empty_graph_shape():
    g = empty_graph("acme")
    assert g == {"workspace": "acme", "nodes": [], "edges": [], "types": [], "top": [],
                 "stats": {"entities": 0, "edges": 0, "connected": 0, "isolated": 0}}


def test_build_graph_payload_basic_shape():
    ents = [{"name": "Alice", "etype": "person_or_concept"},
            {"name": "Acme Corp", "etype": "organization"},
            {"name": "Isolated Co", "etype": "organization"}]
    edges = [{"src": "Alice", "dst": "Acme Corp", "relation": "works_at"}]
    g = build_graph_payload("acme", ents, edges)

    ids = {n["id"] for n in g["nodes"]}
    assert ids == {"Alice", "Acme Corp", "Isolated Co"}
    assert g["edges"] == [{"from": "Alice", "to": "Acme Corp", "label": "works_at"}]

    by_id = {n["id"]: n for n in g["nodes"]}
    assert by_id["Alice"]["degree"] == 1
    assert by_id["Acme Corp"]["degree"] == 1
    assert by_id["Isolated Co"]["degree"] == 0

    types = {t["etype"]: t["count"] for t in g["types"]}
    assert types == {"person_or_concept": 1, "organization": 2}

    assert g["top"] == [{"name": "Alice", "degree": 1}, {"name": "Acme Corp", "degree": 1}] or \
           g["top"] == [{"name": "Acme Corp", "degree": 1}, {"name": "Alice", "degree": 1}]

    assert g["stats"] == {"entities": 3, "edges": 1, "connected": 2, "isolated": 1}


def test_build_graph_payload_defaults_missing_etype_and_skips_dangling_edges():
    ents = [{"name": "Mystery", "etype": None}]
    edges = [{"src": "Mystery", "dst": None, "relation": "mentions"},
             {"src": None, "dst": "Mystery", "relation": "mentions"}]
    g = build_graph_payload("acme", ents, edges)
    assert g["edges"] == []                      # dangling edges (missing src/dst) dropped
    assert g["nodes"] == [{"id": "Mystery", "label": "Mystery",
                           "etype": "person_or_concept", "degree": 0}]


def test_build_graph_payload_top_connected_capped_at_twelve():
    ents = [{"name": f"n{i}", "etype": "person_or_concept"} for i in range(20)]
    edges = [{"src": f"n{i}", "dst": f"n{i+1}", "relation": "related"} for i in range(19)]
    g = build_graph_payload("acme", ents, edges)
    assert len(g["top"]) == 12
    degrees = [t["degree"] for t in g["top"]]
    assert degrees == sorted(degrees, reverse=True)
