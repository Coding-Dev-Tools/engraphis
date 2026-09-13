"""Factory performance diagnostics must never download or silently change backends."""
import json
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from engraphis import factory
from engraphis.backends import DeterministicEmbedder, NumpyVectorIndex
from engraphis.backends.embedder_st import _local_artifact_version
from engraphis.backends.reranker import IdentityReranker
from eval import performance
from eval.performance_engine import PerformanceEngineConfig, factory_benchmark_session


DATASET = [{
    "id": "auth",
    "memories": [{"tag": "token", "text": "The API uses PASETO v4 tokens."}],
    "questions": [{"q": "Which API token format?", "answer": "PASETO v4", "supporting": ["token"]}],
}]
REVISION = "a" * 40


class SemanticTestDouble(DeterministicEmbedder):
    supports_semantic_search = True
    embedding_mode = "semantic"

    def __init__(self, dim):
        super().__init__(dim)
        self.supports_semantic_search = True
        self.embedding_mode = "semantic"


class RerankerTestDouble(IdentityReranker):
    pass


def test_factory_disk_run_reopens_same_populated_database_and_cleans_up(tmp_path):
    config = PerformanceEngineConfig(storage_root=str(tmp_path))
    report = performance.run(DATASET, engine_config=config, warmups=0, iterations=1)

    assert report["schema"] == "engraphis-performance/v1"
    assert report["quality"]["recall_at_k"] == 1.0
    assert report["corpus"]["memories"] == 1
    assert report["environment"]["backend_configuration"]["mode"] == "factory"
    assert report["environment"]["sqlite"] == {"journal_mode": "wal", "synchronous": 2}
    assert report["phases"]["startup_samples"] == 1
    assert report["phases"]["populated_reopen_samples"] == 1
    assert report["phases"]["populated_reopen_ms"]["p50"] >= 0
    assert report["phases"]["queue_wait_ms"]["warm"]["min"] >= 0
    assert "not process startup" in report["phases"]["scope"]["cold"]
    stages = report["phases"]["recall_stages"]
    assert stages["diagnostics_enabled"] is True
    for temperature in ("cold", "warm"):
        assert stages[temperature]["timed_recalls"] == 1
        assert stages[temperature]["recalls_with_observed_timings"] == 1
        for phase in ("engine_recall", "embedding", "vector_search", "fusion_scoring", "reranking", "packing"):
            assert stages[temperature]["phase_ms"][phase]["sample_count"] == 1
            assert stages[temperature]["phase_ms"][phase]["min"] >= 0
    assert list(tmp_path.iterdir()) == []


def test_factory_memory_run_does_not_claim_populated_disk_reopen():
    report = performance.run(
        DATASET, engine_config=PerformanceEngineConfig(storage="memory"), warmups=0, iterations=1,
    )

    assert report["phases"]["populated_reopen_ms"] is None
    assert report["phases"]["populated_reopen_samples"] == 0


def test_serializable_config_runs_in_distinct_spawned_workers(tmp_path):
    config = PerformanceEngineConfig.from_dict(json.loads(json.dumps({
        "storage_root": str(tmp_path), "sqlite_durability": "balanced",
    })))
    report = performance.run(
        DATASET, engine_config=config, processes=2, concurrency=4, warmups=0, iterations=1,
    )

    assert report["acceptance"]["observed_processes"] == 2
    assert len({row["pid"] for row in report["resources"]["processes"]}) == 2
    assert report["phases"]["startup_samples"] == 2
    assert report["phases"]["populated_reopen_samples"] == 2
    assert report["quality"]["hit_at_k"] == 1.0
    assert report["environment"]["sqlite"]["synchronous"] == 1
    stages = report["phases"]["recall_stages"]
    assert stages["warm"]["timed_recalls"] == stages["warm"]["recalls_with_observed_timings"] == 2
    assert stages["warm"]["phase_ms"]["engine_recall"]["sample_count"] == 2
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("settings,message", [
    ({"vector_backend": "auto"}, "explicitly"),
    ({"storage": []}, "storage"),
    ({"sqlite_durability": "off"}, "sqlite_durability"),
    ({"embed_model": "org/model", "embed_revision": REVISION}, "local:"),
    ({"embed_model": "local:org/model"}, "pin the cached model"),
    ({"embed_model": "local:org/model", "embed_revision": "main"}, "40-character"),
    ({"rerank_model": "org/model", "rerank_revision": REVISION}, "local:"),
    ({"rerank_model": "local:https://example.com/model", "rerank_revision": REVISION}, "cached Hub"),
    ({"embed_revision": REVISION}, "model is required"),
    ({"allow_download": True}, "unknown engine config fields"),
])
def test_config_rejects_ambiguous_or_download_capable_settings(settings, message):
    with pytest.raises(ValueError, match=message):
        PerformanceEngineConfig.from_dict(settings)


def test_cached_model_factory_receives_local_only_pins_and_exact_policy(monkeypatch):
    calls = []

    def embedder(model, dim, **kwargs):
        calls.append(("embed", model, kwargs))
        return SemanticTestDouble(dim)

    def reranker(model, **kwargs):
        calls.append(("rerank", model, kwargs))
        return RerankerTestDouble()

    monkeypatch.setattr(factory, "get_embedder", embedder)
    monkeypatch.setattr(factory, "get_reranker", reranker)
    config = PerformanceEngineConfig(
        storage="memory", embed_model="local:org/embed", embed_revision=REVISION,
        rerank_model="local:org/rerank", rerank_revision=REVISION,
    )
    with factory_benchmark_session(config, dim=16) as session:
        assert session.provenance["embedder"] == {
            "source": "cached_hub", "model": "org/embed", "revision": REVISION,
        }
    assert calls == [
        ("embed", "local:org/embed", {
            "revision": REVISION, "require_immutable_models": True, "require_exact": True,
        }),
        ("rerank", "local:org/rerank", {
            "revision": REVISION, "require_immutable_models": True, "require_exact": True,
        }),
    ]


def test_local_directory_requires_content_pin_and_rechecks_after_load(tmp_path, monkeypatch):
    model = tmp_path / "model"
    model.mkdir()
    artifact = model / "config.json"
    artifact.write_text('{"version": 1}', encoding="utf-8")
    settings = {"storage": "memory", "embed_model": f"local:{model}", "embed_revision": REVISION}
    with pytest.raises(ValueError, match="must pin the local artifact bytes"):
        PerformanceEngineConfig.from_dict(settings)
    digest = _local_artifact_version(str(model)).removeprefix("local-content:")
    settings["embed_artifact_sha256"] = digest
    config = PerformanceEngineConfig.from_dict(settings)

    def changing_loader(model, dim, **kwargs):
        artifact.write_text('{"version": 2}', encoding="utf-8")
        return SemanticTestDouble(dim)

    monkeypatch.setattr(factory, "get_embedder", changing_loader)
    with pytest.raises(ValueError, match="artifact digest mismatch"):
        with factory_benchmark_session(config, dim=16):
            pytest.fail("mutated model bytes must not acquire benchmark provenance")


def test_exact_backend_fallback_is_rejected_and_temp_storage_is_cleaned(tmp_path, monkeypatch):
    monkeypatch.setattr(factory, "get_vector_index", lambda store, **kwargs: NumpyVectorIndex(store))
    config = PerformanceEngineConfig(storage_root=str(tmp_path), vector_backend="sqlite-vec")
    with pytest.raises(RuntimeError, match="vector backend resolved to a fallback"):
        performance.run(DATASET, engine_config=config, warmups=0, iterations=1)
    assert list(tmp_path.iterdir()) == []


def test_unavailable_pinned_model_fails_without_fallback(tmp_path, monkeypatch):
    def missing_model(*args, **kwargs):
        assert kwargs["require_exact"] is True
        raise RuntimeError("test model absent from local cache")

    monkeypatch.setattr(factory, "get_embedder", missing_model)
    config = PerformanceEngineConfig(
        storage_root=str(tmp_path), embed_model="local:org/missing", embed_revision=REVISION,
    )
    with pytest.raises(RuntimeError, match="absent from local cache"):
        performance.run(DATASET, engine_config=config, warmups=0, iterations=1)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("role", ["embed", "rerank"])
def test_model_fallback_cannot_be_reported_as_the_requested_model(role, monkeypatch):
    monkeypatch.setattr(factory, "get_embedder", lambda model, dim, **kwargs: (
        DeterministicEmbedder(dim) if role == "embed" else SemanticTestDouble(dim)
    ))
    monkeypatch.setattr(factory, "get_reranker", lambda model, **kwargs: IdentityReranker())
    config = PerformanceEngineConfig.from_dict({
        "storage": "memory", f"{role}_model": "local:org/model", f"{role}_revision": REVISION,
    })
    with pytest.raises(RuntimeError, match="resolved to a fallback"):
        with factory_benchmark_session(config, dim=16):
            pytest.fail("fallback must not be reported as the configured model")


def test_recall_failure_still_closes_disk_engine_before_directory_cleanup(tmp_path, monkeypatch):
    def failed_recall(*args, **kwargs):
        raise RuntimeError("test recall failure")

    monkeypatch.setattr(performance, "_measure_batch", failed_recall)
    with pytest.raises(RuntimeError, match="test recall failure"):
        performance.run(DATASET, engine_config=PerformanceEngineConfig(storage_root=str(tmp_path)))
    assert list(tmp_path.iterdir()) == []


def test_engine_config_reaches_each_acceptance_slice(monkeypatch):
    calls = []
    monkeypatch.setattr(performance, "_question_count", lambda dataset: 1000)
    monkeypatch.setattr(performance, "run", lambda dataset, **kwargs: calls.append(kwargs) or {})
    config = PerformanceEngineConfig(storage="memory")
    performance.run_acceptance_matrix([], engine_config=config)
    assert len(calls) == 3
    assert all(call["engine_config"] == config for call in calls)


def test_engine_config_cli_is_opt_in_and_parsed_before_running(tmp_path, monkeypatch, capsys):
    path = tmp_path / "engine.json"
    path.write_text(json.dumps({"storage": "memory"}), encoding="utf-8")
    calls = []
    monkeypatch.setattr(performance, "run", lambda dataset, **kwargs: calls.append(kwargs) or {})
    assert performance.main(["--engine-config", str(path), "--json"]) == 0
    assert asdict(calls[0]["engine_config"])["storage"] == "memory"
    assert json.loads(capsys.readouterr().out) == {}


def test_queue_wait_is_measured_separately_from_engine_execution(monkeypatch):
    clock = iter([110_000, 160_000])
    monkeypatch.setattr(performance.time, "perf_counter_ns", lambda: next(clock))
    engine = SimpleNamespace(recall_engine=SimpleNamespace(recall=lambda *args, **kwargs: "result"))
    result, execution_ms, queue_ms = performance._measure_recall(
        engine, {"q": "query"}, None, k=1, candidate_k=1, candidate_depth="fixed",
        token_budget=32, retrieval_profile="balanced", submitted_ns=10_000,
    )
    assert result == "result"
    assert execution_ms == 0.05
    assert queue_ms == 0.1


def test_fixture_mode_keeps_stage_diagnostics_disabled(monkeypatch):
    from engraphis.core.recall import RecallEngine

    original = RecallEngine.recall
    requested = []

    def observed(self, *args, **kwargs):
        requested.append(kwargs.get("diagnostics", False))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(RecallEngine, "recall", observed)
    report = performance.run(DATASET, warmups=0, iterations=1)
    assert requested == [False, False]
    assert "recall_stages" not in report["phases"]


def test_stage_capture_copies_only_known_finite_numeric_observations():
    result = SimpleNamespace(diagnostics_v1={
        "phase_ms": {"engine_recall": 1, "packing": 0, "embedding": float("nan"),
                     "planning": True, "vector_search": -1, "reranking": float("inf"),
                     "raw_source_text": 7},
        "retrieval_trace": ["private source text"],
    })
    assert performance._observed_recall_stages(result) == {"engine_recall": 1, "packing": 0}
    for diagnostics in (None, [], {"phase_ms": "private source text"}):
        assert performance._observed_recall_stages(SimpleNamespace(diagnostics_v1=diagnostics)) == {}


def test_stage_summary_records_partial_coverage_without_inventing_zero_timings():
    measurements = performance._Measurements([], [], [], [], [], [], [], [])
    measurements.cold_stage_ms = [{}]
    measurements.warm_stage_ms = [{"engine_recall": 1, "packing": 0}, {}]
    report = performance._recall_stage_report([measurements])
    assert report["cold"] == {"timed_recalls": 1, "recalls_with_observed_timings": 0, "phase_ms": {}}
    assert report["warm"]["timed_recalls"] == 2
    assert report["warm"]["recalls_with_observed_timings"] == 1
    assert report["warm"]["phase_ms"]["engine_recall"]["min"] == 1
    assert report["warm"]["phase_ms"]["packing"]["sample_count"] == 1
    assert "embedding" not in report["warm"]["phase_ms"]
