import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from engraphis.core.interfaces import ContextUsage, PackedChunk
from eval import performance
from eval.performance import AcceptanceConfig, main, run


DATASET = [
    {
        "id": "auth",
        "memories": [
            {"tag": "token", "text": "The API authenticates with PASETO v4 tokens."},
            {"tag": "deploy", "text": "Deployments run through GitHub Actions."},
        ],
        "questions": [
            {
                "q": "Which token format authenticates the API?",
                "answer": "PASETO v4",
                "supporting": ["token"],
            }
        ],
    }
]


def test_performance_report_covers_quality_context_and_latency():
    report = run(DATASET, k=2, warmups=0, iterations=2, filler_memories=3)

    assert report["schema"] == "engraphis-performance/v1"
    assert report["corpus"] == {
        "dataset_cases": 1,
        "memories": 5,
        "questions": 1,
        "filler_memories": 3,
    }
    assert report["run"]["timed_recalls"] == 2
    assert report["run"]["candidate_k"] == 50
    assert report["run"]["candidate_depth"] == "fixed"
    assert report["run"]["actual_candidate_k"] == {"min": 50, "max": 50, "mean": 50.0}
    assert report["run"]["retrieval_profile"] == "balanced"
    assert report["quality"]["hit_at_k"] == 1.0
    assert report["context"]["mean_tokens"] > 0
    assert report["context"]["token_counter"] == "engraphis.regex.v1"
    assert -1 <= report["context"]["median_serialized_payload_savings_ratio"] <= 1
    assert report["context"]["saved_serialized_payload_tokens"] == (
        report["context"]["full_serialized_payload_tokens"]
        - report["context"]["compact_serialized_payload_tokens"]
    )
    assert 0 <= report["latency_ms"]["min"] <= report["latency_ms"]["p50"]
    assert report["latency_ms"]["p50"] <= report["latency_ms"]["p95"]
    assert report["latency_ms"]["p95"] <= report["latency_ms"]["max"]


def test_performance_report_records_normalized_candidate_depth_and_profile():
    report = run(
        DATASET,
        k=2,
        candidate_k=9,
        retrieval_profile=" GRAPH ",
        warmups=0,
        iterations=1,
    )

    assert report["run"]["k"] == 2
    assert report["run"]["candidate_k"] == 9
    assert report["run"]["retrieval_profile"] == "graph"


def test_performance_report_records_adaptive_candidate_depth_used():
    report = run(
        DATASET,
        k=2,
        candidate_k=50,
        candidate_depth="adaptive",
        warmups=0,
        iterations=1,
    )

    assert report["run"]["candidate_depth"] == "adaptive"
    assert report["run"]["actual_candidate_k"] == {"min": 12, "max": 12, "mean": 12.0}


def test_performance_run_rejects_unknown_candidate_depth():
    with pytest.raises(ValueError, match="candidate_depth"):
        run(DATASET, candidate_depth="unbounded", warmups=0, iterations=1)


def test_performance_run_rejects_unknown_retrieval_profile():
    with pytest.raises(ValueError, match="retrieval_profile"):
        run(DATASET, retrieval_profile="unsupported", warmups=0, iterations=1)


def test_codemem_median_compact_payload_savings_clears_release_gate():
    dataset = performance.load_dataset(
        Path(__file__).resolve().parents[1] / "eval" / "datasets" / "codemem.jsonl"
    )

    report = run(dataset, k=5, warmups=0, iterations=1)

    assert report["context"]["median_serialized_payload_savings_ratio"] >= 0.5


def test_performance_report_separates_cold_warm_and_acceptance_shape():
    report = run(DATASET, k=2, warmups=0, iterations=1, concurrency=4)

    assert report["acceptance"] == {
        "concurrency": 4,
        "independent_processes": 1,
        "minimum_queries": 0,
        "canonical": False,
        "query_count": 1,
        "valid": True,
    }
    assert report["run"]["cold_timed_recalls"] == 1
    assert report["run"]["warm_timed_recalls"] == 1
    assert report["latency_ms"]["cold"]["p50"] >= 0
    assert report["latency_ms"]["warm"]["p99"] >= report["latency_ms"]["warm"]["p50"]
    assert report["resources"]["processes"][0]["storage_bytes"] is not None


def test_compact_payload_mirrors_packed_mcp_sources_in_ordinal_order():
    result = SimpleNamespace(
        context="[1] second\n[2] first",
        chunks=[
            {"id": "mem_first", "title": "First", "scope": "repo", "score": 0.5},
            {
                "id": "mem_second",
                "title": "Second",
                "scope": "repo",
                "score": 0.9,
                "provenance": {"source": "agent", "secret": "not forwarded"},
            },
            {"id": "mem_unpacked", "title": "Unpacked", "scope": "repo", "score": 0.1},
        ],
        packed_chunks=[
            PackedChunk("mem_second", "second", 3, False, "full"),
            PackedChunk("mem_first", "first", 2, True, "summary"),
        ],
        usage=ContextUsage(20, 12, 30, 18, 0.6, 2, 1),
    )

    compact = performance._compact_payload(result)

    assert [source["id"] for source in compact["sources"]] == ["mem_second", "mem_first"]
    assert [source["n"] for source in compact["sources"]] == [1, 2]
    assert all(source["id"] != "mem_unpacked" for source in compact["sources"])
    assert compact["sources"][1]["truncated"] is True
    assert "reason" not in compact["sources"][1]
    assert compact["sources"][0]["provenance"] == {"source": "agent"}
    assert set(compact["sources"][0]) == {"n", "id", "tokens", "title", "provenance"}
    assert compact["usage"]["context_tokens"] == 12


@pytest.mark.parametrize(
    ("config", "question_count", "message"),
    [
        (AcceptanceConfig(concurrency=2), 1, "concurrency"),
        (AcceptanceConfig(processes=0), 1, "processes"),
        (AcceptanceConfig(minimum_queries=2), 1, "minimum_queries"),
        (AcceptanceConfig(canonical=True, processes=5), 999, "1000 queries"),
        (AcceptanceConfig(canonical=True, processes=4), 1000, "5 processes"),
    ],
)
def test_acceptance_config_rejects_invalid_protocols(
    config: AcceptanceConfig, question_count: int, message: str
):
    with pytest.raises(ValueError, match=message):
        config.validate(question_count)


def test_canonical_acceptance_validation_does_not_require_a_large_run():
    AcceptanceConfig(concurrency=16, processes=5, minimum_queries=1000, canonical=True).validate(
        1000
    )


def test_per_slice_run_rejects_a_canonical_claim():
    with pytest.raises(ValueError, match="run_acceptance_matrix"):
        run(DATASET, canonical=True)


def test_acceptance_matrix_requires_every_declared_concurrency():
    with pytest.raises(ValueError, match="every concurrency"):
        performance.run_acceptance_matrix([], concurrencies=[1, 4], processes=5)


def test_acceptance_matrix_groups_each_slice_without_running_a_large_benchmark(monkeypatch):
    calls = []

    def fake_run(dataset, **kwargs):
        calls.append(kwargs)
        return {"schema": "engraphis-performance/v1", "acceptance": kwargs}

    monkeypatch.setattr(performance, "_question_count", lambda dataset: 1000)
    monkeypatch.setattr(performance, "run", fake_run)

    report = performance.run_acceptance_matrix([], processes=5)

    assert [call["concurrency"] for call in calls] == [1, 4, 16]
    assert all(call["canonical"] is False for call in calls)
    assert all(call["processes"] == 5 for call in calls)
    assert report["acceptance"]["concurrency_matrix"] == [1, 4, 16]
    assert list(report["slices"]) == ["1", "4", "16"]


def test_cli_acceptance_matrix_uses_matrix_runner(tmp_path, capsys, monkeypatch):
    dataset = tmp_path / "cases.jsonl"
    dataset.write_text(json.dumps(DATASET[0]) + "\n", encoding="utf-8")
    expected = {
        "schema": "engraphis-performance-matrix/v1",
        "acceptance": {"valid": True},
        "slices": {},
    }
    calls = []

    def fake_matrix(dataset, **kwargs):
        calls.append(kwargs)
        return expected

    monkeypatch.setattr(performance, "run_acceptance_matrix", fake_matrix)

    assert main([
        "--dataset", str(dataset),
        "--acceptance-matrix",
        "--candidate-k", "11",
        "--retrieval-profile", "graph",
        "--json",
    ]) == 0

    assert calls == [{
        "k": 5,
        "candidate_k": 11,
        "candidate_depth": "fixed",
        "dim": 256,
        "warmups": 1,
        "iterations": 5,
        "filler_memories": 0,
        "token_budget": 1500,
        "retrieval_profile": "graph",
        "processes": 1,
        "minimum_queries": 0,
    }]
    assert json.loads(capsys.readouterr().out) == expected


def test_performance_cli_emits_json(tmp_path, capsys):
    dataset = tmp_path / "cases.jsonl"
    dataset.write_text(json.dumps(DATASET[0]) + "\n", encoding="utf-8")

    assert main([
        "--dataset", str(dataset),
        "--iterations", "1",
        "--warmups", "0",
        "--json",
    ]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["corpus"]["questions"] == 1
    assert report["latency_ms"]["p95"] >= 0
