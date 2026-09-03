"""Regression tests for capture-time secret blocking and breach remediation."""
from __future__ import annotations

import json

import pytest

from engraphis.core.engine import MemoryEngine
from engraphis.core.interfaces import Edge, ExtractedFact, MemoryRecord, MemoryType, Scope
from engraphis.core.secrets import SecretDetectedError, secret_kind
from engraphis.core.store import Store
from engraphis.service import MemoryService, ValidationError


_LEAK = "sk-proj-0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def test_secret_key_matching_does_not_block_nonsecret_token_metadata():
    assert secret_kind({"chunking": {"token_counter": "regex_v1"}}) is None


def test_service_and_store_block_credentials_before_any_memory_index_write():
    service = MemoryService.create(":memory:")
    with pytest.raises(ValidationError, match="potential OpenAI API key"):
        service.remember(f"Provider key is {_LEAK}", workspace="acme")

    # The rejection occurs before a workspace, memory, FTS row, or vector exists.
    assert service.store.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    assert service.store.conn.execute("SELECT COUNT(*) FROM mem_fts").fetchone()[0] == 0
    assert service.store.conn.execute("SELECT COUNT(*) FROM mem_vectors").fetchone()[0] == 0

    with pytest.raises(ValidationError, match="credential assignment"):
        service.remember("Metadata boundary test.", workspace="acme",
                         metadata={"api_key": "metadata-secret"})
    assert service.store.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0

    with pytest.raises(ValidationError, match="bearer token"):
        service.remember("Authorization: Bearer abcdefghijklmnopqrstuvwxyz", workspace="acme")
    with pytest.raises(ValidationError, match="credential assignment"):
        service.remember("Token boundary test.", workspace="acme",
                         metadata={"token": "0123456789abcdef"})
    assert service.store.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0

    for field, value, expected in (
        ("AWS_SECRET_ACCESS_KEY", "0123456789abcdef", "credential assignment"),
        ("TOKEN", "0123456789abcdef", "credential assignment"),
    ):
        with pytest.raises(ValidationError, match=expected):
            service.remember("Environment boundary test.", workspace="acme",
                             metadata={field: value})
    with pytest.raises(ValidationError, match="credential-bearing connection URI"):
        service.remember("postgresql://agent:0123456789abcdef@db.example/app",
                         workspace="acme")
    assert service.store.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0

    store = Store(":memory:")
    with pytest.raises(SecretDetectedError, match="credential assignment"):
        store.add_memory(MemoryRecord(
            id="", workspace_id="ws_direct", content="api_key=0123456789abcdef",
            mtype=MemoryType.SEMANTIC, scope=Scope.WORKSPACE,
        ))
    assert store.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0


def test_direct_engine_ingest_rejects_before_optional_extractor_runs():
    class RecordingExtractor:
        called = False

        def extract(self, text: str):
            self.called = True
            return [ExtractedFact(content="derived safe-looking fact")]

    engine = MemoryEngine.create(":memory:")
    extractor = RecordingExtractor()
    engine.extractor = extractor
    workspace = engine.store.get_or_create_workspace("acme")

    with pytest.raises(SecretDetectedError, match="OpenAI API key"):
        engine.ingest(f"raw transcript contains {_LEAK}", workspace_id=workspace)

    assert extractor.called is False
    assert engine.store.count_memories() == 0
    assert engine.store.conn.execute("SELECT COUNT(*) FROM mem_fts").fetchone()[0] == 0


def test_retire_is_canonical_and_forget_remains_a_compatibility_alias():
    service = MemoryService.create(":memory:")
    first = service.remember("A safely stored, stale fact.", workspace="acme")
    retired = service.retire(first["id"], workspace="acme", reason="obsolete")
    assert retired["status"] == "retired"
    assert service.store.get_memory(first["id"]) is not None
    assert service.recall("stale fact", workspace="acme")["count"] == 0

    second = service.remember("Another safely stored fact.", workspace="acme")
    legacy = service.forget(second["id"], workspace="acme")
    assert legacy["status"] == "forgotten"
    assert legacy["deprecated"] is True
    assert service.store.get_memory(second["id"]) is not None


def test_secure_erase_removes_local_memory_indexes_and_links(tmp_path, monkeypatch):
    db_path = tmp_path / "engraphis.db"
    service = MemoryService.create(str(db_path))
    leaked = service.remember("Legacy row placeholder.", workspace="acme")
    other = service.remember("Independent safe memory.", workspace="acme")
    mid = leaked["id"]
    workspace_id = service.store.get_or_create_workspace("acme")
    vault_id = service.store.register_source_vault(
        kind="documents", root_digest="a" * 64, workspace_id=workspace_id,
    )
    source_id = service.store.upsert_source_import_item(
        vault_id=vault_id, source_key="b" * 64, relative_path="secret.txt",
        memory_id=mid,
    )
    service.store.mark_memories_sync_exported([mid], workspace_id=workspace_id)

    # Simulate a row captured before the boundary existed, including an FTS mirror.
    service.store.conn.execute("UPDATE memories SET content=? WHERE id=?", (_LEAK, mid))
    service.store._fts_upsert(mid, "", _LEAK, "")
    service.store.add_link(mid, other["id"], "related")
    service.store.audit("tester", "legacy_note", mid, "legacy audit detail " + _LEAK)
    service.store.conn.commit()

    maintenance_transactions = []
    original_maintenance = Store._checkpoint_and_vacuum

    def observe_maintenance(conn, *, durable):
        maintenance_transactions.append(conn.in_transaction)
        return original_maintenance(conn, durable=durable)

    monkeypatch.setattr(Store, "_checkpoint_and_vacuum", staticmethod(observe_maintenance))
    erased = service.secure_erase(mid, workspace="acme", confirmed=True)
    assert erased["status"] == "securely_erased"
    assert erased["vector_index_cleanup"] == "deleted"
    assert service.store.get_memory(mid) is None
    assert service.store.conn.execute("SELECT COUNT(*) FROM mem_fts WHERE id=?", (mid,)).fetchone()[0] == 0
    assert service.store.conn.execute("SELECT COUNT(*) FROM mem_vectors WHERE id=?", (mid,)).fetchone()[0] == 0
    assert service.store.conn.execute(
        "SELECT COUNT(*) FROM mem_links WHERE a=? OR b=?", (mid, mid)
    ).fetchone()[0] == 0
    assert service.store.conn.execute(
        "SELECT COUNT(*) FROM source_imports WHERE id=?", (source_id,)
    ).fetchone()[0] == 0
    # Content-free export proof deliberately survives so the tombstone remains syncable.
    assert service.store.get_memory_sync_export(mid) is not None
    audit_rows = service.store.conn.execute(
        "SELECT action, detail FROM audit WHERE target=?", (mid,)
    ).fetchall()
    assert [(row["action"], row["detail"]) for row in audit_rows] == [
        ("secure_erase", "per-memory secure erasure completed; content intentionally omitted"),
        ("secure_erase", "explicit local-operator confirmation; rotate the credential and "
         "remediate external copies separately")
    ]
    assert _LEAK.encode("utf-8") not in db_path.read_bytes()
    wal_path = db_path.with_name(db_path.name + "-wal")
    if wal_path.exists():
        assert _LEAK.encode("utf-8") not in wal_path.read_bytes()
    # The physical result is explicit; a busy WAL/VACUUM must never be reported as success.
    assert erased["maintenance"]["wal"] in {"truncated", "busy", "failed"}
    assert erased["maintenance"]["vacuum"] == "completed"
    assert maintenance_transactions
    assert maintenance_transactions[-1] is False


def test_writable_store_enables_sqlite_secure_delete_before_an_emergency_erase(tmp_path):
    """Deleted rows are scrubbed even if a later erase cannot VACUUM immediately."""
    store = Store(str(tmp_path / "secure-delete.db"))
    assert store.conn.execute("PRAGMA secure_delete").fetchone()[0] == 1


def test_secure_erase_rebuilds_shared_edge_provenance_from_remaining_support():
    engine = MemoryEngine.create(":memory:")
    workspace = engine.store.get_or_create_workspace("acme")
    erased_id = engine.remember("Erased graph source.", workspace_id=workspace)
    retained_id = engine.remember("Retained graph source.", workspace_id=workspace)
    edge_id = engine.store.upsert_edge(Edge(
        id="edg_shared", src="ent_alpha", dst="ent_beta", relation="uses",
        workspace_id=workspace,
        provenance={"source": "structured", "memory_id": erased_id},
    ))
    engine.store.add_edge_support(
        edge_id, {"source": "manual", "memory_id": retained_id}
    )

    engine.secure_erase(erased_id)

    edge = engine.store.conn.execute(
        "SELECT provenance FROM edges WHERE id=?", (edge_id,)
    ).fetchone()
    assert edge is not None
    provenance = json.loads(edge["provenance"])
    assert provenance["memory_id"] == retained_id
    assert provenance["memory_ids"] == [retained_id]
    assert erased_id not in edge["provenance"]
    supports = engine.store.conn.execute(
        "SELECT memory_id, provenance FROM edge_supports WHERE edge_id=?",
        (edge_id,),
    ).fetchall()
    assert [row["memory_id"] for row in supports] == [retained_id]
    assert erased_id not in supports[0]["provenance"]

    neighbors = engine.store.neighbors(["ent_alpha"])
    assert [edge.id for edge in engine.recall_engine._prompt_eligible_edges(neighbors)] == [
        edge_id
    ]


def test_secure_erase_removes_sync_conflict_successors():
    engine = MemoryEngine.create(":memory:")
    workspace = engine.store.get_or_create_workspace("acme")
    original_id = engine.remember("Original secret source.", workspace_id=workspace)
    from engraphis.core import ids
    successor_id = ids.new_id("memory")
    engine.store.add_memory(MemoryRecord(
        id=successor_id,
        content="Losing secret copy.",
        workspace_id=workspace,
        scope=Scope.WORKSPACE,
        metadata={
            "sync_conflict": {"memory_id": original_id},
        },
        provenance={"conflict_of": original_id, "trusted": False},
    ))

    engine.secure_erase(original_id)

    assert engine.store.get_memory(original_id) is None
    assert engine.store.get_memory(successor_id) is None
    assert {
        row["id"]
        for row in engine.store.list_memory_tombstones()
    } >= {original_id, successor_id}


def test_secure_erase_does_not_follow_foreign_conflict_lineage():
    engine = MemoryEngine.create(":memory:")
    workspace_a = engine.store.get_or_create_workspace("workspace-a")
    workspace_b = engine.store.get_or_create_workspace("workspace-b")
    repo_a = engine.store.get_or_create_repo(workspace_a, "repo-a")
    repo_b = engine.store.get_or_create_repo(workspace_b, "repo-b")
    original_id = engine.remember(
        "Workspace B repository secret.",
        workspace_id=workspace_b,
        repo_id=repo_b,
        scope=Scope.REPO,
    )

    def add_successor(workspace_id, repo_id, label):
        return engine.store.add_memory(MemoryRecord(
            id="",
            content=f"{label} secret copy.",
            workspace_id=workspace_id,
            repo_id=repo_id,
            scope=Scope.REPO,
            metadata={"sync_conflict": {"memory_id": original_id}},
            provenance={
                "source": "sync_conflict",
                "trusted": False,
                "conflict_of": original_id,
            },
        ))

    legitimate_id = add_successor(workspace_b, repo_b, "legitimate")
    foreign_workspace_id = add_successor(workspace_a, repo_a, "foreign workspace")
    foreign_repo_id = add_successor(workspace_b, repo_a, "foreign repository")

    assert set(engine.store.secure_erase_target_ids(original_id)) == {
        original_id, legitimate_id,
    }
    engine.secure_erase(original_id)

    assert engine.store.get_memory(original_id) is None
    assert engine.store.get_memory(legitimate_id) is None
    assert engine.store.get_memory(foreign_workspace_id) is not None
    assert engine.store.get_memory(foreign_repo_id) is not None


def test_secure_erase_deletes_transitive_successors_from_external_vector_index():
    engine = MemoryEngine.create(":memory:")
    workspace = engine.store.get_or_create_workspace("acme")
    original_id = engine.remember("Original secret source.", workspace_id=workspace)
    from engraphis.core import ids
    successor_id = ids.new_id("memory")
    grandchild_id = ids.new_id("memory")
    for memory_id, parent_id in ((successor_id, original_id), (grandchild_id, successor_id)):
        engine.store.add_memory(MemoryRecord(
            id=memory_id,
            content="Losing secret copy.",
            workspace_id=workspace,
            scope=Scope.WORKSPACE,
            metadata={"sync_conflict": {"memory_id": parent_id}},
            provenance={"conflict_of": parent_id, "trusted": False},
        ))

    class RecordingExternalIndex:
        def __init__(self):
            self.deleted = []

        def delete(self, memory_ids, *, commit=True):
            self.deleted.append((tuple(memory_ids), commit))

    index = RecordingExternalIndex()
    engine.index = index

    result = engine.secure_erase(original_id)

    assert result["vector_index_cleanup"] == "deleted"
    assert len(index.deleted) == 1
    assert set(index.deleted[0][0]) == {original_id, successor_id, grandchild_id}
    assert index.deleted[0][1] is True
    assert all(engine.store.get_memory(memory_id) is None for memory_id in index.deleted[0][0])


def test_secure_erase_reports_external_failure_after_erasing_all_successors():
    engine = MemoryEngine.create(":memory:")
    workspace = engine.store.get_or_create_workspace("acme")
    original_id = engine.remember("Original secret source.", workspace_id=workspace)
    from engraphis.core import ids
    successor_id = ids.new_id("memory")
    engine.store.add_memory(MemoryRecord(
        id=successor_id,
        content="Losing secret copy.",
        workspace_id=workspace,
        scope=Scope.WORKSPACE,
        metadata={"sync_conflict": {"memory_id": original_id}},
        provenance={"conflict_of": original_id, "trusted": False},
    ))

    class FailingExternalIndex:
        def __init__(self):
            self.deleted = []

        def delete(self, memory_ids, *, commit=True):
            self.deleted.append((tuple(memory_ids), commit))
            raise RuntimeError("external index unavailable")

    index = FailingExternalIndex()
    engine.index = index

    result = engine.secure_erase(original_id)

    assert result["vector_index_cleanup"] == "failed"
    assert "external_index_limitation" in result
    assert index.deleted == [((original_id, successor_id), True)]
    assert engine.store.get_memory(original_id) is None
    assert engine.store.get_memory(successor_id) is None


def test_secure_erase_reports_partial_external_cleanup_as_limited():
    engine = MemoryEngine.create(":memory:")
    workspace = engine.store.get_or_create_workspace("acme")
    original_id = engine.remember("Original secret source.", workspace_id=workspace)
    from engraphis.core import ids
    successor_id = ids.new_id("memory")

    class PartialExternalIndex:
        def __init__(self):
            self.deleted = []
            self.injected = False

        def delete(self, memory_ids, *, commit=True):
            self.deleted.append((tuple(memory_ids), commit))
            if not self.injected:
                self.injected = True
                engine.store.add_memory(MemoryRecord(
                    id=successor_id,
                    content="Losing secret copy.",
                    workspace_id=workspace,
                    scope=Scope.WORKSPACE,
                    metadata={"sync_conflict": {"memory_id": original_id}},
                    provenance={"conflict_of": original_id, "trusted": False},
                ))
                return
            raise RuntimeError("successor vector unavailable")

    index = PartialExternalIndex()
    engine.index = index

    result = engine.secure_erase(original_id)

    assert result["vector_index_cleanup"] == "partial"
    assert "external_index_limitation" in result
    assert len(index.deleted) == 2
    assert engine.store.get_memory(original_id) is None
    assert engine.store.get_memory(successor_id) is None


def test_secure_erase_reports_limitation_when_successor_external_delete_fails():
    """A successor appearing after the first external scan whose vector delete fails
    must still surface the external_index_limitation warning (cleanup="partial"),
    while the authoritative local erase completes."""
    engine = MemoryEngine.create(":memory:")
    workspace = engine.store.get_or_create_workspace("acme")
    original_id = engine.remember("Original secret source.", workspace_id=workspace)

    # The index succeeds on the first delete (original targets) but fails on the
    # second delete (successors discovered during the final scan under the lock).
    class PartialExternalIndex:
        def __init__(self):
            self.deleted = []
            self.call_count = 0

        def delete(self, memory_ids, *, commit=True):
            self.call_count += 1
            self.deleted.append((tuple(memory_ids), commit))
            if self.call_count >= 2:
                raise RuntimeError("external index unavailable for successors")

    index = PartialExternalIndex()
    engine.index = index

    # Inject a successor *after* the initial target scan but before the final scan.
    # We do this by monkey-patching secure_erase_target_ids to return the successor
    # on its second call (the refreshed scan inside the transaction).
    from engraphis.core import ids
    successor_id = ids.new_id("memory")
    original_target_ids = engine.store.secure_erase_target_ids
    call_counter = {"n": 0}

    def patched_target_ids(mid):
        call_counter["n"] += 1
        if call_counter["n"] == 1:
            return original_target_ids(mid)
        # Second call: simulate a successor having been written between scans.
        engine.store.add_memory(MemoryRecord(
            id=successor_id,
            content="Successor secret copy.",
            workspace_id=workspace,
            scope=Scope.WORKSPACE,
            metadata={"sync_conflict": {"memory_id": mid}},
            provenance={"conflict_of": mid, "trusted": False},
        ))
        return original_target_ids(mid)

    engine.store.secure_erase_target_ids = patched_target_ids
    try:
        result = engine.secure_erase(original_id)
    finally:
        engine.store.secure_erase_target_ids = original_target_ids

    assert result["vector_index_cleanup"] == "partial"
    assert "external_index_limitation" in result
    assert engine.store.get_memory(original_id) is None
    assert engine.store.get_memory(successor_id) is None
def test_secure_erase_preserves_shared_edge_history_from_retired_support():
    engine = MemoryEngine.create(":memory:")
    workspace = engine.store.get_or_create_workspace("acme")
    erased_id = engine.remember("Erased current source.", workspace_id=workspace)
    historical_id = engine.remember("Historical safe source.", workspace_id=workspace)
    edge_id = engine.store.upsert_edge(Edge(
        id="edg_historical", src="ent_alpha", dst="ent_beta", relation="uses",
        workspace_id=workspace,
        provenance={"source": "structured", "memory_id": erased_id},
    ))
    engine.store.add_edge_support(
        edge_id, {"source": "manual", "memory_id": historical_id}
    )
    historical_at = engine.store.conn.execute(
        "SELECT MAX(valid_from) FROM edge_supports WHERE edge_id=?", (edge_id,)
    ).fetchone()[0]
    # Ensure temporal separation so the historical_at anchor is strictly before
    # the valid_to stamped by retire() → invalidate_edges_for_memory().
    # Without this, both can land on the same microsecond and the strict <
    # predicate in _temporal_visibility_sql excludes the support.
    import time
    time.sleep(0.05)  # 50ms for CI/load robustness (was 10ms)
    engine.retire(historical_id, reason="historical evidence")

    engine.secure_erase(erased_id)

    edge = engine.store.conn.execute(
        "SELECT valid_to, valid_to_recorded_at, provenance FROM edges WHERE id=?",
        (edge_id,),
    ).fetchone()
    assert edge is not None
    assert edge["valid_to"] is not None
    assert edge["valid_to_recorded_at"] is not None
    provenance = json.loads(edge["provenance"])
    assert provenance["memory_id"] == historical_id
    assert provenance["memory_ids"] == [historical_id]
    assert erased_id not in edge["provenance"]
    assert engine.store.neighbors(["ent_alpha"]) == []
    historical = engine.store.neighbors(["ent_alpha"], at=historical_at)
    assert [item.id for item in historical] == [edge_id]
    assert [
        item.id for item in engine.recall_engine._prompt_eligible_edges(historical)
    ] == [edge_id]
    supports = engine.store.conn.execute(
        "SELECT memory_id, valid_to, provenance FROM edge_supports WHERE edge_id=?",
        (edge_id,),
    ).fetchall()
    assert [row["memory_id"] for row in supports] == [historical_id]
    assert supports[0]["valid_to"] is not None
    assert erased_id not in supports[0]["provenance"]


def test_sync_drops_secret_bearing_rows_before_store_upsert():
    store = Store(":memory:")
    workspace = store.get_or_create_workspace("acme")
    # Exercise the public sync parser directly: it must reject rather than rely only
    # on Store.add_memory, because sync normally batches raw writes.
    from engraphis.core.sync import SyncEngine

    bundle = {
        "format": "engraphis-sync", "version": 2, "device_id": "peer",
        "workspace_name": "acme", "repos": {},
        "memories": [{
            "id": "mem_peer_secret", "content": _LEAK, "scope": "workspace",
            "mtype": "semantic", "metadata": json.loads("{}"),
        }], "mem_links": [],
    }
    report = SyncEngine(store).apply_bundle(bundle, into_workspace="acme")
    assert workspace
    assert report["rejected"] == 1
    assert store.count_memories() == 0


def test_service_and_engine_redact_secrets_when_opted_in():
    service = MemoryService.create(":memory:")
    # By default, storing an API key raises ValidationError
    with pytest.raises(ValidationError, match="potential OpenAI API key"):
        service.remember(f"Debugging log with key {_LEAK}", workspace="acme")

    # With redact_secrets=True, it succeeds and masks the credential safely
    result = service.remember(
        f"Debugging log with key {_LEAK}", workspace="acme", redact_secrets=True
    )
    assert result["stored"] is True
    mem = service.store.get_memory(result["id"])
    assert mem is not None
    assert _LEAK not in mem.content
    assert "<redacted>" in mem.content

