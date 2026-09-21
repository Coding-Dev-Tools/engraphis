"""Campaign records cannot attach to a repository or session under a different parent."""

import pytest

from eval import campaign_adapters as adapters


def _adapter(tmp_path, name="parentage.db"):
    adapter = adapters.EngraphisAdapter(db_path=str(tmp_path / name))
    prepared = adapter.prepare(
        workspace_id="workspace-a", repo_id="repo-a", session_id="session-a",
    )
    return adapter, prepared


def _one(conn, sql, args=()):
    row = conn.execute(sql, args).fetchone()
    return dict(row) if row is not None else None


def test_foreign_workspace_re_resolves_omitted_and_matching_repo_labels(tmp_path):
    adapter, _ = _adapter(tmp_path)
    try:
        ids = adapter.ingest([
            {
                "record_id": "omitted", "content": "foreign omitted repo",
                "scope": "repo", "workspace": "workspace-b", "valid_at": 1,
            },
            {
                "record_id": "matching", "content": "foreign matching repo",
                "scope": "repo", "workspace": "workspace-b", "repo": "repo-a",
                "valid_at": 2,
            },
        ])
        memories = [adapter.engine.store.get_memory(item) for item in ids]
        assert memories[0] is not None and memories[1] is not None
        assert memories[0].workspace_id == memories[1].workspace_id
        assert memories[0].repo_id == memories[1].repo_id
        repo = _one(
            adapter.engine.store.conn,
            "SELECT workspace_id, name FROM repos WHERE id=?",
            (memories[0].repo_id,),
        )
        assert repo == {"workspace_id": memories[0].workspace_id, "name": "repo-a"}
        assert memories[0].repo_id != adapter.repo_id
    finally:
        adapter.close()


def test_foreign_workspace_omitted_session_uses_logical_label_in_new_repo(tmp_path):
    adapter, _ = _adapter(tmp_path)
    try:
        memory_id = adapter.ingest([{
            "record_id": "foreign-session", "content": "foreign session fact",
            "scope": "session", "workspace": "workspace-b", "repo": "repo-a",
            "valid_at": 1,
        }])[0]
        memory = adapter.engine.store.get_memory(memory_id)
        assert memory is not None
        assert memory.workspace_id != adapter.workspace_id
        assert memory.repo_id != adapter.repo_id
        session = adapter.engine.store.get_session(memory.session_id)
        assert session is not None
        assert session["workspace_id"] == memory.workspace_id
        assert session["repo_id"] == memory.repo_id
        assert adapter.engine.store.get_session(adapter.session_id)["workspace_id"] == adapter.workspace_id
    finally:
        adapter.close()


def test_workspace_scope_does_not_create_irrelevant_repo_or_session(tmp_path):
    adapter, prepared = _adapter(tmp_path)
    try:
        before_repos = adapter.engine.store.conn.execute("SELECT COUNT(*) FROM repos").fetchone()[0]
        before_sessions = adapter.engine.store.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        memory_id = adapter.ingest([{
            "record_id": "workspace", "content": "workspace fact",
            "scope": "workspace", "workspace": "workspace-b",
            "repo": "irrelevant-repo", "session": "irrelevant-session",
            "valid_at": 1,
        }])[0]
        memory = adapter.engine.store.get_memory(memory_id)
        assert memory is not None
        assert memory.scope.value == "workspace"
        assert memory.repo_id is None
        assert memory.session_id is None
        assert adapter.engine.store.conn.execute("SELECT COUNT(*) FROM repos").fetchone()[0] == before_repos
        assert adapter.engine.store.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == before_sessions
        assert prepared["repo_id"] == adapter.repo_id
    finally:
        adapter.close()


@pytest.mark.parametrize("field,value", [
    ("repo", "repo_physical_placeholder"),
    ("session", "ses_physical_placeholder"),
])
def test_physical_parent_aliases_do_not_become_logical_names(tmp_path, field, value):
    adapter, prepared = _adapter(tmp_path, f"physical-{field}.db")
    try:
        record = {
            "record_id": f"foreign-{field}", "content": "must fail closed",
            "scope": "session" if field == "session" else "repo",
            "workspace": "workspace-b", "valid_at": 1,
        }
        if field == "repo":
            record.update({"repo": prepared["repo_id"]})
        else:
            record.update({"repo": "repo-a", "session": prepared["session_id"]})
        with pytest.raises(adapters.AdapterConfigurationError):
            adapter.ingest([record])
        assert adapter.engine.store.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    finally:
        adapter.close()


def test_unknown_physical_session_is_not_created_as_a_label(tmp_path):
    adapter, _ = _adapter(tmp_path, "unknown-session.db")
    try:
        with pytest.raises(adapters.AdapterConfigurationError, match="physical ID"):
            adapter.ingest([{
                "record_id": "unknown", "content": "must fail closed",
                "scope": "session", "workspace": "workspace-b",
                "repo": "repo-a", "session": "ses_missing", "valid_at": 1,
            }])
        assert adapter.engine.store.conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE id=?", ("ses_missing",),
        ).fetchone()[0] == 0
        assert adapter.engine.store.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    finally:
        adapter.close()


def test_same_parent_logical_and_physical_aliases_preserve_identity(tmp_path):
    adapter, prepared = _adapter(tmp_path, "same-parent.db")
    try:
        cases = [
            ({"record_id": "r1", "content": "r1", "scope": "repo", "workspace": "workspace-a"},
             (adapter.workspace_id, adapter.repo_id, None)),
            ({"record_id": "r2", "content": "r2", "scope": "repo", "workspace": "workspace-a", "repo": "repo-a"},
             (adapter.workspace_id, adapter.repo_id, None)),
            ({"record_id": "s1", "content": "s1", "scope": "session", "workspace": "workspace-a", "repo": "repo-a"},
             (adapter.workspace_id, adapter.repo_id, adapter.session_id)),
            ({"record_id": "s2", "content": "s2", "scope": "session", "workspace": prepared["workspace_id"],
              "repo": prepared["repo_id"], "session": prepared["session_id"]},
             (adapter.workspace_id, adapter.repo_id, adapter.session_id)),
        ]
        for record, expected in cases:
            normalized = adapters.CampaignRecord.from_mapping(record)
            assert adapter._record_scope_ids(normalized) == expected
    finally:
        adapter.close()


def test_missing_scope_keeps_workspace_default_and_no_parents(tmp_path):
    adapter, _ = _adapter(tmp_path, "default-scope.db")
    try:
        normalized = adapters.CampaignRecord.from_mapping({
            "record_id": "default", "content": "workspace by default",
        })
        assert normalized.scope == "workspace"
        assert adapter._record_scope_ids(normalized) == (adapter.workspace_id, None, None)
    finally:
        adapter.close()
