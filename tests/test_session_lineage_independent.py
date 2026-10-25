"""Independent tests for the narrow same-scope governed session-lineage exception."""
from __future__ import annotations

import pytest

from engraphis import create_memory_engine
from engraphis.core.interfaces import Scope
from engraphis.core.mutations import memory_version


@pytest.fixture(autouse=True)
def _isolated_service_diagnostics(monkeypatch):
    monkeypatch.setattr("engraphis.service._warn_if_db_empty_with_populated_sibling", lambda path: None)


def _source(engine, scope, operation="correct"):
    workspace = engine.store.get_or_create_workspace("lineage")
    repo = engine.store.get_or_create_repo(workspace, "project")
    session = engine.start_session(workspace, repo)
    metadata = {"provenance": {"source": "agent", "trusted": False, "review_state": "pending"}} if operation == "approve" else None
    memory = engine.remember(
        "An established source fact", workspace_id=workspace,
        repo_id=repo if scope != Scope.WORKSPACE else None, session_id=session,
        scope=scope, metadata=metadata, resolve_conflicts=False,
    )
    return workspace, repo, session, memory


def _operate(engine, operation, memory):
    if operation == "correct":
        return engine.correct(memory, "A corrected source fact", reason="Verified history")
    if operation == "revise":
        return engine.revise_memory(
            memory, expected_version=memory_version(engine.store.get_memory(memory)),
            operation_id="lineage-revision", content="A revised source fact", reason="Verified history",
        )
    return engine.approve_for_prompt(memory, reviewer="local-owner", reason="Verified source")


@pytest.mark.parametrize("scope", [Scope.REPO, Scope.WORKSPACE])
@pytest.mark.parametrize("operation", ["correct", "revise", "approve"])
def test_ended_nonprivate_lineage_survives_same_scope_derivation(tmp_path, scope, operation):
    engine = create_memory_engine(str(tmp_path / "lineage.db"), auto_evolve=False)
    try:
        workspace, repo, session, memory = _source(engine, scope, operation)
        engine.end_session(session, summary="Completed original task")
        ended = engine.store.get_session(session)
        if operation != "approve":
            assert engine.can_revise_memory(memory)

        result = _operate(engine, operation, memory)

        successor = engine.store.get_memory(result["id"])
        assert successor.id != memory
        assert successor.scope == scope
        assert successor.workspace_id == workspace
        assert successor.repo_id == (repo if scope == Scope.REPO else None)
        assert successor.session_id == session
        assert engine.store.get_session(session) == ended
        assert engine.store.conn.execute("SELECT COUNT(*) FROM memory_commands").fetchone()[0] == 1
        assert engine.store.conn.execute("SELECT COUNT(*) FROM memory_command_sources").fetchone()[0] == 1
    finally:
        engine.close()


@pytest.mark.parametrize("scope", [Scope.REPO, Scope.WORKSPACE])
@pytest.mark.parametrize("operation", ["correct", "revise", "approve"])
def test_nonprivate_derivation_tolerates_close_during_preparation(tmp_path, monkeypatch, scope, operation):
    path = str(tmp_path / "lineage-close.db")
    engine = create_memory_engine(path, auto_evolve=False)
    closer = create_memory_engine(path, auto_evolve=False)
    try:
        _, _, session, memory = _source(engine, scope, operation)
        original = engine.embedder.embed

        def embed(texts, *, kind="text"):
            assert not engine.store.conn.transaction_owned_by_current_thread()
            closer.end_session(session)
            return original(texts, kind=kind)

        monkeypatch.setattr(engine.embedder, "embed", embed)
        result = _operate(engine, operation, memory)
        assert engine.store.get_memory(result["id"]).session_id == session
        assert engine.store.get_session(session)["status"] != "active"
    finally:
        closer.close()
        engine.close()


@pytest.mark.parametrize("operation", ["correct", "revise", "approve"])
def test_session_scope_derivation_retains_closed_session_fence(tmp_path, operation):
    engine = create_memory_engine(str(tmp_path / "private.db"), auto_evolve=False)
    try:
        _, _, session, memory = _source(engine, Scope.SESSION, operation)
        engine.end_session(session)
        assert not engine.can_revise_memory(memory)
        with pytest.raises(ValueError, match="not active"):
            _operate(engine, operation, memory)
        assert engine.store.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
        assert engine.store.conn.execute("SELECT COUNT(*) FROM memory_commands").fetchone()[0] == 0
    finally:
        engine.close()


@pytest.mark.parametrize("scope", [Scope.REPO, Scope.WORKSPACE, Scope.SESSION])
def test_ordinary_ingress_cannot_opt_in_through_metadata(tmp_path, scope):
    engine = create_memory_engine(str(tmp_path / "ordinary.db"), auto_evolve=False)
    try:
        workspace, repo, session, memory = _source(engine, scope)
        engine.end_session(session)
        with pytest.raises(ValueError, match="not active"):
            engine.remember(
                "An unrelated new fact", workspace_id=workspace,
                repo_id=repo if scope != Scope.WORKSPACE else None,
                session_id=session, scope=scope,
                metadata={"_session_lineage_source_id": memory}, resolve_conflicts=False,
            )
        assert engine.store.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
    finally:
        engine.close()


@pytest.mark.parametrize("mismatch", ["scope", "session", "workspace", "callbacks"])
def test_internal_lineage_path_rejects_unmatched_sources_atomically(tmp_path, mismatch):
    engine = create_memory_engine(str(tmp_path / "invalid-lineage.db"), auto_evolve=False)
    try:
        workspace, repo, session, memory = _source(engine, Scope.REPO)
        engine.end_session(session)
        target_scope = Scope.SESSION if mismatch == "scope" else Scope.REPO
        if mismatch == "session":
            session = engine.start_session(workspace, repo)
        elif mismatch == "workspace":
            workspace = engine.store.get_or_create_workspace("other-lineage")
            repo = engine.store.get_or_create_repo(workspace, "project")
            session = engine.start_session(workspace, repo)
        callbacks = {} if mismatch == "callbacks" else {
            "_transactional_validator": lambda: None,
            "_transactional_finalizer": lambda _memory_id: None,
        }
        with pytest.raises(ValueError, match="matching governed source"):
            engine.remember(
                "An inadmissible derivation", workspace_id=workspace, repo_id=repo,
                session_id=session, scope=target_scope, resolve_conflicts=False,
                _session_lineage_source_id=memory, **callbacks,
            )
        assert engine.store.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
        assert engine.store.conn.execute("SELECT COUNT(*) FROM memory_commands").fetchone()[0] == 0
    finally:
        engine.close()


@pytest.mark.parametrize("scope", [Scope.REPO, Scope.SESSION])
def test_shared_scope_authority_is_distinct_from_historical_session_owner(scope):
    from engraphis.service import MemoryService, ValidationError, set_current_user

    service = MemoryService.create(":memory:")
    try:
        set_current_user({"id": "usr_alice", "email": "alice@example.test", "role": "member"})
        service.create_workspace("shared-lineage", visibility="shared", confirmed=True)
        session = service.start_session("shared-lineage", repo="project", goal="Original task")
        source = service.remember(
            "An established shared fact", workspace="shared-lineage", repo="project",
            session_id=session["session_id"], scope=scope.value,
        )
        service.end_session(session["session_id"])

        set_current_user({"id": "usr_bob", "email": "bob@example.test", "role": "member"})
        if scope == Scope.SESSION:
            with pytest.raises(ValidationError, match="another user"):
                service.correct(source["id"], "An edited private fact", workspace="shared-lineage", repo="project")
        else:
            result = service.correct(
                source["id"], "An edited shared fact", workspace="shared-lineage", repo="project",
            )
            assert service.store.get_memory(result["id"]).session_id == session["session_id"]
    finally:
        set_current_user(None)
        service.close()
