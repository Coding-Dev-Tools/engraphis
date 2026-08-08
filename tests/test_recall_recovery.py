import json
import sqlite3

import numpy as np
import pytest

from engraphis.backends.reranker import IdentityReranker
from engraphis.backends.vector_numpy import NumpyVectorIndex
from engraphis.core.engine import MemoryEngine
from engraphis.core.interfaces import (
    Edge,
    MemoryRecord,
    Node,
    Scope,
    SearchFilter,
    embedding_space_fingerprint,
)
from engraphis.core.poisoning import prompt_eligible
from engraphis.core.store import Store
from engraphis.service import MemoryService


class _VersionedSemanticEmbedder:
    supports_semantic_search = True
    embedding_mode = "semantic"
    embedding_identity = "test_semantic"
    dim = 4

    def __init__(self, version: str, *, fail: bool = False):
        self.embedding_version = version
        self.fail = fail

    def embed(self, texts, *, kind="text"):
        if self.fail:
            raise RuntimeError("simulated rebuild interruption")
        axis = 0 if self.embedding_version == "A" else 1
        vectors = np.zeros((len(texts), self.dim), dtype=np.float32)
        vectors[:, axis] = 1.0
        return vectors


def _engine_for(db, embedder):
    store = Store(str(db))
    return MemoryEngine(
        store, embedder, NumpyVectorIndex(store, dim=embedder.dim),
        IdentityReranker(),
    )


def test_v10_upgrade_recovers_only_defensible_prompt_review_states(tmp_path):
    db = tmp_path / "v10-review.db"
    store = Store(str(db))
    workspace_id = store.get_or_create_workspace("acme")
    ids = [
        store.add_memory(MemoryRecord(
            id="", content=f"memory {index}", workspace_id=workspace_id,
            scope=Scope.WORKSPACE,
        ))
        for index in range(4)
    ]
    provenances = [
        {"source": "local_store", "trusted": True},
        {
            "source": "agent",
            "trusted": False,
            "review_state": "pending",
            "trust_origin": "service_review_gate",
            "trust_downgraded": True,
        },
        {
            "source": "web",
            "trusted": False,
            "review_state": "pending",
            "trust_origin": "external_ingress",
        },
    ]
    for memory_id, provenance in zip(ids, provenances):
        metadata = {"provenance": provenance}
        store.conn.execute(
            "UPDATE memories SET provenance=?, metadata=? WHERE id=?",
            (json.dumps(provenance), json.dumps(metadata), memory_id),
        )

    store.conn.execute(
        "UPDATE memories SET metadata=? WHERE id=?",
        (
            json.dumps({
                "provenance": {
                    "source": "import", "trusted": False, "review_state": "pending",
                }
            }),
            ids[3],
        ),
    )
    store.conn.execute("DELETE FROM schema_migrations")
    store.conn.execute(
        "INSERT INTO schema_migrations(version, applied_at) VALUES (10, 0)"
    )
    store.conn.commit()
    store.close()

    upgraded = Store(str(db))
    try:
        records = [upgraded.get_memory(memory_id) for memory_id in ids]
        assert records[0].provenance["review_state"] == "approved"
        assert records[0].provenance["review_basis"] == "legacy_explicit_trust"
        assert records[1].provenance["review_state"] == "approved"
        assert records[1].provenance["trusted"] is True
        assert records[1].provenance["review_basis"] == "legacy_local_agent_gate"
        assert records[2].provenance["review_state"] == "pending"
        assert records[2].provenance["trusted"] is False
        assert records[3].provenance["review_state"] == "pending"
        assert records[3].provenance["trusted"] is False
        assert upgraded.prompt_eligibility_counts(
            SearchFilter(workspace_id=workspace_id)
        )["prompt_eligible"] == 2
        audit_count = upgraded.conn.execute(
            "SELECT COUNT(*) AS n FROM audit "
            "WHERE action='prompt_review_backfill_summary'"
        ).fetchone()["n"]
        assert audit_count == 1
    finally:
        upgraded.close()

    reopened = Store(str(db))
    assert reopened.conn.execute(
        "SELECT COUNT(*) AS n FROM audit "
        "WHERE action='prompt_review_backfill_summary'"
    ).fetchone()["n"] == 1
    reopened.close()


def test_v10_upgrade_requires_review_for_only_llm_consolidation_and_retires_graph(tmp_path):
    db = tmp_path / "v10-llm-consolidation.db"
    store = Store(str(db))
    workspace_id = store.get_or_create_workspace("acme")
    repo_id = store.get_or_create_repo(workspace_id, "api")

    source_id = store.add_memory(MemoryRecord(
        id="", content="Authoritative source evidence.", workspace_id=workspace_id,
        repo_id=repo_id, scope=Scope.REPO,
        provenance={"source": "local_store", "trusted": True},
    ))
    peer_id = store.add_memory(MemoryRecord(
        id="", content="Unrelated graph peer.", workspace_id=workspace_id,
        repo_id=repo_id, scope=Scope.REPO,
        provenance={"source": "local_store", "trusted": True},
    ))
    cases = {
        "structured": (
            "A governed structured claim.",
            "structured_consolidation",
            {"entities": ["Acme API", "Lunar Relay"], "relations": [{
                "source": "Acme API", "relation": "stores_keys_on",
                "target": "Lunar Relay",
            }]},
        ),
        "llm_digest": (
            "A model-authored digest.\n\n"
            "(Consolidated from 3 episodes: deployment, policy)",
            "consolidation",
            {},
        ),
        "deterministic_digest": (
            "Recurring pattern (3 episodes): deployment, policy\nEvidence:\n"
            "- Authoritative source evidence.",
            "consolidation",
            {},
        ),
        "llm_profile": (
            "A model-authored profile.\n\n"
            "(Profile of Aurora (person), from 8 memories)",
            "profile_consolidation",
            {},
        ),
        "deterministic_profile": (
            "Profile — Aurora (person): 8 references.\n"
            "- Authoritative source evidence.",
            "profile_consolidation",
            {},
        ),
    }
    memory_ids: dict[str, str] = {}
    for name, (content, provenance_source, extra_metadata) in cases.items():
        provenance = {
            "source": provenance_source,
            "trusted": True,
            "consolidates": [source_id],
        }
        metadata = {**extra_metadata, "provenance": provenance}
        memory_id = store.add_memory(MemoryRecord(
            id="", content=content, workspace_id=workspace_id, repo_id=repo_id,
            scope=Scope.REPO, metadata=metadata, provenance=provenance,
        ))
        # Recreate the pre-review envelope exactly: the current Store would otherwise
        # add an approval stamp before the fixture is relabelled as schema 10.
        store.conn.execute(
            "UPDATE memories SET provenance=?, metadata=? WHERE id=?",
            (json.dumps(provenance), json.dumps(metadata), memory_id),
        )
        relation = "profiles" if "profile" in name else "consolidates"
        store.add_link(memory_id, source_id, relation)
        memory_ids[name] = memory_id

    structured_id = memory_ids["structured"]
    acme_id = store.upsert_entity(Node(
        id="", name="Acme API", ntype="service",
        workspace_id=workspace_id, repo_id=repo_id,
    ))
    relay_id = store.upsert_entity(Node(
        id="", name="Lunar Relay", ntype="service",
        workspace_id=workspace_id, repo_id=repo_id,
    ))
    edge_id = store.upsert_edge(Edge(
        id="", src=acme_id, dst=relay_id, relation="stores_keys_on",
        workspace_id=workspace_id, repo_id=repo_id,
        provenance={"source": "structured_extractor", "memory_id": structured_id},
    ))
    incidence_id = store.link_memory_entity(
        memory_id=structured_id, entity_id=relay_id,
        workspace_id=workspace_id, repo_id=repo_id,
        provenance={"source": "structured_extractor", "memory_id": structured_id},
    )
    symbol_id = store.upsert_symbol(
        repo_id=repo_id, kind="function", name="deploy", fqname="deploy",
        file="deploy.py", span="1-1",
    )
    code_link_id = store.link_memory_symbol(
        repo_id=repo_id, symbol_id=symbol_id, memory_id=structured_id,
    )
    store.add_link(structured_id, peer_id, "related")

    store.conn.execute("DELETE FROM schema_migrations")
    store.conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (10, 0)")
    store.conn.commit()
    store.close()

    upgraded = Store(str(db))
    llm_kinds = {
        "structured": "structured_fact",
        "llm_digest": "digest_summary",
        "llm_profile": "entity_profile",
    }
    try:
        for name, kind in llm_kinds.items():
            record = upgraded.get_memory(memory_ids[name])
            assert record.provenance["trusted"] is False
            assert record.provenance["review_state"] == "pending"
            assert record.provenance["review_basis"] == "legacy_llm_consolidation"
            assert record.provenance["derived_by_llm"] is True
            assert record.provenance["derived_graph_inert"] is True
            assert record.metadata["provenance"] == record.provenance
            assert record.metadata["llm_consolidation"] == {
                "review_required": True, "kind": kind,
            }
            assert not prompt_eligible(record.provenance, record.metadata)

        for name in ("deterministic_digest", "deterministic_profile"):
            record = upgraded.get_memory(memory_ids[name])
            assert record.provenance["trusted"] is True
            assert record.provenance["review_state"] == "approved"
            assert record.provenance["review_basis"] == "legacy_explicit_trust"
            assert "derived_by_llm" not in record.provenance
            assert prompt_eligible(record.provenance, record.metadata)

        structured = upgraded.get_memory(structured_id)
        assert "entities" not in structured.metadata
        assert "relations" not in structured.metadata
        assert structured.metadata["unverified_derived_graph"] == {
            "entities": ["Acme API", "Lunar Relay"],
            "relations": [{
                "source": "Acme API", "relation": "stores_keys_on",
                "target": "Lunar Relay",
            }],
            "source": "llm_consolidation",
        }

        assert upgraded.conn.execute(
            "SELECT valid_to FROM edges WHERE id=?", (edge_id,)
        ).fetchone()["valid_to"] is not None
        assert upgraded.conn.execute(
            "SELECT valid_to FROM memory_entities WHERE id=?", (incidence_id,)
        ).fetchone()["valid_to"] is not None
        assert upgraded.conn.execute(
            "SELECT valid_to FROM code_memory_links WHERE id=?", (code_link_id,)
        ).fetchone()["valid_to"] is not None
        live_links = {
            link["relation"] for link in upgraded.get_links(structured_id)
        }
        assert live_links == {"consolidates"}
        for name, memory_id in memory_ids.items():
            expected = "profiles" if "profile" in name else "consolidates"
            assert expected in {
                link["relation"] for link in upgraded.get_links(memory_id)
            }

        embedder = _VersionedSemanticEmbedder("review")
        engine = MemoryEngine(
            upgraded, embedder, NumpyVectorIndex(upgraded, dim=embedder.dim),
            IdentityReranker(),
        )
        engine._rebuild_versioned_embeddings()
        approval = engine.approve_for_prompt(
            structured_id, reviewer="owner", reason="verified against source evidence",
        )
        successor = upgraded.get_memory(approval["id"])
        assert successor.provenance["source"] == "human_review"
        assert prompt_eligible(successor.provenance, successor.metadata)
        assert "unverified_derived_graph" not in successor.metadata
        assert upgraded.get_memory(structured_id).provenance["review_state"] == "pending"
        summary_audits = upgraded.conn.execute(
            "SELECT COUNT(*) AS n FROM audit "
            "WHERE action='prompt_review_backfill_summary'"
        ).fetchone()["n"]
        assert summary_audits == 1
    finally:
        upgraded.close()

    reopened = Store(str(db))
    try:
        assert reopened.conn.execute(
            "SELECT COUNT(*) AS n FROM audit "
            "WHERE action='prompt_review_backfill_summary'"
        ).fetchone()["n"] == 1
        assert reopened.get_memory(structured_id).provenance["review_state"] == "pending"
        assert {link["relation"] for link in reopened.get_links(structured_id)} == {
            "consolidates"
        }
    finally:
        reopened.close()


def test_existing_v11_llm_repair_is_atomic_one_time_and_precedes_default_recall(
    tmp_path, monkeypatch,
):
    db = tmp_path / "existing-v11-llm-consolidation.db"
    marker_key = "__schema_v11_llm_consolidation_trust_repair"
    store = Store(str(db))
    workspace_id = store.get_or_create_workspace("acme")
    repo_id = store.get_or_create_repo(workspace_id, "api")
    source_id = store.add_memory(MemoryRecord(
        id="", content="Authoritative deployment evidence.",
        workspace_id=workspace_id, repo_id=repo_id, scope=Scope.REPO,
        provenance={"source": "local_store", "trusted": True},
    ))
    peer_id = store.add_memory(MemoryRecord(
        id="", content="Independent graph peer.", workspace_id=workspace_id,
        repo_id=repo_id, scope=Scope.REPO,
        provenance={"source": "local_store", "trusted": True},
    ))

    def add_legacy(content: str) -> str:
        provenance = {
            "source": "consolidation",
            "trusted": True,
            "review_state": "approved",
            "review_basis": "legacy_explicit_trust",
            "review_policy_version": 11,
            "consolidates": [source_id],
        }
        metadata = {"provenance": provenance}
        memory_id = store.add_memory(MemoryRecord(
            id="", content=content, workspace_id=workspace_id, repo_id=repo_id,
            scope=Scope.REPO, metadata=metadata, provenance=provenance,
        ))
        store.conn.execute(
            "UPDATE memories SET provenance=?, metadata=? WHERE id=?",
            (json.dumps(provenance), json.dumps(metadata), memory_id),
        )
        store.add_link(memory_id, source_id, "consolidates")
        return memory_id

    legacy_id = add_legacy(
        "A model-authored deployment digest.\n\n"
        "(Consolidated from 3 episodes: deployment, policy)"
    )
    deterministic_id = add_legacy(
        "Recurring pattern (3 episodes): deployment, policy\nEvidence:\n"
        "- Authoritative deployment evidence."
    )
    store.add_link(legacy_id, peer_id, "related")
    # Simulate a database opened by the earlier pre-release schema-11 build, which
    # necessarily predates this completion marker.
    store.conn.execute(
        "DELETE FROM sync_state WHERE key=?", (marker_key,),
    )
    store.conn.commit()
    store.close()

    original_retire = Store.retire_memory_graph_state

    def fail_after_retirement(self, *args, **kwargs):
        original_retire(self, *args, **kwargs)
        raise RuntimeError("simulated compatibility repair interruption")

    monkeypatch.setattr(Store, "retire_memory_graph_state", fail_after_retirement)
    with pytest.raises(RuntimeError, match="compatibility repair interruption"):
        Store(str(db))
    monkeypatch.setattr(Store, "retire_memory_graph_state", original_retire)

    raw = sqlite3.connect(str(db))
    raw.row_factory = sqlite3.Row
    try:
        assert raw.execute(
            "SELECT COUNT(*) AS n FROM sync_state WHERE key=?", (marker_key,),
        ).fetchone()["n"] == 0
        assert json.loads(raw.execute(
            "SELECT provenance FROM memories WHERE id=?", (legacy_id,),
        ).fetchone()["provenance"])["trusted"] is True
        assert raw.execute(
            "SELECT valid_to FROM mem_links "
            "WHERE ((a=? AND b=?) OR (a=? AND b=?)) AND relation='related'",
            (legacy_id, peer_id, peer_id, legacy_id),
        ).fetchone()["valid_to"] is None
    finally:
        raw.close()

    repaired = Store(str(db))
    try:
        legacy = repaired.get_memory(legacy_id)
        deterministic = repaired.get_memory(deterministic_id)
        assert legacy.provenance["trusted"] is False
        assert legacy.provenance["review_state"] == "pending"
        assert legacy.provenance["derived_graph_inert"] is True
        assert legacy.metadata["provenance"] == legacy.provenance
        assert deterministic.provenance["trusted"] is True
        assert deterministic.provenance["review_state"] == "approved"

        prompt_ids = {
            memory.id for memory in repaired.list_memories(
                SearchFilter(workspace_id=workspace_id, repo_id=repo_id),
                prompt_only=True,
            )
        }
        assert legacy_id not in prompt_ids
        assert deterministic_id in prompt_ids
        assert source_id in prompt_ids

        embedder = _VersionedSemanticEmbedder("same-schema-repair")
        engine = MemoryEngine(
            repaired, embedder, NumpyVectorIndex(repaired, dim=embedder.dim),
            IdentityReranker(),
        )
        engine._rebuild_versioned_embeddings()
        result = engine.recall(
            "model-authored deployment digest",
            workspace_id=workspace_id,
            repo_id=repo_id,
            k=10,
        )
        assert legacy_id not in {chunk["id"] for chunk in result.chunks}
        assert {link["relation"] for link in repaired.get_links(legacy_id)} == {
            "consolidates"
        }
        assert repaired.get_sync_state(marker_key) == "complete"
        assert repaired.conn.execute(
            "SELECT COUNT(*) AS n FROM audit "
            "WHERE action='llm_consolidation_trust_repair_complete'"
        ).fetchone()["n"] == 0
    finally:
        repaired.close()

    reopened = Store(str(db))
    try:
        assert reopened.get_sync_state(marker_key) == "complete"
        assert reopened.conn.execute(
            "SELECT COUNT(*) AS n FROM audit "
            "WHERE action='llm_consolidation_trust_repair' AND target=?",
            (legacy_id,),
        ).fetchone()["n"] == 1
        assert {link["relation"] for link in reopened.get_links(legacy_id)} == {
            "consolidates"
        }
    finally:
        reopened.close()


def test_existing_v12_llm_extraction_repair_demotes_and_retires_graph(tmp_path):
    db = tmp_path / "existing-v12-llm-extraction.db"
    marker_key = "__schema_v12_llm_extraction_trust_repair"
    store = Store(str(db))
    workspace_id = store.get_or_create_workspace("acme")
    peer_id = store.add_memory(MemoryRecord(
        id="", content="Independent trusted peer.", workspace_id=workspace_id,
        scope=Scope.WORKSPACE,
        provenance={"source": "local_store", "trusted": True,
                    "review_state": "approved"},
    ))
    provenance = {
        "source": "agent",
        "trusted": True,
        "review_state": "approved",
        "trust_origin": "local_mcp_agent",
    }
    metadata = {
        "provenance": provenance,
        "llm_extraction": {"mode": "llm_structured", "provider": "test"},
        "entities": ["Fabricated Service"],
        "relations": [{
            "source": "Fabricated Service",
            "relation": "controls",
            "target": "Production",
        }],
    }
    legacy_id = store.add_memory(MemoryRecord(
        id="", content="A model-authored unsupported claim.",
        workspace_id=workspace_id, scope=Scope.WORKSPACE,
        provenance=provenance, metadata=metadata,
    ))
    store.add_link(legacy_id, peer_id, "related")
    store.conn.execute("DELETE FROM sync_state WHERE key=?", (marker_key,))
    store.conn.commit()
    store.close()

    repaired = Store(str(db))
    try:
        record = repaired.get_memory(legacy_id)
        assert record.provenance["trusted"] is False
        assert record.provenance["review_state"] == "pending"
        assert record.provenance["trust_origin"] == "llm_extraction"
        assert record.provenance["derived_by_llm_extraction"] is True
        assert record.provenance["derived_graph_inert"] is True
        assert not prompt_eligible(record.provenance, record.metadata)
        assert "entities" not in record.metadata and "relations" not in record.metadata
        assert record.metadata["unverified_derived_graph"]["entities"] == [
            "Fabricated Service",
        ]
        assert record.metadata["llm_extraction"]["review_required"] is True
        assert repaired.get_links(legacy_id) == []
        assert repaired.get_sync_state(marker_key) == "complete"
        prompt_ids = {
            memory.id for memory in repaired.list_memories(
                SearchFilter(workspace_id=workspace_id), prompt_only=True,
            )
        }
        assert legacy_id not in prompt_ids
    finally:
        repaired.close()

    reopened = Store(str(db))
    try:
        assert reopened.conn.execute(
            "SELECT COUNT(*) AS n FROM audit "
            "WHERE action='llm_extraction_trust_repair' AND target=?",
            (legacy_id,),
        ).fetchone()["n"] == 1
    finally:
        reopened.close()


def test_active_embedding_fingerprint_catches_a_to_b_to_a_switch(tmp_path):
    db = tmp_path / "embedding-switch.db"
    embedder_a = _VersionedSemanticEmbedder("A")
    first = _engine_for(db, embedder_a)
    first._rebuild_versioned_embeddings()
    workspace_id = first.store.get_or_create_workspace("acme")
    memory_id = first.remember(
        "alpha release", workspace_id=workspace_id, scope=Scope.WORKSPACE
    )
    fingerprint_a = embedding_space_fingerprint(embedder_a)
    assert first.store.embedding_space_health(fingerprint_a)["stale_vectors"] == 0
    first.store.close()

    embedder_b = _VersionedSemanticEmbedder("B")
    second = _engine_for(db, embedder_b)
    second._rebuild_versioned_embeddings()
    vector_b = second.store.get_vectors([memory_id])[memory_id].copy()
    assert second.store.active_embedding_space() == embedding_space_fingerprint(embedder_b)
    second.store.close()

    third = _engine_for(db, _VersionedSemanticEmbedder("A"))
    third._rebuild_versioned_embeddings()
    try:
        vector_a = third.store.get_vectors([memory_id])[memory_id]
        assert not np.allclose(vector_a, vector_b)
        assert third.store.active_embedding_space() == fingerprint_a
        assert third.store.embedding_space_ready(fingerprint_a)
        assert third.store.embedding_space_health(fingerprint_a)["stale_vectors"] == 0
    finally:
        third.store.close()


def test_numpy_embedding_rebuild_writes_each_portable_vector_once(tmp_path, monkeypatch):
    db = tmp_path / "single-vector-write-rebuild.db"
    first = _engine_for(db, _VersionedSemanticEmbedder("A"))
    first._rebuild_versioned_embeddings()
    workspace_id = first.store.get_or_create_workspace("acme")
    memory_ids = [
        first.remember(
            content,
            workspace_id=workspace_id,
            scope=Scope.WORKSPACE,
            resolve_conflicts=False,
        )
        for content in ("alpha release", "bravo release")
    ]
    first.store.close()

    second = _engine_for(db, _VersionedSemanticEmbedder("B"))
    calls = []
    original = second.store.put_vector

    def traced_put_vector(memory_id, vector, *, model=""):
        calls.append(memory_id)
        return original(memory_id, vector, model=model)

    monkeypatch.setattr(second.store, "put_vector", traced_put_vector)
    second._rebuild_versioned_embeddings()
    try:
        assert sorted(calls) == sorted(memory_ids)
        assert set(second.store.get_vectors(memory_ids)) == set(memory_ids)
    finally:
        second.store.close()


def test_interrupted_embedding_rebuild_disables_only_vector_arm(tmp_path):
    db = tmp_path / "embedding-interrupt.db"
    first = _engine_for(db, _VersionedSemanticEmbedder("A"))
    first._rebuild_versioned_embeddings()
    workspace_id = first.store.get_or_create_workspace("acme")
    first.remember(
        "alpha release is ready", workspace_id=workspace_id, scope=Scope.WORKSPACE
    )
    first.store.close()

    interrupted = _engine_for(db, _VersionedSemanticEmbedder("B", fail=True))
    with pytest.raises(RuntimeError, match="simulated rebuild interruption"):
        interrupted._rebuild_versioned_embeddings()
    try:
        result = interrupted.recall_engine.recall(
            "alpha release", SearchFilter(workspace_id=workspace_id), k=3
        )
        assert result.count == 1
        assert result.vector_search_ready is False
        assert result.semantic_support is False
        assert result.degraded_mode is True
        assert interrupted.store.embedding_rebuild_target() == (
            embedding_space_fingerprint(interrupted.embedder)
        )
    finally:
        interrupted.store.close()


def test_competing_embedding_rebuild_cannot_publish_or_clear_newer_target(tmp_path):
    db = tmp_path / "embedding-race.db"
    first = _engine_for(db, _VersionedSemanticEmbedder("A"))
    first._rebuild_versioned_embeddings()
    workspace_id = first.store.get_or_create_workspace("acme")
    memory_id = first.remember(
        "alpha release is ready", workspace_id=workspace_id, scope=Scope.WORKSPACE
    )
    original = first.store.get_vectors([memory_id])[memory_id].copy()
    first.store.close()

    contender = _engine_for(db, _VersionedSemanticEmbedder("B"))
    competing_target = "emb:v1:" + "f" * 64
    ordinary_embed = contender.embedder.embed

    def lose_ownership(texts, *, kind="text"):
        contender.store.begin_embedding_rebuild(competing_target)
        return ordinary_embed(texts, kind=kind)

    contender.embedder.embed = lose_ownership
    with pytest.raises(RuntimeError, match="superseded"):
        contender._rebuild_versioned_embeddings()
    try:
        assert contender.store.embedding_rebuild_target() == competing_target
        assert np.allclose(contender.store.get_vectors([memory_id])[memory_id], original)
    finally:
        contender.store.close()


def test_zero_recall_reports_review_gate_and_embedding_health():
    service = MemoryService.create(":memory:", extractor="none", graph_extractor="none")
    pending = service.remember(
        "The release codename is cobalt.", workspace="acme", source="web"
    )
    result = service.recall("release codename", workspace="acme")
    stats = service.stats(workspace="acme")

    assert result["count"] == 0
    assert result["eligibility"]["total"] == 1
    assert result["eligibility"]["prompt_eligible"] == 0
    assert "review" in result["note"]
    assert stats["prompt_eligibility"]["pending"] == 1
    assert "ready" in stats["embedding"]
    assert service.store.get_memory(pending["id"]).provenance["review_state"] == "pending"
