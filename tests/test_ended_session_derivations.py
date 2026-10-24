"""Durable-memory session provenance does not expire with the originating task."""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from engraphis import create_memory_engine
from engraphis.core.interfaces import Scope, SearchFilter
from engraphis.core.mutations import MemoryCommand, memory_version
from engraphis.service import MemoryService, ValidationError


@pytest.fixture(autouse=True)
def _isolated_service_diagnostics(monkeypatch):
    # A temporary test ledger must not probe any operator's default ledger.
    monkeypatch.setattr("engraphis.service._warn_if_db_empty_with_populated_sibling", lambda path: None)


def _seed(engine, scope, *, pending=False):
    workspace = engine.store.get_or_create_workspace("lineage")
    repo = engine.store.get_or_create_repo(workspace, "api") if scope != Scope.WORKSPACE else None
    session = engine.start_session(workspace, repo)
    source = engine.remember(
        "The cache expires after 30 days.", workspace_id=workspace,
        repo_id=repo, session_id=session, scope=scope, valid_from=100.0,
        metadata={"provenance": {"source": "web", "trusted": False}} if pending else None,
        resolve_conflicts=False,
    )
    return workspace, repo, session, source


def _derive(engine, operation, source, version):
    if operation == "correct":
        return engine.correct(source, "The cache expires after 60 days.", reason="verified")
    if operation == "revise":
        return engine.revise_memory(
            source, content="The cache expires after 60 days.", expected_version=version,
            operation_id="lineage-revision", reason="verified",
        )
    return engine.approve_for_prompt(source, reviewer="owner", reason="verified")


@pytest.mark.parametrize("scope", [Scope.REPO, Scope.WORKSPACE])
@pytest.mark.parametrize("ended", [False, True])
@pytest.mark.parametrize("operation", ["correct", "revise", "approve"])
def test_durable_derivation_keeps_exact_origin_session_after_restart(
    tmp_path, monkeypatch, scope, ended, operation,
):
    path = str(tmp_path / "lineage.db")
    engine = create_memory_engine(path, auto_evolve=False)
    try:
        workspace, repo, session, source = _seed(engine, scope, pending=operation == "approve")
        if ended:
            engine.end_session(session, summary="Original work complete.")
        old = engine.store.get_memory(source)
        version = memory_version(old)
        before = time.time()
        assert engine.can_revise_memory(source)
        result = _derive(engine, operation, source, version)
        successor = engine.store.get_memory(result["id"])
        assert successor.session_id == session
        assert successor.workspace_id == workspace
        assert successor.repo_id == repo
        assert successor.scope == scope
        assert engine.store.get_session(session)["status"] == ("summarized" if ended else "active")
        predecessor = engine.store.get_memory(source)
        if operation == "approve":
            assert successor.metadata["approved_from"] == source
            assert successor.provenance["review_state"] == "approved"
            assert predecessor.valid_to is None
            assert predecessor.closure_observations == old.closure_observations
        else:
            assert successor.metadata["corrects"] == source
            assert successor.provenance == old.provenance
            assert predecessor.valid_to is not None
            assert len(predecessor.closure_observations) == 1
            visible_before = engine.store.list_memories(SearchFilter(
                workspace_id=workspace, repo_id=repo, valid_at=before, known_at=before,
            ))
            assert source in {record.id for record in visible_before}
            assert successor.id not in {record.id for record in visible_before}
        frozen_history = predecessor.closure_observations
        engine.close()
        engine = create_memory_engine(path, auto_evolve=False)

        def unavailable(*args, **kwargs):
            raise RuntimeError("replay must not embed")

        monkeypatch.setattr(engine.embedder, "embed", unavailable)
        before_retry = tuple(engine.store.conn.iterdump())
        replay = _derive(engine, operation, source, version)
        assert replay["id"] == successor.id
        assert tuple(engine.store.conn.iterdump()) == before_retry
        assert engine.store.get_memory(source).closure_observations == frozen_history
        assert engine.store.get_memory(successor.id).session_id == session
    finally:
        engine.close()


@pytest.mark.parametrize("scope", [Scope.REPO, Scope.WORKSPACE])
@pytest.mark.parametrize("operation", ["correct", "revise", "approve"])
def test_durable_derivation_rolls_back_source_history_successor_and_command(
    tmp_path, monkeypatch, scope, operation,
):
    engine = create_memory_engine(str(tmp_path / "rollback.db"), auto_evolve=False)
    try:
        _, _, session, source = _seed(engine, scope, pending=operation == "approve")
        engine.end_session(session)
        version = memory_version(engine.store.get_memory(source))
        before = tuple(engine.store.conn.iterdump())
        complete = MemoryCommand.complete

        def fail_after_receipt(command, result_id):
            complete(command, result_id)
            raise RuntimeError("injected after finalization")

        monkeypatch.setattr(MemoryCommand, "complete", fail_after_receipt)
        with pytest.raises(RuntimeError, match="injected after finalization"):
            _derive(engine, operation, source, version)
        assert tuple(engine.store.conn.iterdump()) == before
        assert engine.store.get_memory(source).closure_observations == ()
        assert engine.store.get_memory(source).valid_to is None
        assert engine.can_revise_memory(source)
        monkeypatch.setattr(MemoryCommand, "complete", complete)
        assert _derive(engine, operation, source, version)["id"] != source
    finally:
        engine.close()


@pytest.mark.parametrize("scope", [Scope.REPO, Scope.WORKSPACE])
@pytest.mark.parametrize("operation", ["correct", "revise", "approve"])
def test_durable_session_may_close_during_derivation_preparation(
    tmp_path, monkeypatch, scope, operation,
):
    path = str(tmp_path / "close-race.db")
    engine = create_memory_engine(path, auto_evolve=False)
    closer = create_memory_engine(path, auto_evolve=False)
    try:
        _, _, session, source = _seed(engine, scope, pending=operation == "approve")
        version = memory_version(engine.store.get_memory(source))
        embed = engine.embedder.embed

        def close_during_embed(texts, *, kind="text"):
            assert not engine.store.conn.transaction_owned_by_current_thread()
            closer.end_session(session)
            return embed(texts, kind=kind)

        monkeypatch.setattr(engine.embedder, "embed", close_during_embed)
        result = _derive(engine, operation, source, version)
        assert engine.store.get_memory(result["id"]).session_id == session
        assert engine.store.get_session(session)["status"] == "summarized"
    finally:
        closer.close()
        engine.close()


@pytest.mark.parametrize("scope", [Scope.SESSION, Scope.REPO, Scope.WORKSPACE])
def test_ordinary_ingress_still_rejects_closed_origin_session(scope):
    engine = create_memory_engine(":memory:", auto_evolve=False)
    try:
        workspace, repo, session, _ = _seed(engine, scope)
        engine.end_session(session)
        before = tuple(engine.store.conn.iterdump())
        with pytest.raises(ValueError, match="not active"):
            engine.remember("A new ingress must remain blocked.", workspace_id=workspace,
                            repo_id=repo, session_id=session, scope=scope)
        assert tuple(engine.store.conn.iterdump()) == before
    finally:
        engine.close()


@pytest.mark.parametrize("operation", ["correct", "revise", "approve"])
@pytest.mark.parametrize("ended", [False, True])
def test_session_scoped_derivation_keeps_active_lifetime_fence(operation, ended):
    engine = create_memory_engine(":memory:", auto_evolve=False)
    try:
        _, _, session, source = _seed(engine, Scope.SESSION, pending=operation == "approve")
        if ended:
            engine.end_session(session)
        version = memory_version(engine.store.get_memory(source))
        assert engine.can_revise_memory(source) is not ended
        if ended:
            before = tuple(engine.store.conn.iterdump())
            with pytest.raises(ValueError, match="not active"):
                _derive(engine, operation, source, version)
            assert tuple(engine.store.conn.iterdump()) == before
        else:
            assert engine.store.get_memory(_derive(engine, operation, source, version)["id"]).session_id == session
    finally:
        engine.close()


@pytest.mark.parametrize("violation", ["session_scope", "different_session", "missing_command", "different_repo"])
def test_private_lineage_path_cannot_admit_an_unrelated_write(violation):
    engine = create_memory_engine(":memory:", auto_evolve=False)
    try:
        source_scope = Scope.SESSION if violation == "session_scope" else Scope.REPO
        workspace, repo, session, source = _seed(engine, source_scope)
        other_repo = engine.store.get_or_create_repo(workspace, "other")
        engine.end_session(session)
        kwargs = {
            "workspace_id": workspace, "repo_id": repo, "session_id": session,
            "scope": source_scope, "_session_lineage_source_id": source,
            "_transactional_validator": lambda: None,
            "_transactional_finalizer": lambda result_id: None,
        }
        if violation == "different_session":
            kwargs["session_id"] = engine.start_session(workspace, repo)
        if violation == "missing_command":
            kwargs.pop("_transactional_validator")
        if violation == "different_repo":
            kwargs["repo_id"] = other_repo
        before = tuple(engine.store.conn.iterdump())
        with pytest.raises(ValueError, match="session lineage|does not belong"):
            engine.remember("Unrelated write.", **kwargs)
        assert tuple(engine.store.conn.iterdump()) == before
    finally:
        engine.close()


@pytest.mark.parametrize("scope", [Scope.REPO, Scope.WORKSPACE])
@pytest.mark.parametrize("surface", ["service", "classic", "smart"])
def test_public_correction_accepts_eligible_durable_memory_from_ended_session(
    tmp_path, monkeypatch, scope, surface,
):
    service = MemoryService.create(str(tmp_path / "public.db"), extractor="none")
    try:
        repo = "api" if scope == Scope.REPO else None
        session = service.start_session("lineage", repo=repo)["session_id"]
        source = service.remember(
            "The cache expires after 30 days.", workspace="lineage", repo=repo,
            session_id=session, scope=scope.value,
        )["id"]
        service.end_session(session)
        assert service.inspect(source, workspace="lineage", repo=repo)["memory"]["can_revise"]
        arguments = {"memory_id": source, "new_content": "The cache expires after 60 days.",
                     "workspace": "lineage", "repo": repo}
        if surface == "service":
            result = service.correct(**arguments)
        else:
            pytest.importorskip("mcp")
            from engraphis import mcp_server
            monkeypatch.setattr(mcp_server, "service", lambda: service)

            def call(server, name, arguments):
                response = asyncio.run(server.call_tool(name, arguments))
                content = response.content if hasattr(response, "content") else response
                if isinstance(content, tuple):
                    content = content[0]
                return json.loads(content[0].text)

            if surface == "classic":
                result = call(mcp_server.classic_mcp, "engraphis_correct", arguments)
            else:
                discovered = call(mcp_server.mcp, "engraphis_discover_actions", {
                    "task": "Correct this memory with new content.",
                })["actions"][0]
                assert discovered["canonical_action"] == "correct"
                executed = call(mcp_server.mcp, "engraphis_execute_action", {
                    "capability_id": discovered["capability_id"],
                    "schema_digest": discovered["schema_digest"], "arguments": arguments,
                })
                assert executed["canonical_action"] == "correct", executed
                result = executed["result"]
        assert result["id"] != source, result
        successor = service.store.get_memory(result["id"])
        assert successor.session_id == session
        assert successor.scope == scope
        assert service.inspect(successor.id, workspace="lineage", repo=repo)["memory"]["can_revise"]
        with pytest.raises(ValidationError, match="not active"):
            service.remember("Still closed to ingress.", workspace="lineage", repo=repo,
                             session_id=session, scope=scope.value)
    finally:
        service.close()


def test_widening_and_merging_preserve_ended_session_source_lineage():
    engine = create_memory_engine(":memory:", auto_evolve=False)
    try:
        workspace, repo, session, source = _seed(engine, Scope.REPO)
        second = engine.remember("Backups are encrypted.", workspace_id=workspace, repo_id=repo,
                                 session_id=session, scope=Scope.REPO, resolve_conflicts=False)
        engine.end_session(session)
        promoted = engine.store.get_memory(engine.promote(source, Scope.WORKSPACE)["id"])
        assert promoted.session_id is None
        assert source in promoted.metadata["promoted_from"]
        assert engine.store.get_memory(source).session_id == session
        third = engine.remember("Logs are retained for seven days.", workspace_id=workspace,
                                repo_id=repo, scope=Scope.REPO, resolve_conflicts=False)
        merged = engine.store.get_memory(engine.merge([second, third], "Backups are encrypted; logs last seven days.")["id"])
        assert merged.session_id is None
        assert set(merged.metadata["supersedes"]) == {second, third}
        assert engine.store.get_memory(second).session_id == session
    finally:
        engine.close()
