import hashlib
import logging
import os
import sys
import traceback
from types import SimpleNamespace

import numpy as np
import pytest

from engraphis.backends.embedder_deterministic import DeterministicEmbedder
from engraphis.backends.embedder_st import SentenceTransformerEmbedder, get_embedder
from engraphis.backends.model_source import validate_model_source
from engraphis.backends.reranker import (
    CrossEncoderReranker,
    IdentityReranker,
    get_reranker,
)
from engraphis.backends.vector_numpy import NumpyVectorIndex
from engraphis.backends.vector_sqlitevec import get_vector_index
from engraphis.core.engine import MemoryEngine
from engraphis.core.interfaces import MemoryRecord, Scope
from engraphis.core.store import Store


pytestmark = pytest.mark.native_sqlitevec


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


def test_embedder_strict_failure_is_redacted_and_not_chained(monkeypatch):
    import engraphis.backends.embedder_st as embedder_st

    def unavailable(*args, **kwargs):
        raise RuntimeError("token=super-secret path=C:/private/model")

    monkeypatch.setattr(embedder_st, "SentenceTransformerEmbedder", unavailable)
    with pytest.raises(RuntimeError) as caught:
        get_embedder("C:/private/model", 128, require_exact=True)

    assert "super-secret" not in str(caught.value)
    assert "C:/private/model" not in str(caught.value)
    assert caught.value.__cause__ is None


def test_memory_engine_create_forwards_exact_backend_mode(monkeypatch):
    import engraphis.core.engine as engine_module

    captured = {}

    def factory(**kwargs):
        captured.update(kwargs)
        return "engine"

    monkeypatch.setattr(engine_module, "_ENGINE_FACTORY", factory)
    assert MemoryEngine.create(require_exact_backends=True) == "engine"
    assert captured["require_exact_backends"] is True


def test_embedder_factory_forwards_an_immutable_model_revision(monkeypatch):
    import engraphis.backends.embedder_st as embedder_st

    captured = {}

    class _PinnedEmbedder:
        dim = 128

        def __init__(
            self, model_name, *, revision=None, require_immutable_models=None
        ):
            captured.update(
                model_name=model_name,
                revision=revision,
                require_immutable_models=require_immutable_models,
            )

    monkeypatch.setattr(embedder_st, "SentenceTransformerEmbedder", _PinnedEmbedder)
    result = get_embedder(
        "Qwen/example", 128, revision="a" * 40, require_immutable_models=True,
    )

    assert isinstance(result, _PinnedEmbedder)
    assert captured == {
        "model_name": "Qwen/example",
        "revision": "a" * 40,
        "require_immutable_models": True,
    }


@pytest.mark.parametrize("revision", [None, "main", "A" * 40, "a" * 39])
def test_embedder_strict_mode_rejects_mutable_remote_revision_before_load(monkeypatch, revision):
    import engraphis.backends.embedder_st as embedder_st

    attempts = []
    monkeypatch.setattr(
        embedder_st,
        "SentenceTransformerEmbedder",
        lambda *args, **kwargs: attempts.append((args, kwargs)),
    )

    with pytest.raises(ValueError, match="ENGRAPHIS_REQUIRE_IMMUTABLE_MODELS"):
        get_embedder(
            "organization/remote-model",
            128,
            revision=revision,
            require_immutable_models=True,
        )

    assert attempts == []


def test_embedder_default_mode_keeps_mutable_remote_tag_compatibility(monkeypatch):
    import engraphis.backends.embedder_st as embedder_st

    captured = {}

    class _Embedder:
        dim = 128

        def __init__(
            self, model_name, *, revision=None, require_immutable_models=None
        ):
            captured.update(model_name=model_name, revision=revision)

    monkeypatch.setattr(embedder_st, "SentenceTransformerEmbedder", _Embedder)
    result = get_embedder("organization/remote-model", 128, revision="main")

    assert isinstance(result, _Embedder)
    assert captured == {"model_name": "organization/remote-model", "revision": "main"}


def test_embedder_strict_mode_permits_existing_local_selector(monkeypatch, tmp_path):
    import engraphis.backends.embedder_st as embedder_st

    captured = {}
    model_dir = tmp_path / "cached-model"
    model_dir.mkdir()

    class _Embedder:
        dim = 128

        def __init__(
            self,
            model_name,
            *,
            revision=None,
            local_files_only=False,
            require_immutable_models=None,
        ):
            captured.update(
                model_name=model_name,
                revision=revision,
                local_files_only=local_files_only,
                require_immutable_models=require_immutable_models,
            )

    monkeypatch.setattr(embedder_st, "SentenceTransformerEmbedder", _Embedder)
    result = get_embedder(
        str(model_dir), 128, require_immutable_models=True,
    )

    assert isinstance(result, _Embedder)
    assert captured == {
        "model_name": str(model_dir),
        "revision": None,
        "local_files_only": True,
        "require_immutable_models": True,
    }


def test_model_policy_permits_an_existing_local_directory_without_a_revision(tmp_path):
    validate_model_source(
        str(tmp_path), None, require_immutable_models=True, loader="test model",
    )


def test_model_policy_permits_drive_relative_windows_path_without_a_revision():
    validate_model_source(
        r"C:models\bge-small", None, require_immutable_models=True, loader="test model",
    )


def test_sentence_transformer_disables_remote_code(monkeypatch):
    captured = {}

    class _Model:
        def __init__(self, _name, **kwargs):
            captured.update(kwargs)

        def get_embedding_dimension(self):
            return 128

    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        SimpleNamespace(SentenceTransformer=_Model),
    )

    SentenceTransformerEmbedder("organization/remote-model", revision="a" * 40)

    assert captured == {"trust_remote_code": False, "revision": "a" * 40}


def test_sentence_transformer_strict_local_cache_selector_never_requires_remote_revision(
    monkeypatch,
):
    captured = {}

    class _Model:
        commit_hash = "a" * 40

        def __init__(self, _name, **kwargs):
            captured.update(kwargs)

        def get_embedding_dimension(self):
            return 128

    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        SimpleNamespace(SentenceTransformer=_Model),
    )

    embedder = SentenceTransformerEmbedder(
        "organization/cached-model",
        local_files_only=True,
        require_immutable_models=True,
    )

    assert embedder.embedding_version
    assert captured == {"trust_remote_code": False, "local_files_only": True}


def test_sentence_transformer_identity_uses_loader_resolved_commit(monkeypatch):
    commits = iter(("a" * 40, "b" * 40))

    class _Model:
        def __init__(self, _name, **_kwargs):
            self.commit_hash = next(commits)

        def get_embedding_dimension(self):
            return 128

    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        SimpleNamespace(SentenceTransformer=_Model),
    )

    first = SentenceTransformerEmbedder("organization/model", revision="main")
    second = SentenceTransformerEmbedder("organization/model", revision="main")

    assert first.embedding_version
    assert first.embedding_version != second.embedding_version


def test_cross_encoder_reranker_pins_revision_and_disables_remote_code(monkeypatch):
    captured = {}

    class _Model:
        def __init__(self, name, **kwargs):
            captured.update(name=name, **kwargs)

    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        SimpleNamespace(CrossEncoder=_Model),
    )

    CrossEncoderReranker("organization/reranker", revision="a" * 40)

    assert captured == {
        "name": "organization/reranker",
        "trust_remote_code": False,
        "revision": "a" * 40,
    }


def test_cross_encoder_reranker_local_selector_avoids_remote_load(monkeypatch):
    captured = {}

    class _Model:
        def __init__(self, name, **kwargs):
            captured.update(name=name, **kwargs)

    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        SimpleNamespace(CrossEncoder=_Model),
    )

    CrossEncoderReranker("local:C:/models/reranker")

    assert captured == {
        "name": "C:/models/reranker",
        "trust_remote_code": False,
        "local_files_only": True,
    }


def test_reranker_strict_mode_rejects_mutable_remote_revision_before_load(monkeypatch):
    import engraphis.backends.reranker as reranker

    attempts = []
    monkeypatch.setattr(
        reranker,
        "CrossEncoderReranker",
        lambda *args, **kwargs: attempts.append((args, kwargs)),
    )

    with pytest.raises(ValueError, match="ENGRAPHIS_REQUIRE_IMMUTABLE_MODELS"):
        get_reranker(
            "organization/reranker",
            revision="main",
            require_immutable_models=True,
        )

    assert attempts == []


def test_reranker_fallback_logs_only_the_exception_class(monkeypatch, caplog):
    import engraphis.backends.reranker as reranker

    def unavailable(*args, **kwargs):
        raise RuntimeError("token=super-secret model=private/reranker")

    monkeypatch.setattr(reranker, "CrossEncoderReranker", unavailable)
    with caplog.at_level(logging.WARNING, logger="engraphis"):
        result = get_reranker("private/reranker")

    assert isinstance(result, IdentityReranker)
    assert "RuntimeError" in caplog.text
    assert "super-secret" not in caplog.text
    assert "private/reranker" not in caplog.text


def test_memory_service_forwards_model_provenance_to_the_engine(monkeypatch):
    import engraphis.service as service_module

    captured = {}

    class _Store:
        allowed_workspaces = None

    engine = SimpleNamespace(store=_Store())

    def create(_cls, db_path, **kwargs):
        captured.update(db_path=db_path, **kwargs)
        return engine

    monkeypatch.setattr(service_module.MemoryEngine, "create", classmethod(create))
    monkeypatch.setattr("engraphis.backends.encrypted_db.connector_from_env", lambda: None)

    MemoryService = service_module.MemoryService
    service = MemoryService.create(
        ":memory:",
        embed_model="organization/remote-model",
        embed_revision="a" * 40,
        require_immutable_models=True,
        rerank_model="organization/reranker",
        rerank_revision="b" * 40,
    )

    assert service.engine is engine
    assert captured["embed_model"] == "organization/remote-model"
    assert captured["embed_revision"] == "a" * 40
    assert captured["require_immutable_models"] is True
    assert captured["rerank_model"] == "organization/reranker"
    assert captured["rerank_revision"] == "b" * 40


def test_sentence_transformer_local_identity_changes_with_artifact_manifest(
    monkeypatch, tmp_path
):
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    weights = model_dir / "weights.bin"
    weights.write_bytes(b"weights-v1")

    class _Model:
        def __init__(self, _name, **_kwargs):
            pass

        def get_embedding_dimension(self):
            return 128

    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        SimpleNamespace(SentenceTransformer=_Model),
    )

    original = weights.stat()
    first = SentenceTransformerEmbedder(str(model_dir), local_files_only=True)
    weights.write_bytes(b"weights-v2")
    os.utime(
        weights,
        ns=(original.st_atime_ns, original.st_mtime_ns),
    )
    changed = SentenceTransformerEmbedder(str(model_dir), local_files_only=True)

    assert first.embedding_version
    assert first.embedding_version != changed.embedding_version


def test_sentence_transformer_identity_uses_loaded_artifact_not_mutable_selector():
    first = SentenceTransformerEmbedder.__new__(SentenceTransformerEmbedder)
    first.model_name = "Qwen/example"
    first._artifact_version = "hf-commit:" + "a" * 40
    same = SentenceTransformerEmbedder.__new__(SentenceTransformerEmbedder)
    same.model_name = "Qwen/example"
    same._artifact_version = "hf-commit:" + "a" * 40
    changed = SentenceTransformerEmbedder.__new__(SentenceTransformerEmbedder)
    changed.model_name = "Qwen/example"
    changed._artifact_version = "hf-commit:" + "b" * 40
    unversioned = SentenceTransformerEmbedder.__new__(SentenceTransformerEmbedder)
    unversioned.model_name = "Qwen/example"
    unversioned._artifact_version = ""

    assert first.embedding_identity == "sentence_transformers"
    assert first.embedding_version == same.embedding_version
    assert first.embedding_version != changed.embedding_version
    assert unversioned.embedding_version == ""


def test_sentence_transformer_embedding_failure_is_redacted():
    marker = "private-model-or-input-detail"

    class _Model:
        def encode(self, *_args, **_kwargs):
            raise RuntimeError(marker)

    embedder = SentenceTransformerEmbedder.__new__(SentenceTransformerEmbedder)
    embedder.model = _Model()
    embedder._dim = 2

    with pytest.raises(RuntimeError, match="returned malformed embeddings") as exc_info:
        embedder.embed(["secret input"])

    rendered = "".join(
        traceback.format_exception(
            type(exc_info.value),
            exc_info.value,
            exc_info.value.__traceback__,
        )
    )
    assert marker not in rendered


def test_embedder_factory_local_selector_requires_only_local_model_files(monkeypatch):
    """The local selector is a semantic-capable path that cannot fetch a model."""
    import engraphis.backends.embedder_st as embedder_st

    captured = {}

    class _LocalEmbedder:
        dim = 128
        supports_semantic_search = True
        embedding_mode = "semantic"

        def __init__(
            self,
            model_name,
            *,
            revision=None,
            local_files_only=False,
            require_immutable_models=None,
        ):
            captured.update(
                model_name=model_name,
                revision=revision,
                local_files_only=local_files_only,
            )

    monkeypatch.setattr(embedder_st, "SentenceTransformerEmbedder", _LocalEmbedder)
    result = get_embedder("local:C:/models/bge-small", 128, revision="b" * 40)

    assert isinstance(result, _LocalEmbedder)
    assert captured == {
        "model_name": "C:/models/bge-small",
        "revision": "b" * 40,
        "local_files_only": True,
    }


def test_missing_local_semantic_model_reports_lexical_degradation(monkeypatch):
    import engraphis.backends.embedder_st as embedder_st

    _force_load_failure(monkeypatch, embedder_st, "SentenceTransformerEmbedder")
    result = get_embedder("local:C:/models/missing", 128)

    assert isinstance(result, DeterministicEmbedder)
    assert result.supports_semantic_search is False
    assert "requested local semantic model is unavailable" in result.semantic_support_reason


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


def test_vector_index_avoids_sqlitevec_after_sqlcipher_load(monkeypatch):
    """A native-library conflict must degrade safely or fail clearly, never crash."""
    monkeypatch.setitem(sys.modules, "sqlcipher3", object())
    store = Store(":memory:")

    assert isinstance(get_vector_index(store, dim=128, prefer="auto"), NumpyVectorIndex)
    with pytest.raises(RuntimeError, match="cannot share a process with SQLCipher"):
        get_vector_index(store, dim=128, prefer="sqlite-vec")

    store.close()


@pytest.mark.parametrize("dimension", [True, 0, -1, 1.5, "384", 65_537])
def test_vector_index_rejects_an_invalid_ddl_dimension_before_backend_fallback(dimension):
    store = Store(":memory:")
    try:
        with pytest.raises(ValueError, match="embedding dimension"):
            get_vector_index(store, dim=dimension, prefer="auto")
    finally:
        store.close()


def test_reranker_factory_falls_back_offline(monkeypatch):
    import engraphis.backends.reranker as reranker

    assert isinstance(get_reranker(None), IdentityReranker)
    _force_load_failure(monkeypatch, reranker, "CrossEncoderReranker")
    assert isinstance(get_reranker("definitely-not-a-real-model-xyz"), IdentityReranker)
