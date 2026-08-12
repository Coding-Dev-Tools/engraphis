"""Focused schema/API coverage for the analytical Galaxy Graph vertical slice."""
# ruff: noqa: E402 -- optional-stack guard must run before importing FastAPI routes
import copy
import json
import math
import sqlite3
import threading
import time
import types

import pytest

pytest.importorskip("fastapi", reason="graph HTTP coverage requires the optional server stack")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from engraphis.backends import graph_extractor as graph_extractor_module
from engraphis.backends.graph_extractor import GraphExtraction, get_graph_extractor
from engraphis.core import graph_scene as graph_scene_module
from engraphis.core.graph_scene import build_graph_scene
from engraphis.core.interfaces import Edge, MemoryRecord, MemoryType, Node, Scope
from engraphis.core.schema import SCHEMA_VERSION
from engraphis.core.store import Store
from engraphis.routes import v2_api
from engraphis import service as service_module
from engraphis.service import GraphIndexRebuilding, MemoryService, ValidationError


def test_hierarchy_anchors_are_metadata_driven_not_coding_dev_tools_label():
    """A renamed explicit global remains central even against the historical-name decoy."""
    def resolve(rows):
        nodes = {
            row["id"]: {
                "id": row["id"], "label": row.get("label", row["id"]),
                "gravity_mass": row.get("gravity_mass", 1.0),
                "scene_rank": row.get("scene_rank", 0.0),
                "weighted_degree": row.get("weighted_degree", 0.0),
                "anchor_role": row.get("anchor_role", "community"),
            }
            for row in rows
        }
        return graph_scene_module._hierarchy_anchors(
            nodes, {node_id: [node_id] for node_id in nodes}
        )

    explicit = [
        {"id": "custom-orbit-root", "label": "Before rename", "anchor_role": "global",
         "gravity_mass": 1.0},
        {"id": "Coding-Dev-Tools", "label": "Coding-Dev-Tools", "gravity_mass": 999.0},
    ]
    _, before = resolve(explicit)
    explicit[0]["label"] = "After arbitrary rename"
    _, after = resolve(explicit)
    assert before == after == "custom-orbit-root"

    # Without authored global authority, physics uses deterministic mass, rank, degree, then id.
    _, mass_winner = resolve([
        {"id": "mass-winner", "gravity_mass": 12},
        {"id": "high-rank", "gravity_mass": 11, "scene_rank": 999},
    ])
    _, rank_winner = resolve([
        {"id": "low-rank", "gravity_mass": 8, "scene_rank": 1},
        {"id": "high-rank", "gravity_mass": 8, "scene_rank": 2},
    ])
    _, degree_winner = resolve([
        {"id": "low-degree", "gravity_mass": 8, "scene_rank": 2, "weighted_degree": 1},
        {"id": "high-degree", "gravity_mass": 8, "scene_rank": 2, "weighted_degree": 2},
    ])
    _, id_winner = resolve([
        {"id": "zeta", "gravity_mass": 8, "scene_rank": 2, "weighted_degree": 2},
        {"id": "alpha", "gravity_mass": 8, "scene_rank": 2, "weighted_degree": 2},
    ])
    assert (mass_winner, rank_winner, degree_winner, id_winner) == (
        "mass-winner", "high-rank", "high-degree", "alpha"
    )


def test_v4_migration_backfills_canonical_entities_and_edge_supports(tmp_path):
    db = tmp_path / "v3.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at REAL);
        INSERT INTO schema_migrations VALUES (3, 0);
        CREATE TABLE entities (
            id TEXT PRIMARY KEY, workspace_id TEXT, repo_id TEXT, name TEXT, etype TEXT,
            canonical_id TEXT, created_at REAL,
            UNIQUE(workspace_id, repo_id, name, etype)
        );
        CREATE TABLE edges (
            id TEXT PRIMARY KEY, workspace_id TEXT, repo_id TEXT, src TEXT NOT NULL,
            dst TEXT NOT NULL, relation TEXT NOT NULL, layer TEXT DEFAULT 'semantic',
            weight REAL DEFAULT 1.0, valid_from REAL, valid_to REAL, ingested_at REAL,
            expired_at REAL, provenance TEXT DEFAULT '{}'
        );
        INSERT INTO entities VALUES ('ent_a', 'ws_a', 'repo_a', 'Redis', 'concept', NULL, 1);
        INSERT INTO entities VALUES ('ent_b', 'ws_a', 'repo_b', ' redis ', 'concept', NULL, 2);
        INSERT INTO edges VALUES (
            'edg_a', 'ws_a', NULL, 'ent_a', 'ent_b', 'uses', 'entity', 1,
            1, NULL, 1, NULL,
            '{"source":"structured_extractor","memory_id":"mem_a","memory_ids":["mem_a","mem_b"]}'
        );
    """)
    conn.commit()
    conn.close()

    store = Store(str(db))
    rows = [dict(row) for row in store.conn.execute(
        "SELECT id, normalized_name, canonical_id, canonical_confidence "
        "FROM entities ORDER BY id"
    ).fetchall()]
    supports = store.edge_supports_in_scope(["edg_a"], at=2)

    assert store.schema_version == SCHEMA_VERSION
    assert {row["normalized_name"] for row in rows} == {"redis"}
    assert len({row["canonical_id"] for row in rows}) == 1
    assert all(row["canonical_confidence"] == 1.0 for row in rows)
    assert [(row["memory_id"], row["source_kind"], row["confidence"])
            for row in supports] == [
                ("mem_a", "structured", 0.8),
                ("mem_b", "structured", 0.8),
            ]
    indexes = {row["name"] for row in store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    ).fetchall()}
    assert {"idx_entity_canonical", "idx_entity_normalized",
            "idx_edge_support_edge", "idx_edge_support_memory"} <= indexes
    store.close()


def test_v4_migration_converges_duplicate_live_relations_without_losing_support(tmp_path):
    db = tmp_path / "duplicate-v3.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at REAL);
        INSERT INTO schema_migrations VALUES (3, 0);
        CREATE TABLE edges (
            id TEXT PRIMARY KEY, workspace_id TEXT, repo_id TEXT, src TEXT NOT NULL,
            dst TEXT NOT NULL, relation TEXT NOT NULL, layer TEXT DEFAULT 'semantic',
            weight REAL DEFAULT 1.0, valid_from REAL, valid_to REAL, ingested_at REAL,
            expired_at REAL, provenance TEXT DEFAULT '{}'
        );
        INSERT INTO edges VALUES (
            'edg_a', 'ws_a', NULL, 'ent_a', 'ent_b', 'uses', 'entity', 1,
            1, NULL, 1, NULL, '{"source":"manual","memory_id":"mem_a"}'
        );
        INSERT INTO edges VALUES (
            'edg_b', 'ws_a', NULL, 'ent_a', 'ent_b', 'uses', 'entity', 2,
            2, NULL, 2, NULL, '{"source":"structured","memory_id":"mem_b"}'
        );
    """)
    conn.commit()
    conn.close()

    store = Store(str(db))
    live = store.conn.execute(
        "SELECT id, weight, provenance FROM edges WHERE valid_to IS NULL"
    ).fetchall()
    retired = store.conn.execute(
        "SELECT id, valid_to FROM edges WHERE valid_to IS NOT NULL"
    ).fetchall()
    supports = store.edge_supports_in_scope(["edg_a"])

    assert len(live) == 1 and live[0]["id"] == "edg_a" and live[0]["weight"] == 2
    assert len(retired) == 1 and retired[0]["id"] == "edg_b"
    assert {row["memory_id"] for row in supports} == {"mem_a", "mem_b"}
    provenance = json.loads(live[0]["provenance"])
    assert set(provenance["memory_ids"]) == {"mem_a", "mem_b"}
    assert provenance["canonical_deduplicated_from"] == ["edg_b"]
    indexes = {row["name"] for row in store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    ).fetchall()}
    assert {"idx_edge_workspace_live_unique", "idx_edge_repo_live_unique"} <= indexes
    store.close()


def test_v4_migration_normalizes_reversed_undirected_endpoints_before_dedup(tmp_path):
    db = tmp_path / "reversed-undirected-v3.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at REAL);
        INSERT INTO schema_migrations VALUES (3, 0);
        CREATE TABLE edges (
            id TEXT PRIMARY KEY, workspace_id TEXT, repo_id TEXT, src TEXT NOT NULL,
            dst TEXT NOT NULL, relation TEXT NOT NULL, layer TEXT DEFAULT 'semantic',
            weight REAL DEFAULT 1.0, valid_from REAL, valid_to REAL, ingested_at REAL,
            expired_at REAL, provenance TEXT DEFAULT '{}'
        );
        INSERT INTO edges VALUES (
            'edg_old', 'ws_a', NULL, 'z', 'a', 'co_occurs', 'semantic', 0.2,
            1, NULL, 1, NULL, '{"source":"regex","memory_id":"mem_a"}'
        );
        INSERT INTO edges VALUES (
            'edg_new', 'ws_a', NULL, 'a', 'z', 'co_occurs', 'semantic', 0.4,
            2, NULL, 2, NULL, '{"source":"regex","memory_id":"mem_b"}'
        );
        INSERT INTO edges VALUES (
            'edg_single', 'ws_a', NULL, 'y', 'x', 'related', 'semantic', 1,
            3, NULL, 3, NULL, '{}'
        );
    """)
    conn.commit()
    conn.close()

    store = Store(str(db))
    cooccurs = store.conn.execute(
        "SELECT id, src, dst, weight FROM edges "
        "WHERE relation='co_occurs' AND valid_to IS NULL"
    ).fetchall()
    singleton = store.conn.execute(
        "SELECT src, dst FROM edges WHERE id='edg_single'"
    ).fetchone()

    assert [(row["id"], row["src"], row["dst"], row["weight"])
            for row in cooccurs] == [("edg_old", "a", "z", 0.4)]
    assert (singleton["src"], singleton["dst"]) == ("x", "y")
    assert {row["memory_id"] for row in store.edge_supports_in_scope(["edg_old"])} == {
        "mem_a", "mem_b",
    }
    store.close()


def test_edge_writer_merges_equivalent_live_relation_supports():
    store = Store(":memory:")
    workspace_id = store.get_or_create_workspace("acme")
    first = store.upsert_edge(Edge(
        id="edg_first", src="ent_a", dst="ent_b", relation="uses",
        workspace_id=workspace_id,
        provenance={"source": "manual", "memory_id": "mem_a"},
    ))
    second = store.upsert_edge(Edge(
        id="edg_second", src="ent_a", dst="ent_b", relation="uses",
        workspace_id=workspace_id,
        provenance={"source": "structured", "memory_id": "mem_b"},
    ))

    assert first == second == "edg_first"
    assert store.conn.execute(
        "SELECT COUNT(*) AS n FROM edges WHERE workspace_id=? AND valid_to IS NULL",
        (workspace_id,),
    ).fetchone()["n"] == 1
    supports = store.edge_supports_in_scope([first])
    assert {row["memory_id"] for row in supports} == {"mem_a", "mem_b"}
    store.close()


def test_v4_reopen_keeps_graph_generation_stable_when_backfill_is_already_complete(tmp_path):
    database = tmp_path / "stable-generation.db"
    store = Store(str(database))
    workspace_id = store.get_or_create_workspace("acme")
    store.upsert_entity(Node(
        id="", name="Engraphis", ntype="concept", workspace_id=workspace_id,
    ))
    edge_id = store.upsert_edge(Edge(
        id="edg_historical", src="ent_a", dst="ent_b", relation="uses",
        workspace_id=workspace_id,
        provenance={"source": "structured", "memory_id": "mem_historical"},
    ))
    store.invalidate_edge(edge_id, at=42.0)
    supports_before = store.conn.execute(
        "SELECT COUNT(*) AS n FROM edge_supports WHERE edge_id=?", (edge_id,),
    ).fetchone()["n"]
    before = store.conn.execute(
        "SELECT generation FROM graph_index_state WHERE workspace_id=?",
        (workspace_id,),
    ).fetchone()["generation"]
    store.close()

    reopened = Store(str(database))
    after = reopened.conn.execute(
        "SELECT generation FROM graph_index_state WHERE workspace_id=?",
        (workspace_id,),
    ).fetchone()["generation"]
    supports_after = reopened.conn.execute(
        "SELECT COUNT(*) AS n FROM edge_supports WHERE edge_id=?", (edge_id,),
    ).fetchone()["n"]

    assert after == before
    assert supports_after == supports_before == 1
    reopened.close()


def test_partial_live_edge_indexes_allow_history_but_reject_active_duplicates():
    store = Store(":memory:")
    workspace_id = store.get_or_create_workspace("acme")
    repo_id = store.get_or_create_repo(workspace_id, "web")
    sql = (
        "INSERT INTO edges(id, workspace_id, repo_id, src, dst, relation, layer, "
        "valid_from, valid_to, expired_at) VALUES (?,?,?,?,?,?,?,?,?,?)"
    )
    store.conn.execute(sql, (
        "workspace_live", workspace_id, None, "a", "b", "uses", "entity",
        1.0, None, None,
    ))
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute(sql, (
            "workspace_duplicate", workspace_id, None, "a", "b", "uses", "entity",
            2.0, None, None,
        ))
    store.conn.execute(sql, (
        "workspace_history", workspace_id, None, "a", "b", "uses", "entity",
        0.0, 0.5, None,
    ))
    store.conn.execute(sql, (
        "workspace_expired", workspace_id, None, "a", "b", "uses", "entity",
        0.0, None, 0.5,
    ))
    store.conn.execute(sql, (
        "repo_live", workspace_id, repo_id, "a", "b", "uses", "entity",
        1.0, None, None,
    ))
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute(sql, (
            "repo_duplicate", workspace_id, repo_id, "a", "b", "uses", "entity",
            2.0, None, None,
        ))
    store.conn.execute(sql, (
        "repo_history", workspace_id, repo_id, "a", "b", "uses", "entity",
        0.0, 0.5, None,
    ))
    store.conn.commit()

    assert store.conn.execute(
        "SELECT COUNT(*) AS n FROM edges WHERE src='a' AND dst='b'"
    ).fetchone()["n"] == 5
    store.close()


def test_repeating_same_edge_writer_is_a_storage_noop():
    store = Store(":memory:")
    workspace_id = store.get_or_create_workspace("acme")
    edge = Edge(
        id="edg_repeat", src="ent_a", dst="ent_b", relation="uses",
        workspace_id=workspace_id,
        provenance={"source": "structured", "memory_id": "mem_a"},
    )
    first = store.upsert_edge(edge)
    stored_before = dict(store.conn.execute(
        "SELECT valid_from, ingested_at, provenance FROM edges WHERE id=?", (first,)
    ).fetchone())
    generation_before = store.conn.execute(
        "SELECT generation FROM graph_index_state WHERE workspace_id=?", (workspace_id,)
    ).fetchone()["generation"]

    second = store.upsert_edge(edge)

    stored_after = dict(store.conn.execute(
        "SELECT valid_from, ingested_at, provenance FROM edges WHERE id=?", (second,)
    ).fetchone())
    support_rows = store.conn.execute(
        "SELECT valid_to, expired_at FROM edge_supports WHERE edge_id=?", (first,)
    ).fetchall()
    generation_after = store.conn.execute(
        "SELECT generation FROM graph_index_state WHERE workspace_id=?", (workspace_id,)
    ).fetchone()["generation"]
    assert first == second == "edg_repeat"
    assert stored_after == stored_before
    assert len(support_rows) == 1
    assert support_rows[0]["valid_to"] is None and support_rows[0]["expired_at"] is None
    assert generation_after == generation_before
    store.close()


def test_repeated_support_writer_keeps_one_live_row_and_upgrades_confidence():
    store = Store(":memory:")
    workspace_id = store.get_or_create_workspace("acme")
    edge_id = store.upsert_edge(Edge(
        id="edg_support", src="ent_a", dst="ent_b", relation="uses",
        workspace_id=workspace_id,
    ))
    store.add_edge_support(edge_id, {
        "source": "structured", "memory_id": "mem_a", "confidence": 0.6,
    })
    store.add_edge_support(edge_id, {
        "source": "structured", "memory_id": "mem_a", "confidence": 0.95,
    })

    supports = store.conn.execute(
        "SELECT memory_id, source_kind, confidence FROM edge_supports "
        "WHERE edge_id=? AND valid_to IS NULL AND expired_at IS NULL",
        (edge_id,),
    ).fetchall()
    assert [(row["memory_id"], row["source_kind"], row["confidence"])
            for row in supports] == [("mem_a", "structured", 0.95)]
    store.close()


def test_undirected_equivalent_writers_converge_after_endpoint_normalization():
    store = Store(":memory:")
    workspace_id = store.get_or_create_workspace("acme")
    first = store.upsert_edge(Edge(
        id="edg_forward", src="ent_a", dst="ent_b", relation="co_occurs",
        workspace_id=workspace_id,
        provenance={"source": "regex", "memory_id": "mem_a"},
    ))
    second = store.upsert_edge(Edge(
        id="edg_reverse", src="ent_b", dst="ent_a", relation="co_occurs",
        workspace_id=workspace_id,
        provenance={"source": "regex", "memory_id": "mem_b"},
    ))

    row = store.conn.execute(
        "SELECT id, src, dst FROM edges WHERE workspace_id=? AND valid_to IS NULL",
        (workspace_id,),
    ).fetchone()
    assert first == second == row["id"] == "edg_forward"
    assert (row["src"], row["dst"]) == ("ent_a", "ent_b")
    assert {item["memory_id"] for item in store.edge_supports_in_scope([first])} == {
        "mem_a", "mem_b",
    }
    store.close()


def test_scene_is_canonical_deterministic_and_strength_shortens_links():
    entities = [
        {"id": "a1", "canonical_id": "a1", "name": "Alpha", "etype": "concept",
         "repo_id": "r1"},
        {"id": "a2", "canonical_id": "a1", "name": "alpha!", "etype": "concept",
         "repo_id": "r2"},
        {"id": "b", "canonical_id": "b", "name": "Beta", "etype": "concept",
         "repo_id": "r1"},
        {"id": "c", "canonical_id": "c", "name": "Gamma", "etype": "concept",
         "repo_id": "r1"},
    ]
    edges = [
        {"id": "strong", "src": "a1", "dst": "b", "relation": "uses",
         "layer": "entity", "weight": 4.0, "provenance": "{}"},
        {"id": "weak", "src": "b", "dst": "c", "relation": "related_to",
         "layer": "semantic", "weight": 0.05, "provenance": "{}"},
        {"id": "noise", "src": "a2", "dst": "c", "relation": "co_occurs",
         "layer": "semantic", "weight": 0.2, "provenance": "{}"},
    ]
    supports = [
        {"edge_id": "strong", "memory_id": "m1", "source_kind": "manual",
         "confidence": 0.99, "provenance": "{}"},
        {"edge_id": "strong", "memory_id": "m2", "source_kind": "manual",
         "confidence": 0.99, "provenance": "{}"},
        {"edge_id": "weak", "memory_id": "m3", "source_kind": "legacy_unknown",
         "confidence": 0.5, "provenance": "{}"},
        {"edge_id": "noise", "memory_id": "m4", "source_kind": "co_occurrence",
         "confidence": 0.25, "provenance": "{}"},
    ]

    first = build_graph_scene("w", entities, edges, supports)
    second = build_graph_scene("w", entities, edges, supports)

    assert first == second
    assert first["meta"]["total_nodes"] == 3  # a1/a2 collapse to one canonical entity
    assert "noise" not in {edge["id"] for edge in first["edges"]}
    by_id = {edge["id"]: edge for edge in first["edges"]}
    assert by_id["strong"]["strength"] > by_id["weak"]["strength"]
    assert by_id["strong"]["rest_length"] < by_id["weak"]["rest_length"]
    assert first["nodes"][0]["anchor_role"] == "global"
    assert first["nodes"][0]["x"] == first["nodes"][0]["y"] == 0.0


def test_complete_scene_keeps_every_memory_and_raw_connector_deterministically():
    entities = [
        {"id": "a1", "canonical_id": "a", "name": "Alpha", "etype": "concept"},
        {"id": "a2", "canonical_id": "a", "name": "alpha", "etype": "concept"},
        {"id": "b", "canonical_id": "b", "name": "Beta", "etype": "concept"},
    ]
    edges = [
        {"id": "raw_one", "src": "a1", "dst": "b", "relation": "uses",
         "layer": "entity", "weight": 1.0, "provenance": "{}"},
        {"id": "raw_two", "src": "a2", "dst": "b", "relation": "uses",
         "layer": "entity", "weight": 0.8, "provenance": "{}"},
    ]
    supports = [
        {"id": 1, "edge_id": "raw_one", "memory_id": "m1",
         "source_kind": "structured", "confidence": 0.8, "provenance": "{}"},
        {"id": 2, "edge_id": "raw_two", "memory_id": "m2",
         "source_kind": "manual", "confidence": 1.0, "provenance": "{}"},
    ]
    memories = [
        {"id": "m1", "title": "Alpha uses Beta", "mtype": "semantic",
         "scope": "workspace", "importance": 0.8},
        {"id": "m2", "title": "Second source", "mtype": "episodic",
         "scope": "workspace", "importance": 0.5},
        {"id": "m3", "title": "Unattached memory", "mtype": "procedural",
         "scope": "workspace", "importance": 0.3},
    ]
    memory_links = [{
        "a": "m1", "b": "m3", "relation": "related", "layer": "semantic",
        "reason": "manual", "created_at": 10,
    }]

    kwargs = {
        "level": "complete", "memory_rows": memories,
        "memory_link_rows": memory_links, "include_weak_cooccurrence": True,
    }
    first = build_graph_scene("w", entities, edges, supports, **kwargs)
    second = build_graph_scene("w", entities, edges, supports, **kwargs)

    assert first == second
    assert first["meta"]["complete_scene"] is True
    assert first["meta"]["truncated"] is False
    assert first["meta"]["entity_nodes"] == 2
    assert first["meta"]["memory_nodes"] == 3
    assert first["meta"]["raw_relations"] == 2
    assert first["meta"]["evidence_connectors"] == 4
    assert first["meta"]["memory_connectors"] == 1
    assert first["meta"]["shown_nodes"] == first["meta"]["total_nodes"] == 5
    assert first["meta"]["shown_edges"] == first["meta"]["total_edges"] == 7
    assert {node["node_kind"] for node in first["nodes"]} == {"entity", "memory"}
    by_kind = {}
    for edge in first["edges"]:
        by_kind.setdefault(edge["connector_kind"], set()).add(edge["id"])
    assert by_kind["entity_relation"] == {"raw_one", "raw_two"}
    assert len(by_kind["evidence"]) == 4
    assert len(by_kind["memory_link"]) == 1
    nodes = {node["id"]: node for node in first["nodes"]}
    for community in first["communities"]:
        community_id = community["id"]
        expected_internal = sum(
            edge["strength"] for edge in first["edges"]
            if nodes[edge["source"]]["community_id"] == community_id
            and nodes[edge["target"]]["community_id"] == community_id
        )
        expected_external = sum(
            edge["strength"] for edge in first["edges"]
            if ((nodes[edge["source"]]["community_id"] == community_id)
                != (nodes[edge["target"]]["community_id"] == community_id))
        )
        assert community["internal_strength"] == pytest.approx(expected_internal)
        assert community["external_strength"] == pytest.approx(expected_external)


def test_complete_scene_keeps_every_enabled_code_memory_connector():
    entities = [{
        "id": "code:symbol", "canonical_id": "code:symbol",
        "name": "repo:module.fn", "etype": "code_function", "repo_id": "repo",
    }]
    memories = [{
        "id": "memory", "title": "Function behavior", "mtype": "procedural",
        "scope": "repo", "repo_id": "repo", "importance": 0.5,
    }]
    links = [
        {"id": "code-link-b", "symbol_id": "symbol", "memory_id": "memory",
         "relation": "mentions", "confidence": 0.7},
        {"id": "code-link-a", "symbol_id": "symbol", "memory_id": "memory",
         "relation": "documents", "confidence": 1.0},
    ]

    scene = build_graph_scene(
        "w", entities, [], [], level="complete", memory_rows=memories,
        code_memory_link_rows=links,
    )

    code_edges = [edge for edge in scene["edges"]
                  if edge["connector_kind"] == "code_memory"]
    assert [edge["id"] for edge in code_edges] == ["code-link-a", "code-link-b"]
    assert {(edge["source"], edge["target"]) for edge in code_edges} == {
        ("memory", "code:symbol")
    }
    assert scene["meta"]["code_memory_connectors"] == 2


def test_complete_connected_only_keeps_code_memory_only_endpoints():
    entities = [
        {
            "id": "code:symbol", "canonical_id": "code:symbol",
            "name": "repo:module.fn", "etype": "code_function", "repo_id": "repo",
        },
        {
            "id": "isolated", "canonical_id": "isolated",
            "name": "Isolated", "etype": "concept", "repo_id": "repo",
        },
    ]
    memories = [{
        "id": "memory", "title": "Function behavior", "mtype": "procedural",
        "scope": "repo", "repo_id": "repo", "importance": 0.5,
    }]
    links = [{
        "id": "code-link", "symbol_id": "symbol", "memory_id": "memory",
        "relation": "documents", "confidence": 0.9,
    }]

    scene = build_graph_scene(
        "w", entities, [], [], level="complete", connected_only=True,
        memory_rows=memories, code_memory_link_rows=links,
    )

    assert {node["id"] for node in scene["nodes"]} == {"code:symbol", "memory"}
    assert [(edge["id"], edge["source"], edge["target"])
            for edge in scene["edges"]] == [
        ("code-link", "memory", "code:symbol")
    ]
    assert scene["meta"]["code_memory_connectors"] == 1


def test_community_bridges_keep_aggregate_evidence_for_physics():
    nodes = {
        "a": {"community_id": "ca"},
        "b": {"community_id": "cb"},
        "c": {"community_id": "cc"},
    }

    def edge(edge_id, source, target, strength, support_ids, support_count,
             bundled_edge_count, relation="uses"):
        return {
            "id": edge_id,
            "source": source,
            "target": target,
            "layer": "entity",
            "relation": relation,
            "strength": strength,
            "_support_ids_all": set(support_ids),
            "support_count": support_count,
            "bundled_edge_count": bundled_edge_count,
        }

    edges = [
        # Both public display strengths saturate at one. Physics must still see that
        # the a-c bridge has materially more aggregate evidence than a-b.
        edge("ab_known", "a", "b", 0.6, {"m1"}, 1, 1),
        edge("ab_legacy", "a", "b", 0.6, set(), 2, 2),
        edge("ac_one", "a", "c", 0.9, {"m2", "m3", "m4"}, 3, 3),
        edge("ac_two", "a", "c", 0.9, {"m5", "m6", "m7"}, 3, 3),
        edge("ac_three", "a", "c", 0.9, {"m8", "m9"}, 2, 2),
    ]
    graph = {"nodes": nodes, "edges": edges}

    first = graph_scene_module._bridges(graph, {"ca", "cb", "cc"}, 80)
    second = graph_scene_module._bridges(
        {"nodes": nodes, "edges": list(reversed(edges))}, {"ca", "cb", "cc"}, 80
    )

    assert first == second
    by_pair = {
        (bridge["source_community"], bridge["target_community"]): bridge
        for bridge in first
    }
    weaker = by_pair[("ca", "cb")]
    stronger = by_pair[("ca", "cc")]
    assert weaker["strength"] == stronger["strength"] == 1.0
    assert weaker["aggregate_strength"] == pytest.approx(1.2)
    assert stronger["aggregate_strength"] == pytest.approx(2.7)
    assert stronger["physics_strength"] > weaker["physics_strength"]
    assert 0.0 <= weaker["physics_strength"] <= 1.0
    assert 0.0 <= stronger["physics_strength"] <= 1.0
    # Known support IDs and anonymous legacy support are both represented.
    assert weaker["support_count"] == 3
    assert stronger["support_count"] == 8
    assert weaker["edge_count"] == 3
    assert stronger["edge_count"] == 8


def test_canonical_bundle_filters_use_aggregate_support_and_confidence():
    entities = [
        {"id": "a1", "canonical_id": "a", "name": "Alpha", "etype": "concept"},
        {"id": "a2", "canonical_id": "a", "name": "alpha", "etype": "concept"},
        {"id": "b1", "canonical_id": "b", "name": "Beta", "etype": "concept"},
        {"id": "b2", "canonical_id": "b", "name": "beta", "etype": "concept"},
    ]
    edges = [
        {"id": "one", "src": "a1", "dst": "b1", "relation": "co_occurs",
         "layer": "semantic", "weight": 1.0, "provenance": "{}"},
        {"id": "two", "src": "a2", "dst": "b2", "relation": "co_occurs",
         "layer": "semantic", "weight": 1.0, "provenance": "{}"},
    ]
    supports = [
        {"edge_id": "one", "memory_id": "m1", "confidence": 0.25,
         "provenance": "{}"},
        {"edge_id": "two", "memory_id": "m2", "confidence": 0.25,
         "provenance": "{}"},
    ]

    graph = graph_scene_module.build_canonical_graph(
        entities, edges, supports, include_weak_cooccurrence=False,
        min_support=2, min_confidence=0.4,
    )

    assert len(graph["edges"]) == 1
    edge = graph["edges"][0]
    assert edge["support_count"] == 2
    assert edge["confidence"] == pytest.approx(0.4375)
    assert edge["bundled_edge_count"] == 2


def test_edge_support_count_includes_identified_and_anonymous_evidence():
    entities = [
        {"id": "a", "name": "Alpha", "etype": "concept"},
        {"id": "b", "name": "Beta", "etype": "concept"},
    ]
    edges = [{
        "id": "edge", "src": "a", "dst": "b", "relation": "uses",
        "layer": "entity", "weight": 1.0, "provenance": "{}",
    }]
    supports = [
        {"edge_id": "edge", "memory_id": "known", "confidence": 0.8,
         "provenance": "{}"},
        {"edge_id": "edge", "memory_id": "", "confidence": 0.5,
         "provenance": "{}"},
    ]

    edge = graph_scene_module.build_canonical_graph(entities, edges, supports)["edges"][0]

    assert edge["support_count"] == 2
    assert edge["support_memory_ids"] == ["known"]


def test_anonymous_support_magnitude_contributes_to_node_evidence_mass():
    entities = [
        {"id": "a", "name": "Alpha", "etype": "concept"},
        {"id": "b", "name": "Beta", "etype": "concept"},
        {"id": "c", "name": "Gamma", "etype": "concept"},
        {"id": "d", "name": "Delta", "etype": "concept"},
    ]
    edges = [
        {"id": "light", "src": "a", "dst": "b", "relation": "uses",
         "layer": "entity", "weight": 1.0, "provenance": "{}"},
        {"id": "heavy", "src": "c", "dst": "d", "relation": "uses",
         "layer": "entity", "weight": 1.0, "provenance": "{}"},
    ]
    supports = [
        {"edge_id": "light", "memory_id": "", "confidence": 0.8,
         "provenance": "{}"},
        *[
            {"edge_id": "heavy", "memory_id": "", "confidence": 0.8,
             "provenance": "{}"}
            for _ in range(10)
        ],
    ]

    graph = graph_scene_module.build_canonical_graph(entities, edges, supports)

    assert graph["nodes"]["a"]["support_count"] == 1
    assert graph["nodes"]["c"]["support_count"] == 10
    assert graph["nodes"]["c"]["gravity_mass"] > graph["nodes"]["a"]["gravity_mass"]


@pytest.mark.parametrize("bad", [None, "bad", float("nan"), float("inf")])
def test_graph_scene_sanitizes_malformed_support_confidence(bad):
    entities = [
        {"id": "a", "name": "Alpha", "etype": "concept"},
        {"id": "b", "name": "Beta", "etype": "concept"},
    ]
    edges = [{
        "id": "edge", "src": "a", "dst": "b", "relation": "uses",
        "layer": "entity", "weight": float("nan"), "provenance": "{}",
    }]
    supports = [{
        "edge_id": "edge", "memory_id": "known", "confidence": bad,
        "provenance": "{}",
    }]

    edge = graph_scene_module.build_canonical_graph(entities, edges, supports)["edges"][0]

    assert math.isfinite(edge["weight"])
    assert math.isfinite(edge["confidence"])
    assert edge["confidence"] == pytest.approx(0.5)


@pytest.mark.parametrize("weight", [0, float("nan"), float("inf"), "bad"])
def test_graph_scene_preserves_falsy_weight_default_and_sanitizes_bad_weights(weight):
    entities = [
        {"id": "a", "name": "Alpha", "etype": "concept"},
        {"id": "b", "name": "Beta", "etype": "concept"},
    ]
    edges = [{
        "id": "edge", "src": "a", "dst": "b", "relation": "uses",
        "layer": "entity", "weight": weight, "provenance": "{}",
    }]

    canonical = graph_scene_module.build_canonical_graph(entities, edges, [])
    complete = build_graph_scene("w", entities, edges, [], level="complete")
    relation = next(edge for edge in complete["edges"]
                    if edge["connector_kind"] == "entity_relation")

    assert canonical["edges"][0]["weight"] == 1.0
    assert relation["weight"] == 1.0


def test_overview_ranks_communities_by_the_mass_sent_to_physics(monkeypatch):
    nodes = {}
    community_members = {}
    community_anchors = {}

    def add_community(community_id, gravity_masses, *, global_anchor=False):
        member_ids = []
        for index, gravity_mass in enumerate(gravity_masses):
            node_id = f"{community_id}_{index}"
            member_ids.append(node_id)
            nodes[node_id] = {
                "id": node_id,
                "canonical_id": node_id,
                "label": node_id,
                "type": "concept",
                "member_ids": [node_id],
                "member_count": 1,
                "repo_ids": [],
                "weighted_degree": 0.0,
                "pagerank": 0.0,
                "support_count": 0,
                "entity_quality": 1.0,
                "mass_score": 0.5,
                "gravity_mass": gravity_mass,
                "visual_radius": 5.0,
                "component_id": f"component_{community_id}",
                "community_id": community_id,
                "anchor_role": "global" if global_anchor and index == 0 else (
                    "community" if index == 0 else "none"
                ),
                "core_affinity": 0.5,
                "scene_rank": 0.5,
            }
        community_members[community_id] = member_ids
        community_anchors[community_id] = member_ids[0]

    add_community("c0", [8.0], global_anchor=True)
    add_community("ca", [8.0])
    add_community("cb", [3.0, 3.0])
    for index in range(34):
        add_community(f"f{index:02d}", [3.2, 3.2])

    fake_graph = {
        "nodes": nodes,
        "edges": [],
        "member_to_canonical": {node_id: node_id for node_id in nodes},
        "community_members": community_members,
        "community_anchors": community_anchors,
        "global_anchor": "c0_0",
    }
    monkeypatch.setattr(
        graph_scene_module, "build_canonical_graph", lambda *_args, **_kwargs: fake_graph
    )

    scene = graph_scene_module.build_graph_scene("w", [], [], [])
    chosen = {community["id"] for community in scene["communities"]}
    black_hole = next(node for node in scene["nodes"]
                      if node["anchor_role"] == "global")

    # System physics uses the combined member mass, so sqrt must not be applied before
    # aggregation: ca weighs 8 while cb weighs 6.
    assert "ca" in chosen
    assert "cb" not in chosen
    assert black_hole["gravity_mass"] == max(
        community["mass"] for community in scene["communities"]
        if community["member_count"] == 1
    )
    assert (black_hole["x"], black_hole["y"]) == (0.0, 0.0)


def test_overview_excludes_obvious_regex_extraction_noise_even_when_connected():
    entities = [
        {"id": "product", "name": "Engraphis", "etype": "person_or_concept"},
        {"id": "graph", "name": "Knowledge Graph", "etype": "person_or_concept"},
        {"id": "all", "name": "All", "etype": "person_or_concept"},
        {"id": "true", "name": "True", "etype": "person_or_concept"},
        {"id": "check", "name": "Check", "etype": "person_or_concept"},
        {"id": "if_python", "name": "If Python System", "etype": "person_or_concept"},
        {"id": "python_side", "name": "Python-side", "etype": "person_or_concept"},
        {"id": "generated", "name": "Generated Response", "etype": "person_or_concept"},
        {"id": "supported", "name": "Supported Python-version",
         "etype": "person_or_concept"},
    ]
    edges = [
        {"id": "good", "src": "product", "dst": "graph", "relation": "uses",
         "layer": "entity", "weight": 1.0, "provenance": "{}"},
        {"id": "noise-a", "src": "all", "dst": "product", "relation": "uses",
         "layer": "entity", "weight": 4.0, "provenance": "{}"},
        {"id": "noise-b", "src": "true", "dst": "product", "relation": "uses",
         "layer": "entity", "weight": 4.0, "provenance": "{}"},
        {"id": "noise-c", "src": "check", "dst": "product", "relation": "uses",
         "layer": "entity", "weight": 4.0, "provenance": "{}"},
        {"id": "noise-d", "src": "if_python", "dst": "product", "relation": "uses",
         "layer": "entity", "weight": 4.0, "provenance": "{}"},
        {"id": "noise-e", "src": "python_side", "dst": "product", "relation": "uses",
         "layer": "entity", "weight": 4.0, "provenance": "{}"},
        {"id": "noise-f", "src": "generated", "dst": "product", "relation": "uses",
         "layer": "entity", "weight": 4.0, "provenance": "{}"},
        {"id": "noise-g", "src": "supported", "dst": "product", "relation": "uses",
         "layer": "entity", "weight": 4.0, "provenance": "{}"},
    ]

    scene = build_graph_scene("w", entities, edges, [], level="overview")
    system = build_graph_scene("w", entities, edges, [], level="system", system_id="product")
    explicit = build_graph_scene(
        "w", entities, edges, [], level="neighborhood", center_id="if_python", depth=0
    )

    assert {node["label"] for node in scene["nodes"]} == {"Engraphis", "Knowledge Graph"}
    assert all(node["entity_quality"] == 1.0 for node in scene["nodes"])
    assert {node["label"] for node in system["nodes"]} <= {"Engraphis", "Knowledge Graph"}
    assert "Engraphis" in {node["label"] for node in system["nodes"]}
    assert all(node["entity_quality"] == 1.0 for node in system["nodes"])
    assert [node["id"] for node in explicit["nodes"]] == ["if_python"]
    assert explicit["nodes"][0]["entity_quality"] == 0.0


def test_zero_evidence_ties_do_not_turn_isolates_into_maximum_mass_nodes():
    entities = [
        {"id": "a", "name": "Alpha", "etype": "concept"},
        {"id": "b", "name": "Beta", "etype": "concept"},
        {"id": "c", "name": "Gamma", "etype": "concept"},
    ]
    connected = [{
        "id": "ab", "src": "a", "dst": "b", "relation": "uses",
        "layer": "entity", "weight": 1.0, "provenance": "{}",
    }]

    isolated_graph = graph_scene_module.build_canonical_graph(entities, [], [])
    mixed_graph = graph_scene_module.build_canonical_graph(entities, connected, [])

    assert max(node["mass_score"] for node in isolated_graph["nodes"].values()) < 0.5
    assert mixed_graph["nodes"]["c"]["mass_score"] < mixed_graph["nodes"]["a"]["mass_score"]
    assert mixed_graph["nodes"]["c"]["mass_score"] < mixed_graph["nodes"]["b"]["mass_score"]


def test_skewed_evidence_keeps_mass_and_radius_contrast_after_top_n_cap():
    entities = [
        {"id": f"n{index}", "name": f"Node {index}", "etype": "concept"}
        for index in range(60)
    ]
    edges = []
    supports = []
    for source in range(10):
        for target in range(source + 1, 10):
            edge_id = f"core-{source}-{target}"
            edges.append({
                "id": edge_id, "src": f"n{source}", "dst": f"n{target}",
                "relation": "uses", "layer": "entity", "weight": 1.0,
                "provenance": "{}",
            })
            supports.extend({
                "edge_id": edge_id,
                "memory_id": f"core-memory-{source}-{target}-{support_index}",
                "confidence": 0.9,
                "provenance": "{}",
            } for support_index in range(4))
    for leaf in range(10, 60):
        edge_id = f"leaf-{leaf}"
        edges.append({
            "id": edge_id, "src": f"n{leaf % 10}", "dst": f"n{leaf}",
            "relation": "uses", "layer": "entity", "weight": 1.0,
            "provenance": "{}",
        })
        supports.append({
            "edge_id": edge_id, "memory_id": f"leaf-memory-{leaf}",
            "confidence": 0.9, "provenance": "{}",
        })

    scene = build_graph_scene(
        "w", entities, edges, supports,
        level="overview", node_limit=20, edge_limit=100,
    )
    masses = sorted(node["gravity_mass"] for node in scene["nodes"])
    radii = sorted(node["visual_radius"] for node in scene["nodes"])

    assert len(scene["nodes"]) == 20
    assert masses[-1] / masses[0] >= 4.0
    assert radii[-1] / radii[0] >= 1.5
    for node in scene["nodes"]:
        assert node["gravity_mass"] == pytest.approx(
            1.0 + 15.0 * node["mass_score"] ** 2, abs=1e-6
        )
        assert node["visual_radius"] == pytest.approx(
            1.5 + 2.0 * node["gravity_mass"] ** (2.0 / 3.0), abs=2e-6
        )


def test_visual_mass_mapping_preserves_live_fit_to_view_contrast():
    # The pre-change live top-300 painted range (4.47..9.13) corresponds to these
    # public masses under the old sqrt mapping. The replacement must remain compact
    # while preserving enough contrast to read after the whole galaxy is fitted.
    light_mass = ((4.47 - 2.0) / 2.0) ** 2
    heavy_mass = ((9.13 - 2.0) / 2.0) ** 2

    light_radius = graph_scene_module._visual_radius(light_mass)
    heavy_radius = graph_scene_module._visual_radius(heavy_mass)

    assert heavy_radius / light_radius >= 2.7
    assert heavy_radius < 13.0


def test_scene_seeds_mass_dominant_core_and_expanding_orbit_tiers(monkeypatch):
    nodes = {}
    member_ids = []
    for index in range(21):
        node_id = f"star-{index:02d}"
        gravity_mass = 16.0 - 0.6 * index
        member_ids.append(node_id)
        nodes[node_id] = {
            "id": node_id,
            "canonical_id": node_id,
            "label": f"Star {index}",
            "type": "concept",
            "member_ids": [node_id],
            "member_count": 1,
            "repo_ids": [],
            "repo_names": [],
            "weighted_degree": 21.0 - index,
            "pagerank": 1.0 - index / 21.0,
            "support_count": 21 - index,
            "entity_quality": 1.0,
            "mass_score": math.sqrt((gravity_mass - 1.0) / 15.0),
            "gravity_mass": gravity_mass,
            "visual_radius": graph_scene_module._visual_radius(gravity_mass),
            "component_id": "component-stars",
            "community_id": "community-stars",
            "anchor_role": "none",
            "core_affinity": 1.0 - index / 21.0,
            "scene_rank": 1.0 - index / 21.0,
            "anchor_eligible": True,
        }
    fake_graph = {
        "nodes": nodes,
        "edges": [],
        "member_to_canonical": {node_id: node_id for node_id in nodes},
        "community_members": {"community-stars": member_ids},
        "community_anchors": {"community-stars": member_ids[-1]},
        "global_anchor": member_ids[-1],
    }
    monkeypatch.setattr(
        graph_scene_module,
        "build_canonical_graph",
        lambda *_args, **_kwargs: copy.deepcopy(fake_graph),
    )

    scene = build_graph_scene("w", [], [], [], node_limit=40)
    by_id = {node["id"]: node for node in scene["nodes"]}
    core = by_id[member_ids[0]]

    assert scene["communities"][0]["anchor_id"] == core["id"]
    assert core["gravity_mass"] == max(node["gravity_mass"] for node in by_id.values())
    assert core["anchor_role"] == "global"
    assert core["system_anchor_id"] == core["id"]
    assert core["orbit_tier"] == 0
    assert core["orbit_radius"] == 0.0
    assert (core["x"], core["y"]) == (0.0, 0.0)
    assert core["galactic_radius"] == 0.0
    assert core["galactic_target_radius"] == 0.0
    assert core["galactic_radius_scale"] == 0.4
    assert core["galactic_initial_compactness"] == 0.8
    assert core["galactic_clearance_adjusted"] is False
    assert core["galactic_overlap"] is False
    assert core["galactic_arm"] == -1
    assert core["galactic_phase"] == 0.0
    assert 0.84 <= core["galactic_eccentricity"] <= 0.92
    assert all(node["system_anchor_id"] == core["id"] for node in by_id.values())
    assert [sum(node["orbit_tier"] == tier for node in by_id.values())
            for tier in range(4)] == [1, 4, 8, 8]

    satellites = sorted(
        (node for node in by_id.values() if node["id"] != core["id"]),
        key=lambda node: -node["gravity_mass"],
    )
    assert [node["orbit_tier"] for node in satellites] == sorted(
        node["orbit_tier"] for node in satellites
    )
    tier_radii = {
        tier: {node["orbit_radius"] for node in satellites
               if node["orbit_tier"] == tier}
        for tier in range(1, 4)
    }
    assert all(len(radii) == 1 for radii in tier_radii.values())
    assert [next(iter(tier_radii[tier])) for tier in range(1, 4)] == sorted(
        next(iter(tier_radii[tier])) for tier in range(1, 4)
    )
    for node in satellites:
        distance = math.hypot(node["x"] - core["x"], node["y"] - core["y"])
        assert 0.87 * node["orbit_radius"] <= distance <= node["orbit_radius"] + 1e-5
    assert len({(node["x"], node["y"]) for node in by_id.values()}) == len(by_id)
    assert scene["communities"][0]["radius"] >= max(
        node["orbit_radius"] + node["visual_radius"] for node in by_id.values()
    ) + 5.9

    # Recreate the otherwise-identical pre-contraction orbital positions using
    # the emitted scene seed.  Both local offsets and public orbit metadata are
    # exactly 80% of this reference, including every live satellite.
    reference_nodes = copy.deepcopy(fake_graph["nodes"])
    reference_slots, _reference_radii = graph_scene_module._assign_orbit_hierarchy(
        reference_nodes,
        fake_graph["community_members"],
        {"community-stars": core["id"]},
        radius_scale=1.0,
    )
    for node_id, node in by_id.items():
        if node_id == core["id"]:
            reference_x, reference_y = 0.0, 0.0
        else:
            reference_x, reference_y = graph_scene_module._orbit_position(
                0.0, 0.0, "community-stars", reference_slots[node_id],
                scene["meta"]["layout_seed"],
            )
        assert node["x"] == pytest.approx(0.8 * reference_x, abs=2e-6)
        assert node["y"] == pytest.approx(0.8 * reference_y, abs=2e-6)
        assert math.hypot(node["x"], node["y"]) == pytest.approx(
            0.8 * math.hypot(reference_x, reference_y), abs=2e-6
        )
        assert node["orbit_radius"] == pytest.approx(
            0.8 * reference_nodes[node_id]["orbit_radius"], abs=2e-6
        )


def test_community_spiral_packs_compact_preferred_targets_without_envelope_overlap():
    communities = [
        {"id": f"system-{index:02d}", "mass": 100.0 - index, "radius": radius}
        for index, radius in enumerate([69.6, 66.3, *([36.0] * 22)])
    ]

    positions, hints = graph_scene_module._community_positions(
        communities, "system-00", 1779033703, spacing=98.0
    )
    repeated = graph_scene_module._community_positions(
        communities, "system-00", 1779033703, spacing=98.0
    )
    assert (positions, hints) == repeated
    assert positions["system-00"] == (0.0, 0.0)
    assert hints["system-00"]["galactic_radius"] == 0.0
    assert hints["system-00"]["galactic_target_radius"] == 0.0
    assert hints["system-00"]["galactic_radius_scale"] == 0.4
    assert hints["system-00"]["galactic_initial_compactness"] == 0.8
    assert hints["system-00"]["galactic_overlap"] is False
    assert hints["system-00"]["galactic_arm"] == -1
    outer_hints = [hint for community_id, hint in hints.items() if community_id != "system-00"]
    assert len({hint["galactic_arm"] for hint in outer_hints}) in {2, 3}
    assert all(0.84 <= hint["galactic_eccentricity"] <= 0.92
               for hint in hints.values())

    for community in communities[1:]:
        community_id = community["id"]
        # 0.4 remains the compact preferred scale, but the resulting physical carrier orbit
        # must expand as needed for complete painted solar-system envelopes.
        assert hints[community_id]["galactic_preferred_radius"] <= hints[community_id]["galactic_radius"]
        assert hints[community_id]["galactic_target_radius"] == hints[community_id]["galactic_radius"]
        assert math.hypot(*positions[community_id]) == pytest.approx(
            hints[community_id]["galactic_radius"], abs=1e-6
        )

    overlap_pairs = 0
    for left_index, left in enumerate(communities):
        for right in communities[left_index + 1:]:
            distance = math.dist(positions[left["id"]], positions[right["id"]])
            if distance < 1.15 * (left["radius"] + right["radius"]) - 1e-8:
                overlap_pairs += 1
    assert overlap_pairs == 0
    assert not any(hint["galactic_overlap"] for hint in outer_hints)
    radial_span = max(math.hypot(x, y) for x, y in positions.values())
    x_span = max(x for x, _y in positions.values()) - min(
        x for x, _y in positions.values()
    )
    y_span = max(y for _x, y in positions.values()) - min(
        y for _x, y in positions.values()
    )
    outer_radii = sorted(
        math.hypot(x, y) for community_id, (x, y) in positions.items()
        if community_id != "system-00"
    )
    angles = sorted(
        math.atan2(y, x) % math.tau
        for community_id, (x, y) in positions.items()
        if community_id != "system-00"
    )
    angular_gaps = [
        ((angles[(index + 1) % len(angles)] - angle) % math.tau)
        for index, angle in enumerate(angles)
    ]
    mean_gap = sum(angular_gaps) / len(angular_gaps)
    gap_deviation = math.sqrt(sum(
        (gap - mean_gap) ** 2 for gap in angular_gaps
    ) / len(angular_gaps))
    assert outer_radii[-1] / outer_radii[0] >= 2.0
    assert gap_deviation / mean_gap >= 0.25
    assert len({round(gap, 3) for gap in angular_gaps}) >= len(angular_gaps) // 2
    # Envelope clearance grows a dense galaxy only as much as is geometrically necessary.
    assert radial_span < 1200.0
    assert max(x_span, y_span) < 2400.0


def test_community_spiral_spatial_traversal_is_subquadratic(monkeypatch):
    calls = 0
    original_hypot = math.hypot

    def counted_hypot(*values):
        nonlocal calls
        calls += 1
        return original_hypot(*values)

    monkeypatch.setattr(graph_scene_module.math, "hypot", counted_hypot)
    traversal_counts = []
    for count in (200, 400):
        before = calls
        communities = [
            {"id": f"system-{index:04d}", "mass": count - index, "radius": 36.0}
            for index in range(count)
        ]
        positions, _hints = graph_scene_module._community_positions(
            communities, "system-0000", 42, spacing=92.0
        )
        assert len(positions) == count
        traversal_counts.append(calls - before)

    assert traversal_counts[1] < 2.5 * traversal_counts[0]


def test_scene_bounds_public_support_ids_and_deduplicates_confidence():
    entities = [
        {"id": "a", "canonical_id": "a", "name": "Alpha", "etype": "concept"},
        {"id": "b", "canonical_id": "b", "name": "Beta", "etype": "concept"},
    ]
    edges = [{"id": "edge", "src": "a", "dst": "b", "relation": "uses",
              "layer": "entity", "weight": 1.0, "provenance": "{}"}]
    supports = [
        {"edge_id": "edge", "memory_id": "same", "source_kind": "manual",
         "confidence": 0.5, "provenance": "{}"},
        {"edge_id": "edge", "memory_id": "same", "source_kind": "structured",
         "confidence": 0.5, "provenance": "{}"},
        *[
            {"edge_id": "edge", "memory_id": f"mem_{index:03d}",
             "source_kind": "manual", "confidence": 0.5, "provenance": "{}"}
            for index in range(205)
        ],
    ]

    scene = build_graph_scene("w", entities, edges, supports)
    edge = scene["edges"][0]

    assert edge["support_count"] == 206
    assert len(edge["support_memory_ids"]) == 200
    assert edge["support_ids_truncated"] is True
    # Repeated normalized rows for one memory are one source, not two independent votes.
    baseline = build_graph_scene("w", entities, edges, supports[:2])["edges"][0]
    assert baseline["confidence"] == pytest.approx(0.5)


def _seed_service() -> tuple[MemoryService, str, str, str]:
    service = MemoryService.create(":memory:", graph_extractor="none")
    workspace_id = service.store.get_or_create_workspace("acme")
    memory_a = service.store.add_memory(MemoryRecord(
        id="", content="Alpha uses Beta.", workspace_id=workspace_id,
        scope=Scope.WORKSPACE,
        provenance={"trusted": True, "review_state": "approved"},
    ))
    memory_b = service.store.add_memory(MemoryRecord(
        id="", content="Beta causes Gamma.", workspace_id=workspace_id,
        scope=Scope.WORKSPACE,
        provenance={"trusted": True, "review_state": "approved"},
    ))
    alpha = service.store.upsert_entity(Node(
        id="", name="Alpha", ntype="concept", workspace_id=workspace_id,
    ))
    beta = service.store.upsert_entity(Node(
        id="", name="Beta", ntype="concept", workspace_id=workspace_id,
    ))
    gamma = service.store.upsert_entity(Node(
        id="", name="Gamma", ntype="concept", workspace_id=workspace_id,
    ))
    service.store.upsert_edge(Edge(
        id="edge_ab", src=alpha, dst=beta, relation="uses", workspace_id=workspace_id,
        provenance={"source": "structured_extractor", "memory_id": memory_a},
    ))
    service.store.upsert_edge(Edge(
        id="edge_bg", src=beta, dst=gamma, relation="causes", workspace_id=workspace_id,
        provenance={"source": "manual", "memory_id": memory_b},
    ))
    return service, alpha, beta, gamma


def test_graph_entity_evidence_avoids_rebuilding_the_workspace_graph(monkeypatch):
    service, alpha, _beta, _gamma = _seed_service()

    def fail_if_called(**_kwargs):
        raise AssertionError("node evidence must not materialize the full graph")

    monkeypatch.setattr(service, "_graph_scene_rows", fail_if_called)
    detail = service.graph_entity_evidence(alpha, workspace="acme")

    assert detail["canonical_id"] == alpha
    assert [(item["excerpt"], item["confidence"]) for item in detail["evidence"]] == [
        ("Alpha uses Beta.", 0.8),
    ]

    service.store.conn.execute("UPDATE memories SET valid_from=1")
    service.store.conn.execute("UPDATE edges SET valid_from=100 WHERE id='edge_ab'")
    service.store.conn.execute("UPDATE edge_supports SET valid_from=100 WHERE edge_id='edge_ab'")
    service.store.conn.commit()
    assert service.graph_entity_evidence(alpha, workspace="acme", as_of=99)["evidence"] == []
    visible = service.graph_entity_evidence(alpha, workspace="acme", as_of=101)
    assert visible["evidence"]
    assert "valid_to_recorded_at" in visible["evidence"][0]

    service.store.conn.execute(
        "UPDATE memories SET ingested_at=200 WHERE content='Alpha uses Beta.'"
    )
    service.store.conn.execute(
        "UPDATE edges SET ingested_at=200 WHERE id='edge_ab'"
    )
    service.store.conn.execute(
        "UPDATE edge_supports SET ingested_at=200 WHERE edge_id='edge_ab'"
    )
    service.store.conn.commit()
    assert service.graph_entity_evidence(
        alpha, workspace="acme", valid_at=101, known_at=199,
    )["evidence"] == []
    assert service.graph_entity_evidence(
        alpha, workspace="acme", valid_at=101, known_at=200,
    )["evidence"]


def test_graph_scene_applies_independent_world_and_system_anchors():
    service, _alpha, _beta, _gamma = _seed_service()
    service.store.conn.execute(
        "UPDATE memories SET valid_from=100, ingested_at=200"
    )
    service.store.conn.execute(
        "UPDATE edges SET valid_from=100, ingested_at=200"
    )
    service.store.conn.execute(
        "UPDATE edge_supports SET valid_from=100, ingested_at=200"
    )
    service.store.conn.execute("UPDATE entities SET created_at=200")
    service.store.conn.commit()

    unknown = service.graph_scene(
        workspace="acme", valid_at=150, known_at=199,
    )
    known = service.graph_scene(
        workspace="acme", as_of=150, valid_at=150, known_at=200,
    )

    assert unknown["nodes"] == [] and unknown["edges"] == []
    assert {edge["id"] for edge in known["edges"]} == {"edge_ab", "edge_bg"}
    assert known["meta"]["filters"]["valid_at"] == 150
    assert known["meta"]["filters"]["known_at"] == 200
    with pytest.raises(ValidationError, match="as_of and valid_at"):
        service.graph_scene(
            workspace="acme", as_of=149, valid_at=150, known_at=200,
        )


def test_graph_scene_keeps_support_metadata_when_closure_was_recorded_later():
    service, _alpha, _beta, _gamma = _seed_service()
    support = service.store.conn.execute(
        "SELECT memory_id FROM edge_supports WHERE edge_id='edge_ab'"
    ).fetchone()
    assert support is not None
    service.store.conn.execute(
        "UPDATE memories SET valid_from=0, ingested_at=0, valid_to=50, "
        "valid_to_recorded_at=100 WHERE id=?",
        (support["memory_id"],),
    )
    service.store.conn.execute(
        "UPDATE edge_supports SET valid_from=0, ingested_at=0, valid_to=50, "
        "valid_to_recorded_at=100 WHERE edge_id='edge_ab'"
    )
    service.store.conn.execute(
        "UPDATE edges SET valid_from=0, ingested_at=0, valid_to=50, "
        "valid_to_recorded_at=100 WHERE id='edge_ab'"
    )
    service.store.conn.execute("UPDATE entities SET created_at=0")
    service.store.conn.commit()

    scene = service.graph_scene(
        workspace="acme", valid_at=75, known_at=25,
    )

    edge = next(edge for edge in scene["edges"] if edge["id"] == "edge_ab")
    assert support["memory_id"] in edge["support_memory_ids"]


def test_graph_explorer_endpoints_and_legacy_graph_gets_are_read_only():
    service, alpha, _beta, gamma = _seed_service()
    app = FastAPI()
    app.include_router(v2_api.router)
    v2_api.set_service(service)
    client = TestClient(app)

    before = {
        table: service.store.conn.execute(
            f"SELECT COUNT(*) AS n FROM {table}"
        ).fetchone()["n"]
        for table in ("entities", "edges", "edge_supports")
    }
    scene_response = client.get("/api/graph/scene", params={"workspace": "acme"})
    assert scene_response.status_code == 200
    scene = scene_response.json()
    assert set(scene) == {
        "meta", "nodes", "edges", "communities", "community_bridges", "facets"
    }
    assert scene["meta"]["shown_nodes"] == 3

    suggestions = client.get(
        "/api/graph/suggest", params={"workspace": "acme", "query": "alp"}
    ).json()
    assert suggestions["groups"]["entities"][0]["label"] == "Alpha"

    detail = client.get(
        f"/api/graph/entities/{alpha}", params={"workspace": "acme"}
    )
    assert detail.status_code == 200
    assert detail.json()["canonical_id"] == alpha
    assert detail.json()["evidence"]

    node_evidence = client.get(
        f"/api/graph/entities/{alpha}/memories", params={"workspace": "acme"}
    )
    assert node_evidence.status_code == 200
    assert node_evidence.json()["canonical_id"] == alpha
    assert node_evidence.json()["evidence"][0]["excerpt"] == "Alpha uses Beta."

    path = client.get("/api/graph/path", params={
        "workspace": "acme", "source": alpha, "target": gamma,
    }).json()
    assert path["found"] is True
    assert path["edge_ids"] == ["edge_ab", "edge_bg"]

    # A pre-existing memory with extraction subsequently enabled must not be lazily
    # materialized by either the compatibility GET or the new scene GET.
    service.remember("Delta works at Example Corp.", workspace="acme", scope="workspace")
    service.engine.graph_extractor = get_graph_extractor("regex")
    before_lazy = service.store.conn.execute(
        "SELECT COUNT(*) AS n FROM entities"
    ).fetchone()["n"]
    assert client.get("/api/graph", params={"workspace": "acme"}).status_code == 200
    assert client.get("/api/graph/scene", params={"workspace": "acme"}).status_code == 200
    after_lazy = service.store.conn.execute(
        "SELECT COUNT(*) AS n FROM entities"
    ).fetchone()["n"]
    assert after_lazy == before_lazy

    after = {
        table: service.store.conn.execute(
            f"SELECT COUNT(*) AS n FROM {table}"
        ).fetchone()["n"]
        for table in ("entities", "edges", "edge_supports")
    }
    assert after == before


def test_complete_scene_api_returns_all_scoped_memories_and_connector_kinds():
    service, _alpha, _beta, _gamma = _seed_service()
    workspace_id = service.store.conn.execute(
        "SELECT id FROM workspaces WHERE name='acme'"
    ).fetchone()["id"]
    existing = [row["id"] for row in service.store.conn.execute(
        "SELECT id FROM memories WHERE workspace_id=? ORDER BY id", (workspace_id,)
    ).fetchall()]
    third = service.store.add_memory(MemoryRecord(
        id="", content="A standalone procedural memory.",
        mtype=MemoryType.PROCEDURAL, workspace_id=workspace_id,
        scope=Scope.WORKSPACE,
    ))
    service.store.add_link(existing[0], third, relation="related", reason="manual")
    app = FastAPI()
    app.include_router(v2_api.router)
    v2_api.set_service(service)
    client = TestClient(app)

    response = client.get("/api/graph/scene", params={
        "workspace": "acme", "level": "complete",
    })

    assert response.status_code == 200
    scene = response.json()
    meta = scene["meta"]
    assert meta["level"] == "complete"
    assert meta["complete_scene"] is True
    assert meta["safety_state"] == "full"
    assert meta["degraded"] is False
    assert meta["truncated"] is False
    assert meta["memory_nodes"] == 3
    assert meta["entity_nodes"] == 3
    assert meta["raw_relations"] == 2
    assert meta["evidence_connectors"] == 4
    assert meta["memory_connectors"] == 1
    assert meta["payload_bytes_estimate"] > 0
    assert meta["safety_limits"] == {
        "entity_rows": 40_000,
        "raw_relations": 200_000,
        "evidence_rows": 500_000,
        "memory_nodes": 100_000,
        "memory_connectors": 300_000,
        "code_memory_connectors": 300_000,
        "payload_bytes": 128 * 1024 * 1024,
    }
    assert meta["shown_nodes"] == meta["total_nodes"]
    assert meta["shown_edges"] == meta["total_edges"]
    assert {node["id"] for node in scene["nodes"] if node["node_kind"] == "memory"} \
        == {*existing, third}
    assert {edge["id"] for edge in scene["edges"]
            if edge["connector_kind"] == "entity_relation"} == {"edge_ab", "edge_bg"}
    assert all(bridge["edge_ids_truncated"] is False
               for bridge in scene["community_bridges"])

    legacy = client.get("/api/graph/scene", params={
        "workspace": "acme", "level": "full", "node_limit": 3,
    })
    assert legacy.status_code == 200
    assert legacy.json()["meta"]["level"] == "complete"
    assert legacy.json()["meta"]["include_memory_nodes"] is False

    limited = client.get("/api/graph/scene", params={
        "workspace": "acme", "level": "complete", "node_limit": 3,
    })
    assert limited.status_code == 400
    assert "do not accept node_limit" in limited.json()["detail"]["error"]


def test_graph_scene_code_overlay_degrades_without_repo_filter(monkeypatch):
    service = MemoryService.create(":memory:", graph_extractor="none")
    calls = []

    def fake_graph_scene(**kwargs):
        calls.append(kwargs)
        if kwargs["include_code"]:
            raise ValidationError(
                "graph analysis exceeds the entity candidate limit; "
                "filter the code overlay by repository"
            )
        return {"meta": {"level": kwargs["level"]}, "nodes": [], "edges": []}

    monkeypatch.setattr(service, "graph_scene", fake_graph_scene)
    app = FastAPI()
    app.include_router(v2_api.router)
    v2_api.set_service(service)
    try:
        response = TestClient(app).get("/api/graph/scene", params={
            "workspace": "acme", "include_code": True,
        })
    finally:
        v2_api.set_service(None)

    assert response.status_code == 200
    assert [call["include_code"] for call in calls] == [True, False]
    assert response.json()["meta"]["degraded_reason"] == (
        "code_overlay_requires_repository_filter"
    )
    assert response.json()["meta"]["include_code"] is False


def test_graph_scene_code_overlay_failure_does_not_fail_entity_graph(monkeypatch):
    service = MemoryService.create(":memory:", graph_extractor="none")
    calls = []

    def fake_graph_scene(**kwargs):
        calls.append(kwargs)
        if kwargs["include_code"]:
            raise RuntimeError("private code overlay failure")
        return {"meta": {"level": kwargs["level"]}, "nodes": [], "edges": []}

    monkeypatch.setattr(service, "graph_scene", fake_graph_scene)
    app = FastAPI()
    app.include_router(v2_api.router)
    v2_api.set_service(service)
    try:
        response = TestClient(app).get("/api/graph/scene", params={
            "workspace": "acme", "include_code": True,
        })
    finally:
        v2_api.set_service(None)

    assert response.status_code == 200
    assert [call["include_code"] for call in calls] == [True, False]
    assert response.json()["meta"]["degraded_reason"] == "code_overlay_failed"
    assert response.json()["meta"]["include_code"] is False


def test_complete_scene_capacity_error_is_explicit_and_never_samples(monkeypatch):
    service = MemoryService.create(":memory:", graph_extractor="none")
    workspace_id = service.store.get_or_create_workspace("acme")
    for content in ("one", "two"):
        service.store.add_memory(MemoryRecord(
            id="", content=content, workspace_id=workspace_id,
            scope=Scope.WORKSPACE,
        ))
    monkeypatch.setattr(service_module, "MAX_GRAPH_COMPLETE_MEMORIES", 1)
    app = FastAPI()
    app.include_router(v2_api.router)
    v2_api.set_service(service)
    response = TestClient(app).get("/api/graph/scene", params={
        "workspace": "acme", "level": "complete",
    })

    assert response.status_code == 413
    detail = response.json()["detail"]
    assert detail["safety_state"] == "capacity_exceeded"
    assert detail["degraded"] is True
    assert detail["truncated"] is False
    assert detail["resource"] == "memory nodes"
    assert detail["count"] == 2
    assert detail["limit"] == 1


def test_graph_scene_filters_supporting_memory_type_and_time_window():
    service = MemoryService.create(":memory:", graph_extractor="none")
    workspace_id = service.store.get_or_create_workspace("acme")
    semantic = service.store.add_memory(MemoryRecord(
        id="", content="Alpha uses Beta.", mtype=MemoryType.SEMANTIC,
        workspace_id=workspace_id, scope=Scope.WORKSPACE, valid_from=100,
        ingested_at=100,
    ))
    procedural = service.store.add_memory(MemoryRecord(
        id="", content="Beta deploys Gamma.", mtype=MemoryType.PROCEDURAL,
        workspace_id=workspace_id, scope=Scope.WORKSPACE, valid_from=200,
        ingested_at=200,
    ))
    alpha = service.store.upsert_entity(Node(
        id="", name="Alpha", ntype="concept", workspace_id=workspace_id,
    ))
    beta = service.store.upsert_entity(Node(
        id="", name="Beta", ntype="concept", workspace_id=workspace_id,
    ))
    gamma = service.store.upsert_entity(Node(
        id="", name="Gamma", ntype="concept", workspace_id=workspace_id,
    ))
    service.store.upsert_edge(Edge(
        id="edge_semantic", src=alpha, dst=beta, relation="uses",
        workspace_id=workspace_id, valid_from=100, ingested_at=100,
        provenance={"source": "structured", "memory_id": semantic},
    ))
    service.store.upsert_edge(Edge(
        id="edge_procedural", src=beta, dst=gamma, relation="deploys",
        workspace_id=workspace_id, valid_from=200, ingested_at=200,
        provenance={"source": "manual", "memory_id": procedural},
    ))
    app = FastAPI()
    app.include_router(v2_api.router)
    v2_api.set_service(service)
    client = TestClient(app)

    response = client.get("/api/graph/scene", params={
        "workspace": "acme", "memory_types": "procedural",
        "time_from": 150, "time_to": 250,
    })

    assert response.status_code == 200
    scene = response.json()
    assert {edge["id"] for edge in scene["edges"]} == {"edge_procedural"}
    assert {node["label"] for node in scene["nodes"]} == {"Beta", "Gamma"}
    assert scene["meta"]["filters"]["memory_types"] == ["procedural"]
    assert scene["meta"]["filters"]["time_from"] == 150
    assert scene["facets"]["memory_types"] == [
        {"value": "procedural", "count": 1}
    ]
    context = {
        "workspace": "acme", "memory_types": "procedural",
        "time_from": 150, "time_to": 250,
        "include_weak_cooccurrence": False,
    }
    suggestions = client.get(
        "/api/graph/suggest", params={**context, "q": "Alpha"}
    ).json()
    # Identity search remains complete-index even while evidence-backed memory
    # suggestions honor the active memory/time scope.
    assert [item["id"] for item in suggestions["groups"]["entities"]] == [alpha]
    assert suggestions["groups"]["memories"] == []
    detail = client.get(f"/api/graph/entities/{beta}", params=context).json()
    assert {edge["id"] for edge in detail["relations"]} == {"edge_procedural"}
    path = client.get("/api/graph/path", params={
        **context, "source": alpha, "target": gamma,
    }).json()
    assert path["found"] is False
    assert client.get("/api/graph/scene", params={
        "workspace": "acme", "time_from": 250, "time_to": 150,
    }).status_code == 400


def test_graph_suggest_does_not_let_extractor_fragments_crowd_out_exact_identity():
    assert graph_scene_module.is_obvious_entity_noise(
        "Full Python", "person_or_concept",
    ) is False
    assert graph_scene_module.is_broad_search_fragment(
        "Full Python", "person_or_concept",
    ) is True
    service = MemoryService.create(":memory:", graph_extractor="none")
    workspace_id = service.store.get_or_create_workspace("acme")
    python_id = service.store.upsert_entity(Node(
        id="", name="Python", ntype="person_or_concept", workspace_id=workspace_id,
    ))
    fragment_id = service.store.upsert_entity(Node(
        id="", name="If Python", ntype="person_or_concept", workspace_id=workspace_id,
    ))
    broad_fragments = [
        "Python-based", "No Python", "Add Python", "Added Python", "Full Python",
        "Three Python", "Orphan-Python", "Ignored Python", "Ignores Python",
        "Compiled Python", "Codex-descended Python",
    ]
    for name in broad_fragments:
        service.store.upsert_entity(Node(
            id="", name=name, ntype="person_or_concept", workspace_id=workspace_id,
        ))

    broad = service.graph_suggest("Python", workspace="acme")
    assert [item["id"] for item in broad["groups"]["entities"]] == [python_id]
    assert [item["id"] for item in broad["groups"]["systems"]] == [python_id]

    exact_fragment = service.graph_suggest("If Python", workspace="acme")
    assert [item["id"] for item in exact_fragment["groups"]["entities"]] == [fragment_id]
    exact_id = service.graph_suggest(fragment_id, workspace="acme")
    assert [item["id"] for item in exact_id["groups"]["entities"]] == [fragment_id]


def test_scene_hash_versions_physics_and_index_generation():
    entities = [
        {"id": "a", "name": "Alpha", "etype": "concept"},
        {"id": "b", "name": "Beta", "etype": "concept"},
        {"id": "c", "name": "Gamma", "etype": "concept"},
    ]
    baseline_edges = [
        {"id": "ab", "src": "a", "dst": "b", "relation": "uses",
         "layer": "entity", "weight": 0.1, "provenance": "{}"},
        {"id": "bc", "src": "b", "dst": "c", "relation": "uses",
         "layer": "entity", "weight": 1.0, "provenance": "{}"},
    ]
    stronger_edges = [dict(edge) for edge in baseline_edges]
    stronger_edges[0]["weight"] = 4.0

    baseline = build_graph_scene("w", entities, baseline_edges, [], index_generation=4)
    stronger = build_graph_scene("w", entities, stronger_edges, [], index_generation=4)
    next_generation = build_graph_scene(
        "w", entities, baseline_edges, [], index_generation=5
    )

    assert baseline["meta"]["scene_hash"] != stronger["meta"]["scene_hash"]
    assert baseline["meta"]["scene_hash"] != next_generation["meta"]["scene_hash"]
    assert baseline["meta"]["algorithm_version"] == "galaxy-v7-system-envelope-packing"


def test_graph_scene_v7_flags_projection_repo_names_and_cache_identity():
    service, alpha, _beta, _gamma = _seed_service()
    workspace_id = service.store.conn.execute(
        "SELECT id FROM workspaces WHERE name='acme'"
    ).fetchone()["id"]
    repo_id = service.store.get_or_create_repo(workspace_id, "product")
    service.store.conn.execute(
        "UPDATE entities SET repo_id=? WHERE id=?", (repo_id, alpha)
    )
    service.store.upsert_entity(Node(
        id="", name="Isolated", ntype="concept", workspace_id=workspace_id,
    ))
    service.store.conn.commit()

    baseline = service.graph_scene(workspace="acme")
    connected = service.graph_scene(workspace="acme", connected_only=True)
    complete = service.graph_scene(
        workspace="acme", level="complete", include_memory_nodes=False,
    )

    assert baseline["meta"]["algorithm_version"] == "galaxy-v7-system-envelope-packing"
    assert baseline["meta"]["scene_hash"] != connected["meta"]["scene_hash"]
    assert baseline["meta"]["filters"]["connected_only"] is False
    assert connected["meta"]["filters"]["connected_only"] is True
    assert "Isolated" in {node["label"] for node in baseline["nodes"]}
    assert "Isolated" not in {node["label"] for node in connected["nodes"]}
    alpha_node = next(node for node in baseline["nodes"] if node["id"] == alpha)
    assert alpha_node["repo_names"] == ["product"]
    assert complete["meta"]["node_projection"] == "entities"
    assert complete["meta"]["include_memory_nodes"] is False
    assert {node["node_kind"] for node in complete["nodes"]} == {"entity"}

    app = FastAPI()
    app.include_router(v2_api.router)
    v2_api.set_service(service)
    try:
        accepted = TestClient(app).get("/api/graph/scene", params={
            "workspace": "acme", "level": "complete", "include_memory_nodes": False,
            "connected_only": True, "include_history": True,
        })
        assert accepted.status_code == 200
        assert accepted.json()["meta"]["node_projection"] == "entities"
        rejected = TestClient(app).get("/api/graph/scene", params={
            "workspace": "acme", "include_memory_nodes": False,
        })
        assert rejected.status_code == 400
        with pytest.raises(ValidationError, match="only accepted for complete"):
            service.graph_scene(workspace="acme", include_memory_nodes=False)
    finally:
        v2_api.set_service(None)


def test_graph_scene_repo_names_are_deterministic_and_bounded():
    entities = [
        {
            "id": f"member-{index}", "canonical_id": "canonical",
            "name": "Canonical", "etype": "concept", "repo_id": f"repo-{index}",
            "repo_name": f"Repo {index:03d}",
        }
        for index in range(graph_scene_module.PUBLIC_REPO_NAME_LIMIT + 5)
    ]

    scene = build_graph_scene("w", entities, [], [])

    node = scene["nodes"][0]
    assert len(node["repo_names"]) == graph_scene_module.PUBLIC_REPO_NAME_LIMIT
    assert node["repo_names"] == sorted(node["repo_names"], key=str.casefold)


def test_graph_scene_hash_changes_when_public_repo_metadata_changes():
    base = [
        {"id": "a", "canonical_id": "a", "name": "Alpha", "etype": "concept",
         "repo_id": "repo", "repo_name": "First"},
    ]
    changed = [dict(base[0], repo_name="Second")]

    first = build_graph_scene("w", base, [], [])
    second = build_graph_scene("w", changed, [], [])

    assert first["nodes"][0]["repo_names"] == ["First"]
    assert second["nodes"][0]["repo_names"] == ["Second"]
    assert first["meta"]["scene_hash"] != second["meta"]["scene_hash"]


def test_graph_scene_history_is_zero_physics_and_does_not_change_live_mass():
    service, alpha, beta, _gamma = _seed_service()
    closed_at = time.time() + 10.0
    service.store.invalidate_edge("edge_ab", at=closed_at)

    live = service.graph_scene(
        workspace="acme", valid_at=closed_at + 1.0, known_at=closed_at + 1.0,
    )
    history = service.graph_scene(
        workspace="acme", valid_at=closed_at + 1.0, known_at=closed_at + 1.0,
        include_history=True,
    )

    assert {edge["id"] for edge in live["edges"]} == {"edge_bg"}
    ghost = next(edge for edge in history["edges"] if edge["id"] == "edge_ab")
    assert ghost["ghost"] is True
    assert ghost["valid_to"] == closed_at
    assert ghost["strength"] == 0.0
    assert ghost["spring_strength"] == 0.0
    assert ghost["rest_length"] == 0.0
    alpha_node = next(node for node in history["nodes"] if node["id"] == alpha)
    beta_live = next(node for node in live["nodes"] if node["id"] == beta)
    beta_history = next(node for node in history["nodes"] if node["id"] == beta)
    assert alpha_node["ghost"] is True
    assert alpha_node["gravity_mass"] == 0.0
    assert beta_history["gravity_mass"] == beta_live["gravity_mass"]
    assert (beta_history["x"], beta_history["y"]) == (
        beta_live["x"], beta_live["y"]
    )
    assert history["meta"]["layout_seed"] == live["meta"]["layout_seed"]
    assert alpha_node["community_id"] not in {
        community["id"] for community in history["communities"]
    }
    assert history["meta"]["filters"]["include_history"] is True
    assert history["meta"]["scene_hash"] != live["meta"]["scene_hash"]
    complete = service.graph_scene(
        workspace="acme", level="complete", include_history=True,
        include_memory_nodes=False, valid_at=closed_at + 1.0,
        known_at=closed_at + 1.0,
    )
    complete_ghost = next(edge for edge in complete["edges"] if edge["id"] == "edge_ab")
    assert complete_ghost["connector_kind"] == "entity_relation"
    assert complete_ghost["ghost"] is True
    assert complete_ghost["strength"] == 0.0
    complete_alpha = next(node for node in complete["nodes"] if node["id"] == alpha)
    assert complete_alpha["community_id"] not in {
        community["id"] for community in complete["communities"]
    }


def test_graph_scene_history_collision_uses_consistent_ghost_node_id():
    entities = [
        {"id": "live", "canonical_id": "canon", "name": "Current", "etype": "concept"},
        {"id": "historical", "canonical_id": "canon", "name": "Former", "etype": "concept"},
        {"id": "other", "canonical_id": "other", "name": "Other", "etype": "concept"},
    ]
    edges = [
        {"id": "live-edge", "src": "live", "dst": "other", "relation": "uses",
         "layer": "entity", "weight": 1.0, "provenance": "{}"},
        {"id": "ghost-edge", "src": "historical", "dst": "other", "relation": "uses",
         "layer": "entity", "weight": 1.0, "ghost": True, "provenance": "{}"},
    ]

    scene = build_graph_scene("w", entities, edges, [], include_history=True)
    nodes_by_id = {node["id"]: node for node in scene["nodes"]}

    assert len(nodes_by_id) == len(scene["nodes"])
    assert nodes_by_id["canon"].get("ghost") is not True
    assert nodes_by_id["canon:ghost"]["ghost"] is True
    assert nodes_by_id["canon:ghost"]["id"] == "canon:ghost"
    ghost_edge = next(edge for edge in scene["edges"] if edge["id"] == "ghost-edge")
    assert ghost_edge["source"] == "canon:ghost"


def test_graph_scene_history_ghost_alias_does_not_overwrite_live_node():
    entities = [
        {"id": "live", "canonical_id": "canon", "name": "Current", "etype": "concept"},
        {"id": "live-ghost", "canonical_id": "canon:ghost", "name": "Literal Ghost", "etype": "concept"},
        {"id": "historical", "canonical_id": "canon", "name": "Former", "etype": "concept"},
        {"id": "other", "canonical_id": "other", "name": "Other", "etype": "concept"},
    ]
    edges = [
        {"id": "live-edge", "src": "live-ghost", "dst": "other", "relation": "uses",
         "layer": "entity", "weight": 1.0, "provenance": "{}"},
        {"id": "ghost-edge", "src": "historical", "dst": "other", "relation": "uses",
         "layer": "entity", "weight": 1.0, "ghost": True, "provenance": "{}"},
    ]

    scene = build_graph_scene("w", entities, edges, [], include_history=True)
    nodes_by_id = {node["id"]: node for node in scene["nodes"]}

    assert nodes_by_id["canon:ghost"].get("ghost") is not True
    assert nodes_by_id["canon:ghost:ghost"]["ghost"] is True
    assert {edge["id"]: edge["source"] for edge in scene["edges"]} == {
        "live-edge": "canon:ghost",
        "ghost-edge": "canon:ghost:ghost",
    }


def test_live_graph_excludes_edges_supported_only_by_session_memories():
    service, alpha, beta, _gamma = _seed_service()
    workspace_id = service.store.get_or_create_workspace("acme")
    repo_id = service.store.get_or_create_repo(workspace_id, "web")
    session = service.start_session("acme", repo="web", goal="private graph")
    private_id = service.store.add_memory(MemoryRecord(
        id="mem_session_only", content="private graph relation",
        workspace_id=workspace_id, repo_id=repo_id,
        session_id=session["session_id"], scope=Scope.SESSION,
    ))
    service.store.conn.execute("DELETE FROM edge_supports WHERE edge_id='edge_ab'")
    service.store.conn.execute(
        "INSERT INTO edge_supports(edge_id, memory_id, source_kind, confidence) "
        "VALUES ('edge_ab', ?, 'manual', 1.0)", (private_id,),
    )
    service.store.conn.commit()

    live = service.graph_scene(workspace="acme")

    assert {edge["id"] for edge in live["edges"]} == {"edge_bg"}
    assert alpha not in {edge["source"] for edge in live["edges"]}
    assert beta in {edge["source"] for edge in live["edges"]}


def test_history_edge_metadata_counts_appended_ghost_relations():
    service, _alpha, _beta, _gamma = _seed_service()
    closed_at = time.time() + 10.0
    service.store.invalidate_edge("edge_ab", at=closed_at)

    history = service.graph_scene(
        workspace="acme", include_history=True,
        valid_at=closed_at + 1.0, known_at=closed_at + 1.0,
    )

    assert history["meta"]["total_edges"] == 2
    assert history["meta"]["shown_edges"] == 2
    assert history["meta"]["truncated"] is False


def test_graph_scene_history_reserves_edge_cap_for_historical_relations():
    service, _alpha, _beta, _gamma = _seed_service()
    closed_at = time.time() + 10.0
    service.store.invalidate_edge("edge_ab", at=closed_at)

    history = service.graph_scene(
        workspace="acme", valid_at=closed_at + 1.0, known_at=closed_at + 1.0,
        include_history=True, edge_limit=1,
    )

    assert len(history["edges"]) == 1
    assert history["edges"][0]["id"] == "edge_ab"
    assert history["edges"][0]["ghost"] is True


def test_graph_scene_history_reserves_edge_cap_for_relations_between_live_nodes():
    entities = [
        {"id": node_id, "canonical_id": node_id, "name": node_id, "etype": "concept"}
        for node_id in ("left", "middle", "right")
    ]
    edges = [
        {"id": "live-left", "src": "left", "dst": "middle", "relation": "uses",
         "layer": "entity", "weight": 10.0},
        {"id": "live-right", "src": "middle", "dst": "right", "relation": "uses",
         "layer": "entity", "weight": 9.0},
        {"id": "history-live", "src": "left", "dst": "middle", "relation": "used",
         "layer": "entity", "weight": 1.0, "ghost": True},
    ]

    scene = build_graph_scene(
        "w", entities, edges, [], include_history=True, edge_limit=1,
    )

    assert [(edge["id"], edge["ghost"]) for edge in scene["edges"]] == [
        ("history-live", True),
    ]


@pytest.mark.parametrize("node_limit", [1, 2])
def test_graph_scene_history_keeps_one_relation_atomic_beyond_node_cap(node_limit):
    entities = [
        {"id": node_id, "canonical_id": node_id, "name": node_id, "etype": "concept"}
        for node_id in ("hub", "live-a", "live-b", "old-a", "old-b")
    ]
    edges = [
        {"id": "live-a", "src": "hub", "dst": "live-a", "relation": "uses",
         "layer": "entity", "weight": 10.0},
        {"id": "live-b", "src": "hub", "dst": "live-b", "relation": "uses",
         "layer": "entity", "weight": 9.0},
        {"id": "history", "src": "old-a", "dst": "old-b", "relation": "used",
         "layer": "entity", "weight": 1.0, "ghost": True},
    ]

    scene = build_graph_scene(
        "w", entities, edges, [], include_history=True,
        node_limit=node_limit, edge_limit=1,
    )

    assert {node["id"] for node in scene["nodes"]} == {"old-a", "old-b"}
    assert [(edge["id"], edge["ghost"]) for edge in scene["edges"]] == [
        ("history", True),
    ]


def test_graph_scene_history_binds_edge_support_to_the_edge_workspace():
    service, _alpha, _beta, _gamma = _seed_service()
    other_workspace_id = service.store.get_or_create_workspace("other")
    other_memory = service.store.add_memory(MemoryRecord(
        id="", content="Evidence from another workspace.",
        workspace_id=other_workspace_id, scope=Scope.WORKSPACE,
    ))
    service.store.conn.execute(
        "DELETE FROM edge_supports WHERE edge_id='edge_ab'"
    )
    service.store.conn.execute(
        "INSERT INTO edge_supports(edge_id, memory_id, source_kind, confidence) "
        "VALUES ('edge_ab', ?, 'manual', 1.0)", (other_memory,)
    )
    service.store.conn.commit()

    history = service.graph_scene(
        workspace="acme", include_history=True,
        valid_at=time.time() + 20.0, known_at=time.time() + 20.0,
    )

    assert "edge_ab" not in {edge["id"] for edge in history["edges"]}


def test_graph_scene_history_facets_keep_invalidated_supports_visible():
    service, _alpha, _beta, _gamma = _seed_service()
    closed_at = time.time() + 10.0
    service.store.conn.execute(
        "UPDATE memories SET valid_from=0, valid_to=?, valid_to_recorded_at=? "
        "WHERE content='Alpha uses Beta.'",
        (closed_at, closed_at),
    )
    service.store.conn.execute(
        "UPDATE edges SET valid_from=0, valid_to=?, valid_to_recorded_at=? "
        "WHERE id='edge_ab'",
        (closed_at, closed_at),
    )
    service.store.conn.execute(
        "UPDATE edge_supports SET valid_from=0, valid_to=?, valid_to_recorded_at=? "
        "WHERE edge_id='edge_ab'",
        (closed_at, closed_at),
    )
    service.store.conn.commit()

    history = service.graph_scene(
        workspace="acme", include_history=True, memory_types=["semantic"],
        valid_at=closed_at + 1.0, known_at=closed_at + 1.0,
    )

    edge = next(edge for edge in history["edges"] if edge["id"] == "edge_ab")
    assert edge["ghost"] is True
    assert edge["support_memory_ids"]


def test_graph_scene_history_includes_closed_code_rows_and_marks_them_ghost():
    service = MemoryService.create(":memory:", graph_extractor="none")
    workspace_id = service.store.get_or_create_workspace("acme")
    repo_id = service.store.get_or_create_repo(workspace_id, "web")
    source = service.store.upsert_symbol(
        repo_id=repo_id, kind="function", name="source", fqname="source",
        file="source.py", span="1:1-2:1", lang="python", commit=False,
    )
    target = service.store.upsert_symbol(
        repo_id=repo_id, kind="function", name="target", fqname="target",
        file="target.py", span="1:1-2:1", lang="python", commit=False,
    )
    edge_id = service.store.add_code_edge(
        repo_id=repo_id, src=source, dst=target, relation="calls", commit=False,
    )
    live = service.graph_scene(
        workspace="acme", level="complete", include_code=True,
        include_memory_nodes=False,
    )
    assert f"code-edge:{edge_id}" in {edge["id"] for edge in live["edges"]}
    closed_at = time.time() + 10.0
    service.store.conn.execute(
        "UPDATE symbols SET valid_from=0, valid_to=?, valid_to_recorded_at=?",
        (closed_at, closed_at),
    )
    service.store.conn.execute(
        "UPDATE code_edges SET valid_from=0, valid_to=?, valid_to_recorded_at=?",
        (closed_at, closed_at),
    )
    service.store.conn.commit()

    history = service.graph_scene(
        workspace="acme", level="complete", include_code=True,
        include_history=True, include_memory_nodes=False,
        valid_at=closed_at + 1.0, known_at=closed_at + 1.0,
    )

    assert {node["id"] for node in history["nodes"]} >= {
        f"code:{source}", f"code:{target}",
    }
    code_edge = next(
        edge for edge in history["edges"] if edge["id"] == f"code-edge:{edge_id}"
    )
    assert code_edge["ghost"] is True


def test_graph_scene_history_honors_known_at_for_expired_code_rows():
    service = MemoryService.create(":memory:", graph_extractor="none")
    workspace_id = service.store.get_or_create_workspace("acme")
    repo_id = service.store.get_or_create_repo(workspace_id, "web")
    memory_id = service.store.add_memory(MemoryRecord(
        id="", content="Historical code note.", workspace_id=workspace_id,
        repo_id=repo_id, scope=Scope.REPO,
    ))
    source = service.store.upsert_symbol(
        repo_id=repo_id, kind="function", name="source", fqname="source",
        file="source.py", span="1:1-2:1", lang="python", commit=False,
    )
    target = service.store.upsert_symbol(
        repo_id=repo_id, kind="function", name="target", fqname="target",
        file="target.py", span="1:1-2:1", lang="python", commit=False,
    )
    code_edge_id = service.store.add_code_edge(
        repo_id=repo_id, src=source, dst=target, relation="calls", commit=False,
    )
    code_link_id = service.store.link_memory_symbol(
        repo_id=repo_id, symbol_id=source, memory_id=memory_id,
        relation="documents", commit=False,
    )
    service.store.conn.execute(
        "UPDATE symbols SET valid_from=0, ingested_at=0, expired_at=200"
    )
    service.store.conn.execute(
        "UPDATE code_edges SET valid_from=0, ingested_at=0, expired_at=200"
    )
    service.store.conn.execute(
        "UPDATE code_memory_links SET valid_from=0, ingested_at=0, expired_at=200"
    )
    service.store.conn.execute(
        "UPDATE memories SET valid_from=0, ingested_at=0"
    )
    service.store.conn.commit()

    known = service.graph_scene(
        workspace="acme", level="complete", include_code=True,
        include_history=True, valid_at=1.0, known_at=100.0,
    )
    expired = service.graph_scene(
        workspace="acme", level="complete", include_code=True,
        include_history=True, valid_at=1.0, known_at=300.0,
    )

    assert {f"code:{source}", f"code:{target}"} <= {
        node["id"] for node in known["nodes"]
    }
    assert f"code-edge:{code_edge_id}" in {edge["id"] for edge in known["edges"]}
    assert code_link_id in {edge["id"] for edge in known["edges"]}
    assert f"code:{source}" not in {node["id"] for node in expired["nodes"]}
    assert f"code:{target}" not in {node["id"] for node in expired["nodes"]}
    assert f"code-edge:{code_edge_id}" not in {edge["id"] for edge in expired["edges"]}
    assert code_link_id not in {edge["id"] for edge in expired["edges"]}


def test_graph_history_does_not_expose_support_learned_after_known_at():
    service, _alpha, _beta, _gamma = _seed_service()
    support = service.store.conn.execute(
        "SELECT memory_id FROM edge_supports WHERE edge_id='edge_ab'"
    ).fetchone()
    assert support is not None
    service.store.conn.execute(
        "UPDATE edges SET valid_from=0, ingested_at=0 WHERE id='edge_ab'"
    )
    service.store.conn.execute(
        "UPDATE memories SET valid_from=0, ingested_at=0 WHERE id=?",
        (support["memory_id"],),
    )
    service.store.conn.execute(
        "UPDATE edge_supports SET valid_from=0, ingested_at=200 WHERE edge_id='edge_ab'"
    )
    service.store.conn.commit()

    scene = service.graph_scene(
        workspace="acme", valid_at=1.0, known_at=100.0, include_history=True,
    )

    assert "edge_ab" not in {edge["id"] for edge in scene["edges"]}


def test_graph_history_keeps_evidence_expired_after_known_at():
    service, _alpha, _beta, _gamma = _seed_service()
    support = service.store.conn.execute(
        "SELECT memory_id FROM edge_supports WHERE edge_id='edge_ab'"
    ).fetchone()
    assert support is not None
    service.store.conn.execute(
        "UPDATE edges SET valid_from=0, ingested_at=0, expired_at=200 "
        "WHERE id='edge_ab'"
    )
    service.store.conn.execute("UPDATE entities SET created_at=0")
    service.store.conn.execute(
        "UPDATE memories SET valid_from=0, ingested_at=0, expired_at=200 "
        "WHERE id=?",
        (support["memory_id"],),
    )
    service.store.conn.execute(
        "UPDATE edge_supports SET valid_from=0, ingested_at=0, expired_at=200 "
        "WHERE edge_id='edge_ab'"
    )
    service.store.conn.commit()

    scene = service.graph_scene(
        workspace="acme", level="complete", valid_at=1.0, known_at=100.0,
        include_history=True,
    )

    edge = next(edge for edge in scene["edges"] if edge["id"] == "edge_ab")
    assert edge["ghost"] is False


def test_complete_scene_history_returns_closed_memory_as_temporal_ghost():
    service = MemoryService.create(":memory:", graph_extractor="none")
    workspace_id = service.store.get_or_create_workspace("acme")
    memory_id = service.store.add_memory(MemoryRecord(
        id="", content="Historical decision.", workspace_id=workspace_id,
        scope=Scope.WORKSPACE,
    ))
    closed_at = time.time() + 10.0
    service.store.close_validity(memory_id, at=closed_at)

    scene = service.graph_scene(
        workspace="acme", level="complete", include_history=True,
        valid_at=closed_at + 1.0, known_at=closed_at + 1.0,
    )

    node = next(node for node in scene["nodes"] if node["id"] == memory_id)
    assert node["node_kind"] == "memory"
    assert node["ghost"] is True
    assert node["valid_to"] == closed_at
    assert node["gravity_mass"] == 0.0
    assert math.isfinite(node["x"])
    assert math.isfinite(node["y"])
    assert scene["communities"] == []
    assert scene["meta"]["node_projection"] == "all"


def test_complete_scene_history_returns_closed_memory_link_with_zero_physics():
    service = MemoryService.create(":memory:", graph_extractor="none")
    workspace_id = service.store.get_or_create_workspace("acme")
    memory_ids = [
        service.store.add_memory(MemoryRecord(
            id="", content=content, workspace_id=workspace_id,
            scope=Scope.WORKSPACE,
        ))
        for content in ("First decision.", "Second decision.")
    ]
    service.store.add_link(memory_ids[0], memory_ids[1], relation="related")
    closed_at = time.time() + 10.0
    service.store.conn.execute(
        "UPDATE mem_links SET valid_to=?, valid_to_recorded_at=?",
        (closed_at, time.time()),
    )
    service.store.conn.commit()

    scene = service.graph_scene(
        workspace="acme", level="complete", include_history=True,
        valid_at=closed_at + 1.0, known_at=closed_at + 1.0,
    )

    link = next(edge for edge in scene["edges"]
                if edge["connector_kind"] == "memory_link")
    assert link["ghost"] is True
    assert link["valid_to"] == closed_at
    assert link["strength"] == 0.0
    assert link["rest_length"] == 0.0
    assert link["spring_strength"] == 0.0


def test_graph_scene_cache_is_warm_and_invalidates_on_store_write():
    service, _alpha, _beta, _gamma = _seed_service()

    first = service.graph_scene(workspace="acme")
    second = service.graph_scene(workspace="acme")

    assert first["meta"]["cache_hit"] is False
    assert second["meta"]["cache_hit"] is True
    assert second["meta"]["scene_hash"] == first["meta"]["scene_hash"]

    workspace_id = service.store.get_or_create_workspace("acme")
    service.store.upsert_entity(Node(
        id="", name="Delta", ntype="concept", workspace_id=workspace_id,
    ))
    refreshed = service.graph_scene(workspace="acme")

    assert refreshed["meta"]["cache_hit"] is False
    assert refreshed["meta"]["total_nodes"] == first["meta"]["total_nodes"] + 1
    assert refreshed["meta"]["index_generation"] > first["meta"]["index_generation"]


def test_graph_scene_cache_separates_algorithm_contract(monkeypatch):
    service, _alpha, _beta, _gamma = _seed_service()

    first = service.graph_scene(workspace="acme")
    warm = service.graph_scene(workspace="acme")
    assert warm["meta"]["cache_hit"] is True

    monkeypatch.setattr(service_module, "GRAPH_SCENE_ALGORITHM_VERSION", "galaxy-test")
    monkeypatch.setattr(graph_scene_module, "ALGORITHM_VERSION", "galaxy-test")
    refreshed = service.graph_scene(workspace="acme")

    assert refreshed["meta"]["cache_hit"] is False
    assert refreshed["meta"]["algorithm_version"] == "galaxy-test"
    assert refreshed["meta"]["scene_hash"] != first["meta"]["scene_hash"]


def test_explicit_graph_index_dry_run_is_persisted_counted_and_audited():
    service, _alpha, _beta, _gamma = _seed_service()
    before = {
        table: service.store.conn.execute(
            f"SELECT COUNT(*) AS n FROM {table}"
        ).fetchone()["n"]
        for table in ("entities", "edges")
    }

    started = service.start_graph_index_job(workspace="acme", dry_run=True)
    deadline = time.time() + 5
    job = started
    while job["state"] in {"queued", "running"} and time.time() < deadline:
        time.sleep(0.01)
        job = service.graph_index_job(started["id"], workspace="acme")

    assert job["state"] == "completed"
    assert job["progress"] == 1.0
    assert job["counts"]["memories_scanned"] == 2
    assert job["counts"]["entity_mentions"] >= 3
    assert job["counts"]["entities_added"] == 0
    assert {
        table: service.store.conn.execute(
            f"SELECT COUNT(*) AS n FROM {table}"
        ).fetchone()["n"]
        for table in ("entities", "edges")
    } == before
    receipt = service.store.conn.execute(
        "SELECT operation, status FROM operation_receipts "
        "WHERE operation='graph_index' ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    assert dict(receipt) == {"operation": "graph_index", "status": "ok"}


def test_mutating_graph_index_is_bounded_atomic_and_returns_ready():
    service = MemoryService.create(":memory:", graph_extractor="none")
    pending = service.remember(
        "Alice Johnson works at Acme Corporation.",
        workspace="acme",
        scope="workspace",
    )
    service.engine.approve_for_prompt(
        pending["id"], reviewer="test", reason="approved fixture"
    )

    started = service.start_graph_index_job(workspace="acme", dry_run=False)
    deadline = time.time() + 5
    job = started
    while job["state"] in {"queued", "running"} and time.time() < deadline:
        time.sleep(0.01)
        job = service.graph_index_job(started["id"], workspace="acme")

    assert job["state"] == "completed"
    assert job["counts"]["memories_scanned"] == 1
    assert service.graph_index_status(workspace="acme")["index"]["state"] == "ready"
    assert service.store.conn.in_transaction is False
    assert service.store.conn.execute(
        "SELECT COUNT(*) AS n FROM entities"
    ).fetchone()["n"] == 2


def test_zero_item_graph_job_cancelled_before_claim_stays_cancelled():
    service = MemoryService.create(":memory:", graph_extractor="none")
    workspace_id = service.store.get_or_create_workspace("acme")
    now = time.time()
    service.store.conn.execute(
        "INSERT INTO jobs(id, workspace_id, kind, state, dry_run, total_items, "
        "processed_items, counts, errors, request, cancel_requested, runner_id, "
        "heartbeat_at, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "job_cancelled", workspace_id, "graph_index", "queued", 1, 0, 0,
            "{}", "[]", "{}", 1, service._graph_runner_id, now, now,
        ),
    )
    service.store.conn.commit()

    service._run_graph_index_job("job_cancelled")
    job = service.graph_index_job("job_cancelled", workspace="acme")

    assert job["state"] == "cancelled"
    assert job["cancel_requested"] is True


def test_stale_graph_worker_lease_recovers_rebuilding_state():
    service, _alpha, _beta, _gamma = _seed_service()
    workspace_id = service.store.get_or_create_workspace("acme")
    stale = time.time() - service_module.GRAPH_INDEX_LEASE_SECONDS - 1
    service.store.conn.execute(
        "INSERT INTO jobs(id, workspace_id, kind, state, dry_run, total_items, "
        "processed_items, counts, errors, request, cancel_requested, runner_id, "
        "heartbeat_at, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "job_stale", workspace_id, "graph_index", "running", 0, 2, 1,
            '{"memories_scanned":1,"error_count":0}', "[]", "{}", 0,
            "dev_gone", stale, stale,
        ),
    )
    service.store.conn.execute(
        "UPDATE graph_index_state SET state='rebuilding', active_job_id='job_stale' "
        "WHERE workspace_id=?", (workspace_id,),
    )
    service.store.conn.commit()

    scene = service.graph_scene(workspace="acme")
    job = service.graph_index_job("job_stale", workspace="acme")

    assert scene["meta"]["index_state"] == "ready"
    assert job["state"] == "failed"
    assert job["errors"][-1]["code"] == "worker_lease_expired"
    assert service.graph_index_status(workspace="acme")["index"]["active_job_id"] is None


def test_cross_service_graph_job_start_reuses_one_database_job(tmp_path, monkeypatch):
    database = tmp_path / "shared.db"
    first = MemoryService.create(str(database), graph_extractor="none")
    first.remember("Alpha uses Beta.", workspace="acme", scope="workspace")
    second = MemoryService.create(str(database), graph_extractor="none")
    release = threading.Event()
    entered = threading.Event()

    def blocked_worker(_service, _job_id):
        entered.set()
        release.wait(5)

    monkeypatch.setattr(MemoryService, "_run_graph_index_job", blocked_worker)
    barrier = threading.Barrier(3)
    results = []

    def launch(service):
        barrier.wait()
        results.append(service.start_graph_index_job(
            workspace="acme", dry_run=True
        ))

    callers = [threading.Thread(target=launch, args=(service,))
               for service in (first, second)]
    for caller in callers:
        caller.start()
    barrier.wait()
    for caller in callers:
        caller.join(5)

    try:
        assert entered.wait(1)
        assert len(results) == 2
        assert len({result["id"] for result in results}) == 1
        assert sum(bool(result["reused"]) for result in results) == 1
    finally:
        release.set()
        for service in (first, second):
            for worker in service._graph_job_threads.values():
                worker.join(5)
            service.store.close()


def test_cross_service_graph_status_is_one_database_snapshot(tmp_path, monkeypatch):
    database = tmp_path / "status-race.db"
    reader = MemoryService.create(str(database), graph_extractor="none")
    reader.remember("Alpha uses Beta.", workspace="acme", scope="workspace")
    writer = MemoryService.create(str(database), graph_extractor="none")
    index_read = threading.Event()
    continue_status = threading.Event()
    release_worker = threading.Event()
    original_info = reader._graph_index_info

    def paused_info(workspace_id):
        info = original_info(workspace_id)
        index_read.set()
        continue_status.wait(5)
        return info

    def blocked_worker(_service, _job_id):
        release_worker.wait(5)

    monkeypatch.setattr(reader, "_graph_index_info", paused_info)
    monkeypatch.setattr(MemoryService, "_run_graph_index_job", blocked_worker)
    result = {}
    status_thread = threading.Thread(
        target=lambda: result.setdefault(
            "status", reader.graph_index_status(workspace="acme")
        )
    )
    status_thread.start()
    assert index_read.wait(2)
    started = writer.start_graph_index_job(workspace="acme", dry_run=True)
    continue_status.set()
    status_thread.join(5)

    try:
        assert result["status"]["index"]["state"] == "ready"
        assert result["status"]["job"] is None
        assert started["state"] in {"queued", "running"}
    finally:
        release_worker.set()
        for worker in writer._graph_job_threads.values():
            worker.join(5)
        reader.store.close()
        writer.store.close()


def test_graph_job_memory_candidate_limit_fails_before_persisting(monkeypatch):
    service, _alpha, _beta, _gamma = _seed_service()
    monkeypatch.setattr(service_module, "MAX_GRAPH_INDEX_MEMORIES", 1)

    with pytest.raises(ValidationError, match="memory candidate limit"):
        service.start_graph_index_job(workspace="acme", dry_run=True)

    assert service.store.conn.in_transaction is False
    assert service.store.conn.execute(
        "SELECT COUNT(*) AS n FROM jobs"
    ).fetchone()["n"] == 0


def test_graph_job_candidate_limit_ignores_pending_rows(monkeypatch):
    service = MemoryService.create(":memory:", graph_extractor="none")
    workspace_id = service.store.get_or_create_workspace("acme")
    for index in range(105):
        service.store.add_memory(MemoryRecord(
            id="", content=f"Pending graph candidate {index}.", workspace_id=workspace_id,
            scope=Scope.WORKSPACE,
            provenance={"source": "import", "trusted": False, "review_state": "pending"},
        ))
    service.store.add_memory(MemoryRecord(
        id="", content="One approved graph candidate.", workspace_id=workspace_id,
        scope=Scope.WORKSPACE,
        provenance={"source": "human_review", "trusted": True, "review_state": "approved"},
    ))
    monkeypatch.setattr(service_module, "MAX_GRAPH_INDEX_MEMORIES", 1)
    monkeypatch.setattr(MemoryService, "_run_graph_index_job", lambda *_args, **_kwargs: None)

    started = service.start_graph_index_job(workspace="acme", dry_run=True)

    assert started["total_items"] == 1
    for worker in service._graph_job_threads.values():
        worker.join(5)


def test_active_graph_job_blocks_workspace_lifecycle_and_terminal_rows_are_deleted():
    service, _alpha, _beta, _gamma = _seed_service()
    workspace_id = service.store.get_or_create_workspace("acme")
    now = time.time()
    service.store.conn.execute(
        "INSERT INTO jobs(id, workspace_id, kind, state, dry_run, total_items, "
        "processed_items, counts, errors, request, cancel_requested, runner_id, "
        "heartbeat_at, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "job_active", workspace_id, "graph_index", "queued", 1, 2, 0,
            "{}", "[]", "{}", 0, "dev_live", now, now,
        ),
    )
    service.store.conn.commit()

    with pytest.raises(ValidationError, match="still active"):
        service.delete_workspace("acme")
    with pytest.raises(ValidationError, match="still active"):
        service.copy_workspace("acme", "acme-copy")

    service.store.conn.execute(
        "UPDATE jobs SET state='completed', finished_at=? WHERE id='job_active'", (now,)
    )
    service.store.conn.commit()
    assert service.delete_workspace("acme")["deleted"] is True
    assert service.store.conn.execute(
        "SELECT COUNT(*) AS n FROM jobs WHERE workspace_id=?", (workspace_id,)
    ).fetchone()["n"] == 0
    assert service.store.conn.execute(
        "SELECT COUNT(*) AS n FROM graph_index_state WHERE workspace_id=?", (workspace_id,)
    ).fetchone()["n"] == 0


def test_edge_support_delete_advances_graph_generation():
    service, _alpha, _beta, _gamma = _seed_service()
    workspace_id = service.store.get_or_create_workspace("acme")
    before = service._graph_index_info(workspace_id)["generation"]

    service.store.conn.execute("DELETE FROM edge_supports WHERE edge_id='edge_ab'")
    service.store.conn.commit()

    assert service._graph_index_info(workspace_id)["generation"] > before


def test_explicit_graph_index_write_populates_evidence_and_advances_generation():
    service = MemoryService.create(":memory:", graph_extractor="none")
    workspace_id = service.store.get_or_create_workspace("acme")
    memory_id = service.store.add_memory(MemoryRecord(
        id="", content="Alice works at Acme Corp.", workspace_id=workspace_id,
        scope=Scope.WORKSPACE,
        provenance={"trusted": True, "review_state": "approved"},
    ))
    initial = service.graph_index_status(workspace="acme")["index"]["generation"]

    started = service.start_graph_index_job(workspace="acme", dry_run=False)
    deadline = time.time() + 5
    job = started
    while job["state"] in {"queued", "running"} and time.time() < deadline:
        time.sleep(0.01)
        job = service.graph_index_job(started["id"], workspace="acme")

    status = service.graph_index_status(workspace="acme")
    supports = service.store.conn.execute(
        "SELECT memory_id, source_kind, confidence FROM edge_supports "
        "WHERE memory_id=? ORDER BY confidence DESC",
        (memory_id,),
    ).fetchall()
    assert job["state"] == "completed"
    assert job["counts"]["entities_added"] >= 2
    assert job["counts"]["relations_added"] >= 1
    assert status["index"]["state"] == "ready"
    assert status["index"]["active_job_id"] is None
    assert status["index"]["generation"] > initial
    assert supports
    assert all(row["memory_id"] == memory_id for row in supports)


def test_graph_index_job_honors_persisted_cancellation(monkeypatch):
    from engraphis.backends import graph_extractor as graph_extractor_module
    from engraphis.backends.graph_extractor import GraphExtraction

    service = MemoryService.create(":memory:", graph_extractor="none")
    workspace_id = service.store.get_or_create_workspace("acme")
    for content in ("Alice knows Bob.", "Carol knows Dana."):
        service.store.add_memory(MemoryRecord(
            id="", content=content, workspace_id=workspace_id, scope=Scope.WORKSPACE,
            provenance={"trusted": True, "review_state": "approved"},
        ))
    entered = threading.Event()
    release = threading.Event()

    class SlowExtractor:
        def extract(self, content, *, title=""):
            entered.set()
            release.wait(timeout=2)
            return GraphExtraction()

    monkeypatch.setattr(
        graph_extractor_module, "get_graph_extractor", lambda _kind: SlowExtractor()
    )
    started = service.start_graph_index_job(workspace="acme", dry_run=True)
    assert entered.wait(timeout=2)
    cancelled = service.cancel_graph_index_job(started["id"], workspace="acme")
    assert cancelled["cancel_requested"] is True
    release.set()
    deadline = time.time() + 5
    job = cancelled
    while job["state"] in {"queued", "running"} and time.time() < deadline:
        time.sleep(0.01)
        job = service.graph_index_job(started["id"], workspace="acme")

    assert job["state"] == "cancelled"
    assert job["processed_items"] == 1


def test_graph_reads_return_explicit_rebuilding_conflict():
    service, _alpha, _beta, _gamma = _seed_service()
    workspace_id = service.store.get_or_create_workspace("acme")
    service.store.conn.execute(
        "UPDATE graph_index_state SET state='rebuilding', active_job_id='job_test' "
        "WHERE workspace_id=?",
        (workspace_id,),
    )
    service.store.conn.commit()
    app = FastAPI()
    app.include_router(v2_api.router)
    v2_api.set_service(service)
    client = TestClient(app)

    response = client.get("/api/graph/scene", params={"workspace": "acme"})

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "error": "graph index rebuilding (job job_test)",
        "index_state": "rebuilding",
        "job_id": "job_test",
    }


def test_cross_service_scene_never_returns_partial_rebuild(tmp_path, monkeypatch):
    database = tmp_path / "scene-race.db"
    writer = MemoryService.create(str(database), graph_extractor="none")
    for content in ("Alpha works at Acme.", "Beta works at Bravo."):
        pending = writer.remember(content, workspace="acme", scope="workspace")
        writer.engine.approve_for_prompt(
            pending["id"], reviewer="test", reason="approved fixture"
        )
    ordered = writer.store.conn.execute(
        "SELECT content FROM memories ORDER BY id"
    ).fetchall()
    blocking_content = ordered[1]["content"]
    reader = MemoryService.create(str(database), graph_extractor="none")
    second_started = threading.Event()
    release_second = threading.Event()
    ready_check_passed = threading.Event()

    class BlockingExtractor:
        def extract(self, content, *, title=""):
            if content == blocking_content:
                second_started.set()
                release_second.wait(5)
            prefix = "Alpha" if "Alpha" in content else "Beta"
            company = "Acme" if "Acme" in content else "Bravo"
            return GraphExtraction(
                entities=[(prefix, "concept"), (company, "company")],
                relations=[(prefix, "works at", company)],
            )

    monkeypatch.setattr(
        graph_extractor_module, "get_graph_extractor", lambda _kind: BlockingExtractor()
    )
    original_revision = reader._graph_scene_revision

    def pause_after_ready_check():
        ready_check_passed.set()
        assert second_started.wait(5)
        return original_revision()

    monkeypatch.setattr(reader, "_graph_scene_revision", pause_after_ready_check)
    result = {}

    def read_scene():
        try:
            result["scene"] = reader.graph_scene(workspace="acme")
        except Exception as exc:  # captured for the parent test thread
            result["error"] = exc

    read_thread = threading.Thread(target=read_scene)
    read_thread.start()
    assert ready_check_passed.wait(2)
    job = writer.start_graph_index_job(workspace="acme", dry_run=False)
    assert second_started.wait(5)
    read_thread.join(5)

    try:
        assert "scene" not in result
        assert isinstance(result.get("error"), GraphIndexRebuilding)
    finally:
        release_second.set()
        deadline = time.time() + 5
        while job["state"] in {"queued", "running"} and time.time() < deadline:
            time.sleep(0.01)
            job = writer.graph_index_job(job["id"], workspace="acme")
        read_thread.join(5)
        reader.store.close()
        writer.store.close()


def test_current_graph_scene_cache_expires_at_next_temporal_boundary(monkeypatch):
    service, _alpha, _beta, _gamma = _seed_service()
    now = time.time()
    service.store.conn.execute(
        "UPDATE edges SET valid_to=? WHERE id='edge_bg'", (now + 1.0,)
    )
    service.store.conn.commit()
    clock = {"now": now}
    monkeypatch.setattr(service_module, "time", types.SimpleNamespace(
        time=lambda: clock["now"], perf_counter=time.perf_counter,
    ))

    first = service.graph_scene(workspace="acme")
    warm = service.graph_scene(workspace="acme")
    clock["now"] = now + 2.0
    expired = service.graph_scene(workspace="acme")

    assert first["meta"]["total_edges"] == 2
    assert warm["meta"]["cache_hit"] is True
    assert expired["meta"]["cache_hit"] is False
    assert expired["meta"]["total_edges"] == 1


def test_current_graph_scene_cache_tracks_memory_temporal_boundaries(monkeypatch):
    service, _alpha, _beta, _gamma = _seed_service()
    now = time.time()
    service.store.conn.execute(
        "UPDATE memories SET valid_from=?, ingested_at=?, expired_at=? "
        "WHERE content='Alpha uses Beta.'",
        (now + 1.0, now + 2.0, now + 4.0),
    )
    workspace_id = service.store.get_or_create_workspace("acme")
    service.store.conn.execute(
        "INSERT INTO entities(id, workspace_id, name, etype, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("ent_future", workspace_id, "Future", "concept", now + 3.0),
    )
    service.store.conn.commit()
    clock = {"now": now}
    monkeypatch.setattr(service_module, "time", types.SimpleNamespace(
        time=lambda: clock["now"], perf_counter=time.perf_counter,
    ))

    before = service.graph_scene(workspace="acme")
    warm = service.graph_scene(workspace="acme")
    clock["now"] = now + 1.5
    before_ingestion = service.graph_scene(workspace="acme")
    clock["now"] = now + 2.5
    after_ingestion = service.graph_scene(workspace="acme")
    clock["now"] = now + 3.5
    after_entity_creation = service.graph_scene(workspace="acme")
    clock["now"] = now + 4.5
    after_expiry = service.graph_scene(workspace="acme")

    assert before["meta"]["total_edges"] == 1
    assert warm["meta"]["cache_hit"] is True
    assert before_ingestion["meta"]["cache_hit"] is False
    assert before_ingestion["meta"]["total_edges"] == 1
    assert after_ingestion["meta"]["cache_hit"] is False
    assert after_ingestion["meta"]["total_edges"] == 2
    assert after_entity_creation["meta"]["cache_hit"] is False
    assert after_expiry["meta"]["cache_hit"] is False
    assert after_expiry["meta"]["total_edges"] == 1


def test_history_cache_expires_when_known_time_is_unanchored(monkeypatch):
    service, _alpha, _beta, _gamma = _seed_service()
    workspace_id = service.store.get_or_create_workspace("acme")
    recorded_at = 105.0
    service.store.conn.execute(
        "UPDATE memories SET valid_from=0, ingested_at=0, valid_to=5, "
        "valid_to_recorded_at=? WHERE workspace_id=?",
        (recorded_at, workspace_id),
    )
    service.store.conn.execute(
        "UPDATE edge_supports SET valid_from=0, ingested_at=0, valid_to=5, "
        "valid_to_recorded_at=? WHERE edge_id='edge_ab'",
        (recorded_at,),
    )
    service.store.conn.execute(
        "UPDATE edges SET valid_from=0, ingested_at=0, valid_to=5, "
        "valid_to_recorded_at=? WHERE id='edge_ab'",
        (recorded_at,),
    )
    service.store.conn.execute(
        "UPDATE entities SET created_at=0 WHERE workspace_id=?",
        (workspace_id,),
    )
    service.store.conn.commit()
    clock = {"now": 100.0}
    monkeypatch.setattr(service_module, "time", types.SimpleNamespace(
        time=lambda: clock["now"], perf_counter=time.perf_counter,
    ))

    before_recording = service.graph_scene(
        workspace="acme", level="complete", include_history=True, valid_at=10.0,
    )
    clock["now"] = 106.0
    after_recording = service.graph_scene(
        workspace="acme", level="complete", include_history=True, valid_at=10.0,
    )

    before_edge = next(edge for edge in before_recording["edges"] if edge["id"] == "edge_ab")
    after_edge = next(edge for edge in after_recording["edges"] if edge["id"] == "edge_ab")
    assert before_edge["ghost"] is False
    assert after_recording["meta"]["cache_hit"] is False
    assert after_edge["ghost"] is True


@pytest.mark.parametrize(("kwargs", "message"), [
    ({"level": "unknown"}, "level must be one of"),
    ({"seeds": ["seed"] * 65}, "too many seeds"),
    ({"min_confidence": float("nan")}, "min_confidence"),
    ({"node_limit": 1001}, "node_limit"),
    ({"edge_limit": 2001}, "edge_limit"),
    ({"edge_limit": -1}, "edge_limit"),
])
def test_graph_scene_direct_service_inputs_are_bounded(kwargs, message):
    service, _alpha, _beta, _gamma = _seed_service()

    with pytest.raises(ValidationError, match=message):
        service.graph_scene(workspace="acme", **kwargs)



def test_graph_scene_accepts_the_1000_node_2000_relation_overview_limit():
    service, _alpha, _beta, _gamma = _seed_service()

    scene = service.graph_scene(
        workspace="acme", node_limit=1000, edge_limit=2000,
    )

    assert scene["meta"]["shown_nodes"] <= 1000
    assert scene["meta"]["shown_edges"] <= 2000

def test_graph_lookup_direct_service_inputs_are_bounded():
    service, _alpha, _beta, _gamma = _seed_service()

    with pytest.raises(ValidationError, match="query exceeds"):
        service.graph_suggest("q" * 1_001, workspace="acme")
    with pytest.raises(ValidationError, match="canonical_id exceeds"):
        service.graph_entity("e" * 201, workspace="acme")
    with pytest.raises(ValidationError, match="max_visits"):
        service.graph_path("a", "b", workspace="acme", max_visits=50_001)


def test_entity_evidence_rechecks_workspace_on_forged_memory_pointer():
    service, alpha, _beta, _gamma = _seed_service()
    private_workspace = service.store.get_or_create_workspace("private")
    secret = service.store.add_memory(MemoryRecord(
        id="", content="cross-workspace secret", workspace_id=private_workspace,
        scope=Scope.WORKSPACE,
    ))
    acme_workspace = service.store.get_or_create_workspace("acme")
    decoy = service.store.upsert_entity(Node(
        id="", name="Decoy", ntype="concept", workspace_id=acme_workspace,
    ))
    # Edge provenance is untrusted/syncable data. Even if it names a valid foreign
    # memory id, the second-hop evidence lookup must remain inside the requested scope.
    service.store.upsert_edge(Edge(
        id="edge_forged", src=alpha, dst=decoy, relation="mentions",
        workspace_id=acme_workspace,
        provenance={"source": "manual", "memory_id": secret},
    ))

    detail = service.graph_entity(alpha, workspace="acme")

    assert secret not in {item["memory_id"] for item in detail["evidence"]}
    assert all("cross-workspace secret" not in item["excerpt"]
               for item in detail["evidence"])


def test_entity_inspector_bounds_history_and_reports_complete_counts(monkeypatch):
    service, alpha, _beta, gamma = _seed_service()
    workspace_id = service.store.get_or_create_workspace("acme")
    service.store.upsert_edge(Edge(
        id="edge_old_one", src=alpha, dst=gamma, relation="preceded",
        workspace_id=workspace_id,
    ))
    service.store.upsert_edge(Edge(
        id="edge_old_two", src=gamma, dst=alpha, relation="replaced",
        workspace_id=workspace_id,
    ))
    closed_at = time.time()
    service.store.invalidate_edge("edge_old_one", at=closed_at)
    service.store.invalidate_edge("edge_old_two", at=closed_at + 0.001)
    monkeypatch.setattr(service_module, "GRAPH_ENTITY_HISTORY_LIMIT", 1)

    detail = service.graph_entity(alpha, workspace="acme")

    assert detail["totals"]["history"] == 2
    assert detail["truncation"]["history"] is True
    assert len(detail["history"]) == 1
    assert detail["history"][0]["event"] == "Relation invalidated"


def test_graph_analysis_candidate_limit_fails_bounded(monkeypatch):
    service, _alpha, _beta, _gamma = _seed_service()
    monkeypatch.setattr(service_module, "MAX_GRAPH_ANALYSIS_ENTITIES", 2)

    with pytest.raises(ValidationError, match="entity candidate limit"):
        service.graph_scene(workspace="acme")


def test_graph_route_bounds_csv_filter_cardinality():
    service, _alpha, _beta, _gamma = _seed_service()
    app = FastAPI()
    app.include_router(v2_api.router)
    v2_api.set_service(service)
    client = TestClient(app)

    response = client.get("/api/graph/scene", params={
        "workspace": "acme",
        "entity_types": ",".join(f"type-{index}" for index in range(65)),
    })

    assert response.status_code == 422


def test_workspace_copy_remaps_canonical_and_support_ids():
    service, alpha, _beta, _gamma = _seed_service()
    copied = service.copy_workspace("acme", "acme-copy")
    copied_workspace_id = copied["id"]
    copied_entities = [dict(row) for row in service.store.conn.execute(
        "SELECT id, canonical_id FROM entities WHERE workspace_id=? ORDER BY id",
        (copied_workspace_id,),
    ).fetchall()]
    source_ids = {row["id"] for row in service.store.conn.execute(
        "SELECT id FROM entities WHERE workspace_id<>(?)", (copied_workspace_id,)
    ).fetchall()}
    copied_edges = [dict(row) for row in service.store.conn.execute(
        "SELECT id, provenance FROM edges WHERE workspace_id=? ORDER BY id",
        (copied_workspace_id,),
    ).fetchall()]
    copied_supports = [dict(row) for row in service.store.conn.execute(
        "SELECT s.edge_id, s.memory_id FROM edge_supports s "
        "JOIN edges e ON e.id=s.edge_id WHERE e.workspace_id=? ORDER BY s.id",
        (copied_workspace_id,),
    ).fetchall()]
    copied_memory_ids = {row["id"] for row in service.store.conn.execute(
        "SELECT id FROM memories WHERE workspace_id=?", (copied_workspace_id,)
    ).fetchall()}

    assert alpha in source_ids
    assert all(row["canonical_id"] not in source_ids for row in copied_entities)
    assert {row["edge_id"] for row in copied_supports} == {
        row["id"] for row in copied_edges
    }
    assert {row["memory_id"] for row in copied_supports} <= copied_memory_ids
    for edge in copied_edges:
        provenance = json.loads(edge["provenance"])
        assert provenance["memory_id"] in copied_memory_ids


def test_graph_entity_evidence_resolves_history_for_nested_ghost_and_live_endpoint():
    service = MemoryService.create(":memory:", graph_extractor="none")
    workspace_id = service.store.get_or_create_workspace("acme")
    archived_memory = service.store.add_memory(MemoryRecord(
        id="", content="Archived relation evidence.", workspace_id=workspace_id,
        scope=Scope.WORKSPACE,
    ))
    current_memory = service.store.add_memory(MemoryRecord(
        id="", content="Current relation evidence.", workspace_id=workspace_id,
        scope=Scope.WORKSPACE,
    ))
    literal_memory = service.store.add_memory(MemoryRecord(
        id="", content="Literal suffix evidence.", workspace_id=workspace_id,
        scope=Scope.WORKSPACE,
    ))
    archived = service.store.upsert_entity(Node(
        id="archived-member", name="Archived Origin", ntype="concept",
        workspace_id=workspace_id, canonical_id="canon",
    ))
    current = service.store.upsert_entity(Node(
        id="current-member", name="Current Primary", ntype="concept",
        workspace_id=workspace_id, canonical_id="canon",
    ))
    literal = service.store.upsert_entity(Node(
        id="literal-member", name="Literal Suffix", ntype="concept",
        workspace_id=workspace_id, canonical_id="canon:ghost",
    ))
    shared = service.store.upsert_entity(Node(
        id="shared-member", name="Shared Target", ntype="concept",
        workspace_id=workspace_id,
    ))
    for edge_id, source, memory_id in (
        ("archived-edge", archived, archived_memory),
        ("current-edge", current, current_memory),
        ("literal-edge", literal, literal_memory),
    ):
        service.store.upsert_edge(Edge(
            id=edge_id, src=source, dst=shared, relation="uses",
            workspace_id=workspace_id,
            provenance={"source": "manual", "memory_id": memory_id},
        ))
    closed_at = time.time() + 10.0
    service.store.invalidate_edge("archived-edge", at=closed_at)

    scene = service.graph_scene(
        workspace="acme", include_history=True,
        valid_at=closed_at + 1.0, known_at=closed_at + 1.0,
    )
    ghost = next(
        node for node in scene["nodes"]
        if node.get("ghost") and archived in node["member_ids"]
    )
    assert ghost["id"] == "canon:ghost:ghost"

    app = FastAPI()
    app.include_router(v2_api.router)
    v2_api.set_service(service)
    literal_response = TestClient(app).get(
        "/api/graph/entities/canon:ghost/memories",
        params={"workspace": "acme"},
    )
    assert literal_response.status_code == 200
    literal_detail = literal_response.json()
    assert literal_detail["canonical_id"] == "canon:ghost"
    assert [item["excerpt"] for item in literal_detail["evidence"]] == [
        "Literal suffix evidence.",
    ]

    response = TestClient(app).get(
        f"/api/graph/entities/{ghost['id']}/memories",
        params={
            "workspace": "acme",
            "valid_at": closed_at + 1.0,
            "known_at": closed_at + 1.0,
            "member_id": archived,
            "include_history": True,
        },
    )

    assert response.status_code == 200
    detail = response.json()
    assert detail["canonical_id"] == "canon"
    assert [item["excerpt"] for item in detail["evidence"]] == [
        "Archived relation evidence.",
    ]

    live_response = TestClient(app).get(
        "/api/graph/entities/canon/memories",
        params={
            "workspace": "acme",
            "valid_at": closed_at + 1.0,
            "known_at": closed_at + 1.0,
            "include_history": True,
        },
    )
    assert live_response.status_code == 200
    assert [item["excerpt"] for item in live_response.json()["evidence"]] == [
        "Archived relation evidence.",
    ]


def test_graph_scene_history_visibility_scopes_to_requested_repo():
    """Regression: visibility preclassification must apply the same repo scope
    as the subsequent edge/entity queries so unrelated-repo edges cannot
    promote private entities into the candidate set."""
    service = MemoryService.create(":memory:", graph_extractor="none")
    workspace_id = service.store.get_or_create_workspace("acme")
    repo_a = service.store.get_or_create_repo(workspace_id, "alpha")
    repo_b = service.store.get_or_create_repo(workspace_id, "beta")
    memory_a = service.store.add_memory(MemoryRecord(
        id="", content="Alpha evidence.", workspace_id=workspace_id,
        repo_id=repo_a, scope=Scope.REPO,
    ))
    entity_shared = service.store.upsert_entity(Node(
        id="ent_shared", name="Shared", ntype="concept", workspace_id=workspace_id,
    ))
    entity_shared2 = service.store.upsert_entity(Node(
        id="ent_shared2", name="Shared2", ntype="concept", workspace_id=workspace_id,
    ))
    entity_b_only = service.store.upsert_entity(Node(
        id="ent_b_only", name="BetaOnly", ntype="concept",
        workspace_id=workspace_id, repo_id=repo_b,
    ))
    # Edge in repo_b touching a shared entity — should be invisible when
    # filtering to repo_a.
    service.store.upsert_edge(Edge(
        id="edge_b", src=entity_b_only, dst=entity_shared, relation="uses",
        workspace_id=workspace_id, repo_id=repo_b,
        provenance={"source": "manual", "memory_id": memory_a},
    ))
    # Repo-less shared edge between two shared entities — must remain visible.
    service.store.upsert_edge(Edge(
        id="edge_shared", src=entity_shared, dst=entity_shared2, relation="relates",
        workspace_id=workspace_id,
        provenance={"source": "manual", "memory_id": memory_a},
    ))

    scene = service.graph_scene(
        workspace="acme", repo="alpha", include_history=True,
        valid_at=time.time() + 10.0, known_at=time.time() + 10.0,
    )
    node_ids = {node["id"] for node in scene["nodes"]}
    edge_ids = {edge["id"] for edge in scene["edges"]}

    assert entity_shared in node_ids
    assert entity_shared2 in node_ids
    assert "edge_shared" in edge_ids
    assert entity_b_only not in node_ids
    assert "edge_b" not in edge_ids


def test_graph_entity_evidence_history_scopes_to_requested_repo():
    service = MemoryService.create(":memory:", graph_extractor="none")
    workspace_id = service.store.get_or_create_workspace("acme")
    repo_a = service.store.get_or_create_repo(workspace_id, "alpha")
    repo_b = service.store.get_or_create_repo(workspace_id, "beta")
    shared = service.store.upsert_entity(Node(
        id="shared-evidence", name="Shared", ntype="concept", workspace_id=workspace_id,
    ))
    target = service.store.upsert_entity(Node(
        id="target-evidence", name="Target", ntype="concept", workspace_id=workspace_id,
    ))
    memory_a = service.store.add_memory(MemoryRecord(
        id="memory-alpha", content="Alpha-only history.", workspace_id=workspace_id,
        repo_id=repo_a, scope=Scope.REPO,
    ))
    memory_shared = service.store.add_memory(MemoryRecord(
        id="memory-shared", content="Shared history.", workspace_id=workspace_id,
        scope=Scope.WORKSPACE,
    ))
    memory_b = service.store.add_memory(MemoryRecord(
        id="memory-beta", content="Beta history.", workspace_id=workspace_id,
        repo_id=repo_b, scope=Scope.REPO,
    ))
    service.store.upsert_edge(Edge(
        id="edge-history-evidence", src=shared, dst=target, relation="relates",
        workspace_id=workspace_id,
    ))
    for memory_id in (memory_a, memory_shared, memory_b):
        service.store.add_edge_support(
            "edge-history-evidence", {"source": "manual", "memory_id": memory_id},
        )
    closed_at = time.time() + 10.0
    service.store.invalidate_edge("edge-history-evidence", at=closed_at)

    detail = service.graph_entity_evidence(
        shared, workspace="acme", repo="beta", include_history=True,
        valid_at=closed_at + 1.0, known_at=closed_at + 1.0,
    )

    assert detail["repo"] == "beta"
    assert {item["memory_id"] for item in detail["evidence"]} == {
        memory_shared, memory_b,
    }


def test_graph_scene_history_support_scopes_to_requested_repo():
    """Historical support enrichment must honor the selected repository."""
    service = MemoryService.create(":memory:", graph_extractor="none")
    workspace_id = service.store.get_or_create_workspace("acme")
    repo_a = service.store.get_or_create_repo(workspace_id, "alpha")
    repo_b = service.store.get_or_create_repo(workspace_id, "beta")
    memory_a = service.store.add_memory(MemoryRecord(
        id="", content="Alpha-only evidence.", workspace_id=workspace_id,
        repo_id=repo_a, scope=Scope.REPO,
    ))
    entity_shared = service.store.upsert_entity(Node(
        id="ent_shared_support", name="Shared", ntype="concept",
        workspace_id=workspace_id,
    ))
    entity_shared2 = service.store.upsert_entity(Node(
        id="ent_shared_support2", name="Shared2", ntype="concept",
        workspace_id=workspace_id,
    ))
    _entity_b_only = service.store.upsert_entity(Node(
        id="ent_b_only_support", name="BetaOnly", ntype="concept",
        workspace_id=workspace_id, repo_id=repo_b,
    ))
    service.store.upsert_edge(Edge(
        id="edge_shared_support", src=entity_shared, dst=entity_shared2,
        relation="relates", workspace_id=workspace_id,
        provenance={"source": "manual", "memory_id": memory_a},
    ))
    memory_shared = service.store.add_memory(MemoryRecord(
        id="", content="Shared evidence.", workspace_id=workspace_id,
        scope=Scope.WORKSPACE,
    ))
    memory_b = service.store.add_memory(MemoryRecord(
        id="", content="Beta-only evidence.", workspace_id=workspace_id,
        repo_id=repo_b, scope=Scope.REPO,
    ))
    service.store.add_edge_support(
        "edge_shared_support", {"source": "manual", "memory_id": memory_shared},
    )
    service.store.add_edge_support(
        "edge_shared_support", {"source": "manual", "memory_id": memory_b},
    )
    closed_at = time.time() + 1.0
    service.store.conn.execute(
        "UPDATE memories SET valid_from=0, valid_to=?, valid_to_recorded_at=? "
        "WHERE id=?",
        (closed_at, closed_at, memory_a),
    )
    service.store.conn.execute(
        "UPDATE edges SET valid_from=0, valid_to=?, valid_to_recorded_at=? "
        "WHERE id='edge_shared_support'",
        (closed_at, closed_at),
    )
    service.store.conn.commit()

    scene = service.graph_scene(
        workspace="acme", repo="beta", include_history=True,
        valid_at=closed_at + 1.0, known_at=closed_at + 1.0,
    )

    shared_edge = next(
        (edge for edge in scene["edges"] if edge["id"] == "edge_shared_support"),
        None,
    )
    assert shared_edge is not None
    support_ids = set(shared_edge.get("support_memory_ids") or [])
    assert memory_a not in support_ids
    assert {memory_shared, memory_b} <= support_ids
    assert shared_edge["support_count"] == 2


def test_graph_repo_filter_keeps_legacy_workspace_memory_ancestors():
    """Scope, not a retained legacy repo id, controls ancestor visibility."""
    service = MemoryService.create(":memory:", graph_extractor="none")
    try:
        workspace_id = service.store.get_or_create_workspace("acme")
        repo_a = service.store.get_or_create_repo(workspace_id, "alpha")
        service.store.get_or_create_repo(workspace_id, "beta")
        service.store.conn.executemany(
            "INSERT INTO entities(id, workspace_id, repo_id, name, etype, created_at) "
            "VALUES (?, ?, NULL, ?, 'concept', 0)",
            [
                ("legacy-ancestor-source", workspace_id, "Legacy Source"),
                ("legacy-ancestor-target", workspace_id, "Legacy Target"),
            ],
        )
        service.store.conn.execute("PRAGMA ignore_check_constraints=ON")
        workspace_memory = service.store.add_memory(MemoryRecord(
            id="mem_legacy_workspace_ancestor", content="shared ancestor evidence",
            workspace_id=workspace_id, repo_id=repo_a, scope=Scope.WORKSPACE,
            valid_from=0.0, valid_to=100.0, valid_to_recorded_at=100.0,
            ingested_at=0.0,
        ))
        service.store.conn.execute("PRAGMA ignore_check_constraints=OFF")
        edge_id = service.store.upsert_edge(Edge(
            id="edge_legacy_workspace_ancestor", src="legacy-ancestor-source",
            dst="legacy-ancestor-target", relation="relates", workspace_id=workspace_id,
            valid_from=0.0, valid_to=100.0, valid_to_recorded_at=100.0,
            ingested_at=0.0,
        ))
        service.store.add_edge_support(edge_id, {"memory_id": workspace_memory})
        service.store.conn.execute(
            "UPDATE edge_supports SET valid_from=0, valid_to=100, "
            "valid_to_recorded_at=100, ingested_at=0 WHERE edge_id=? AND memory_id=?",
            (edge_id, workspace_memory),
        )
        service.store.conn.commit()

        scene = service.graph_scene(
            workspace="acme", repo="beta", include_history=True,
            valid_at=150.0, known_at=150.0,
        )
        edge = next(item for item in scene["edges"] if item["id"] == edge_id)
        assert workspace_memory in edge["support_memory_ids"]

        detail = service.graph_entity_evidence(
            "legacy-ancestor-source", workspace="acme", repo="beta",
            include_history=True, valid_at=150.0, known_at=150.0,
        )
        assert [item["memory_id"] for item in detail["evidence"]] == [workspace_memory]
    finally:
        service.close()


def test_graph_scene_connected_only_uses_filtered_relations():
    """Facet-excluded relations must not keep their endpoints connected."""
    service = MemoryService.create(":memory:", graph_extractor="none")
    workspace_id = service.store.get_or_create_workspace("acme")
    source = service.store.upsert_entity(Node(
        id="ent_source", name="Source", ntype="concept", workspace_id=workspace_id,
    ))
    shared = service.store.upsert_entity(Node(
        id="ent_shared", name="Shared", ntype="concept", workspace_id=workspace_id,
    ))
    excluded = service.store.upsert_entity(Node(
        id="ent_excluded", name="Excluded", ntype="concept", workspace_id=workspace_id,
    ))
    service.store.upsert_edge(Edge(
        id="edge_excluded", src=source, dst=shared, relation="uses",
        workspace_id=workspace_id,
    ))
    service.store.upsert_edge(Edge(
        id="edge_selected", src=shared, dst=excluded, relation="likes",
        workspace_id=workspace_id,
    ))

    scene = service.graph_scene(
        workspace="acme", relations=["likes"], connected_only=True,
    )

    node_ids = {node["id"] for node in scene["nodes"]}
    assert source not in node_ids
    assert {shared, excluded} <= node_ids
    assert {edge["id"] for edge in scene["edges"]} == {"edge_selected"}


def test_graph_entity_preserves_literal_ghost_suffix():
    """Regression: canonical IDs ending in :ghost must not be stripped.

    The member_to_canonical mapping already resolves ghost aliases to their
    live canonical IDs when needed. Unconditionally stripping :ghost would
    corrupt legitimate IDs like 'canon:ghost' that happen to end with that
    suffix.
    """
    service = MemoryService.create(":memory:", graph_extractor="none")
    workspace_id = service.store.get_or_create_workspace("acme")
    service.store.upsert_entity(
        Node(
            id="canon:ghost", name="Literal Ghost", ntype="concept",
            workspace_id=workspace_id,
        )
    )

    result = service.graph_entity("canon:ghost", workspace="acme")
    assert result["canonical_id"] == "canon:ghost"
    assert result["label"] == "Literal Ghost"
