"""Shared contract checks for the dashboard and graph-scene service."""
from __future__ import annotations

import json
from pathlib import Path


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
        "mass_score", "gravity_mass", "visual_radius", "community_id",
        "anchor_role", "scene_rank",
    } <= set(scene["nodes"][0])
    assert {
        "id", "source", "target", "relation", "layer", "directed", "confidence",
        "support_count", "strength", "rest_length", "spring_strength", "tier",
        "visible_by_default", "bundled_edge_count",
    } <= set(scene["edges"][0])


def test_graph_scene_fixture_encodes_galaxy_invariants():
    scene = _scene()
    nodes = {node["id"]: node for node in scene["nodes"]}
    black_holes = [node for node in scene["nodes"] if node["anchor_role"] == "global"]
    assert len(black_holes) == 1
    assert black_holes[0]["mass_score"] == max(node["mass_score"] for node in scene["nodes"])
    assert (black_holes[0]["x"], black_holes[0]["y"]) == (0, 0)

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

