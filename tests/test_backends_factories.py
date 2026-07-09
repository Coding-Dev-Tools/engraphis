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


def test_vector_index_factory_modes():
    s = Store(":memory:")
    assert isinstance(get_vector_index(s, dim=128, prefer="numpy"), NumpyVectorIndex)
    assert isinstance(get_vector_index(s, dim=128, prefer="auto"), NumpyVectorIndex)
    s.close()


def test_reranker_factory_falls_back_offline():
    assert isinstance(get_reranker(None), IdentityReranker)
    assert isinstance(get_reranker("definitely-not-a-real-model-xyz"), IdentityReranker)
