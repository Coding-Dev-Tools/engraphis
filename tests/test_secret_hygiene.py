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


def test_secure_erase_removes_local_memory_indexes_and_links(tmp_path):
    db_path = tmp_path / "engraphis.db"
    service = MemoryService.create(str(db_path))
    leaked = service.remember("Legacy row placeholder.", workspace="acme")
    other = service.remember("Independent safe memory.", workspace="acme")
    mid = leaked["id"]

    # Simulate a row captured before the boundary existed, including an FTS mirror.
    service.store.conn.execute("UPDATE memories SET content=? WHERE id=?", (_LEAK, mid))
    service.store._fts_upsert(mid, "", _LEAK, "")
    service.store.add_link(mid, other["id"], "related")
    service.store.audit("tester", "legacy_note", mid, "legacy audit detail " + _LEAK)
    service.store.conn.commit()

    erased = service.secure_erase(mid, workspace="acme")
    assert erased["status"] == "securely_erased"
    assert erased["vector_index_cleanup"] == "deleted"
    assert service.store.get_memory(mid) is None
    assert service.store.conn.execute("SELECT COUNT(*) FROM mem_fts WHERE id=?", (mid,)).fetchone()[0] == 0
    assert service.store.conn.execute("SELECT COUNT(*) FROM mem_vectors WHERE id=?", (mid,)).fetchone()[0] == 0
    assert service.store.conn.execute(
        "SELECT COUNT(*) FROM mem_links WHERE a=? OR b=?", (mid, mid)
    ).fetchone()[0] == 0
    audit_rows = service.store.conn.execute(
        "SELECT action, detail FROM audit WHERE target=?", (mid,)
    ).fetchall()
    assert [(row["action"], row["detail"]) for row in audit_rows] == [
        ("secure_erase", "per-memory secure erasure completed; content intentionally omitted")
    ]
    assert _LEAK.encode("utf-8") not in db_path.read_bytes()
    wal_path = db_path.with_name(db_path.name + "-wal")
    if wal_path.exists():
        assert _LEAK.encode("utf-8") not in wal_path.read_bytes()
    # The physical result is explicit; a busy WAL/VACUUM must never be reported as success.
    assert erased["maintenance"]["wal"] in {"truncated", "busy", "failed"}
    assert erased["maintenance"]["vacuum"] in {"completed", "failed"}


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
