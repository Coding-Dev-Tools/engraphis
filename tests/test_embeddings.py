"""Focused regression tests for the dependency-free offline embedder."""

import numpy as np
import pytest

from engraphis.backends.embedder_api import ApiEmbedder
from engraphis.backends.embedder_deterministic import (
    DeterministicEmbedder,
    _bounded_trigrams,
    _tokenize,
)


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


def test_trigram_work_is_bounded_before_slicing_long_input():
    class _CountingText(str):
        def __new__(cls, value):
            instance = super().__new__(cls, value)
            instance.slices = 0
            return instance

        def __getitem__(self, item):
            if isinstance(item, slice):
                self.slices += 1
            return super().__getitem__(item)

    text = _CountingText("x" * 1_000_000)

    assert len(_bounded_trigrams(text)) == 512
    assert text.slices == 512


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
    httpx = pytest.importorskip("httpx")
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


def test_api_per_item_fallback_rejects_partial_failure_instead_of_zero_vector(monkeypatch):
    httpx = pytest.importorskip("httpx")
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

    with pytest.raises(RuntimeError, match="incomplete response"):
        ApiEmbedder(model="model", api_key="key").embed(["a", "b"])


def test_api_embeddings_endpoint_normalizes_versioned_and_unversioned_bases():
    assert ApiEmbedder(
        model="model", api_key="key", dim=2,
    )._embeddings_url == "https://openrouter.ai/api/v1/embeddings"
    assert ApiEmbedder(
        model="model", base_url="https://provider.example", api_key="key", dim=2,
    )._embeddings_url == "https://provider.example/v1/embeddings"
    assert ApiEmbedder(
        model="model", base_url="https://provider.example/custom/v1/", api_key="key", dim=2,
    )._embeddings_url == "https://provider.example/custom/v1/embeddings"
    assert ApiEmbedder(
        model="model",
        base_url="https://provider.example/custom/v1/?tenant=one",
        api_key="key",
        dim=2,
    )._embeddings_url == "https://provider.example/custom/v1/embeddings?tenant=one"
    assert ApiEmbedder(
        model="model",
        base_url="https://provider.example/custom/v1/?signature=abc/",
        api_key="key",
        dim=2,
    )._embeddings_url == (
        "https://provider.example/custom/v1/embeddings?signature=abc/"
    )
    assert ApiEmbedder(
        model="model",
        base_url="https://provider.example/custom/v1/embeddings?tenant=one",
        api_key="key",
        dim=2,
    )._embeddings_url == "https://provider.example/custom/v1/embeddings?tenant=one"


def test_api_embedding_identity_is_credential_free_and_space_specific():
    first = ApiEmbedder(
        model="model-a", base_url="https://provider.example", api_key="secret-a", dim=2,
        space_version="provider-revision-1",
    )
    same = ApiEmbedder(
        model="model-a", base_url="https://provider.example", api_key="secret-b", dim=2,
        space_version="provider-revision-1",
    )
    other = ApiEmbedder(
        model="model-b", base_url="https://provider.example", api_key="secret-a", dim=2,
        space_version="provider-revision-1",
    )
    changed = ApiEmbedder(
        model="model-a", base_url="https://provider.example", api_key="secret-a", dim=2,
        space_version="provider-revision-2",
    )
    credentialed = ApiEmbedder(
        model="model-a",
        base_url="https://alice:secret@provider.example/v1?token=one",
        api_key="secret-a",
        dim=2,
        space_version="provider-revision-1",
    )
    rotated_credentials = ApiEmbedder(
        model="model-a",
        base_url="https://bob:rotated@provider.example/v1?token=two",
        api_key="secret-b",
        dim=2,
        space_version="provider-revision-1",
    )

    assert first.embedding_identity == "api_embeddings"
    assert first.embedding_version == same.embedding_version
    assert len({first.embedding_version, other.embedding_version, changed.embedding_version}) == 3
    assert "secret" not in first.embedding_version
    assert credentialed.embedding_version == rotated_credentials.embedding_version
    assert ApiEmbedder(model="model-a", api_key="key", dim=2).embedding_version == ""


def test_api_rejects_all_failed_fallback_without_a_known_dimension():
    embedder = ApiEmbedder(model="model", api_key="key")

    with pytest.raises(RuntimeError, match="no usable vectors"):
        embedder._finalize_vectors([None, None], 2)
    assert embedder._dim is None


def test_api_normalizes_finite_extreme_vectors_without_float32_overflow():
    embedder = ApiEmbedder(model="model", api_key="key")
    extreme = float(np.finfo(np.float32).max)

    result = embedder._finalize_vectors([[extreme, extreme]], 1)

    assert np.isfinite(result).all()
    np.testing.assert_allclose(np.linalg.norm(result[0]), 1.0, rtol=1e-6)


def test_api_rejects_configured_dimension_mismatch_from_batch_response(monkeypatch):
    httpx = pytest.importorskip("httpx")
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
    httpx = pytest.importorskip("httpx")
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
