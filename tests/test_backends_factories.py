import hashlib

import numpy as np
import pytest

from engraphis.backends.embedder_deterministic import DeterministicEmbedder
from engraphis.backends.embedder_st import get_embedder
from engraphis.backends.reranker import IdentityReranker, get_reranker
from engraphis.backends.vector_numpy import NumpyVectorIndex
from engraphis.backends.vector_sqlitevec import get_vector_index
from engraphis.core.engine import MemoryEngine
from engraphis.core.interfaces import MemoryRecord, Scope
from engraphis.core.store import Store


def _force_load_failure(monkeypatch, module, attr: str) -> None:
    """Make the heavy model adapter fail at construction, without touching the network.

    The factories fall back when a model fails to load, but resolving an unknown model name
    normally goes out to the Hugging Face Hub. On a host with no route to the Hub that
    connect() blocks rather than erroring, so the offline gate would hang forever — the
    fallback relies on the network failing *fast*, not on it being *absent* (AGENTS.md §3:
    the core must run offline).

    Patch the adapter the factory constructs, not sentence-transformers itself: the optional
    heavy stack is then never imported here, so an install that raises something other than
    ImportError (e.g. a RuntimeError from a mismatched torch) cannot fail this test. Every
    such failure stays inside the factory, which catches Exception and falls back — which is
    exactly the contract under test.
    """
    def _raise(*args, **kwargs):
        raise OSError("simulated unresolvable model (offline)")

    monkeypatch.setattr(module, attr, _raise)


def test_embedder_factory_falls_back_offline(monkeypatch):
    import engraphis.backends.embedder_st as embedder_st

    assert isinstance(get_embedder(None, 128), DeterministicEmbedder)
    # An unresolvable model name must not crash — it falls back.
    _force_load_failure(monkeypatch, embedder_st, "SentenceTransformerEmbedder")
    assert isinstance(get_embedder("definitely-not-a-real-model-xyz", 128), DeterministicEmbedder)


def test_embedder_factory_forwards_an_immutable_model_revision(monkeypatch):
    import engraphis.backends.embedder_st as embedder_st

    captured = {}

    class _PinnedEmbedder:
        dim = 128

        def __init__(self, model_name, *, revision=None):
            captured.update(model_name=model_name, revision=revision)

    monkeypatch.setattr(embedder_st, "SentenceTransformerEmbedder", _PinnedEmbedder)
    result = get_embedder("Qwen/example", 128, revision="a" * 40)

    assert isinstance(result, _PinnedEmbedder)
    assert captured == {"model_name": "Qwen/example", "revision": "a" * 40}


def test_deterministic_embedder_preserves_legacy_feature_hash_mapping():
    """Changing the feature-hash algorithm would invalidate existing local vectors."""
    vectors = DeterministicEmbedder(dim=64).embed(["alpha beta graph", "offline mapping 123"])
    assert hashlib.sha256(vectors.tobytes()).hexdigest() == (
        "c2378cd31c56863b0c65fe7b0634aa62250af35b94853298bfed34fbb71875df"
    )


def test_deterministic_embedder_upgrade_rebuilds_legacy_vectors(tmp_path):
    db = tmp_path / "legacy-deterministic.db"
    text = "The API config allows 1 minute between requests."
    store = Store(str(db))
    workspace_id = store.get_or_create_workspace("w")
    memory_id = store.add_memory(MemoryRecord(
        id="", content=text, workspace_id=workspace_id, scope=Scope.WORKSPACE,
    ))
    quarantined_id = store.add_memory(MemoryRecord(
        id="", content="Quarantined payload.", workspace_id=workspace_id,
        scope=Scope.WORKSPACE,
        provenance={"source": "import", "trusted": False, "quarantined": True},
    ))
    legacy_vector = np.zeros(64, dtype=np.float32)
    legacy_vector[0] = 1.0
    store.put_vector(memory_id, legacy_vector)
    store.conn.execute("DROP TABLE embedding_state")
    store.conn.execute("DELETE FROM schema_migrations")
    store.conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (6, 0)")
    store.conn.commit()
    store.close()

    engine = MemoryEngine.create(str(db), embed_dim=64, vector_backend="numpy")
    expected = engine.embedder.embed([text])[0]
    rebuilt = dict(engine.store.iter_vectors(dim=64))

    assert np.allclose(rebuilt[memory_id], expected)
    assert not np.allclose(legacy_vector, rebuilt[memory_id])
    assert quarantined_id not in rebuilt
    assert engine.store.embedding_version("deterministic_hashing") == "v2_aliases_measurements"
    engine.store.close()


def test_deterministic_embedder_upgrade_refreshes_sqlitevec_and_store_mirrors(tmp_path):
    """A later NumPy fallback must see the vector rebuilt through sqlite-vec."""
    pytest.importorskip("sqlite_vec", reason="sqlite-vec extra not installed")
    db = tmp_path / "legacy-deterministic-sqlitevec.db"
    text = "The API config allows 1 minute between requests."
    store = Store(str(db))
    workspace_id = store.get_or_create_workspace("w")
    memory_id = store.add_memory(MemoryRecord(
        id="", content=text, workspace_id=workspace_id, scope=Scope.WORKSPACE,
    ))
    legacy_vector = np.zeros(64, dtype=np.float32)
    legacy_vector[0] = 1.0
    store.put_vector(memory_id, legacy_vector)
    store.conn.execute("DROP TABLE embedding_state")
    store.conn.execute("DELETE FROM schema_migrations")
    store.conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (6, 0)")
    store.conn.commit()
    store.close()

    sqlitevec_engine = MemoryEngine.create(str(db), embed_dim=64, vector_backend="sqlite-vec")
    expected = sqlitevec_engine.embedder.embed([text])[0]
    stored = dict(sqlitevec_engine.store.iter_vectors(dim=64))
    ann_row = sqlitevec_engine.store.conn.execute(
        "SELECT embedding FROM mem_vec_ann WHERE id=?", (memory_id,)
    ).fetchone()

    assert np.allclose(stored[memory_id], expected)
    assert ann_row is not None
    assert np.allclose(np.frombuffer(ann_row["embedding"], dtype=np.float32), expected)
    assert sqlitevec_engine.store.embedding_version("deterministic_hashing") == (
        "v2_aliases_measurements"
    )
    sqlitevec_engine.store.close()

    numpy_engine = MemoryEngine.create(str(db), embed_dim=64, vector_backend="numpy")
    assert np.allclose(dict(numpy_engine.store.iter_vectors(dim=64))[memory_id], expected)
    numpy_engine.store.close()


def test_vector_index_factory_modes(monkeypatch):
    """prefer="numpy" always forces the reference index; prefer="auto" returns the
    best AVAILABLE backend — asserted for both availability branches explicitly
    (sqlite-vec is a [test] dependency now, so its absence must be simulated)."""
    import engraphis.backends.vector_sqlitevec as vs

    s = Store(":memory:")
    assert isinstance(get_vector_index(s, dim=128, prefer="numpy"), NumpyVectorIndex)
    try:
        import sqlite_vec  # noqa: F401
        assert isinstance(get_vector_index(s, dim=128, prefer="auto"),
                          vs.SqliteVecVectorIndex)
    except ImportError:
        pass

    class _Unavailable:
        def __init__(self, *a, **k):
            raise ImportError("sqlite_vec not installed (simulated)")

    monkeypatch.setattr(vs, "SqliteVecVectorIndex", _Unavailable)
    assert isinstance(get_vector_index(s, dim=128, prefer="auto"), NumpyVectorIndex)
    s.close()


def test_reranker_factory_falls_back_offline(monkeypatch):
    import engraphis.backends.reranker as reranker

    assert isinstance(get_reranker(None), IdentityReranker)
    _force_load_failure(monkeypatch, reranker, "CrossEncoderReranker")
    assert isinstance(get_reranker("definitely-not-a-real-model-xyz"), IdentityReranker)
