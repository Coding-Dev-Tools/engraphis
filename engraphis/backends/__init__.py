"""Pluggable backends implementing the engraphis.core interfaces.

Phase 0 ships *reference* implementations chosen for portability and zero extra
dependencies so the system runs and is testable anywhere:

* ``NumpyVectorIndex``  — brute-force cosine over the store. Correct, not fast.
                          Phase 1 replaces it with a ``sqlite-vec`` / LanceDB /
                          Qdrant backend behind the same ``VectorIndex`` interface.
* ``DeterministicEmbedder`` — a hashing embedder with no model download, for
                          offline tests and CI. Production uses a real model
                          (BGE-M3 / Qwen3 class) behind the same ``Embedder`` interface.
"""
from engraphis.backends.embedder_deterministic import DeterministicEmbedder
from engraphis.backends.vector_numpy import NumpyVectorIndex

__all__ = ["DeterministicEmbedder", "NumpyVectorIndex"]
