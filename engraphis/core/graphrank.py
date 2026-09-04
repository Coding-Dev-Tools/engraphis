"""Deterministic sparse personalized PageRank for local memory graphs.

The graph arm can contain thousands of entities, memories, and links.  A dense
``N × N`` transition matrix turns an otherwise modest local graph into quadratic
memory pressure, so this implementation stores only normalized outgoing edges
and walks them directly.  It deliberately depends on no sparse-matrix package.
"""
from __future__ import annotations

import math

import numpy as np


DAMPING = 0.85
ITERATIONS = 30
TOL = 1e-9
# Safety limits for direct callers. Recall already builds a bounded scoped graph;
# these make a malformed local/plugin adjacency fail deterministically rather than
# allocating unbounded state. They are comfortably above normal local graph arms.
MAX_NODES = 100_000
MAX_EDGES = 1_000_000
MAX_ITERATIONS = 100


def personalized_pagerank(
    adjacency: dict[str, list[tuple[str, float]]],
    seeds: list[str],
    *,
    damping: float = DAMPING,
    iterations: int = ITERATIONS,
    tol: float = TOL,
) -> dict[str, float]:
    """Rank nodes by a sparse random walk with restart.

    ``adjacency`` maps node -> ``[(neighbor, weight), ...]``; pass both
    directions for an undirected graph. Unknown seed ids retain the legacy
    restart behavior when at least one seed has outgoing adjacency. Oversized
    inputs return ``{}`` deterministically instead of attempting an unbounded
    local computation.
    """
    if not isinstance(adjacency, dict) or not isinstance(seeds, list):
        return {}
    if not adjacency or not seeds:
        return {}
    try:
        damping = float(damping)
        tol = float(tol)
        iterations = int(iterations)
    except (TypeError, ValueError, OverflowError):
        return {}
    if not math.isfinite(damping) or not 0.0 <= damping <= 1.0:
        return {}
    if not math.isfinite(tol) or tol < 0.0:
        return {}
    sanitized: dict[str, list[tuple[str, float]]] = {}
    for source, neighbors in adjacency.items():
        if not isinstance(source, str) or not isinstance(neighbors, (list, tuple)):
            continue
        clean_neighbors: list[tuple[str, float]] = []
        for item in neighbors:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                continue
            destination, weight = item
            if not isinstance(destination, str):
                continue
            try:
                weight = float(weight)
            except (TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(weight) and weight > 0.0:
                clean_neighbors.append((destination, weight))
        sanitized[source] = clean_neighbors
    adjacency = sanitized
    if not any(adjacency.values()):
        return {}
    nodes = set(adjacency)
    edge_count = 0
    for neighbors in adjacency.values():
        edge_count += len(neighbors)
        if edge_count > MAX_EDGES:
            return {}
        nodes.update(dst for dst, _ in neighbors)
    nodes.update(seeds)
    if len(nodes) > MAX_NODES:
        return {}

    ordered_nodes = sorted(nodes)
    node_index = {node: index for index, node in enumerate(ordered_nodes)}
    n_nodes = len(ordered_nodes)
    seed_ids = list(dict.fromkeys(node_index[seed] for seed in seeds if seed in node_index))
    live_seeds = [seed for seed in seeds if seed in adjacency and adjacency[seed]]
    if not seed_ids or not live_seeds:
        return {}

    # Aggregate duplicate destinations before applying a source's mass. This
    # matches the old dense matrix's ``M[dst, src] += ...`` semantics while
    # keeping the storage and each iteration O(nodes + edges).
    src_list: list[int] = []
    dst_list: list[int] = []
    weight_list: list[float] = []
    dangling_list: list[int] = []

    for source_id, source in enumerate(ordered_nodes):
        neighbors = adjacency.get(source, [])
        total = sum(weight for _, weight in neighbors)
        if total <= 0.0 or not math.isfinite(total):
            dangling_list.append(source_id)
            continue
        destination_weights: dict[int, float] = {}
        for destination, weight in neighbors:
            destination_id = node_index[destination]
            destination_weights[destination_id] = (
                destination_weights.get(destination_id, 0.0) + weight / total
            )
        if not destination_weights:
            dangling_list.append(source_id)
            continue
        for destination_id, weight in destination_weights.items():
            src_list.append(source_id)
            dst_list.append(destination_id)
            weight_list.append(weight)

    src_arr = np.array(src_list, dtype=np.intp)
    dst_arr = np.array(dst_list, dtype=np.intp)
    weight_arr = np.array(weight_list, dtype=np.float64)
    dangling_arr = np.array(dangling_list, dtype=np.intp)

    restart = np.zeros(n_nodes, dtype=np.float64)
    restart[seed_ids] = 1.0 / len(seed_ids)
    probability = restart.copy()
    spread = np.zeros(n_nodes, dtype=np.float64)
    iteration_limit = max(0, min(int(iterations), MAX_ITERATIONS))

    for _ in range(iteration_limit):
        spread.fill(0.0)
        if src_arr.size > 0:
            np.add.at(spread, dst_arr, probability[src_arr] * weight_arr)
        if dangling_arr.size > 0:
            dangling_mass = float(np.sum(probability[dangling_arr]))
            if dangling_mass:
                spread += dangling_mass * restart
        next_probability = (1.0 - damping) * restart + damping * spread
        diff = float(np.sum(np.abs(next_probability - probability)))
        probability = next_probability
        if diff < tol:
            break

    pos = np.flatnonzero(probability > 0.0)
    return {
        ordered_nodes[index]: float(probability[index])
        for index in pos
    }
