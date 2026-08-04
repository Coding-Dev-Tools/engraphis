"""v1 ingest trust model: local documents are recallable, external imports are not.

The legacy ``engines.ingest`` path predates the v2 quarantine/provenance boundary.
Local user documents (memory files, manual entries) are trusted and recall-visible;
external file imports (folder/upload) are untrusted and filtered from agent-facing
recall — mirroring the v2 prompt gate without breaking the primary v1 UX.
"""
from __future__ import annotations

import json
import threading

import numpy as np
import pytest

from engraphis.engines import ingest as ingest_engine
from engraphis.engines import recall as recall_engine
from engraphis.stores import get_conn, init_db


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