"""v1.6 envelope additions: backend/reranker capability keys + ledger health counts.

Pins the additive recall-envelope keys (``vector_index_backend``,
``reranker_mode``), the ``stats()`` ledger row counts (operation_receipts /
events / audit), visibility-batch parity with the store's ``IN_CLAUSE_CHUNK``,
and the single fallback warning in the sqlite-vec factory. Additive-only:
existing envelope keys must never disappear.
"""
import logging

import pytest

from engraphis.backends import vector_sqlitevec
from engraphis.backends.vector_numpy import NumpyVectorIndex
from engraphis.core.store import IN_CLAUSE_CHUNK
from engraphis.service import MemoryService, _reranker_mode_label, _vector_index_backend_label


def _svc():
    return MemoryService.create(":memory:", extractor="none", graph_extractor="none")


# ── recall envelope: additive capability keys ─────────────────────────────────

def test_recall_envelope_gains_backend_keys_and_keeps_existing_ones():
    svc = _svc()
    try:
        svc.remember("The deploy key rotates weekly.", workspace="acme")
        out = svc.recall("deploy key", workspace="acme")
    finally:
        svc.close()
    # Pre-existing keys survive untouched (additive-only contract).
    for key in (
        "degraded_mode", "semantic_support", "embedding_mode",
        "degraded_reason", "vector_search_ready",
    ):
        assert key in out, f"envelope regression: '{key}' missing"
    # New additive keys.
    assert out["vector_index_backend"] == "numpy"
    assert out["reranker_mode"] == "identity"


    cls = type("SqliteVecVectorIndex", (), {})
    assert _vector_index_backend_label(cls()) == "sqlite-vec"
    assert _vector_index_backend_label(NumpyVectorIndex.__new__(NumpyVectorIndex)) == "numpy"
    assert _vector_index_backend_label(None) == "numpy"
    # Unknown third-party backends report their own class name, never a lie.
    exotic = type("ExoticIndex", (), {})
    assert _vector_index_backend_label(exotic()) == "ExoticIndex"

    assert _reranker_mode_label(None) == "identity"
    identity = type("IdentityReranker", (), {})
    assert _reranker_mode_label(identity()) == "identity"
    cross = type("CrossEncoderReranker", (), {})
    assert _reranker_mode_label(cross()) == "cross-encoder"
    custom = type("MyReranker", (), {})
    assert _reranker_mode_label(custom()) == "MyReranker"


# ── stats()/health payload: ledger row counts ─────────────────────────────────

def test_stats_reports_ledger_row_counts():
    svc = _svc()
    try:
        wid = svc.store.get_or_create_workspace("acme")
        svc.remember("Ledger health probe.", workspace="acme")
        svc.store.record_receipt("remember", workspace_id=wid)
        st = svc.stats(workspace="acme")
    finally:
        svc.close()
    for key in ("operation_receipts", "events", "audit"):
        assert key in st, f"health payload regression: '{key}' missing"
        assert isinstance(st[key], int) and st[key] >= 0
    assert st["operation_receipts"] >= 1


def test_stats_ledger_counts_degrade_to_none_not_raise():
    svc = _svc()
    try:
        svc.remember("Degraded count probe.", workspace="acme")
        # Simulate an unreadable ledger table; stats() must report None, not raise.
        svc.store.conn.execute("DROP TABLE audit")
        st = svc.stats(workspace="acme")
    finally:
        svc.close()
    assert st["audit"] is None
    assert isinstance(st["operation_receipts"], int)
    assert isinstance(st["events"], int)
    # Core memory counts are unaffected.
    assert st["memories"] == 1




# ── visibility batching parity ────────────────────────────────────────────────

def test_visibility_batch_size_matches_store_in_clause_chunk():
    # Identical IN-chunk semantics; the batch size must track the store limit so
    # visible_memory_ids never receives an oversized batch.
    assert vector_sqlitevec._VISIBILITY_BATCH_SIZE == IN_CLAUSE_CHUNK == 500


# ── factory fallback observability ────────────────────────────────────────────

def test_get_vector_index_auto_fallback_warns_once_naming_backends(caplog, tmp_path):
    from engraphis.core.store import Store

    def _boom(store, dimension):
        raise RuntimeError("sqlite-vec extension unavailable")

    store = Store(str(tmp_path / "fallback.db"))
    try:
        with caplog.at_level(logging.WARNING, logger="engraphis"):
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(vector_sqlitevec, "SqliteVecVectorIndex", _boom)
                index = vector_sqlitevec.get_vector_index(store, dim=8, prefer="auto")
        assert isinstance(index, NumpyVectorIndex)
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        assert "sqlite-vec" in message and "NumpyVectorIndex" in message
        assert "RuntimeError" in message
    finally:
        store.close()


def test_get_vector_index_sqlite_vec_pref_raises_no_warning(caplog, tmp_path):
    from engraphis.core.store import Store

    def _boom(store, dimension):
        raise RuntimeError("sqlite-vec extension unavailable")

    store = Store(str(tmp_path / "pref.db"))
    try:
        with caplog.at_level(logging.WARNING, logger="engraphis"):
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(vector_sqlitevec, "SqliteVecVectorIndex", _boom)
                with pytest.raises(RuntimeError):
                    vector_sqlitevec.get_vector_index(store, dim=8, prefer="sqlite-vec")
        assert not [r for r in caplog.records if r.levelno == logging.WARNING]
    finally:
        store.close()
