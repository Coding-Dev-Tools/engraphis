"""Campaign memory creation and validity closures share the engine transaction."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest

from eval import campaign_adapters as adapters


def _adapter(tmp_path: Path, *, name: str = "campaign.db") -> Any:
    adapter = adapters.EngraphisAdapter(db_path=str(tmp_path / name))
    adapter.prepare(workspace_id="workspace-a", repo_id="repo-a")
    return adapter


def _record(record_id: str, content: str = "fact", **extra: Any) -> dict[str, Any]:
    return {
        "record_id": record_id,
        "content": content,
        "scope": "workspace",
        "workspace": "workspace-a",
        **extra,
    }


def _memory_count(adapter: Any) -> int:
    return int(
        adapter.engine.store.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    )


def test_correction_close_failure_rolls_back_successor_and_mapping(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, name="correction-close-failure.db")
    try:
        old_id = adapter.ingest([_record("old", "old", valid_at=10.0)])[0]
        store = adapter.engine.store
        original_close = store.close_validity

        def fail_predecessor(memory_id: str, *args: Any, **kwargs: Any) -> None:
            if memory_id == old_id:
                raise RuntimeError("injected predecessor close failure")
            original_close(memory_id, *args, **kwargs)

        monkeypatch.setattr(store, "close_validity", fail_predecessor)
        with pytest.raises(RuntimeError, match="injected predecessor"):
            adapter.ingest([_record(
                "new", "replacement", operation="correct", corrects="old",
                valid_at=20.0,
            )])

        assert adapter._memory_ids == {"old": old_id}
        assert _memory_count(adapter) == 1
        assert store.get_memory(old_id).valid_to is None
    finally:
        adapter.close()


def test_correction_validity_boundary_failure_rolls_back_predecessor_close(
    tmp_path, monkeypatch,
):
    adapter = _adapter(tmp_path, name="correction-boundary-failure.db")
    try:
        old_id = adapter.ingest([_record("old", "old", valid_at=10.0)])[0]
        store = adapter.engine.store
        original_close = store.close_validity

        def fail_successor(memory_id: str, *args: Any, **kwargs: Any) -> None:
            if memory_id != old_id:
                raise RuntimeError("injected successor close failure")
            original_close(memory_id, *args, **kwargs)

        monkeypatch.setattr(store, "close_validity", fail_successor)
        with pytest.raises(RuntimeError, match="injected successor"):
            adapter.ingest([_record(
                "new", "replacement", operation="correct", corrects="old",
                valid_at=20.0, valid_to=30.0,
            )])

        assert adapter._memory_ids == {"old": old_id}
        assert _memory_count(adapter) == 1
        assert store.get_memory(old_id).valid_to is None
    finally:
        adapter.close()


def test_validity_boundary_failure_rolls_back_new_memory(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path, name="remember-boundary-failure.db")
    try:
        store = adapter.engine.store

        def fail_close(memory_id: str, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("injected validity boundary failure")

        monkeypatch.setattr(store, "close_validity", fail_close)
        with pytest.raises(RuntimeError, match="injected validity"):
            adapter.ingest([_record("new", "new", valid_at=10.0, valid_to=20.0)])

        assert adapter._memory_ids == {}
        assert _memory_count(adapter) == 0
    finally:
        adapter.close()


@pytest.mark.parametrize("operation", ["correct", "invalidate"])
def test_mutation_rechecks_target_scope_after_batch_preflight(
    tmp_path, monkeypatch, operation,
):
    adapter = _adapter(tmp_path, name=f"{operation}-recheck.db")
    try:
        old_id = adapter.ingest([_record("old", "old", valid_at=10.0)])[0]
        store = adapter.engine.store
        original_get = store.get_memory
        transactional_reads = 0

        def forged_scope(memory_id: str) -> Any:
            nonlocal transactional_reads
            target = original_get(memory_id)
            if (
                memory_id == old_id and target is not None
                and store.conn.transaction_owned_by_current_thread()
            ):
                transactional_reads += 1
                return dataclasses.replace(target, workspace_id="workspace-other")
            return target

        monkeypatch.setattr(store, "get_memory", forged_scope)
        record = _record(
            "new" if operation == "correct" else "old",
            "replacement" if operation == "correct" else "",
            operation=operation,
            valid_at=20.0,
        )
        if operation == "correct":
            record["corrects"] = "old"
        with pytest.raises(adapters.AdapterError, match="scope boundary"):
            adapter.ingest([record])

        assert transactional_reads >= 1
        assert adapter._memory_ids == {"old": old_id}
        assert _memory_count(adapter) == 1
        assert store.get_memory(old_id).valid_to is None
    finally:
        adapter.close()
