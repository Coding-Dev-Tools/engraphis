"""Campaign identity and declared history validation regressions."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from eval import campaign_adapters as adapters


class FakeMem0:
    def __init__(self) -> None:
        self.add_calls = []

    def add(self, messages, **kwargs):
        self.add_calls.append((messages, kwargs))
        return {"results": [{"id": f"mem-{len(self.add_calls)}"}]}


class FakeGraphiti:
    def __init__(self) -> None:
        self.add_calls = []

    async def build_indices_and_constraints(self):
        return None

    async def add_episode(self, **kwargs):
        self.add_calls.append(kwargs)
        return SimpleNamespace(
            episode=SimpleNamespace(uuid=f"episode-{len(self.add_calls)}")
        )


def _engraphis(tmp_path, name="campaign.db"):
    adapter = adapters.EngraphisAdapter(
        db_path=str(tmp_path / name),
        engine_kwargs={
            "fixture_clock": {
                "enabled": True,
                "anchor": 2_000_000_000.0,
                "logical_origin": 0.0,
            }
        },
    )
    adapter.prepare(workspace_id="workspace-a", repo_id="repo-a")
    return adapter


def _record(record_id, content="fact", **extra):
    return {
        "record_id": record_id,
        "content": content,
        "scope": "workspace",
        "workspace": "workspace-a",
        **extra,
    }


@pytest.mark.parametrize("kind", ["engraphis", "mem0", "graphiti"])
def test_duplicate_non_invalidation_ids_are_rejected_before_any_backend_write(
    tmp_path, kind,
):
    if kind == "engraphis":
        adapter = _engraphis(tmp_path, "duplicate-engraphis.db")
        calls = []
        original = adapter.engine.remember_with_resolution

        def remember(*args, **kwargs):
            calls.append((args, kwargs))
            return original(*args, **kwargs)

        adapter.engine.remember_with_resolution = remember
        try:
            with pytest.raises(ValueError, match="duplicate campaign record id"):
                adapter.ingest([_record("same", "one"), _record("same", "two")])
            assert calls == []
            assert adapter._memory_ids == {}
        finally:
            adapter.close()
        return

    client = FakeMem0() if kind == "mem0" else FakeGraphiti()
    adapter = (
        adapters.Mem0Adapter(client=client)
        if kind == "mem0"
        else adapters.GraphitiAdapter(client=client)
    )
    adapter.prepare(workspace_id="workspace-a")
    try:
        with pytest.raises(ValueError, match="duplicate campaign record id"):
            adapter.ingest([_record("same", "one"), _record("same", "two")])
        assert client.add_calls == []
        assert adapter._memory_ids == {}
    finally:
        adapter.close()


def test_existing_non_invalidation_id_cannot_be_overwritten(tmp_path):
    adapter = _engraphis(tmp_path, "duplicate-existing.db")
    try:
        adapter.ingest([_record("same", "original", valid_at=10.0)])
        with pytest.raises(ValueError, match="duplicate campaign record id"):
            adapter.ingest([_record("same", "replacement", valid_at=20.0)])
        assert adapter.engine.store.conn.execute(
            "SELECT COUNT(*) FROM memories"
        ).fetchone()[0] == 1
    finally:
        adapter.close()


def test_repeated_invalidation_reuses_id_and_keeps_earliest_close(tmp_path):
    adapter = _engraphis(tmp_path, "idempotent-invalidation.db")
    try:
        memory_id = adapter.ingest([_record("old", valid_at=10.0)])[0]
        assert adapter.ingest([_record("old", content="", operation="invalidate", valid_at=20.0)]) == [memory_id]
        assert adapter.ingest([_record("old", content="", operation="invalidate", valid_at=30.0)]) == [memory_id]
        assert adapter.engine.store.get_memory(memory_id).valid_to == 2_000_000_020.0
        assert adapter.engine.store.conn.execute(
            "SELECT COUNT(*) FROM memories"
        ).fetchone()[0] == 1
    finally:
        adapter.close()


def test_same_batch_remember_and_repeated_invalidation_reuses_declared_id(tmp_path):
    adapter = _engraphis(tmp_path, "batch-invalidation.db")
    try:
        ids = adapter.ingest([
            _record("old", content="old", timestamp=10.0, valid_at=10.0),
            _record("old", content="", operation="invalidate", timestamp=20.0, valid_at=20.0),
            _record("old", content="", operation="invalidate", timestamp=30.0, valid_at=30.0),
        ])
        assert ids == [ids[0], ids[0], ids[0]]
        assert adapter.engine.store.get_memory(ids[0]).valid_to == 2_000_000_020.0
    finally:
        adapter.close()


def test_backdated_correction_is_rejected_before_successor_write(tmp_path):
    adapter = _engraphis(tmp_path, "backdated.db")
    try:
        old_id = adapter.ingest([_record("old", valid_at=10.0)])[0]
        with pytest.raises(adapters.AdapterError, match="cannot predate target valid_at"):
            adapter.ingest([_record(
                "new", "replacement", operation="correct", corrects="old", valid_at=5.0,
            )])
        assert adapter._memory_ids == {"old": old_id}
        assert adapter.engine.store.get_memory(old_id).valid_to is None
        assert adapter.engine.store.conn.execute(
            "SELECT COUNT(*) FROM memories"
        ).fetchone()[0] == 1
    finally:
        adapter.close()


def test_correction_can_close_a_predecessor_before_its_declared_future_valid_to(tmp_path):
    adapter = _engraphis(tmp_path, "future-close.db")
    try:
        ids = adapter.ingest([
            _record("old", "old", timestamp=10.0, valid_at=10.0, valid_to=100.0),
            _record(
                "new", "new", operation="correct", corrects="old",
                timestamp=20.0, valid_at=20.0,
            ),
        ])
        assert len(ids) == 2
        assert adapter.engine.store.get_memory(ids[0]).valid_to == 2_000_000_020.0
        assert adapter.engine.store.get_memory(ids[1]).valid_to is None
    finally:
        adapter.close()


def test_omitted_valid_at_with_past_valid_to_fails_before_memory_write(tmp_path):
    adapter = _engraphis(tmp_path, "omitted-valid-at.db")
    try:
        calls = []
        original = adapter.engine.remember_with_resolution

        def remember(*args, **kwargs):
            calls.append((args, kwargs))
            return original(*args, **kwargs)

        adapter.engine.remember_with_resolution = remember
        with pytest.raises(ValueError, match="effective valid_at"):
            adapter.ingest([_record("bad", "bad", valid_to=-1.0)])
        assert calls == []
        assert adapter.engine.store.conn.execute(
            "SELECT COUNT(*) FROM memories"
        ).fetchone()[0] == 0
    finally:
        adapter.close()


def test_invalidation_valid_to_is_ignored_by_plan_and_execution(tmp_path):
    adapter = _engraphis(tmp_path, "ignored-invalidation-valid-to.db")
    try:
        old_id = adapter.ingest([_record("old", valid_at=10.0)])[0]
        new_id = adapter.ingest([
            _record(
                "old", content="", operation="invalidate", timestamp=10.0,
                valid_at=30.0, valid_to=5.0,
            ),
            _record(
                "new", content="replacement", operation="correct", corrects="old",
                timestamp=20.0, valid_at=20.0,
            ),
        ])[1]
        assert adapter.engine.store.get_memory(old_id).valid_to == 2_000_000_020.0
        assert adapter.engine.store.get_memory(new_id).valid_to is None
    finally:
        adapter.close()


def test_default_correction_time_can_precede_a_future_valid_to(tmp_path):
    adapter = _engraphis(tmp_path, "default-time-future-close.db")
    try:
        old_id = adapter.ingest([_record("old", valid_at=10.0, valid_to=100.0)])[0]
        new_id = adapter.ingest([_record(
            "new", content="default-time replacement", operation="correct", corrects="old",
        )])[0]
        assert adapter.engine.store.get_memory(old_id).valid_to == 2_000_000_010.0
        assert adapter.engine.store.get_memory(new_id).valid_to is None
    finally:
        adapter.close()


@pytest.mark.parametrize("timestamps", [
    {"valid_at": 1e308},
    {"valid_at": 0.0, "known_at": 1e308},
])
def test_fixture_timestamp_mapping_overflow_fails_before_engine_write(tmp_path, timestamps):
    adapter = adapters.EngraphisAdapter(
        db_path=str(tmp_path / "mapped-overflow.db"),
        engine_kwargs={
            "fixture_clock": {
                "enabled": True,
                "anchor": 1e308,
                "logical_origin": 0.0,
            }
        },
    )
    adapter.prepare(workspace_id="workspace-a")
    try:
        calls = []
        original = adapter.engine.remember_with_resolution

        def remember(*args, **kwargs):
            calls.append((args, kwargs))
            return original(*args, **kwargs)

        adapter.engine.remember_with_resolution = remember
        with pytest.raises(ValueError, match="effective"):
            adapter.ingest([_record("huge", **timestamps)])
        assert calls == []
        assert adapter.engine.store.conn.execute(
            "SELECT COUNT(*) FROM memories"
        ).fetchone()[0] == 0
    finally:
        adapter.close()


def test_closed_predecessor_cannot_receive_second_successor(tmp_path):
    adapter = _engraphis(tmp_path, "closed-predecessor.db")
    try:
        old_id = adapter.ingest([_record("old", valid_at=10.0)])[0]
        first_id = adapter.ingest([_record(
            "new-1", "first replacement", operation="correct", corrects="old", valid_at=20.0,
        )])[0]
        with pytest.raises(adapters.AdapterError, match="already closed"):
            adapter.ingest([_record(
                "new-2", "second replacement", operation="correct", corrects="old", valid_at=30.0,
            )])
        assert adapter._memory_ids == {"old": old_id, "new-1": first_id}
        assert adapter.engine.store.conn.execute(
            "SELECT COUNT(*) FROM memories"
        ).fetchone()[0] == 2
        assert adapter.engine.store.get_memory(old_id).valid_to == 2_000_000_020.0
    finally:
        adapter.close()


def test_equal_correction_time_is_allowed_but_backdating_is_not(tmp_path):
    adapter = _engraphis(tmp_path, "equal-correction-time.db")
    try:
        old_id = adapter.ingest([_record("old", valid_at=10.0)])[0]
        new_id = adapter.ingest([_record(
            "new", "equal-time replacement", operation="correct",
            corrects="old", valid_at=10.0,
        )])[0]
        assert adapter.engine.store.get_memory(old_id).valid_to == 2_000_000_010.0
        assert adapter.engine.store.get_memory(new_id).valid_to is None
    finally:
        adapter.close()


@pytest.mark.parametrize(
    "bad_record",
    [
        _record("bad", "bad", valid_at=20.0, valid_to=10.0),
        _record("missing-correction", "bad", operation="correct", corrects="future", valid_at=20.0),
    ],
)
def test_declared_batch_validation_happens_before_first_memory_write(tmp_path, bad_record):
    adapter = _engraphis(tmp_path, "batch-preflight.db")
    try:
        good = _record("good", "kept only if batch succeeds", timestamp=10.0, valid_at=10.0)
        bad = dict(bad_record, timestamp=20.0)
        with pytest.raises((ValueError, adapters.AdapterError)):
            adapter.ingest([good, bad])
        assert adapter._memory_ids == {}
        assert adapter.engine.store.conn.execute(
            "SELECT COUNT(*) FROM memories"
        ).fetchone()[0] == 0
    finally:
        adapter.close()


def test_declared_batch_correction_uses_earlier_record_without_future_backend_id(tmp_path):
    adapter = _engraphis(tmp_path, "batch-correction.db")
    try:
        ids = adapter.ingest([
            _record("old", "old", timestamp=10.0, valid_at=10.0),
            _record(
                "new", "new", operation="correct", corrects="old",
                timestamp=20.0, valid_at=20.0,
            ),
        ])
        assert len(ids) == 2
        assert adapter.engine.store.get_memory(ids[0]).valid_to == 2_000_000_020.0
        assert adapter.engine.store.get_memory(ids[1]).valid_to is None
    finally:
        adapter.close()


def test_declared_batch_scope_mismatch_is_rejected_before_first_memory_write(tmp_path):
    adapter = _engraphis(tmp_path, "batch-scope.db")
    try:
        with pytest.raises(adapters.AdapterError, match="scope boundary"):
            adapter.ingest([
                _record("old", "old", timestamp=10.0, valid_at=10.0),
                {
                    "record_id": "new",
                    "content": "new",
                    "operation": "correct",
                    "corrects": "old",
                    "timestamp": 20.0,
                    "valid_at": 20.0,
                    "scope": "workspace",
                    "workspace": "workspace-b",
                },
            ])
        assert adapter._memory_ids == {}
        assert adapter.engine.store.conn.execute(
            "SELECT COUNT(*) FROM memories"
        ).fetchone()[0] == 0
    finally:
        adapter.close()


def test_future_declared_target_is_missing_without_partial_write(tmp_path):
    adapter = _engraphis(tmp_path, "future-target.db")
    try:
        with pytest.raises(adapters.AdapterError, match="target is missing"):
            adapter.ingest([
                _record(
                    "new", "new", operation="correct", corrects="future",
                    timestamp=10.0, valid_at=10.0,
                ),
                _record("future", "future", timestamp=20.0, valid_at=20.0),
            ])
        assert adapter._memory_ids == {}
        assert adapter.engine.store.conn.execute(
            "SELECT COUNT(*) FROM memories"
        ).fetchone()[0] == 0
    finally:
        adapter.close()


@pytest.mark.parametrize("scope", ["repo", "session"])
def test_missing_required_parent_fails_before_first_memory_write(tmp_path, scope):
    adapter = adapters.EngraphisAdapter(db_path=str(tmp_path / "missing-parent.db"))
    adapter.prepare(workspace_id="workspace-a")
    try:
        with pytest.raises(adapters.AdapterConfigurationError, match="scope requires"):
            adapter.ingest([
                _record("good", content="valid workspace fact"),
                _record("bad", content="missing required parent", scope=scope),
            ])
        assert adapter._memory_ids == {}
        assert adapter.engine.store.conn.execute(
            "SELECT COUNT(*) FROM memories"
        ).fetchone()[0] == 0
    finally:
        adapter.close()
