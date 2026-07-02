"""Personalized PageRank over the memory/entity graph (MASTER_PLAN.md §13.5, Phase 2).

HippoRAG-style single-step graph retrieval: seed the walk at the query's entities and
let the stationary distribution rank everything reachable — multi-hop associations
included — instead of expanding a fixed number of hops. Pure NumPy (AGENTS.md §3.8),
deterministic, and sized for the local-first reality: the adjacency is built per query
from the scoped store (hundreds to low thousands of nodes), where a dense power
iteration is both exact and fast. A sparse/persistent implementation can replace this
behind the same function signature when scale demands it.
"""
from __future__ import annotations

import numpy as np

DAMPING = 0.85
ITERATIONS = 30
TOL = 1e-9


def personalized_pagerank(
    adjacency: dict[str, list[tuple[str, float]]],
    seeds: list[str],
    *,
    damping: float = DAMPING,
    iterations: int = ITERATIONS,
    tol: float = TOL,
) -> dict[str, float]:
    """Rank nodes by their stationary probability under a random walk with restart.

    ``adjacency`` maps node -> [(neighbor, weight), ...]; pass both directions for an
    undirected graph. ``seeds`` are the restart set (unknown seeds are ignored). Nodes
    unreachable from every seed score 0. Returns {} when there is nothing to walk.
    """
    if not adjacency or not seeds:
        return {}
    nodes: list[str] = sorted(
        set(adjacency)
        | {dst for nbrs in adjacency.values() for dst, _ in nbrs}
        | set(seeds)
    )
    idx = {n: i for i, n in enumerate(nodes)}
    n = len(nodes)

    seed_ids = [idx[s] for s in seeds if s in idx]
    live_seeds = [s for s in seeds if s in adjacency and adjacency[s]]
    if not seed_ids or not live_seeds:
        return {}

    # Column-stochastic transition matrix; dangling nodes restart to the seeds.
    M = np.zeros((n, n), dtype=np.float64)
    for src, nbrs in adjacency.items():
        col = idx[src]
        total = float(sum(max(w, 0.0) for _, w in nbrs))
        if total <= 0.0:
            continue
        for dst, w in nbrs:
            if w > 0.0:
                M[idx[dst], col] += w / total

    restart = np.zeros(n, dtype=np.float64)
    restart[seed_ids] = 1.0 / len(seed_ids)
    dangling = M.sum(axis=0) == 0.0

    p = restart.copy()
    for _ in range(iterations):
        spread = M @ p + p[dangling].sum() * restart
        p_next = (1.0 - damping) * restart + damping * spread
        if float(np.abs(p_next - p).sum()) < tol:
            p = p_next
            break
        p = p_next

    return {nodes[i]: float(p[i]) for i in range(n) if p[i] > 0.0}
