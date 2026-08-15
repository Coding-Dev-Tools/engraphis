"""Shared contract checks for the dashboard and graph-scene service."""
from __future__ import annotations

import json
import math
from pathlib import Path

from engraphis.core.graph_scene import project_all_presentation

FIXTURE = Path(__file__).with_name("graph_scene_fixture.json")


def _scene() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_graph_scene_fixture_has_stable_public_shape():
    scene = _scene()
    assert set(scene) == {
        "meta", "nodes", "edges", "communities", "community_bridges", "facets"
    }
    assert {
        "workspace", "level", "scene_hash", "index_generation", "total_nodes",
        "total_edges", "shown_nodes", "shown_edges", "truncated", "query_ms",
        "layout_seed", "index_state", "filters",
    } <= set(scene["meta"])
    assert {
        "id", "canonical_id", "label", "type", "member_ids", "repo_ids",
        "repo_names",
        "mass_score", "gravity_mass", "visual_radius", "community_id",
        "anchor_role", "system_anchor_id", "orbit_tier", "orbit_radius",
        "galactic_radius", "galactic_target_radius", "galactic_radius_scale",
        "galactic_initial_compactness",
        "galactic_clearance_adjusted", "galactic_overlap", "galactic_arm",
        "galactic_phase",
        "galactic_eccentricity",
        "scene_rank",
    } <= set(scene["nodes"][0])
    assert {
        "id", "source", "target", "relation", "layer", "directed", "confidence",
        "support_count", "strength", "rest_length", "spring_strength", "tier",
        "visible_by_default", "bundled_edge_count",
    } <= set(scene["edges"][0])
    assert {
        "id", "anchor_id", "mass", "radius", "galactic_radius",
        "galactic_target_radius", "galactic_radius_scale",
        "galactic_initial_compactness",
        "galactic_clearance_adjusted", "galactic_overlap", "galactic_arm",
        "galactic_phase",
        "galactic_eccentricity",
    } <= set(scene["communities"][0])


def test_graph_scene_fixture_encodes_galaxy_invariants():
    scene = _scene()
    nodes = {node["id"]: node for node in scene["nodes"]}
    communities = {community["id"]: community for community in scene["communities"]}
    assert scene["meta"]["algorithm_version"] == "galaxy-v6"
    for node in scene["nodes"]:
        expected_mass = 1.0 + 15.0 * node["mass_score"] ** 2
        assert math.isclose(node["gravity_mass"], expected_mass, abs_tol=1e-6)
        assert math.isclose(
            node["visual_radius"],
            1.5 + 2.0 * node["gravity_mass"] ** (2.0 / 3.0),
            abs_tol=1e-6,
        )
    for community in scene["communities"]:
        expected_mass = sum(
            node["gravity_mass"] for node in scene["nodes"]
            if node["community_id"] == community["id"]
        )
        assert math.isclose(community["mass"], expected_mass, abs_tol=1e-6)
    black_holes = [node for node in scene["nodes"] if node["anchor_role"] == "global"]
    assert len(black_holes) == 1
    assert black_holes[0]["mass_score"] == max(node["mass_score"] for node in scene["nodes"])
    assert (black_holes[0]["x"], black_holes[0]["y"]) == (0, 0)
    core_system = communities[black_holes[0]["community_id"]]
    assert core_system["galactic_radius"] == 0.0
    assert core_system["galactic_arm"] == -1
    assert all(
        community["galactic_radius"] == community["galactic_target_radius"]
        for community in communities.values()
    )
    for node in scene["nodes"]:
        system = communities[node["community_id"]]
        assert node["galactic_radius"] == system["galactic_radius"]
        assert node["galactic_target_radius"] == system["galactic_target_radius"]
        assert node["galactic_radius_scale"] == system["galactic_radius_scale"] == 0.4
        assert (node["galactic_initial_compactness"]
                == system["galactic_initial_compactness"] == 0.8)
        assert (node["galactic_clearance_adjusted"]
                == system["galactic_clearance_adjusted"])
        assert node["galactic_overlap"] == system["galactic_overlap"]
        assert node["galactic_arm"] == system["galactic_arm"]
        assert node["galactic_phase"] == system["galactic_phase"]
        assert node["galactic_eccentricity"] == system["galactic_eccentricity"]

    strengths_and_lengths = sorted(
        (edge["strength"], edge["rest_length"]) for edge in scene["edges"]
    )
    for (weaker, longer), (stronger, shorter) in zip(
        strengths_and_lengths, strengths_and_lengths[1:]
    ):
        assert weaker <= stronger
        assert longer >= shorter

    for edge in scene["edges"]:
        cross_system = (
            nodes[edge["source"]]["community_id"]
            != nodes[edge["target"]]["community_id"]
        )
        if cross_system and scene["meta"]["level"] == "overview":
            assert not edge["visible_by_default"]
            assert edge["tier"] in {"context", "ambient"}

    assert scene["community_bridges"]


def test_all_presentation_projects_only_renderer_fields_without_mutating_scene():
    scene = _scene()
    scene["nodes"][0]["private_evidence"] = [{"memory": "must stay server-side"}]
    scene["edges"][0]["support_ids"] = ["mem_private"]
    projected = project_all_presentation(scene)

    assert projected["meta"]["all_projected"] is True
    assert "private_evidence" not in projected["nodes"][0]
    assert "support_ids" not in projected["edges"][0]
    assert scene["nodes"][0]["private_evidence"]
    assert scene["edges"][0]["support_ids"] == ["mem_private"]
    assert {
        "id", "label", "type", "community_id", "x", "y",
        "anchor_role", "system_anchor_id", "gravity_mass", "visual_radius",
    } <= projected["nodes"][0].keys()
    assert {
        "id", "source", "target", "layer", "strength", "rest_length",
        "spring_strength", "bridge",
    } <= projected["edges"][0].keys()
    assert set(projected) == {"meta", "nodes", "edges"}
    assert "facets" not in projected
    assert "community_bridges" not in projected


def test_all_presentation_allowlist_is_closed_to_unknown_fields():
    """The all-presentation projection must use an explicit allowlist and never
    pass through unknown keys. This guards against accidental leakage of new
    server-only fields added to the analytical scene."""
    from engraphis.core.graph_scene import (
        _ALL_PRESENTATION_EDGE_FIELDS,
        _ALL_PRESENTATION_META_FIELDS,
        _ALL_PRESENTATION_NODE_FIELDS,
    )
    scene = _scene()
    scene["nodes"][0]["unknown_future_field"] = "should be stripped"
    scene["edges"][0]["unknown_future_edge"] = "should be stripped"
    scene["meta"]["unknown_future_meta"] = "should be stripped"
    projected = project_all_presentation(scene)
    allowed_node_keys = set(_ALL_PRESENTATION_NODE_FIELDS)
    allowed_edge_keys = set(_ALL_PRESENTATION_EDGE_FIELDS) | {"bridge"}
    allowed_meta_keys = set(_ALL_PRESENTATION_META_FIELDS) | {"all_projected"}
    assert "unknown_future_field" not in projected["nodes"][0]
    assert "unknown_future_edge" not in projected["edges"][0]
    assert "unknown_future_meta" not in projected["meta"]
    assert set(projected["nodes"][0].keys()) <= allowed_node_keys
    assert set(projected["edges"][0].keys()) <= allowed_edge_keys
    assert set(projected["meta"].keys()) <= allowed_meta_keys