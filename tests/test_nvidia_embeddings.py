"""Unit tests for optional CUDA device selection and NVIDIA NIM embeddings preset.

All HTTP and PyTorch interactions are mocked — no network access or GPU required.
"""
from __future__ import annotations

import logging
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from engraphis.backends.embedder_api import (
    NVIDIA_NIM_DEFAULT_BASE_URL,
    NVIDIA_NIM_DEFAULT_MODEL,
    ApiEmbedder,
    create_nvidia_nim_embedder,
)
from engraphis.backends.embedder_deterministic import DeterministicEmbedder
from engraphis.backends.embedder_st import (
    SentenceTransformerEmbedder,
    get_embedder,
    resolve_embed_device,
)
from engraphis.config import Settings, _default_embed_model
from engraphis.core.engine import MemoryEngine
from engraphis.core.grounded import support_scores
from engraphis.core.interfaces import (
    MemoryRecord,
    Scope,
    embed_texts,
    embedding_space_fingerprint,
)
from engraphis.core.secrets import SecretDetectedError
from engraphis.core.store import Store
from engraphis.factory import backend_health, create_memory_engine


def _fake_torch(*, available: bool, device_count: int = 1):
    return SimpleNamespace(
        cuda=SimpleNamespace(
            is_available=lambda: available,
            device_count=lambda: device_count,
        )
    )


def _mock_httpx_client(monkeypatch, *, dim: int = 4, fail_batch: bool = False):
    httpx = pytest.importorskip("httpx")
    recorded: list[dict] = []

    class _Response:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class _Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, url, *, headers=None, json=None):
            recorded.append({"url": url, "headers": dict(headers or {}), "json": dict(json or {})})
            inputs = list((json or {}).get("input") or [])
            if fail_batch and len(inputs) > 1:
                raise RuntimeError("simulated batch failure")
            input_type = (json or {}).get("input_type")
            data = []
            for idx, text in enumerate(inputs):
                base = [0.0] * dim
                seed = sum(ord(ch) for ch in str(text))
                base[seed % dim] = 1.0
                base[(seed + (1 if input_type == "query" else 2)) % dim] += 0.25
                data.append({"index": idx, "embedding": base})
            return _Response({"data": data})

    monkeypatch.setattr(httpx, "Client", _Client)
    return recorded


# ── Optional CUDA device selection ───────────────────────────────────────────


def test_default_embed_device_is_unset_and_preserves_offline_behavior(monkeypatch):
    monkeypatch.delenv("ENGRAPHIS_EMBED_DEVICE", raising=False)
    monkeypatch.delenv("ENGRAPHIS_EMBED_PROVIDER", raising=False)
    monkeypatch.delenv("ENGRAPHIS_EMBED_MODEL", raising=False)

    assert resolve_embed_device() is None
    assert isinstance(get_embedder(None, 128), DeterministicEmbedder)

    cfg = Settings()
    assert cfg.embed_device == ""
    assert cfg.embed_provider == ""
    assert cfg.embed_model == "sentence-transformers/all-MiniLM-L6-v2"


def test_resolve_embed_device_auto_uses_cuda_when_available_else_cpu(monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", _fake_torch(available=True))
    assert resolve_embed_device("auto") == "cuda"

    monkeypatch.setitem(sys.modules, "torch", _fake_torch(available=False))
    assert resolve_embed_device("auto") == "cpu"

    # Simulate an offline/CPU-only install where torch is not importable.
    monkeypatch.setitem(sys.modules, "torch", None)
    assert resolve_embed_device("auto") == "cpu"
    assert resolve_embed_device("cpu") == "cpu"


def test_resolve_embed_device_cuda_index_and_fallback_behavior(monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, "torch", _fake_torch(available=True, device_count=2))
    assert resolve_embed_device("cuda") == "cuda"
    assert resolve_embed_device("cuda:1") == "cuda:1"

    with caplog.at_level(logging.WARNING, logger="engraphis"):
        assert resolve_embed_device("cuda:2", require_exact=False) == "cpu"
    assert "falling back to CPU" in caplog.text

    with pytest.raises(RuntimeError, match="no compatible CUDA GPU"):
        resolve_embed_device("cuda:2", require_exact=True)

    monkeypatch.setitem(sys.modules, "torch", _fake_torch(available=False, device_count=0))
    assert resolve_embed_device("cuda", require_exact=False) == "cpu"
    with pytest.raises(RuntimeError, match="ENGRAPHIS_EMBED_DEVICE=cpu or auto"):
        resolve_embed_device("cuda", require_exact=True)


@pytest.mark.parametrize("bad_device", ["tpu", "mps", "cuda:abc", "cuda:-1"])
def test_resolve_embed_device_rejects_invalid_selector(bad_device, caplog):
    with pytest.raises(ValueError, match="ENGRAPHIS_EMBED_DEVICE"):
        resolve_embed_device(bad_device, require_exact=True)
    with caplog.at_level(logging.WARNING, logger="engraphis"):
        assert resolve_embed_device(bad_device, require_exact=False) is None
    assert "ENGRAPHIS_EMBED_DEVICE" in caplog.text


def test_sentence_transformer_passes_resolved_cuda_device_only_when_configured(monkeypatch):
    calls: list[tuple[str, dict]] = []

    class _FakeST:
        def __init__(self, model_name, **kwargs):
            calls.append((model_name, kwargs))

        def get_embedding_dimension(self):
            return 384

        def encode(self, texts, **_kwargs):
            return np.ones((len(texts), 384), dtype=np.float32)

    monkeypatch.setitem(
        sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=_FakeST)
    )
    monkeypatch.setitem(sys.modules, "torch", _fake_torch(available=True))
    monkeypatch.delenv("ENGRAPHIS_EMBED_DEVICE", raising=False)

    # Default: no device kwarg passed to SentenceTransformer.
    default_emb = get_embedder("sentence-transformers/all-MiniLM-L6-v2", 384)
    assert isinstance(default_emb, SentenceTransformerEmbedder)
    assert default_emb.device is None
    assert "device" not in calls[-1][1]

    # Opt-in via ENGRAPHIS_EMBED_DEVICE=auto -> resolves to cuda when torch reports CUDA.
    monkeypatch.setenv("ENGRAPHIS_EMBED_DEVICE", "auto")
    cuda_emb = get_embedder("sentence-transformers/all-MiniLM-L6-v2", 384)
    assert isinstance(cuda_emb, SentenceTransformerEmbedder)
    assert cuda_emb.device == "cuda"
    assert calls[-1][1]["device"] == "cuda"

    # Explicit engine factory forwarding + backend_health visibility.
    engine = create_memory_engine(
        ":memory:",
        embed_model="sentence-transformers/all-MiniLM-L6-v2",
        embed_device="cuda",
    )
    try:
        health = backend_health(engine)
        assert health["embedder"]["device"] == "cuda"
    finally:
        engine.close()


# ── NVIDIA NIM preset & input_type handling ──────────────────────────────────


def test_nvidia_nim_preset_defaults_and_input_type_payloads(monkeypatch):
    monkeypatch.delenv("ENGRAPHIS_EMBED_BASE_URL", raising=False)
    monkeypatch.delenv("NVIDIA_NIM_BASE_URL", raising=False)
    monkeypatch.delenv("ENGRAPHIS_EMBED_API_KEY", raising=False)
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test-key-123")
    recorded = _mock_httpx_client(monkeypatch, dim=4)

    emb = create_nvidia_nim_embedder(space_version="nim-v1")
    assert emb.model == NVIDIA_NIM_DEFAULT_MODEL
    assert emb._embeddings_url == f"{NVIDIA_NIM_DEFAULT_BASE_URL}/embeddings"
    assert emb.embedding_identity == "nvidia_nim"
    assert emb.supports_input_type is True
    assert emb.is_remote is True

    passage_vecs = embed_texts(emb, ["store this passage"], input_type="passage")
    query_vecs = embed_texts(emb, ["find this passage"], input_type="query")

    assert passage_vecs.shape == (1, 4)
    assert query_vecs.shape == (1, 4)
    assert recorded[0]["headers"]["Authorization"] == "Bearer nvapi-test-key-123"
    assert recorded[0]["json"]["model"] == "nvidia/nv-embedqa-e5-v5"
    assert recorded[0]["json"]["input_type"] == "passage"
    assert recorded[0]["json"]["encoding_format"] == "float"
    assert recorded[0]["json"]["truncate"] == "END"
    assert recorded[1]["json"]["input_type"] == "query"


def test_nvidia_nim_per_item_fallback_preserves_input_type(monkeypatch):
    recorded = _mock_httpx_client(monkeypatch, dim=4, fail_batch=True)
    emb = create_nvidia_nim_embedder(api_key="nvapi-test-key")

    vecs = emb.embed(["first query", "second query"], input_type="query")

    assert vecs.shape == (2, 4)
    assert len(recorded) == 3  # 1 failed batch + 2 single-item fallbacks
    assert all(call["json"]["input_type"] == "query" for call in recorded)


def test_nvidia_nim_self_hosted_loopback_allows_no_api_key_and_is_local(monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.delenv("ENGRAPHIS_EMBED_API_KEY", raising=False)
    monkeypatch.setenv("NVIDIA_NIM_BASE_URL", "http://127.0.0.1:8000/v1")
    recorded = _mock_httpx_client(monkeypatch, dim=4)

    emb = get_embedder("nvidia-nim:nvidia/llama-3.2-nv-embedqa-1b-v2")
    assert isinstance(emb, ApiEmbedder)
    assert emb.model == "nvidia/llama-3.2-nv-embedqa-1b-v2"
    assert emb._embeddings_url == "http://127.0.0.1:8000/v1/embeddings"
    assert emb.is_remote is False

    vecs = emb.embed(["self-hosted passage"])
    assert vecs.shape == (1, 4)
    assert "Authorization" not in recorded[0]["headers"]
    assert recorded[0]["json"]["input_type"] == "passage"


def test_nvidia_nim_rejects_non_loopback_http_and_handles_missing_key(monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.delenv("ENGRAPHIS_EMBED_API_KEY", raising=False)
    monkeypatch.delenv("ENGRAPHIS_EMBED_BASE_URL", raising=False)
    monkeypatch.delenv("NVIDIA_NIM_BASE_URL", raising=False)

    with pytest.raises(ValueError, match="HTTPS"):
        get_embedder("nvidia-nim", base_url="http://nim.example.com/v1")

    # Missing key with require_exact=False degrades gracefully to deterministic mode.
    fallback = get_embedder("nvidia-nim", dim=128, require_exact=False)
    assert isinstance(fallback, DeterministicEmbedder)
    assert fallback.supports_semantic_search is False
    assert "NVIDIA NIM" in fallback.semantic_support_reason

    # Missing key with require_exact=True surfaces an actionable error without secrets.
    with pytest.raises(RuntimeError, match="NVIDIA_API_KEY"):
        get_embedder("nvidia-nim", dim=128, require_exact=True)


def test_nvidia_nim_provider_env_sets_default_model_and_routes_factory(monkeypatch):
    monkeypatch.delenv("ENGRAPHIS_EMBED_MODEL", raising=False)
    monkeypatch.setenv("ENGRAPHIS_EMBED_PROVIDER", "nvidia-nim")
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-env-key")

    cfg = Settings()
    assert cfg.embed_provider == "nvidia-nim"
    assert cfg.embed_model == "nvidia/nv-embedqa-e5-v5"

    emb = get_embedder(cfg.embed_model, revision="nim-space-v1")
    assert isinstance(emb, ApiEmbedder)
    assert emb.embedding_identity == "nvidia_nim"
    assert emb.model == "nvidia/nv-embedqa-e5-v5"


def test_nvidia_nim_strict_immutable_policy_requires_space_version(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test-key")
    with pytest.raises(ValueError, match="ENGRAPHIS_EMBED_REVISION"):
        get_embedder("nvidia-nim", require_immutable_models=True)

    emb = get_embedder("nvidia-nim", revision="nim-2026-01", require_immutable_models=True)
    assert isinstance(emb, ApiEmbedder)


def test_nvidia_nim_failure_redacts_api_key_and_endpoint(monkeypatch, caplog):
    httpx = pytest.importorskip("httpx")
    secret_key = "nvapi-super-secret-value-999"

    class _FailingClient:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, *_args, **_kwargs):
            raise RuntimeError(f"failed with {secret_key}")

    monkeypatch.setattr(httpx, "Client", _FailingClient)
    emb = create_nvidia_nim_embedder(
        base_url="https://nim-private.example/tenant-secret/v1",
        api_key=secret_key,
        dim=4,
    )
    with caplog.at_level(logging.INFO, logger="engraphis.embedder_api"):
        with pytest.raises(RuntimeError) as caught:
            emb.embed(["hello"])

    assert secret_key not in caplog.text
    assert secret_key not in str(caught.value)
    assert "tenant-secret" not in caplog.text
    assert "tenant-secret" not in str(caught.value)


# ── Engine integration: query/passage split, dim switch, and secret guards ───


def test_nvidia_nim_engine_uses_passage_for_writes_and_query_for_recall(monkeypatch):
    recorded = _mock_httpx_client(monkeypatch, dim=4)
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test-key")

    engine = MemoryEngine.create(
        ":memory:",
        embed_model="nvidia-nim:nvidia/nv-embedqa-e5-v5",
        embed_revision="nim-v1",
    )
    try:
        wid = engine.store.get_or_create_workspace("acme")
        engine.remember(
            "Deployment pipeline runs nightly integration checks on GPU workers.",
            workspace_id=wid,
            scope=Scope.WORKSPACE,
            title="GPU CI",
        )
        recorded_after_write = [call["json"]["input_type"] for call in recorded]
        assert recorded_after_write[-1] == "passage"

        recall_start = len(recorded)
        result = engine.recall(
            "nightly GPU workers",
            workspace_id=wid,
            scopes=[Scope.WORKSPACE],
            k=3,
        )
        assert result.chunks
        recall_types = [call["json"]["input_type"] for call in recorded[recall_start:]]
        assert recall_types == ["query"]

        # Grounded support scoring also separates query from passage inputs.
        support_start = len(recorded)
        scores = support_scores(
            "nightly GPU workers",
            ["Deployment pipeline runs nightly integration checks on GPU workers."],
            engine.embedder,
        )
        assert len(scores) == 1
        support_types = [call["json"]["input_type"] for call in recorded[support_start:]]
        assert support_types == ["query", "passage"]
    finally:
        engine.close()


def test_nvidia_nim_dimension_switch_rebuilds_store_without_mixing_dimensions(
    monkeypatch, tmp_path
):
    db_path = str(tmp_path / "nim-switch.db")
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test-key")
    monkeypatch.delenv("ENGRAPHIS_EMBED_DIM", raising=False)

    # 1. Start with default offline deterministic embedder (384-dim).
    offline = MemoryEngine.create(db_path)
    try:
        wid = offline.store.get_or_create_workspace("acme")
        mid = offline.remember(
            "Use PostgreSQL connection pooling for analytics jobs.",
            workspace_id=wid,
            scope=Scope.WORKSPACE,
        )
        initial_space = offline.store.active_embedding_space()
        row = offline.store.conn.execute(
            "SELECT dim FROM mem_vectors WHERE id=?", (mid,)
        ).fetchone()
        assert row is not None
        assert row["dim"] == 384
    finally:
        offline.close()

    # 2. Switch to versioned NVIDIA NIM (mocked 8-dim): store rebuilds cleanly.
    _mock_httpx_client(monkeypatch, dim=8)
    nim_engine = MemoryEngine.create(
        db_path,
        embed_model="nvidia-nim:nvidia/nv-embedqa-e5-v5",
        embed_revision="nim-e5-v5-rev1",
    )
    try:
        new_space = nim_engine.store.active_embedding_space()
        assert new_space == embedding_space_fingerprint(nim_engine.embedder)
        assert new_space != initial_space
        dims = {
            int(r["dim"])
            for r in nim_engine.store.conn.execute("SELECT dim FROM mem_vectors").fetchall()
        }
        assert dims == {8}
    finally:
        nim_engine.close()

    # 3. Unversioned API embedder on a persistent store fails closed on write.
    unversioned = MemoryEngine.create(
        db_path,
        embed_model="nvidia-nim:nvidia/nv-embedqa-e5-v5",
    )
    try:
        assert unversioned.embedding_space == ""
        with pytest.raises(RuntimeError, match="durable embedding_identity and embedding_version"):
            unversioned.remember(
                "Another note without a space version.",
                workspace_id=wid,
                scope=Scope.WORKSPACE,
            )
    finally:
        unversioned.close()


def test_nvidia_nim_configured_dim_mismatch_raises_error(monkeypatch):
    _mock_httpx_client(monkeypatch, dim=6)
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test-key")
    monkeypatch.setenv("ENGRAPHIS_EMBED_DIM", "4")

    emb = get_embedder("nvidia-nim", revision="nim-v1")
    with pytest.raises(RuntimeError, match="unexpected dimension"):
        emb.embed(["mismatch check"])


def test_remote_nvidia_nim_respects_secret_memory_and_credential_guards(monkeypatch, tmp_path):
    db_path = str(tmp_path / "nim-secrets.db")
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test-key")
    monkeypatch.delenv("ENGRAPHIS_EMBED_DIM", raising=False)

    # Seed one normal memory and one sensitivity='secret' memory locally.
    store = Store(db_path)
    try:
        wid = store.get_or_create_workspace("acme")
        normal_id = store.add_memory(MemoryRecord(
            id="",
            content="Public architecture note about event queues.",
            workspace_id=wid,
            scope=Scope.WORKSPACE,
            sensitivity="normal",
        ))
        secret_id = store.add_memory(MemoryRecord(
            id="",
            content="TOP_SECRET_LOCAL_ONLY_PAYLOAD_XYZ",
            workspace_id=wid,
            scope=Scope.WORKSPACE,
            sensitivity="secret",
        ))
    finally:
        store.close()

    recorded = _mock_httpx_client(monkeypatch, dim=4)
    engine = MemoryEngine.create(
        db_path,
        embed_model="nvidia-nim:nvidia/nv-embedqa-e5-v5",
        embed_revision="nim-secret-test-v1",
    )
    try:
        # Rebuild must embed the normal memory and NEVER send the secret memory remotely.
        sent_texts = [t for call in recorded for t in call["json"].get("input", [])]
        assert any("event queues" in t for t in sent_texts)
        assert not any("TOP_SECRET_LOCAL_ONLY_PAYLOAD_XYZ" in t for t in sent_texts)
        assert engine.store.conn.execute(
            "SELECT 1 FROM mem_vectors WHERE id=?", (normal_id,)
        ).fetchone() is not None
        assert engine.store.conn.execute(
            "SELECT 1 FROM mem_vectors WHERE id=?", (secret_id,)
        ).fetchone() is None

        # Writing a memory tagged sensitivity=secret in metadata must skip remote embedding.
        before = len(recorded)
        engine.remember(
            "SECOND_LOCAL_SECRET_MEMORY_ABC",
            workspace_id=wid,
            scope=Scope.WORKSPACE,
            metadata={"sensitivity": "secret"},
        )
        assert len(recorded) == before

        # Credential-shaped payloads must be rejected BEFORE any remote NIM call.
        synthetic_aws = "AWS_SECRET_ACCESS_KEY=" + "AKIA" + "IOSFODNN7EXAMPLE"
        with pytest.raises(SecretDetectedError):
            engine.remember(
                synthetic_aws,
                workspace_id=wid,
                scope=Scope.WORKSPACE,
            )
        assert len(recorded) == before
    finally:
        engine.close()


def test_nvidia_nim_endpoint_outage_during_recall_degrades_to_lexical(monkeypatch):
    httpx = pytest.importorskip("httpx")
    _mock_httpx_client(monkeypatch, dim=4)
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test-key")

    engine = MemoryEngine.create(
        ":memory:",
        embed_model="nvidia-nim:nvidia/nv-embedqa-e5-v5",
        embed_revision="nim-v1",
    )
    try:
        wid = engine.store.get_or_create_workspace("acme")
        engine.remember(
            "Circuit breaker protects checkout latency during upstream outages.",
            workspace_id=wid,
            scope=Scope.WORKSPACE,
            title="Circuit breaker",
        )

        class _OutageClient:
            def __init__(self, **_kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def post(self, *_args, **_kwargs):
                raise RuntimeError("connection refused")

        monkeypatch.setattr(httpx, "Client", _OutageClient)
        res = engine.recall(
            "checkout latency circuit breaker",
            workspace_id=wid,
            scopes=[Scope.WORKSPACE],
            k=3,
        )
        assert res.chunks
        assert res.degraded_mode is True
        assert res.vector_search_ready is False
    finally:
        engine.close()


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://localhost.evil.com/v1",
        "http://127.0.0.1.nip.io/v1",
        "http://0.0.0.0:8000/v1",
        "https://user:pass@integrate.api.nvidia.com/v1",
        "https://integrate.api.nvidia.com/v1?x=1",
        "https://integrate.api.nvidia.com/v1#frag",
        "https://integrate.api.nvidia.com/v1\x00",
        "https://integrate.api.nvidia.com/v1\x1b",
    ],
)
def test_nvidia_nim_rejects_ssrf_and_control_char_urls(monkeypatch, bad_url):
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test-key")
    with pytest.raises(ValueError):
        create_nvidia_nim_embedder(base_url=bad_url)


def test_nvidia_nim_ipv6_loopback_and_whitespace_url_and_blank_model(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test-key")
    monkeypatch.setenv("ENGRAPHIS_EMBED_PROVIDER", "nvidia-nim")
    monkeypatch.setenv("ENGRAPHIS_EMBED_MODEL", "   ")
    monkeypatch.setenv("ENGRAPHIS_EMBED_BASE_URL", "   ")
    assert _default_embed_model() == NVIDIA_NIM_DEFAULT_MODEL
    emb_v6 = create_nvidia_nim_embedder(base_url="http://[::1]:8000/v1", api_key="")
    assert emb_v6.is_remote is False
    emb_ws = create_nvidia_nim_embedder(base_url="   ")
    assert emb_ws._embeddings_url == f"{NVIDIA_NIM_DEFAULT_BASE_URL}/embeddings"


def test_nvidia_nim_single_item_failure_does_not_retry_and_startup_outage_degrades(monkeypatch):
    httpx = pytest.importorskip("httpx")
    calls = 0

    class _FailClient:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            raise RuntimeError("unreachable")

    monkeypatch.setattr(httpx, "Client", _FailClient)
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test-key")
    emb = create_nvidia_nim_embedder(dim=4)
    with pytest.raises(RuntimeError):
        emb.embed(["only one item"])
    assert calls == 1

    engine = MemoryEngine.create(
        ":memory:",
        embed_model="nvidia-nim:nvidia/nv-embedqa-e5-v5",
        embed_revision="nim-outage-v1",
        require_exact_backends=False,
    )
    try:
        assert engine.embedder.supports_semantic_search is False
    finally:
        engine.close()

    with pytest.raises(RuntimeError):
        MemoryEngine.create(
            ":memory:",
            embed_model="nvidia-nim:nvidia/nv-embedqa-e5-v5",
            embed_revision="nim-outage-v1",
            require_exact_backends=True,
        )


def test_remote_nvidia_nim_never_exfiltrates_secret_memories_on_grounded_adaptive_sync_or_update(
    monkeypatch, tmp_path
):
    from engraphis.core.interfaces import MemoryRecord, MemoryType
    from engraphis.core.sync import SyncEngine
    from engraphis.service import MemoryService

    db_path = str(tmp_path / "secret_guards.db")
    secret_marker = "ULTRA_CONFIDENTIAL_MEMORY_TOKEN_98765"

    # 1. Local write with metadata={"sensitivity": "secret"} must persist column sensitivity="secret".
    local_svc = MemoryService.create(db_path, embed_model=None)
    try:
        wid = local_svc.store.get_or_create_workspace("acme")
        created_id = local_svc.engine.remember(
            f"Vault rotation note {secret_marker} for quarterly audit.",
            workspace_id=wid,
            scope=Scope.WORKSPACE,
            title="Vault note",
            metadata={"sensitivity": "secret"},
        )
        stored = local_svc.store.get_memory(created_id)
        assert stored is not None and stored.sensitivity == "secret"
    finally:
        local_svc.close()

    # 2. Switch to remote NIM: update_memory, grounded_recall, adaptive_context, and SyncEngine._write
    # must never send the secret text remotely.
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test-key")
    recorded = _mock_httpx_client(monkeypatch, dim=4)
    svc = MemoryService.create(
        db_path,
        embed_model="nvidia-nim:nvidia/nv-embedqa-e5-v5",
        embed_revision="nim-secret-guard-v1",
    )
    try:
        svc.update_memory(created_id, workspace="acme", title="Renamed vault note")
        grounded = svc.engine.grounded_recall(
            "quarterly audit vault rotation",
            workspace_id=wid,
            scopes=[Scope.WORKSPACE],
        )
        assert grounded.abstained is False and secret_marker in grounded.answer
        svc.engine.adaptive_context(
            "quarterly audit vault rotation",
            history="Recent turn mentioning quarterly audit.",
            workspace_id=wid,
            scopes=[Scope.WORKSPACE],
        )
        sync_engine = SyncEngine(svc.store, embedder=svc.engine.embedder)
        sync_rec = MemoryRecord(
            id="mem_sync_secret_test",
            content=f"Synced secret payload {secret_marker}",
            mtype=MemoryType.SEMANTIC,
            scope=Scope.WORKSPACE,
            workspace_id=wid,
            sensitivity="secret",
        )
        sync_engine._write(sync_rec)
        assert sync_rec.embedding is None

        sent_texts = [t for call in recorded for t in call["json"].get("input", [])]
        assert not any(secret_marker in text for text in sent_texts)
    finally:
        svc.close()


def test_remote_nvidia_nim_never_exfiltrates_secret_memories_on_why_correct_approve_promote_merge_or_legacy_update(
    monkeypatch, tmp_path
):
    from engraphis.service import MemoryService

    db_path = str(tmp_path / "secret_lifecycle.db")
    secret_marker = "LIFECYCLE_SECRET_SENTINEL_44321"
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test-key")
    recorded = _mock_httpx_client(monkeypatch, dim=4)
    svc = MemoryService.create(
        db_path,
        embed_model="nvidia-nim:nvidia/nv-embedqa-e5-v5",
        embed_revision="nim-lifecycle-v1",
    )
    try:
        wid = svc.store.get_or_create_workspace("acme")
        rid = svc.store.get_or_create_repo(wid, "api")
        m1 = svc.engine.remember(
            f"Runbook secret {secret_marker} rotation step one.",
            workspace_id=wid,
            repo_id=rid,
            scope=Scope.REPO,
            title="Runbook step one",
            metadata={"sensitivity": "secret"},
        )
        m2 = svc.engine.remember(
            f"Runbook secret {secret_marker} rotation step two.",
            workspace_id=wid,
            repo_id=rid,
            scope=Scope.REPO,
            title="Runbook step two",
            metadata={"sensitivity": "secret"},
        )
        # Simulate pre-PR legacy row with sensitivity="normal" column + metadata sensitivity="secret".
        svc.store.conn.execute("UPDATE memories SET sensitivity='normal' WHERE id=?", (m1,))
        svc.store.conn.commit()
        svc.update_memory(m1, workspace="acme", title="Renamed legacy secret")

        why_res = svc.engine.why("runbook rotation step", workspace_id=wid, repo_id=rid)
        assert why_res["answer"]

        corrected = svc.engine.correct(
            m1,
            f"Corrected secret {secret_marker} rotation step one.",
            reason="update step",
        )
        promoted = svc.engine.promote(
            corrected["id"],
            Scope.WORKSPACE,
            reason="share across workspace",
        )
        svc.engine.merge(
            [promoted["id"], m2],
            f"Merged secret {secret_marker} rotation runbook.",
            reason="combine steps",
        )

        sent_texts = [t for call in recorded for t in call["json"].get("input", [])]
        assert not any(secret_marker in text for text in sent_texts)
    finally:
        svc.close()
