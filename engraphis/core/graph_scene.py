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
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict, deque
from typing import Any, Iterable, Mapping, Optional, Sequence


ALGORITHM_VERSION = "galaxy-v2"
PUBLIC_REFERENCE_ID_LIMIT = 200
PUBLIC_FACET_LIMIT = 100
GOLDEN_ANGLE = math.pi * (3.0 - math.sqrt(5.0))
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


def _mass_percentile(value: float, ordered: Sequence[float]) -> float:
    """Tie-aware percentile for non-negative mass inputs.

    Zero means no evidence and must remain zero. Positive ties receive their mid-rank
    instead of the top of the tie block; otherwise thousands of isolates all acquire a
    near-maximal mass merely because they share the same zero degree/support values.
    """
    if value <= 0.0 or not ordered:
        return 0.0
    if len(ordered) == 1:
        return 1.0
    left = bisect_left(ordered, value)
    right = bisect_right(ordered, value)
    midpoint = (left + right - 1) / 2.0
    return _clamp(midpoint / (len(ordered) - 1))


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
        complement *= 1.0 - _clamp(float(value), 0.05, 0.99)
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
        nodes[canonical_id] = {
            "id": canonical_id,
            "canonical_id": canonical_id,
            "label": label,
            "type": entity_type,
            "member_ids": sorted(str(item["id"]) for item in group),
            "member_count": len(group),
            "repo_ids": repo_ids,
            "aliases": sorted(labels, key=lambda item: (item.casefold(), item)),
        }

    supports_by_edge: dict[str, list[dict]] = defaultdict(list)
    for raw in support_rows:
        support = _row(raw)
        supports_by_edge[str(support.get("edge_id") or "")].append(support)

    bundled: dict[tuple[str, str, str, str, bool], dict] = {}
    for raw in edge_rows:
        edge = _row(raw)
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
        if not evidence:
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
                "weight": max(0.05, min(4.0, float(edge.get("weight") or 1.0))),
                "_confidence_by_support": {},
                "_support_ids": set(),
                "_support_rows": [],
                "_memory_types": set(),
                "_support_times": [],
                "underlying_edge_ids": [],
            }
            bundled[key] = item
        item["weight"] = max(item["weight"], float(edge.get("weight") or 1.0))
        for index, row in enumerate(evidence):
            memory_id = str(row.get("memory_id") or "")
            support_key = memory_id or f"anonymous:{edge_id}:{index}"
            support_confidence = float(
                row.get("confidence") if row.get("confidence") is not None else 0.50
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
        item["_support_times"].extend(
            float(row["support_time"]) for row in evidence
            if row.get("support_time") is not None
        )
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
        node_supports[source].update(edge["_support_ids_all"])
        node_supports[target].update(edge["_support_ids_all"])
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

    degree_values = sorted(degree.values())
    pagerank_values = sorted(pagerank.values())
    support_values = sorted(float(len(value)) for value in node_supports.values())
    repo_values = sorted(float(len(node["repo_ids"])) for node in nodes.values())
    max_pagerank = max(pagerank.values(), default=1.0) or 1.0
    for node_id, node in nodes.items():
        obvious_noise = is_obvious_entity_noise(node["label"], node["type"])
        quality = 0.0 if obvious_noise else 1.0
        support_count = len(node_supports[node_id])
        mass_score = quality * (
            0.45 * _mass_percentile(degree[node_id], degree_values)
            + 0.30 * _mass_percentile(pagerank[node_id], pagerank_values)
            + 0.15 * _mass_percentile(float(support_count), support_values)
            + 0.10 * _mass_percentile(float(len(node["repo_ids"])), repo_values)
        )
        node.update({
            "weighted_degree": round(degree[node_id], 6),
            "pagerank": round(pagerank[node_id] / max_pagerank, 6),
            "support_count": support_count,
            "entity_quality": quality,
            "mass_score": round(_clamp(mass_score), 6),
            "gravity_mass": round(1.0 + 7.0 * (_clamp(mass_score) ** 1.35), 6),
            "visual_radius": round(2.5 + 6.0 * math.sqrt(_clamp(mass_score)), 6),
            "anchor_eligible": bool(quality),
        })

    components = _components(sorted(nodes), edges)
    communities = _louvain(sorted(nodes), edges)
    eligible = [node for node in nodes.values() if node["anchor_eligible"]] or list(nodes.values())
    global_anchor = min(
        eligible,
        key=lambda node: (-node["mass_score"], -node["weighted_degree"], node["canonical_id"]),
        default=None,
    )
    global_id = global_anchor["id"] if global_anchor else ""
    community_members: dict[str, list[str]] = defaultdict(list)
    for node_id in sorted(nodes):
        community_members[communities[node_id]].append(node_id)
    community_anchors: dict[str, str] = {}
    for community_id, ids in community_members.items():
        pool = [nodes[node_id] for node_id in ids if nodes[node_id]["anchor_eligible"]]
        pool = pool or [nodes[node_id] for node_id in ids]
        community_anchors[community_id] = min(
            pool,
            key=lambda node: (-node["mass_score"], -node["weighted_degree"], node["id"]),
        )["id"]

    direct_core: dict[str, float] = defaultdict(float)
    for edge in edges:
        if edge["source"] == global_id:
            direct_core[edge["target"]] = max(direct_core[edge["target"]], edge["strength"])
        if edge["target"] == global_id:
            direct_core[edge["source"]] = max(direct_core[edge["source"]], edge["strength"])
    for node_id, node in nodes.items():
        community_id = communities[node_id]
        role = "global" if node_id == global_id else (
            "community" if community_anchors[community_id] == node_id else "none"
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
    if level == "overview":
        candidates = [edge for edge in candidates if
                      graph["nodes"][edge["source"]]["community_id"]
                      == graph["nodes"][edge["target"]]["community_id"]]
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
        anchor_id = graph["community_anchors"][community_id]
        internal = [edge for edge in edges if edge["source"] in member_ids
                    and edge["target"] in member_ids]
        external = [edge for edge in edges if
                    (edge["source"] in member_ids) != (edge["target"] in member_ids)]
        mass = _community_mass(graph, member_ids)
        representatives = sorted(member_ids, key=lambda node_id: (
            -graph["nodes"][node_id]["scene_rank"], node_id
        ))[:8]
        result.append({
            "id": community_id,
            "label": f"{graph['nodes'][anchor_id]['label']} System",
            "anchor_id": anchor_id,
            "mass": round(mass, 6),
            "radius": round(_clamp(30.0 + 5.0 * math.sqrt(len(member_ids)), 36.0, 110.0), 6),
            "member_count": len(member_ids),
            "shown_member_count": len(set(member_ids).intersection(selected)),
            "internal_strength": round(sum(edge["strength"] for edge in internal), 6),
            "external_strength": round(sum(edge["strength"] for edge in external), 6),
            "representative_ids": representatives,
        })
    return sorted(result, key=lambda item: (-item["mass"], item["id"]))


def _community_mass(graph: dict, member_ids: Iterable[str]) -> float:
    """Return the same aggregate mass used by the system-layout contract."""
    return sum(
        math.sqrt(max(1.0, float(graph["nodes"][node_id]["gravity_mass"])))
        for node_id in member_ids
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
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return every filtered physical relation and its explicit evidence links.

    Normal analytical scenes intentionally bundle parallel canonical relations.  A
    complete scene has the opposite contract: the physical edge id is the public id,
    and each supporting memory is connected to both relation endpoints.  The latter
    makes evidence selectable without replacing or hiding the factual relation.
    """
    supports_by_edge: dict[str, list[dict[str, Any]]] = defaultdict(list)
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
        evidence = [dict(item) for item in supports_by_edge.get(edge_id, [])]
        if not evidence:
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
                float(support.get("confidence")
                      if support.get("confidence") is not None else 0.50),
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

        weight = max(0.05, min(4.0, float(edge.get("weight") or 1.0)))
        support_boost = 1.0 + min(math.log2(1.0 + support_count) / 4.0, 0.75)
        raw_log = math.log1p(
            weight * confidence * support_boost * _relation_factor(layer, relation)
        )
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
            "_raw_log": raw_log,
        })
        for support in evidence:
            memory_id = str(support.get("memory_id") or "")
            if not memory_id or memory_id not in memory_ids:
                continue
            source_kind = str(support.get("source_kind") or "legacy_unknown")
            evidence_confidence = _clamp(float(
                support.get("confidence")
                if support.get("confidence") is not None else 0.50
            ), 0.05, 0.99)
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
                    "source_kind": source_kind,
                    "strength": round(evidence_confidence, 6),
                    "rest_length": round(12.0 + 10.0 * (1.0 - evidence_confidence), 6),
                    "spring_strength": round(0.04 + 0.12 * evidence_confidence, 6),
                })

    low, high = _quantile(raw_logs, 0.05), _quantile(raw_logs, 0.95)
    relations_out = []
    for edge in pending:
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
    filters: dict[str, Any],
    index_generation: int,
) -> dict[str, Any]:
    memory_rows_by_id = {
        str(row.get("id") or ""): _row(row) for row in memory_rows if row.get("id")
    }
    memory_ids = set(memory_rows_by_id)
    raw_relations, evidence_edges = _complete_relations(
        graph, edge_rows, support_rows, memory_ids=memory_ids,
        include_weak_cooccurrence=include_weak_cooccurrence,
        layers=layers, relations=relations, min_support=min_support,
        min_confidence=min_confidence,
    )

    entity_nodes = {node_id: dict(node) for node_id, node in graph["nodes"].items()}
    for node in entity_nodes.values():
        node["node_kind"] = "entity"
        node.pop("aliases", None)
        node.pop("anchor_eligible", None)

    evidence_targets: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for edge in evidence_edges:
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
        memory_degree[edge["source"]] += 1
    memory_link_edges = []
    for raw in sorted(memory_link_rows, key=lambda item: (
        str(item.get("a") or ""), str(item.get("b") or ""),
        str(item.get("relation") or ""), float(item.get("created_at") or 0.0),
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
            "reason": str(row.get("reason") or ""),
            "strength": 0.72,
            "rest_length": 22.0,
            "spring_strength": 0.12,
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
        confidence = _clamp(float(row.get("confidence") or 1.0), 0.05, 1.0)
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
            "strength": round(confidence, 6),
            "rest_length": round(14.0 + 8.0 * (1.0 - confidence), 6),
            "spring_strength": round(0.05 + 0.12 * confidence, 6),
        })

    memory_nodes: dict[str, dict[str, Any]] = {}
    degree_values = sorted(float(memory_degree[memory_id]) for memory_id in memory_ids)
    for memory_id, memory in sorted(memory_rows_by_id.items()):
        title = str(memory.get("title") or "").strip()
        summary = str(memory.get("summary") or "").strip()
        content = str(memory.get("content") or "").strip()
        label = title or summary or content or memory_id
        label = " ".join(label.split())[:160]
        importance = _clamp(float(memory.get("importance") or 0.0))
        degree_percentile = _mass_percentile(
            float(memory_degree[memory_id]), degree_values
        )
        mass_score = _clamp(0.08 + 0.34 * importance + 0.18 * degree_percentile, 0.08, 0.60)
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
            "weighted_degree": round(float(memory_degree[memory_id]), 6),
            "pagerank": 0.0,
            "support_count": int(memory_degree[memory_id]),
            "entity_quality": 1.0,
            "mass_score": round(mass_score, 6),
            "gravity_mass": round(0.55 + 2.2 * (mass_score ** 1.35), 6),
            "visual_radius": round(2.0 + 3.5 * math.sqrt(mass_score), 6),
            "component_id": f"component_memory_{memory_id}",
            "community_id": memory_community[memory_id],
            "anchor_role": "none",
            "core_affinity": 0.0,
            "scene_rank": round(_clamp(0.70 * mass_score + 0.30 * degree_percentile), 6),
            "importance": round(importance, 6),
            "pinned": bool(memory.get("pinned")),
            "valid_from": memory.get("valid_from"),
            "ingested_at": memory.get("ingested_at"),
        }

    all_nodes: dict[str, dict[str, Any]] = {**entity_nodes, **memory_nodes}
    community_members: dict[str, list[str]] = defaultdict(list)
    for node_id, node in all_nodes.items():
        community_members[node["community_id"]].append(node_id)
    community_anchors = dict(graph["community_anchors"])
    for community_id, member_ids in sorted(community_members.items()):
        if community_id not in community_anchors:
            community_anchors[community_id] = min(member_ids, key=lambda node_id: (
                -all_nodes[node_id]["scene_rank"], node_id
            ))
            all_nodes[community_anchors[community_id]]["anchor_role"] = "community"

    global_anchor = graph["global_anchor"]
    if not global_anchor and all_nodes:
        global_anchor = min(all_nodes, key=lambda node_id: (
            -all_nodes[node_id]["scene_rank"], node_id
        ))
        all_nodes[global_anchor]["anchor_role"] = "global"
        community_anchors[all_nodes[global_anchor]["community_id"]] = global_anchor

    complete_edges = sorted(
        [*raw_relations, *evidence_edges, *memory_link_edges, *code_memory_edges],
        key=lambda edge: (
            edge["connector_kind"], -float(edge["strength"]), edge["id"]
        ),
    )
    internal_strength: dict[str, float] = defaultdict(float)
    external_strength: dict[str, float] = defaultdict(float)
    for edge in complete_edges:
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
        anchor_id = community_anchors[community_id]
        mass = sum(math.sqrt(max(0.1, float(all_nodes[node_id]["gravity_mass"])))
                   for node_id in member_ids)
        communities.append({
            "id": community_id,
            "label": f"{all_nodes[anchor_id]['label']} System",
            "anchor_id": anchor_id,
            "mass": round(mass, 6),
            "radius": round(_clamp(
                30.0 + 5.0 * math.sqrt(len(member_ids)), 36.0, 180.0
            ), 6),
            "member_count": len(member_ids),
            "shown_member_count": len(member_ids),
            "internal_strength": round(internal_strength[community_id], 6),
            "external_strength": round(external_strength[community_id], 6),
            "representative_ids": sorted(member_ids, key=lambda node_id: (
                -all_nodes[node_id]["scene_rank"], node_id
            ))[:8],
        })
    communities.sort(key=lambda item: (-item["mass"], item["id"]))

    hash_payload = {
        "algorithm": f"{ALGORITHM_VERSION}-complete-1",
        "index_generation": index_generation,
        "workspace": workspace,
        "filters": filters,
        "nodes": [(
            node_id, all_nodes[node_id]["node_kind"], all_nodes[node_id]["label"],
            all_nodes[node_id]["community_id"], all_nodes[node_id]["mass_score"],
        ) for node_id in sorted(all_nodes)],
        "edges": [(
            edge["id"], edge["source"], edge["target"], edge["relation"],
            edge["connector_kind"], edge["strength"],
        ) for edge in sorted(complete_edges, key=lambda item: item["id"])],
    }
    scene_hash = hashlib.sha256(json.dumps(
        hash_payload, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    layout_seed = int(scene_hash[:8], 16)

    positions: dict[str, tuple[float, float]] = {}
    for index, community in enumerate(communities):
        if global_anchor in community_members[community["id"]]:
            positions[community["id"]] = (0.0, 0.0)
        else:
            radius = 82.0 * math.sqrt(index + 1)
            angle = GOLDEN_ANGLE * (index + 1) + (layout_seed % 360) * math.pi / 180.0
            positions[community["id"]] = (
                radius * math.cos(angle), radius * math.sin(angle)
            )
    ranks: dict[str, int] = defaultdict(int)
    scene_nodes = []
    radius_by_community = {item["id"]: item["radius"] for item in communities}
    for node_id in sorted(all_nodes, key=lambda value: (
        -all_nodes[value]["scene_rank"], value
    )):
        node = dict(all_nodes[node_id])
        community_id = node["community_id"]
        center_x, center_y = positions[community_id]
        rank = ranks[community_id]
        ranks[community_id] += 1
        if node_id == community_anchors[community_id]:
            x, y = center_x, center_y
        else:
            system_radius = radius_by_community[community_id]
            orbit = _clamp(
                13.0 + 5.5 * math.sqrt(rank + 1)
                + (1.0 - node["mass_score"]) * 0.35 * system_radius,
                14.0, system_radius,
            )
            angle = GOLDEN_ANGLE * (rank + 1) + (layout_seed % 180) * math.pi / 180.0
            x = center_x + orbit * math.cos(angle)
            y = center_y + orbit * math.sin(angle)
        node["x"], node["y"] = round(x, 6), round(y, 6)
        scene_nodes.append(node)

    facets = _facets(graph)
    memory_type_counts = Counter(node["memory_type"] for node in memory_nodes.values())
    facets["memory_types"] = [{"value": value, "count": count}
                               for value, count in sorted(
        memory_type_counts.items(), key=lambda item: (-item[1], item[0])
    )[:PUBLIC_FACET_LIMIT]]
    bridges = _complete_bridges(all_nodes, complete_edges)
    return {
        "meta": {
            "workspace": workspace,
            "level": "complete",
            "complete_scene": True,
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
            "algorithm_version": f"{ALGORITHM_VERSION}-complete-1",
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
    filters: Optional[dict] = None,
    index_generation: int = 4,
) -> dict[str, Any]:
    level = level if level in {
        "overview", "system", "neighborhood", "path", "complete"
    } else "overview"
    graph = build_canonical_graph(
        entity_rows, edge_rows, support_rows,
        include_weak_cooccurrence=include_weak_cooccurrence,
        layers=layers, relations=relations,
        min_support=min_support, min_confidence=min_confidence,
    )
    if level == "complete":
        return _build_complete_scene(
            workspace, graph, edge_rows, support_rows, memory_rows,
            memory_link_rows, code_memory_link_rows,
            include_weak_cooccurrence=include_weak_cooccurrence,
            layers=layers, relations=relations, min_support=min_support,
            min_confidence=min_confidence, filters=filters or {},
            index_generation=index_generation,
        )
    caps = {
        "overview": (80, 80),
        "system": (150, 400),
        "neighborhood": (100, 250),
        "path": (100, 250),
    }
    default_node_cap, default_edge_cap = caps[level]
    node_cap = min(300, max(1, int(node_limit or default_node_cap)))
    edge_cap = min(900, max(0, int(edge_limit if edge_limit is not None else default_edge_cap)))
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
        ][:24]
        chosen_communities.update(overview_communities)
        anchors = [graph["community_anchors"][community_id]
                   for community_id in overview_communities
                   if nodes[graph["community_anchors"][community_id]]["entity_quality"] > 0]
        selected.update(anchors[:node_cap])
        for node_id in ranked_nodes:
            if len(selected) >= node_cap:
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

    if len(selected) > node_cap:
        forced = {
            graph["community_anchors"][community_id] for community_id in chosen_communities
        }
        forced.add(graph["global_anchor"])
        forced.update(explicit_requested)
        selected = set(sorted(
            (node_id for node_id in forced if node_id in selected and eligible(node_id)),
            key=lambda node_id: (
                0 if node_id in explicit_requested else 1,
                0 if node_id == graph["global_anchor"] else 1,
                -nodes[node_id]["scene_rank"], node_id,
            ),
        )[:node_cap])
        for node_id in ranked_nodes:
            if len(selected) >= node_cap:
                break
            if eligible(node_id) and (
                not chosen_communities or nodes[node_id]["community_id"] in chosen_communities
            ):
                selected.add(node_id)
    chosen_communities = {nodes[node_id]["community_id"] for node_id in selected}
    scene_edges = _selected_edges(graph, selected, level, edge_cap)
    communities = _community_summaries(graph, chosen_communities, selected)
    bridges = _bridges(graph, chosen_communities, 80)

    hash_payload = {
        "algorithm": ALGORITHM_VERSION,
        "index_generation": index_generation,
        "workspace": workspace,
        "level": level,
        "filters": filters or {},
        "nodes": [
            (
                node_id, nodes[node_id]["label"], nodes[node_id]["type"],
                nodes[node_id]["mass_score"], nodes[node_id]["gravity_mass"],
                nodes[node_id]["visual_radius"], nodes[node_id]["community_id"],
                nodes[node_id]["anchor_role"], nodes[node_id]["scene_rank"],
            )
            for node_id in sorted(selected)
        ],
        "edges": [
            (
                edge["id"], edge["strength"], edge["rest_length"],
                edge["spring_strength"], edge["support_count"], edge["confidence"],
                edge["tier"], edge["visible_by_default"],
            )
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
    layout_seed = int(scene_hash[:8], 16)

    community_positions: dict[str, tuple[float, float]] = {}
    for index, community in enumerate(communities):
        if graph["global_anchor"] in graph["community_members"][community["id"]]:
            community_positions[community["id"]] = (0.0, 0.0)
            continue
        radius = 145.0 * math.sqrt(index + 1)
        angle = GOLDEN_ANGLE * (index + 1) + (layout_seed % 360) * math.pi / 180.0
        community_positions[community["id"]] = (
            radius * math.cos(angle), radius * math.sin(angle)
        )
    scene_nodes = []
    ranks_by_community: dict[str, int] = defaultdict(int)
    for node_id in sorted(selected, key=lambda value: (-nodes[value]["scene_rank"], value)):
        node = dict(nodes[node_id])
        community_id = node["community_id"]
        center_x, center_y = community_positions.get(community_id, (0.0, 0.0))
        rank = ranks_by_community[community_id]
        ranks_by_community[community_id] += 1
        if node["anchor_role"] in {"global", "community"}:
            x, y = center_x, center_y
        else:
            system_radius = next(
                item["radius"] for item in communities if item["id"] == community_id
            )
            orbit = _clamp(
                14.0 + 9.0 + ((1.0 - node["mass_score"]) ** 1.4)
                * (system_radius - 18.0), 14.0, system_radius
            )
            angle = GOLDEN_ANGLE * (rank + 1) + (layout_seed % 180) * math.pi / 180.0
            x, y = center_x + orbit * math.cos(angle), center_y + orbit * math.sin(angle)
        node["x"], node["y"] = round(x, 6), round(y, 6)
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
            "total_edges": len(graph["edges"]),
            "shown_nodes": len(scene_nodes),
            "shown_edges": len(scene_edges),
            "truncated": len(scene_nodes) < len(nodes) or len(scene_edges) < len(graph["edges"]),
            "query_ms": 0.0,
            "layout_seed": layout_seed,
            "index_state": "ready",
            "filters": filters or {},
            "algorithm_version": ALGORITHM_VERSION,
        },
        "nodes": scene_nodes,
        "edges": scene_edges,
        "communities": communities,
        "community_bridges": bridges,
        "facets": _facets(graph),
    }


def strongest_path(graph: dict[str, Any], source: str, target: str, *,
                   max_hops: int = 8, max_visits: int = 10_000) -> dict[str, Any]:
    source_id = graph["member_to_canonical"].get(source, source)
    target_id = graph["member_to_canonical"].get(target, target)
    if source_id not in graph["nodes"] or target_id not in graph["nodes"]:
        return {"found": False, "node_ids": [], "edge_ids": [], "nodes": [],
                "edges": [], "cost": None, "hops": 0}
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
