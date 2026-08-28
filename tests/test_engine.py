import json
import os
import sqlite3
import tempfile
import time

import pytest

from engraphis.backends.vector_numpy import NumpyVectorIndex
from engraphis.core.engine import MemoryEngine
from engraphis.core.interfaces import MemoryRecord, MemoryType, Node, Scope, SearchFilter


class _RecordingExternalIndex:
    """Small separately-backed index used to observe publication ordering."""

    shares_store_vector_table = False

    def __init__(self):
        self.ids = set()
        self.upserts = []
        self.deletes = []

    def search(self, _vec, _k, *, filter=None):
        return []

    def upsert(self, ids, _vecs, meta=None, *, commit=True):
        self.upserts.append(tuple(ids))
        self.ids.update(ids)

    def delete(self, ids, *, commit=True):
        self.deletes.append(tuple(ids))
        self.ids.difference_update(ids)


def _use_recording_external_index(engine):
    index = _RecordingExternalIndex()
    engine.index = index
    engine.recall_engine.index = index
    return index


def test_engine_remember_and_recall():
    eng = MemoryEngine.create(":memory:")          # offline defaults
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    eng.remember("We deploy with GitHub Actions to AWS ECS.", workspace_id=wid, repo_id=rid,
                 title="deployment", importance=0.8)
    eng.remember("Lunch is usually around noon.", workspace_id=wid, repo_id=rid)
    res = eng.recall("how do we deploy?", workspace_id=wid, k=2)
    assert res.count >= 1
    assert "actions" in res.context.lower() or "aws" in res.context.lower()


def test_entity_incidence_includes_title_only_mentions_on_write_and_backfill():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    fresh_entity = eng.store.upsert_entity(Node(
        id="", name="Apollo", ntype="project", workspace_id=wid,
    ))
    fresh = eng.remember(
        "The body intentionally contains no project name.",
        title="Apollo launch status", workspace_id=wid, resolve_conflicts=False,
    )
    legacy = eng.remember(
        "The body intentionally contains no program name.",
        title="Beacon migration status", workspace_id=wid, resolve_conflicts=False,
    )
    legacy_entity = eng.store.upsert_entity(Node(
        id="", name="Beacon", ntype="project", workspace_id=wid,
    ))

    incidence = eng.store.list_memory_entities(SearchFilter(workspace_id=wid))
    pairs = {(row["memory_id"], row["entity_id"]) for row in incidence}
    assert (fresh, fresh_entity) in pairs
    assert (legacy, legacy_entity) in pairs


def test_repo_memory_links_existing_workspace_entity_on_write():
    """A repo write must see the workspace ancestor entity already in scope."""
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    entity_id = eng.store.upsert_entity(Node(
        id="", name="Apollo", ntype="project", workspace_id=wid,
    ))

    memory_id = eng.remember(
        "Apollo owns the release calendar.",
        workspace_id=wid, repo_id=rid, resolve_conflicts=False,
    )

    rows = eng.store.list_memory_entities(SearchFilter(
        workspace_id=wid, repo_id=rid, include_ancestors=True,
    ))
    assert (memory_id, entity_id) in {
        (row["memory_id"], row["entity_id"]) for row in rows
    }


def test_link_memory_entities_commits_a_standalone_enrichment():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    memory_id = eng.remember(
        "This note predates its named entity.",
        workspace_id=wid,
        resolve_conflicts=False,
    )
    entity_id = eng.store.upsert_entity(Node(
        id="", name="Apollo", ntype="project", workspace_id=wid,
    ))

    eng._link_memory_entities(
        memory_id,
        "Apollo now owns the launch.",
        workspace_id=wid,
        repo_id=None,
        valid_from=None,
    )

    assert eng.store.conn.in_transaction is False
    assert (memory_id, entity_id) in {
        (row["memory_id"], row["entity_id"])
        for row in eng.store.list_memory_entities(SearchFilter(workspace_id=wid))
    }


def test_link_memory_entities_does_not_commit_a_caller_transaction():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    memory_id = eng.remember(
        "This note predates its named entity.",
        workspace_id=wid,
        resolve_conflicts=False,
    )
    entity_id = eng.store.upsert_entity(Node(
        id="", name="Apollo", ntype="project", workspace_id=wid,
    ))

    eng.store.conn.execute("BEGIN IMMEDIATE")
    eng._link_memory_entities(
        memory_id,
        "Apollo now owns the launch.",
        workspace_id=wid,
        repo_id=None,
        valid_from=None,
    )

    assert eng.store.conn.transaction_owned_by_current_thread()
    assert eng.store.conn.in_transaction is True
    pending = eng.store.conn.execute(
        "SELECT 1 FROM memory_entities WHERE memory_id=? AND entity_id=?",
        (memory_id, entity_id),
    ).fetchone()
    assert pending is not None
    eng.store.conn.rollback()
    assert eng.store.conn.in_transaction is False
    assert eng.store.conn.execute(
        "SELECT 1 FROM memory_entities WHERE memory_id=? AND entity_id=?",
        (memory_id, entity_id),
    ).fetchone() is None


def test_engine_recall_requires_explicit_reinforcement_signal():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    mid = eng.remember("The deployment target is AWS ECS.", workspace_id=wid, repo_id=rid)
    before = eng.store.get_memory(mid).access_count

    eng.recall("unrelated lunch menu", workspace_id=wid, repo_id=rid, k=1)
    assert eng.store.get_memory(mid).access_count == before

    eng.recall(
        "deployment target",
        workspace_id=wid,
        repo_id=rid,
        k=1,
        reinforce=True,
    )
    assert eng.store.get_memory(mid).access_count > before


def test_index_upsert_failure_preserves_memory_and_audits(caplog):
    class BrokenIndex:
        def search(self, _vec, _k, *, filter=None):
            return []

        def upsert(self, _ids, _vecs, meta=None):
            raise RuntimeError("simulated index outage")

    eng = MemoryEngine.create(":memory:", vector_backend="numpy", auto_evolve=False)
    eng.index = BrokenIndex()
    eng.recall_engine.index = eng.index
    wid = eng.store.get_or_create_workspace("w")
    with caplog.at_level("WARNING"):
        out = eng.remember_with_resolution("Durable fact.", workspace_id=wid)
    assert eng.store.get_memory(out["id"]).content == "Durable fact."
    row = eng.store.conn.execute(
        "SELECT action, target, detail FROM audit WHERE action='index_upsert_failed'"
    ).fetchone()
    assert dict(row) == {
        "action": "index_upsert_failed",
        "target": out["id"],
        "detail": "failure_type=RuntimeError",
    }
    assert "simulated index outage" not in caplog.text
    assert "RuntimeError" in caplog.text


def test_session_rollback_does_not_publish_an_external_vector(monkeypatch):
    eng = MemoryEngine.create(":memory:", auto_evolve=False)
    workspace_id = eng.store.get_or_create_workspace("session-index-rollback")
    repo_id = eng.store.get_or_create_repo(workspace_id, "repo")
    session_id = eng.start_session(workspace_id, repo_id)
    index = _use_recording_external_index(eng)

    def fail_after_old_upsert_position(*_args, **_kwargs):
        raise RuntimeError("late session failure")

    monkeypatch.setattr(eng, "_evolve", fail_after_old_upsert_position)
    with pytest.raises(RuntimeError, match="late session failure"):
        eng.remember(
            "session write that must roll back",
            workspace_id=workspace_id,
            repo_id=repo_id,
            session_id=session_id,
            scope=Scope.SESSION,
            resolve_conflicts=False,
        )

    assert index.upserts == []
    assert index.ids == set()
    assert eng.store.list_memories(
        SearchFilter(workspace_id=workspace_id, session_id=session_id),
        include_invalid=True,
    ) == []
    assert eng.store.conn.in_transaction is False


def test_lifecycle_rollback_does_not_publish_an_external_vector(monkeypatch):
    eng = MemoryEngine.create(":memory:", auto_evolve=False)
    workspace_id = eng.store.get_or_create_workspace("lifecycle-index-rollback")
    original_id = eng.remember(
        "the original deployment target",
        workspace_id=workspace_id,
        resolve_conflicts=False,
    )
    index = _use_recording_external_index(eng)

    def fail_after_old_upsert_position(*_args, **_kwargs):
        raise RuntimeError("late lifecycle failure")

    monkeypatch.setattr(eng, "_evolve", fail_after_old_upsert_position)
    with pytest.raises(RuntimeError, match="late lifecycle failure"):
        eng.correct(original_id, "the corrected deployment target")

    assert index.upserts == []
    assert index.ids == set()
    records = eng.store.list_memories(
        SearchFilter(workspace_id=workspace_id), include_invalid=True,
    )
    assert [record.id for record in records] == [original_id]
    assert eng.store.get_memory(original_id).valid_to is None
    assert original_id in eng.store.get_vectors([original_id])
    assert eng.store.conn.in_transaction is False


def test_caller_owned_transaction_rejects_external_index_before_mutation():
    eng = MemoryEngine.create(":memory:", auto_evolve=False)
    workspace_id = eng.store.get_or_create_workspace("caller-index-transaction")
    index = _use_recording_external_index(eng)
    eng.store.conn.execute("BEGIN IMMEDIATE")

    with pytest.raises(
        RuntimeError,
        match="caller-owned transactions cannot write through a separate vector index",
    ):
        eng.remember(
            "a caller-owned write must not escape its transaction",
            workspace_id=workspace_id,
            resolve_conflicts=False,
        )

    assert eng.store.conn.transaction_owned_by_current_thread()
    assert eng.store.conn.in_transaction is True
    assert index.upserts == []
    assert index.ids == set()
    assert eng.store.list_memories(
        SearchFilter(workspace_id=workspace_id), include_invalid=True,
    ) == []
    eng.store.conn.rollback()
    assert eng.store.conn.in_transaction is False


def test_graph_extraction_failure_is_nonfatal_and_redacted(caplog):
    class BrokenGraphExtractor:
        def extract(self, _content, *, title=""):
            raise RuntimeError("private graph payload detail")

    eng = MemoryEngine.create(":memory:", graph_extractor="none", auto_evolve=False)
    eng.graph_extractor = BrokenGraphExtractor()
    wid = eng.store.get_or_create_workspace("w")

    with caplog.at_level("WARNING", logger="engraphis.core.engine"):
        memory_id = eng.remember(
            "The confidential project marker is indigo.",
            workspace_id=wid,
            resolve_conflicts=False,
        )

    assert eng.store.get_memory(memory_id).content == (
        "The confidential project marker is indigo."
    )
    assert "graph extraction failed (RuntimeError)" in caplog.text
    assert "private graph payload detail" not in caplog.text
    assert "confidential project marker" not in caplog.text
    assert memory_id not in caplog.text


def test_best_effort_failure_warnings_are_per_operation_and_rate_limited(caplog):
    class Clock:
        value = 100.0

        def __call__(self):
            return self.value

    clock = Clock()
    eng = MemoryEngine.create(":memory:", auto_evolve=False)
    eng._failure_warning_clock = clock

    with caplog.at_level("WARNING", logger="engraphis.core.engine"):
        eng._warn_redacted_failure("graph extraction", RuntimeError("first-secret"))
        eng._warn_redacted_failure("graph extraction", RuntimeError("second-secret"))
        eng._warn_redacted_failure("memory evolution", KeyError("other-secret"))
        clock.value += 60.0
        eng._warn_redacted_failure("graph extraction", ValueError("summary-secret"))

    messages = [record.getMessage() for record in caplog.records]
    assert messages == [
        "graph extraction failed (RuntimeError)",
        "memory evolution failed (KeyError)",
        "graph extraction failed (ValueError); suppressed 1 similar failures",
    ]
    assert "secret" not in caplog.text


def test_best_effort_failure_warning_limits_are_independent_per_engine(caplog):
    class Clock:
        def __call__(self):
            return 100.0

    first = MemoryEngine.create(":memory:", auto_evolve=False)
    second = MemoryEngine.create(":memory:", auto_evolve=False)
    first._failure_warning_clock = Clock()
    second._failure_warning_clock = Clock()

    with caplog.at_level("WARNING", logger="engraphis.core.engine"):
        first._warn_redacted_failure("graph extraction", RuntimeError("first-secret"))
        first._warn_redacted_failure("graph extraction", RuntimeError("second-secret"))
        second._warn_redacted_failure("graph extraction", RuntimeError("third-secret"))

    assert [record.getMessage() for record in caplog.records] == [
        "graph extraction failed (RuntimeError)",
        "graph extraction failed (RuntimeError)",
    ]
    assert "secret" not in caplog.text


def test_resolution_index_failure_uses_canonical_vectors_and_audits(caplog):
    eng = MemoryEngine.create(":memory:", vector_backend="numpy", auto_evolve=False)
    wid = eng.store.get_or_create_workspace("w")
    first = eng.remember_with_resolution(
        "The release marker is indigo.",
        workspace_id=wid,
    )
    delegate = eng.index

    class BrokenSearchIndex:
        def search(self, _vec, _k, *, filter=None):
            raise RuntimeError("sensitive-provider-detail")

        def upsert(self, ids, vecs, meta=None, *, commit=True):
            return delegate.upsert(ids, vecs, meta, commit=commit)

        def delete(self, ids, *, commit=True):
            return delegate.delete(ids, commit=commit)

    eng.index = BrokenSearchIndex()
    with caplog.at_level("WARNING"):
        repeated = eng.remember_with_resolution(
            "The release marker is indigo.",
            workspace_id=wid,
        )

    assert repeated["op"] == "noop"
    assert repeated["id"] == first["id"]
    assert eng.store.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
    audit = eng.store.conn.execute(
        "SELECT actor, action, target, detail FROM audit "
        "WHERE action='index_search_fallback'"
    ).fetchone()
    assert dict(audit) == {
        "actor": "resolver",
        "action": "index_search_fallback",
        "target": wid,
        "detail": "failure_type=RuntimeError",
    }
    assert "sensitive-provider-detail" not in caplog.text
    assert "RuntimeError" in caplog.text


def test_resolution_empty_index_uses_canonical_vectors():
    eng = MemoryEngine.create(":memory:", vector_backend="numpy", auto_evolve=False)
    wid = eng.store.get_or_create_workspace("w")
    first = eng.remember_with_resolution(
        "The empty index must not hide an existing release marker.",
        workspace_id=wid,
    )
    delegate = eng.index

    class EmptySearchIndex:
        def search(self, _vec, _k, *, filter=None):
            return []

        def upsert(self, ids, vecs, meta=None, *, commit=True):
            return delegate.upsert(ids, vecs, meta, commit=commit)

        def delete(self, ids, *, commit=True):
            return delegate.delete(ids, commit=commit)

    eng.index = EmptySearchIndex()
    repeated = eng.remember_with_resolution(
        "The empty index must not hide an existing release marker.",
        workspace_id=wid,
    )

    assert repeated["op"] == "noop"
    assert repeated["id"] == first["id"]
    assert eng.store.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1


def test_resolution_aborts_before_write_when_index_and_canonical_scan_fail(
        monkeypatch, caplog):
    eng = MemoryEngine.create(":memory:", vector_backend="numpy", auto_evolve=False)
    wid = eng.store.get_or_create_workspace("w")
    eng.remember("Existing fact.", workspace_id=wid, resolve_conflicts=False)

    def fail_search(_vec, _k, *, filter=None):
        raise RuntimeError("provider-secret")

    def fail_scan(*_args, **_kwargs):
        raise sqlite3.DatabaseError("database-secret")

    monkeypatch.setattr(eng.index, "search", fail_search)
    monkeypatch.setattr(eng.store, "iter_vectors", fail_scan)
    before = eng.store.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]

    with caplog.at_level("WARNING"):
        with pytest.raises(RuntimeError, match="vector neighbor resolution unavailable"):
            eng.remember_with_resolution("New fact.", workspace_id=wid)

    assert eng.store.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == before
    assert "provider-secret" not in caplog.text
    assert "database-secret" not in caplog.text
    assert eng.store.conn.in_transaction is False


def test_engine_infers_scope_and_rejects_impossible_parents():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")

    workspace = eng.remember("Workspace fact.", workspace_id=wid)
    repo = eng.remember("Repo fact.", workspace_id=wid, repo_id=rid)
    assert eng.store.get_memory(workspace).scope == Scope.WORKSPACE
    assert eng.store.get_memory(repo).scope == Scope.REPO

    session_id = eng.start_session(wid, rid)
    session_grouped = eng.remember(
        "Session-grouped repo fact.", workspace_id=wid, session_id=session_id
    )
    assert eng.store.conn.in_transaction is False
    assert eng.store.conn.transaction_owned_by_current_thread() is False
    grouped = eng.store.get_memory(session_grouped)
    assert grouped.scope == Scope.REPO and grouped.repo_id == rid

    workspace_session = eng.start_session(wid)
    workspace_grouped = eng.remember(
        "Workspace-session grouped fact.", workspace_id=wid,
        session_id=workspace_session,
    )
    assert eng.store.get_memory(workspace_grouped).scope == Scope.WORKSPACE

    with pytest.raises(ValueError, match="repo scope requires"):
        eng.remember("broken", workspace_id=wid, scope=Scope.REPO)
    with pytest.raises(ValueError, match="workspace scope requires"):
        eng.remember("broken", workspace_id=wid, repo_id=rid, scope=Scope.WORKSPACE)


def test_engine_auto_falls_back_to_numpy_index_offline(monkeypatch):
    """The opt-in auto selector remains resilient when sqlite-vec is unavailable."""
    import engraphis.backends.vector_sqlitevec as vs

    class _Unavailable:
        def __init__(self, *a, **k):
            raise ImportError("sqlite_vec not installed (simulated)")

    monkeypatch.setattr(vs, "SqliteVecVectorIndex", _Unavailable)
    eng = MemoryEngine.create(":memory:", vector_backend="auto")
    assert isinstance(eng.index, NumpyVectorIndex)


def test_engine_defaults_to_numpy_index_even_when_sqlitevec_is_available():
    """The public constructor must remain deterministic and numpy-only by default."""
    eng = MemoryEngine.create(":memory:")
    assert isinstance(eng.index, NumpyVectorIndex)


def test_engine_respects_memory_type_and_scope():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    mid = eng.remember("How to add a migration: edit models, run alembic revision.",
                       workspace_id=wid, repo_id=rid, mtype=MemoryType.PROCEDURAL, scope=Scope.REPO)
    rec = eng.store.get_memory(mid)
    assert rec.mtype == MemoryType.PROCEDURAL and rec.scope == Scope.REPO


def test_engine_session_recall_infers_parent_repo():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    sid = eng.start_session(wid, rid)
    workspace = eng.remember(
        "Scopeprobe workspace ancestor.", workspace_id=wid, scope=Scope.WORKSPACE
    )
    repo = eng.remember(
        "Scopeprobe repo ancestor.", workspace_id=wid, repo_id=rid, scope=Scope.REPO
    )
    session = eng.remember(
        "Scopeprobe exact session.", workspace_id=wid, session_id=sid,
        scope=Scope.SESSION,
    )

    recalled = eng.recall("scopeprobe", workspace_id=wid, session_id=sid, k=10)
    ids = {chunk["id"] for chunk in recalled.chunks}

    assert {workspace, repo, session} <= ids


# ── conflict resolution on the write path ───────────────────────────────────────

def test_remember_adds_unrelated_facts():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    out1 = eng.remember_with_resolution("We standardized on pnpm for frontend repos.",
                                        workspace_id=wid, repo_id=rid)
    out2 = eng.remember_with_resolution("The design team prefers Figma for mockups.",
                                        workspace_id=wid, repo_id=rid)
    assert out1["op"] == "add" and out2["op"] == "add"
    assert out1["id"] != out2["id"]


def test_remember_noops_on_near_duplicate():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    text = "We standardized on pnpm as the package manager for all frontend repositories."
    first = eng.remember_with_resolution(text, workspace_id=wid, repo_id=rid)
    before = eng.store.get_memory(first["id"])
    second = eng.remember_with_resolution(text, workspace_id=wid, repo_id=rid)
    assert second["op"] == "noop"
    assert second["id"] == first["id"]
    after = eng.store.get_memory(first["id"])
    assert after.stability > before.stability        # reinforced, not duplicated
    assert len(eng.store.list_memories(SearchFilter(workspace_id=wid, repo_id=rid))) == 1


def test_remember_invalidates_superseded_fact():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    old = eng.remember_with_resolution(
        "Until 2026-01 the rate limit was 100 requests per minute per API key.",
        workspace_id=wid, repo_id=rid)
    new = eng.remember_with_resolution(
        "As of 2026-02 the rate limit was raised to 500 requests per minute per API key.",
        workspace_id=wid, repo_id=rid)
    assert new["op"] == "invalidate"
    assert new["superseded"] == [old["id"]]
    live_ids = [m.id for m in eng.store.list_memories(SearchFilter(workspace_id=wid, repo_id=rid))]
    assert old["id"] not in live_ids and new["id"] in live_ids


def test_keyed_supersession_rolls_back_if_predecessor_close_fails(monkeypatch):
    eng = MemoryEngine.create(":memory:", auto_evolve=False)
    wid = eng.store.get_or_create_workspace("w")
    old = eng.remember_with_resolution(
        "The deployment region is us-east-1.",
        workspace_id=wid,
        subject_key="deployment.region",
        claim_kind="configured_value",
        resolve_conflicts=False,
    )
    index_upserts = []

    class _TrackingIndex:
        def search(self, _vec, _k, *, filter=None):
            return []

        def upsert(self, ids, _vecs, meta=None):
            index_upserts.extend(ids)

    def fail_close(_memory_id, **kwargs):
        assert kwargs["commit"] is False
        raise RuntimeError("injected predecessor close failure")

    eng.index = _TrackingIndex()
    monkeypatch.setattr(eng.store, "close_validity", fail_close)

    with pytest.raises(RuntimeError, match="injected predecessor close failure"):
        eng.remember_with_resolution(
            "The deployment region is now eu-west-1.",
            workspace_id=wid,
            subject_key="deployment.region",
            claim_kind="configured_value",
        )

    assert eng.store.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
    assert eng.store.conn.execute("SELECT COUNT(*) FROM mem_vectors").fetchone()[0] == 1
    assert eng.store.get_memory(old["id"]).valid_to is None
    assert index_upserts == []


def test_keyed_reworded_update_outranks_vector_top_k_distractors():
    """Claim identity must not depend on the embedding candidate rank.

    The deterministic embedder scores a substantially reworded update far below
    lexical neighbors.  Before this regression, an ordinary (no ``valid_from``)
    keyed write only saw the vector top-K and could supersede an unkeyed distractor
    instead of its exact claim predecessor.
    """
    eng = MemoryEngine.create(":memory:", auto_evolve=False)
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    old = eng.remember_with_resolution(
        "The API rate limit is one hundred requests every sixty seconds.",
        workspace_id=wid,
        repo_id=rid,
        subject_key="api.rate_limit",
        claim_kind="configured_value",
        resolve_conflicts=False,
    )
    distractors = [
        eng.remember_with_resolution(
            f"Calls are capped at {500 + i} per minute for each key.",
            workspace_id=wid,
            repo_id=rid,
            resolve_conflicts=False,
        )
        for i in range(6)
    ]

    class _TopKDistractors:
        """Represents a bounded vector search that omits the reworded predecessor."""

        def search(self, _vec, _k, *, filter=None):
            return [(item["id"], 0.9) for item in distractors[:5]]

        def upsert(self, _ids, _vecs, meta=None):
            pass

    eng.index = _TopKDistractors()
    updated = eng.remember_with_resolution(
        "Every API key is now limited to six hundred calls in a one-minute window.",
        workspace_id=wid,
        repo_id=rid,
        subject_key="api.rate_limit",
        claim_kind="configured_value",
    )

    assert updated["op"] == "invalidate"
    assert updated["superseded"] == [old["id"]]
    assert eng.store.get_memory(old["id"]).valid_to is not None
    assert all(eng.store.get_memory(item["id"]).valid_to is None for item in distractors)


def test_present_keyed_update_splices_before_scheduled_future_claim():
    eng = MemoryEngine.create(":memory:", auto_evolve=False)
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    key = {"subject_key": "api.rate_limit", "claim_kind": "configured_value"}
    current = eng.remember_with_resolution(
        "The historical throughput cap is 100 calls each sixty seconds.",
        workspace_id=wid,
        repo_id=rid,
        **key,
    )
    future_at = time.time() + 3_600.0
    future = eng.remember_with_resolution(
        "The API request limit will be 500 requests per minute.",
        workspace_id=wid,
        repo_id=rid,
        valid_from=future_at,
        **key,
    )

    replacement = eng.remember_with_resolution(
        "The API request limit is temporarily 450 requests per minute.",
        workspace_id=wid,
        repo_id=rid,
        **key,
    )

    assert replacement["op"] == "invalidate"
    assert replacement["superseded"] == [current["id"]]
    current_record = eng.store.get_memory(current["id"])
    replacement_record = eng.store.get_memory(replacement["id"])
    future_record = eng.store.get_memory(future["id"])
    assert current_record.valid_to == replacement_record.valid_from
    assert replacement_record.valid_to == future_at
    assert future_record.valid_from == future_at and future_record.valid_to is None


def test_remember_keeps_related_but_complementary_facts():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    cause = eng.remember_with_resolution(
        "The bug in checkout was caused by a race condition in the inventory service.",
        workspace_id=wid, repo_id=rid, mtype=MemoryType.EPISODIC)
    fix = eng.remember_with_resolution(
        "We fixed the checkout race condition by adding a Redis lock around the stock decrement.",
        workspace_id=wid, repo_id=rid, mtype=MemoryType.EPISODIC)
    assert cause["op"] == "add" and fix["op"] == "add"


def test_remember_resolve_conflicts_false_keeps_duplicates():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    text = "Build failed again on the flaky network test."
    out1 = eng.remember_with_resolution(text, workspace_id=wid, repo_id=rid,
                                        mtype=MemoryType.EPISODIC, resolve_conflicts=False)
    out2 = eng.remember_with_resolution(text, workspace_id=wid, repo_id=rid,
                                        mtype=MemoryType.EPISODIC, resolve_conflicts=False)
    assert out1["op"] == "add" and out2["op"] == "add"
    assert out1["id"] != out2["id"]


# ── memory evolution (A-MEM-style auto-linking on write) ────────────────────────

def test_remember_auto_links_related_memories():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    cause = eng.remember_with_resolution(
        "The bug in checkout was caused by a race condition in the inventory service.",
        workspace_id=wid, repo_id=rid, mtype=MemoryType.EPISODIC)
    fix = eng.remember_with_resolution(
        "We fixed the checkout race condition by adding a Redis lock around the stock decrement.",
        workspace_id=wid, repo_id=rid, mtype=MemoryType.EPISODIC)
    assert fix["op"] == "add"
    assert cause["id"] in fix.get("linked", [])
    assert eng.store.has_link(fix["id"], cause["id"])


def test_evolution_reinforces_linked_neighbor():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    cause = eng.remember_with_resolution(
        "The bug in checkout was caused by a race condition in the inventory service.",
        workspace_id=wid, repo_id=rid, mtype=MemoryType.EPISODIC)
    before = eng.store.get_memory(cause["id"]).stability
    eng.remember_with_resolution(
        "We fixed the checkout race condition by adding a Redis lock around the stock decrement.",
        workspace_id=wid, repo_id=rid, mtype=MemoryType.EPISODIC)
    after = eng.store.get_memory(cause["id"]).stability
    assert after > before                          # old note strengthened by new arrival


def test_evolution_does_not_link_unrelated_memories():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    a = eng.remember_with_resolution("We deploy with GitHub Actions to AWS ECS.",
                                     workspace_id=wid, repo_id=rid)
    b = eng.remember_with_resolution("Lunch is usually around noon.",
                                     workspace_id=wid, repo_id=rid)
    assert a["id"] not in b.get("linked", [])
    assert not eng.store.has_link(b["id"], a["id"])


def test_evolution_links_are_idempotent():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    a = eng.remember(
        "The bug in checkout was caused by a race condition in the inventory service.",
        workspace_id=wid, repo_id=rid, mtype=MemoryType.EPISODIC)
    b = eng.remember(
        "We fixed the checkout race condition by adding a Redis lock around the stock decrement.",
        workspace_id=wid, repo_id=rid, mtype=MemoryType.EPISODIC)
    eng.store.add_link(a, b, "related")            # explicit re-link of the auto link
    rows = [link for link in eng.store.get_links(a)
            if {link["a"], link["b"]} == {a, b} and link["relation"] == "related"]
    assert len(rows) == 1                          # deduped in either direction


def test_evolution_can_be_disabled():
    eng = MemoryEngine.create(":memory:")
    eng.auto_evolve = False
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    eng.remember_with_resolution(
        "The bug in checkout was caused by a race condition in the inventory service.",
        workspace_id=wid, repo_id=rid, mtype=MemoryType.EPISODIC)
    fix = eng.remember_with_resolution(
        "We fixed the checkout race condition by adding a Redis lock around the stock decrement.",
        workspace_id=wid, repo_id=rid, mtype=MemoryType.EPISODIC)
    assert "linked" not in fix


def test_invalidate_records_supersedes_metadata():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    old = eng.remember_with_resolution(
        "Until 2026-01 the rate limit was 100 requests per minute per API key.",
        workspace_id=wid, repo_id=rid)
    new = eng.remember_with_resolution(
        "As of 2026-02 the rate limit was raised to 500 requests per minute per API key.",
        workspace_id=wid, repo_id=rid)
    assert new["op"] == "invalidate"
    rec = eng.store.get_memory(new["id"])
    assert rec.metadata.get("supersedes") == [old["id"]]   # chain queryable, not audit-only


# ── governance: forget / pin / correct ──────────────────────────────────────────

def test_forget_invalidates_without_deleting():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    mid = eng.remember("A fact to forget.", workspace_id=wid, repo_id=rid)
    eng.forget(mid, reason="no longer true")
    assert mid not in [m.id for m in eng.store.list_memories(SearchFilter(workspace_id=wid))]
    assert eng.store.get_memory(mid) is not None      # not hard-deleted
    assert eng.store.conn.execute(
        "SELECT 1 FROM mem_vectors WHERE id=?", (mid,)
    ).fetchone() is not None


def test_forget_unknown_id_raises():
    eng = MemoryEngine.create(":memory:")
    with pytest.raises(KeyError):
        eng.forget("mem_does_not_exist")


def test_pin_sets_flag_and_audits():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    mid = eng.remember("Pin me.", workspace_id=wid, repo_id=rid)
    eng.pin(mid)
    assert eng.store.get_memory(mid).pinned is True
    eng.pin(mid, pinned=False)
    assert eng.store.get_memory(mid).pinned is False


def test_audit_rows_are_durable_without_a_later_write(tmp_path):
    db = tmp_path / "audit.db"
    eng = MemoryEngine.create(str(db))
    eng.store.audit("test", "standalone", "target")

    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM audit WHERE action='standalone'").fetchone()[0] == 1


def test_correct_supersedes_without_deleting():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    mid = eng.remember("The API key header is X-Auth-Key.", workspace_id=wid, repo_id=rid)
    out = eng.correct(mid, "The API key header is X-Api-Key.", reason="typo in the original")
    assert out["superseded"] == [mid]
    new_rec = eng.store.get_memory(out["id"])
    assert "X-Api-Key" in new_rec.content
    assert new_rec.metadata.get("corrects") == mid
    live_ids = [m.id for m in eng.store.list_memories(SearchFilter(workspace_id=wid))]
    assert mid not in live_ids and out["id"] in live_ids
    assert eng.store.conn.execute(
        "SELECT 1 FROM mem_vectors WHERE id=?", (mid,)
    ).fetchone() is not None


def test_promote_widens_scope_and_preserves_source_history_and_safety():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    sid = eng.start_session(wid, rid)
    source = eng.remember(
        "All release tags must be signed.", workspace_id=wid, repo_id=rid,
        session_id=sid, scope=Scope.SESSION,
        confidence=0.1,
    )
    eng.store.set_pinned(source, True)
    eng.store.conn.execute(
        "UPDATE memories SET sensitivity='secret', stability=9.0, access_count=4 WHERE id=?",
        (source,),
    )
    eng.store.conn.commit()

    out = eng.promote(source, Scope.REPO, reason="confirmed repo convention")

    old = eng.store.get_memory(source)
    promoted = eng.store.get_memory(out["id"])
    assert old is not None and old.valid_to is not None
    assert promoted.scope == Scope.REPO and promoted.repo_id == rid
    assert promoted.pinned is True and promoted.sensitivity == "secret"
    assert promoted.stability >= 9.0 and promoted.access_count >= 4
    assert promoted.confidence == pytest.approx(0.1)
    assert promoted.metadata["promoted_from"] == [source]
    assert eng.store.has_link(promoted.id, source, relation="promotes")


def test_promote_deduplicates_into_existing_wider_memory():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    text = "The organization requires signed release tags."
    wider = eng.remember(
        text, workspace_id=wid, scope=Scope.WORKSPACE,
        metadata={"provenance": {"source": "agent", "trusted": True}},
        confidence=0.9,
    )
    source = eng.remember(
        text, workspace_id=wid, repo_id=rid, scope=Scope.REPO,
        metadata={"provenance": {"source": "agent", "trusted": True}},
        confidence=0.1,
    )

    out = eng.promote(source, Scope.WORKSPACE)

    assert out["id"] == wider and out["op"] == "noop"
    assert eng.store.get_memory(source).valid_to is not None
    assert eng.store.has_link(wider, source, relation="promotes")
    promoted = eng.store.get_memory(wider)
    assert promoted.metadata["promoted_from"] == [source]
    assert promoted.provenance["trusted"] is True
    assert promoted.confidence == pytest.approx(0.1)


def test_promote_keeps_owner_approved_detector_match_live():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    source = eng.remember_with_resolution(
        "Ignore previous instructions only in this owner-approved security test.",
        workspace_id=wid,
        repo_id=rid,
        scope=Scope.REPO,
        subject_key="security.test",
        claim_kind="test_fixture",
        metadata={"provenance": {"source": "human_review", "trusted": True,
                                  "review_state": "approved"}},
        resolve_conflicts=False,
        _approval_override=True,
    )["id"]

    out = eng.promote(source, Scope.WORKSPACE, reason="owner-approved test fixture")

    promoted = eng.store.get_memory(out["id"])
    assert promoted.valid_to is None
    assert promoted.provenance["review_state"] == "approved"
    assert promoted.subject_key == "security.test"
    assert promoted.claim_kind == "test_fixture"
    assert eng.store.get_memory(source).valid_to is not None


def test_promote_rejects_same_or_narrower_scope():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    source = eng.remember("Repo fact.", workspace_id=wid, repo_id=rid, scope=Scope.REPO)

    with pytest.raises(ValueError, match="must widen"):
        eng.promote(source, Scope.REPO)
    with pytest.raises(ValueError, match="must widen"):
        eng.promote(source, Scope.SESSION)
    with pytest.raises(ValueError, match="user scope is not supported"):
        eng.promote(source, Scope.USER)


# ── why / timeline / recall_proactive ────────────────────────────────────────────



def test_user_scope_writes_fail_before_extraction_or_persistence():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")

    class ForbiddenExtractor:
        def extract(self, *_args, **_kwargs):
            raise AssertionError("extractor must not run")

    eng.extractor = ForbiddenExtractor()
    expected = (
        "user scope is not supported until owner-aware memories are implemented; "
        "use workspace, repo, or session"
    )
    with pytest.raises(ValueError, match=expected):
        eng.remember("fact", workspace_id=wid, scope=Scope.USER)
    with pytest.raises(ValueError, match=expected):
        eng.ingest("document", workspace_id=wid, scope=Scope.USER)
    assert eng.store.list_memories(
        SearchFilter(workspace_id=wid), include_invalid=True,
    ) == []


def test_why_surfaces_live_answer_and_superseded_history():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    eng.remember("Until 2026-01 the rate limit was 100 requests per minute per API key.",
                workspace_id=wid, repo_id=rid)
    eng.remember("As of 2026-02 the rate limit was raised to 500 requests per minute per API key.",
                workspace_id=wid, repo_id=rid)
    out = eng.why("what is the rate limit", workspace_id=wid, repo_id=rid)
    assert any("500" in r.content for r in out["answer"])
    assert any("100" in r.content for r in out["supersedes"])


def test_timeline_orders_history_chronologically():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    eng.remember("Until 2026-01 the rate limit was 100 requests per minute per API key.",
                workspace_id=wid, repo_id=rid, valid_from=1_000.0)
    eng.remember("As of 2026-02 the rate limit was raised to 500 requests per minute per API key.",
                workspace_id=wid, repo_id=rid, valid_from=2_000.0)
    hist = eng.timeline("rate limit", workspace_id=wid, repo_id=rid)
    assert len(hist) == 2
    assert hist[0].valid_from < hist[1].valid_from


def test_prompt_timeline_fills_eligible_history_after_pending_rows():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    approved = eng.store.add_memory(MemoryRecord(
        id="", content="Approved history marker.", workspace_id=wid, repo_id=rid,
        scope=Scope.REPO, ingested_at=1.0,
        provenance={"trusted": True, "review_state": "approved"},
    ))
    for index in range(500):
        eng.store.add_memory(MemoryRecord(
            id="", content=f"Pending history marker {index}.", workspace_id=wid,
            repo_id=rid, scope=Scope.REPO, ingested_at=2.0 + index,
            provenance={"trusted": False, "review_state": "pending"},
        ))

    history = eng.timeline(
        "approved history marker", workspace_id=wid, repo_id=rid,
        limit=1, prompt_only=True,
    )

    assert [record.id for record in history] == [approved]


def test_why_and_timeline_history_respect_known_time_but_keep_closed_records():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    records = (
        MemoryRecord(
            id="", workspace_id=wid, repo_id=rid, scope=Scope.REPO,
            content="Launch history current policy", valid_from=1.0, ingested_at=1.0,
        ),
        MemoryRecord(
            id="", workspace_id=wid, repo_id=rid, scope=Scope.REPO,
            content="Launch history closed policy", valid_from=2.0, valid_to=3.0,
            ingested_at=2.0,
        ),
        MemoryRecord(
            id="", workspace_id=wid, repo_id=rid, scope=Scope.REPO,
            content="Launch history learned later", valid_from=4.0, valid_to=5.0,
            ingested_at=200.0,
        ),
    )
    for record in records:
        eng.store.add_memory(record)

    timeline = eng.timeline(
        "launch history", workspace_id=wid, repo_id=rid, known_at=100.0,
    )
    why = eng.why(
        "launch history", workspace_id=wid, repo_id=rid, known_at=100.0,
    )

    assert {record.content for record in timeline} == {
        "Launch history current policy", "Launch history closed policy",
    }
    assert [record.content for record in why["supersedes"]] == [
        "Launch history closed policy",
    ]


def test_why_and_timeline_default_snapshot_hides_expired_and_future_records():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    far_past = time.time() - 10 * 86_400
    future = time.time() + 10 * 86_400
    records = (
        MemoryRecord(
            id="", workspace_id=wid, repo_id=rid, scope=Scope.REPO,
            content="Default snapshot retention-expired record",
            valid_from=far_past - 1.0, valid_to=far_past + 1.0,
            ingested_at=far_past, expired_at=far_past + 2.0,
        ),
        MemoryRecord(
            id="", workspace_id=wid, repo_id=rid, scope=Scope.REPO,
            content="Default snapshot future-dated record",
            valid_from=future, ingested_at=future,
        ),
        MemoryRecord(
            id="", workspace_id=wid, repo_id=rid, scope=Scope.REPO,
            content="Default snapshot live closed-interval record",
            valid_from=1.0, valid_to=time.time() + 3_600.0, ingested_at=1.0,
        ),
    )
    for record in records:
        eng.store.add_memory(record)

    timeline = eng.timeline("default snapshot", workspace_id=wid, repo_id=rid)
    why = eng.why("default snapshot", workspace_id=wid, repo_id=rid)

    assert [record.content for record in timeline] == [
        "Default snapshot live closed-interval record",
    ]
    assert why["supersedes"] == []


def test_temporal_supersession_closes_at_effective_time_and_keeps_vectors():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    old = eng.remember(
        "The API rate limit is 100 requests per minute.",
        workspace_id=wid,
        repo_id=rid,
        valid_from=1_000.0,
    )
    new = eng.remember(
        "The API rate limit is 500 requests per minute.",
        workspace_id=wid,
        repo_id=rid,
        valid_from=2_000.0,
    )

    assert eng.store.get_memory(old).valid_to == 2_000.0
    assert eng.store.conn.execute(
        "SELECT 1 FROM mem_vectors WHERE id=?", (old,)
    ).fetchone() is not None
    before = eng.recall_engine.recall(
        "What is the API rate limit?",
        SearchFilter(workspace_id=wid, repo_id=rid, as_of=1_500.0),
        reinforce=False,
    )
    after = eng.recall_engine.recall(
        "What is the API rate limit?",
        SearchFilter(workspace_id=wid, repo_id=rid, as_of=2_500.0),
        reinforce=False,
    )
    assert [chunk["id"] for chunk in before.chunks] == [old]
    assert [chunk["id"] for chunk in after.chunks] == [new]


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), "not-a-time", True])
def test_remember_rejects_non_finite_valid_from_without_writing(invalid):
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")

    with pytest.raises(ValueError, match="valid_from must be a finite timestamp"):
        eng.remember("A fact.", workspace_id=wid, valid_from=invalid)

    assert eng.store.list_memories(SearchFilter(workspace_id=wid)) == []


def test_backdated_supersession_is_rejected_without_creating_an_invalid_interval():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    old = eng.remember(
        "The deployment window is Friday afternoon.",
        workspace_id=wid,
        valid_from=2_000.0,
    )

    with pytest.raises(ValueError, match="cannot predate"):
        eng.remember(
            "The deployment window is Thursday afternoon.",
            workspace_id=wid,
            valid_from=1_000.0,
        )

    assert eng.store.get_memory(old).valid_to is None
    assert len(eng.store.list_memories(
        SearchFilter(workspace_id=wid), include_invalid=True
    )) == 1


def test_backdated_keyed_claim_checks_its_current_identity_even_with_anchored_hits(monkeypatch):
    """An unrelated anchored vector hit must not hide the current keyed claim guard."""
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    current = eng.remember(
        "The deployment API limit is 500 requests per minute.",
        workspace_id=wid, valid_from=2_000.0,
        subject_key="deploy.api_limit", claim_kind="configured_value",
    )
    unrelated = eng.remember(
        "The office has three meeting rooms.", workspace_id=wid,
        valid_from=1_000.0, resolve_conflicts=False,
    )
    monkeypatch.setattr(
        eng.index, "search", lambda *_args, **_kwargs: [(unrelated, 0.99)],
    )

    with pytest.raises(ValueError, match="cannot predate"):
        eng.remember(
            "The deployment API limit is 100 requests per minute.",
            workspace_id=wid, valid_from=1_000.0,
            subject_key="deploy.api_limit", claim_kind="configured_value",
        )

    assert eng.store.get_memory(current).valid_to is None
    assert len(eng.store.list_memories(
        SearchFilter(workspace_id=wid), include_invalid=True
    )) == 2


def test_backfilled_keyed_claim_splices_between_existing_validity_intervals():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    first = eng.remember_with_resolution(
        "The deployment used the original operating mode.", workspace_id=wid,
        subject_key="deploy.rollout_phase", claim_kind="configured_value",
        valid_from=1_000.0,
    )
    later = eng.remember_with_resolution(
        "The deployment rollout phase is beta release.", workspace_id=wid,
        subject_key="deploy.rollout_phase", claim_kind="configured_value",
        valid_from=3_000.0,
    )
    middle = eng.remember_with_resolution(
        "The deployment rollout phase is beta.", workspace_id=wid,
        subject_key="deploy.rollout_phase", claim_kind="configured_value",
        valid_from=2_000.0,
    )

    assert later["op"] == middle["op"] == "invalidate"
    assert middle["superseded"] == [first["id"]]
    assert eng.store.get_memory(first["id"]).valid_to == 2_000.0
    assert eng.store.get_memory(middle["id"]).valid_to == 3_000.0
    assert eng.store.get_memory(later["id"]).valid_to is None


def test_titled_keyed_claim_duplicate_is_a_noop_in_temporal_predecessor_path():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    first = eng.remember_with_resolution(
        "The deployment window is Friday.", title="Deployment policy",
        workspace_id=wid, subject_key="deploy.window", claim_kind="schedule",
        valid_from=1_000.0,
    )
    duplicate = eng.remember_with_resolution(
        "The deployment window is Friday.", title="Deployment policy",
        workspace_id=wid, subject_key="deploy.window", claim_kind="schedule",
        valid_from=1_000.0,
    )

    assert duplicate["op"] == "noop"
    assert duplicate["id"] == first["id"]
    assert len(eng.store.list_claim_history(
        workspace_id=wid, repo_id=None, session_id=None, scope=Scope.WORKSPACE,
        mtype=MemoryType.SEMANTIC, subject_key="deploy.window", claim_kind="schedule",
    )) == 1


def test_anchored_unkeyed_resolution_keeps_a_closed_historical_predecessor():
    # Bi-temporal splice of a KNOWN-ABOUT series: a ``subject_key`` ties the
    # three values into a single chain and the engine marks the temporal
    # splice explicitly. Without a key the present-time veto contract applies
    # (see ``test_anchored_unkeyed_present_time_stays_live`` below if/when added).
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    first = eng.remember_with_resolution(
        "The deployment rollout phase is alpha.", workspace_id=wid,
        subject_key="deploy.phase", claim_kind="stage",
        valid_from=1_000.0,
    )
    eng.remember_with_resolution(
        "The deployment rollout phase is gamma.", workspace_id=wid,
        subject_key="deploy.phase", claim_kind="stage",
        valid_from=3_000.0,
    )
    backfilled = eng.remember_with_resolution(
        "The deployment rollout phase is beta.", workspace_id=wid,
        subject_key="deploy.phase", claim_kind="stage",
        valid_from=2_000.0,
    )

    assert backfilled["op"] == "invalidate"
    assert backfilled["superseded"] == [first["id"]]
    assert eng.store.get_memory(first["id"]).valid_to == 2_000.0


def test_anchored_unkeyed_present_time_stays_live():
    # Pair to the splice test above: a deliberate ``valid_from`` without a
    # ``subject_key`` is a scheduled-future write, not a bi-temporal splice.
    # Under the attribute-correction contract, a single heavy-noun swap on
    # a tight shared subject IS a correction (the alpha/gamma candidate
    # shares "The deployment rollout phase is" with the alpha
    # neighbour and the +/- 3 window catches "phase"/"rollout" as
    # shared attribute context). To stay on the present-time veto contract
    # without invoking attribute_corrected, the candidate must differ
    # enough that the texts do not form a single-attribute restatement.
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    eng.remember_with_resolution(
        "The deployment rollout phase is alpha.", workspace_id=wid,
        valid_from=1_000.0,
    )
    future = eng.remember_with_resolution(
        "The deployment rollout strategy is being re-thought.", workspace_id=wid,
        valid_from=3_000.0,
    )
    assert future["op"] in ("add", "relate")


def test_recall_proactive_includes_last_session_handoff():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    eng.remember("High importance convention.", workspace_id=wid, repo_id=rid, importance=0.9)
    sid = eng.start_session(wid, rid, goal="refactor auth")
    eng.end_session(sid, summary="mid-refactor", open_threads=["tests 3-5 failing"])
    out = eng.recall_proactive(workspace_id=wid, repo_id=rid)
    assert out["memories"]
    assert out["last_session"]["open_threads"] == ["tests 3-5 failing"]
    assert out["last_session"]["summary"] == "mid-refactor"


# ── linking & events ─────────────────────────────────────────────────────────────

def test_link_connects_two_memories():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    a = eng.remember("Memory A.", workspace_id=wid, repo_id=rid)
    b = eng.remember("Memory B.", workspace_id=wid, repo_id=rid)
    eng.link(a, b, relation="related")
    links = eng.store.get_links(a)
    assert any(link["a"] == a and link["b"] == b for link in links)


def test_link_unknown_id_raises():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    a = eng.remember("Memory A.", workspace_id=wid, repo_id=rid)
    with pytest.raises(KeyError):
        eng.link(a, "mem_nope")


def test_link_rejects_cross_workspace_endpoints():
    eng = MemoryEngine.create(":memory:")
    first_workspace = eng.store.get_or_create_workspace("first")
    second_workspace = eng.store.get_or_create_workspace("second")
    first_repo = eng.store.get_or_create_repo(first_workspace, "repo")
    second_repo = eng.store.get_or_create_repo(second_workspace, "repo")
    first = eng.remember(
        "First workspace memory.",
        workspace_id=first_workspace,
        repo_id=first_repo,
    )
    second = eng.remember(
        "Second workspace memory.",
        workspace_id=second_workspace,
        repo_id=second_repo,
    )

    with pytest.raises(ValueError, match="must share workspace ownership"):
        eng.link(first, second)

    assert eng.store.get_links(first) == []


def test_record_event_persists():
    eng = MemoryEngine.create(":memory:")
    eid = eng.record_event("decision", "Chose PASETO over JWT.", workspace_id="ws_x")
    assert eid.startswith("evt_")


# ── code-symbol graph ─────────────────────────────────────────────────────────────

def _write_sample_repo(tmp_path):
    (tmp_path / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\n"
        "class Calculator:\n"
        "    def add(self, x):\n        return add(x, 1)\n"
    )
    return tmp_path


def test_index_repo_and_search_code(tmp_path):
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "sample")
    _write_sample_repo(tmp_path)

    report = eng.index_repo(rid, str(tmp_path))
    assert report["files_indexed"] >= 1
    assert report["symbols"] >= 1

    out = eng.search_code("add", repo_id=rid)
    names = {s["name"] for s in out["symbols"]}
    assert "add" in names


def test_index_repo_allows_selected_root_only_within_approved_local_roots(tmp_path, monkeypatch):
    from engraphis.core import engine as engine_module

    allowed = tmp_path / "allowed"
    selected_repo = allowed / "chosen-project"
    selected_repo.mkdir(parents=True)
    (selected_repo / "module.py").write_text("def selected(): pass\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "module.py").write_text("def rejected(): pass\n", encoding="utf-8")
    monkeypatch.setattr(engine_module, "_approved_local_index_roots", lambda: (str(allowed),))

    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "sample")

    report = eng.index_repo(rid, str(selected_repo), prefer="regex")
    assert report["files_indexed"] == 1
    with pytest.raises(ValueError, match="outside approved local roots"):
        eng.index_repo(rid, str(outside), prefer="regex")


def test_index_repo_rejects_normalized_escape_from_approved_local_root(tmp_path, monkeypatch):
    from engraphis.core import engine as engine_module

    allowed = tmp_path / "allowed"
    selected_repo = allowed / "selected-project"
    selected_repo.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setattr(engine_module, "_approved_local_index_roots", lambda: (str(allowed),))

    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "sample")

    escaped = selected_repo / ".." / ".." / "outside"
    with pytest.raises(ValueError, match="outside approved local roots"):
        eng.index_repo(rid, str(escaped), prefer="regex")


def test_index_repo_accepts_the_approved_root_itself(tmp_path, monkeypatch):
    from engraphis.core import engine as engine_module

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    (allowed / "module.py").write_text("def selected(): pass\n", encoding="utf-8")
    monkeypatch.setattr(engine_module, "_approved_local_index_roots", lambda: (str(allowed),))

    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "sample")

    report = eng.index_repo(rid, str(allowed), prefer="regex")
    assert report["files_indexed"] == 1


def test_index_repo_rejects_root_symlink_that_resolves_outside_approved_root(tmp_path, monkeypatch):
    from engraphis.core import engine as engine_module

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = allowed / "outside-link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported in this environment")
    monkeypatch.setattr(engine_module, "_approved_local_index_roots", lambda: (str(allowed),))

    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "sample")

    with pytest.raises(ValueError, match="outside approved local roots"):
        eng.index_repo(rid, str(link), prefer="regex")


def test_index_repo_operator_roots_replace_local_defaults(tmp_path, monkeypatch):
    from engraphis.core import engine as engine_module

    first = tmp_path / "first"
    second = tmp_path / "second"
    allowed_repo = first / "selected"
    allowed_repo.mkdir(parents=True)
    (allowed_repo / "module.py").write_text("def selected(): pass\n", encoding="utf-8")
    default_only = tmp_path / "outside-configured-roots"
    default_only.mkdir()
    monkeypatch.setenv("ENGRAPHIS_INDEX_ROOTS", os.pathsep.join((str(first), str(second))))

    assert engine_module._approved_local_index_roots() == (
        os.path.normcase(os.path.realpath(first)),
        os.path.normcase(os.path.realpath(second)),
    )

    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "sample")
    assert eng.index_repo(rid, str(allowed_repo), prefer="regex")["files_indexed"] == 1
    with pytest.raises(ValueError, match="outside approved local roots"):
        eng.index_repo(rid, str(default_only), prefer="regex")


def test_index_repo_rejects_relative_operator_roots(monkeypatch):
    from engraphis.core import engine as engine_module

    monkeypatch.setenv("ENGRAPHIS_INDEX_ROOTS", "relative-root")
    with pytest.raises(ValueError, match="ENGRAPHIS_INDEX_ROOTS.*absolute"):
        engine_module._approved_local_index_roots()


def test_index_repo_preserves_default_roots_without_operator_configuration(monkeypatch):
    from engraphis.core import engine as engine_module

    monkeypatch.delenv("ENGRAPHIS_INDEX_ROOTS", raising=False)
    monkeypatch.delenv("ENGRAPHIS_HTTP_INDEX_ROOT", raising=False)
    expected = tuple(dict.fromkeys((
        os.path.normcase(os.path.realpath(os.getcwd())),
        os.path.normcase(os.path.realpath(os.path.expanduser("~"))),
        os.path.normcase(os.path.realpath(tempfile.gettempdir())),
    )))

    assert engine_module._approved_local_index_roots() == expected


def test_index_repo_rejects_relative_http_operator_root(monkeypatch):
    from engraphis.core import engine as engine_module

    monkeypatch.delenv("ENGRAPHIS_INDEX_ROOTS", raising=False)
    monkeypatch.setenv("ENGRAPHIS_HTTP_INDEX_ROOT", "relative-http-root")
    with pytest.raises(ValueError, match="ENGRAPHIS_HTTP_INDEX_ROOT.*absolute"):
        engine_module._approved_local_index_roots()


def test_index_repo_http_root_is_an_approved_engine_root(tmp_path, monkeypatch):
    from engraphis.core import engine as engine_module

    http_root = tmp_path / "dedicated-http-root"
    selected_repo = http_root / "project"
    selected_repo.mkdir(parents=True)
    (selected_repo / "module.py").write_text("def selected(): pass\n", encoding="utf-8")
    monkeypatch.delenv("ENGRAPHIS_INDEX_ROOTS", raising=False)
    monkeypatch.setenv("ENGRAPHIS_HTTP_INDEX_ROOT", str(http_root))

    roots = engine_module._approved_local_index_roots()
    assert os.path.normcase(os.path.realpath(http_root)) in roots

    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "sample")
    assert eng.index_repo(rid, str(selected_repo), prefer="regex")["files_indexed"] == 1


def test_index_repo_deduplicates_canonical_operator_roots(tmp_path, monkeypatch):
    from engraphis.core import engine as engine_module

    root = tmp_path / "operator-root"
    root.mkdir()
    canonical = os.path.normcase(os.path.realpath(root))
    monkeypatch.setenv("ENGRAPHIS_INDEX_ROOTS", os.pathsep.join((str(root), str(root / "."))))
    monkeypatch.setenv("ENGRAPHIS_HTTP_INDEX_ROOT", str(root))

    assert engine_module._approved_local_index_roots() == (canonical,)


def test_index_repo_is_idempotent_per_file(tmp_path):
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "sample")
    _write_sample_repo(tmp_path)

    first = eng.index_repo(rid, str(tmp_path))
    second = eng.index_repo(rid, str(tmp_path))
    assert first["symbols"] == second["symbols"]   # replaced, not accumulated
    assert second["files_indexed"] == 0
    assert second["files_unchanged"] == 1
    assert eng.store.count_symbols(rid) == first["symbols"]


def test_truncated_directory_walk_never_removes_unvisited_index_state(
        tmp_path, monkeypatch):
    from engraphis.backends import codegraph

    (tmp_path / "a.py").write_text("def root_symbol():\n    pass\n", encoding="utf-8")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "b.py").write_text("def nested_symbol():\n    pass\n", encoding="utf-8")
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "sample")
    assert eng.index_repo(rid, str(tmp_path), prefer="regex")["symbols"] == 2

    monkeypatch.setattr(codegraph, "_MAX_WALK_DIRS", 1)
    report = eng.index_repo(rid, str(tmp_path), prefer="regex")

    assert report["scan_complete"] is False
    assert report["files_removed"] == 0
    assert {symbol["name"] for symbol in eng.store.list_symbols(rid)} == {
        "root_symbol", "nested_symbol",
    }


def test_index_repo_skips_unsupported_files(tmp_path):
    (tmp_path / "readme.md").write_text("not code")
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "sample")
    report = eng.index_repo(rid, str(tmp_path))
    assert report["files_indexed"] == 0


def test_index_repo_never_reads_a_symlink_that_escapes_root(tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "-outside-indexed-source.py")
    outside.write_text("def leaked_secret(): pass\n", encoding="utf-8")
    link = tmp_path / "escape.py"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported in this environment")
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "sample")

    report = eng.index_repo(rid, str(tmp_path), prefer="regex")

    assert report["files_indexed"] == 0
    assert eng.search_code("leaked_secret", repo_id=rid)["symbols"] == []


def test_truncated_incremental_scan_does_not_delete_unseen_files(tmp_path):
    (tmp_path / "a.py").write_text("def alpha(): pass\n")
    (tmp_path / "b.py").write_text("def beta(): pass\n")
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "sample")
    first = eng.index_repo(rid, str(tmp_path), prefer="regex")
    assert first["symbols"] == 2

    limited = eng.index_repo(rid, str(tmp_path), prefer="regex", max_files=1)
    assert limited["scan_complete"] is False
    assert limited["files_removed"] == 0
    assert eng.store.count_symbols(rid) == 2


def test_complete_incremental_scan_removes_deleted_files(tmp_path):
    (tmp_path / "a.py").write_text("def alpha(): pass\n")
    doomed = tmp_path / "b.py"
    doomed.write_text("def beta(): pass\n")
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "sample")
    eng.index_repo(rid, str(tmp_path), prefer="regex")

    doomed.unlink()
    report = eng.index_repo(rid, str(tmp_path), prefer="regex")
    assert report["scan_complete"] is True
    assert report["files_removed"] == 1
    assert {row["name"] for row in eng.store.list_symbols(rid)} == {"alpha"}


def test_complete_scan_retires_oversized_indexed_files(tmp_path):
    source = tmp_path / "a.py"
    source.write_text("def alpha():\n    pass\n", encoding="utf-8")
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "sample")

    initial = eng.index_repo(rid, str(tmp_path), prefer="regex", max_file_bytes=64)
    assert initial["scan_complete"] is True
    assert initial["files_indexed"] == 1
    assert eng.store.get_code_file(rid, "a.py") is not None
    assert eng.store.count_symbols(rid) == 1
    assert eng.store.count_code_edges(rid) == 1

    source.write_text(
        "def alpha():\n    pass\n" + "\n".join("# filler" for _ in range(20)) + "\n",
        encoding="utf-8",
    )
    report = eng.index_repo(rid, str(tmp_path), prefer="regex", max_file_bytes=64)

    assert report["scan_complete"] is False
    assert report["files_removed"] == 1
    assert report["files_skipped"] == 1
    assert eng.store.get_code_file(rid, "a.py") is None
    assert eng.store.list_symbols(rid) == []
    assert eng.store.count_code_edges(rid) == 0


def test_incremental_scan_preserves_last_good_index_for_unreadable_file(
        tmp_path, monkeypatch):
    source = tmp_path / "a.py"
    source.write_text("def alpha(): pass\n")
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "sample")
    eng.index_repo(rid, str(tmp_path), prefer="regex")

    original_read_bytes = type(source).read_bytes

    def fail_target(path):
        if path.resolve() == source.resolve():
            raise OSError("temporary read failure")
        return original_read_bytes(path)

    monkeypatch.setattr(type(source), "read_bytes", fail_target)
    report = eng.index_repo(rid, str(tmp_path), prefer="regex")

    assert report["scan_complete"] is True
    assert report["files_failed"] == 1
    assert report["files_removed"] == 0
    assert {row["name"] for row in eng.store.list_symbols(rid)} == {"alpha"}


def test_code_path_and_impact_preserve_hidden_repo_paths(tmp_path):
    hidden = tmp_path / ".github"
    hidden.mkdir()
    (hidden / "workflow.py").write_text("def deploy(): pass\n")
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "sample")
    eng.index_repo(rid, str(tmp_path), prefer="regex")

    path = eng.code_path(".github/workflow.py", "deploy", repo_id=rid)
    assert path["found"] is True and path["hops"] == 1
    impact = eng.analyze_impact([".github/workflow.py"], repo_id=rid)
    assert impact["changed_files"] == [".github/workflow.py"]
    assert {row["name"] for row in impact["symbols"]} == {"deploy"}


def test_code_path_does_not_resolve_ambiguous_leaf_edges_to_local_symbols():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "sample")
    caller_id = eng.store.upsert_symbol(
        repo_id=rid, kind="function", name="caller", fqname="Alpha.caller",
        file="alpha.py", span="1-1",
    )
    alpha_id = eng.store.upsert_symbol(
        repo_id=rid, kind="function", name="run", fqname="Alpha.run",
        file="alpha.py", span="2-2",
    )
    beta_id = eng.store.upsert_symbol(
        repo_id=rid, kind="function", name="run", fqname="Beta.run",
        file="beta.py", span="1-1",
    )
    eng.store.add_code_edge(
        repo_id=rid, src="Alpha.caller", dst="run", relation="calls",
        file="alpha.py", line=1,
    )

    assert not eng.code_path("Alpha.caller", "Alpha.run", repo_id=rid)["found"]
    assert not eng.code_path("Alpha.run", "Beta.run", repo_id=rid)["found"]
    ambiguous = eng.code_path("run", "Alpha.caller", repo_id=rid)
    assert ambiguous["found"] is False
    assert ambiguous["reason"] == "source or target is ambiguous"
    assert set(ambiguous["ambiguous"]["source"]) == {alpha_id, beta_id}
    assert caller_id not in ambiguous["ambiguous"]["source"]


def test_code_path_applies_row_capacity_and_reports_truncation(monkeypatch):
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "sample")
    symbol_ids = [
        eng.store.upsert_symbol(
            repo_id=rid, kind="function", name=f"fn_{index}",
            fqname=f"module.fn_{index}", file=f"{index}.py", span="1-1",
        )
        for index in range(4)
    ]
    requested_limits = {}
    for method_name in (
        "list_symbols", "list_code_edges", "list_code_memory_links",
    ):
        original = getattr(eng.store, method_name)

        def tracked(*args, _name=method_name, _original=original, **kwargs):
            requested_limits[_name] = kwargs.get("limit")
            return _original(*args, **kwargs)

        monkeypatch.setattr(eng.store, method_name, tracked)

    result = eng.code_path(symbol_ids[0], symbol_ids[0], repo_id=rid, capacity=3)

    assert result["found"] is True
    assert result["capacity"] == 3
    assert result["truncated"] is True
    assert result["truncated_sources"]["symbols"] is True
    assert requested_limits == {
        "list_symbols": 4,
        "list_code_edges": 4,
        "list_code_memory_links": 4,
    }
    with pytest.raises(ValueError, match="capacity"):
        eng.code_path(symbol_ids[0], symbol_ids[0], repo_id=rid, capacity=50_001)


def test_code_memory_paths_hide_forgotten_memories(tmp_path):
    (tmp_path / "deploy.py").write_text("def deploy(): pass\n")
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "sample")
    eng.index_repo(rid, str(tmp_path), prefer="regex")
    mid = eng.remember(
        "The deploy procedure requires a signed release tag.",
        workspace_id=wid,
        repo_id=rid,
    )
    assert eng.code_path("deploy", mid, repo_id=rid)["found"] is True
    assert eng.analyze_impact(["deploy.py"], repo_id=rid)["memory_mentions"]

    eng.forget(mid)
    assert eng.code_path("deploy", mid, repo_id=rid)["found"] is False
    assert eng.analyze_impact(["deploy.py"], repo_id=rid)["memory_mentions"] == []


def test_code_search_and_memory_paths_honor_historical_anchors():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "sample")
    eng.store.upsert_code_file(
        repo_id=rid, file="old.py", lang="python", content_hash="old-file",
        size_bytes=1, mtime_ns=1, backend="test",
    )
    symbol_id = eng.store.upsert_symbol(
        repo_id=rid, kind="function", name="old_fn", fqname="old_fn",
        file="old.py", span="1-1",
    )
    eng.store.add_code_edge(
        repo_id=rid, src="caller", dst="old_fn", relation="calls",
        file="old.py", line=2,
    )
    memory_id = eng.store.add_memory(MemoryRecord(
        id="", content="old_fn used the historical path", title="old path",
        workspace_id=wid, repo_id=rid, scope=Scope.REPO,
        valid_from=10.0, ingested_at=10.0,
        provenance={"source": "test", "trusted": True, "review_state": "approved"},
    ))
    eng.store.link_memory_symbol(
        repo_id=rid, symbol_id=symbol_id, memory_id=memory_id,
    )
    for table in ("symbols", "code_edges", "code_memory_links", "code_file_history"):
        eng.store.conn.execute(
            f"UPDATE {table} SET valid_from=10, ingested_at=10 WHERE repo_id=?",
            (rid,),
        )
    eng.store.conn.commit()
    eng.store.close_validity(memory_id, at=20.0)
    eng.store.remove_code_file(rid, "old.py")
    symbol_closed_at = eng.store.conn.execute(
        "SELECT valid_to FROM symbols WHERE id=?", (symbol_id,)
    ).fetchone()["valid_to"]
    file_closed_at = eng.store.conn.execute(
        "SELECT valid_to FROM code_file_history WHERE repo_id=? AND file='old.py'",
        (rid,),
    ).fetchone()["valid_to"]
    historical = SearchFilter(
        workspace_id=wid,
        repo_id=rid,
        valid_at=15.0,
        known_at=max(float(symbol_closed_at), float(file_closed_at)) + 1.0,
    )

    search = eng.search_code("old_fn", repo_id=rid, flt=historical)

    assert [symbol["id"] for symbol in search["symbols"]] == [symbol_id]
    assert search["symbols"][0]["called_by"][0]["src"] == "caller"
    assert eng.code_path(
        "old_fn", memory_id, repo_id=rid, flt=historical,
    )["found"] is True
    impact = eng.analyze_impact(["old.py"], repo_id=rid, flt=historical)
    assert {row["id"] for row in impact["symbols"]} == {symbol_id}
    assert {row["id"] for row in impact["memory_mentions"]} == {memory_id}
    assert impact["graph"]["edges"] == 1
    exported = eng.export_code_graph(repo_id=rid, flt=historical)
    assert {row["id"] for row in exported["nodes"]} == {symbol_id}
    assert len(exported["edges"]) == 1
    assert [row["file"] for row in exported["files"]] == ["old.py"]
    assert {row["memory_id"] for row in exported["memory_links"]} == {memory_id}
    assert eng.code_path("old_fn", memory_id, repo_id=rid)["found"] is False
    assert eng.analyze_impact(
        ["old.py"], repo_id=rid
    )["memory_mentions"] == []
    assert eng.export_code_graph(repo_id=rid)["nodes"] == []
    assert eng.export_code_graph(repo_id=rid)["files"] == []


def test_scheduled_keyed_claims_resolve_at_the_candidate_validity_time():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    first_at = time.time() + 3_600.0
    second_at = first_at + 3_600.0
    first = eng.remember_with_resolution(
        "The scheduled API limit is 100 requests per minute.",
        workspace_id=wid, subject_key="api-limit", claim_kind="configured_value",
        valid_from=first_at,
    )
    second = eng.remember_with_resolution(
        "The scheduled API limit is 200 requests per minute.",
        workspace_id=wid, subject_key="api-limit", claim_kind="configured_value",
        valid_from=second_at,
    )

    assert first["op"] == "add"
    assert second["op"] == "invalidate"
    assert eng.store.get_memory(first["id"]).valid_to == second_at


def test_code_reads_apply_session_visibility_to_every_memory_surface():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "sample")
    session_id = eng.store.start_session(wid, rid)
    symbol_id = eng.store.upsert_symbol(
        repo_id=rid, kind="function", name="deploy", fqname="deploy",
        file="deploy.py", span="1-1",
    )
    repo_memory = eng.store.add_memory(MemoryRecord(
        id="", content="deploy uses the public release process", title="repo deploy",
        workspace_id=wid, repo_id=rid, scope=Scope.REPO,
        provenance={"source": "test", "trusted": True, "review_state": "approved"},
    ))
    session_memory = eng.store.add_memory(MemoryRecord(
        id="", content="deploy uses a private session token", title="session deploy secret",
        workspace_id=wid, repo_id=rid, session_id=session_id, scope=Scope.SESSION,
        provenance={"source": "test", "trusted": True, "review_state": "approved"},
    ))
    for memory_id in (repo_memory, session_memory):
        eng.store.link_memory_symbol(
            repo_id=rid, symbol_id=symbol_id, memory_id=memory_id,
        )

    repo_filter = SearchFilter(
        workspace_id=wid, repo_id=rid, include_ancestors=True,
    )
    search = eng.search_code("deploy", repo_id=rid, flt=repo_filter)
    assert {row["id"] for row in search["symbols"][0]["linked_memories"]} == {
        repo_memory
    }
    assert eng.code_path("deploy", repo_memory, repo_id=rid, flt=repo_filter)["found"]
    assert not eng.code_path(
        "deploy", session_memory, repo_id=rid, flt=repo_filter,
    )["found"]
    impact = eng.analyze_impact(["deploy.py"], repo_id=rid, flt=repo_filter)
    assert {row["id"] for row in impact["memory_mentions"]} == {repo_memory}
    exported = eng.export_code_graph(repo_id=rid, flt=repo_filter)
    assert {row["memory_id"] for row in exported["memory_links"]} == {repo_memory}
    assert session_memory not in eng.code_graph_html(repo_id=rid, flt=repo_filter)

    session_filter = SearchFilter(
        workspace_id=wid, repo_id=rid, session_id=session_id,
        include_ancestors=True,
    )
    session_search = eng.search_code("deploy", repo_id=rid, flt=session_filter)
    assert {row["id"] for row in session_search["symbols"][0]["linked_memories"]} == {
        repo_memory, session_memory
    }
    assert eng.code_path(
        "deploy", session_memory, repo_id=rid, flt=session_filter,
    )["found"]


def test_code_reads_reject_mismatched_workspace_or_repo_filters():
    eng = MemoryEngine.create(":memory:")
    first_workspace = eng.store.get_or_create_workspace("first")
    second_workspace = eng.store.get_or_create_workspace("second")
    first_repo = eng.store.get_or_create_repo(first_workspace, "api")
    second_repo = eng.store.get_or_create_repo(second_workspace, "api")
    eng.store.upsert_symbol(
        repo_id=second_repo, kind="function", name="secret_fn",
        fqname="secret_fn", file="secret.py", span="1-1",
    )

    with pytest.raises(ValueError, match="workspace_id"):
        eng.search_code(
            "secret_fn",
            repo_id=second_repo,
            flt=SearchFilter(workspace_id=first_workspace, repo_id=second_repo),
        )
    with pytest.raises(ValueError, match="repo_id"):
        eng.export_code_graph(
            repo_id=second_repo,
            flt=SearchFilter(
                workspace_id=second_workspace,
                repo_id=first_repo,
            ),
        )


def test_rebuild_code_memory_links_keysets_past_five_thousand_session_records():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "sample")
    session_id = eng.store.start_session(wid, rid)
    symbol_id = eng.store.upsert_symbol(
        repo_id=rid, kind="function", name="deploy", fqname="deploy",
        file="deploy.py", span="1-1",
    )
    target_id = "mem_00000"
    rows = [
        (
            target_id, wid, rid, session_id, "session", "semantic",
            "oldest", "deploy remains linked", 0.0, 0.0,
        )
    ]
    rows.extend(
        (
            f"mem_{i:05d}", wid, rid, None, "repo", "semantic",
            "", "unrelated filler", float(i), float(i),
        )
        for i in range(1, 5001)
    )
    eng.store.conn.executemany(
        "INSERT INTO memories("
        "id, workspace_id, repo_id, session_id, scope, mtype, title, content, "
        "valid_from, ingested_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    approved_provenance = {
        "source": "human_review", "trusted": True, "review_state": "approved",
    }
    eng.store.conn.execute(
        "UPDATE memories SET metadata=?, provenance=? WHERE id=?",
        (
            json.dumps({"provenance": approved_provenance}),
            json.dumps(approved_provenance),
            target_id,
        ),
    )
    eng.store.conn.commit()
    eng.store.link_memory_symbol(
        repo_id=rid, symbol_id=symbol_id, memory_id=target_id,
    )

    eng.rebuild_code_memory_links(repo_id=rid)

    assert {
        row["memory_id"] for row in eng.store.list_code_memory_links(rid)
    } == {target_id}


def test_code_graph_html_escapes_embedded_graph_data():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "sample")
    eng.store.upsert_symbol(
        repo_id=rid,
        kind="function",
        name="run",
        fqname="run",
        file="</script><script>alert(1)</script>.py",
        span="1-1",
    )
    html = eng.code_graph_html(repo_id=rid)
    assert '<svg id="graph"' in html
    assert "Scroll to zoom" in html
    assert "</script><script>alert(1)</script>.py" not in html
    assert "\\u003c/script>" in html
    assert "&lt;/script&gt;&lt;script&gt;alert(1)&lt;/script&gt;.py" in html


# ── correct(): write the replacement before retiring the original ───────────────────

def test_correct_leaves_the_original_live_when_the_replacement_write_fails():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    mid = eng.remember("Old fact.", workspace_id=wid, resolve_conflicts=False)

    def boom(*_args, **_kw):
        raise RuntimeError("simulated write failure")

    eng.remember = boom
    with pytest.raises(RuntimeError):
        eng.correct(mid, "New fact.")

    rec = eng.store.get_memory(mid)
    assert rec.valid_to is None and rec.content == "Old fact."


def test_correct_repairs_a_repo_scoped_row_that_has_no_repo():
    """The sync apply path can persist a scope/repo_id combination ``remember`` rejects.
    Correcting one used to retire it and *then* raise, leaving nothing behind."""
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    mid = eng.remember("Synced fact.", workspace_id=wid, resolve_conflicts=False)
    eng.store.conn.execute(
        "UPDATE memories SET scope='repo', repo_id=NULL WHERE id=?", (mid,))
    eng.store.conn.commit()

    out = eng.correct(mid, "Corrected fact.")

    new = eng.store.get_memory(out["id"])
    assert new.content == "Corrected fact." and new.scope == Scope.WORKSPACE
    assert new.valid_to is None
    assert eng.store.get_memory(mid).valid_to is not None   # retired, not deleted


# ── export_code_graph is bounded (viewer-reachable payload) ─────────────────────────

def test_export_code_graph_is_bounded_and_flags_truncation(monkeypatch):
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "sample")
    for n in range(12):
        eng.store.upsert_symbol(repo_id=rid, kind="function", name=f"fn_{n:03d}",
                                fqname=f"mod{n}.fn_{n:03d}", file=f"mod{n}.py", span="1-1")
        eng.store.upsert_code_file(repo_id=rid, file=f"mod{n}.py", lang="python",
                                   content_hash=f"h{n}", size_bytes=1, mtime_ns=0,
                                   backend="test")

    requested_limits = []
    list_code_files = eng.store.list_code_files

    def tracked_list_code_files(repo_id, **kwargs):
        requested_limits.append(kwargs.get("limit"))
        return list_code_files(repo_id, **kwargs)

    monkeypatch.setattr(eng.store, "list_code_files", tracked_list_code_files)

    capped = eng.export_code_graph(repo_id=rid, limit=5)
    assert capped["limit"] == 5
    assert len(capped["nodes"]) == 5 and len(capped["files"]) == 5
    assert capped["truncated"] is True
    assert requested_limits == [6]  # five payload rows plus one truncation sentinel

    full = eng.export_code_graph(repo_id=rid)
    assert len(full["nodes"]) == 12 and len(full["files"]) == 12
    assert full["truncated"] is False
    # Bogus limits are clamped, never passed through to SQL.
    assert eng.export_code_graph(repo_id=rid, limit=-7)["limit"] == 1


# ── code↔memory linking: the compiled matcher must reproduce the old links exactly ──

_OVERLAPPING_SYMBOLS = [
    # (kind, name, fqname, file) — deliberately overlapping/substring names.
    ("class", "Engine", "engraphis.core.engine.Engine", "engine.py"),
    ("function", "engine", "engraphis.core.engine", "engine.py"),
    ("function", "engine_v2", "engraphis.core.engine_v2", "engine.py"),
    ("function", "run", "run", "run.py"),
    ("function", "run_all", "run.run_all", "run.py"),
    ("function", "ru", "ru", "run.py"),                 # < 3 chars: always skipped
    ("class", "Store", "engraphis.core.store.Store", "store.py"),
    ("function", "store", "store", "store.py"),
    ("function", "add", "Calculator.add", "calc.py"),
    ("class", "Calculator", "Calculator", "calc.py"),
]

_LINK_TEXTS = [
    "engraphis.core.engine wraps engine and engine_v2 for the migration.",
    "See engraphis.core.store.Store; the store module also exports Store.",
    "Calculator.add is the only caller of add() in calc.py.",
    "run_all invokes run, but run_allocation is unrelated.",
    "The engine_v2 rewrite lives beside engraphis.core.engine.Engine.",
    "Nothing here mentions any indexed symbol at all.",
]


def _legacy_links(symbols, content):
    """The pre-optimization per-symbol matcher, reproduced verbatim as the oracle."""
    import re

    from engraphis.core.textutil import tokenize

    hay = str(content or "")
    hay_lower = hay.lower()
    hay_tokens = tokenize(hay)
    out = []
    for symbol in symbols:
        name = str(symbol.get("name") or "").strip()
        fqname = str(symbol.get("fqname") or "").strip()
        if len(name) < 3:
            continue
        confidence = 0.0
        fqname_lower = fqname.lower()
        name_lower = name.lower()
        if fqname and len(fqname) >= 3 and fqname_lower in hay_lower and re.search(
            r"(?<!\w)" + re.escape(fqname.lower()) + r"(?!\w)", hay_lower
        ):
            confidence = 1.0
        elif name_lower in hay_lower and re.search(
            r"(?<!\w)" + re.escape(name_lower) + r"(?!\w)", hay_lower
        ):
            confidence = 0.9
        else:
            name_tokens = tokenize(name)
            if name_tokens and name_tokens <= hay_tokens:
                confidence = 0.75
        if confidence <= 0.0:
            continue
        out.append((symbol["id"], confidence))
        if len(out) >= 200:
            break
    return out


def test_compiled_symbol_matcher_reproduces_the_legacy_links_exactly():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "sample")
    for kind, name, fqname, file in _OVERLAPPING_SYMBOLS:
        eng.store.upsert_symbol(repo_id=rid, kind=kind, name=name, fqname=fqname,
                                file=file, span="1-1")
    symbols = eng.store.list_symbols(rid)

    for text in _LINK_TEXTS:
        mid = eng.remember(text, workspace_id=wid, repo_id=rid, resolve_conflicts=False)
        actual = sorted(
            (row["symbol_id"], row["confidence"])
            for row in eng.store.list_code_memory_links(rid)
            if row["memory_id"] == mid
        )
        assert actual == sorted(_legacy_links(symbols, text)), text


def test_symbol_matcher_still_sees_a_name_nested_inside_a_longer_fqname():
    """A plain non-overlapping ``finditer`` over the alternation would let
    ``engraphis.core.engine`` swallow ``engine`` and silently downgrade its confidence
    from 0.9 to the 0.75 token fallback. Candidate offsets must stay overlapping."""
    from engraphis.core.engine import _CodeSymbolMatcher

    symbols = [
        {"id": "sym_long", "name": "engine", "fqname": "engraphis.core.engine"},
        {"id": "sym_short", "name": "engine", "fqname": "engine"},
    ]
    matcher = _CodeSymbolMatcher(symbols)
    matched, positions = matcher.match("see engraphis.core.engine for details", set())
    assert matched == {"engraphis.core.engine", "engine"}
    assert positions == [0, 1], "candidates must come back in store order for the cap"


def test_code_matcher_cache_is_invalidated_when_symbols_change():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "sample")
    first = eng.remember("The deployer handles rollout.", workspace_id=wid, repo_id=rid,
                         resolve_conflicts=False)
    assert [r for r in eng.store.list_code_memory_links(rid)
            if r["memory_id"] == first] == []

    eng.store.upsert_symbol(repo_id=rid, kind="function", name="deployer",
                            fqname="deployer", file="d.py", span="1-1")
    second = eng.remember("The deployer also signs the release.", workspace_id=wid,
                          repo_id=rid, resolve_conflicts=False)

    assert [r["symbol_id"] for r in eng.store.list_code_memory_links(rid)
            if r["memory_id"] == second], "a new symbol must invalidate the cached matcher"


def test_extracted_graph_evidence_inherits_memory_temporal_anchors():
    eng = MemoryEngine.create(":memory:", graph_extractor="regex")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    future = time.time() + 10_000
    first = eng.remember(
        "Alice uses Stripe.",
        workspace_id=wid,
        repo_id=rid,
        valid_from=future,
        resolve_conflicts=False,
    )
    memory = eng.store.get_memory(first)
    edges = eng.store.edges_in_scope(SearchFilter(
        workspace_id=wid,
        repo_id=rid,
        valid_at=future,
        known_at=memory.ingested_at,
    ))
    assert len(edges) == 1
    assert edges[0].valid_from == future
    assert edges[0].ingested_at == memory.ingested_at
    assert eng.store.edges_in_scope(SearchFilter(
        workspace_id=wid,
        repo_id=rid,
        valid_at=future - 1,
        known_at=memory.ingested_at,
    )) == []
    assert eng.store.edges_in_scope(SearchFilter(
        workspace_id=wid,
        repo_id=rid,
        valid_at=future,
        known_at=memory.ingested_at - 1,
    )) == []

    earlier = future - 500
    eng.remember(
        "Alice uses Stripe.",
        workspace_id=wid,
        repo_id=rid,
        valid_from=earlier,
        resolve_conflicts=False,
    )
    edge = eng.store.edges_in_scope(SearchFilter(
        workspace_id=wid,
        repo_id=rid,
        valid_at=earlier,
    ))[0]
    assert edge.valid_from == earlier



def test_correct_preserves_claim_identity_protection_and_temporal_boundary():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    original_id = eng.remember(
        "The deployment target is blue.",
        workspace_id=wid,
        confidence=0.42,
        subject_key="deployment",
        claim_kind="target",
        valid_from=time.time() - 1.0,
        resolve_conflicts=False,
    )
    eng.store.conn.execute(
        "UPDATE memories SET pinned=1, sensitivity='sensitive', stability=9, "
        "access_count=4, last_access=123 WHERE id=?",
        (original_id,),
    )
    eng.store.conn.commit()

    result = eng.correct(original_id, "The deployment target is green.")
    original = eng.store.get_memory(original_id)
    replacement = eng.store.get_memory(result["id"])

    assert original.valid_to == replacement.valid_from
    assert replacement.subject_key == "deployment"
    assert replacement.claim_kind == "target"
    assert replacement.confidence == pytest.approx(0.42)
    assert replacement.pinned is True
    assert replacement.sensitivity == "sensitive"
    assert replacement.stability == pytest.approx(9)
    assert replacement.access_count == 4
    assert replacement.last_access == pytest.approx(123)
    before = eng.store.list_memories(SearchFilter(
        workspace_id=wid, valid_at=original.valid_to - 0.001,
    ))
    after = eng.store.list_memories(SearchFilter(
        workspace_id=wid, valid_at=original.valid_to + 0.001,
    ))
    assert {record.id for record in before} == {original_id}
    assert {record.id for record in after} == {replacement.id}


def test_lifecycle_finalizers_roll_back_every_authoritative_change(monkeypatch):
    def memory_ids(engine, workspace_id):
        return {
            record.id
            for record in engine.store.list_memories(
                SearchFilter(workspace_id=workspace_id), include_invalid=True,
            )
        }

    def reject_action(engine, action):
        original_audit = engine.store.audit

        def audited(actor, candidate_action, target, detail="", **kwargs):
            if candidate_action == action:
                raise RuntimeError(f"fail {action}")
            return original_audit(
                actor, candidate_action, target, detail, **kwargs,
            )

        return audited

    # Correction: the successor insert and predecessor closure are one transaction.
    correction = MemoryEngine.create(":memory:")
    correction_wid = correction.store.get_or_create_workspace("correct")
    correction_source = correction.remember(
        "old", workspace_id=correction_wid, resolve_conflicts=False,
    )
    with monkeypatch.context() as patch:
        patch.setattr(correction.store, "audit", reject_action(correction, "invalidate"))
        with pytest.raises(RuntimeError, match="fail invalidate"):
            correction.correct(correction_source, "new")
    assert memory_ids(correction, correction_wid) == {correction_source}
    assert correction.store.get_memory(correction_source).valid_to is None

    # Approval: a failed required audit cannot leave a prompt-eligible successor.
    approval = MemoryEngine.create(":memory:")
    approval_wid = approval.store.get_or_create_workspace("approval")
    pending = approval.remember(
        "pending",
        workspace_id=approval_wid,
        metadata={"provenance": {
            "source": "web", "trusted": False, "review_state": "pending",
        }},
        resolve_conflicts=False,
    )
    with monkeypatch.context() as patch:
        patch.setattr(approval.store, "audit", reject_action(approval, "approve"))
        with pytest.raises(RuntimeError, match="fail approve"):
            approval.approve_for_prompt(pending, reviewer="owner", reason="verified")
    assert memory_ids(approval, approval_wid) == {pending}
    approved = approval.approve_for_prompt(
        pending, reviewer="owner", reason="verified",
    )
    approved_retry = approval.approve_for_prompt(
        pending, reviewer="owner", reason="transport retry",
    )
    assert approved_retry["id"] == approved["id"]
    successor = approval.store.get_memory(approved["id"])
    assert successor.provenance["review_state"] == "approved"
    source = approval.store.get_memory(pending)
    assert (
        successor.pinned,
        successor.sensitivity,
        successor.stability,
        successor.access_count,
        successor.last_access,
    ) == (
        source.pinned,
        source.sensitivity,
        source.stability,
        source.access_count,
        source.last_access,
    )
    assert approval.store.conn.execute(
        "SELECT COUNT(*) FROM audit WHERE action='approve' AND target=?",
        (approved["id"],),
    ).fetchone()[0] == 1

    # Promotion: target, source closure, link, metadata, and audit roll back together.
    promotion = MemoryEngine.create(":memory:")
    promotion_wid = promotion.store.get_or_create_workspace("promotion")
    promotion_rid = promotion.store.get_or_create_repo(promotion_wid, "repo")
    promotion_source = promotion.remember(
        "repo fact", workspace_id=promotion_wid, repo_id=promotion_rid,
        scope=Scope.REPO, resolve_conflicts=False,
    )
    with monkeypatch.context() as patch:
        patch.setattr(promotion.store, "audit", reject_action(promotion, "promote"))
        with pytest.raises(RuntimeError, match="fail promote"):
            promotion.promote(promotion_source, Scope.WORKSPACE)
    assert memory_ids(promotion, promotion_wid) == {promotion_source}
    assert promotion.store.get_memory(promotion_source).valid_to is None
    assert promotion.store.get_links(promotion_source) == []

    # Merge: no partial successor, closures, links, or audits survive a late failure.
    merging = MemoryEngine.create(":memory:")
    merge_wid = merging.store.get_or_create_workspace("merge")
    source_a = merging.remember(
        "alpha", workspace_id=merge_wid, resolve_conflicts=False,
    )
    source_b = merging.remember(
        "beta", workspace_id=merge_wid, resolve_conflicts=False,
    )
    with monkeypatch.context() as patch:
        patch.setattr(merging.store, "audit", reject_action(merging, "merge"))
        with pytest.raises(RuntimeError, match="fail merge"):
            merging.merge([source_a, source_b], "combined")
    assert memory_ids(merging, merge_wid) == {source_a, source_b}
    assert merging.store.get_memory(source_a).valid_to is None
    assert merging.store.get_memory(source_b).valid_to is None
    assert merging.store.get_links(source_a) == []
    assert merging.store.get_links(source_b) == []

    original_close = merging.store.close_validity
    close_calls = 0

    def fail_second_close(memory_id, *args, **kwargs):
        nonlocal close_calls
        close_calls += 1
        if close_calls == 2:
            raise RuntimeError("fail second close")
        return original_close(memory_id, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(merging.store, "close_validity", fail_second_close)
        with pytest.raises(RuntimeError, match="fail second close"):
            merging.merge([source_a, source_b], "combined")
    assert memory_ids(merging, merge_wid) == {source_a, source_b}
    assert merging.store.get_memory(source_a).valid_to is None
    assert merging.store.get_memory(source_b).valid_to is None
    assert merging.store.conn.execute(
        "SELECT COUNT(*) FROM audit WHERE action='merge'"
    ).fetchone()[0] == 0


def test_engine_descriptive_writers_advance_memory_clocks(monkeypatch):
    eng = MemoryEngine.create(":memory:", auto_evolve=False)

    conflict_workspace = eng.store.get_or_create_workspace("clock-conflict")
    conflict_repo = eng.store.get_or_create_repo(conflict_workspace, "repo")
    conflicted = eng.remember_with_resolution(
        "The API uses JWT tokens for authentication.",
        workspace_id=conflict_workspace,
        repo_id=conflict_repo,
    )["id"]

    correction_workspace = eng.store.get_or_create_workspace("clock-correction")
    correction_source = eng.remember(
        "The deployment target is blue.",
        workspace_id=correction_workspace,
        resolve_conflicts=False,
    )

    approval_workspace = eng.store.get_or_create_workspace("clock-approval")
    pending = eng.remember(
        "Owner-reviewed pending fact.",
        workspace_id=approval_workspace,
        metadata={"provenance": {
            "source": "web", "trusted": False, "review_state": "pending",
        }},
        resolve_conflicts=False,
    )

    promotion_workspace = eng.store.get_or_create_workspace("clock-promotion")
    promotion_repo = eng.store.get_or_create_repo(promotion_workspace, "repo")
    wider = eng.remember(
        "Shared promotion fact.",
        workspace_id=promotion_workspace,
        scope=Scope.WORKSPACE,
        resolve_conflicts=False,
    )
    promotion_source = eng.remember(
        "Shared promotion fact.",
        workspace_id=promotion_workspace,
        repo_id=promotion_repo,
        scope=Scope.REPO,
        resolve_conflicts=False,
    )

    merge_workspace = eng.store.get_or_create_workspace("clock-merge")
    merge_sources = [
        eng.remember(
            content,
            workspace_id=merge_workspace,
            resolve_conflicts=False,
        )
        for content in ("alpha", "beta")
    ]

    advances = {}
    original_advance = eng.store.advance_memory_modified_hlc

    def tracked_advance(memory_id, *, observed_hlc="", commit=True):
        before = eng.store.get_memory(memory_id).modified_hlc
        after = original_advance(
            memory_id, observed_hlc=observed_hlc, commit=commit,
        )
        advances.setdefault(memory_id, []).append((before, after))
        return after

    monkeypatch.setattr(
        eng.store, "advance_memory_modified_hlc", tracked_advance,
    )

    conflict_result = eng.remember_with_resolution(
        "The API does not use JWT tokens for authentication.",
        workspace_id=conflict_workspace,
        repo_id=conflict_repo,
    )
    correction_result = eng.correct(
        correction_source, "The deployment target is green.",
    )
    approval_result = eng.approve_for_prompt(
        pending, reviewer="owner", reason="verified",
    )
    promotion_result = eng.promote(promotion_source, Scope.WORKSPACE)
    merge_result = eng.merge(merge_sources, "combined")

    assert conflict_result["conflict_with"] == conflicted
    assert promotion_result["id"] == wider
    expected = {
        conflicted,
        correction_result["id"],
        approval_result["id"],
        wider,
        merge_result["id"],
    }
    assert set(advances) == expected
    assert all(
        before < after
        for calls in advances.values()
        for before, after in calls
    )


def test_conflict_repair_rolls_back_clock_only_partial_failure(monkeypatch):
    eng = MemoryEngine.create(":memory:", auto_evolve=False)
    workspace_id = eng.store.get_or_create_workspace("clock-conflict-rollback")
    repo_id = eng.store.get_or_create_repo(workspace_id, "repo")
    original_id = eng.remember_with_resolution(
        "The API uses JWT tokens for authentication.",
        workspace_id=workspace_id,
        repo_id=repo_id,
    )["id"]
    before = eng.store.get_memory(original_id)
    original_advance = eng.store.advance_memory_modified_hlc

    def advance_then_fail(memory_id, *, observed_hlc="", commit=True):
        original_advance(
            memory_id, observed_hlc=observed_hlc, commit=commit,
        )
        raise RuntimeError("fail conflict confidence")

    monkeypatch.setattr(
        eng.store, "advance_memory_modified_hlc", advance_then_fail,
    )

    result = eng.remember_with_resolution(
        "The API does not use JWT tokens for authentication.",
        workspace_id=workspace_id,
        repo_id=repo_id,
    )

    after = eng.store.get_memory(original_id)
    assert result["conflict_with"] == original_id
    assert after.modified_hlc == before.modified_hlc
    assert after.confidence == before.confidence
    assert not eng.store.conn.transaction_owned_by_current_thread()


def test_promotion_descriptive_clock_rolls_back_with_failed_finalizer(monkeypatch):
    eng = MemoryEngine.create(":memory:")
    workspace_id = eng.store.get_or_create_workspace("clock-rollback")
    repo_id = eng.store.get_or_create_repo(workspace_id, "repo")
    wider = eng.remember(
        "Shared promotion fact.",
        workspace_id=workspace_id,
        scope=Scope.WORKSPACE,
        resolve_conflicts=False,
    )
    source = eng.remember(
        "Shared promotion fact.",
        workspace_id=workspace_id,
        repo_id=repo_id,
        scope=Scope.REPO,
        resolve_conflicts=False,
    )
    before = eng.store.get_memory(wider)
    original_audit = eng.store.audit

    def reject_promotion(actor, action, target, detail="", **kwargs):
        if action == "promote":
            raise RuntimeError("fail promote")
        return original_audit(actor, action, target, detail, **kwargs)

    monkeypatch.setattr(eng.store, "audit", reject_promotion)

    with pytest.raises(RuntimeError, match="fail promote"):
        eng.promote(source, Scope.WORKSPACE)

    after = eng.store.get_memory(wider)
    assert after.modified_hlc == before.modified_hlc
    assert after.metadata == before.metadata
    assert eng.store.get_memory(source).valid_to is None



def test_merge_exact_retry_returns_original_successor():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    source_a = eng.remember(
        "alpha", workspace_id=wid, keywords=["a"], resolve_conflicts=False,
    )
    source_b = eng.remember(
        "beta", workspace_id=wid, keywords=["b"], resolve_conflicts=False,
    )

    first = eng.merge([source_a, source_b], "combined", reason="deduplicate")
    retried = eng.merge([source_a, source_b], "combined", reason="deduplicate")

    assert retried == first
    assert eng.store.conn.execute(
        "SELECT COUNT(*) FROM memories WHERE content='combined'"
    ).fetchone()[0] == 1
    assert eng.store.conn.execute(
        "SELECT COUNT(*) FROM audit WHERE actor='user' AND action='merge'"
    ).fetchone()[0] == 3


def test_concurrent_lifecycle_retries_create_exactly_one_successor():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    def run_pair(operation):
        barrier = Barrier(3)

        def invoke():
            barrier.wait()
            return operation()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(invoke) for _ in range(2)]
            barrier.wait()
            return [future.result() for future in futures]

    approval = MemoryEngine.create(":memory:")
    approval_wid = approval.store.get_or_create_workspace("approval-concurrency")
    pending = approval.remember(
        "The deployment target is blue.",
        workspace_id=approval_wid,
        metadata={"provenance": {
            "source": "web", "trusted": False, "review_state": "pending",
        }},
        resolve_conflicts=False,
    )
    approved = run_pair(lambda: approval.approve_for_prompt(
        pending, reviewer="owner", reason="verified",
    ))
    assert len({result["id"] for result in approved}) == 1
    assert approval.store.conn.execute(
        "SELECT COUNT(*) FROM memories WHERE provenance LIKE '%\"approved_from\"%'"
    ).fetchone()[0] == 1
    assert approval.store.conn.execute(
        "SELECT COUNT(*) FROM audit WHERE action='approve'"
    ).fetchone()[0] == 1

    merging = MemoryEngine.create(":memory:")
    merge_wid = merging.store.get_or_create_workspace("merge-concurrency")
    source_a = merging.remember(
        "alpha", workspace_id=merge_wid, resolve_conflicts=False,
    )
    source_b = merging.remember(
        "beta", workspace_id=merge_wid, resolve_conflicts=False,
    )
    merged = run_pair(lambda: merging.merge(
        [source_a, source_b], "combined", reason="deduplicate",
    ))
    assert len({result["id"] for result in merged}) == 1
    assert merging.store.conn.execute(
        "SELECT COUNT(*) FROM memories WHERE content='combined'"
    ).fetchone()[0] == 1
    assert merging.store.conn.execute(
        "SELECT COUNT(*) FROM audit WHERE action='merge'"
    ).fetchone()[0] == 3


def test_merge_retry_identity_is_not_lost_behind_unrelated_links():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("merge-key")
    source_a = eng.remember(
        "alpha", workspace_id=wid, keywords=["a"], resolve_conflicts=False,
    )
    source_b = eng.remember(
        "beta", workspace_id=wid, keywords=["b"], resolve_conflicts=False,
    )
    for index in range(65):
        distractor = eng.remember(
            f"distractor {index}",
            workspace_id=wid,
            resolve_conflicts=False,
        )
        eng.store.add_link(source_a, distractor, "merges")

    first = eng.merge([source_a, source_b], "combined", reason="deduplicate")
    retried = eng.merge([source_a, source_b], "combined", reason="deduplicate")

    assert retried == first
    assert eng.store.conn.execute(
        "SELECT COUNT(*) FROM memories WHERE content='combined'"
    ).fetchone()[0] == 1

def test_cross_session_merge_requires_explicit_broader_target():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    first_session = eng.start_session(wid, rid)
    second_session = eng.start_session(wid, rid)
    first = eng.remember(
        "first", workspace_id=wid, repo_id=rid, session_id=first_session,
        scope=Scope.SESSION, resolve_conflicts=False,
    )
    second = eng.remember(
        "second", workspace_id=wid, repo_id=rid, session_id=second_session,
        scope=Scope.SESSION, resolve_conflicts=False,
    )

    with pytest.raises(ValueError, match="cross-session merge"):
        eng.merge([first, second], "combined")
    result = eng.merge([first, second], "combined", scope=Scope.REPO)
    merged = eng.store.get_memory(result["id"])
    assert merged.scope == Scope.REPO
    assert merged.repo_id == rid
    assert merged.session_id is None


def test_cross_session_merge_can_explicitly_widen_to_workspace_scope():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    first_session = eng.start_session(wid, rid)
    second_session = eng.start_session(wid, rid)
    first = eng.remember(
        "first workspace fact", workspace_id=wid, repo_id=rid,
        session_id=first_session, scope=Scope.SESSION, resolve_conflicts=False,
    )
    second = eng.remember(
        "second workspace fact", workspace_id=wid, repo_id=rid,
        session_id=second_session, scope=Scope.SESSION, resolve_conflicts=False,
    )

    result = eng.merge(
        [first, second], "combined workspace fact", scope=Scope.WORKSPACE,
    )

    merged = eng.store.get_memory(result["id"])
    assert merged.scope == Scope.WORKSPACE
    assert merged.repo_id is None
    assert merged.session_id is None


def test_session_merge_rejects_an_ended_target_session():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    session_id = eng.start_session(wid, rid)
    first = eng.remember(
        "first", workspace_id=wid, repo_id=rid, session_id=session_id,
        scope=Scope.SESSION, resolve_conflicts=False,
    )
    second = eng.remember(
        "second", workspace_id=wid, repo_id=rid, session_id=session_id,
        scope=Scope.SESSION, resolve_conflicts=False,
    )
    eng.end_session(session_id)

    with pytest.raises(ValueError, match="active session"):
        eng.merge([first, second], "combined", scope=Scope.SESSION)
    assert eng.store.get_memory(first).valid_to is None
    assert eng.store.get_memory(second).valid_to is None


def test_read_only_engine_recall_does_not_mutate_database(tmp_path):
    db_path = tmp_path / "readonly.db"
    writer = MemoryEngine.create(str(db_path), vector_backend="numpy")
    wid = writer.store.get_or_create_workspace("w")
    writer.remember(
        "The production deployment target is blue.",
        workspace_id=wid,
        resolve_conflicts=False,
    )
    writer.store.close()
    before = db_path.read_bytes()

    reader = MemoryEngine.create(
        str(db_path), vector_backend="numpy", read_only=True,
    )
    recalled = reader.recall("production deployment target", workspace_id=wid)
    reader.store.close()

    assert recalled.count == 1
    assert recalled.chunks[0]["content"] == "The production deployment target is blue."
    assert db_path.read_bytes() == before


def test_read_only_engine_rejects_a_mismatched_embedding_space(monkeypatch, tmp_path):
    from engraphis import factory as factory_module

    db_path = tmp_path / "readonly-mismatch.db"
    writer = MemoryEngine.create(str(db_path), vector_backend="numpy")
    workspace_id = writer.store.get_or_create_workspace("w")
    writer.remember(
        "Embedding fingerprint fixture.",
        workspace_id=workspace_id,
        resolve_conflicts=False,
    )
    writer.store.close()
    before = db_path.read_bytes()

    class DifferentEmbedder:
        dim = 384
        embedding_identity = "test-different-embedder"
        embedding_version = "v1"
        supports_semantic_search = True

    monkeypatch.setattr(
        factory_module,
        "get_embedder",
        lambda *_args, **_kwargs: DifferentEmbedder(),
    )

    with pytest.raises(RuntimeError, match="matching embedder"):
        MemoryEngine.create(
            str(db_path), vector_backend="numpy", read_only=True,
        )

    assert db_path.read_bytes() == before
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1




def test_engine_factory_closes_owned_resources_when_composition_fails(monkeypatch):
    from engraphis import factory as factory_module

    opened_stores = []
    real_store = factory_module.Store

    class TrackingStore(real_store):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.close_count = 0
            opened_stores.append(self)

        def close(self):
            self.close_count += 1
            return super().close()

    monkeypatch.setattr(factory_module, "Store", TrackingStore)

    def fail_embedder(*_args, **_kwargs):
        raise RuntimeError("embedder unavailable")

    monkeypatch.setattr(factory_module, "get_embedder", fail_embedder)
    with pytest.raises(RuntimeError, match="embedder unavailable"):
        MemoryEngine.create(":memory:")
    assert opened_stores[0].close_count == 1
    with pytest.raises(sqlite3.ProgrammingError):
        opened_stores[0].conn.execute("SELECT 1")

    class FakeEmbedder:
        dim = 4

    class ClosableIndex:
        def __init__(self):
            self.closed = 0

        def close(self):
            self.closed += 1

    index = ClosableIndex()
    monkeypatch.setattr(
        factory_module, "get_embedder", lambda *_args, **_kwargs: FakeEmbedder(),
    )
    monkeypatch.setattr(
        factory_module, "get_vector_index", lambda *_args, **_kwargs: index,
    )

    def fail_reranker(*_args, **_kwargs):
        raise RuntimeError("reranker unavailable")

    monkeypatch.setattr(factory_module, "get_reranker", fail_reranker)
    with pytest.raises(RuntimeError, match="reranker unavailable"):
        MemoryEngine.create(":memory:")
    assert opened_stores[1].close_count == 1
    assert index.closed == 1
    with pytest.raises(sqlite3.ProgrammingError):
        opened_stores[1].conn.execute("SELECT 1")


def test_engine_factory_closes_all_owned_resources_when_rebuild_fails(monkeypatch):
    from engraphis import factory as factory_module

    opened_stores = []
    real_store = factory_module.Store

    class TrackingStore(real_store):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            opened_stores.append(self)

    class Closable:
        def __init__(self):
            self.closed = 0

        def close(self):
            self.closed += 1

    class FakeEmbedder(Closable):
        dim = 4
        embedding_identity = "factory-cleanup"
        embedding_version = "v1"

    resources = {
        name: (FakeEmbedder() if name == "embedder" else Closable())
        for name in ("embedder", "index", "reranker", "extractor", "graph", "supervisor")
    }
    monkeypatch.setattr(factory_module, "Store", TrackingStore)
    monkeypatch.setattr(
        factory_module, "get_embedder",
        lambda *_args, **_kwargs: resources["embedder"],
    )
    monkeypatch.setattr(
        factory_module, "get_vector_index",
        lambda *_args, **_kwargs: resources["index"],
    )
    monkeypatch.setattr(
        factory_module, "get_reranker",
        lambda *_args, **_kwargs: resources["reranker"],
    )
    monkeypatch.setattr(
        factory_module, "get_extractor",
        lambda *_args, **_kwargs: resources["extractor"],
    )
    monkeypatch.setattr(
        factory_module, "get_graph_extractor",
        lambda *_args, **_kwargs: resources["graph"],
    )
    monkeypatch.setattr(
        factory_module, "get_retention_supervisor",
        lambda *_args, **_kwargs: resources["supervisor"],
    )

    def fail_rebuild(_self):
        raise RuntimeError("rebuild failed")

    monkeypatch.setattr(
        MemoryEngine, "_rebuild_versioned_embeddings", fail_rebuild,
    )

    with pytest.raises(RuntimeError, match="rebuild failed"):
        MemoryEngine.create(
            ":memory:",
            extractor="fake",
            graph_extractor="fake",
            retention_supervisor="fake",
        )

    assert all(resource.closed == 1 for resource in resources.values())
    with pytest.raises(sqlite3.ProgrammingError):
        opened_stores[0].conn.execute("SELECT 1")


def test_engine_factory_transfers_successful_composition_ownership(monkeypatch):
    from engraphis import factory as factory_module

    engine_ref = {}

    class ClosableReranker:
        def __init__(self):
            self.close_count = 0

        def close(self):
            engine_ref["engine"].store.conn.execute("SELECT 1")
            self.close_count += 1

    reranker = ClosableReranker()
    monkeypatch.setattr(
        factory_module, "get_reranker", lambda *_args, **_kwargs: reranker,
    )
    eng = MemoryEngine.create(":memory:")
    engine_ref["engine"] = eng

    eng.close()
    eng.close()

    assert reranker.close_count == 1
    with pytest.raises(sqlite3.ProgrammingError):
        eng.store.conn.execute("SELECT 1")


def test_service_close_releases_factory_owned_engine_resources(monkeypatch):
    from engraphis import factory as factory_module
    from engraphis.service import MemoryService

    service_ref = {}

    class ClosableReranker:
        def __init__(self):
            self.close_count = 0

        def close(self):
            service_ref["service"].store.conn.execute("SELECT 1")
            self.close_count += 1

    reranker = ClosableReranker()
    monkeypatch.setattr(
        factory_module, "get_reranker", lambda *_args, **_kwargs: reranker,
    )
    service = MemoryService.create(":memory:")
    service_ref["service"] = service

    service.close()
    service.close()

    assert reranker.close_count == 1
    with pytest.raises(sqlite3.ProgrammingError):
        service.store.conn.execute("SELECT 1")


def test_public_outer_factory_constructs_the_default_engine():
    from engraphis import create_memory_engine

    eng = create_memory_engine(":memory:", vector_backend="numpy")
    wid = eng.store.get_or_create_workspace("w")
    memory_id = eng.remember(
        "Outer composition works.", workspace_id=wid, resolve_conflicts=False,
    )

    assert eng.store.get_memory(memory_id).content == "Outer composition works."


def test_importing_core_engine_does_not_import_concrete_backends():
    import subprocess
    import sys

    probe = (
        "import sys; import engraphis.core.engine; "
        "loaded = sorted(n for n in sys.modules if n.startswith('engraphis.backends.')); "
        "assert loaded == [], loaded"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_grounded_recall_empty_query_abstains():
    """An empty/whitespace-only query has no meaningful support signal and must
    abstain rather than returning a hallucinated answer from nearest neighbours."""
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    eng.remember("Some stored fact about authentication.", workspace_id=wid)
    for query in ("", "   ", "\t"):
        answer = eng.grounded_recall(query, workspace_id=wid)
        assert answer.abstained is True
        assert answer.grounded is False
        assert answer.answer == ""


def test_grounded_recall_min_support_zero_disables_abstain_gate():
    """Setting min_support=0 explicitly opts out of the abstain gate: even weak
    evidence produces an answer (the caller asked for it)."""
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    eng.remember("PostgreSQL uses MVCC for concurrency control.", workspace_id=wid)
    # Query with lexical overlap ("PostgreSQL") but weak semantic relevance;
    # default floor would abstain, floor=0 produces an answer.
    answer = eng.grounded_recall("PostgreSQL banana", workspace_id=wid, min_support=0.0)
    assert answer.abstained is False
    assert answer.grounded is True
    assert len(answer.citations) >= 1


def test_grounded_recall_no_memories_in_scope_abstains_with_reason():
    """When the scope contains no memories at all, grounded recall must abstain
    with a clear reason rather than raising or returning empty citations."""
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("empty-ws")
    answer = eng.grounded_recall("anything", workspace_id=wid)
    assert answer.abstained is True
    assert answer.grounded is False
    assert "no memory" in answer.reason.lower() or "support" in answer.reason.lower()
    assert answer.citations == []


def test_grounded_recall_invalid_min_support_raises():
    """Non-finite or out-of-range min_support is a caller error, not a silent default."""
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    for bad in (float("nan"), float("inf"), -0.1, 1.5):
        with pytest.raises(ValueError, match="min_support"):
            eng.grounded_recall("query", workspace_id=wid, min_support=bad)
