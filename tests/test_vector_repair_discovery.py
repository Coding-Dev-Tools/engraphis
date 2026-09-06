"""Repair discovery must not reserve the writer for unrelated queued updates."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading

import numpy as np
import pytest

from engraphis.core.sync import SyncEngine
from engraphis.core.store import Store
from engraphis.core import vector_repair
from engraphis.factory import create_memory_engine
from tests.test_sync_index_repair import ExternalIndex, _bundle, _queue_blocked_upserts_and_cleanup


@pytest.fixture
def queued_index(tmp_path):
    engine = create_memory_engine(str(tmp_path / "discovery.db"), auto_evolve=False)
    index = ExternalIndex()
    sync = SyncEngine(engine.store, embedder=engine.embedder, vector_index=index)
    try:
        yield engine, index, sync
    finally:
        engine.close()


@pytest.mark.parametrize("cleanup", ["erase", "quarantine"])
@pytest.mark.parametrize("count", [105, 1000])
def test_cleanup_discovery_writer_cost_does_not_grow_with_upsert_backlog(
    queued_index, monkeypatch, cleanup, count,
):
    engine, index, sync = queued_index
    target, victim = _queue_blocked_upserts_and_cleanup(
        engine, index, sync, cleanup=cleanup, count=count,
    )
    connection = engine.store.conn
    execute = type(connection).execute
    writer_reservations = []

    def measured_execute(conn, sql, *args, **kwargs):
        if conn is connection and sql.strip().upper() == "BEGIN IMMEDIATE":
            writer_reservations.append(sql)
        return execute(conn, sql, *args, **kwargs)

    monkeypatch.setattr(type(connection), "execute", measured_execute)
    result = vector_repair.repair_vector_index(
        engine.store, index, embedding_space="unavailable-space", dim=0, limit=1,
    )
    assert result == {"attempted": 1, "repaired": 1, "pending": count}
    assert victim not in index.rows
    assert engine.store.vector_index_repair_generations(target, [victim]) == {}
    # Registration plus the actual publication. Inspecting skipped candidates must
    # not repeatedly contend with other processes for SQLite's single writer.
    assert len(writer_reservations) <= 1 + result["attempted"]


@pytest.mark.parametrize("delete_fails", [False, True])
def test_repair_stops_discovery_when_the_provider_budget_is_spent(
    queued_index, monkeypatch, delete_fails,
):
    engine, index, sync = queued_index
    count = 105
    target, victim = _queue_blocked_upserts_and_cleanup(
        engine, index, sync, cleanup="erase", count=count,
    )
    # Put newer updates after the pending erasure using real generation triggers.
    with engine.store.write_transaction():
        for mid, values in engine.store.get_vectors(
            [f"mem_sync_{number}" for number in range(count)],
        ).items():
            engine.store.put_vector(mid, values, model=engine.embedding_space)
    first = engine.store.conn.execute(
        "SELECT memory_id FROM vector_index_repairs WHERE identity=? "
        "ORDER BY generation,memory_id LIMIT 1", (target,),
    ).fetchone()
    assert first[0] == victim
    index.fail = delete_fails
    eligible = vector_repair.inspection_eligible
    inspected = []

    def measured_eligibility(provenance, metadata):
        inspected.append(True)
        return eligible(provenance, metadata)

    monkeypatch.setattr(vector_repair, "inspection_eligible", measured_eligibility)
    result = vector_repair.repair_vector_index(
        engine.store, index, embedding_space="unavailable-space", dim=0, limit=1,
    )
    assert result == {"attempted": 1, "repaired": int(not delete_fails),
                      "pending": count + int(delete_fails)}
    assert (victim in index.rows) == delete_fails
    assert bool(engine.store.vector_index_repair_generations(target, [victim])) == delete_fails
    # The erased first candidate requires no eligibility call. Do not resume the
    # filtered iterator and scan every later update just to discover the limit.
    assert inspected == []


def test_independent_writer_can_complete_while_discovery_is_paused(queued_index, monkeypatch):
    engine, index, sync = queued_index
    _queue_blocked_upserts_and_cleanup(engine, index, sync, cleanup="erase")
    other = create_memory_engine(engine.store.path, auto_evolve=False)
    workspace = other.store.get_or_create_workspace("other-project")
    entered, release = threading.Event(), threading.Event()
    eligible = vector_repair.inspection_eligible
    discovery_owned_writer = []

    def pause_discovery(provenance, metadata):
        if not entered.is_set():
            discovery_owned_writer.append(engine.store.conn.transaction_owned_by_current_thread())
            entered.set()
            assert release.wait(10), "discovery release timed out"
        return eligible(provenance, metadata)

    monkeypatch.setattr(vector_repair, "inspection_eligible", pause_discovery)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            repair = pool.submit(
                vector_repair.repair_vector_index, engine.store, index,
                embedding_space="unavailable-space", dim=0, limit=1,
            )
            try:
                assert entered.wait(10), "discovery did not reach an eligible update"
                write = pool.submit(
                    other.remember, "Independent project evidence.", workspace_id=workspace,
                )
                memory_id = write.result(timeout=3)
                assert other.store.get_memory(memory_id) is not None
            finally:
                release.set()
            assert repair.result(timeout=10)["repaired"] == 1
        assert discovery_owned_writer == [False]
    finally:
        release.set()
        other.close()


@pytest.mark.parametrize("transition", ["erase", "quarantine", "replace_vector", "restore_vector"])
def test_classification_hint_cannot_override_new_canonical_state(
    queued_index, monkeypatch, transition,
):
    engine, index, sync = queued_index
    assert sync.apply_bundle(_bundle())["added"] == 1
    mid = "mem_sync_0"
    target = vector_repair.index_repair_identity(index, engine.store)
    if transition == "restore_vector":
        engine.store.conn.execute("DELETE FROM mem_vectors WHERE id=?", (mid,))
        engine.store.conn.commit()
    else:
        engine.store.queue_vector_index_repairs(target, [mid])
    candidates = vector_repair._repair_candidates
    changed = []
    replacement = engine.embedder.embed(["Current canonical replacement."])[0]

    def change_after_classification(*args, **kwargs):
        for item in candidates(*args, **kwargs):
            if item[0] == mid and not changed:
                changed.append(True)
                if transition == "erase":
                    engine.store.conn.execute("DELETE FROM memories WHERE id=?", (mid,))
                    engine.store.conn.commit()
                elif transition == "quarantine":
                    # Deliberately keep the vector generation unchanged. Eligibility
                    # must also be revalidated under the writer, not just the queue ID.
                    engine.store.conn.execute(
                        "UPDATE memories SET metadata=? WHERE id=?",
                        (json.dumps({"quarantine": {"state": "quarantined"}}), mid),
                    )
                    engine.store.conn.commit()
                else:
                    engine.store.put_vector(mid, replacement, model=engine.embedding_space)
                    engine.store.conn.commit()
            yield item

    monkeypatch.setattr(vector_repair, "_repair_candidates", change_after_classification)
    before = len(index.published)
    vector_repair.repair_vector_index(
        engine.store, index, embedding_space=engine.embedding_space, dim=engine.embedder.dim,
    )
    assert changed == [True]
    assert len(index.published) == before
    pending = engine.store.vector_index_pending(target)
    assert pending == 1 or (transition == "quarantine" and pending == 0)
    monkeypatch.setattr(vector_repair, "_repair_candidates", candidates)
    result = vector_repair.repair_vector_index(
        engine.store, index, embedding_space=engine.embedding_space, dim=engine.embedder.dim,
    )
    assert result == {"attempted": pending, "repaired": pending, "pending": 0}
    if transition in {"erase", "quarantine"}:
        assert mid not in index.rows
    else:
        np.testing.assert_array_equal(index.rows[mid], engine.store.get_vectors([mid])[mid])


@pytest.mark.parametrize("column,raw,cleanup", [
    ("metadata", "not-json", False),
    ("metadata", "[]", False),
    ("metadata", '{"quarantine":{"state":"quarantined"}}', True),
    ("metadata", '{"provenance":{"quarantined":true}}', True),
    ("provenance", "not-json", False),
    ("provenance", "null", False),
    ("provenance", '{"quarantined":true}', True),
])
def test_discovery_uses_canonical_legacy_metadata_decoding(queued_index, column, raw, cleanup):
    engine, index, sync = queued_index
    sync.apply_bundle(_bundle())
    mid = "mem_sync_0"
    target = vector_repair.index_repair_identity(index, engine.store)
    # column is a closed, test-owned parameter set, never supplied by a request.
    engine.store.conn.execute(f"UPDATE memories SET {column}=? WHERE id=?", (raw, mid))
    engine.store.conn.commit()
    engine.store.queue_vector_index_repairs(target, [mid])
    result = vector_repair.repair_vector_index(
        engine.store, index, embedding_space=engine.embedding_space, dim=engine.embedder.dim,
    )
    assert result == {"attempted": 1, "repaired": 1, "pending": 0}
    assert (mid not in index.rows) == cleanup


@pytest.mark.parametrize("allowed", [{"sync-index"}, {"unrelated"}, {"sync-index", "unrelated"}])
@pytest.mark.parametrize("temporal_column", ["valid_from", "valid_to", "expired_at"])
def test_discovery_matches_bound_store_visibility_without_hiding_history(
    queued_index, allowed, temporal_column,
):
    engine, index, sync = queued_index
    sync.apply_bundle(_bundle())
    mid = "mem_sync_0"
    target = vector_repair.index_repair_identity(index, engine.store)
    # Historical canonical vectors remain indexable; only the instance binding
    # determines whether this target is allowed to retain the row.
    anchor = 4_000_000_000 if temporal_column == "valid_from" else 1
    engine.store.conn.execute(
        f"UPDATE memories SET {temporal_column}=? WHERE id=?", (anchor, mid),
    )
    engine.store.conn.commit()
    engine.store.queue_vector_index_repairs(target, [mid])
    bound = Store(engine.store.path, allowed_workspaces=allowed)
    try:
        visible = bound.get_memory(mid) is not None
        result = vector_repair.repair_vector_index(
            bound, index, embedding_space=engine.embedding_space, dim=engine.embedder.dim,
        )
        assert result == {"attempted": 1, "repaired": 1, "pending": 0}
        assert (mid in index.rows) == visible
        assert bound.vector_index_repair_generations(target, [mid]) == {}
        assert engine.store.get_memory(mid) is not None  # Cleanup never erases canonical data.
    finally:
        bound.close()


def test_bound_cleanup_preserves_allowed_work_until_embedding_recovers(queued_index):
    engine, index, sync = queued_index
    sync.apply_bundle(_bundle(count=2))
    allowed = engine.store.get_or_create_workspace("allowed")
    engine.store.conn.execute(
        "UPDATE memories SET workspace_id=? WHERE id=?", (allowed, "mem_sync_1"),
    )
    engine.store.conn.commit()
    target = vector_repair.index_repair_identity(index, engine.store)
    engine.store.queue_vector_index_repairs(target, ["mem_sync_0", "mem_sync_1"])
    bound = Store(engine.store.path, allowed_workspaces={"allowed"})
    try:
        result = vector_repair.repair_vector_index(
            bound, index, embedding_space="unavailable-space", dim=0, limit=1,
        )
        assert result == {"attempted": 1, "repaired": 1, "pending": 1}
        assert "mem_sync_0" not in index.rows and "mem_sync_1" in index.rows
        assert engine.store.get_memory("mem_sync_0") is not None
        assert "mem_sync_0" in engine.store.get_vectors(["mem_sync_0"])
        result = vector_repair.repair_vector_index(
            bound, index, embedding_space=engine.embedding_space, dim=engine.embedder.dim,
        )
        assert result == {"attempted": 1, "repaired": 1, "pending": 0}
        assert "mem_sync_0" not in index.rows and "mem_sync_1" in index.rows
    finally:
        bound.close()
