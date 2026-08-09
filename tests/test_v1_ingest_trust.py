"""v1 ingest trust model: local documents are recallable, external imports are not.

The legacy ``engines.ingest`` path predates the v2 quarantine/provenance boundary.
Local user documents (memory files, manual entries) are trusted and recall-visible;
external file imports (folder/upload) are untrusted and filtered from agent-facing
recall — mirroring the v2 prompt gate without breaking the primary v1 UX.
"""
from __future__ import annotations

import json
import math
import threading

import numpy as np
import pytest

pytest.importorskip(
    "pydantic", minversion="2.0", reason="legacy v1 stack extra not installed"
)
pytest.importorskip("httpx", reason="legacy LLM client extra not installed")

from engraphis.engines import ingest as ingest_engine
from engraphis.engines import recall as recall_engine
from engraphis.engines import reweight, thoughts as thoughts_engine
from engraphis.stores import get_conn, init_db
from engraphis.stores import graph as graph_store
from engraphis.stores import ledger as ledger_store
from engraphis.stores import vaults as vault_store
from engraphis.stores import vectors as mem_store


@pytest.fixture()
def v1_store(tmp_path, monkeypatch):
    from engraphis.config import settings
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "v1.db"))
    monkeypatch.setattr(settings, "embed_model", "")   # never download a model
    monkeypatch.setattr("engraphis.stores._local", threading.local())
    init_db()
    yield


def _ingest(ns, doc_id, title, content, trusted=True):
    return ingest_engine.ingest_document(
        namespace=ns, document_id=doc_id, title=title, content=content,
        vector=np.ones(8, dtype=np.float32), trusted=trusted,
    )


def test_local_document_is_trusted_and_recallable(v1_store):
    _ingest("ns", "doc-1", "Deployment", "The release manager approves all deployments.")
    # recall_master ranks by retention without embedding the prompt (avoids any model).
    out = recall_engine.recall_master(namespace="ns", max_chunks=10)
    assert out["count"] >= 1
    assert any("release manager" in c["content"] for c in out["chunks"])


def test_external_import_is_untrusted_and_filtered_from_recall(v1_store):
    _ingest("ns", "doc-1", "Deployment", "The release manager approves all deployments.",
            trusted=False)
    out = recall_engine.recall_master(namespace="ns", max_chunks=10)
    assert out["count"] == 0


def test_ingest_stamps_provenance_by_trust(v1_store):
    _ingest("ns", "local-1", "T", "local note")
    _ingest("ns", "ext-1", "T", "external note", trusted=False)
    rows = {r["document_id"]: r for r in get_conn().execute(
        "SELECT document_id, metadata FROM memories WHERE namespace='ns'").fetchall()}
    local_prov = json.loads(rows["local-1"]["metadata"])["provenance"]
    ext_prov = json.loads(rows["ext-1"]["metadata"])["provenance"]
    assert local_prov["trusted"] is True
    assert local_prov["review_state"] == "approved"
    assert ext_prov["trusted"] is False
    assert ext_prov["review_state"] == "pending"



def test_external_trust_metadata_cannot_self_approve(v1_store):
    ingest_engine.ingest_document(
        namespace="ns", document_id="forged", title="T", content="external note",
        metadata={"provenance": {"trusted": True, "review_state": "approved"}},
        vector=np.ones(8, dtype=np.float32), trusted=False,
    )
    row = get_conn().execute(
        "SELECT metadata FROM memories WHERE document_id='forged'"
    ).fetchone()
    provenance = json.loads(row["metadata"])["provenance"]
    assert provenance["trusted"] is False
    assert provenance["review_state"] == "pending"


def test_batch_preserves_each_item_trust_decision(v1_store):
    result = ingest_engine.ingest_batch([
        {
            "namespace": "ns",
            "document_id": "external",
            "title": "T",
            "content": "external note",
            "vector": np.ones(8, dtype=np.float32),
            "trusted": False,
        },
        {
            "namespace": "ns",
            "document_id": "local",
            "title": "T",
            "content": "local note",
            "vector": np.ones(8, dtype=np.float32),
            "trusted": True,
        },
    ])
    assert result["count"] == 2
    out = recall_engine.recall_master(namespace="ns", max_chunks=10)
    assert out["count"] == 1
    assert out["chunks"][0]["documentId"] == "local"


def test_trust_flag_is_strictly_boolean(v1_store):
    with pytest.raises(ValueError, match="trusted must be a boolean"):
        _ingest("ns", "forged", "T", "external note", trusted="false")
    assert get_conn().execute(
        "SELECT COUNT(*) FROM memories WHERE document_id='forged'"
    ).fetchone()[0] == 0


def test_secret_rejection_is_content_free(v1_store):
    secret = "Bearer " + "s" * 20
    with pytest.raises(ValueError) as caught:
        _ingest("ns", "secret", "T", secret)
    assert secret not in str(caught.value)
    assert get_conn().execute(
        "SELECT COUNT(*) FROM memories WHERE document_id='secret'"
    ).fetchone()[0] == 0


def test_ambiguous_legacy_trust_marker_fails_closed(v1_store):
    _ingest("ns", "legacy", "T", "legacy note")
    get_conn().execute(
        "UPDATE memories SET metadata=? WHERE document_id='legacy'",
        (json.dumps({"trusted": False}),),
    )
    get_conn().commit()
    assert recall_engine.recall_master(namespace="ns", max_chunks=10)["count"] == 0


def test_prompt_recall_filters_untrusted_and_does_not_reinforce_by_default(
    v1_store, monkeypatch
):
    monkeypatch.setattr(
        recall_engine.embedder, "embed", lambda _prompt: np.ones(8, dtype=np.float32)
    )
    _ingest("ns", "local", "T", "local note")
    _ingest("ns", "external", "T", "external note", trusted=False)
    out = recall_engine.recall(namespace="ns", prompt="local", num_chunks=10)
    assert out["count"] == 1
    assert out["chunks"][0]["documentId"] == "local"


def test_recall_limits_reject_negative_values(v1_store):
    with pytest.raises(ValueError, match="non-negative integer"):
        recall_engine.recall_master(namespace="ns", max_chunks=-1)
    with pytest.raises(ValueError, match="non-negative integer"):
        recall_engine.recall_by_retention(namespace="ns", top_k=-1)


def test_document_graph_evidence_tracks_edit_move_and_delete(v1_store, monkeypatch):
    monkeypatch.setattr(
        ingest_engine.embedder, "embed", lambda _text: np.ones(8, dtype=np.float32)
    )
    _ingest("source", "doc-1", "", "Alice works at Acme.")
    first = graph_store.graph_snapshot("source")
    assert {node["name"] for node in first["entities"]} == {"Alice", "Acme"}
    assert first["edges"][0]["weight"] == 1.0
    assert all(node["documents"] == ["doc-1"] for node in first["entities"])

    ingest_engine.update_document(
        namespace="source",
        document_id="doc-1",
        content="Carol works at Globex.",
    )
    edited = graph_store.graph_snapshot("source")
    assert {node["name"] for node in edited["entities"]} == {"Carol", "Globex"}

    assert mem_store.move_memory("doc-1", "source", "target") is True
    assert graph_store.graph_snapshot("source")["entity_count"] == 0
    moved = graph_store.graph_snapshot("target")
    assert {node["name"] for node in moved["entities"]} == {"Carol", "Globex"}
    assert all(node["documents"] == ["doc-1"] for node in moved["entities"])

    assert mem_store.delete_memory_document("doc-1", "target") == 1
    deleted = graph_store.graph_snapshot("target")
    assert deleted["entity_count"] == 0
    assert deleted["edge_count"] == 0


def test_shared_graph_support_survives_until_last_document_is_deleted(v1_store):
    _ingest("ns", "doc-1", "", "Alice works at Acme.")
    _ingest("ns", "doc-2", "", "Alice works at Acme.")
    assert graph_store.get_edges("ns")[0]["weight"] == 2.0

    mem_store.delete_memory_document("doc-1", "ns")
    remaining = graph_store.graph_snapshot("ns")
    assert remaining["edge_count"] == 1
    assert remaining["edges"][0]["weight"] == 1.0
    assert all(node["documents"] == ["doc-2"] for node in remaining["entities"])

    mem_store.delete_memory_document("doc-2", "ns")
    assert graph_store.graph_snapshot("ns")["entity_count"] == 0


def test_ingest_rolls_back_memory_graph_event_and_job_together(v1_store, monkeypatch):
    def fail_graph(*_args, **_kwargs):
        raise RuntimeError("graph failed")

    monkeypatch.setattr(graph_store, "replace_document_evidence", fail_graph)
    with pytest.raises(RuntimeError, match="graph failed"):
        _ingest("ns", "doc-1", "", "Alice works at Acme.")

    conn = get_conn()
    for table in ("memories", "graph_documents", "events", "jobs"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_graph_snapshot_reports_full_totals_when_page_is_bounded(v1_store):
    for index in range(5):
        graph_store.upsert_entity("ns", f"Entity{index}", "concept")

    snapshot = graph_store.graph_snapshot("ns", limit=2)

    assert snapshot["entity_count"] == 5
    assert snapshot["returned_entity_count"] == 2
    assert snapshot["truncated"] is True


def test_retention_recovers_corrupt_state_and_caps_reinforcement(v1_store):
    from engraphis.core.retention_policy import MAX_ACCESS_COUNT, MAX_STABILITY_DAYS

    memory = _ingest("ns", "doc-1", "", "bounded retention")
    conn = get_conn()
    conn.execute(
        "UPDATE memories SET stability=?, access_count=?, last_access=? WHERE id=?",
        (float("inf"), MAX_ACCESS_COUNT + 10, float("inf"), memory["id"]),
    )
    conn.commit()

    assert 0.0 <= reweight.retention_score(
        {"stability": float("nan"), "last_access": float("nan")}
    ) <= 1.0
    reweight.reinforce(memory["id"])
    repaired = conn.execute(
        "SELECT stability, access_count, last_access FROM memories WHERE id=?",
        (memory["id"],),
    ).fetchone()
    assert math.isfinite(repaired["stability"])
    assert repaired["stability"] <= MAX_STABILITY_DAYS
    assert repaired["access_count"] == MAX_ACCESS_COUNT
    assert math.isfinite(repaired["last_access"])


def test_ledger_rejects_non_json_numbers_before_insert(v1_store):
    with pytest.raises(ValueError):
        ledger_store.append_event(
            namespace="ns",
            entity_name="Alice",
            event_type="unsafe",
            payload={"score": float("nan")},
        )
    with pytest.raises(ValueError):
        ledger_store.create_job(
            namespace="ns",
            job_type="unsafe",
            payload={"score": float("inf")},
        )
    conn = get_conn()
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_thought_persistence_records_real_document_ids(v1_store, monkeypatch):
    monkeypatch.setattr(
        thoughts_engine.recall_engine,
        "recall_master",
        lambda **_kwargs: {
            "chunks": [
                {"documentId": "doc-a", "id": 101, "content": "alpha"},
                {"documentId": "doc-b", "id": 202, "content": "beta"},
            ],
            "count": 2,
            "llmContextMessage": "context",
        },
    )

    class _ThoughtLLM:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def synthesize_thought(self, *_args, **_kwargs):
            return {"inference": "combined"}

    monkeypatch.setattr(thoughts_engine, "LLMClient", _ThoughtLLM)
    result = thoughts_engine.synthesize_thoughts(namespace="ns", persist=True)

    assert result["persisted"] is True
    thoughts = ledger_store.get_thoughts("ns")
    assert thoughts[0]["source_memory_ids"] == [
        {"namespace": "ns", "document_id": "doc-a"},
        {"namespace": "ns", "document_id": "doc-b"},
    ]


def test_active_vault_invariant_survives_invalid_activation_and_delete(v1_store):
    vault_store.create_vault(namespace="second", name="Second")
    vault_store.set_active_vault("second")

    with pytest.raises(ValueError, match="does not exist"):
        vault_store.set_active_vault("missing")
    assert vault_store.get_active_vault()["namespace"] == "second"

    vault_store.delete_vault("second")
    active = vault_store.get_active_vault()
    assert active is not None
    assert active["namespace"] == "default"
    assert get_conn().execute(
        "SELECT COUNT(*) FROM vaults WHERE is_active=1"
    ).fetchone()[0] == 1
