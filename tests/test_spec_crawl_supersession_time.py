"""Effective supersession lineage without backdating recording-time knowledge."""

import pytest

from engraphis.core.interfaces import SearchFilter
from engraphis.service import MemoryService, ValidationError


@pytest.fixture
def temporal_service(monkeypatch):
    clock = {"now": 10_000.0}
    monkeypatch.setattr("engraphis.core.store.now_ts", lambda: clock["now"])
    monkeypatch.setattr("engraphis.core.engine.now_ts", lambda: clock["now"])
    svc = MemoryService.create(":memory:", extractor="none", graph_extractor="none")
    monkeypatch.setattr(svc.engine, "_evolve", lambda *args, **kwargs: None)
    try:
        yield svc, clock
    finally:
        svc.close()


def _pair(svc, effective=2000.0):
    old = svc.remember(
        "Historical deployment policy", workspace="alpha", trusted=True,
        valid_from=effective - 1000, resolve_conflicts=False,
    )["id"]
    current = svc.remember(
        "Current deployment policy", workspace="alpha", trusted=True,
        valid_from=effective, resolve_conflicts=False,
    )["id"]
    return old, current


def _supersede(svc, old, current):
    return svc.spec_crawl_resolve(
        action="supersede", node_a=current, node_b=old, workspace="alpha", confirmed=True,
    )


@pytest.mark.parametrize("effective", [2000.0, 0.0, -1000.0, 2000.25])
def test_backfilled_lineage_is_visible_between_effective_and_resolution_times(temporal_service, effective):
    svc, clock = temporal_service
    old, current = _pair(svc, effective)
    wid = svc.store.get_memory(current).workspace_id
    clock["now"] = 30_000.0
    _supersede(svc, old, current)
    after_world = effective + 100
    after_knowledge = SearchFilter(workspace_id=wid, as_of=after_world, known_at=clock["now"])
    assert svc.store.visible_memory_ids([old, current], after_knowledge) == {current}
    # The predecessor is already retired here: its explaining lineage must exist.
    links = svc.store.get_links(current, flt=after_knowledge)
    assert [(link["a"], link["b"], link["relation"]) for link in links] == [(current, old, "supersedes")]
    assert svc.store.links_among([old, current], flt=after_knowledge) == links
    before_knowledge = SearchFilter(workspace_id=wid, valid_at=after_world, known_at=clock["now"] - 1)
    assert svc.store.get_links(current, flt=before_knowledge) == []
    assert svc.store.links_among([old, current], flt=before_knowledge) == []
    assert svc.store.visible_memory_ids([old, current], before_knowledge) == {old, current}
    before_world = SearchFilter(workspace_id=wid, valid_at=effective - 1, known_at=clock["now"])
    assert svc.store.get_links(current, flt=before_world) == []
    assert svc.store.visible_memory_ids([old, current], before_world) == {old}


def test_link_world_time_is_backdated_but_recording_time_and_contents_are_preserved(temporal_service):
    svc, clock = temporal_service
    old, current = _pair(svc)
    originals = {mid: svc.store.get_memory(mid) for mid in (old, current)}
    clock["now"] = 30_000.0
    result = _supersede(svc, old, current)
    assert result["retained_node"] == current and result["superseded_node"] == old
    link = dict(svc.store.conn.execute(
        "SELECT * FROM mem_links WHERE a=? AND b=? AND relation='supersedes'", (current, old),
    ).fetchone())
    assert link["valid_from"] == originals[current].valid_from
    assert link["ingested_at"] == link["created_at"] == clock["now"]
    assert link["valid_to"] is link["valid_to_recorded_at"] is link["expired_at"] is None
    assert link["reason"] == "spec_crawl_resolve"
    for mid, original in originals.items():
        actual = svc.store.get_memory(mid)
        assert (actual.content, actual.valid_from, actual.ingested_at, actual.metadata) == (
            original.content, original.valid_from, original.ingested_at, original.metadata,
        )
    retired = svc.store.conn.execute("SELECT valid_to,valid_to_recorded_at FROM memories WHERE id=?", (old,)).fetchone()
    assert tuple(retired) == (2000.0, clock["now"])
    assert svc.store.get_memory(current).valid_to is None
    assert [entry["id"] for entry in svc.inspect(current, workspace="alpha")["chain"]] == [old, current]


@pytest.mark.parametrize("use_resolution", [True, False])
def test_ordinary_association_links_keep_resolution_time_defaults(temporal_service, use_resolution):
    svc, clock = temporal_service
    old, current = _pair(svc)
    clock["now"] = 30_000.0
    if use_resolution:
        svc.spec_crawl_resolve(action="link", node_a=current, node_b=old, workspace="alpha")
    else:
        svc.link(current, old, workspace="alpha", relation="related_to")
    link = svc.store.get_links(current)[0]
    assert link["valid_from"] == link["ingested_at"] == clock["now"]
    assert svc.store.get_memory(old).valid_to is None
    assert svc.store.get_links(current, flt=SearchFilter(valid_at=2500, known_at=clock["now"])) == []


def test_other_populated_link_versions_remain_unchanged(temporal_service):
    svc, clock = temporal_service
    old, current = _pair(svc)
    other = svc.remember("Unrelated policy", workspace="alpha", trusted=True, resolve_conflicts=False)["id"]
    svc.link(current, other, workspace="alpha", relation="related_to", reason="original")
    clock["now"] += 10
    svc.link(current, other, workspace="alpha", relation="related_to", reason="updated")
    def versions():
        return [tuple(row) for row in svc.store.conn.execute(
            "SELECT * FROM mem_links WHERE relation='related_to' ORDER BY rowid",
        )]
    before = versions()
    assert len(before) == 2
    clock["now"] = 30_000
    _supersede(svc, old, current)
    assert versions() == before
    assert svc.store.get_memory(other).valid_to is None


def test_reversed_transition_rolls_back_link_versions_and_receipts(temporal_service):
    svc, clock = temporal_service
    old, current = _pair(svc)
    before = svc.store.conn.execute("SELECT COUNT(*) FROM operation_receipts").fetchone()[0]
    clock["now"] = 30_000
    with pytest.raises(ValidationError, match="predate"):
        _supersede(svc, current, old)
    assert svc.store.conn.execute("SELECT COUNT(*) FROM mem_links").fetchone()[0] == 0
    assert svc.store.conn.execute("SELECT COUNT(*) FROM operation_receipts").fetchone()[0] == before
    assert svc.store.get_memory(current).valid_to is None
