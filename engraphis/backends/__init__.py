"""Portable defaults for the pluggable :mod:`engraphis.core` interfaces.

``NumpyVectorIndex`` and ``DeterministicEmbedder`` keep the core dependency-light,
deterministic, and fully offline. Deployments can select the optional native
``SQLiteVecIndex`` and sentence-transformer or API embedders through the backend
factories without changing core code.
"""
from engraphis.backends.embedder_deterministic import DeterministicEmbedder
from engraphis.backends.vector_numpy import NumpyVectorIndex

__all__ = ["DeterministicEmbedder", "NumpyVectorIndex"]
