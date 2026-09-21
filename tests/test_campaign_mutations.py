"""Campaign mutations preserve predecessor scope and traversable history."""

import pytest

from eval import campaign_adapters as adapters


def _adapter(tmp_path, name="scope.db"):
    adapter = adapters.EngraphisAdapter(db_path=str(tmp_path / name))
    adapter.prepare(workspace_id="workspace-a", repo_id="repo-a")
    return adapter


def _seed(adapter, *, scope="workspace", workspace="workspace-a", repo="repo-a", session=None):
    return adapter.ingest([{
        "record_id": "old", "content": "old scoped fact", "scope": scope,
        "workspace": workspace, "repo": repo, "session": session,
    }])[0]


def test_cross_workspace_correction_is_rejected_before_successor_or_close(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path)
    try:
        old_id = _seed(adapter)
        writes = []
        original_write = adapter.engine.remember_with_resolution

        def remember(*args, **kwargs):
            writes.append((args, kwargs))
            return original_write(*args, **kwargs)

        monkeypatch.setattr(adapter.engine, "remember_with_resolution", remember)
        with pytest.raises(adapters.AdapterError, match="scope boundary"):
            adapter.ingest([{
                "record_id": "new", "content": "corrected fact", "operation": "correct",
                "corrects": "old", "scope": "workspace", "workspace": "workspace-b",
            }])
        assert writes == []
        assert set(adapter._memory_ids) == {"old"}
        assert adapter.engine.store.get_memory(old_id).valid_to is None
    finally:
        adapter.close()


def test_cross_workspace_invalidation_is_rejected_before_close(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path)
    try:
        old_id = _seed(adapter)
        closes = []
        original_close = adapter.engine.store.close_validity

        def close(*args, **kwargs):
            closes.append((args, kwargs))
            return original_close(*args, **kwargs)

        monkeypatch.setattr(adapter.engine.store, "close_validity", close)
        with pytest.raises(adapters.AdapterError, match="scope boundary"):
            adapter.ingest([{
                "record_id": "old", "content": "", "operation": "invalidate",
                "scope": "workspace", "workspace": "workspace-b",
            }])
        assert closes == []
        assert adapter.engine.store.get_memory(old_id).valid_to is None
    finally:
        adapter.close()


def test_same_scope_correction_closes_predecessor_after_successor_write(tmp_path):
    adapter = _adapter(tmp_path)
    try:
        old_id = _seed(adapter)
        new_id = adapter.ingest([{
            "record_id": "new", "content": "corrected scoped fact", "operation": "correct",
            "corrects": "old", "scope": "workspace", "workspace": "workspace-a",
        }])[0]
        old = adapter.engine.store.get_memory(old_id)
        new = adapter.engine.store.get_memory(new_id)
        assert old is not None and old.valid_to is not None
        assert new is not None and new.valid_to is None
        assert (new.workspace_id, new.repo_id, new.session_id, new.scope) == (
            old.workspace_id, old.repo_id, old.session_id, old.scope,
        )
    finally:
        adapter.close()


def test_same_scope_invalidation_remains_supported(tmp_path):
    adapter = _adapter(tmp_path)
    try:
        old_id = _seed(adapter)
        result = adapter.ingest([{
            "record_id": "old", "content": "", "operation": "invalidate",
            "scope": "workspace", "workspace": "workspace-a",
        }])
        assert result == [old_id]
        assert adapter.engine.store.get_memory(old_id).valid_to is not None
    finally:
        adapter.close()


def test_default_correction_closes_predecessor_at_successor_start(tmp_path):
    adapter = _adapter(tmp_path, "default-correction-boundary.db")
    try:
        old_id = _seed(adapter)
        new_id = adapter.ingest([{
            "record_id": "new", "content": "corrected scoped fact", "operation": "correct",
            "corrects": "old", "scope": "workspace", "workspace": "workspace-a",
        }])[0]
        predecessor = adapter.engine.store.get_memory(old_id)
        successor = adapter.engine.store.get_memory(new_id)
        assert predecessor.valid_to == successor.valid_from
    finally:
        adapter.close()


@pytest.mark.parametrize(
    ("scope", "record_repo", "record_session", "correction_repo", "correction_session"),
    [
        ("repo", "repo-a", None, "repo-b", None),
        ("session", "repo-a", "session-a", "repo-a", "session-b"),
    ],
)
@pytest.mark.parametrize("operation", ["correct", "invalidate"])
def test_mutation_compares_exact_repo_and_session_scope(
    tmp_path, scope, record_repo, record_session, correction_repo, correction_session, operation,
):
    adapter = _adapter(tmp_path, f"{scope}.db")
    try:
        old_id = _seed(adapter, scope=scope, repo=record_repo, session=record_session)
        with pytest.raises(adapters.AdapterError, match="scope boundary"):
            adapter.ingest([{
                "record_id": "new" if operation == "correct" else "old",
                "content": "corrected fact", "operation": operation,
                "corrects": "old" if operation == "correct" else None,
                "scope": scope, "workspace": "workspace-a",
                "repo": correction_repo, "session": correction_session,
            }])
        assert set(adapter._memory_ids) == {"old"}
        assert adapter.engine.store.get_memory(old_id).valid_to is None
    finally:
        adapter.close()


@pytest.mark.parametrize("operation", ["correct", "invalidate"])
def test_unknown_history_target_keeps_existing_missing_target_error(tmp_path, operation):
    adapter = _adapter(tmp_path, f"{operation}.db")
    try:
        record = {
            "record_id": "new" if operation == "correct" else "missing",
            "content": "replacement" if operation == "correct" else "",
            "operation": operation, "scope": "workspace", "workspace": "workspace-a",
        }
        if operation == "correct":
            record["corrects"] = "missing"
        with pytest.raises(adapters.AdapterError, match="target is missing"):
            adapter.ingest([record])
        assert adapter._memory_ids == {}
    finally:
        adapter.close()


def test_engraphis_correction_maps_fixture_target_to_core_lineage(tmp_path):
    from engraphis.service import MemoryService

    adapter = adapters.EngraphisAdapter(db_path=str(tmp_path / "correction-lineage.db"))
    try:
        adapter.prepare(workspace_id="workspace-a", repo_id="repo-a")
        adapter.ingest([
            {
                "record_id": "old-fact", "content": "Historical timeout was 30 seconds.",
                "scope": "repo", "workspace": "workspace-a", "repo": "repo-a",
                "valid_at": 5.0, "trusted": True,
            },
            {
                "record_id": "new-fact", "content": "Current timeout is 60 seconds.",
                "op": "correct", "corrects": "old-fact", "scope": "repo",
                "workspace": "workspace-a", "repo": "repo-a", "valid_at": 20.0,
                "trusted": True,
            },
            {
                "record_id": "latest-fact", "content": "Current timeout is 90 seconds.",
                "op": "correct", "corrects": "new-fact", "scope": "repo",
                "workspace": "workspace-a", "repo": "repo-a", "valid_at": 30.0,
                "trusted": True,
            },
        ])
        old_id = adapter._memory_ids["old-fact"]
        new_id = adapter._memory_ids["new-fact"]
        latest_id = adapter._memory_ids["latest-fact"]
        latest = adapter.engine.store.get_memory(latest_id)
        assert latest is not None
        assert latest.metadata["campaign_corrects"] == "new-fact"
        assert latest.metadata["corrects"] == new_id
        assert latest.metadata["supersedes"] == [new_id]

        service = MemoryService(adapter.engine)
        chain = service.inspect(latest_id, workspace="workspace-a", repo="repo-a")["chain"]
        assert [record["id"] for record in chain] == [old_id, new_id, latest_id]
        root_chain = service.inspect(old_id, workspace="workspace-a", repo="repo-a")["chain"]
        assert [record["id"] for record in root_chain] == [old_id, new_id, latest_id]
        history = service.memory_history(latest_id, workspace="workspace-a", repo="repo-a")
        assert [record["id"] for record in history["versions"]] == [old_id, new_id, latest_id]
    finally:
        adapter.close()


@pytest.mark.parametrize("operation", ["correct", "invalidate"])
def test_mutation_cannot_change_scope_level(tmp_path, operation):
    adapter = _adapter(tmp_path)
    try:
        old_id = _seed(adapter)
        with pytest.raises(adapters.AdapterError, match="scope boundary"):
            adapter.ingest([{
                "record_id": "new" if operation == "correct" else "old",
                "content": "replacement", "operation": operation,
                "corrects": "old" if operation == "correct" else None,
                "scope": "repo", "repo": "repo-a",
            }])
        assert adapter.engine.store.get_memory(old_id).valid_to is None
        assert set(adapter._memory_ids) == {"old"}
    finally:
        adapter.close()


def test_physical_scope_ids_and_forged_lineage_metadata_use_validated_target(tmp_path):
    adapter = _adapter(tmp_path)
    try:
        old_id = _seed(adapter, scope="repo")
        new_id = adapter.ingest([{
            "record_id": "new", "content": "replacement", "op": "correct", "corrects": "old",
            "scope": "repo", "workspace": adapter.workspace_id, "repo": adapter.repo_id,
            "metadata": {"supersedes": ["forged"], "corrects": "forged", "campaign_corrects": "forged"},
        }])[0]
        new = adapter.engine.store.get_memory(new_id)
        assert new.metadata["corrects"] == old_id
        assert new.metadata["supersedes"] == [old_id]
        assert new.metadata["campaign_corrects"] == "old"
        assert (new.workspace_id, new.repo_id) == (adapter.workspace_id, adapter.repo_id)
    finally:
        adapter.close()
