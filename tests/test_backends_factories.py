import hashlib

from engraphis.backends.embedder_deterministic import DeterministicEmbedder
from engraphis.backends.embedder_st import get_embedder
from engraphis.backends.reranker import IdentityReranker, get_reranker
from engraphis.backends.vector_numpy import NumpyVectorIndex
from engraphis.backends.vector_sqlitevec import get_vector_index
from engraphis.core.store import Store


def test_embedder_factory_falls_back_offline():
    assert isinstance(get_embedder(None, 128), DeterministicEmbedder)
    # An unresolvable model name must not crash — it falls back.
    assert isinstance(get_embedder("definitely-not-a-real-model-xyz", 128), DeterministicEmbedder)


def test_deterministic_embedder_preserves_legacy_feature_hash_mapping():
    """Changing the feature-hash algorithm would invalidate existing local vectors."""
    vectors = DeterministicEmbedder(dim=64).embed(["alpha beta graph", "offline mapping 123"])
    assert hashlib.sha256(vectors.tobytes()).hexdigest() == (
        "c2378cd31c56863b0c65fe7b0634aa62250af35b94853298bfed34fbb71875df"
    )


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


def test_reranker_factory_falls_back_offline():
    assert isinstance(get_reranker(None), IdentityReranker)
    assert isinstance(get_reranker("definitely-not-a-real-model-xyz"), IdentityReranker)
