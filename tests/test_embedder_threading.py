"""Tests for embedder thread-safety (double-checked locking) and warmup()."""
import sys
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _reset_embedder_state():
    """Reset module-level globals so each test starts clean."""
    import engraphis.engines.embedder as emb_mod
    old_model, old_dim = emb_mod._model, emb_mod._dim
    emb_mod._model = None
    emb_mod._dim = None
    yield
    emb_mod._model = old_model
    emb_mod._dim = old_dim


def test_concurrent_get_model_loads_only_once():
    """N threads racing on _get_model() must trigger exactly one model load."""
    import engraphis.engines.embedder as emb_mod

    load_count = 0
    load_lock = threading.Lock()

    fake_model = MagicMock()
    fake_model.get_embedding_dimension.return_value = 384

    def slow_load(*args, **kwargs):
        nonlocal load_count
        with load_lock:
            load_count += 1
        # Simulate the 80-400 MB model load taking real time
        threading.Event().wait(0.05)
        return fake_model

    with patch("engraphis.engines.embedder.SentenceTransformer", side_effect=slow_load, create=True):
        with patch.dict("sys.modules", {"sentence_transformers": MagicMock(SentenceTransformer=slow_load)}):
            barrier = threading.Barrier(8)
            results = [None] * 8

            def worker(idx):
                barrier.wait()  # all threads start simultaneously
                results[idx] = emb_mod._get_model()

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

    assert load_count == 1, f"Model loaded {load_count} times; expected exactly 1"
    assert all(r is fake_model for r in results)


def test_warmup_returns_true_on_success():
    """warmup() returns True when the model loads successfully."""
    import engraphis.engines.embedder as emb_mod

    fake_model = MagicMock()
    fake_model.get_embedding_dimension.return_value = 384

    with patch.dict("sys.modules", {"sentence_transformers": MagicMock(SentenceTransformer=lambda *a, **kw: fake_model)}):
        assert emb_mod.warmup() is True
    assert emb_mod._model is fake_model


def test_chunk_text_preserves_source_when_overlap_fills_budget():
    from engraphis.engines.embedder import chunk_text

    chunks = chunk_text(
        "abcdefghij klmnopqrst uvwxyz ABCDEFGHIJ KLMNOPQRST UVWXYZ",
        chunk_size=20,
        overlap=8,
    )

    assert len(chunks) > 1
    assert chunks[1].endswith("klmnopqrst uvwxyz")
    assert all(len(chunk) <= 20 for chunk in chunks)


def test_chunk_text_preserves_source_when_overlap_requires_rechunking():
    from engraphis.engines.embedder import chunk_text

    source = "leadword suffix-marker " + " ".join(f"w{index:02d}" for index in range(24))
    chunks = chunk_text(source, chunk_size=20, overlap=8)

    recovered = " ".join(chunks)
    assert all(token in recovered.split() for token in source.split())
    assert all(len(chunk) <= 20 for chunk in chunks)


def test_warmup_returns_false_on_failure():
    """warmup() returns False (never raises) when model loading fails."""
    import engraphis.engines.embedder as emb_mod

    def exploding_load(*args, **kwargs):
        raise RuntimeError("model file missing")

    with patch.dict("sys.modules", {"sentence_transformers": MagicMock(SentenceTransformer=exploding_load)}):
        assert emb_mod.warmup() is False
    assert emb_mod._model is None


def test_warmup_idempotent():
    """Calling warmup() twice loads the model only once."""
    import engraphis.engines.embedder as emb_mod

    load_count = 0
    fake_model = MagicMock()
    fake_model.get_embedding_dimension.return_value = 384

    def counting_load(*args, **kwargs):
        nonlocal load_count
        load_count += 1
        return fake_model

    with patch.dict("sys.modules", {"sentence_transformers": MagicMock(SentenceTransformer=counting_load)}):
        assert emb_mod.warmup() is True
        assert emb_mod.warmup() is True

    assert load_count == 1


def test_legacy_embedder_forwards_pinned_revision_and_disables_remote_code(monkeypatch):
    """The legacy reference loader must preserve the v2 source-policy guarantees."""
    import engraphis.engines.embedder as emb_mod

    captured = {}
    fake_model = MagicMock()
    fake_model.get_embedding_dimension.return_value = 384

    def load(name, **kwargs):
        captured.update(name=name, **kwargs)
        return fake_model

    monkeypatch.setattr(emb_mod.settings, "embed_model", "organization/semantic-model")
    monkeypatch.setattr(emb_mod.settings, "embed_revision", "a" * 40)
    monkeypatch.setattr(emb_mod.settings, "require_immutable_models", True)
    monkeypatch.setitem(
        sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=load)
    )

    assert emb_mod._get_model() is fake_model
    assert captured == {
        "name": "organization/semantic-model",
        "revision": "a" * 40,
        "trust_remote_code": False,
    }


def test_legacy_embedder_local_selector_is_offline_only(monkeypatch):
    import engraphis.engines.embedder as emb_mod

    captured = {}
    fake_model = MagicMock()
    fake_model.get_embedding_dimension.return_value = 384

    def load(name, **kwargs):
        captured.update(name=name, **kwargs)
        return fake_model

    monkeypatch.setattr(emb_mod.settings, "embed_model", "local:C:/models/bge-small")
    monkeypatch.setattr(emb_mod.settings, "embed_revision", "")
    monkeypatch.setattr(emb_mod.settings, "require_immutable_models", True)
    monkeypatch.setitem(
        sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=load)
    )

    assert emb_mod._get_model() is fake_model
    assert captured == {
        "name": "C:/models/bge-small",
        "local_files_only": True,
        "trust_remote_code": False,
    }


def test_legacy_embedder_strict_policy_rejects_before_loader_import(monkeypatch):
    import engraphis.engines.embedder as emb_mod

    monkeypatch.setattr(emb_mod.settings, "embed_model", "organization/semantic-model")
    monkeypatch.setattr(emb_mod.settings, "embed_revision", "main")
    monkeypatch.setattr(emb_mod.settings, "require_immutable_models", True)

    with pytest.raises(ValueError, match="ENGRAPHIS_REQUIRE_IMMUTABLE_MODELS"):
        emb_mod._get_model()


def test_legacy_embedder_warmup_redacts_loader_error(monkeypatch, caplog):
    import engraphis.engines.embedder as emb_mod

    marker = "signed-provider-url-secret"
    monkeypatch.setattr(emb_mod.settings, "embed_model", "organization/semantic-model")
    monkeypatch.setattr(emb_mod.settings, "embed_revision", "")
    monkeypatch.setattr(emb_mod.settings, "require_immutable_models", False)

    def load(*_args, **_kwargs):
        raise RuntimeError(marker)

    monkeypatch.setitem(
        sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=load)
    )

    with caplog.at_level("WARNING", logger="engraphis.embedder"):
        assert emb_mod.warmup() is False

    assert marker not in caplog.text
