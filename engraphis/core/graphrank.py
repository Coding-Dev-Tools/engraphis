"""Deterministic sparse personalized PageRank for local memory graphs.

The graph arm can contain thousands of entities, memories, and links.  A dense
``N × N`` transition matrix turns an otherwise modest local graph into quadratic
memory pressure, so this implementation stores only normalized outgoing edges
and walks them directly.  It deliberately depends on no sparse-matrix package.
"""
from __future__ import annotations

import math


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
    if not adjacency or not seeds:
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
    seed_ids = [node_index[seed] for seed in seeds if seed in node_index]
    live_seeds = [seed for seed in seeds if seed in adjacency and adjacency[seed]]
    if not seed_ids or not live_seeds:
        return {}

    # Aggregate duplicate destinations before applying a source's mass. This
    # matches the old dense matrix's ``M[dst, src] += ...`` semantics while
    # keeping the storage and each iteration O(nodes + edges).
    outgoing: list[list[tuple[int, float]]] = [[] for _ in range(n_nodes)]
    for source in ordered_nodes:
        neighbors = adjacency.get(source, [])
        total = sum(max(float(weight), 0.0) for _, weight in neighbors)
        if total <= 0.0 or not math.isfinite(total):
            continue
        destination_weights: dict[int, float] = {}
        for destination, weight in neighbors:
            if weight > 0.0:
                destination_id = node_index[destination]
                destination_weights[destination_id] = (
                    destination_weights.get(destination_id, 0.0) + float(weight) / total
                )
        outgoing[node_index[source]] = list(destination_weights.items())

    restart = [0.0] * n_nodes
    for seed_id in seed_ids:
        restart[seed_id] = 1.0 / len(seed_ids)
    dangling = [index for index, neighbors in enumerate(outgoing) if not neighbors]
    probability = restart[:]
    iteration_limit = max(0, min(int(iterations), MAX_ITERATIONS))
    for _ in range(iteration_limit):
        spread = [0.0] * n_nodes
        for source_id, edges in enumerate(outgoing):
            if probability[source_id] == 0.0:
                continue
            for destination_id, weight in edges:
                spread[destination_id] += probability[source_id] * weight
        dangling_mass = sum(probability[index] for index in dangling)
        if dangling_mass:
            for index, weight in enumerate(restart):
                if weight:
                    spread[index] += dangling_mass * weight
        next_probability = [
            (1.0 - damping) * restart[index] + damping * spread[index]
            for index in range(n_nodes)
        ]
        if sum(abs(after - before) for after, before in zip(next_probability, probability)) < tol:
            probability = next_probability
            break
        probability = next_probability

    return {
        ordered_nodes[index]: score
        for index, score in enumerate(probability)
        if score > 0.0
    }
