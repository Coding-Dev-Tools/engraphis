"""Release contract: write-path end-to-end through ``MemoryEngine.remember``.

``remember`` returns only the resulting id, so every assertion below checks the
store state directly: which ids are live, which validity intervals were closed,
and how many records exist.
"""
from engraphis.core.engine import MemoryEngine
from engraphis.core.interfaces import MemoryType, SearchFilter


def _engine():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    return eng, wid, rid


def _live_ids(eng, wid, rid):
    return {
        record.id
        for record in eng.store.list_memories(SearchFilter(workspace_id=wid, repo_id=rid))
    }


def test_temporal_update_invalidates_superseded_fact():
    eng, wid, rid = _engine()
    old_id = eng.remember(
        "Until 2026-01 the rate limit was 100 requests per minute per API key.",
        workspace_id=wid, repo_id=rid,
    )
    new_id = eng.remember(
        "As of 2026-02 the rate limit was raised to 500 requests per minute per API key.",
        workspace_id=wid, repo_id=rid,
    )

    assert new_id != old_id
    assert eng.store.get_memory(old_id).valid_to is not None
    assert eng.store.get_memory(new_id).valid_to is None
    live = _live_ids(eng, wid, rid)
    assert old_id not in live and new_id in live


def test_exact_restatement_noops_to_a_single_id():
    eng, wid, rid = _engine()
    text = "We standardized on pnpm as the package manager for all frontend repositories."
    first_id = eng.remember(text, workspace_id=wid, repo_id=rid)
    second_id = eng.remember(text, workspace_id=wid, repo_id=rid)

    assert second_id == first_id
    assert _live_ids(eng, wid, rid) == {first_id}
    assert eng.store.get_memory(first_id).valid_to is None


def test_cause_and_fix_are_both_kept():
    eng, wid, rid = _engine()
    cause_id = eng.remember(
        "The bug in checkout was caused by a race condition in the inventory service.",
        workspace_id=wid, repo_id=rid, mtype=MemoryType.EPISODIC,
    )
    fix_id = eng.remember(
        "We fixed the checkout race condition by adding a Redis lock around the stock decrement.",
        workspace_id=wid, repo_id=rid, mtype=MemoryType.EPISODIC,
    )

    assert fix_id != cause_id
    assert _live_ids(eng, wid, rid) == {cause_id, fix_id}
    assert eng.store.get_memory(cause_id).valid_to is None
    assert eng.store.get_memory(fix_id).valid_to is None


def test_keyed_number_change_invalidates_predecessor():
    eng, wid, _rid = _engine()
    key = {"subject_key": "api.rate_limit", "claim_kind": "configured_value"}
    old_id = eng.remember(
        "The API rate limit is 100 requests per minute per API key.",
        workspace_id=wid, **key,
    )
    new_id = eng.remember(
        "The API rate limit is now 500 requests per minute per API key.",
        workspace_id=wid, **key,
    )

    assert new_id != old_id
    assert eng.store.get_memory(old_id).valid_to is not None
    assert eng.store.get_memory(new_id).valid_to is None
    live = {
        record.id
        for record in eng.store.list_memories(SearchFilter(workspace_id=wid))
    }
    assert old_id not in live and new_id in live


def test_remember_with_resolution_provides_superseded_detail():
    eng, wid, _rid = _engine()
    key = {"subject_key": "api.rate_limit", "claim_kind": "configured_value"}
    old_id = eng.remember(
        "The API rate limit is 100 requests per minute per API key.",
        workspace_id=wid, **key,
    )
    eng.store.reinforce(old_id)

    res = eng.remember_with_resolution(
        "The API rate limit is now 500 requests per minute per API key.",
        workspace_id=wid, **key,
    )
    assert res["op"] == "invalidate"
    assert res["superseded"] == [old_id]
    assert "superseded_detail" in res
    detail = res["superseded_detail"]
    assert detail["id"] == old_id
    assert "100 requests" in detail["content_preview"]
    assert detail["access_count"] >= 1
    assert detail["stability_days"] > 0

