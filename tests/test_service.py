"""Offline tests for the MemoryService facade (numpy-only, no model download, no mcp).

Covers the validated write/read path the MCP server delegates to: round-trip recall,
scope isolation, session lifecycle, input validation, and untrusted-content sanitization
(the memory-poisoning guard), plus conflict resolution, governance, and the bi-temporal
why/timeline/proactive tools.
"""
import sqlite3
import threading
import time
from types import SimpleNamespace
import numpy as np
import pytest

import engraphis.service as service_module
from engraphis.core.interfaces import MemoryRecord, SchemaSnapshot, Scope
from engraphis.core.poisoning import source_is_external
from engraphis.service import (
    MemoryService,
    ValidationError,
    _warn_if_db_empty_with_populated_sibling,
    set_current_user,
)


class _ReviewedLocalService:
    """Compatibility facade for fixtures that still exercise external review.

    Normal local-agent writes are approved immediately by ``MemoryService``. For
    legacy fixtures that use a non-external source label, retain the explicit
    successor ceremony so those tests continue to model that separate workflow.
    """

    def __init__(self, service: MemoryService) -> None:
        self._service = service

    def __getattr__(self, name):
        return getattr(self._service, name)

    def remember(self, content, *args, **kwargs):
        result = self._service.remember(content, *args, **kwargs)
        record = self._service.store.get_memory(result["id"])
        source = kwargs.get("source", "agent")
        requested_trust = kwargs.get("trusted", True)
        if (
            requested_trust is True
            and not source_is_external(source)
            and record is not None
            and record.provenance.get("review_state") == "pending"
            and not record.provenance.get("quarantined")
        ):
            approved = self._service.engine.approve_for_prompt(
                result["id"], reviewer="test-owner", reason="approved test fixture",
            )
            return {
                **result,
                "id": approved["id"],
                "pending_id": approved["approved_from"],
            }
        return result


def _svc() -> _ReviewedLocalService:
    return _ReviewedLocalService(MemoryService.create(":memory:"))


def test_service_create_forwards_exact_backend_mode(monkeypatch):
    captured = {}
    store = SimpleNamespace(allowed_workspaces=None)

    def fake_create(cls, db_path, **kwargs):
        captured.update(db_path=db_path, **kwargs)
        return SimpleNamespace(store=store)

    monkeypatch.setattr(service_module.MemoryEngine, "create", classmethod(fake_create))
    MemoryService.create(
        ":memory:", extractor="none", graph_extractor="none",
        retention_supervisor="none", require_exact_backends=True,
    )

    assert captured["require_exact_backends"] is True


def test_empty_configured_db_warns_about_populated_owner_db(tmp_path, monkeypatch, capsys):
    """A stale ENGRAPHIS_DB_PATH must not look like lost local memories."""
    configured = tmp_path / "stale" / "engraphis.db"
    configured.parent.mkdir()
    owner_db = tmp_path / ".engraphis" / "engraphis.db"
    owner_db.parent.mkdir()
    for path in (configured, owner_db):
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE memories (id TEXT)")
    with sqlite3.connect(owner_db) as conn:
        conn.execute("INSERT INTO memories (id) VALUES ('mem_present')")

    monkeypatch.setattr(service_module.Path, "home", classmethod(lambda cls: tmp_path))
    _warn_if_db_empty_with_populated_sibling(str(configured))

    warning = capsys.readouterr().err
    assert str(configured) in warning
    assert str(owner_db) in warning
    assert "1 memories" in warning


def test_memory_service_preserves_regular_sqlite_uri_options(tmp_path):
    """URI modes must reach SQLite instead of being converted to a writable path."""
    missing = tmp_path / "missing.db"
    uri = missing.as_uri() + "?mode=rw"

    with pytest.raises(sqlite3.OperationalError, match="unable to open database file"):
        MemoryService.create(uri, extractor="none", graph_extractor="none")
    assert not missing.exists()


def test_remember_batch_omitted_trusted_matches_single_write_safe_default():
    service = MemoryService.create(":memory:", graph_extractor="none")

    result = service.remember_batch(
        [{"content": "Imported evidence awaiting review.",
          "source": "web", "scope": "workspace"}],
        workspace="acme",
    )

    assert result["succeeded"] == 1
    record = service.store.get_memory(result["results"][0]["id"])
    assert record is not None
    assert record.provenance["trusted"] is False
    assert record.provenance["review_state"] == "pending"
    assert "trust_downgraded" not in record.provenance


def test_remember_batch_forwards_write_options():
    service = MemoryService.create(":memory:", graph_extractor="none")
    calls = []
    original = service.remember

    def capture(*args, **kwargs):
        calls.append(kwargs.copy())
        return original(*args, **kwargs)

    service.remember = capture
    result = service.remember_batch(
        [{
            "content": "Critical batch evidence.",
            "metadata": {"ticket": "OPS-123"},
            "retention_class": "critical",
            "retention_reason": "incident runbook",
            "resolve_conflicts": False,
        }],
        workspace="acme",
    )

    assert result["succeeded"] == 1
    assert calls[0]["metadata"] == {"ticket": "OPS-123"}
    assert calls[0]["resolve_conflicts"] is False
    assert calls[0]["retention_class"] == "critical"
    assert calls[0]["retention_reason"] == "incident runbook"
    record = service.store.get_memory(result["results"][0]["id"])
    assert record.metadata["retention_supervision"]["label"] == "critical"

def test_remember_batch_forwards_metadata_retention_and_conflict_policy():
    service = MemoryService.create(":memory:", graph_extractor="none")

    result = service.remember_batch(
        [{
            "content": "A critical batch fact.",
            "metadata": {"origin": "batch"},
            "retention_class": "critical",
            "retention_reason": "release policy",
            "resolve_conflicts": False,
        }],
        workspace="acme",
    )

    record = service.store.get_memory(result["results"][0]["id"])
    assert record is not None
    assert record.metadata["origin"] == "batch"
    assert record.metadata["retention_supervision"]["label"] == "critical"
    assert record.metadata["retention_supervision"]["reason"] == "release policy"


def test_memory_health_binds_time_parameters_and_scopes_conflicts_to_workspace():
    service = MemoryService.create(":memory:", graph_extractor="none")
    alpha = service.remember(
        "Alpha workspace health marker.", workspace="alpha", scope="workspace"
    )
    beta = service.remember(
        "Beta workspace health marker.", workspace="beta", scope="workspace"
    )
    service.store.audit("test", "conflict_detected", alpha["id"])
    service.store.audit("test", "sync_trust_conflict", beta["id"])
    alpha_reader = MemoryService(service.engine, allowed_workspaces=["alpha"])

    health = alpha_reader.memory_health(workspace="alpha")

    assert sum(bucket["count"] for bucket in health["decay_distribution"]) == 1
    assert health["orphan_count"] == 1
    assert health["conflict_frequency"] == {"total": 1, "last_7d": 1}


def test_memory_health_uses_current_bitemporal_visibility():
    service = MemoryService.create(":memory:", graph_extractor="none")
    service.remember(
        "Visible health marker.", workspace="alpha", scope="workspace"
    )
    future_ingested = service.remember(
        "Future ingestion marker.", workspace="alpha", scope="workspace"
    )
    future_expiring = service.remember(
        "Future expiration marker.", workspace="alpha", scope="workspace"
    )
    now = time.time()
    service.store.conn.execute(
        "UPDATE memories SET ingested_at=? WHERE id=?",
        (now + 3600, future_ingested["id"]),
    )
    service.store.conn.execute(
        "UPDATE memories SET expired_at=? WHERE id=?",
        (now + 3600, future_expiring["id"]),
    )
    service.store.conn.commit()

    health = service.memory_health(workspace="alpha")

    assert sum(bucket["count"] for bucket in health["decay_distribution"]) == 2
    assert health["orphan_count"] == 2


def test_remember_then_recall_roundtrip():
    s = _svc()
    out = s.remember("We use pnpm as the package manager for all frontend repos.",
                     workspace="acme", repo="web")
    assert out["stored"] is True
    assert out["id"].startswith("mem_")
    assert out["scope"] == "repo" and out["mtype"] == "semantic"

    r = s.recall("which package manager for the frontend?", workspace="acme", repo="web")
    assert r["count"] >= 1
    assert "pnpm" in r["context"]
    assert any("pnpm" in m["content"] for m in r["memories"])


def test_recall_distinguishes_query_relative_rank_from_absolute_support():
    s = _svc()
    s.remember("Frontend repositories use pnpm for package management.",
               workspace="acme", repo="web")

    full = s.recall("which package manager do frontend repositories use?",
                    workspace="acme", repo="web")
    memory = full["memories"][0]
    assert memory["score"] == memory["relative_score"]  # compatibility alias
    assert 0.0 <= memory["absolute_support"] <= 1.0
    assert "Query-relative" in full["score_semantics"]["relative_score"]
    assert "[0, 1]" in full["score_semantics"]["absolute_support"]

    compact = s.recall("which package manager do frontend repositories use?",
                       workspace="acme", repo="web", response_mode="compact")
    compact_memory = compact["memories"][0]
    assert compact_memory["relative_score"] == compact_memory["score"]
    assert 0.0 <= compact_memory["absolute_support"] <= 1.0
    assert compact["score_semantics"] == full["score_semantics"]


def test_recall_support_reuses_vector_arm_without_a_second_embedding_batch():
    s = _svc()
    s.remember("Frontend repositories use pnpm for package management.",
               workspace="acme", repo="web")

    class CountingEmbedder:
        # This test models a declared semantic production adapter while retaining a
        # deterministic vector implementation to keep the unit test offline.
        supports_semantic_search = True
        embedding_mode = "semantic"

        def __init__(self, wrapped):
            self.wrapped = wrapped
            self.batches = []

        def embed(self, texts):
            self.batches.append(list(texts))
            return self.wrapped.embed(texts)

    counter = CountingEmbedder(s.engine.recall_engine.embedder)
    s.engine.recall_engine.embedder = counter
    result = s.recall("which package manager do frontend repositories use?",
                      workspace="acme", repo="web")

    assert len(counter.batches) == 1
    assert counter.batches[0] == ["which package manager do frontend repositories use?"]
    assert result["score_semantics"]["version"] == "retrieval-support-v1"


def test_deterministic_recall_reports_degraded_mode_and_disables_vector_arm():
    s = _svc()
    s.remember("Frontend repositories use pnpm for package management.",
               workspace="acme", repo="web")

    result = s.recall(
        "which package manager do frontend repositories use?",
        workspace="acme", repo="web", diagnostics=True,
    )

    assert result["degraded_mode"] is True
    assert result["semantic_support"] is False
    assert result["embedding_mode"] == "lexical_hashing"
    assert "Semantic cosine is disabled" in result["score_semantics"]["absolute_support"]
    assert result["retrieval_trace"][0]["raw"]["semantic"] is None


def test_deterministic_grounded_recall_is_explicitly_lexical_only():
    s = _svc()
    s.remember("Frontend repositories use pnpm for package management.",
               workspace="acme", repo="web")

    result = s.grounded_recall(
        "which package manager do frontend repositories use?",
        workspace="acme", repo="web",
    )

    assert result["grounded"] is True
    assert result["degraded_mode"] is True
    assert result["semantic_support"] is False
    assert result["embedding_mode"] == "lexical_hashing"


def test_degraded_recall_does_not_return_a_weak_vector_neighbour():
    s = _svc()
    s.remember("Production deploys to AWS ECS after approval.",
               workspace="acme", repo="web")

    result = s.recall("What sourdough hydration ratio should I use?",
                      workspace="acme", repo="web")
    assert result["count"] == 0
    assert result["memories"] == []


def test_local_agent_writes_resolve_claims_without_owner_approval():
    s = _svc()
    old_text = "The API rate limit is one hundred requests every sixty seconds."
    new_text = "Calls are capped at 500 per minute for each key."

    unkeyed_old = s.remember(old_text, workspace="unkeyed", repo="api")
    unkeyed_new = s.remember(new_text, workspace="unkeyed", repo="api")
    assert unkeyed_new["op"] == "add"
    assert s.store.get_memory(unkeyed_old["id"]).valid_to is None

    keyed_old = s.remember(
        old_text, workspace="keyed", repo="api", subject_key="api-rate-limit",
        claim_kind="configured_value",
    )
    keyed_new = s.remember(
        new_text, workspace="keyed", repo="api", subject_key="api-rate-limit",
        claim_kind="configured_value",
    )
    assert keyed_new["op"] == "invalidate"
    assert s.store.get_memory(keyed_old["id"]).valid_to is not None


@pytest.mark.parametrize("method", ("remember", "ingest"))
def test_invalid_trust_label_is_rejected_before_scope_creation(method):
    s = _svc()

    with pytest.raises(ValidationError, match="trusted must be a boolean"):
        getattr(s, method)("untrusted input", workspace="must-not-exist", trusted="false")

    assert s.list_workspaces()["workspaces"] == []


def test_service_recall_does_not_reinforce_weak_results_by_default():
    s = _svc()
    stored = s.remember("The deployment target is AWS ECS.", workspace="acme", repo="web")
    before = s.store.get_memory(stored["id"]).access_count

    s.recall("unrelated lunch menu", workspace="acme", repo="web", k=1)
    assert s.store.get_memory(stored["id"]).access_count == before

    s.recall("deployment target", workspace="acme", repo="web", k=1, reinforce=True)
    assert s.store.get_memory(stored["id"]).access_count > before


def test_scope_isolation_by_workspace():
    s = _svc()
    s.remember("Secret alpha fact about widgets.", workspace="alpha")
    s.remember("Secret beta fact about gadgets.", workspace="beta")
    r = s.recall("fact", workspace="alpha")
    assert r["count"] >= 1
    assert all(m["content"] != "Secret beta fact about gadgets." for m in r["memories"])


def test_repo_recall_inherits_workspace_memories():
    s = _svc()
    workspace = s.remember(
        "Scopeprobe: every repository must use signed commits.",
        workspace="acme", scope="workspace",
    )
    repo = s.remember(
        "Scopeprobe: the web repository deploys on tags.",
        workspace="acme", repo="web", scope="repo",
    )

    recalled = s.recall("scopeprobe", workspace="acme", repo="web", k=10)
    ids = {memory["id"] for memory in recalled["memories"]}

    assert {workspace["id"], repo["id"]} <= ids


def test_session_recall_is_exact_and_inherits_ancestors():
    s = _svc()
    workspace = s.remember(
        "Scopeprobe workspace convention.", workspace="acme", scope="workspace"
    )
    repo = s.remember(
        "Scopeprobe repo convention.", workspace="acme", repo="web", scope="repo"
    )
    first_session = s.start_session("acme", repo="web", goal="first", force_new=True)
    second_session = s.start_session("acme", repo="web", goal="second", force_new=True)
    first = s.remember(
        "Scopeprobe private first-session state.", workspace="acme", repo="web",
        session_id=first_session["session_id"], scope="session",
    )
    second = s.remember(
        "Scopeprobe private second-session state.", workspace="acme", repo="web",
        session_id=second_session["session_id"], scope="session",
    )

    repo_recall = s.recall("scopeprobe", workspace="acme", repo="web", k=10)
    repo_ids = {memory["id"] for memory in repo_recall["memories"]}
    assert {workspace["id"], repo["id"]} <= repo_ids
    assert first["id"] not in repo_ids and second["id"] not in repo_ids

    session_recall = s.recall(
        "scopeprobe", workspace="acme", repo="web",
        session_id=first_session["session_id"], k=10,
    )
    session_ids = {memory["id"] for memory in session_recall["memories"]}
    assert {workspace["id"], repo["id"], first["id"]} <= session_ids
    assert second["id"] not in session_ids


def test_write_scope_defaults_and_parent_validation():
    s = _svc()
    workspace = s.remember("Workspace default.", workspace="acme")
    repo = s.remember("Repo default.", workspace="acme", repo="web")
    assert workspace["scope"] == "workspace"
    assert repo["scope"] == "repo"

    session = s.start_session("acme", repo="web", force_new=True)
    session_grouped_repo = s.remember(
        "Session-grouped durable repo fact.", workspace="acme", repo="web",
        session_id=session["session_id"],
    )
    session_private = s.remember(
        "Session-private working state.", workspace="acme", repo="web",
        session_id=session["session_id"], scope="session",
    )
    assert session_grouped_repo["scope"] == "repo"
    assert session_private["scope"] == "session"

    workspace_session = s.start_session("acme", force_new=True)
    workspace_session_default = s.remember(
        "Workspace-session grouped fact.", workspace="acme",
        session_id=workspace_session["session_id"],
    )
    assert workspace_session_default["scope"] == "workspace"

    with pytest.raises(ValidationError, match="repo scope requires"):
        s.remember("broken", workspace="acme", scope="repo")
    with pytest.raises(ValidationError, match="session scope requires"):
        s.remember("broken", workspace="acme", repo="web", scope="session")
    with pytest.raises(ValidationError, match="workspace scope requires repo"):
        s.remember("broken", workspace="acme", repo="web", scope="workspace")


def test_merge_requires_explicit_wider_scope_for_different_sessions():
    service = _svc()
    first_session = service.start_session(
        "acme", repo="web", goal="first", force_new=True
    )
    second_session = service.start_session(
        "acme", repo="web", goal="second", force_new=True
    )
    first = service.remember(
        "First session deployment evidence.",
        workspace="acme",
        repo="web",
        session_id=first_session["session_id"],
        scope="session",
    )
    second = service.remember(
        "Second session deployment evidence.",
        workspace="acme",
        repo="web",
        session_id=second_session["session_id"],
        scope="session",
    )

    with pytest.raises(ValidationError, match="one session"):
        service.merge(
            [first["id"], second["id"]],
            "Combined deployment evidence.",
            workspace="acme",
        )

    assert service.store.get_memory(first["id"]).valid_to is None
    assert service.store.get_memory(second["id"]).valid_to is None
    merged = service.merge(
        [first["id"], second["id"]],
        "Combined deployment evidence.",
        workspace="acme",
        scope="repo",
    )
    assert service.store.get_memory(merged["id"]).scope == Scope.REPO


def test_postgres_schema_successful_retry_reuses_stable_chunk(monkeypatch):
    from engraphis.backends import postgres_schema

    snapshot = SchemaSnapshot(
        title="PostgreSQL schema: app",
        text="Table public.accounts has column account_id.",
        metadata={
            "database": "app",
            "schemas": ["public"],
            "source_digest": "0123456789abcdef01234567",
            "tables": 1,
        },
    )

    class _Introspector:
        def inspect(self, dsn, *, schemas=None):
            del dsn, schemas
            return snapshot

    monkeypatch.setattr(
        postgres_schema, "get_postgres_introspector", lambda: _Introspector()
    )
    service = _svc()

    first = service.import_postgres_schema(
        "postgresql://user:password@localhost/app",
        workspace="acme",
        schemas=["public"],
    )
    second = service.import_postgres_schema(
        "postgresql://user:password@localhost/app",
        workspace="acme",
        schemas=["public"],
    )

    assert second["memory_ids"] == first["memory_ids"]
    assert service.store.get_memory(first["id"]).valid_to is None


def test_recall_unknown_workspace_is_empty_not_error():
    s = _svc()
    r = s.recall("anything", workspace="does-not-exist")
    assert r["count"] == 0
    assert "note" in r


def test_session_lifecycle():
    s = _svc()
    started = s.start_session("acme", repo="web", agent="claude-code", goal="ship auth")
    sid = started["session_id"]
    assert sid.startswith("ses_") and started["status"] == "active"

    s.remember("Decided to use PASETO over JWT.", workspace="acme", repo="web",
               session_id=sid, mtype="episodic")
    ended = s.end_session(sid, summary="Auth migrated to PASETO.", outcome="shipped")
    assert ended["status"] == "summarized"

    with pytest.raises(ValidationError):
        s.end_session("ses_does_not_exist")


def test_stats_counts():
    s = _svc()
    s.remember("one", workspace="acme", mtype="semantic")
    s.remember("two", workspace="acme", mtype="procedural")
    st = s.stats(workspace="acme")
    # Local-agent writes do not create a pending + approved duplicate pair.
    assert st["memories"] == 2
    assert st["by_type"].get("procedural") == 1
    assert st["schema_version"] >= 2


@pytest.mark.parametrize("kwargs", [
    {"content": "", "workspace": "acme"},                       # empty content
    {"content": "x", "workspace": ""},                          # empty workspace
    {"content": "x", "workspace": "bad;name"},                  # illegal name char
    {"content": "x", "workspace": "acme", "mtype": "bogus"},    # bad enum
    {"content": "x", "workspace": "acme", "scope": "bogus"},    # bad enum
    {"content": "x" * 100_001, "workspace": "acme"},            # oversized content
])
def test_remember_validation_rejects_bad_input(kwargs):
    s = _svc()
    with pytest.raises(ValidationError):
        s.remember(**kwargs)


def test_control_characters_are_stripped():
    s = _svc()
    out = s.remember("hello\x00\x07world", workspace="acme")  # NUL + BEL injected
    rec = s.store.get_memory(out["id"])
    assert "\x00" not in rec.content and "\x07" not in rec.content
    assert rec.content == "helloworld"


def test_importance_is_clamped():
    s = _svc()
    out = s.remember("important", workspace="acme", importance=9.0)
    rec = s.store.get_memory(out["id"])
    assert rec.importance == 1.0


def test_update_memory_preserves_metadata_changes_on_a_correction_replacement():
    s = _svc()
    original = s.remember(
        "The original deployment runbook.", workspace="acme", title="Runbook",
        mtype="semantic", importance=0.2,
    )
    replacement = s.correct(
        original["id"], "The revised deployment runbook.", workspace="acme",
    )
    before_hlc = s.store.get_memory(replacement["id"]).modified_hlc

    out = s.update_memory(
        replacement["id"], workspace="acme", title="Deployment runbook",
        mtype="procedural", importance=0.9,
    )
    saved = s.store.get_memory(replacement["id"])

    assert out["updated"] == ["title", "type=procedural", "importance"]
    assert (saved.title, saved.mtype.value, saved.importance) == (
        "Deployment runbook", "procedural", 0.9,
    )
    assert saved.modified_hlc != before_hlc


def test_update_memory_advances_descriptive_hlc():
    service = MemoryService.create(":memory:", graph_extractor="none")
    created = service.remember("A synced deployment note.", workspace="acme", title="Old")
    before = service.store.get_memory(created["id"]).modified_hlc

    result = service.update_memory(
        created["id"], workspace="acme", title="New", importance=0.8,
    )

    after = service.store.get_memory(created["id"]).modified_hlc
    assert result["updated"] == ["title", "importance"]
    assert after != before
    assert service.store.conn.in_transaction is False


def test_update_memory_reembeds_changed_title_in_both_vector_mirrors(monkeypatch):
    service = MemoryService.create(":memory:")
    created = service.remember(
        "The release procedure uses a signed artifact.",
        workspace="acme", title="Initial runbook",
    )
    mid = created["id"]
    service.engine.embedder.model = "test-model"
    calls = []
    original = service.store.put_vector

    def traced_put_vector(memory_id, vector, *, model=""):
        calls.append(memory_id)
        return original(memory_id, vector, model=model)

    monkeypatch.setattr(service.store, "put_vector", traced_put_vector)
    service.update_memory(mid, workspace="acme", title="Nebula archival runbook")

    assert calls == [mid]
    row = service.store.conn.execute(
        "SELECT dim, vector, model FROM mem_vectors WHERE id=?", (mid,)
    ).fetchone()
    after = row["vector"]
    expected = service.engine.embedder.embed(
        ["Nebula archival runbook\nThe release procedure uses a signed artifact."]
    )[0]
    expected = expected / (float(np.linalg.norm(expected)) or 1.0)
    stored = np.frombuffer(after, dtype=np.float32)
    assert np.allclose(stored, expected)
    assert row["model"] == service.engine.embedding_space
    query_vec = service.engine.embedder.embed(["Nebula archival runbook"])[0]
    assert mid in {memory_id for memory_id, _score in service.engine.index.search(query_vec, 5)}
    assert mid in {memory_id for memory_id, _score in service.store.fts_search(
        "Nebula archival", 5)}
    assert mid not in {memory_id for memory_id, _score in service.store.fts_search(
        "Initial", 5)}


def test_update_memory_quarantined_title_does_not_embed_or_create_vector():
    service = MemoryService.create(":memory:")
    wid = service.store.get_or_create_workspace("acme")
    mid = service.store.add_memory(MemoryRecord(
        id="", content="untrusted retained payload", title="Old title",
        workspace_id=wid, scope=Scope.WORKSPACE,
        provenance={"trusted": False, "quarantined": True, "review_state": "pending"},
    ))
    calls = []

    def forbidden_embed(_texts):
        calls.append(True)
        raise AssertionError("quarantined title edits must not embed")

    service.engine.embedder.embed = forbidden_embed
    out = service.update_memory(mid, workspace="acme", title="Safe title")

    assert out["updated"] == ["title"]
    assert calls == []
    assert service.store.conn.execute(
        "SELECT 1 FROM mem_vectors WHERE id=?", (mid,)
    ).fetchone() is None

def test_update_memory_secret_title_does_not_embed_or_create_vector():
    service = MemoryService.create(":memory:")
    wid = service.store.get_or_create_workspace("acme")
    mid = service.store.add_memory(MemoryRecord(
        id="", content="private but non-credential payload", title="Old title",
        workspace_id=wid, scope=Scope.WORKSPACE, sensitivity="secret",
        provenance={"trusted": True, "review_state": "approved"},
    ))
    calls = []

    def forbidden_embed(_texts):
        calls.append(True)
        raise AssertionError("secret title edits must not embed")

    service.engine.embedder.embed = forbidden_embed
    service.update_memory(mid, workspace="acme", title="Safe title")

    assert calls == []
    assert service.store.conn.execute(
        "SELECT 1 FROM mem_vectors WHERE id=?", (mid,)
    ).fetchone() is None



def test_update_memory_commits_canonical_title_when_external_index_update_fails(caplog):
    service = MemoryService.create(":memory:")
    created = service.remember("A durable release note.", workspace="acme", title="Old")
    mid = created["id"]
    before_vector = service.store.conn.execute(
        "SELECT vector FROM mem_vectors WHERE id=?", (mid,)
    ).fetchone()["vector"]
    original_index = service.engine.index
    commits = []

    class BrokenIndex:
        dim = original_index.dim

        def upsert(self, _ids, _vectors, meta=None, *, commit=True):
            commits.append(commit)
            raise RuntimeError("sensitive-index-detail")

        def delete(self, _ids, *, commit=True):
            return None

    service.engine.index = BrokenIndex()
    with caplog.at_level("WARNING", logger="engraphis.service"):
        result = service.update_memory(mid, workspace="acme", title="New")

    assert result == {"id": mid, "updated": ["title"]}
    assert commits == [True]
    saved = service.store.get_memory(mid)
    assert saved.title == "New"
    assert service.store.conn.execute(
        "SELECT vector FROM mem_vectors WHERE id=?", (mid,)
    ).fetchone()["vector"] != before_vector
    assert mid in {
        memory_id for memory_id, _score in service.store.fts_search("New", 5)
    }
    audit = service.store.conn.execute(
        "SELECT detail FROM audit WHERE action='index_upsert_failed' AND target=?",
        (mid,),
    ).fetchone()
    assert audit is not None and audit["detail"] == "failure_type=RuntimeError"
    assert "sensitive-index-detail" not in caplog.text


def test_update_memory_late_store_failure_does_not_publish_external_vector(monkeypatch):
    service = MemoryService.create(":memory:")
    created = service.remember("A durable release note.", workspace="acme", title="Old")
    mid = created["id"]
    before_vector = service.store.conn.execute(
        "SELECT vector FROM mem_vectors WHERE id=?", (mid,)
    ).fetchone()["vector"]
    publications = []

    class RecordingExternalIndex:
        def upsert(self, ids, _vectors, meta=None, *, commit=True):
            publications.append(("upsert", tuple(ids), commit))

        def delete(self, ids, *, commit=True):
            publications.append(("delete", tuple(ids), commit))

    service.engine.index = RecordingExternalIndex()
    original_audit = service.store.audit

    def fail_late(actor, action, target, detail="", *, commit=True):
        if action == "memory_update":
            raise RuntimeError("late store failure")
        return original_audit(actor, action, target, detail, commit=commit)

    monkeypatch.setattr(service.store, "audit", fail_late)
    with pytest.raises(RuntimeError, match="late store failure"):
        service.update_memory(mid, workspace="acme", title="New")

    assert service.store.get_memory(mid).title == "Old"
    assert service.store.conn.execute(
        "SELECT vector FROM mem_vectors WHERE id=?", (mid,)
    ).fetchone()["vector"] == before_vector
    assert publications == []


def test_update_memory_commit_failure_does_not_publish_external_vector(monkeypatch):
    service = MemoryService.create(":memory:")
    created = service.remember("A durable release note.", workspace="acme", title="Old")
    mid = created["id"]
    publications = []

    class RecordingExternalIndex:
        def upsert(self, ids, _vectors, meta=None, *, commit=True):
            publications.append(("upsert", tuple(ids), commit))

        def delete(self, ids, *, commit=True):
            publications.append(("delete", tuple(ids), commit))

    service.engine.index = RecordingExternalIndex()
    connection_type = type(service.store.conn)
    real_commit = connection_type.commit

    def fail_outer_commit(connection):
        if (
            connection is service.store.conn
            and not getattr(connection._pin, "defer_commits", 0)
        ):
            raise RuntimeError("late commit failure")
        return real_commit(connection)

    monkeypatch.setattr(connection_type, "commit", fail_outer_commit)
    with pytest.raises(RuntimeError, match="late commit failure"):
        service.update_memory(mid, workspace="acme", title="New")

    assert service.store.get_memory(mid).title == "Old"
    assert publications == []

def test_update_memory_rebuilds_missing_fts_row_when_title_is_reapplied():
    service = MemoryService.create(":memory:")
    created = service.remember(
        "The release procedure uses a signed artifact.",
        workspace="acme", title="Existing runbook",
    )
    mid = created["id"]
    service.store.conn.execute("DELETE FROM mem_fts WHERE id=?", (mid,))
    service.store.conn.commit()
    assert service.store.fts_search("Existing", 5) == []

    service.update_memory(mid, workspace="acme", title="Existing runbook")

    assert mid in {
        memory_id for memory_id, _score in service.store.fts_search("Existing", 5)
    }


def test_conflict_review_hides_another_callers_session_memory():
    service = MemoryService.create(":memory:")
    try:
        set_current_user({
            "id": "usr_alice", "email": "alice@example.test", "role": "member",
        })
        service.create_workspace("acme", visibility="shared", confirmed=True)
        session = service.start_session("acme", repo="web", goal="private review")
        private = service.remember(
            "Alice's private pending review item.",
            workspace="acme", repo="web", session_id=session["session_id"],
            scope="session", source="import", trusted=False,
        )

        set_current_user({
            "id": "usr_bob", "email": "bob@example.test", "role": "member",
        })
        review = service.conflict_review(workspace="acme", repo="web")
        assert private["id"] not in {item["id"] for item in review["items"]}
    finally:
        set_current_user(None)


def test_first_use_workspace_rechecks_personal_owner_after_atomic_race(monkeypatch):
    service = MemoryService.create(':memory:')
    try:
        set_current_user({'id': 'usr_alice', 'email': 'alice@example.test', 'role': 'member'})
        winner_id = service.store.create_workspace(
            'raced', settings={'visibility': 'personal', 'owner': 'bob@example.test'},
        )

        def return_racing_winner(_name, *, settings=None):
            del settings
            return winner_id

        monkeypatch.setattr(service.store, 'get_or_create_workspace', return_racing_winner)
        with pytest.raises(ValidationError, match='personal folder of another user'):
            service.remember('must not enter the raced workspace', workspace='raced')
    finally:
        set_current_user(None)


def test_conflict_review_pages_past_ineligible_newer_rows_before_limit():
    service = MemoryService.create(":memory:")
    wid = service.store.get_or_create_workspace("acme")
    for index in range(120):
        service.store.add_memory(MemoryRecord(
            id="", content=f"ordinary memory {index}", scope=Scope.WORKSPACE,
            workspace_id=wid, ingested_at=1000.0 + index,
            provenance={"trusted": True, "review_state": "approved"},
        ))
    eligible = service.store.add_memory(MemoryRecord(
        id="", content="old pending review evidence", scope=Scope.WORKSPACE,
        workspace_id=wid, ingested_at=1.0,
        provenance={"trusted": False, "review_state": "pending"},
    ))

    review = service.conflict_review(workspace="acme", limit=1)
    assert review["count"] == 1
    assert review["items"][0]["id"] == eligible


def test_provenance_recorded():
    s = _svc()
    out = s.remember("traceable fact", workspace="acme", source="unit-test")
    pending = s.store.get_memory(out["pending_id"])
    approved = s.store.get_memory(out["id"])
    assert pending.metadata.get("provenance", {}).get("source") == "unit-test"
    assert approved.provenance["source"] == "human_review"
    assert approved.provenance["approved_from"] == pending.id


def test_mcp_operator_attestation_does_not_approve_external_ingest():
    service = MemoryService.create(":memory:")
    result = service.ingest(
        "Imported release notes mention an amber rollout marker.",
        workspace="acme",
        source="import",
        trusted=True,
        _local_agent_operator=True,
        _ingress="mcp",
    )
    record = service.store.get_memory(result["facts"][0]["id"])
    assert record.provenance["trusted"] is False
    assert record.provenance["review_state"] == "pending"
    assert record.provenance["ingress"] == "mcp"
    recalled = service.recall("amber rollout marker", workspace="acme")
    assert record.id not in {item["id"] for item in recalled["memories"]}


# ── conflict resolution on the write path ───────────────────────────────────────

def test_remember_reports_add_op():
    s = _svc()
    out = s.remember("We use pnpm.", workspace="acme", repo="web")
    assert out["op"] == "add"


def test_local_agent_writes_dedupe_without_owner_approval():
    s = _svc()
    text = "We standardized on pnpm as the package manager for all frontend repos."
    first = s.remember(text, workspace="acme", repo="web")
    out = s.remember(text, workspace="acme", repo="web")
    assert out["op"] == "noop"
    assert out["id"] == first["id"]


def test_local_agent_writes_invalidate_without_owner_approval():
    s = _svc()
    first = s.remember("Until 2026-01 the rate limit was 100 requests per minute per API key.",
                       workspace="acme", repo="web")
    second = s.remember(
        "As of 2026-02 the rate limit was raised to 500 requests per minute per API key.",
        workspace="acme", repo="web")
    assert second["op"] == "invalidate"
    assert s.store.get_memory(first["id"]).valid_to is not None


def test_remember_resolve_conflicts_false_keeps_both():
    s = _svc()
    text = "Build failed again."
    a = s.remember(text, workspace="acme", repo="web", mtype="episodic", resolve_conflicts=False)
    b = s.remember(text, workspace="acme", repo="web", mtype="episodic", resolve_conflicts=False)
    assert a["op"] == "add" and b["op"] == "add" and a["id"] != b["id"]


# ── session continuity (cross-session handoff) ───────────────────────────────────

def test_start_session_bootstraps_from_prior_session():
    s = _svc()
    first = s.start_session("acme", repo="web", goal="refactor auth")
    assert first["bootstrap"] == {}
    s.end_session(first["session_id"], summary="mid-refactor", outcome="blocked",
                  open_threads=["tests 3-5 failing"])
    second = s.start_session("acme", repo="web", goal="finish refactor")
    assert second["bootstrap"]["summary"] == "mid-refactor"
    assert second["bootstrap"]["open_threads"] == ["tests 3-5 failing"]
    assert second["bootstrap"]["outcome"] == "blocked"


# ── governance: forget / pin / correct ──────────────────────────────────────────

def test_forget_then_recall_excludes_it():
    s = _svc()
    out = s.remember("A fact to forget.", workspace="acme", repo="web")
    s.forget(out["id"], workspace="acme", repo="web", reason="no longer relevant")
    r = s.recall("fact to forget", workspace="acme", repo="web")
    assert all(m["id"] != out["id"] for m in r["memories"])


def test_forget_unknown_id_raises_validation_error():
    s = _svc()
    s.remember("anchor", workspace="acme")   # workspace must exist for _require_scope
    with pytest.raises(ValidationError):
        s.forget("mem_does_not_exist", workspace="acme")


def test_forget_wrong_workspace_raises_validation_error():
    s = _svc()
    out = s.remember("Alpha's private fact.", workspace="alpha")
    s.remember("anchor", workspace="beta")
    with pytest.raises(ValidationError):
        s.forget(out["id"], workspace="beta")          # beta doesn't own alpha's memory
    r = s.recall("private fact", workspace="alpha")
    assert any(m["id"] == out["id"] for m in r["memories"])   # untouched


def test_pin_roundtrip():
    s = _svc()
    out = s.remember("Pin me.", workspace="acme")
    pinned = s.pin(out["id"], workspace="acme")
    assert pinned["pinned"] is True
    unpinned = s.pin(out["id"], workspace="acme", pinned=False)
    assert unpinned["pinned"] is False


def test_pin_wrong_workspace_raises_validation_error():
    s = _svc()
    out = s.remember("Alpha's private fact.", workspace="alpha")
    s.remember("anchor", workspace="beta")
    with pytest.raises(ValidationError):
        s.pin(out["id"], workspace="beta")


def test_correct_supersedes():
    s = _svc()
    out = s.remember("The API key header is X-Auth-Key.", workspace="acme")
    corrected = s.correct(out["id"], "The API key header is X-Api-Key.", workspace="acme",
                          reason="typo")
    assert corrected["superseded"] == [out["id"]]
    r = s.recall("API key header", workspace="acme")
    assert any("X-Api-Key" in m["content"] for m in r["memories"])


def test_correct_wrong_workspace_raises_validation_error():
    s = _svc()
    out = s.remember("Alpha's private fact.", workspace="alpha")
    s.remember("anchor", workspace="beta")
    with pytest.raises(ValidationError):
        s.correct(out["id"], "tampered", workspace="beta")


def test_correct_translates_engine_value_error_to_validation_error():
    """A deliberate engine ValueError must remain actionable at the service boundary."""
    s = _svc()
    out = s.remember("A fact worth correcting.", workspace="acme")

    def _boom(*_args, **_kwargs):
        raise ValueError("session scope requires session_id")

    s.engine.correct = _boom
    with pytest.raises(ValidationError, match="session scope requires"):
        s.correct(out["id"], "replacement", workspace="acme")


def test_pin_translates_engine_value_error_to_validation_error():
    s = _svc()
    out = s.remember("A fact worth pinning.", workspace="acme")

    def _boom(*_args, **_kwargs):
        raise ValueError("cannot pin an expired memory")

    s.engine.pin = _boom
    with pytest.raises(ValidationError, match="cannot pin"):
        s.pin(out["id"], workspace="acme")


def test_promote_repo_memory_to_workspace():
    s = _svc()
    source = s.remember(
        "Every service uses structured JSON logs.",
        workspace="acme", repo="api", scope="repo",
    )

    promoted = s.promote(
        source["id"], "workspace", workspace="acme", repo="api",
        reason="confirmed across repositories",
    )

    assert promoted["scope"] == "workspace"
    assert promoted["promoted_from"] == source["id"]
    assert promoted["receipt"]["operation"] == "promote"
    rec = s.store.get_memory(promoted["id"])
    assert rec.repo_id is None
    assert s.store.get_memory(source["id"]).valid_to is not None


def test_promote_rejects_wrong_workspace_and_non_widening_scope():
    s = _svc()
    source = s.remember("Alpha convention.", workspace="alpha", repo="api")
    s.remember("Beta anchor.", workspace="beta")

    with pytest.raises(ValidationError):
        s.promote(source["id"], "workspace", workspace="beta")
    with pytest.raises(ValidationError, match="must widen"):
        s.promote(source["id"], "repo", workspace="alpha", repo="api")


def test_promote_session_to_repo_infers_repo_name():
    s = _svc()
    session = s.start_session("acme", repo="api", force_new=True)
    source = s.remember(
        "This session finding is now a repo convention.",
        workspace="acme", repo="api", session_id=session["session_id"],
        scope="session",
    )

    promoted = s.promote(source["id"], "repo", workspace="acme")

    assert promoted["scope"] == "repo" and promoted["repo"] == "api"


# ── bi-temporal: why / timeline / recall_proactive ───────────────────────────────

def test_why_returns_answer_and_history():
    s = _svc()
    s.remember("Until 2026-01 the rate limit was 100 requests per minute per API key.",
              workspace="acme", repo="web")
    s.remember("As of 2026-02 the rate limit was raised to 500 requests per minute per API key.",
              workspace="acme", repo="web")
    out = s.why("what is the rate limit", workspace="acme", repo="web")
    assert any("500" in m["content"] for m in out["answer"])
    assert any("100" in m["content"] for m in out["supersedes"])


def test_why_unknown_workspace_raises():
    s = _svc()
    with pytest.raises(ValidationError):
        s.why("anything", workspace="does-not-exist")


def test_timeline_orders_chronologically():
    s = _svc()
    s.remember("Until 2026-01 the rate limit was 100 requests per minute per API key.",
              workspace="acme", repo="web")
    s.remember("As of 2026-02 the rate limit was raised to 500 requests per minute per API key.",
              workspace="acme", repo="web")
    out = s.timeline("rate limit", workspace="acme", repo="web")
    # Prompt-visible local-agent history contains the active record and its
    # bi-temporal predecessor, without a pending/approved duplicate pair.
    assert len(out["history"]) == 2
    assert out["history"][0]["valid_from"] <= out["history"][-1]["valid_from"]


@pytest.mark.parametrize(
    ("intent", "result_key", "engine_method"),
    [
        ("why", "explanation", "why"),
        ("timeline", "history", "timeline"),
    ],
)
def test_intent_recall_forwards_temporal_anchors_to_secondary_reads(
        monkeypatch, intent, result_key, engine_method):
    s = _svc()
    s.remember("Temporal intent anchor regression fixture.", workspace="acme", repo="web")
    observed = {}

    def observe(*args, **kwargs):
        observed.update(kwargs)
        return {"answer": [], "supersedes": []} if engine_method == "why" else []

    monkeypatch.setattr(s.engine, engine_method, observe)
    out = s.intent_recall(
        "Temporal intent anchor", intent=intent, workspace="acme", repo="web",
        as_of=10.0, valid_at=10.0, known_at=20.0,
    )

    assert result_key in out
    assert observed["valid_at"] == 10.0
    assert observed["known_at"] == 20.0


def test_service_exposes_world_time_writes_and_point_in_time_recall():
    s = _svc()
    old = s.remember(
        "The API rate limit is 100 requests per minute.",
        workspace="acme",
        repo="web",
        valid_from=1_000.0,
    )
    new = s.remember(
        "The API rate limit is 500 requests per minute.",
        workspace="acme",
        repo="web",
        valid_from=2_000.0,
    )

    before = s.recall(
        "What is the API rate limit?",
        workspace="acme",
        repo="web",
        as_of=1_500.0,
        reinforce=False,
    )
    after = s.recall(
        "What is the API rate limit?",
        workspace="acme",
        repo="web",
        as_of=2_500.0,
        reinforce=False,
    )
    assert [memory["id"] for memory in before["memories"]] == [old["id"]]
    assert {memory["id"] for memory in after["memories"]} == {new["id"]}


@pytest.mark.parametrize(
    ("method", "kwargs"),
    [
        ("remember", {"content": "A fact.", "workspace": "acme", "valid_from": float("nan")}),
        ("remember", {"content": "A fact.", "workspace": "acme", "valid_from": True}),
        ("recall", {"query": "A fact.", "workspace": "acme", "as_of": float("inf")}),
        (
            "grounded_recall",
            {"query": "A fact.", "workspace": "acme", "as_of": "not-a-time"},
        ),
    ],
)
def test_service_rejects_invalid_temporal_anchors(method, kwargs):
    s = _svc()
    with pytest.raises(ValidationError, match="finite timestamp"):
        getattr(s, method)(**kwargs)


def test_external_backdated_candidate_remains_passive_without_supersession():
    s = _svc()
    original = s.remember(
        "The deployment window is Friday afternoon.",
        workspace="acme",
        valid_from=2_000.0,
    )

    candidate = s.remember(
        "The deployment window is Thursday afternoon.",
        workspace="acme",
        source="web",
        trusted=False,
        valid_from=1_000.0,
    )

    assert candidate["op"] == "add"
    assert s.store.get_memory(original["id"]).valid_to is None


def test_recall_proactive_includes_last_session():
    s = _svc()
    s.remember("High importance convention.", workspace="acme", repo="web", importance=0.9)
    started = s.start_session("acme", repo="web")
    s.end_session(started["session_id"], summary="mid-work", open_threads=["thing left undone"])
    out = s.recall_proactive(workspace="acme", repo="web")
    assert out["memories"]
    assert out["last_session"]["open_threads"] == ["thing left undone"]


def test_recall_proactive_filters_untrusted_before_applying_k():
    s = _svc()
    s.remember(
        "A trusted project convention.", workspace="acme", repo="web",
        importance=0.1,
    )
    untrusted = s.remember(
        "A high-priority imported instruction.", workspace="acme", repo="web",
        importance=1.0, source="import", trusted=False,
    )

    out = s.recall_proactive(workspace="acme", repo="web", k=1)

    assert len(out["memories"]) == 1
    assert out["memories"][0]["id"] != untrusted["id"]


# ── linking & events ─────────────────────────────────────────────────────────────

def test_record_event_and_link():
    s = _svc()
    a = s.remember("Memory A.", workspace="acme", repo="web")
    b = s.remember("Memory B.", workspace="acme", repo="web")
    ev = s.record_event("decision", "Chose PASETO over JWT.", workspace="acme", repo="web")
    assert ev["id"].startswith("evt_")
    link = s.link(a["id"], b["id"], workspace="acme", repo="web", relation="related")
    assert link["linked"] is True


def test_link_unknown_id_raises():
    s = _svc()
    a = s.remember("Memory A.", workspace="acme")
    with pytest.raises(ValidationError):
        s.link(a["id"], "mem_nope", workspace="acme")


def test_link_wrong_workspace_raises_validation_error():
    s = _svc()
    a = s.remember("Alpha's fact.", workspace="alpha")
    b = s.remember("Beta's fact.", workspace="beta")
    with pytest.raises(ValidationError):
        s.link(a["id"], b["id"], workspace="alpha")    # b isn't alpha's to link


# ── code-symbol graph ─────────────────────────────────────────────────────────────

def test_index_repo_and_search_code(tmp_path):
    (tmp_path / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n"
    )
    s = _svc()
    report = s.index_repo(workspace="acme", repo="sample", root_path=str(tmp_path))
    assert report["files_indexed"] == 1
    out = s.search_code("add", workspace="acme", repo="sample")
    assert any(sym["name"] == "add" for sym in out["symbols"])


def test_link_symbol_rejects_ambiguous_short_name_but_accepts_qualified_name():
    s = _svc()
    workspace_id = s.store.get_or_create_workspace("acme")
    repo_id = s.store.get_or_create_repo(workspace_id, "api")
    first = s.store.upsert_symbol(
        repo_id=repo_id, kind="function", name="deploy", fqname="api.deploy",
        file="api.py", span="1-1",
    )
    s.store.upsert_symbol(
        repo_id=repo_id, kind="function", name="deploy", fqname="worker.deploy",
        file="worker.py", span="1-1",
    )
    memory = s.remember("Deploy uses the release runbook.", workspace="acme", repo="api")

    with pytest.raises(ValidationError, match="ambiguous"):
        s.link_symbol("deploy", memory["id"], workspace="acme", repo="api")

    linked = s.link_symbol("api.deploy", memory["id"], workspace="acme", repo="api")
    assert linked["symbol_id"] == first
    assert linked["receipt"]["operation"] == "link"
    assert s.store.conn.execute(
        "SELECT action FROM audit WHERE action='link_symbol' AND target=?", (linked["link_id"],)
    ).fetchone() is not None

    repeated = s.link_symbol("api.deploy", memory["id"], workspace="acme", repo="api")
    assert repeated["link_id"] == linked["link_id"]
    assert s.store.conn.execute(
        "SELECT COUNT(*) FROM code_memory_links WHERE repo_id=? AND symbol_id=? "
        "AND memory_id=? AND relation=? AND valid_to IS NULL AND expired_at IS NULL",
        (repo_id, first, memory["id"], "mentions"),
    ).fetchone()[0] == 1


def test_link_symbol_concurrent_creation_is_idempotent():
    """Multiple threads racing to create the same symbol-memory link must produce exactly one row."""
    from concurrent.futures import ThreadPoolExecutor

    s = _svc()
    workspace_id = s.store.get_or_create_workspace("acme")
    repo_id = s.store.get_or_create_repo(workspace_id, "api")
    symbol_id = s.store.upsert_symbol(
        repo_id=repo_id, kind="function", name="race_target", fqname="race_target",
        file="race.py", span="1-1",
    )
    memory = s.remember("Race condition test", workspace="acme", repo="api")

    def link_concurrently():
        return s.link_symbol("race_target", memory["id"], workspace="acme", repo="api")

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(link_concurrently) for _ in range(20)]
        results = [f.result() for f in futures]

    link_ids = {r["link_id"] for r in results}
    assert len(link_ids) == 1, f"expected 1 unique link_id, got {len(link_ids)}: {link_ids}"

    count = s.store.conn.execute(
        "SELECT COUNT(*) FROM code_memory_links WHERE repo_id=? AND symbol_id=? "
        "AND memory_id=? AND relation=? AND valid_to IS NULL AND expired_at IS NULL",
        (repo_id, symbol_id, memory["id"], "mentions"),
    ).fetchone()[0]
    assert count == 1, f"expected 1 row in code_memory_links, got {count}"


def test_search_code_requires_repo():
    s = _svc()
    s.remember("x", workspace="acme")
    with pytest.raises(ValidationError):
        s.search_code("add", workspace="acme", repo="")


def test_service_code_search_honors_bitemporal_anchors():
    """The public service must not append present-day code to historic recall."""
    from engraphis.core.interfaces import MemoryRecord, Scope

    s = _svc()
    workspace_id = s.store.get_or_create_workspace("acme")
    repo_id = s.store.get_or_create_repo(workspace_id, "api")
    symbol_id = s.store.upsert_symbol(
        repo_id=repo_id, kind="function", name="legacy_route", fqname="legacy_route",
        file="legacy.py", span="1-1",
    )
    memory_id = s.store.add_memory(MemoryRecord(
        id="", content="legacy_route handled historic requests", title="legacy route",
        workspace_id=workspace_id, repo_id=repo_id, scope=Scope.REPO,
        valid_from=10.0, ingested_at=10.0,
        provenance={"source": "test", "trusted": True, "review_state": "approved"},
    ))
    s.store.link_memory_symbol(repo_id=repo_id, symbol_id=symbol_id, memory_id=memory_id)
    for table in ("symbols", "code_memory_links"):
        s.store.conn.execute(
            f"UPDATE {table} SET valid_from=10, ingested_at=10 WHERE repo_id=?", (repo_id,)
        )
    s.store.conn.commit()
    s.store.close_validity(memory_id, at=20.0)
    s.store.clear_symbols_for_file(repo_id, "legacy.py")
    closed_at = s.store.conn.execute(
        "SELECT valid_to FROM symbols WHERE id=?", (symbol_id,)
    ).fetchone()["valid_to"]

    current = s.search_code("legacy_route", workspace="acme", repo="api")
    historic = s.search_code(
        "legacy_route", workspace="acme", repo="api", valid_at=15.0,
        known_at=float(closed_at) + 1.0,
    )

    assert current["symbols"] == []
    assert [symbol["id"] for symbol in historic["symbols"]] == [symbol_id]
    with pytest.raises(ValidationError, match="as_of and valid_at"):
        s.search_code(
            "legacy_route", workspace="acme", repo="api", as_of=14.0, valid_at=15.0
        )


def test_service_code_export_honors_bitemporal_anchors():
    """Every export companion must be rendered from one anchored graph payload."""
    from engraphis.core.interfaces import MemoryRecord, Scope

    s = _svc()
    workspace_id = s.store.get_or_create_workspace("acme")
    repo_id = s.store.get_or_create_repo(workspace_id, "api")
    symbol_id = s.store.upsert_symbol(
        repo_id=repo_id, kind="function", name="legacy_route", fqname="legacy_route",
        file="legacy.py", span="1-1",
    )
    memory_id = s.store.add_memory(MemoryRecord(
        id="", content="legacy_route handled historic requests", title="legacy route",
        workspace_id=workspace_id, repo_id=repo_id, scope=Scope.REPO,
        valid_from=10.0, ingested_at=10.0,
        provenance={"source": "test", "trusted": True, "review_state": "approved"},
    ))
    s.store.link_memory_symbol(
        repo_id=repo_id, symbol_id=symbol_id, memory_id=memory_id
    )
    for table in ("symbols", "code_memory_links"):
        s.store.conn.execute(
            f"UPDATE {table} SET valid_from=10, ingested_at=10 WHERE repo_id=?",
            (repo_id,),
        )
    s.store.conn.commit()
    s.store.close_validity(memory_id, at=20.0)
    s.store.clear_symbols_for_file(repo_id, "legacy.py")
    learned_close = s.store.conn.execute(
        "SELECT valid_to FROM symbols WHERE id=?", (symbol_id,)
    ).fetchone()["valid_to"]

    current = s.export_code_graph(workspace="acme", repo="api")
    before_ingestion = s.export_code_graph(
        workspace="acme", repo="api", valid_at=15.0, known_at=9.0,
    )
    historical = s.export_code_graph(
        workspace="acme", repo="api", as_of=15.0, valid_at=15.0,
        known_at=float(learned_close) + 1.0,
    )

    assert current["graph"]["nodes"] == []
    assert before_ingestion["graph"]["nodes"] == []
    assert {row["id"] for row in historical["graph"]["nodes"]} == {symbol_id}
    assert {row["memory_id"] for row in historical["graph"]["memory_links"]} == {
        memory_id
    }
    assert "- Symbols: 1" in historical["report_markdown"]
    assert "legacy_route" in historical["graph_html"]
    assert historical["valid_at"] == 15.0
    assert historical["known_at"] == float(learned_close) + 1.0
    assert historical["historical"] is True
    with pytest.raises(ValidationError, match="as_of and valid_at"):
        s.export_code_graph(
            workspace="acme", repo="api", as_of=14.0, valid_at=15.0
        )


# ── folder / file import (dashboard "Import files & folders" section, SECURITY.md §5) ─

def test_import_folder_success(tmp_path, monkeypatch):
    (tmp_path / "notes.md").write_text("# DB choice\nWe use Postgres 16.\n")
    (tmp_path / "empty.md").write_text("   \n")
    (tmp_path / "skip.txt").write_text("wrong pattern, not imported")
    monkeypatch.setenv("ENGRAPHIS_IMPORT_ROOTS", str(tmp_path))
    s = _svc()
    report = s.import_folder(workspace="acme", path=str(tmp_path))
    assert report["scanned"] == 2          # only *.md matched skip.txt is excluded
    assert report["imported"] == 1
    assert report["skipped"] == 1          # empty.md
    r = s.recall("Postgres", workspace="acme", include_untrusted=True)
    assert any("Postgres" in m["content"] for m in r["memories"])


def test_import_folder_marks_untrusted(tmp_path, monkeypatch):
    (tmp_path / "a.md").write_text("An imported fact about narwhals.")
    monkeypatch.setenv("ENGRAPHIS_IMPORT_ROOTS", str(tmp_path))
    s = _svc()
    s.import_folder(workspace="acme", path=str(tmp_path))
    r = s.recall("narwhals", workspace="acme", include_untrusted=True)
    assert r["memories"], "expected the imported memory to be recallable"
    prov = r["memories"][0]["provenance"]
    assert prov["source"] == "import" and prov["trusted"] is False
    assert prov["kind"] == "file_import"


def test_import_folder_respects_file_pattern(tmp_path, monkeypatch):
    (tmp_path / "a.md").write_text("markdown note")
    (tmp_path / "b.txt").write_text("text note")
    monkeypatch.setenv("ENGRAPHIS_IMPORT_ROOTS", str(tmp_path))
    s = _svc()
    report = s.import_folder(workspace="acme", path=str(tmp_path), file_pattern="*.txt")
    assert report["scanned"] == 1 and report["imported"] == 1
    r = s.recall("text note", workspace="acme", include_untrusted=True)
    assert any("text note" in m["content"] for m in r["memories"])


def test_import_folder_missing_path_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("ENGRAPHIS_IMPORT_ROOTS", str(tmp_path))
    s = _svc()
    with pytest.raises(ValidationError):
        s.import_folder(workspace="acme", path=str(tmp_path / "does-not-exist"))


def test_import_folder_error_messages_do_not_echo_user_path(tmp_path, monkeypatch):
    """SEC-001: import error messages must not echo the user-supplied path back to
    the caller — leaking filesystem structure aids path-traversal reconnaissance."""
    sentinel = "zzz-hostile-sentinel-zzz"
    hostile = str(tmp_path / sentinel / "secret.md")
    monkeypatch.delenv("ENGRAPHIS_IMPORT_ROOTS", raising=False)
    import pathlib
    decoy_home = tmp_path / "decoy-home"
    decoy_home.mkdir()
    monkeypatch.setattr(pathlib.Path, "home", lambda: decoy_home)
    s = _svc()
    # Path traversal rejection — sentinel must not appear in the message.
    with pytest.raises(ValidationError) as exc_info:
        s.import_folder(workspace="acme", path=hostile)
    assert sentinel not in str(exc_info.value)
    # "path not found" — sentinel must not appear.
    allowed_but_gone = tmp_path / sentinel
    monkeypatch.setenv("ENGRAPHIS_IMPORT_ROOTS", str(tmp_path))
    with pytest.raises(ValidationError) as exc_info:
        s.import_folder(workspace="acme", path=str(allowed_but_gone))
    assert sentinel not in str(exc_info.value)
    # "not a directory" — sentinel must not appear.
    blocker = tmp_path / sentinel
    blocker.write_text("not a dir", encoding="utf-8")
    with pytest.raises(ValidationError) as exc_info:
        s.import_folder(workspace="acme", path=str(blocker))
    assert sentinel not in str(exc_info.value)

def test_import_folder_path_traversal_blocked(tmp_path, monkeypatch):
    """A path outside the allowed roots (home dir / ENGRAPHIS_IMPORT_ROOTS) must be
    refused before anything under it is read — SECURITY.md §5's threat model treats the
    path as attacker-controlled (any team member who can reach the dashboard, or a
    prompt-injected agent calling through it)."""
    import pathlib
    decoy_home = tmp_path / "decoy-home"
    decoy_home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("do not import me")
    monkeypatch.delenv("ENGRAPHIS_IMPORT_ROOTS", raising=False)
    monkeypatch.setattr(pathlib.Path, "home", lambda: decoy_home)
    s = _svc()
    with pytest.raises(ValidationError):
        s.import_folder(workspace="acme", path=str(outside))


def test_import_folder_allows_home_directory(tmp_path, monkeypatch):
    """A path *under* the (possibly faked) home directory is allowed without needing
    ENGRAPHIS_IMPORT_ROOTS — the default, no-config case."""
    import pathlib
    home = tmp_path / "home"
    sub = home / "notes"
    sub.mkdir(parents=True)
    (sub / "a.md").write_text("fact under home")
    monkeypatch.delenv("ENGRAPHIS_IMPORT_ROOTS", raising=False)
    monkeypatch.setattr(pathlib.Path, "home", lambda: home)
    s = _svc()
    report = s.import_folder(workspace="acme", path=str(sub))
    assert report["imported"] == 1


def test_import_folder_symlink_escape_blocked(tmp_path, monkeypatch):
    """A symlink *inside* an allowed root that points *outside* it must not let
    ``import_folder`` read the target — ``rglob`` follows symlinked directories, so
    ``_resolve_import_root``'s containment check on the root alone isn't enough; each
    candidate file is re-resolved and re-contained in ``_iter_import_files``."""
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside-secret"
    outside.mkdir()
    (outside / "secret.md").write_text("classified narwhal launch codes")
    try:
        (allowed / "escape").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported in this environment")
    monkeypatch.setenv("ENGRAPHIS_IMPORT_ROOTS", str(allowed))
    s = _svc()
    report = s.import_folder(workspace="acme", path=str(allowed))
    assert report["imported"] == 0 and report["scanned"] == 0
    r = s.recall("narwhal launch codes", workspace="acme")
    assert not any("launch codes" in m["content"] for m in r["memories"])


def test_import_files_success():
    s = _svc()
    report = s.import_files(workspace="acme", files=[
        {"name": "one.md", "content": "# Title\nA fact about pangolins."},
        {"name": "two.md", "content": ""},
    ])
    assert report["imported"] == 1
    assert report["skipped"] == 1
    r = s.recall("pangolins", workspace="acme", include_untrusted=True)
    assert any("pangolins" in m["content"] for m in r["memories"])


def test_import_files_marks_untrusted_with_upload_kind():
    s = _svc()
    s.import_files(workspace="acme", files=[
        {"name": "x.md", "content": "A fact about uploaded quokkas."}])
    r = s.recall("quokkas", workspace="acme", include_untrusted=True)
    prov = r["memories"][0]["provenance"]
    assert prov["source"] == "import" and prov["trusted"] is False
    assert prov["kind"] == "file_upload"


def test_import_files_caps_count():
    s = _svc()
    too_many = [{"name": f"f{i}.md", "content": "x"} for i in range(600)]
    with pytest.raises(ValidationError):
        s.import_files(workspace="acme", files=too_many)


def test_import_files_rejects_non_list():
    s = _svc()
    with pytest.raises(ValidationError):
        s.import_files(workspace="acme", files={"name": "a.md", "content": "x"})


def test_import_files_failure_preserves_caller_owned_transaction(monkeypatch):
    service = MemoryService.create(":memory:")
    created = service.create_workspace("caller-owned-import")
    conn = service.store.conn
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "UPDATE workspaces SET settings=? WHERE id=?",
        ('{"outer":"preserved"}', created["id"]),
    )

    def fail_fts(*args, **kwargs):
        raise RuntimeError("fts unavailable")

    monkeypatch.setattr(service.store, "_fts_upsert", fail_fts)

    with pytest.raises(RuntimeError, match="fts unavailable"):
        service.import_files(
            workspace="caller-owned-import",
            files=[{"name": "fact.md", "content": "A durable imported fact."}],
        )

    assert conn.in_transaction is True
    assert conn.transaction_owned_by_current_thread() is True
    assert conn.execute(
        "SELECT settings FROM workspaces WHERE id=?", (created["id"],)
    ).fetchone()["settings"] == '{"outer":"preserved"}'
    assert conn.execute(
        "SELECT COUNT(*) FROM memories WHERE workspace_id=?", (created["id"],)
    ).fetchone()[0] == 0
    conn.rollback()


class _CloseStore:
    def __init__(self) -> None:
        self.close_calls = 0
        self.closed = False
        self.allowed_workspaces = None

    def close(self) -> None:
        self.close_calls += 1
        self.closed = True


def _service_with_close_store():
    store = _CloseStore()
    engine = type("_Engine", (), {"store": store})()
    return MemoryService(engine), store


def test_service_close_waits_for_owned_workers_before_store_close():
    service, store = _service_with_close_store()
    worker_observation = []

    def worker():
        while not service._closing:
            time.sleep(0.001)
        worker_observation.append(store.closed)

    thread = threading.Thread(target=worker)
    service._graph_job_threads["job_1"] = thread
    thread.start()

    service.close(timeout=1)

    assert worker_observation == [False]
    assert not thread.is_alive()
    assert store.close_calls == 1
    service.close(timeout=1)
    assert store.close_calls == 1


def test_service_close_keeps_store_open_when_worker_misses_deadline():
    service, store = _service_with_close_store()
    release = threading.Event()
    thread = threading.Thread(target=release.wait)
    service._graph_job_threads["job_1"] = thread
    thread.start()

    with pytest.raises(RuntimeError, match="did not stop before shutdown"):
        service.close(timeout=0)

    assert store.close_calls == 0
    release.set()
    thread.join(1)
    service.close(timeout=1)
    assert store.close_calls == 1


def test_graph_index_job_rejects_new_work_during_shutdown():
    service = MemoryService.create(":memory:", graph_extractor="none")
    service.create_workspace("acme")
    service._closing = True

    with pytest.raises(ValidationError, match="shutting down"):
        service.start_graph_index_job(workspace="acme")

    service._closing = False
    service.close()


def test_graph_index_job_reuse_preserves_caller_owned_transaction(monkeypatch):
    service = MemoryService.create(":memory:", graph_extractor="none")
    service.create_workspace("acme")
    started = threading.Event()
    release = threading.Event()

    def hold_worker(_job_id):
        started.set()
        release.wait(5)

    monkeypatch.setattr(service, "_run_graph_index_job", hold_worker)
    try:
        service.start_graph_index_job(workspace="acme")
        assert started.wait(2)

        conn = service.store.conn
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE workspaces SET settings=? WHERE name=?",
            ('{"outer":"preserved"}', "acme"),
        )
        reused = service.start_graph_index_job(workspace="acme")

        assert reused["reused"] is True
        assert conn.transaction_owned_by_current_thread() is True
        assert conn.execute(
            "SELECT settings FROM workspaces WHERE name=?", ("acme",)
        ).fetchone()["settings"] == '{"outer":"preserved"}'
        conn.rollback()
    finally:
        release.set()
        service.close()


def test_document_import_launcher_marks_job_failed_when_worker_start_raises(monkeypatch):
    """If Thread.start() raises, the job must not be left running forever: the
    launcher marks it failed and removes the thread from the owned-workers dict
    (the same failure pattern the graph-index launcher uses)."""
    from engraphis.document_import import DocumentImporter

    s = _svc()
    s.create_workspace("acme")

    original_thread_start = threading.Thread.start

    def fail_start(self, *args, **kwargs):
        raise RuntimeError("thread pool exhausted")

    monkeypatch.setattr(threading.Thread, "start", fail_start)
    try:
        with pytest.raises(RuntimeError, match="thread pool exhausted"):
            s.import_document_upload(
                files=[("notes.md", b"# Title\nstart failure fact")],
                attachment_manifest=None,
                workspace="acme",
                source_label="start-failure-source",
                confirmed=True,
            )
    finally:
        monkeypatch.setattr(threading.Thread, "start", original_thread_start)

    assert s._obsidian_job_threads == {}
    row = s.store.conn.execute(
        "SELECT id, state FROM jobs WHERE kind=? ORDER BY created_at DESC LIMIT 1",
        (DocumentImporter.JOB_KIND,),
    ).fetchone()
    assert row is not None and row["state"] == "failed"
