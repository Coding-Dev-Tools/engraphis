"""Shared contract checks for the dashboard and graph-scene service."""
from __future__ import annotations

import json
import math
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
    assert scene["meta"]["algorithm_version"] == "galaxy-v10-even-orbital-spacing"
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
            # Overview retains cross-system bridge edges so galaxy mode can paint
            # inter-system connections. These are promoted to primary tier.
            assert edge["tier"] in {"primary", "backbone", "context", "ambient"}

    assert scene["community_bridges"]

    # --- v10 even-orbital-spacing invariants ---
    global_community_id = core_system["id"]
    non_global_systems = [
        c for c in communities.values()
        if c["id"] != global_community_id
    ]

    # Non-global communities have approximately even angular spacing around the black hole.
    if len(non_global_systems) >= 2:
        angles = sorted(
            math.atan2(
                nodes[c["anchor_id"]]["y"],
                nodes[c["anchor_id"]]["x"],
            )
            for c in non_global_systems
        )
        n = len(angles)
        expected_gap = math.tau / n
        gaps = [
            (angles[(i + 1) % n] - angles[i]) % math.tau
            for i in range(n)
        ]
        for gap in gaps:
            assert abs(gap - expected_gap) < 0.5, (
                f"Angular gap {gap:.3f} deviates from even spacing {expected_gap:.3f}"
            )

    # Every non-global system center is beyond core_outer_extent + minimum gap.
    # The layout guarantees this in de-eccentrified orbital space; the Euclidean
    # distance may be smaller by a factor of eccentricity on the compressed axis.
    core_outer_extent = core_system["radius"]
    galaxy_system_min_gap = 48.0  # matches GALAXY_SYSTEM_MIN_GAP in graph_scene
    for c in non_global_systems:
        anchor = nodes[c["anchor_id"]]
        ecc = c["galactic_eccentricity"]
        orbital_radius = (
            math.hypot(anchor["x"], anchor["y"] / ecc)
            if ecc > 0
            else math.hypot(anchor["x"], anchor["y"])
        )
        assert orbital_radius >= core_outer_extent + galaxy_system_min_gap - 1.0, (
            f"System {c['id']} orbital radius {orbital_radius:.2f} "
            f"inside core extent {core_outer_extent:.2f} + gap {galaxy_system_min_gap}"
        )
