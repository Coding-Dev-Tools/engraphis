"""Release contract: proactive agenda diverges from ordinary query recall.

* ``score_memory`` carries no recency term while ``score_proactive`` does, so
  two records that score equally as query candidates order strictly by
  freshness on the proactive agenda.
* ``score_memory`` honors ``known_at`` staleness: a retroactive closure that
  postdates the requested system time contributes no penalty.
* End to end, ``grounded_recall(as_of=...)`` time-travels past a supersession
  while the live view grounds on the replacement.
"""
from engraphis.core import scoring
from engraphis.core.engine import MemoryEngine
from engraphis.core.interfaces import MemoryRecord, MemoryType

NOW = 1_700_000_000.0
DAY = 86400.0


def _record(**overrides):
    base = {
        "id": "mem", "content": "The API rate limit is 100 requests per minute.",
        "mtype": MemoryType.SEMANTIC, "stability": 1.0,
        "last_access": NOW, "ingested_at": NOW, "valid_from": NOW,
        "importance": 0.5,
    }
    base.update(overrides)
    return MemoryRecord(**base)


def test_equal_query_score_orders_proactive_by_recency():
    weights = scoring.weights_for(MemoryType.SEMANTIC)
    old = _record(id="old", ingested_at=NOW - 7 * DAY, valid_from=NOW - 7 * DAY)
    fresh = _record(id="fresh", ingested_at=NOW, valid_from=NOW)

    assert scoring.score_memory(old, now=NOW, weights=weights) == scoring.score_memory(
        fresh, now=NOW, weights=weights
    )
    assert scoring.score_proactive(fresh, now=NOW) > scoring.score_proactive(old, now=NOW)


def test_score_memory_ignores_closure_predating_known_at():
    weights = scoring.weights_for(MemoryType.SEMANTIC)
    closed = _record(
        valid_to=NOW - DAY, valid_to_recorded_at=NOW - DAY,
    )
    penalized = scoring.score_memory(closed, now=NOW, weights=weights)
    unpenalized = scoring.score_memory(
        closed, now=NOW, weights=weights, known_at=NOW - 2 * DAY,
    )
    assert unpenalized > penalized


def test_grounded_recall_as_of_sees_superseded_fact(monkeypatch):
    # Freeze the engine clock: the supersession chain must order purely by the
    # recorded anchors, never by ambient wall-time skew between two writes.
    frozen = [1_000.0]
    monkeypatch.setattr("engraphis.core.store.now_ts", lambda: frozen[0])
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    old_id = eng.remember(
        "Until 2026-01 the rate limit was 100 requests per minute per API key.",
        workspace_id=wid, repo_id=rid,
    )
    frozen[0] = 2_000.0
    new_id = eng.remember(
        "As of 2026-02 the rate limit was raised to 500 requests per minute per API key.",
        workspace_id=wid, repo_id=rid,
    )
    assert old_id != new_id
    as_of = eng.store.get_memory(old_id).valid_from
    assert as_of == 1_000.0

    live = eng.grounded_recall(
        "what is the API rate limit per key?", workspace_id=wid, repo_id=rid,
    )
    historical = eng.grounded_recall(
        "what is the API rate limit per key?", workspace_id=wid, repo_id=rid,
        as_of=as_of,
    )

    assert live.grounded and not live.abstained
    assert "500" in live.answer
    assert historical.grounded and not historical.abstained
    assert "100" in historical.answer
    assert {cite["id"] for cite in historical.citations} == {old_id}


def test_proactive_agenda_prefers_fresh_evidence_over_stale_importance():
    from engraphis.core.store import now_ts

    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("acme")
    now = now_ts()
    old_important = eng.store.add_memory(MemoryRecord(
        id="", content="Production deploys require an approval.",
        workspace_id=wid, mtype=MemoryType.SEMANTIC,
        importance=0.9, stability=1.0,
        ingested_at=now - 7 * DAY, last_access=now - 7 * DAY,
    ))
    fresh = eng.store.add_memory(MemoryRecord(
        id="", content="Temporary scratch note.", workspace_id=wid,
        mtype=MemoryType.SEMANTIC, importance=0.0, stability=1.0,
        ingested_at=now, last_access=now,
    ))

    agenda = eng.recall_proactive(workspace_id=wid, k=2)
    assert [memory.id for memory in agenda["memories"]][0] == fresh
    assert old_important not in [memory.id for memory in agenda["memories"]][:1]
