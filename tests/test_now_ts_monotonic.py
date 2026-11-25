"""Regression tests for coarse-clock supersession timestamp advancement."""
from __future__ import annotations

import time

import pytest

from engraphis.core import store as core_store
from engraphis.core.engine import MemoryEngine
from engraphis.core.interfaces import MemoryType, Scope


def test_next_wall_ts_after_advances_past_current_tick_without_exceeding_wall_clock():
    floor = core_store.now_ts()
    ts = core_store.next_wall_ts_after(floor)
    assert ts > floor
    assert ts <= time.time()


def test_next_wall_ts_after_does_not_sleep_for_past_future_or_mocked_clocks(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", sleeps.append)

    # Past floor returns immediately.
    now = core_store.now_ts()
    assert core_store.next_wall_ts_after(now - 5.0) >= now
    assert sleeps == []

    # Future floor beyond max_wait_s returns immediately so callers fail closed.
    future = now + 60.0
    assert core_store.next_wall_ts_after(future) < future
    assert sleeps == []

    # Monkeypatched time.time returns verbatim without sleeping.
    frozen = 1_700_000_000.0
    monkeypatch.setattr(time, "time", lambda: frozen)
    assert core_store.next_wall_ts_after(frozen) == frozen
    assert sleeps == []


def test_next_wall_ts_after_respects_monkeypatched_store_now_ts(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    monkeypatch.setattr(core_store, "now_ts", lambda: 500.0)
    assert core_store.next_wall_ts_after(500.0) == 500.0
    assert sleeps == []


def test_rapid_superseding_writes_succeed_on_coarse_clock_and_stay_visible_to_wall_time():
    eng = MemoryEngine.create(":memory:", embed_model="")
    try:
        wid = eng.store.get_or_create_workspace("acme")
        rid = eng.store.get_or_create_repo(wid, "api")
        first = eng.remember_with_resolution(
            "Auth tokens use JWT signing.",
            workspace_id=wid,
            repo_id=rid,
            scope=Scope.REPO,
            mtype=MemoryType.SEMANTIC,
            subject_key="auth.token_format",
            claim_kind="decision",
        )
        second = eng.remember_with_resolution(
            "Auth tokens migrated from JWT to PASETO v4.",
            workspace_id=wid,
            repo_id=rid,
            scope=Scope.REPO,
            mtype=MemoryType.SEMANTIC,
            subject_key="auth.token_format",
            claim_kind="decision",
        )
        after = time.time()
        assert second["op"] == "invalidate"
        old_rec = eng.store.get_memory(first["id"])
        new_rec = eng.store.get_memory(second["id"])
        assert old_rec is not None and new_rec is not None
        assert old_rec.valid_to is not None
        assert old_rec.valid_from is not None
        assert new_rec.valid_from is not None
        assert new_rec.ingested_at is not None
        assert old_rec.valid_to > old_rec.valid_from
        assert old_rec.valid_to == new_rec.valid_from
        assert new_rec.valid_from <= after
        assert new_rec.ingested_at <= after

        # Explicit valid_from colliding with predecessor still fails closed.
        with pytest.raises(ValueError, match="valid_to cannot predate valid_from"):
            eng.remember_with_resolution(
                "Auth tokens switched to opaque tokens.",
                workspace_id=wid,
                repo_id=rid,
                scope=Scope.REPO,
                mtype=MemoryType.SEMANTIC,
                subject_key="auth.token_format",
                claim_kind="decision",
                valid_from=new_rec.valid_from,
            )
    finally:
        eng.close()
