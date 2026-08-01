"""Focused regression tests for the dependency-free offline embedder."""

import numpy as np

from engraphis.backends.embedder_deterministic import DeterministicEmbedder, _tokenize


def _similarity(left: str, right: str) -> float:
    vectors = DeterministicEmbedder(dim=384).embed([left, right])
    return float(vectors[0] @ vectors[1])


def test_numeric_unit_rewrites_share_a_canonical_measure_feature():
    assert _similarity("retry after 1 minute", "retry after 60 seconds") > 0.45


def test_common_abbreviations_and_plural_forms_are_lexically_compatible():
    assert _similarity("request limit for the repository", "req limit for the repo") > 0.55
    assert _similarity("database configuration", "db config") > 0.25


def test_rate_features_do_not_attach_an_unrelated_number_to_a_nearby_unit():
    features = _tokenize("version 2 limit 100 requests per minute", "text")

    assert "rate:second:100" in features
    assert "rate:second:2" not in features


def test_embedding_remains_deterministic_and_normalized():
    embedder = DeterministicEmbedder(dim=97)
    first = embedder.embed(["one minute", "60 seconds"], kind="text")
    second = embedder.embed(["one minute", "60 seconds"], kind="text")
    np.testing.assert_array_equal(first, second)
    np.testing.assert_allclose(np.linalg.norm(first, axis=1), [1.0, 1.0])


def test_unrecognized_ordinary_text_keeps_legacy_feature_mapping():
    # No alias or number-unit feature is present in this input, so the old
    # stable feature-hash mapping remains byte-for-byte compatible.
    import hashlib

    vectors = DeterministicEmbedder(dim=64).embed(["alpha beta graph", "offline mapping 123"])
    assert hashlib.sha256(vectors.tobytes()).hexdigest() == (
        "c2378cd31c56863b0c65fe7b0634aa62250af35b94853298bfed34fbb71875df"
    )
