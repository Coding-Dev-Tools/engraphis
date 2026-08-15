"""Deterministic evidence-backed graph scene construction.

This module is deliberately pure: callers provide scoped entity, edge and support
rows, and receive JSON-ready canonical graph scenes. SQLite/FastAPI integration stays
in the service and route layers.
"""
from __future__ import annotations

import hashlib
import heapq
import json
import math
import re
from bisect import bisect_right
from collections import Counter, defaultdict, deque
from typing import Any, Iterable, Mapping, Optional, Sequence


ALGORITHM_VERSION = "galaxy-v8-cross-system-links"
PUBLIC_REFERENCE_ID_LIMIT = 200
PUBLIC_FACET_LIMIT = 100
PUBLIC_REPO_NAME_LIMIT = 100
GOLDEN_ANGLE = math.pi * (3.0 - math.sqrt(5.0))
# v6 begins every live star at 80% of its v5 radial placement.  Community
# centres use the accumulated .4 scale (v5's .5 times this compactness) while
# local orbital bands apply the same .8 factor independently.  That makes each
# emitted coordinate exactly .8 of the corresponding uncontracted seed rather
# than merely making the system anchors appear closer.
GALACTIC_INITIAL_COMPACTNESS = 0.8
GALACTIC_RADIUS_SCALE = 0.5 * GALACTIC_INITIAL_COMPACTNESS
# Keep complete solar-system envelopes just outside one another while avoiding the
# large empty radial bands that made most systems appear beyond the black-hole interior.
# This matches the dashboard's default painted carrier gap (4 units) as a small
# proportional envelope allowance instead of adding a blanket 15% radial tax.
GALAXY_ENVELOPE_CLEARANCE_FACTOR = 1.04
_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in",
    "is", "it", "of", "on", "or", "that", "the", "this", "to", "was", "were",
    "with", "unknown", "untitled", "none", "null",
    # Capitalized sentence fragments produced by the fully-offline regex extractor are
    # not useful entity identities. Keep this deliberately conservative and limited to
    # unambiguous function words, booleans, generic workflow verbs, and directions; it is
    # only applied to ``person_or_concept`` nodes, never code symbols or typed entities.
    "all", "also", "any", "both", "each", "either", "every", "more", "most",
    "other", "same", "several", "some", "such", "than", "then", "there", "here",
    "too", "very", "yes", "no", "true", "false", "one", "two", "three",
    "first", "second", "last", "left", "right", "new", "old", "now",
    "can", "cannot", "could", "did", "do", "does", "doing", "done", "had",
    "has", "have", "having", "may", "might", "must", "shall", "should", "will",
    "would", "run", "running", "fix", "fixed", "create", "created", "review",
    "reviewed", "blocked", "refusing", "investigate", "overall", "subject",
    "reason", "action", "actions", "outcome", "add", "added", "check", "checked",
    "scan", "scanned", "merge", "merged", "comment", "comments", "artifact",
    "artifacts", "manifest", "key", "keys", "per", "local", "test", "tests",
    "verdict", "connection", "connections", "input", "output", "request",
    "response", "result", "results", "status", "detail", "details",
    "active", "author", "because", "commit", "missing", "only", "possible",
    "title", "available", "existing", "expected", "following", "given", "next",
    "previous", "required", "single", "still", "total", "used", "using", "without",
    "approval", "approved", "categories", "degraded", "error", "errors", "failed",
    "passed", "rejected", "skipped", "success", "verify", "warning", "warnings",
    "see", "successful", "prose", "supported", "generated", "matched",
    "enumerated", "reached", "posted", "completed",
}
_HARD_BOILERPLATE_PREFIXES = {
    "if", "generated", "matched", "enumerated", "reached", "posted", "completed",
    "supported",
}
_SEARCH_FRAGMENT_PREFIXES = _HARD_BOILERPLATE_PREFIXES | {
    # Sentence-openers observed in legacy/offline extraction output. These are too
    # broad to erase from an analytical scene ("Full Stack", for example, can be a
    # valid concept), but they should not crowd out a direct identity suggestion.
    "no", "add", "added", "full", "three", "orphan", "ignored", "ignores",
    "compiled", "codex-descended",
}
_BOILERPLATE_SUFFIXES = ("-based", "-side", "-level", "-version")


def _row(row: Mapping[str, Any]) -> dict[str, Any]:
    return dict(row)


def _temporal_fields(row: Mapping[str, Any]) -> dict[str, Any]:
    """Return the stable, public bi-temporal fields carried by a scene row."""
    return {
        key: row.get(key)
        for key in (
            "valid_from", "valid_to", "valid_to_recorded_at",
            "ingested_at", "expired_at",
        )
        if key in row
    }


def _hash_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Return a deterministic hash view of an emitted scene record.

    Layout coordinates are derived from ``scene_hash`` and therefore must not be fed back
    into it. All other fields are part of the public scene identity, including optional
    repository and temporal metadata.
    """
    return {
        str(key): value for key, value in sorted(record.items())
        if key not in {"x", "y"}
    }


def _loads(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError, RecursionError):
        return {}
    return value if isinstance(value, dict) else {}


def _memory_ids(provenance: Any) -> list[str]:
    value = _loads(provenance)
    candidates: list[Any] = [value.get("memory_id")]
    if isinstance(value.get("memory_ids"), list):
        candidates.extend(value["memory_ids"])
    result: list[str] = []
    for candidate in candidates:
        memory_id = str(candidate or "")
        if memory_id and memory_id not in result:
            result.append(memory_id)
    return result


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _finite_float(value: Any, default: float = 0.0) -> float:
    """Coerce an untrusted row value without allowing NaN/Infinity into physics."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def _edge_weight(value: Any) -> float:
    """Return a bounded edge weight, retaining the legacy falsy default."""
    # Existing graph rows use zero as an unspecified value, not a request for a
    # nearly invisible relation. Preserve that contract while rejecting malformed
    # non-finite/string values before physics consumes them.
    if not value:
        return 1.0
    return _clamp(_finite_float(value, 1.0), 0.05, 4.0)


def _quantile(values: Sequence[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _percentile(value: float, ordered: Sequence[float]) -> float:
    if len(ordered) <= 1:
        return 1.0 if ordered else 0.0
    return (bisect_right(ordered, value) - 1) / (len(ordered) - 1)


def _positive_p95(values: Iterable[float]) -> float:
    """Return a robust global scale without letting zero-evidence nodes erase it."""
    positive = sorted(value for value in values if value > 0.0 and math.isfinite(value))
    return _quantile(positive, 0.95)


def _log_p95_signal(value: float, p95: float) -> float:
    """Compress an evidence magnitude while retaining distinctions above its p95.

    A hard p95 clamp makes a common one-support leaf and a hundred-support hub identical
    whenever leaves comprise at least 95% of the graph.  Soft saturation keeps the p95 as
    the global scale but lets the evidence tail continue toward one deterministically.
    """
    if value <= 0.0 or p95 <= 0.0 or not math.isfinite(value) or not math.isfinite(p95):
        return 0.0
    ratio = math.log1p(value) / math.log1p(p95)
    return _clamp(1.0 - math.exp(-ratio))


def _gravity_mass(mass_score: float) -> float:
    """Map evidence score to the one physical mass used throughout Galaxy scenes."""
    score = _clamp(mass_score)
    return 1.0 + 15.0 * score * score


def _visual_radius(gravity_mass: float) -> float:
    """Derive appearance solely from mass with enough contrast to survive fit-to-view.

    A square-root mapping compressed ordinary live scenes to roughly a 2:1 painted range,
    which made evidence-distinct stars read as uniform after the full galaxy was fitted.
    The bounded mass contract (1..16) keeps this two-thirds-power view modest (3.5..14.2px)
    while making the strongest observed stars about three times wider than light ones.
    """
    return 1.5 + 2.0 * max(0.0, gravity_mass) ** (2.0 / 3.0)


def _public_mass_metrics(mass_score: float) -> tuple[float, float, float]:
    """Return self-consistent six-decimal score, mass, and display radius fields."""
    public_score = round(_clamp(mass_score), 6)
    public_mass = round(_gravity_mass(public_score), 6)
    public_radius = round(_visual_radius(public_mass), 6)
    return public_score, public_mass, public_radius


def _ghost_position(layout_seed: int, node_id: str,
                    base_radius: float) -> tuple[float, float]:
    """Place presentation-only history without perturbing the live physics seed."""
    digest = hashlib.sha256(
        f"{ALGORITHM_VERSION}:{layout_seed}:ghost:{node_id}".encode("utf-8")
    ).digest()
    angle = int.from_bytes(digest[:8], "big") / float(1 << 64) * math.tau
    ring = 1.0 + 0.18 * (int.from_bytes(digest[8:10], "big") % 3)
    radius = max(36.0, base_radius) * ring
    return radius * math.cos(angle), radius * math.sin(angle)


def _dominant_member(nodes: Mapping[str, Mapping[str, Any]],
                     member_ids: Iterable[str]) -> str:
    """Return the live evidence-mass core for one community.

    Physical mass is the primary and authoritative ordering. The remaining fields only
    break genuine public-mass ties, keeping the result deterministic without manufacturing
    visual mass for an otherwise ordinary node.
    """
    live_ids = [
        node_id for node_id in member_ids
        if node_id in nodes and not nodes[node_id].get("ghost")
    ]
    if not live_ids:
        return ""
    eligible_ids = [
        node_id for node_id in live_ids
        if _finite_float(nodes[node_id].get("entity_quality"), 1.0) > 0.0
    ]
    pool = eligible_ids or live_ids
    return min(pool, key=lambda node_id: (
        -_finite_float(nodes[node_id].get("gravity_mass"), 0.0),
        -_finite_float(nodes[node_id].get("scene_rank"), 0.0),
        -_finite_float(nodes[node_id].get("weighted_degree"), 0.0),
        node_id,
    ))


def _hierarchy_anchors(
    nodes: Mapping[str, Mapping[str, Any]],
    community_members: Mapping[str, Sequence[str]],
) -> tuple[dict[str, str], str]:
    """Choose explicit hierarchy authority first, then deterministic evidence cores.

    ``anchor_role`` is server-authored authority and survives filtering/reprojection. Labels
    and names are deliberately absent from selection: renamed entities retain identical
    physics. A malformed payload with several explicit candidates is resolved by the same
    mass/structure/id ordering as an unannotated payload.
    """
    anchors: dict[str, str] = {}
    for community_id, member_ids in sorted(community_members.items()):
        explicit = [
            node_id for node_id in member_ids
            if node_id in nodes
            and nodes[node_id].get("anchor_role") in {"global", "community"}
        ]
        anchor_id = _dominant_member(nodes, explicit or member_ids)
        if anchor_id:
            anchors[community_id] = anchor_id
    explicit_global = [
        node_id for node_id, node in nodes.items()
        if not node.get("ghost") and node.get("anchor_role") == "global"
    ]
    global_anchor = _dominant_member(
        nodes, explicit_global or anchors.values()
    )
    return anchors, global_anchor


def _assign_orbit_hierarchy(
    nodes: dict[str, dict[str, Any]],
    community_members: Mapping[str, Sequence[str]],
    community_anchors: Mapping[str, str],
    *,
    radius_scale: Optional[float] = None,
) -> tuple[dict[str, dict[str, int | float]], dict[str, float]]:
    """Assign deterministic, mass-ranked orbital bands without changing node mass.

    Four heavy satellites occupy the inner band, then band capacity doubles up to 32.
    Radii account for the actual evidence-derived node radii before the uniform v6
    compactness factor is applied.  This keeps the rank/band hierarchy stable while
    making every local orbital offset an exact fraction of its uncontracted seed.
    Dense systems may consequently overlap; compactness is deliberate and their
    public system envelope remains derived from the emitted orbit radii.
    """
    slots: dict[str, dict[str, int | float]] = {}
    system_radii: dict[str, float] = {}
    clean_radius_scale = _clamp(
        _finite_float(
            GALACTIC_INITIAL_COMPACTNESS if radius_scale is None else radius_scale,
            GALACTIC_INITIAL_COMPACTNESS,
        ),
        0.05,
        2.0,
    )
    for node in nodes.values():
        node["system_anchor_id"] = ""
        node["orbit_tier"] = -1 if node.get("ghost") else 0
        node["orbit_radius"] = 0.0

    for community_id, member_ids in sorted(community_members.items()):
        anchor_id = community_anchors.get(community_id, "")
        if not anchor_id or anchor_id not in nodes or nodes[anchor_id].get("ghost"):
            continue
        live_ids = [
            node_id for node_id in member_ids
            if node_id in nodes and not nodes[node_id].get("ghost")
        ]
        satellites = sorted(
            (node_id for node_id in live_ids if node_id != anchor_id),
            key=lambda node_id: (
                -_finite_float(nodes[node_id].get("gravity_mass"), 0.0),
                -_finite_float(nodes[node_id].get("scene_rank"), 0.0),
                -_finite_float(nodes[node_id].get("weighted_degree"), 0.0),
                node_id,
            ),
        )
        anchor_radius = max(
            2.0, _finite_float(nodes[anchor_id].get("visual_radius"), 2.0)
        )
        nodes[anchor_id].update({
            "system_anchor_id": anchor_id,
            "orbit_tier": 0,
            "orbit_radius": 0.0,
        })
        slots[anchor_id] = {"tier": 0, "slot": 0, "count": 1, "radius": 0.0}

        previous_outer = anchor_radius
        compact_outer = anchor_radius
        offset = 0
        tier = 1
        while offset < len(satellites):
            first_radius = max(2.0, _finite_float(
                nodes[satellites[offset]].get("visual_radius"), 2.0
            ))
            gap = max(8.0, 0.55 * anchor_radius)
            nominal_radius = previous_outer + first_radius + gap
            if tier <= 3:
                capacity = 4 * (2 ** (tier - 1))
            else:
                angular_footprint = max(8.0, 2.0 * first_radius + 0.5 * gap)
                capacity = max(32, int(math.tau * nominal_radius / angular_footprint))
            ring_ids = satellites[offset:offset + capacity]
            ring_max_radius = max(
                max(2.0, _finite_float(nodes[node_id].get("visual_radius"), 2.0))
                for node_id in ring_ids
            )
            nominal_radius = previous_outer + ring_max_radius + gap
            compact_radius = nominal_radius * clean_radius_scale
            for slot, node_id in enumerate(ring_ids):
                nodes[node_id].update({
                    "system_anchor_id": anchor_id,
                    "orbit_tier": tier,
                    "orbit_radius": round(compact_radius, 6),
                })
                slots[node_id] = {
                    "tier": tier,
                    "slot": slot,
                    "count": len(ring_ids),
                    "radius": compact_radius,
                }
            previous_outer = nominal_radius + ring_max_radius
            compact_outer = max(compact_outer, compact_radius + ring_max_radius)
            offset += len(ring_ids)
            tier += 1
        system_radii[community_id] = round(
            _clamp(compact_outer + 6.0, 36.0, 10_000.0), 6
        )
    return slots, system_radii


def _orbit_position(
    center_x: float,
    center_y: float,
    community_id: str,
    slot: Mapping[str, int | float],
    layout_seed: int,
) -> tuple[float, float]:
    """Place one satellite on its deterministic, slightly elliptical orbital band."""
    tier = int(slot["tier"])
    if tier <= 0:
        return center_x, center_y
    count = max(1, int(slot["count"]))
    ordinal = int(slot["slot"])
    digest = hashlib.sha256(
        f"{ALGORITHM_VERSION}:{layout_seed}:{community_id}:{tier}".encode("utf-8")
    ).digest()
    phase = int.from_bytes(digest[:8], "big") / float(1 << 64) * math.tau
    direction = -1.0 if digest[8] & 1 else 1.0
    eccentricity = 0.88 + (digest[9] / 255.0) * 0.08
    rotation = digest[10] / 255.0 * math.tau
    angle = phase + direction * math.tau * ordinal / count
    radius = float(slot["radius"])
    local_x = radius * math.cos(angle)
    local_y = radius * eccentricity * math.sin(angle)
    cos_rotation, sin_rotation = math.cos(rotation), math.sin(rotation)
    return (
        center_x + local_x * cos_rotation - local_y * sin_rotation,
        center_y + local_x * sin_rotation + local_y * cos_rotation,
    )


def _community_positions(
    communities: Sequence[Mapping[str, Any]],
    global_community_id: str,
    layout_seed: int,
    *,
    spacing: float,
    radius_scale: Optional[float] = None,
) -> tuple[
    dict[str, tuple[float, float]],
    dict[str, dict[str, int | float | bool]],
]:
    """Seed deterministic logarithmic arms, then pack complete system envelopes.

    ``radius_scale`` controls the preferred spiral target, not a post-layout geometric
    contraction.  Contracting already-packed centres was visually compact but invalidated the
    very system radii used by the collision test: large communities consequently began life
    intersecting the black-hole system or one another.  The final pass starts from the scaled
    targets and moves whole systems outward/along the arm until their painted envelopes clear.
    """
    ordered = sorted(communities, key=lambda item: (
        0 if str(item["id"]) == global_community_id else 1,
        -_finite_float(item.get("mass"), 0.0),
        str(item["id"]),
    ))
    clean_radius_scale = _clamp(
        _finite_float(
            GALACTIC_RADIUS_SCALE if radius_scale is None else radius_scale,
            GALACTIC_RADIUS_SCALE,
        ),
        0.05,
        2.0,
    )
    morphology = hashlib.sha256(
        f"{ALGORITHM_VERSION}:{layout_seed}:galaxy-morphology".encode("utf-8")
    ).digest()
    arm_count = 2 + (morphology[0] & 1)
    arm_offset = morphology[1] % arm_count
    direction = -1.0 if morphology[2] & 1 else 1.0
    disk_eccentricity = 0.84 + (morphology[3] / 255.0) * 0.08
    base_phase = int.from_bytes(morphology[4:12], "big") / float(1 << 64) * math.tau
    arm_populations = [0 for _ in range(arm_count)]
    specs: list[dict[str, int | float | str]] = []
    orbital_rank = 0
    for community in ordered:
        community_id = str(community["id"])
        system_radius = _clamp(
            _finite_float(community.get("radius"), 36.0), 36.0, 10_000.0
        )
        if community_id == global_community_id:
            specs.append({
                "id": community_id, "system_radius": system_radius,
                "arm": -1, "nominal_x": 0.0, "nominal_y": 0.0,
            })
            continue
        orbital_rank += 1
        arm = (orbital_rank - 1 + arm_offset) % arm_count
        arm_rank = arm_populations[arm]
        arm_populations[arm] += 1
        digest = hashlib.sha256(
            f"{ALGORITHM_VERSION}:{layout_seed}:system:{community_id}".encode("utf-8")
        ).digest()
        angular_jitter = (
            int.from_bytes(digest[:4], "big") / float(1 << 32) - 0.5
        ) * 0.34
        radial_jitter = 0.91 + (
            int.from_bytes(digest[4:8], "big") / float(1 << 32)
        ) * 0.18
        # r = a * exp(b * theta) is logarithmic. Parameterising theta with log(rank)
        # keeps very large scenes finite while retaining visible arm winding.
        spiral_phase = 3.10 * math.log1p(arm_rank)
        arm_phase = base_phase + math.tau * arm / arm_count
        angle = arm_phase + direction * spiral_phase + angular_jitter
        baseline_radius = (
            spacing * 1.10 * math.exp(0.175 * spiral_phase) * radial_jitter
        )
        specs.append({
            "id": community_id,
            "system_radius": system_radius,
            "arm": arm,
            "nominal_x": baseline_radius * math.cos(angle),
            "nominal_y": disk_eccentricity * baseline_radius * math.sin(angle),
        })

    def pack_with_radial_clearance(
        targets: Mapping[str, tuple[float, float]],
    ) -> tuple[dict[str, tuple[float, float]], set[str]]:
        positions: dict[str, tuple[float, float]] = {}
        # Radius-aware cells keep a pathological 10,000-unit community from scanning tens of
        # thousands of empty 98-unit buckets on every attempt.
        cell_size = max(36.0, spacing, max(
            (float(spec["system_radius"]) for spec in specs), default=36.0
        ))
        spatial_cells: dict[tuple[int, int], list[tuple[float, float, float]]] = (
            defaultdict(list)
        )
        unresolved: set[str] = set()
        maximum_placed_radius = 0.0

        def place(x: float, y: float, system_radius: float) -> None:
            nonlocal maximum_placed_radius
            cell = (math.floor(x / cell_size), math.floor(y / cell_size))
            spatial_cells[cell].append((x, y, system_radius))
            maximum_placed_radius = max(maximum_placed_radius, system_radius)

        def collides(x: float, y: float, system_radius: float) -> bool:
            reach = GALAXY_ENVELOPE_CLEARANCE_FACTOR * (
                system_radius + maximum_placed_radius
            )
            cell_x, cell_y = math.floor(x / cell_size), math.floor(y / cell_size)
            cell_reach = max(1, math.ceil(reach / cell_size))
            for grid_x in range(cell_x - cell_reach, cell_x + cell_reach + 1):
                for grid_y in range(cell_y - cell_reach, cell_y + cell_reach + 1):
                    for other_x, other_y, other_radius in spatial_cells.get(
                        (grid_x, grid_y), ()
                    ):
                        clearance = GALAXY_ENVELOPE_CLEARANCE_FACTOR * (
                            system_radius + other_radius
                        )
                        if math.hypot(x - other_x, y - other_y) < clearance:
                            return True
            return False

        for spec in specs:
            community_id = str(spec["id"])
            system_radius = float(spec["system_radius"])
            target_x, target_y = targets[community_id]
            if community_id == global_community_id:
                x, y = 0.0, 0.0
            else:
                axis_radius = math.hypot(target_x, target_y / disk_eccentricity)
                angle = math.atan2(target_y / disk_eccentricity, target_x)
                # Moving only the system centre preserves every local star/planet offset.  The
                # logarithmic walk is deterministic and gives dense 500+ node scenes enough
                # radial headroom without a quadratic all-node relaxation.
                found = False
                for attempt in range(256):
                    trial_angle = angle + direction * 0.045 * attempt
                    trial_radius = axis_radius * math.exp(0.018 * attempt)
                    x = trial_radius * math.cos(trial_angle)
                    y = disk_eccentricity * trial_radius * math.sin(trial_angle)
                    if not collides(x, y, system_radius):
                        found = True
                        break
                if not found:
                    unresolved.add(community_id)
            positions[community_id] = (x, y)
            place(x, y, system_radius)
        return positions, unresolved


    nominal_targets = {
        str(spec["id"]): (float(spec["nominal_x"]), float(spec["nominal_y"]))
        for spec in specs
    }
    preferred_targets = {
        community_id: (
            nominal_x * clean_radius_scale,
            nominal_y * clean_radius_scale,
        )
        for community_id, (nominal_x, nominal_y) in nominal_targets.items()
    }
    # Pack *after* applying compactness.  This is the key invariant: compactness may choose a
    # close preferred orbit, but it may never contract two complete solar-system envelopes
    # through each other.  The older fixed-radius angular search could only flag an impossible
    # ring; this radial continuation always has a collision-free solution in open space.
    positions, unresolved = pack_with_radial_clearance(preferred_targets)
    placement_flags = {
        community_id: {
            "adjusted": math.hypot(
                positions[community_id][0] - preferred_x,
                positions[community_id][1] - preferred_y,
            ) > 1e-9,
            "overlap": community_id in unresolved,
        }
        for community_id, (preferred_x, preferred_y) in preferred_targets.items()
    }
    hints: dict[str, dict[str, int | float | bool]] = {}
    for spec in specs:
        community_id = str(spec["id"])
        x, y = positions[community_id]
        target_x, target_y = preferred_targets[community_id]
        actual_radius = math.hypot(x, y)
        preferred_radius = math.hypot(target_x, target_y)
        hints[community_id] = {
            "galactic_radius": round(actual_radius, 6),
            # Convergence follows this target every live slice.  It must therefore be the
            # clearance-adjusted carrier orbit, or it continually drags the freshly packed
            # system back through its neighbours.  Preserve the compact spiral preference as
            # a diagnostic only; it is never a physical attractor after packing.
            "galactic_target_radius": round(actual_radius, 6),
            "galactic_preferred_radius": round(preferred_radius, 6),
            "galactic_radius_scale": round(clean_radius_scale, 6),
            "galactic_initial_compactness": GALACTIC_INITIAL_COMPACTNESS,
            "galactic_clearance_adjusted": placement_flags[community_id]["adjusted"],
            "galactic_overlap": placement_flags[community_id]["overlap"],
            "galactic_arm": int(spec["arm"]),
            "galactic_phase": round(math.atan2(y, x), 6),
            "galactic_eccentricity": round(disk_eccentricity, 6),
        }
    return positions, hints


def is_obvious_entity_noise(label: str, entity_type: str) -> bool:
    """Conservatively flag extractor fragments without deleting graph identity rows."""
    if entity_type not in {"concept", "person_or_concept"}:
        return False
    normalized = " ".join(label.casefold().split())
    tokens = re.findall(r"[a-z0-9]+", normalized)
    if len(normalized) < 2 or not tokens:
        return True
    if normalized in _STOPWORDS or all(token in _STOPWORDS for token in tokens):
        return True
    if len(tokens) > 1 and (
        tokens[0] in _HARD_BOILERPLATE_PREFIXES
        or any(normalized.startswith(f"{prefix} ") or normalized.startswith(f"{prefix}-")
               for prefix in _HARD_BOILERPLATE_PREFIXES if "-" in prefix)
    ):
        return True
    dashed = re.sub(r"\s*[\N{EN DASH}\N{EM DASH}_/]\s*", "-", normalized)
    return dashed.endswith(_BOILERPLATE_SUFFIXES)


def is_broad_search_fragment(label: str, entity_type: str) -> bool:
    """Demote likely sentence fragments without removing them from graph scenes."""
    if is_obvious_entity_noise(label, entity_type):
        return True
    if entity_type not in {"concept", "person_or_concept"}:
        return False
    normalized = " ".join(label.casefold().split())
    tokens = re.findall(r"[a-z0-9]+", normalized)
    return len(tokens) > 1 and (
        tokens[0] in _SEARCH_FRAGMENT_PREFIXES
        or any(normalized.startswith(f"{prefix} ") or normalized.startswith(f"{prefix}-")
               for prefix in _SEARCH_FRAGMENT_PREFIXES if "-" in prefix)
    )


def _combined_confidence(values: Iterable[float]) -> float:
    complement = 1.0
    seen = False
    for value in values:
        seen = True
        safe_value = _finite_float(value, 0.50)
        complement *= 1.0 - _clamp(safe_value, 0.05, 0.99)
    return 1.0 - complement if seen else 0.50


def _relation_factor(layer: str, relation: str) -> float:
    if relation == "co_occurs":
        return 0.25
    if layer in {"entity", "causal"}:
        return 1.0
    if layer == "temporal":
        return 0.90
    return 0.80


def _source_default(relation: str, provenance: Any) -> tuple[str, float]:
    if relation == "co_occurs":
        return "co_occurrence", 0.25
    raw = str(_loads(provenance).get("source") or "").casefold()
    if "manual" in raw or "schema" in raw:
        return "manual", 1.0
    if "structured" in raw:
        return "structured", 0.80
    if "regex" in raw or "backfill" in raw:
        return "regex_proximity", 0.55
    return "legacy_unknown", 0.50


def _stable_id(prefix: str, *parts: Any) -> str:
    payload = "\x1f".join(str(part) for part in parts).encode("utf-8")
    return prefix + hashlib.sha256(payload).hexdigest()[:16]


def _components(node_ids: Sequence[str], edges: Sequence[dict]) -> dict[str, str]:
    adjacent: dict[str, set[str]] = {node_id: set() for node_id in node_ids}
    for edge in edges:
        adjacent.setdefault(edge["source"], set()).add(edge["target"])
        adjacent.setdefault(edge["target"], set()).add(edge["source"])
    result: dict[str, str] = {}
    components: list[list[str]] = []
    for start in sorted(adjacent):
        if start in result:
            continue
        members: list[str] = []
        queue = deque([start])
        result[start] = ""
        while queue:
            current = queue.popleft()
            members.append(current)
            for neighbor in sorted(adjacent[current]):
                if neighbor not in result:
                    result[neighbor] = ""
                    queue.append(neighbor)
        components.append(members)
    components.sort(key=lambda members: (-len(members), min(members)))
    for index, members in enumerate(components):
        for member in members:
            result[member] = f"component_{index}"
    return result


def _louvain(node_ids: Sequence[str], edges: Sequence[dict]) -> dict[str, str]:
    """Deterministic first-level weighted Louvain local moving.

    Sorted traversal and canonical tie-breaking make identical inputs produce
    identical communities without relying on process-randomized hash order.
    """
    adjacency: dict[str, dict[str, float]] = {node_id: {} for node_id in node_ids}
    for edge in edges:
        source, target = edge["source"], edge["target"]
        weight = max(float(edge.get("strength") or 0.0), 0.0001)
        adjacency[source][target] = adjacency[source].get(target, 0.0) + weight
        adjacency[target][source] = adjacency[target].get(source, 0.0) + weight
    degree = {node_id: sum(adjacency[node_id].values()) for node_id in node_ids}
    total = sum(degree.values())
    community = {node_id: node_id for node_id in node_ids}
    totals = dict(degree)
    if total <= 0.0:
        return {node_id: _stable_id("community_", node_id) for node_id in node_ids}
    for _ in range(24):
        moved = False
        for node_id in sorted(node_ids):
            current = community[node_id]
            node_degree = degree[node_id]
            weights: dict[str, float] = defaultdict(float)
            for neighbor, weight in adjacency[node_id].items():
                weights[community[neighbor]] += weight
            totals[current] -= node_degree
            best = current
            best_gain = 0.0
            for candidate in sorted(weights):
                gain = weights[candidate] - (totals.get(candidate, 0.0) * node_degree / total)
                if gain > best_gain + 1e-12:
                    best, best_gain = candidate, gain
            community[node_id] = best
            totals[best] = totals.get(best, 0.0) + node_degree
            if best != current:
                moved = True
        if not moved:
            break
    grouped: dict[str, list[str]] = defaultdict(list)
    for node_id, raw_id in community.items():
        grouped[raw_id].append(node_id)
    stable = {
        raw_id: _stable_id("community_", *sorted(members))
        for raw_id, members in grouped.items()
    }
    return {node_id: stable[raw_id] for node_id, raw_id in community.items()}


def build_canonical_graph(
    entity_rows: Sequence[Mapping[str, Any]],
    edge_rows: Sequence[Mapping[str, Any]],
    support_rows: Sequence[Mapping[str, Any]],
    *,
    include_weak_cooccurrence: bool = False,
    layers: Optional[set[str]] = None,
    relations: Optional[set[str]] = None,
    min_support: int = 1,
    min_confidence: float = 0.0,
) -> dict[str, Any]:
    """Canonicalize and score the complete filtered graph before scene caps."""
    members: dict[str, list[dict]] = defaultdict(list)
    member_to_canonical: dict[str, str] = {}
    for raw in entity_rows:
        entity = _row(raw)
        canonical_id = str(entity.get("canonical_id") or entity.get("id") or "")
        entity_id = str(entity.get("id") or "")
        if not entity_id or not canonical_id:
            continue
        members[canonical_id].append(entity)
        member_to_canonical[entity_id] = canonical_id

    nodes: dict[str, dict] = {}
    for canonical_id, group in sorted(members.items()):
        labels = Counter(str(item.get("name") or canonical_id) for item in group)
        label = sorted(labels, key=lambda item: (-labels[item], item.casefold(), item))[0]
        types = Counter(str(item.get("etype") or "person_or_concept") for item in group)
        entity_type = sorted(types, key=lambda item: (-types[item], item))[0]
        repo_ids = sorted({str(item["repo_id"]) for item in group if item.get("repo_id")})
        repo_names = sorted({
            str(item["repo_name"]) for item in group if item.get("repo_name")
        }, key=lambda value: (value.casefold(), value))[:PUBLIC_REPO_NAME_LIMIT]
        node_is_ghost = bool(group) and all(bool(item.get("ghost")) for item in group)
        nodes[canonical_id] = {
            "id": canonical_id,
            "canonical_id": canonical_id,
            "label": label,
            "type": entity_type,
            "member_ids": sorted(str(item["id"]) for item in group),
            "member_count": len(group),
            "repo_ids": repo_ids,
            "repo_names": repo_names,
            "aliases": sorted(labels, key=lambda item: (item.casefold(), item)),
            # A canonical node remains live when any alias is live.  This preserves
            # historical-only code symbols without replacing a live canonical node.
            **({"ghost": True} if node_is_ghost else {}),
        }

    supports_by_edge: dict[str, list[dict]] = defaultdict(list)
    for raw in support_rows:
        support = _row(raw)
        supports_by_edge[str(support.get("edge_id") or "")].append(support)

    bundled: dict[tuple[str, str, str, str, bool], dict] = {}
    for raw in edge_rows:
        edge = _row(raw)
        if edge.get("ghost"):
            continue
        source = member_to_canonical.get(str(edge.get("src") or ""))
        target = member_to_canonical.get(str(edge.get("dst") or ""))
        relation = str(edge.get("relation") or "related")
        layer = str(edge.get("layer") or "semantic")
        if not source or not target or source == target:
            continue
        if layers is not None and layer not in layers:
            continue
        if relations is not None and relation not in relations:
            continue
        directed = relation not in {"co_occurs", "related", "associated_with"}
        if not directed and target < source:
            source, target = target, source
        edge_id = str(edge.get("id") or _stable_id("edge_", source, target, relation, layer))
        evidence = [dict(item) for item in supports_by_edge.get(edge_id, [])]
        if not evidence and not edge.get("_has_normalized_support"):
            source_kind, default_confidence = _source_default(relation, edge.get("provenance"))
            memory_ids = _memory_ids(edge.get("provenance"))
            evidence = [{
                "edge_id": edge_id,
                "memory_id": memory_id,
                "source_kind": source_kind,
                "confidence": default_confidence,
                "provenance": edge.get("provenance") or "{}",
            } for memory_id in memory_ids]
            if not evidence:
                evidence = [{
                    "edge_id": edge_id,
                    "memory_id": "",
                    "source_kind": "legacy_unknown",
                    "confidence": 0.50,
                    "provenance": edge.get("provenance") or "{}",
                }]
        memory_ids = {str(item.get("memory_id") or "") for item in evidence}
        memory_ids.discard("")
        key = (source, target, relation, layer, directed)
        item = bundled.get(key)
        if item is None:
            item = {
                "id": edge_id,
                "source": source,
                "target": target,
                "relation": relation,
                "layer": layer,
                "directed": directed,
                "weight": _edge_weight(edge.get("weight")),
                "_confidence_by_support": {},
                "_support_ids": set(),
                "_support_rows": [],
                "_memory_types": set(),
                "_support_times": [],
                "underlying_edge_ids": [],
            }
            bundled[key] = item
        item["weight"] = max(item["weight"], _edge_weight(edge.get("weight")))
        for index, row in enumerate(evidence):
            memory_id = str(row.get("memory_id") or "")
            support_key = memory_id or f"anonymous:{edge_id}:{index}"
            support_confidence = _finite_float(
                row.get("confidence") if row.get("confidence") is not None else 0.50,
                0.50,
            )
            item["_confidence_by_support"][support_key] = max(
                support_confidence,
                item["_confidence_by_support"].get(support_key, 0.0),
            )
        item["_support_ids"].update(memory_ids)
        item["_support_rows"].extend(evidence)
        item["_memory_types"].update(
            str(row.get("memory_type") or "") for row in evidence
            if row.get("memory_type")
        )
        for row in evidence:
            raw_support_time = row.get("support_time")
            if raw_support_time is None:
                continue
            support_time = _finite_float(raw_support_time, float("nan"))
            if math.isfinite(support_time):
                item["_support_times"].append(support_time)
        item["underlying_edge_ids"].append(edge_id)

    edges = []
    raw_logs: list[float] = []
    for key in sorted(bundled):
        item = bundled[key]
        all_underlying_ids = sorted(set(item["underlying_edge_ids"]))
        item["_underlying_edge_ids_all"] = set(all_underlying_ids)
        item["underlying_edge_ids"] = all_underlying_ids[:PUBLIC_REFERENCE_ID_LIMIT]
        item["underlying_edge_ids_truncated"] = (
            len(all_underlying_ids) > PUBLIC_REFERENCE_ID_LIMIT
        )
        if len(all_underlying_ids) > 1:
            item["id"] = _stable_id("bundle_", *all_underlying_ids)
        item["bundled_edge_count"] = len(all_underlying_ids)
        # The confidence map is keyed by stable memory id or a per-row anonymous key,
        # so it counts identified and legacy anonymous evidence without double-counting
        # duplicate rows for the same memory.
        item["support_count"] = len(item["_confidence_by_support"])
        all_support_ids = set(item["_support_ids"])
        item["_support_ids_all"] = all_support_ids
        item["support_memory_ids"] = sorted(all_support_ids)[:PUBLIC_REFERENCE_ID_LIMIT]
        item["support_ids_truncated"] = len(all_support_ids) > PUBLIC_REFERENCE_ID_LIMIT
        item["confidence"] = _combined_confidence(
            item["_confidence_by_support"].values()
        )
        # Filters apply to the canonical display relation after parallel member-level
        # rows have been bundled. Applying them above would discard two independent
        # one-support alias edges that together form a supported canonical relation.
        if (item["support_count"] < max(0, int(min_support))
                or item["confidence"] < min_confidence):
            continue
        if (item["relation"] == "co_occurs" and item["support_count"] <= 1
                and not include_weak_cooccurrence):
            continue
        item["memory_types"] = sorted(item["_memory_types"])
        item["support_time_min"] = (
            min(item["_support_times"]) if item["_support_times"] else None
        )
        item["support_time_max"] = (
            max(item["_support_times"]) if item["_support_times"] else None
        )
        support_boost = 1.0 + min(math.log2(1.0 + item["support_count"]) / 4.0, 0.75)
        raw_strength = (
            max(0.05, min(4.0, item["weight"]))
            * item["confidence"]
            * support_boost
            * _relation_factor(item["layer"], item["relation"])
        )
        item["_raw_log"] = math.log1p(raw_strength)
        raw_logs.append(item["_raw_log"])
        edges.append(item)
    low, high = _quantile(raw_logs, 0.05), _quantile(raw_logs, 0.95)
    for edge in edges:
        edge["strength"] = (
            1.0 if high - low <= 1e-12
            else _clamp((edge["_raw_log"] - low) / (high - low))
        )

    degree = {node_id: 0.0 for node_id in nodes}
    node_supports: dict[str, set[str]] = {node_id: set() for node_id in nodes}
    adjacency: dict[str, dict[str, float]] = {node_id: {} for node_id in nodes}
    for edge in edges:
        source, target = edge["source"], edge["target"]
        strength = edge["strength"]
        degree[source] += strength
        degree[target] += strength
        # Stable memory ids deduplicate evidence reused across relations. Anonymous legacy
        # rows use their deterministic edge/index key, so their magnitude still contributes
        # without exposing a synthetic id in the public support-memory list.
        node_supports[source].update(edge["_confidence_by_support"])
        node_supports[target].update(edge["_confidence_by_support"])
        adjacency[source][target] = adjacency[source].get(target, 0.0) + strength
        adjacency[target][source] = adjacency[target].get(source, 0.0) + strength

    pagerank = {node_id: 1.0 / max(1, len(nodes)) for node_id in nodes}
    damping = 0.85
    for _ in range(32):
        base = (1.0 - damping) / max(1, len(nodes))
        updated = {node_id: base for node_id in nodes}
        dangling = sum(pagerank[node_id] for node_id in nodes if degree[node_id] <= 0.0)
        spread = damping * dangling / max(1, len(nodes))
        for node_id in updated:
            updated[node_id] += spread
        for source in sorted(nodes):
            if degree[source] <= 0.0:
                continue
            for target, weight in sorted(adjacency[source].items()):
                updated[target] += damping * pagerank[source] * weight / degree[source]
        pagerank = updated

    # These scales are computed over the complete canonical graph, before any overview cap.
    # Unlike empirical ranks, log magnitudes retain the difference between one piece of
    # evidence and a hundred while p95 scaling prevents one pathological hub from flattening
    # every ordinary node.  PageRank is evidence only for connected bodies: its uniform
    # dangling-node base must not give isolates gravitational mass.
    pagerank_evidence = {
        node_id: pagerank[node_id] if degree[node_id] > 0.0 else 0.0
        for node_id in nodes
    }
    degree_p95 = _positive_p95(degree.values())
    pagerank_p95 = _positive_p95(pagerank_evidence.values())
    support_p95 = _positive_p95(
        float(len(value)) for value in node_supports.values()
    )
    repo_p95 = _positive_p95(
        float(len(node["repo_ids"])) for node in nodes.values()
    )
    max_pagerank = max(pagerank.values(), default=1.0) or 1.0
    for node_id, node in nodes.items():
        obvious_noise = is_obvious_entity_noise(node["label"], node["type"])
        quality = 0.0 if obvious_noise else 1.0
        support_count = len(node_supports[node_id])
        mass_score = quality * (
            0.45 * _log_p95_signal(degree[node_id], degree_p95)
            + 0.30 * _log_p95_signal(pagerank_evidence[node_id], pagerank_p95)
            + 0.15 * _log_p95_signal(float(support_count), support_p95)
            + 0.10 * _log_p95_signal(float(len(node["repo_ids"])), repo_p95)
        )
        public_score, gravity_mass, visual_radius = _public_mass_metrics(mass_score)
        node.update({
            "weighted_degree": round(degree[node_id], 6),
            "pagerank": round(pagerank[node_id] / max_pagerank, 6),
            "support_count": support_count,
            "entity_quality": quality,
            "mass_score": public_score,
            "gravity_mass": gravity_mass,
            "visual_radius": visual_radius,
            "anchor_eligible": bool(quality),
        })
        if node.get("ghost"):
            node.update({
                "weighted_degree": 0.0,
                "pagerank": 0.0,
                "support_count": 0,
                "entity_quality": 0.0,
                "mass_score": 0.0,
                "gravity_mass": 0.0,
                "visual_radius": 0.0,
                "anchor_eligible": False,
            })

    components = _components(sorted(nodes), edges)
    communities = _louvain(sorted(nodes), edges)
    community_members: dict[str, list[str]] = defaultdict(list)
    for node_id in sorted(nodes):
        community_members[communities[node_id]].append(node_id)
    community_anchors, global_id = _hierarchy_anchors(nodes, community_members)

    direct_core: dict[str, float] = defaultdict(float)
    for edge in edges:
        if edge["source"] == global_id:
            direct_core[edge["target"]] = max(direct_core[edge["target"]], edge["strength"])
        if edge["target"] == global_id:
            direct_core[edge["source"]] = max(direct_core[edge["source"]], edge["strength"])
    for node_id, node in nodes.items():
        community_id = communities[node_id]
        role = "global" if node_id == global_id else (
            "community" if community_anchors.get(community_id) == node_id else "none"
        )
        affinity = 1.0 if node_id == global_id else _clamp(
            0.65 * node["mass_score"] + 0.35 * direct_core[node_id]
        )
        node.update({
            "component_id": components[node_id],
            "community_id": community_id,
            "anchor_role": role,
            "core_affinity": round(affinity, 6),
            "scene_rank": round(_clamp(0.75 * node["mass_score"] + 0.25 * affinity), 6),
        })
    _assign_orbit_hierarchy(nodes, community_members, community_anchors)

    for edge in edges:
        source_radius = nodes[edge["source"]]["visual_radius"]
        target_radius = nodes[edge["target"]]["visual_radius"]
        edge["rest_length"] = round(_clamp(
            12.0 + 14.0 * (1.0 - edge["strength"])
            + 0.8 * (source_radius + target_radius), 14.0, 34.0
        ), 6)
        edge["spring_strength"] = round(0.035 + 0.17 * edge["strength"], 6)
        edge["tier"] = "context"
        edge["visible_by_default"] = True
        edge.pop("_raw_log", None)
        edge.pop("_confidence_by_support", None)
        edge.pop("_support_ids", None)
        edge.pop("_support_rows", None)
        edge.pop("_memory_types", None)
        edge.pop("_support_times", None)

    return {
        "nodes": nodes,
        "edges": sorted(edges, key=lambda edge: (
            -edge["strength"], edge["source"], edge["target"], edge["relation"], edge["id"]
        )),
        "member_to_canonical": member_to_canonical,
        "community_members": dict(community_members),
        "community_anchors": community_anchors,
        "global_anchor": global_id,
    }


class _UnionFind:
    def __init__(self, values: Iterable[str]) -> None:
        self.parent = {value: value for value in values}

    def find(self, value: str) -> str:
        while self.parent[value] != value:
            self.parent[value] = self.parent[self.parent[value]]
            value = self.parent[value]
        return value

    def union(self, left: str, right: str) -> bool:
        a, b = self.find(left), self.find(right)
        if a == b:
            return False
        if b < a:
            a, b = b, a
        self.parent[b] = a
        return True


def _selected_edges(graph: dict, selected: set[str], level: str, cap: int) -> list[dict]:
    candidates = [edge for edge in graph["edges"]
                  if edge["source"] in selected and edge["target"] in selected]
    bridge_ids: set[str] = set()
    if level == "overview":
        internal = [edge for edge in candidates if
                    graph["nodes"][edge["source"]]["community_id"]
                    == graph["nodes"][edge["target"]]["community_id"]]
        internal_ids = {edge["id"] for edge in internal}
        cross_system = [edge for edge in candidates if edge["id"] not in internal_ids]
        # Overview used to discard every cross-community edge. Galaxy mode still got the
        # aggregate bridge metadata, but had no real endpoints to paint, so black-hole and
        # inter-system relationships appeared disconnected. Keep the strongest connector for
        # every visible system pair, plus every direct global-anchor link; the regular per-node
        # ranking below can add a few more when the edge budget permits.
        pair_best: dict[tuple[str, str, str], dict] = {}
        for edge in sorted(cross_system, key=lambda item: (-item["strength"], item["id"])):
            source = graph["nodes"][edge["source"]]
            target = graph["nodes"][edge["target"]]
            communities = tuple(sorted((source["community_id"], target["community_id"])))
            key = (*communities, edge["layer"])
            pair_best.setdefault(key, edge)
        bridge_edges = list(pair_best.values())
        global_anchor = graph.get("global_anchor")
        if global_anchor in selected:
            bridge_edges.extend(
                edge for edge in cross_system
                if edge["source"] == global_anchor or edge["target"] == global_anchor
            )
        bridge_ids = {edge["id"] for edge in bridge_edges}
        for edge in bridge_edges:
            if edge["tier"] == "context":
                edge["tier"] = "primary"
        candidates = internal + cross_system
    retained: set[str] = set()
    for community_id, member_ids in graph["community_members"].items():
        members = selected.intersection(member_ids)
        forest = _UnionFind(members)
        internal = [edge for edge in candidates if edge["source"] in members
                    and edge["target"] in members]
        for edge in sorted(internal, key=lambda item: (-item["strength"], item["id"])):
            if forest.union(edge["source"], edge["target"]):
                retained.add(edge["id"])
                edge["tier"] = "backbone"
    per_node = 4 if level in {"neighborhood", "path"} else 2
    incident: dict[str, list[dict]] = defaultdict(list)
    for edge in candidates:
        incident[edge["source"]].append(edge)
        incident[edge["target"]].append(edge)
        if edge["layer"] in {"causal", "temporal"}:
            retained.add(edge["id"])
            if edge["tier"] != "backbone":
                edge["tier"] = "primary"
    for node_id in sorted(selected):
        ranked = sorted(incident[node_id], key=lambda item: (-item["strength"], item["id"]))
        for edge in ranked[:per_node]:
            retained.add(edge["id"])
            if edge["tier"] == "context":
                edge["tier"] = "primary"
    if level == "overview":
        retained.update(bridge_ids)
    chosen = [
        {key: value for key, value in edge.items() if not key.startswith("_")}
        for edge in candidates if edge["id"] in retained
    ]
    chosen.sort(key=lambda edge: (
        {"backbone": 0, "primary": 1, "context": 2}.get(edge["tier"], 3),
        -edge["strength"], edge["id"],
    ))
    return chosen[:cap]


def _community_summaries(graph: dict, community_ids: set[str],
                         selected: set[str]) -> list[dict]:
    edges = graph["edges"]
    result = []
    for community_id in community_ids:
        member_ids = graph["community_members"][community_id]
        internal = [edge for edge in edges if edge["source"] in member_ids
                    and edge["target"] in member_ids]
        external = [edge for edge in edges if
                    (edge["source"] in member_ids) != (edge["target"] in member_ids)]
        active_member_ids = [
            node_id for node_id in member_ids
            if not graph["nodes"][node_id].get("ghost")
        ]
        if not active_member_ids:
            continue
        anchor_id = graph["community_anchors"][community_id]
        mass = _community_mass(graph, active_member_ids)
        hierarchy_radius = max((
            _finite_float(graph["nodes"][node_id].get("orbit_radius"), 0.0)
            + max(0.0, _finite_float(
                graph["nodes"][node_id].get("visual_radius"), 0.0
            ))
            for node_id in active_member_ids
        ), default=0.0) + 6.0
        representatives = sorted(active_member_ids, key=lambda node_id: (
            -graph["nodes"][node_id]["scene_rank"], node_id
        ))[:8]
        result.append({
            "id": community_id,
            "label": f"{graph['nodes'][anchor_id]['label']} System",
            "anchor_id": anchor_id,
            "mass": round(mass, 6),
            "radius": round(_clamp(max(
                hierarchy_radius,
                30.0 + 5.0 * math.sqrt(len(active_member_ids)),
            ), 36.0, 10_000.0), 6),
            "member_count": len(active_member_ids),
            "shown_member_count": len(set(active_member_ids).intersection(selected)),
            "internal_strength": round(sum(edge["strength"] for edge in internal), 6),
            "external_strength": round(sum(edge["strength"] for edge in external), 6),
            "representative_ids": representatives,
        })
    return sorted(result, key=lambda item: (-item["mass"], item["id"]))


def _community_mass(graph: dict, member_ids: Iterable[str]) -> float:
    """Return the same aggregate mass used by the system-layout contract."""
    return sum(
        max(0.0, float(graph["nodes"][node_id]["gravity_mass"]))
        for node_id in member_ids if not graph["nodes"][node_id].get("ghost")
    )


def _bridge_physics_strength(value: float, ordered: Sequence[float]) -> float:
    """Robustly normalize aggregate bridge evidence without flattening the tails.

    The p05/p95 component keeps one extreme bridge from compressing the useful range.
    A small empirical-percentile component preserves deterministic distinctions among
    values outside those robust bounds, where a plain clamp would make them identical.
    """
    if not ordered:
        return 0.0
    if len(ordered) == 1 or ordered[-1] - ordered[0] <= 1e-12:
        return 1.0
    low, high = _quantile(ordered, 0.05), _quantile(ordered, 0.95)
    if high - low <= 1e-12:
        robust = _percentile(value, ordered)
    else:
        robust = _clamp((value - low) / (high - low))
    rank = _percentile(value, ordered)
    return _clamp(0.90 * robust + 0.10 * rank)


def _bridges(graph: dict, community_ids: set[str], cap: int) -> list[dict]:
    grouped: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for edge in graph["edges"]:
        source = graph["nodes"][edge["source"]]["community_id"]
        target = graph["nodes"][edge["target"]]["community_id"]
        if source == target or source not in community_ids or target not in community_ids:
            continue
        if target < source:
            source, target = target, source
        grouped[(source, target, edge["layer"])].append(edge)
    result = []
    for (source, target, layer), edges in grouped.items():
        all_edge_ids = sorted(edge["id"] for edge in edges)
        relations = Counter()
        for edge in edges:
            relations[edge["relation"]] += max(1, int(edge["bundled_edge_count"]))
        support_ids = {
            memory_id for edge in edges for memory_id in edge["_support_ids_all"]
        }
        anonymous_support_count = sum(
            max(0, int(edge["support_count"]) - len(edge["_support_ids_all"]))
            for edge in edges
        )
        support_count = len(support_ids) + anonymous_support_count
        edge_count = sum(max(1, int(edge["bundled_edge_count"])) for edge in edges)
        aggregate_strength = sum(max(0.0, float(edge["strength"])) for edge in edges)
        # Strength carries most of the signal; unique evidence and relation cardinality
        # add bounded corroboration without allowing raw counts to dominate the layout.
        physics_raw = (
            0.60 * math.log1p(aggregate_strength)
            + 0.25 * math.log1p(support_count)
            + 0.15 * math.log1p(edge_count)
        )
        result.append({
            "id": _stable_id("bridge_", source, target, layer),
            "source_community": source,
            "target_community": target,
            "layer": layer,
            # Keep the original display field compatible for one contract version.
            "strength": round(_clamp(aggregate_strength), 6),
            "aggregate_strength": round(aggregate_strength, 6),
            "support_count": support_count,
            "edge_count": edge_count,
            "top_relations": sorted(relations, key=lambda relation: (
                -relations[relation], relation
            ))[:5],
            "edge_ids": all_edge_ids[:PUBLIC_REFERENCE_ID_LIMIT],
            "edge_ids_truncated": len(all_edge_ids) > PUBLIC_REFERENCE_ID_LIMIT,
            "_physics_raw": physics_raw,
        })
    # Rank before the cap with unsaturated aggregate evidence. Otherwise every bridge
    # whose summed display strength exceeds one ties and the cap becomes ID-driven.
    result.sort(key=lambda bridge: (-bridge["_physics_raw"], bridge["id"]))
    retained = result[:max(0, cap)]
    ordered = sorted(bridge["_physics_raw"] for bridge in retained)
    for bridge in retained:
        bridge["physics_strength"] = round(
            _bridge_physics_strength(bridge["_physics_raw"], ordered), 6
        )
        bridge.pop("_physics_raw", None)
    retained.sort(key=lambda bridge: (
        -bridge["physics_strength"], -bridge["aggregate_strength"], bridge["id"]
    ))
    return retained


def _facets(graph: dict) -> dict[str, list[dict]]:
    types = Counter(node["type"] for node in graph["nodes"].values())
    repos = Counter(repo for node in graph["nodes"].values() for repo in node["repo_ids"])
    layers = Counter(edge["layer"] for edge in graph["edges"])
    relations = Counter(edge["relation"] for edge in graph["edges"])
    memory_types = Counter(
        memory_type for edge in graph["edges"]
        for memory_type in edge.get("memory_types", [])
    )
    support = Counter(
        "1" if edge["support_count"] <= 1 else
        "2-3" if edge["support_count"] <= 3 else
        "4-7" if edge["support_count"] <= 7 else "8+"
        for edge in graph["edges"]
    )
    confidence = Counter(
        "0-49%" if edge["confidence"] < 0.5 else
        "50-74%" if edge["confidence"] < 0.75 else
        "75-89%" if edge["confidence"] < 0.9 else "90-100%"
        for edge in graph["edges"]
    )
    support_times = [
        float(value) for edge in graph["edges"]
        for value in (edge.get("support_time_min"), edge.get("support_time_max"))
        if value is not None
    ]

    def items(counter: Counter) -> list[dict]:
        return [{"value": value, "count": count} for value, count in sorted(
            counter.items(), key=lambda item: (-item[1], item[0])
        )[:PUBLIC_FACET_LIMIT]]

    return {
        "entity_types": items(types),
        "memory_types": items(memory_types),
        "layers": items(layers),
        "relations": items(relations),
        "repos": items(repos),
        "support": items(support),
        "confidence": items(confidence),
        "time": ([{
            "value": "range",
            "count": len(support_times),
            "from": min(support_times),
            "to": max(support_times),
        }] if support_times else []),
    }


def _complete_relations(
    graph: dict[str, Any],
    edge_rows: Sequence[Mapping[str, Any]],
    support_rows: Sequence[Mapping[str, Any]],
    *,
    memory_ids: set[str],
    include_weak_cooccurrence: bool,
    layers: Optional[set[str]],
    relations: Optional[set[str]],
    min_support: int,
    min_confidence: float,
    memory_ghost_ids: Optional[set[str]] = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return every filtered physical relation and its explicit evidence links.

    Normal analytical scenes intentionally bundle parallel canonical relations.  A
    complete scene has the opposite contract: the physical edge id is the public id,
    and each supporting memory is connected to both relation endpoints.  The latter
    makes evidence selectable without replacing or hiding the factual relation.
    """
    supports_by_edge: dict[str, list[dict[str, Any]]] = defaultdict(list)
    memory_ghost_ids = memory_ghost_ids or set()
    for raw in support_rows:
        support = _row(raw)
        supports_by_edge[str(support.get("edge_id") or "")].append(support)

    pending: list[dict[str, Any]] = []
    evidence_pending: list[dict[str, Any]] = []
    raw_logs: list[float] = []
    for raw in sorted(edge_rows, key=lambda item: str(item.get("id") or "")):
        edge = _row(raw)
        source = graph["member_to_canonical"].get(str(edge.get("src") or ""))
        target = graph["member_to_canonical"].get(str(edge.get("dst") or ""))
        if not source or not target:
            continue
        relation = str(edge.get("relation") or "related")
        layer = str(edge.get("layer") or "semantic")
        if layers is not None and layer not in layers:
            continue
        if relations is not None and relation not in relations:
            continue
        edge_id = str(edge.get("id") or _stable_id(
            "edge_", source, target, relation, layer
        ))
        ghost = bool(edge.get("ghost"))
        evidence = [dict(item) for item in supports_by_edge.get(edge_id, [])]
        if not evidence and not edge.get("_has_normalized_support"):
            source_kind, default_confidence = _source_default(
                relation, edge.get("provenance")
            )
            evidence = [{
                "edge_id": edge_id,
                "memory_id": memory_id,
                "source_kind": source_kind,
                "confidence": default_confidence,
                "provenance": edge.get("provenance") or "{}",
            } for memory_id in _memory_ids(edge.get("provenance"))]
            if not evidence:
                evidence = [{
                    "edge_id": edge_id,
                    "memory_id": "",
                    "source_kind": "legacy_unknown",
                    "confidence": 0.50,
                    "provenance": edge.get("provenance") or "{}",
                }]

        confidence_by_support: dict[str, float] = {}
        support_memory_ids: set[str] = set()
        for index, support in enumerate(evidence):
            memory_id = str(support.get("memory_id") or "")
            support_key = memory_id or f"anonymous:{edge_id}:{index}"
            confidence_by_support[support_key] = max(
                _finite_float(
                    support.get("confidence")
                    if support.get("confidence") is not None else 0.50,
                    0.50,
                ),
                confidence_by_support.get(support_key, 0.0),
            )
            if memory_id:
                support_memory_ids.add(memory_id)
        support_count = len(confidence_by_support)
        confidence = _combined_confidence(confidence_by_support.values())
        if support_count < max(0, int(min_support)) or confidence < min_confidence:
            continue
        if (relation == "co_occurs" and support_count <= 1
                and not include_weak_cooccurrence):
            continue

        weight = _edge_weight(edge.get("weight"))
        support_boost = 1.0 + min(math.log2(1.0 + support_count) / 4.0, 0.75)
        raw_log = math.log1p(
            weight * confidence * support_boost * _relation_factor(layer, relation)
        )
        if not ghost:
            raw_logs.append(raw_log)
        pending.append({
            "id": edge_id,
            "source": source,
            "target": target,
            "relation": relation,
            "layer": layer,
            "directed": relation not in {"co_occurs", "related", "associated_with"},
            "weight": weight,
            "confidence": round(confidence, 6),
            "support_count": support_count,
            "support_memory_ids": sorted(support_memory_ids),
            "underlying_edge_ids": [edge_id],
            "bundled_edge_count": 1,
            "tier": "raw",
            "visible_by_default": True,
            "connector_kind": "entity_relation",
            "ghost": ghost,
            **_temporal_fields(edge),
            "_raw_log": raw_log,
        })
        for support in evidence:
            memory_id = str(support.get("memory_id") or "")
            if not memory_id or memory_id not in memory_ids:
                continue
            source_kind = str(support.get("source_kind") or "legacy_unknown")
            evidence_ghost = bool(
                ghost
                or support.get("ghost")
                or support.get("memory_ghost")
                or memory_id in memory_ghost_ids
            )
            evidence_confidence = _clamp(
                _finite_float(
                    support.get("confidence")
                    if support.get("confidence") is not None else 0.50,
                    0.50,
                ),
                0.05,
                0.99,
            )
            for endpoint in sorted({source, target}):
                evidence_pending.append({
                    "id": _stable_id(
                        "evidence_", edge_id, memory_id, source_kind, endpoint
                    ),
                    "source": memory_id,
                    "target": endpoint,
                    "relation": "supports",
                    "layer": "evidence",
                    "directed": True,
                    "weight": evidence_confidence,
                    "confidence": round(evidence_confidence, 6),
                    "support_count": 1,
                    "support_memory_ids": [memory_id],
                    "underlying_edge_ids": [edge_id],
                    "bundled_edge_count": 1,
                    "tier": "evidence",
                    "visible_by_default": True,
                    "connector_kind": "evidence",
                    "ghost": evidence_ghost,
                    **_temporal_fields(support),
                    "source_kind": source_kind,
                    "strength": round(evidence_confidence, 6),
                    "rest_length": round(12.0 + 10.0 * (1.0 - evidence_confidence), 6),
                    "spring_strength": round(0.04 + 0.12 * evidence_confidence, 6),
                })

    low, high = _quantile(raw_logs, 0.05), _quantile(raw_logs, 0.95)
    relations_out = []
    for edge in pending:
        if edge["ghost"]:
            edge["strength"] = 0.0
            edge["rest_length"] = 0.0
            edge["spring_strength"] = 0.0
            edge["visible_by_default"] = False
            edge.pop("_raw_log", None)
            relations_out.append(edge)
            continue
        strength = (
            1.0 if high - low <= 1e-12
            else _clamp((edge["_raw_log"] - low) / (high - low))
        )
        source_radius = graph["nodes"][edge["source"]]["visual_radius"]
        target_radius = graph["nodes"][edge["target"]]["visual_radius"]
        edge["strength"] = round(strength, 6)
        edge["rest_length"] = round(_clamp(
            12.0 + 14.0 * (1.0 - strength)
            + 0.8 * (source_radius + target_radius), 14.0, 34.0
        ), 6)
        edge["spring_strength"] = round(0.035 + 0.17 * strength, 6)
        edge.pop("_raw_log", None)
        relations_out.append(edge)
    for edge in evidence_pending:
        if edge["ghost"]:
            edge["strength"] = 0.0
            edge["rest_length"] = 0.0
            edge["spring_strength"] = 0.0
            edge["visible_by_default"] = False
    return (
        sorted(relations_out, key=lambda item: (
            -item["strength"], item["source"], item["target"],
            item["relation"], item["id"],
        )),
        sorted(evidence_pending, key=lambda item: item["id"]),
    )


def _complete_bridges(nodes: Mapping[str, dict], edges: Sequence[dict]) -> list[dict]:
    """Aggregate every cross-system connector for system-level live gravity.

    These quotient-graph bridges are additive physics metadata; the complete scene
    still returns every raw connector in ``edges``.
    """
    grouped: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for edge in edges:
        if edge.get("ghost"):
            continue
        source_node = nodes.get(str(edge.get("source") or ""))
        target_node = nodes.get(str(edge.get("target") or ""))
        if not source_node or not target_node:
            continue
        source = source_node["community_id"]
        target = target_node["community_id"]
        if source == target:
            continue
        if target < source:
            source, target = target, source
        grouped[(source, target, str(edge.get("layer") or "semantic"))].append(edge)
    pending = []
    for (source, target, layer), grouped_edges in sorted(grouped.items()):
        strength = sum(max(0.0, float(edge.get("strength") or 0.0))
                       for edge in grouped_edges)
        support_ids = {
            memory_id for edge in grouped_edges
            for memory_id in edge.get("support_memory_ids", [])
        }
        relations = Counter(str(edge.get("relation") or "related")
                            for edge in grouped_edges)
        raw = (
            0.60 * math.log1p(strength)
            + 0.25 * math.log1p(len(support_ids))
            + 0.15 * math.log1p(len(grouped_edges))
        )
        pending.append({
            "id": _stable_id("bridge_", source, target, layer),
            "source_community": source,
            "target_community": target,
            "layer": layer,
            "strength": round(_clamp(strength), 6),
            "aggregate_strength": round(strength, 6),
            "support_count": len(support_ids),
            "edge_count": len(grouped_edges),
            "top_relations": sorted(relations, key=lambda relation: (
                -relations[relation], relation
            ))[:5],
            "edge_ids": sorted(str(edge["id"]) for edge in grouped_edges),
            "edge_ids_truncated": False,
            "_physics_raw": raw,
        })
    ordered = sorted(bridge["_physics_raw"] for bridge in pending)
    for bridge in pending:
        bridge["physics_strength"] = round(
            _bridge_physics_strength(bridge["_physics_raw"], ordered), 6
        )
        bridge.pop("_physics_raw", None)
    return sorted(pending, key=lambda bridge: (
        -bridge["physics_strength"], -bridge["aggregate_strength"], bridge["id"]
    ))


def _build_complete_scene(
    workspace: str,
    graph: dict[str, Any],
    edge_rows: Sequence[Mapping[str, Any]],
    support_rows: Sequence[Mapping[str, Any]],
    memory_rows: Sequence[Mapping[str, Any]],
    memory_link_rows: Sequence[Mapping[str, Any]],
    code_memory_link_rows: Sequence[Mapping[str, Any]],
    *,
    include_weak_cooccurrence: bool,
    layers: Optional[set[str]],
    relations: Optional[set[str]],
    min_support: int,
    min_confidence: float,
    connected_only: bool,
    include_history: bool,
    include_memory_nodes: bool,
    filters: dict[str, Any],
    index_generation: int,
) -> dict[str, Any]:
    memory_rows_by_id = {
        str(row.get("id") or ""): _row(row) for row in memory_rows if row.get("id")
    } if include_memory_nodes else {}
    memory_ids = set(memory_rows_by_id)
    raw_relations, evidence_edges = _complete_relations(
        graph, edge_rows, support_rows, memory_ids=memory_ids,
        include_weak_cooccurrence=include_weak_cooccurrence,
        layers=layers, relations=relations, min_support=min_support,
        min_confidence=min_confidence,
        memory_ghost_ids={
            memory_id for memory_id, memory in memory_rows_by_id.items()
            if memory.get("ghost")
        },
    )

    entity_nodes = {node_id: dict(node) for node_id, node in graph["nodes"].items()}
    for node in entity_nodes.values():
        node["node_kind"] = "entity"
        node.pop("aliases", None)
        node.pop("anchor_eligible", None)

    evidence_targets: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for edge in evidence_edges:
        if edge.get("ghost"):
            continue
        evidence_targets[edge["source"]].append((
            float(edge["strength"]), edge["target"]
        ))

    memory_community: dict[str, str] = {}
    for memory_id in sorted(memory_ids):
        candidates = evidence_targets.get(memory_id, [])
        if candidates:
            target = min(candidates, key=lambda item: (-item[0], item[1]))[1]
            memory_community[memory_id] = entity_nodes[target]["community_id"]
    for memory_id, memory in sorted(memory_rows_by_id.items()):
        if memory_id not in memory_community:
            memory_community[memory_id] = _stable_id(
                "community_memory_", memory.get("repo_id") or "workspace",
                memory.get("mtype") or "semantic",
            )

    memory_degree = Counter()
    for edge in evidence_edges:
        if not edge.get("ghost"):
            memory_degree[edge["source"]] += 1
    memory_link_edges = []
    for raw in sorted(memory_link_rows, key=lambda item: (
        str(item.get("a") or ""), str(item.get("b") or ""),
        _finite_float(item.get("created_at"), 0.0),
    )):
        row = _row(raw)
        source, target = str(row.get("a") or ""), str(row.get("b") or "")
        if source not in memory_ids or target not in memory_ids:
            continue
        relation = str(row.get("relation") or "related")
        layer = str(row.get("layer") or "semantic")
        if layers is not None and layer not in layers:
            continue
        if relations is not None and relation not in relations:
            continue
        ghost = bool(row.get("ghost") or
            memory_rows_by_id[source].get("ghost")
            or memory_rows_by_id[target].get("ghost")
        )
        if not ghost:
            memory_degree[source] += 1
            memory_degree[target] += 1
        memory_link_edges.append({
            "id": _stable_id(
                "memlink_", source, target, relation, layer,
                row.get("reason") or "", row.get("created_at") or 0.0,
            ),
            "source": source,
            "target": target,
            "relation": relation,
            "layer": layer,
            "directed": False,
            "weight": 1.0,
            "confidence": 1.0,
            "support_count": 1,
            "support_memory_ids": sorted({source, target}),
            "underlying_edge_ids": [],
            "bundled_edge_count": 1,
            "tier": "raw",
            "visible_by_default": True,
            "connector_kind": "memory_link",
            "ghost": ghost,
            **_temporal_fields(row),
            "reason": str(row.get("reason") or ""),
            "strength": 0.0 if ghost else 0.72,
            "rest_length": 0.0 if ghost else 22.0,
            "spring_strength": 0.0 if ghost else 0.12,
        })

    code_memory_edges = []
    for raw in sorted(code_memory_link_rows, key=lambda item: str(item.get("id") or "")):
        row = _row(raw)
        memory_id = str(row.get("memory_id") or "")
        symbol_id = f"code:{row.get('symbol_id')}"
        if memory_id not in memory_ids or symbol_id not in entity_nodes:
            continue
        relation = str(row.get("relation") or "mentions")
        if layers is not None and "entity" not in layers:
            continue
        if relations is not None and relation not in relations:
            continue
        _raw_conf = row.get("confidence")
        confidence = _clamp(
            _finite_float(_raw_conf if _raw_conf is not None else 1.0, 1.0),
            0.05,
            1.0,
        )
        ghost = bool(
            row.get("ghost")
            or memory_rows_by_id[memory_id].get("ghost")
            or entity_nodes.get(symbol_id, {}).get("ghost")
        )
        if not ghost:
            memory_degree[memory_id] += 1
        code_memory_edges.append({
            "id": str(row.get("id") or _stable_id(
                "code_memory_", memory_id, symbol_id, relation
            )),
            "source": memory_id,
            "target": symbol_id,
            "relation": relation,
            "layer": "entity",
            "directed": True,
            "weight": confidence,
            "confidence": round(confidence, 6),
            "support_count": 1,
            "support_memory_ids": [memory_id],
            "underlying_edge_ids": [],
            "bundled_edge_count": 1,
            "tier": "raw",
            "visible_by_default": True,
            "connector_kind": "code_memory",
            "ghost": ghost,
            **_temporal_fields(row),
            "strength": 0.0 if ghost else round(confidence, 6),
            "rest_length": (0.0 if ghost else
                            round(14.0 + 8.0 * (1.0 - confidence), 6)),
            "spring_strength": (0.0 if ghost else
                                round(0.05 + 0.12 * confidence, 6)),
        })

    memory_nodes: dict[str, dict[str, Any]] = {}
    degree_p95 = _positive_p95(
        float(memory_degree[memory_id]) for memory_id in memory_ids
    )
    for memory_id, memory in sorted(memory_rows_by_id.items()):
        title = str(memory.get("title") or "").strip()
        summary = str(memory.get("summary") or "").strip()
        content = str(memory.get("content") or "").strip()
        label = title or summary or content or memory_id
        label = " ".join(label.split())[:160]
        importance = _clamp(_finite_float(memory.get("importance"), 0.0))
        degree_signal = _log_p95_signal(
            float(memory_degree[memory_id]), degree_p95
        )
        mass_score = _clamp(
            0.08 + 0.34 * importance + 0.18 * degree_signal, 0.08, 0.60
        )
        public_score, gravity_mass, visual_radius = _public_mass_metrics(mass_score)
        memory_nodes[memory_id] = {
            "id": memory_id,
            "canonical_id": memory_id,
            "label": label,
            "type": str(memory.get("mtype") or "semantic"),
            "node_kind": "memory",
            "memory_type": str(memory.get("mtype") or "semantic"),
            "scope": str(memory.get("scope") or "workspace"),
            "member_ids": [memory_id],
            "member_count": 1,
            "repo_ids": [str(memory["repo_id"])] if memory.get("repo_id") else [],
            "repo_names": ([str(memory["repo_name"])]
                           if memory.get("repo_name") else []),
            "weighted_degree": round(float(memory_degree[memory_id]), 6),
            "pagerank": 0.0,
            "support_count": int(memory_degree[memory_id]),
            "entity_quality": 1.0,
            "mass_score": public_score,
            "gravity_mass": gravity_mass,
            "visual_radius": visual_radius,
            "component_id": f"component_memory_{memory_id}",
            "community_id": memory_community[memory_id],
            "anchor_role": "none",
            "core_affinity": 0.0,
            "scene_rank": round(_clamp(0.70 * mass_score + 0.30 * degree_signal), 6),
            "importance": round(importance, 6),
            "pinned": bool(memory.get("pinned")),
            "valid_from": memory.get("valid_from"),
            "ingested_at": memory.get("ingested_at"),
            "valid_to": memory.get("valid_to"),
            "valid_to_recorded_at": memory.get("valid_to_recorded_at"),
            "expired_at": memory.get("expired_at"),
            "ghost": bool(memory.get("ghost")),
        }

    # Historical nodes are presentation context only. They retain their deterministic
    # community/position identity, but never contribute gravitational mass.
    for node in memory_nodes.values():
        if node.get("ghost"):
            node["mass_score"] = 0.0
            node["gravity_mass"] = 0.0
            node["weighted_degree"] = 0.0
            node["pagerank"] = 0.0
            node["support_count"] = 0
            node["scene_rank"] = 0.0
            node["visual_radius"] = 0.0

    all_nodes: dict[str, dict[str, Any]] = {**entity_nodes, **memory_nodes}
    community_members: dict[str, list[str]] = defaultdict(list)
    for node_id, node in all_nodes.items():
        community_members[node["community_id"]].append(node_id)
    community_anchors, global_anchor = _hierarchy_anchors(
        all_nodes, community_members
    )
    for node in all_nodes.values():
        node["anchor_role"] = "none"
    for anchor_id in community_anchors.values():
        all_nodes[anchor_id]["anchor_role"] = "community"
    if global_anchor:
        all_nodes[global_anchor]["anchor_role"] = "global"
    orbit_slots, system_radii = _assign_orbit_hierarchy(
        all_nodes, community_members, community_anchors
    )

    complete_edges = sorted(
        [*raw_relations, *evidence_edges, *memory_link_edges, *code_memory_edges],
        key=lambda edge: (
            edge["connector_kind"], -float(edge["strength"]), edge["id"]
        ),
    )
    if connected_only:
        connected_ids = {
            str(edge[endpoint])
            for edge in complete_edges
            if not edge.get("ghost")
            for endpoint in ("source", "target")
        }
        if include_history:
            connected_ids |= {
                str(edge[endpoint])
                for edge in complete_edges
                if edge.get("ghost")
                for endpoint in ("source", "target")
            }
        all_nodes = {
            node_id: node for node_id, node in all_nodes.items()
            if node_id in connected_ids
        }
        entity_nodes = {
            node_id: node for node_id, node in entity_nodes.items()
            if node_id in all_nodes
        }
        memory_nodes = {
            node_id: node for node_id, node in memory_nodes.items()
            if node_id in all_nodes
        }
        complete_edges = [
            edge for edge in complete_edges
            if edge["source"] in all_nodes and edge["target"] in all_nodes
        ]
        community_members = defaultdict(list)
        for node_id, node in all_nodes.items():
            community_members[node["community_id"]].append(node_id)
        community_anchors, global_anchor = _hierarchy_anchors(
            all_nodes, community_members
        )
        for node in all_nodes.values():
            node["anchor_role"] = "none"
        for anchor_id in community_anchors.values():
            all_nodes[anchor_id]["anchor_role"] = "community"
        if global_anchor:
            all_nodes[global_anchor]["anchor_role"] = "global"
        orbit_slots, system_radii = _assign_orbit_hierarchy(
            all_nodes, community_members, community_anchors
        )
    internal_strength: dict[str, float] = defaultdict(float)
    external_strength: dict[str, float] = defaultdict(float)
    for edge in complete_edges:
        if edge.get("ghost"):
            continue
        if (all_nodes[edge["source"]].get("ghost")
                or all_nodes[edge["target"]].get("ghost")):
            continue
        source_community = all_nodes[edge["source"]]["community_id"]
        target_community = all_nodes[edge["target"]]["community_id"]
        strength = float(edge["strength"])
        if source_community == target_community:
            internal_strength[source_community] += strength
        else:
            external_strength[source_community] += strength
            external_strength[target_community] += strength
    communities = []
    for community_id, member_ids in sorted(community_members.items()):
        active_member_ids = [
            node_id for node_id in member_ids if not all_nodes[node_id].get("ghost")
        ]
        if not active_member_ids:
            continue
        anchor_id = community_anchors[community_id]
        mass = sum(max(0.0, float(all_nodes[node_id]["gravity_mass"]))
                   for node_id in active_member_ids)
        communities.append({
            "id": community_id,
            "label": f"{all_nodes[anchor_id]['label']} System",
            "anchor_id": anchor_id,
            "mass": round(mass, 6),
            "radius": system_radii[community_id],
            "member_count": len(active_member_ids),
            "shown_member_count": len(active_member_ids),
            "internal_strength": round(internal_strength[community_id], 6),
            "external_strength": round(external_strength[community_id], 6),
            "representative_ids": sorted(active_member_ids, key=lambda node_id: (
                -all_nodes[node_id]["scene_rank"], node_id
            ))[:8],
        })
    communities.sort(key=lambda item: (-item["mass"], item["id"]))
    bridges = _complete_bridges(all_nodes, complete_edges)

    hash_payload = {
        "algorithm": ALGORITHM_VERSION,
        "index_generation": index_generation,
        "workspace": workspace,
        "filters": filters,
        "nodes": [
            (node_id, _hash_record(all_nodes[node_id]))
            for node_id in sorted(all_nodes)
        ],
        "edges": [
            _hash_record(edge)
            for edge in sorted(complete_edges, key=lambda item: item["id"])
        ],
        "communities": [
            (
                community["id"], community["anchor_id"], community["mass"],
                community["radius"], community["member_count"],
                community["shown_member_count"],
            )
            for community in sorted(communities, key=lambda item: item["id"])
        ],
        "bridges": [
            (
                bridge["id"], bridge["aggregate_strength"],
                bridge["physics_strength"], bridge["support_count"],
                bridge["edge_count"],
            )
            for bridge in sorted(bridges, key=lambda item: item["id"])
        ],
    }
    scene_hash = hashlib.sha256(json.dumps(
        hash_payload, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    layout_filters = dict(filters)
    layout_filters.pop("include_history", None)
    layout_hash_payload = {
        **hash_payload,
        "filters": layout_filters,
        "nodes": [
            (node_id, _hash_record(all_nodes[node_id]))
            for node_id in sorted(all_nodes) if not all_nodes[node_id].get("ghost")
        ],
        "edges": [
            _hash_record(edge)
            for edge in sorted(complete_edges, key=lambda item: item["id"])
            if not edge.get("ghost")
        ],
    }
    layout_hash = hashlib.sha256(json.dumps(
        layout_hash_payload, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    layout_seed = int(layout_hash[:8], 16)

    global_community_id = (
        str(all_nodes[global_anchor]["community_id"]) if global_anchor else ""
    )
    positions, community_hints = _community_positions(
        communities, global_community_id, layout_seed, spacing=92.0
    )
    for community in communities:
        community.update(community_hints[community["id"]])
    scene_nodes = []
    for node_id in sorted(all_nodes, key=lambda value: (
        -all_nodes[value]["scene_rank"], value
    )):
        node = dict(all_nodes[node_id])
        community_id = node["community_id"]
        if node.get("ghost") or community_id not in positions:
            x, y = _ghost_position(
                layout_seed, node_id, 82.0 * math.sqrt(len(communities) + 1)
            )
        elif node_id == community_anchors[community_id]:
            x, y = positions[community_id]
        else:
            center_x, center_y = positions[community_id]
            x, y = _orbit_position(
                center_x, center_y, community_id,
                orbit_slots[node_id], layout_seed,
            )
        node["x"], node["y"] = round(x, 6), round(y, 6)
        if community_id in community_hints:
            node.update(community_hints[community_id])
        scene_nodes.append(node)

    facets = _facets(graph)
    memory_type_counts = Counter(node["memory_type"] for node in memory_nodes.values())
    facets["memory_types"] = [{"value": value, "count": count}
                               for value, count in sorted(
        memory_type_counts.items(), key=lambda item: (-item[1], item[0])
    )[:PUBLIC_FACET_LIMIT]]
    return {
        "meta": {
            "workspace": workspace,
            "level": "complete",
            "complete_scene": True,
            "node_projection": "all" if include_memory_nodes else "entities",
            "connected_only": connected_only,
            "include_history": include_history,
            "include_memory_nodes": include_memory_nodes,
            "scene_hash": scene_hash,
            "index_generation": index_generation,
            "total_nodes": len(scene_nodes),
            "total_edges": len(complete_edges),
            "shown_nodes": len(scene_nodes),
            "shown_edges": len(complete_edges),
            "entity_nodes": len(entity_nodes),
            "memory_nodes": len(memory_nodes),
            "raw_relations": len(raw_relations),
            "evidence_connectors": len(evidence_edges),
            "memory_connectors": len(memory_link_edges),
            "code_memory_connectors": len(code_memory_edges),
            "truncated": False,
            "degraded": False,
            "safety_state": "full",
            "query_ms": 0.0,
            "layout_seed": layout_seed,
            "index_state": "ready",
            "filters": filters,
            "algorithm_version": ALGORITHM_VERSION,
        },
        "nodes": scene_nodes,
        "edges": complete_edges,
        "communities": communities,
        "community_bridges": bridges,
        "facets": facets,
    }


def build_graph_scene(
    workspace: str,
    entity_rows: Sequence[Mapping[str, Any]],
    edge_rows: Sequence[Mapping[str, Any]],
    support_rows: Sequence[Mapping[str, Any]],
    *,
    memory_rows: Sequence[Mapping[str, Any]] = (),
    memory_link_rows: Sequence[Mapping[str, Any]] = (),
    code_memory_link_rows: Sequence[Mapping[str, Any]] = (),
    level: str = "overview",
    center_id: Optional[str] = None,
    system_id: Optional[str] = None,
    seeds: Optional[Sequence[str]] = None,
    depth: int = 1,
    node_limit: Optional[int] = None,
    edge_limit: Optional[int] = None,
    include_weak_cooccurrence: bool = False,
    layers: Optional[set[str]] = None,
    relations: Optional[set[str]] = None,
    min_support: int = 1,
    min_confidence: float = 0.0,
    connected_only: bool = False,
    include_history: bool = False,
    include_memory_nodes: bool = True,
    filters: Optional[dict] = None,
    index_generation: int = 4,
) -> dict[str, Any]:
    level = level if level in {
        "overview", "system", "neighborhood", "path", "complete"
    } else "overview"
    ghost_member_ids = {
        str(edge.get(endpoint) or "")
        for edge in edge_rows if edge.get("ghost")
        for endpoint in ("src", "dst")
    }
    active_member_ids = {
        str(edge.get(endpoint) or "")
        for edge in edge_rows if not edge.get("ghost")
        for endpoint in ("src", "dst")
    }
    historical_only_members = ghost_member_ids - active_member_ids
    live_entity_rows = [
        row for row in entity_rows
        if str(row.get("id") or "") not in historical_only_members
    ]
    graph = build_canonical_graph(
        live_entity_rows, edge_rows, support_rows,
        include_weak_cooccurrence=include_weak_cooccurrence,
        layers=layers, relations=relations,
        min_support=min_support, min_confidence=min_confidence,
    )
    if include_history and historical_only_members:
        historical_graph = build_canonical_graph(
            [row for row in entity_rows
             if str(row.get("id") or "") in historical_only_members],
            [], [], min_support=0,
        )
        historical_id_map: dict[str, str] = {}
        for node_id, node in historical_graph["nodes"].items():
            historical_id = node_id
            live = graph["nodes"].get(node_id)
            if live is not None:
                # The canonical ID already holds a live evidence node.
                # Record the historical-only alias under a distinct key so
                # the live node keeps its mass, community, and relations.
                node_id = f"{node_id}:ghost"
                while node_id in graph["nodes"] or node_id in historical_id_map.values():
                    node_id = f"{node_id}:ghost"
            historical_id_map[historical_id] = node_id
            node["id"] = node_id
            node["ghost"] = True
            node["mass_score"] = 0.0
            node["gravity_mass"] = 0.0
            node["weighted_degree"] = 0.0
            node["pagerank"] = 0.0
            node["support_count"] = 0
            node["core_affinity"] = 0.0
            node["scene_rank"] = 0.0
            node["entity_quality"] = 0.0
            node["visual_radius"] = 0.0
            node["anchor_eligible"] = False
            node["system_anchor_id"] = ""
            node["orbit_tier"] = -1
            node["orbit_radius"] = 0.0
            touching = [
                edge for edge in edge_rows if edge.get("ghost") and (
                    str(edge.get("src") or "") in node["member_ids"]
                    or str(edge.get("dst") or "") in node["member_ids"]
                )
            ]
            for field in (
                "valid_from", "valid_to", "valid_to_recorded_at",
                "ingested_at", "expired_at",
            ):
                values: list[float] = [
                    _finite_float(edge[field])
                    for edge in touching if edge.get(field) is not None
                ]
                if values:
                    node[field] = max(values) if field in {"valid_to", "expired_at"} else min(values)
            graph["nodes"][node_id] = node
        for member, canonical in historical_graph["member_to_canonical"].items():
            canonical = historical_id_map.get(canonical, canonical)
            if canonical in graph["nodes"]:
                # Route the member to the ghost alias when the live slot
                # is already occupied so member_to_canonical stays a bijection.
                if graph["nodes"][canonical].get("ghost") is not True:
                    canonical = f"{canonical}:ghost"
            graph["member_to_canonical"][member] = canonical
        for community_id, members in historical_graph["community_members"].items():
            members = [historical_id_map.get(member, member) for member in members]
            existing = graph["community_members"].get(community_id)
            if existing is None:
                graph["community_members"][community_id] = list(members)
            else:
                seen = set(existing)
                for member_id in members:
                    if member_id not in seen:
                        existing.append(member_id)
                        seen.add(member_id)
        for community_id, anchor in historical_graph["community_anchors"].items():
            anchor = historical_id_map.get(anchor, anchor)
            if community_id not in graph["community_anchors"]:
                graph["community_anchors"][community_id] = anchor

    filtered_history_relations: list[dict[str, Any]] = []
    if include_history:
        filtered_history_relations, _ = _complete_relations(
            graph, [edge for edge in edge_rows if edge.get("ghost")], support_rows,
            memory_ids=set(), include_weak_cooccurrence=include_weak_cooccurrence,
            layers=layers, relations=relations, min_support=min_support,
            min_confidence=min_confidence,
        )

    # Complete scenes construct memory and code-memory connectors below. Pruning their
    # entity projection here would discard symbol endpoints before those connectors exist;
    # _build_complete_scene performs the authoritative connected-only pass after assembling
    # every enabled connector kind.
    if connected_only and level != "complete":
        connected_canonical_ids = {
            str(edge[endpoint])
            for edge in graph["edges"]
            for endpoint in ("source", "target")
        }
        connected_canonical_ids.discard("")
        if include_history:
            connected_canonical_ids |= {
                str(edge[endpoint])
                for edge in filtered_history_relations
                for endpoint in ("source", "target")
            }
            connected_canonical_ids.discard("")
        graph["nodes"] = {
            node_id: node for node_id, node in graph["nodes"].items()
            if node_id in connected_canonical_ids
        }
        graph["edges"] = [
            edge for edge in graph["edges"]
            if edge["source"] in graph["nodes"] and edge["target"] in graph["nodes"]
        ]
        graph["community_members"] = {
            community_id: [node_id for node_id in member_ids if node_id in graph["nodes"]]
            for community_id, member_ids in graph["community_members"].items()
            if any(node_id in graph["nodes"] for node_id in member_ids)
        }
    graph["community_anchors"], graph["global_anchor"] = _hierarchy_anchors(
        graph["nodes"], graph["community_members"]
    )
    for node in graph["nodes"].values():
        node["anchor_role"] = "none"
    for anchor_id in graph["community_anchors"].values():
        graph["nodes"][anchor_id]["anchor_role"] = "community"
    if graph["global_anchor"]:
        graph["nodes"][graph["global_anchor"]]["anchor_role"] = "global"
    orbit_slots, _system_radii = _assign_orbit_hierarchy(
        graph["nodes"], graph["community_members"], graph["community_anchors"]
    )
    if level == "complete":
        return _build_complete_scene(
            workspace, graph, edge_rows, support_rows, memory_rows,
            memory_link_rows, code_memory_link_rows,
            include_weak_cooccurrence=include_weak_cooccurrence,
            layers=layers, relations=relations, min_support=min_support,
            min_confidence=min_confidence, connected_only=connected_only,
            include_history=include_history,
            include_memory_nodes=include_memory_nodes, filters=filters or {},
            index_generation=index_generation,
        )
    caps = {
        "overview": (80, 80),
        "system": (150, 400),
        "neighborhood": (100, 250),
        "path": (100, 250),
    }
    default_node_cap, default_edge_cap = caps[level]
    node_cap = min(1000, max(1, int(node_limit or default_node_cap)))
    edge_cap = min(2000, max(0, int(edge_limit if edge_limit is not None else default_edge_cap)))
    nodes = graph["nodes"]
    ranked_nodes = sorted(nodes, key=lambda node_id: (-nodes[node_id]["scene_rank"], node_id))
    ranked_communities = sorted(graph["community_members"], key=lambda community_id: (
        -_community_mass(graph, graph["community_members"][community_id]), community_id
    ))
    if graph["global_anchor"]:
        core_community = nodes[graph["global_anchor"]]["community_id"]
        ranked_communities = [core_community] + [community_id for community_id in ranked_communities
                                                 if community_id != core_community]

    selected: set[str] = set()
    chosen_communities: set[str] = set()
    requested_ids = [value for value in [center_id, *(seeds or [])] if value]
    canonical_requested = [graph["member_to_canonical"].get(value, value)
                           for value in requested_ids]
    explicit_requested = {node_id for node_id in canonical_requested if node_id in nodes}
    historical_node_ids = {
        node_id for node_id, node in nodes.items() if node.get("ghost")
    }
    ghost_relations = filtered_history_relations
    reserved_history_endpoints: set[str] = set()
    history_required_node_ids = set(historical_node_ids)
    if include_history:
        history_required_node_ids.update(
            node_id
            for edge in ghost_relations
            for node_id in (edge["source"], edge["target"])
            if node_id in nodes
        )
        if edge_cap:
            for edge in sorted(ghost_relations, key=lambda item: (
                -float(item.get("strength") or 0.0), item["id"]
            )):
                if edge["source"] in nodes and edge["target"] in nodes:
                    reserved_history_endpoints.update((edge["source"], edge["target"]))
                    break
    # A historical relation is atomic in the UI: returning only one endpoint makes
    # the edge disappear and leaves an unexplained ghost. An undersized caller cap
    # therefore yields the two endpoints of one deterministic relation.
    selection_node_cap = max(node_cap, len(reserved_history_endpoints))

    def eligible(node_id: str) -> bool:
        return nodes[node_id]["entity_quality"] > 0 or node_id in explicit_requested

    if system_id:
        target_system = system_id
        if target_system not in graph["community_members"]:
            canonical = graph["member_to_canonical"].get(system_id, system_id)
            if canonical in nodes:
                explicit_requested.add(canonical)
            target_system = nodes.get(canonical, {}).get("community_id", "")
        if target_system in graph["community_members"]:
            chosen_communities.add(target_system)
            selected.update(
                node_id for node_id in graph["community_members"][target_system]
                if eligible(node_id)
            )
    elif canonical_requested:
        adjacent: dict[str, set[str]] = defaultdict(set)
        for edge in graph["edges"]:
            adjacent[edge["source"]].add(edge["target"])
            adjacent[edge["target"]].add(edge["source"])
        queue = deque((node_id, 0) for node_id in canonical_requested if node_id in nodes)
        visited: set[str] = set()
        while queue:
            node_id, distance = queue.popleft()
            if node_id in visited or distance > max(0, min(2, int(depth))):
                continue
            visited.add(node_id)
            if eligible(node_id):
                selected.add(node_id)
                chosen_communities.add(nodes[node_id]["community_id"])
            for neighbor in sorted(adjacent[node_id]):
                queue.append((neighbor, distance + 1))
    elif level == "overview":
        overview_communities = [
            community_id for community_id in ranked_communities
            if any(nodes[node_id]["entity_quality"] > 0
                   for node_id in graph["community_members"][community_id])
        ][:36]
        chosen_communities.update(overview_communities)
        anchors = [graph["community_anchors"][community_id]
                   for community_id in overview_communities
                   if nodes[graph["community_anchors"][community_id]]["entity_quality"] > 0]
        selected.update(anchors[:selection_node_cap])
        for node_id in ranked_nodes:
            if len(selected) >= selection_node_cap:
                break
            if (nodes[node_id]["community_id"] in chosen_communities
                    and nodes[node_id]["entity_quality"] > 0):
                selected.add(node_id)
    else:
        target = ranked_communities[0] if ranked_communities else ""
        if target:
            chosen_communities.add(target)
            selected.update(
                node_id for node_id in graph["community_members"][target]
                if eligible(node_id)
            )

    if include_history:
        # Retain endpoints of ghost relations so forced historical nodes keep
        # their explanatory edges even when the other endpoint would not
        # otherwise be selected by the overview/community filter.
        selected.update(history_required_node_ids)

    if len(selected) > selection_node_cap:
        forced = {
            graph["community_anchors"][community_id] for community_id in chosen_communities
        }
        forced.add(graph["global_anchor"])
        forced.update(explicit_requested)
        forced.update(history_required_node_ids)
        selected = set(sorted(
            (
                node_id for node_id in forced
                if node_id in selected
                and (eligible(node_id) or node_id in history_required_node_ids)
            ),
            key=lambda node_id: (
                0 if node_id in reserved_history_endpoints else 1,
                0 if node_id in explicit_requested else 1,
                0 if node_id == graph["global_anchor"] else 1,
                -nodes[node_id]["scene_rank"], node_id,
            ),
        )[:selection_node_cap])
        for node_id in ranked_nodes:
            if len(selected) >= selection_node_cap:
                break
            if eligible(node_id) and (
                not chosen_communities or nodes[node_id]["community_id"] in chosen_communities
            ):
                selected.add(node_id)
    chosen_communities = {nodes[node_id]["community_id"] for node_id in selected}
    if include_history:
        # Defer _selected_edges until after ghost filtering; calling it here
        # would mutate the source graph's edge tier fields (backbone/primary)
        # via _selected_edges's in-place tier promotion, and the result is
        # discarded when the history branch re-invokes it with reduced capacity.
        scene_edges: list[dict] = []
        ghost_relations = [
            edge for edge in ghost_relations
            if edge["source"] in selected and edge["target"] in selected
        ]
        historical_node_ids = {
            node_id for node_id in selected if nodes[node_id].get("ghost")
        }
        reserved_history_edges: list[dict] = []
        sorted_ghost = sorted(ghost_relations, key=lambda item: (
            -float(item.get("strength") or 0.0), item["id"]
        ))
        if edge_cap and sorted_ghost:
            uncovered = set(historical_node_ids)
            for edge in sorted_ghost:
                touched = {
                    endpoint for endpoint in (edge["source"], edge["target"])
                    if endpoint in historical_node_ids
                }
                if not touched or not touched.intersection(uncovered):
                    continue
                reserved_history_edges.append(edge)
                uncovered.difference_update(touched)
                if len(reserved_history_edges) >= edge_cap or not uncovered:
                    break
            if not reserved_history_edges:
                # A ghost relation can connect entities that are still live. It
                # remains part of the requested history and needs one reserved slot
                # even though there is no historical-only endpoint to cover.
                reserved_history_edges.append(sorted_ghost[0])
        remaining_capacity = max(0, edge_cap - len(reserved_history_edges))
        scene_edges = _selected_edges(
            graph, selected, level, remaining_capacity,
        )
        scene_edges.extend(reserved_history_edges)
        reserved_set = {edge["id"] for edge in reserved_history_edges}
        scene_edges.extend(
            edge for edge in sorted_ghost
            if edge["id"] not in reserved_set
        )
        scene_edges = scene_edges[:edge_cap]
    else:
        scene_edges = _selected_edges(graph, selected, level, edge_cap)
        ghost_relations = [
            edge for edge in ghost_relations
            if edge["source"] in selected and edge["target"] in selected
        ]
    total_scene_edges = len(graph["edges"]) + len(ghost_relations)
    communities = _community_summaries(graph, chosen_communities, selected)
    bridges = _bridges(graph, chosen_communities, 80)

    hash_payload = {
        "algorithm": ALGORITHM_VERSION,
        "index_generation": index_generation,
        "workspace": workspace,
        "level": level,
        "filters": filters or {},
        "nodes": [
            (node_id, _hash_record(nodes[node_id]))
            for node_id in sorted(selected)
        ],
        "edges": [
            _hash_record(edge)
            for edge in sorted(scene_edges, key=lambda item: item["id"])
        ],
        "communities": [
            (
                community["id"], community["anchor_id"], community["mass"],
                community["radius"], community["member_count"],
                community["shown_member_count"],
            )
            for community in sorted(communities, key=lambda item: item["id"])
        ],
        "bridges": [
            (
                bridge["id"], bridge["aggregate_strength"],
                bridge["physics_strength"], bridge["support_count"],
                bridge["edge_count"],
            )
            for bridge in sorted(bridges, key=lambda item: item["id"])
        ],
    }
    scene_hash = hashlib.sha256(json.dumps(
        hash_payload, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    layout_filters = dict(filters or {})
    layout_filters.pop("include_history", None)
    layout_hash_payload = {
        **hash_payload,
        "filters": layout_filters,
        "nodes": [
            (node_id, _hash_record(nodes[node_id]))
            for node_id in sorted(selected) if not nodes[node_id].get("ghost")
        ],
        "edges": [
            _hash_record(edge)
            for edge in sorted(scene_edges, key=lambda item: item["id"])
            if not edge.get("ghost")
        ],
    }
    layout_hash = hashlib.sha256(json.dumps(
        layout_hash_payload, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    layout_seed = int(layout_hash[:8], 16)

    global_community_id = (
        str(nodes[graph["global_anchor"]]["community_id"])
        if graph["global_anchor"] else ""
    )
    community_positions, community_hints = _community_positions(
        communities, global_community_id, layout_seed, spacing=98.0
    )
    for community in communities:
        community.update(community_hints[community["id"]])
    scene_nodes = []
    for node_id in sorted(selected, key=lambda value: (-nodes[value]["scene_rank"], value)):
        node = dict(nodes[node_id])
        community_id = node["community_id"]
        if node.get("ghost") or community_id not in community_positions:
            x, y = _ghost_position(
                layout_seed, node_id, 98.0 * math.sqrt(len(communities) + 1)
            )
        elif node_id == graph["community_anchors"][community_id]:
            x, y = community_positions[community_id]
        else:
            center_x, center_y = community_positions[community_id]
            x, y = _orbit_position(
                center_x, center_y, community_id,
                orbit_slots[node_id], layout_seed,
            )
        node["x"], node["y"] = round(x, 6), round(y, 6)
        if community_id in community_hints:
            node.update(community_hints[community_id])
        node.pop("aliases", None)
        node.pop("anchor_eligible", None)
        scene_nodes.append(node)

    return {
        "meta": {
            "workspace": workspace,
            "level": level,
            "scene_hash": scene_hash,
            "index_generation": index_generation,
            "total_nodes": len(nodes),
            "total_edges": total_scene_edges,
            "shown_nodes": len(scene_nodes),
            "shown_edges": len(scene_edges),
            "truncated": len(scene_nodes) < len(nodes) or len(scene_edges) < total_scene_edges,
            "query_ms": 0.0,
            "layout_seed": layout_seed,
            "index_state": "ready",
            "filters": filters or {},
            "connected_only": connected_only,
            "include_history": include_history,
            "include_memory_nodes": include_memory_nodes,
            "algorithm_version": ALGORITHM_VERSION,
        },
        "nodes": scene_nodes,
        "edges": scene_edges,
        "communities": communities,
        "community_bridges": bridges,
        "facets": _facets(graph),
    }

_ALL_PRESENTATION_NODE_FIELDS = (
    "id", "label", "type", "node_kind", "community_id", "ghost",
    "x", "y", "gravity_mass", "visual_radius", "mass_score",
    "weighted_degree", "pagerank", "support_count", "scene_rank",
    "anchor_role", "system_anchor_id", "orbit_tier", "orbit_radius",
)
_ALL_PRESENTATION_EDGE_FIELDS = (
    "id", "source", "target", "relation", "layer", "ghost", "strength",
    "rest_length", "spring_strength",
)
_ALL_PRESENTATION_META_FIELDS = (
    "workspace", "level", "scene_hash", "index_generation",
    "total_nodes", "total_edges", "shown_nodes", "shown_edges", "truncated",
    "query_ms", "layout_seed", "index_state", "connected_only",
    "include_history", "include_memory_nodes", "algorithm_version",
)


def project_all_presentation(scene: Mapping[str, Any]) -> dict[str, Any]:
    """Return the compact renderer contract for ``presentation=all``.

    Complete analytical scenes retain provenance, temporal evidence, and inspector fields.
    The all-node renderer needs only stable identity, canonical layout/hierarchy, display
    metrics, and relation physics. Keeping this projection explicit prevents multi-megabyte
    evidence arrays from crossing the HTTP/worker boundary only to be discarded.
    """
    nodes = [
        {key: node[key] for key in _ALL_PRESENTATION_NODE_FIELDS if key in node}
        for node in scene.get("nodes", ())
    ]
    communities = {
        str(node.get("id") or ""): str(node.get("community_id") or "")
        for node in nodes
    }
    edges = []
    for edge in scene.get("edges", ()):
        projected = {
            key: edge[key] for key in _ALL_PRESENTATION_EDGE_FIELDS if key in edge
        }
        source_community = communities.get(str(projected.get("source") or ""), "")
        target_community = communities.get(str(projected.get("target") or ""), "")
        projected["bridge"] = bool(
            source_community and target_community
            and source_community != target_community
        )
        edges.append(projected)
    meta = {
        key: scene.get("meta", {})[key]
        for key in _ALL_PRESENTATION_META_FIELDS
        if key in scene.get("meta", {})
    }
    meta["all_projected"] = True
    return {"meta": meta, "nodes": nodes, "edges": edges}



def strongest_path(graph: dict[str, Any], source: str, target: str, *,
                   max_hops: int = 8, max_visits: int = 10_000) -> dict[str, Any]:
    source_id = graph["member_to_canonical"].get(source, source)
    target_id = graph["member_to_canonical"].get(target, target)
    if source_id not in graph["nodes"] or target_id not in graph["nodes"]:
        return {"found": False, "node_ids": [], "edge_ids": [], "nodes": [],
                "edges": [], "cost": None, "hops": 0, "visited": 0}
    adjacency: dict[str, list[tuple[str, dict, float]]] = defaultdict(list)
    penalties = {"entity": 0.0, "causal": 0.0, "temporal": 0.1, "semantic": 0.2}
    for edge in graph["edges"]:
        cost = -math.log(max(float(edge["strength"]), 0.02))
        cost += 1.0 if edge["relation"] == "co_occurs" else penalties.get(edge["layer"], 0.2)
        adjacency[edge["source"]].append((edge["target"], edge, cost))
        adjacency[edge["target"]].append((edge["source"], edge, cost))
    heap: list[tuple[float, int, str, tuple[str, ...], tuple[str, ...]]] = [
        (0.0, 0, source_id, (source_id,), ())
    ]
    best: dict[tuple[str, int], float] = {(source_id, 0): 0.0}
    visits = 0
    while heap and visits < max(1, max_visits):
        cost, hops, node_id, path_nodes, path_edges = heapq.heappop(heap)
        visits += 1
        if node_id == target_id:
            edge_by_id = {edge["id"]: edge for edge in graph["edges"]}
            return {
                "found": True,
                "node_ids": list(path_nodes),
                "edge_ids": list(path_edges),
                "nodes": [
                    {key: item for key, item in graph["nodes"][value].items()
                     if not key.startswith("_") and key != "anchor_eligible"}
                    for value in path_nodes
                ],
                "edges": [
                    {key: item for key, item in edge_by_id[value].items()
                     if not key.startswith("_")}
                    for value in path_edges
                ],
                "cost": round(cost, 6),
                "hops": hops,
                "visited": visits,
            }
        if hops >= max(1, min(8, int(max_hops))):
            continue
        for neighbor, edge, edge_cost in sorted(
            adjacency[node_id], key=lambda item: (item[2], item[1]["id"], item[0])
        ):
            if neighbor in path_nodes:
                continue
            next_cost = cost + edge_cost
            key = (neighbor, hops + 1)
            if next_cost + 1e-12 >= best.get(key, math.inf):
                continue
            best[key] = next_cost
            heapq.heappush(heap, (
                next_cost, hops + 1, neighbor,
                (*path_nodes, neighbor), (*path_edges, edge["id"]),
            ))
    return {"found": False, "node_ids": [], "edge_ids": [], "nodes": [],
            "edges": [], "cost": None, "hops": 0, "visited": visits}
