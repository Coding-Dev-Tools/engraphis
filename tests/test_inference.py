"""Cross-cluster associative inference (consolidate.infer_links) — dream pass 4.

An entity that appears across two dissimilar subject clusters should be proposed as a
bridge. The write is dry-run by default; when applied it produces a low-salience,
untrusted, linked memory — never a trusted fabricated fact.
"""

from engraphis.core import consolidate
from engraphis.core.interfaces import MemoryType, SearchFilter
from engraphis.service import MemoryService

# Two dissimilar subjects, both mentioning the entity "Redis".
CACHING = [
    "Redis caches API responses to cut request latency for the gateway.",
    "The Redis cache lowers latency and raises throughput on gateway API responses.",
]
SESSIONS = [
    "User login sessions are stored in Redis keyed by a signed session token.",
    "Redis holds the session token for each user login and expires it on logout.",
]


def _seed():
    svc = MemoryService.create(":memory:")  # graph_extractor defaults to regex → entities
    wid = svc.store.get_or_create_workspace("ws")
    for text in CACHING + SESSIONS:
        # keep all four live (don't let the resolver supersede same-subject pairs)
        svc.remember(text, workspace="ws", mtype="episodic", resolve_conflicts=False)
    return svc, wid


def _live(svc, wid):
    return svc.store.list_memories(SearchFilter(workspace_id=wid), limit=100)


def test_entity_bridge_exists_in_graph():
    svc, wid = _seed()
    names = {e.name.lower() for e in svc.store.list_entities(SearchFilter(workspace_id=wid))}
    assert "redis" in names  # the regex graph extractor caught the bridging entity


def test_dry_run_proposes_without_writing():
    svc, wid = _seed()
    before = len(_live(svc, wid))
    rep = consolidate.infer_links(svc.engine, workspace_id=wid, dry_run=True)
    assert any(e["entity"].lower() == "redis" for e in rep["links_created"])
    assert all("would_link" in e for e in rep["links_created"])
    assert len(_live(svc, wid)) == before        # nothing written on a dry run


def test_apply_writes_low_salience_untrusted_linked_memory():
    svc, wid = _seed()
    rep = consolidate.infer_links(svc.engine, workspace_id=wid, dry_run=False)
    assert rep["links_created"]
    inferred = [m for m in _live(svc, wid)
                if m.metadata.get("provenance", {}).get("source") == "dream_inference"]
    assert len(inferred) == 1
    m = inferred[0]
    assert m.mtype == MemoryType.SEMANTIC
    assert m.provenance.get("trusted") is False          # never trusted
    assert m.importance <= 0.3                            # low salience
    assert m.provenance.get("links")                      # linked back to its sources
    # the inference is linked to real source memories via the inference relation
    linked = svc.store.get_links(m.id)
    assert any(link["relation"] == consolidate.INFER_RELATION for link in linked)


def test_apply_is_idempotent():
    svc, wid = _seed()
    consolidate.infer_links(svc.engine, workspace_id=wid, dry_run=False)
    rep2 = consolidate.infer_links(svc.engine, workspace_id=wid, dry_run=False)
    assert rep2["skipped_existing"] >= 1
    assert rep2["links_created"] == []                    # no duplicate inference


def test_similar_clusters_are_not_bridged():
    # One coherent subject → no cross-cluster inference to make.
    svc = MemoryService.create(":memory:")
    wid = svc.store.get_or_create_workspace("ws")
    for text in CACHING:
        svc.remember(text, workspace="ws", mtype="episodic", resolve_conflicts=False)
    rep = consolidate.infer_links(svc.engine, workspace_id=wid, dry_run=True)
    assert rep["links_created"] == []


def test_consolidate_pass4_runs_only_when_infer_true():
    svc, wid = _seed()
    off = svc.engine.consolidate(workspace_id=wid, dry_run=True)
    assert "inferences" not in off
    on = svc.engine.consolidate(workspace_id=wid, dry_run=True, infer=True)
    assert "inferences" in on
