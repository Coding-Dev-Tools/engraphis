"""Shared entity-relation graph shaping for the Graph tab.

Both ``dashboard_app.py`` (the restored v1-look UI, served on the v2 engine) and
``inspector/`` (the flagship v2 product UI) render the same force-directed
knowledge graph over the ``entities``/``edges`` tables. Extracting the raw-row
-> response shaping here keeps the two UIs from silently drifting apart, in
the same spirit as AGENTS.md's "pure, tested functions over duplicated inline
logic" (this module has no I/O and no dependency on FastAPI/MemoryService, so
it is trivial to unit test).
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

DEFAULT_ETYPE = "person_or_concept"


def empty_graph(workspace: str) -> dict:
    """The shape returned for a workspace that doesn't exist yet (not an error —
    a brand-new workspace with no memories simply has no entities yet)."""
    return {
        "workspace": workspace, "nodes": [], "edges": [], "types": [], "top": [],
        "stats": {"entities": 0, "edges": 0, "connected": 0, "isolated": 0},
    }


def build_graph_payload(workspace: str, entity_rows: Sequence[Mapping[str, Any]],
                         edge_rows: Sequence[Mapping[str, Any]]) -> dict:
    """Shape raw ``entities``/``edges`` rows into the Graph tab's payload:
    vis-network-ready nodes/edges, per-type counts, top-connected entities, and
    connectivity stats.

    ``entity_rows``: objects with ``id`` / ``name`` / ``etype`` (e.g. ``sqlite3.Row``).
    ``edge_rows``: objects with ``src`` / ``dst`` / ``relation``, where ``src``/``dst``
    are entity **ids** (that's what ``backends.graph_extractor.feed`` writes into the
    ``edges`` table — never the entity's display name). Node identity here must key off
    ``id`` for exactly that reason: keying off name instead (as this used to) silently
    duplicated every connected entity into a correctly-named-but-falsely-isolated node
    plus a correctly-connected-but-id-labeled phantom (visible as stray "ent_..."-named
    nodes on the Graph tab).
    """
    label_of = {r["id"]: (r["name"] or r["id"]) for r in entity_rows}
    etype_of = {r["id"]: (r["etype"] or DEFAULT_ETYPE) for r in entity_rows}
    deg: dict = {}
    edges = []
    for e in edge_rows:
        src, dst, rel = e["src"], e["dst"], e["relation"]
        if not src or not dst:
            continue
        deg[src] = deg.get(src, 0) + 1
        deg[dst] = deg.get(dst, 0) + 1
        edges.append({"from": src, "to": dst, "label": rel or ""})

    # every node referenced by an edge must exist so the network renders cleanly, even
    # in the (should-never-happen) case an edge outlives its entity row
    ids_ = set(label_of) | set(deg)
    nodes = [{"id": i, "label": label_of.get(i, i), "etype": etype_of.get(i, DEFAULT_ETYPE),
              "degree": deg.get(i, 0)} for i in ids_]

    types: dict = {}
    for n in nodes:
        types[n["etype"]] = types.get(n["etype"], 0) + 1
    top = sorted(({"id": i, "name": label_of.get(i, i), "degree": d} for i, d in deg.items()),
                 key=lambda r: -r["degree"])[:12]
    connected = sum(1 for n in nodes if n["degree"] > 0)
    return {
        "workspace": workspace, "nodes": nodes, "edges": edges,
        "types": [{"etype": k, "count": v}
                  for k, v in sorted(types.items(), key=lambda kv: -kv[1])],
        "top": top,
        "stats": {"entities": len(nodes), "edges": len(edges),
                  "connected": connected, "isolated": len(nodes) - connected},
    }
