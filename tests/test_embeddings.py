"""Focused regression tests for the dependency-free offline embedder."""

import numpy as np
import pytest
import httpx

from engraphis.backends.embedder_api import ApiEmbedder
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


def test_deterministic_embedder_explicitly_disables_semantic_search():
    embedder = DeterministicEmbedder()

    assert embedder.embedding_mode == "lexical_hashing"
    assert embedder.supports_semantic_search is False


def test_unrecognized_ordinary_text_keeps_legacy_feature_mapping():
    # No alias or number-unit feature is present in this input, so the old
    # stable feature-hash mapping remains byte-for-byte compatible.
    import hashlib

    vectors = DeterministicEmbedder(dim=64).embed(["alpha beta graph", "offline mapping 123"])
    assert hashlib.sha256(vectors.tobytes()).hexdigest() == (
        "c2378cd31c56863b0c65fe7b0634aa62250af35b94853298bfed34fbb71875df"
    )


@pytest.mark.parametrize("dimension", [True, 0, -1, 1.5, "384", 65_537])
def test_embedding_dimensions_are_bounded_integers(dimension):
    with pytest.raises(ValueError, match="embedding dimension"):
        DeterministicEmbedder(dim=dimension)
    with pytest.raises(ValueError, match="embedding dimension"):
        ApiEmbedder(model="model", api_key="key", dim=dimension)


def test_empty_api_embedding_batch_never_probes_for_a_dimension(monkeypatch):
    embedder = ApiEmbedder(model="model", api_key="key")
    monkeypatch.setattr(
        embedder,
        "embed",
        lambda texts, **kwargs: (_ for _ in ()).throw(AssertionError("remote probe")),
    )

    # Invoke the class implementation so the instance monkeypatch would catch a
    # recursive dimension probe made through ``self.dim``.
    result = ApiEmbedder.embed(embedder, [])

    assert result.shape == (0, 0)


def test_api_batch_vectors_require_complete_unique_indices_and_consistent_width():
    embedder = ApiEmbedder(model="model", api_key="key", dim=2)

    assert embedder._ordered_batch_vectors({"data": [
        {"index": 1, "embedding": [0.0, 1.0]},
        {"index": 0, "embedding": [1.0, 0.0]},
    ]}, 2) == [[1.0, 0.0], [0.0, 1.0]]
    assert embedder._ordered_batch_vectors({"data": [
        {"index": 0, "embedding": [1.0, 0.0]},
    ]}, 2) is None
    assert embedder._ordered_batch_vectors({"data": [
        {"index": 0, "embedding": [1.0, 0.0]},
        {"index": 0, "embedding": [0.0, 1.0]},
    ]}, 2) is None
    assert embedder._ordered_batch_vectors({"data": [
        {"index": 0, "embedding": [float("nan"), 0.0]},
        {"index": 1, "embedding": [0.0, 1.0]},
    ]}, 2) is None


def test_api_per_item_fallback_is_cardinality_safe_and_normalized(monkeypatch):
    responses = [
        {"data": [{"index": 0, "embedding": [3.0, 4.0]}]},
        {"data": [{"index": 0, "embedding": [0.0, 2.0]}]},
    ]

    class _Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class _Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, *_args, **kwargs):
            if len(kwargs["json"]["input"]) > 1:
                return _Response({"data": []})
            return _Response(responses.pop(0))

    monkeypatch.setattr(httpx, "Client", _Client)

    result = ApiEmbedder(model="model", api_key="key").embed(["a", "b"])

    np.testing.assert_allclose(result, [[0.6, 0.8], [0.0, 1.0]])


def test_api_per_item_fallback_fills_malformed_rows_at_the_valid_width(monkeypatch):
    responses = [
        {"data": [{"index": "private-index", "embedding": [9.0]}]},
        {"data": [{"index": 0, "embedding": [0.0, 2.0]}]},
    ]

    class _Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class _Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, *_args, **kwargs):
            if len(kwargs["json"]["input"]) > 1:
                return _Response({"data": []})
            return _Response(responses.pop(0))

    monkeypatch.setattr(httpx, "Client", _Client)

    result = ApiEmbedder(model="model", api_key="key").embed(["a", "b"])

    assert result.shape == (2, 2)
    np.testing.assert_allclose(result, [[0.0, 0.0], [0.0, 1.0]])


def test_api_rejects_configured_dimension_mismatch_from_batch_response(monkeypatch):
    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"index": 0, "embedding": [1.0, 0.0, 0.0]}]}

    class _Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, *_args, **_kwargs):
            return _Response()

    monkeypatch.setattr(httpx, "Client", _Client)

    with pytest.raises(RuntimeError, match="unexpected dimension"):
        ApiEmbedder(model="model", api_key="key", dim=2).embed(["a"])


def test_api_rejects_configured_dimension_mismatch_during_per_item_fallback(monkeypatch):
    class _Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class _Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, *_args, **kwargs):
            if len(kwargs["json"]["input"]) > 1:
                return _Response({"data": []})
            return _Response({"data": [{"index": 0, "embedding": [1.0, 0.0, 0.0]}]})

    monkeypatch.setattr(httpx, "Client", _Client)

    with pytest.raises(RuntimeError, match="unexpected dimension"):
        ApiEmbedder(model="model", api_key="key", dim=2).embed(["a", "b"])
