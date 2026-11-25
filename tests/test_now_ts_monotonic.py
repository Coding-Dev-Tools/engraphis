"""Regression tests for coarse-clock supersession and closure timestamp advancement."""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from engraphis.core import store as core_store
from engraphis.core.engine import FactSpec, MemoryEngine
from engraphis.core.interfaces import MemoryType, Scope


def test_next_wall_ts_after_advances_past_current_tick_without_exceeding_wall_clock():
    floor = core_store.now_ts()
    ts = core_store.next_wall_ts_after(floor)
    assert ts > floor
    assert ts <= time.time()


def test_next_wall_ts_after_wait_loop_advances_deterministically(monkeypatch):
    """Deterministically exercise the poll/sleep loop on every OS (including Linux/macOS)."""
    ticks = iter([1000.0, 1000.0, 1000.0, 1000.015625])
    mono = iter([0.0, 0.001, 0.002, 0.003])
    sleeps: list[float] = []

    def _fake_now() -> float:
        return next(ticks)

    monkeypatch.setattr(core_store, "now_ts", _fake_now)
    monkeypatch.setattr(core_store, "_DEFAULT_NOW_TS", _fake_now)
    monkeypatch.setattr(time, "monotonic", lambda: next(mono))
    monkeypatch.setattr(time, "sleep", sleeps.append)

    result = core_store.next_wall_ts_after(1000.0, max_wait_s=0.05)
    assert result == 1000.015625
    assert sleeps == [0.001, 0.001, 0.001]


def test_next_wall_ts_after_respects_max_wait_deadline_and_final_recheck(monkeypatch):
    """Bounded deadline terminates sleep loop and performs a final now_ts() check."""
    calls = 0

    def _stalled_now() -> float:
        nonlocal calls
        calls += 1
        return 1000.0

    mono_val = {"t": 0.0}
    sleeps: list[float] = []

    def _fake_sleep(dt: float) -> None:
        sleeps.append(dt)
        mono_val["t"] += 0.02

    monkeypatch.setattr(core_store, "now_ts", _stalled_now)
    monkeypatch.setattr(core_store, "_DEFAULT_NOW_TS", _stalled_now)
    monkeypatch.setattr(time, "monotonic", lambda: mono_val["t"])
    monkeypatch.setattr(time, "sleep", _fake_sleep)

    result = core_store.next_wall_ts_after(1000.0, max_wait_s=0.05)
    assert result == 1000.0
    assert len(sleeps) == 3
    assert calls >= 5


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


def test_rapid_superseding_writes_align_creation_timestamps_and_as_of_recall():
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
        assert new_rec.last_access is not None
        assert old_rec.valid_to > old_rec.valid_from
        assert old_rec.valid_to == new_rec.valid_from
        # Single-instant creation invariant: as_of=new_rec.valid_from must see new_rec.
        assert new_rec.valid_from == new_rec.ingested_at == new_rec.last_access
        assert new_rec.valid_from <= after

        current_hits = eng.recall(
            "auth token format",
            workspace_id=wid,
            repo_id=rid,
        )
        assert [c["id"] for c in current_hits.chunks] == [second["id"]]

        exact_anchor_hits = eng.recall(
            "auth token format",
            workspace_id=wid,
            repo_id=rid,
            valid_at=new_rec.valid_from,
            known_at=new_rec.valid_from,
        )
        assert second["id"] in [c["id"] for c in exact_anchor_hits.chunks]

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


def test_frozen_store_now_ts_fails_closed_end_to_end(monkeypatch):
    eng = MemoryEngine.create(":memory:", embed_model="")
    try:
        wid = eng.store.get_or_create_workspace("acme")
        rid = eng.store.get_or_create_repo(wid, "api")

        sleeps: list[float] = []
        monkeypatch.setattr(time, "sleep", sleeps.append)
        monkeypatch.setattr(core_store, "now_ts", lambda: 500.0)
        eng.remember_with_resolution(
            "Cache backend is memcached.",
            workspace_id=wid,
            repo_id=rid,
            scope=Scope.REPO,
            mtype=MemoryType.SEMANTIC,
            subject_key="cache.backend",
            claim_kind="decision",
        )
        with pytest.raises(ValueError, match="valid_to cannot predate valid_from"):
            eng.remember_with_resolution(
                "Cache backend is redis.",
                workspace_id=wid,
                repo_id=rid,
                scope=Scope.REPO,
                mtype=MemoryType.SEMANTIC,
                subject_key="cache.backend",
                claim_kind="decision",
            )
        assert sleeps == []
    finally:
        eng.close()


def test_remember_many_and_governance_closures_never_persist_empty_intervals():
    eng = MemoryEngine.create(":memory:", embed_model="")
    try:
        wid = eng.store.get_or_create_workspace("acme")
        rid = eng.store.get_or_create_repo(wid, "api")

        # Within-batch same-key supersession in remember_many().
        batch = eng.remember_many(
            [
                FactSpec(
                    content="Queue broker is RabbitMQ.",
                    mtype=MemoryType.SEMANTIC,
                    subject_key="queue.broker",
                    claim_kind="decision",
                ),
                FactSpec(
                    content="Queue broker migrated to NATS JetStream.",
                    mtype=MemoryType.SEMANTIC,
                    subject_key="queue.broker",
                    claim_kind="decision",
                ),
            ],
            workspace_id=wid,
            repo_id=rid,
            scope=Scope.REPO,
        )
        assert batch[1]["op"] == "invalidate"
        old_b = eng.store.get_memory(batch[0]["id"])
        assert old_b is not None and old_b.valid_from is not None and old_b.valid_to is not None
        assert old_b.valid_to > old_b.valid_from

        # Immediate correct() and auto close_validity(at=None) produce non-empty intervals.
        corrected = eng.correct(
            batch[1]["id"],
            "Queue broker uses NATS JetStream v2.",
            reason="version clarification",
        )
        prev_corr = eng.store.get_memory(batch[1]["id"])
        assert prev_corr is not None and prev_corr.valid_from is not None and prev_corr.valid_to is not None
        assert prev_corr.valid_to > prev_corr.valid_from

        eng.store.close_validity(corrected["id"], reason="retired")
        closed = eng.store.get_memory(corrected["id"])
        assert closed is not None and closed.valid_from is not None and closed.valid_to is not None
        assert closed.valid_to > closed.valid_from
    finally:
        eng.close()


def test_concurrent_superseding_writes_preserve_valid_non_empty_chain():
    eng = MemoryEngine.create(":memory:", embed_model="")
    try:
        wid = eng.store.get_or_create_workspace("acme")
        rid = eng.store.get_or_create_repo(wid, "api")
        eng.remember_with_resolution(
            "Timeout is 10s.",
            workspace_id=wid,
            repo_id=rid,
            scope=Scope.REPO,
            mtype=MemoryType.SEMANTIC,
            subject_key="http.timeout",
            claim_kind="config",
        )

        def _write(i: int) -> dict:
            return eng.remember_with_resolution(
                f"Timeout is {20 + i}s.",
                workspace_id=wid,
                repo_id=rid,
                scope=Scope.REPO,
                mtype=MemoryType.SEMANTIC,
                subject_key="http.timeout",
                claim_kind="config",
            )

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(_write, range(6)))

        assert len(results) == 6
        rows = eng.store.conn.execute(
            "SELECT valid_from, valid_to FROM memories WHERE workspace_id=? AND subject_key=?",
            (wid, "http.timeout"),
        ).fetchall()
        live = [r for r in rows if r["valid_to"] is None]
        closed = [r for r in rows if r["valid_to"] is not None]
        assert len(live) == 1
        assert len(closed) == 6
        assert all(r["valid_to"] > r["valid_from"] for r in closed)
    finally:
        eng.close()
