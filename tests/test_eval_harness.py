import json
from pathlib import Path

import pytest

from eval.benchmark import LONGMEMEVAL_V2_CANONICAL_PROFILE_TEMPLATE, validate_report
from engraphis.backends import DeterministicEmbedder
from engraphis.core.interfaces import SearchFilter
from engraphis.core.store import Store
from eval.harness import (
    _seed_case_graph,
    executable_baseline,
    load_dataset,
    main as harness_main,
    paired_v2_bootstrap,
    run,
    run_baseline_matrix,
)

DATASET = Path(__file__).resolve().parent.parent / "eval" / "datasets" / "sample.jsonl"
GRAPH_DATASET = (
    Path(__file__).resolve().parent.parent / "eval" / "datasets" / "graph_multihop.jsonl"
)


class FakePinnedReaderCounter:
    def __init__(self, model, revision):
        self.identity = f"{model}@{revision}"

    def __call__(self, text):
        return len(text.split())


def test_harness_runs_and_scores():
    report = run(load_dataset(str(DATASET)), k=3)
    assert report["questions"] == 9
    # The deterministic embedder should retrieve supporting facts for these
    # lexically-grounded questions; demand non-trivial recall so a regression trips CI.
    assert report["hit_at_k"] >= 0.75
    assert report["recall_at_k"] > 0.5


def test_harness_seeds_declared_fixture_graph_before_memory_ingestion():
    case = load_dataset(str(GRAPH_DATASET))[0]
    store = Store(":memory:")
    workspace_id = store.get_or_create_workspace("eval")
    repo_id = store.get_or_create_repo(workspace_id, case["id"])

    _seed_case_graph(
        store,
        workspace_id=workspace_id,
        repo_id=repo_id,
        case=case,
    )

    flt = SearchFilter(workspace_id=workspace_id, repo_id=repo_id)
    assert len(store.list_entities(flt)) == 3
    assert len(store.edges_in_scope(flt)) == 2
    store.close()


def test_metrics_edges():
    from eval import metrics
    assert metrics.recall_at_k([], []) == 0.0
    assert metrics.ndcg_at_k([], [], 5) == 0.0
    assert metrics.recall_at_k(["a", "b"], ["b"]) == 1.0
    assert metrics.hit_at_k(["a"], ["b"]) == 0.0
    assert metrics.answer_token_recall(["redis lock around stock decrement"],
                                       "Redis lock") == 1.0


def test_grounded_and_abstention_binary_metrics_are_explicit():
    from eval import metrics

    grounded = metrics.grounded_precision_recall_f1(
        [True, False, True], [True, True, False]
    )
    assert grounded == {
        "precision": 0.5, "recall": 0.5, "f1": 0.5,
        "true_positive": 1, "false_positive": 1, "false_negative": 1, "n": 3,
    }
    abstention = metrics.abstention_precision_recall_f1(
        [False, True, True], [True, True, False]
    )
    assert abstention["precision"] == 0.5
    assert abstention["recall"] == 1.0
    assert abstention["f1"] == pytest.approx(2 / 3)
    with pytest.raises(ValueError, match="equal length"):
        metrics.binary_precision_recall_f1([True], [])


def test_v2_harness_envelope_records_usage_latency_and_rank_metrics():
    report = run(load_dataset(str(DATASET)), k=3, v2=True, dataset_path=str(DATASET),
                 bootstrap_iterations=8)
    assert report["schema"] == "engraphis-benchmark/v2"
    assert len(report["suite"]["sha256"]) == 64
    assert len(report["system"]["config_sha256"]) == 64
    assert len(report["models"]["embedder"]["sha256"]) == 64
    first = report["records"][0]
    assert {"usage", "latency_ms", "recall_at_1", "recall_at_5", "recall_at_10",
            "mrr_at_5", "ndcg_at_10"} <= set(first)
    assert {"budget_tokens", "context_tokens", "source_tokens", "saved_tokens",
            "savings_ratio", "packed_count", "omitted_count", "token_counter"} <= set(first["usage"])
    metrics = report["metrics"]
    assert {"recall_at_1", "recall_at_5", "recall_at_10", "mrr_at_5", "ndcg_at_5",
            "confidence_intervals", "paired_bootstrap"} <= set(metrics)
    assert metrics["confidence_intervals"]["recall_at_5"]["iterations"] == 8
    assert metrics["paired_bootstrap"]["available"] is False
    assert report["legacy_summary"]["questions"] == 9


def test_canonical_harness_requires_pinned_profile_and_complete_artifact(
    monkeypatch, tmp_path,
):
    with pytest.raises(ValueError, match="pinned revisions"):
        run(load_dataset(str(DATASET)), canonical=True, dataset_path=str(DATASET))

    profile = {
        **LONGMEMEVAL_V2_CANONICAL_PROFILE_TEMPLATE,
        "benchmark": {
            "repository": "example/benchmark", "repository_revision": "a" * 40,
            "dataset_revision": "b" * 40,
        },
        "reader": {"model": "example/reader", "revision": "c" * 40},
        "embedding": {"model": "example/embedder", "revision": "d" * 40},
        "baseline_label": "full_hybrid",
    }
    monkeypatch.setattr(
        "eval.harness._load_pinned_reader_token_counter",
        lambda model, revision: FakePinnedReaderCounter(model, revision),
    )
    monkeypatch.setattr(
        "eval.benchmark.git_provenance",
        lambda: {"commit": "a" * 40, "dirty": False, "dirty_state_sha256": "b" * 64},
    )
    canonical_embedder = DeterministicEmbedder(dim=256)
    canonical_embedder.model_name = "example/embedder"
    canonical_embedder.revision = "d" * 40
    canonical_embedder.supports_semantic_search = True
    with pytest.raises(ValueError, match="positive bootstrap_iterations"):
        run(
            load_dataset(str(DATASET)),
            canonical=True,
            embedder=canonical_embedder,
            dataset_path=str(DATASET),
            canonical_profile=profile,
            bootstrap_iterations=0,
        )
    report = run(load_dataset(str(DATASET)), k=3, canonical=True, embedder=canonical_embedder,
                 dataset_path=str(DATASET), canonical_profile=profile,
                 bootstrap_iterations=2)
    assert report["protocol"]["complete_dataset"] is True
    assert report["protocol"]["source_questions"] == len(report["records"])
    assert validate_report(report, canonical=True) == []
    curve = report["metrics"]["fixed_budget_curve"]
    assert curve["available"] is True
    assert [row["token_budget"] for row in curve["rows"]] == [256, 512, 1024, 2048, 4096]
    assert all(row["status"] == "measured" for row in curve["rows"])
    assert all(len(row["records"]) == len(report["records"]) for row in curve["rows"])
    assert len(report["models"]["embedder"]["sha256"]) == 64
    assert "q" not in report["records"][0]
    assert "question_sha256" not in report["records"][0]
    assert (
        report["records"][0]["context_token_method"]
        == "pinned_reader_content_tokenizer"
    )
    assert report["records"][0]["context_tokenizer_identity"] == (
        "example/reader@" + "c" * 40
    )
    assert report["records"][0]["usage"]["token_counter"] == (
        "example/reader@" + "c" * 40
    )
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    private_report = tmp_path / "private.json"
    public_artifact = tmp_path / "public.json"
    monkeypatch.setattr(
        "eval.harness.get_embedder",
        lambda model, dim, **kwargs: canonical_embedder,
    )
    harness_main([
        "--dataset", str(DATASET),
        "--canonical",
        "--canonical-profile", str(profile_path),
        "--embed-model", "example/embedder",
        "--embed-revision", "d" * 40,
        "--bootstrap-iterations", "2",
        "--report", str(private_report),
        "--artifact", str(public_artifact),
    ])
    assert private_report.is_file()
    assert public_artifact.is_file()
    assert public_artifact.with_name("public.json.sha256").is_file()
    assert not private_report.with_name("private.json.sha256").exists()
    artifact = json.loads(public_artifact.read_text(encoding="utf-8"))
    assert artifact["models"]["embedder"]["model_id"] == "example/embedder"
    assert artifact["models"]["embedder"]["revision"] == "d" * 40
    with pytest.raises(ValueError, match="model_name and revision"):
        run(load_dataset(str(DATASET)), k=3, canonical=True,
            dataset_path=str(DATASET), canonical_profile=profile,
            bootstrap_iterations=2)
    mismatch = {**profile, "baseline_label": "lexical_only"}
    with pytest.raises(ValueError, match="must match the executed baseline_label"):
        run(load_dataset(str(DATASET)), canonical=True, dataset_path=str(DATASET),
            canonical_profile=mismatch, bootstrap_iterations=2)


def test_canonical_budget_curve_scores_only_packed_evidence(monkeypatch):
    import eval.harness as harness
    from engraphis.core.interfaces import ContextUsage, PackedChunk
    from engraphis.core.recall import RecallResult

    def controlled_recall(
        engine, query, *, workspace_id, repo_id, k, token_budget, baseline,
        source_records=None,
    ):
        del query, k, baseline, source_records
        record = engine.store.list_memories(
            SearchFilter(workspace_id=workspace_id, repo_id=repo_id)
        )[0]
        budget = 1500 if token_budget is None else token_budget
        admitted = budget != 256
        packed = (
            [PackedChunk(id=record.id, excerpt=record.content, tokens=1, reason="test")]
            if admitted else []
        )
        return RecallResult(
            chunks=[{"id": record.id, "title": record.title, "content": record.content}],
            packed_chunks=packed,
            context=record.content if admitted else "",
            count=1,
            usage=ContextUsage(
                budget_tokens=budget,
                context_tokens=1 if admitted else 0,
                source_tokens=1,
                saved_tokens=0 if admitted else 1,
                savings_ratio=0.0 if admitted else 1.0,
                packed_count=1 if admitted else 0,
                omitted_count=0 if admitted else 1,
                token_counter="test",
            ),
        )

    monkeypatch.setattr(harness, "_recall_for_baseline", controlled_recall)
    monkeypatch.setattr(
        harness,
        "_load_pinned_reader_token_counter",
        lambda model, revision: FakePinnedReaderCounter(model, revision),
    )
    profile = {
        **LONGMEMEVAL_V2_CANONICAL_PROFILE_TEMPLATE,
        "benchmark": {
            "repository": "example/benchmark", "repository_revision": "a" * 40,
            "dataset_revision": "b" * 40,
        },
        "reader": {"model": "example/reader", "revision": "c" * 40},
        "embedding": {"model": "example/embedder", "revision": "d" * 40},
        "baseline_label": "full_hybrid",
    }
    embedder = DeterministicEmbedder(dim=256)
    embedder.model_name = "example/embedder"
    embedder.revision = "d" * 40
    dataset = [{
        "id": "packed-budget",
        "memories": [{"tag": "gold", "text": "The release train leaves Tuesday."}],
        "questions": [{
            "id": "q1",
            "q": "when does the release train leave",
            "supporting": ["gold"],
        }],
    }]

    report = run(
        dataset,
        canonical=True,
        dataset_path=str(DATASET),
        canonical_profile=profile,
        embedder=embedder,
        bootstrap_iterations=2,
    )
    rows = {
        row["token_budget"]: row
        for row in report["metrics"]["fixed_budget_curve"]["rows"]
    }
    assert rows[256]["recall_at_1"] == 0.0
    assert rows[256]["records"][0]["context_tokens"] == 0
    assert rows[512]["recall_at_1"] == 1.0
    assert rows[512]["records"][0]["context_tokens"] == 1


def test_paired_bootstrap_rejects_partial_comparisons():
    candidate = [{"question_id": "a", "recall_at_5": 1.0},
                 {"question_id": "b", "recall_at_5": 0.0}]
    baseline = [{"question_id": "a", "recall_at_5": 0.0},
                {"question_id": "b", "recall_at_5": 0.0}]
    paired = paired_v2_bootstrap(candidate, baseline, iterations=8)
    assert paired["available"] is True and paired["delta"] == 0.5
    with pytest.raises(ValueError, match="identical scored question IDs"):
        paired_v2_bootstrap(candidate, baseline[:1])


def test_paired_bootstrap_rejects_duplicate_scored_question_ids():
    candidate = [
        {"question_id": "a", "recall_at_5": 1.0},
        {"question_id": "a", "recall_at_5": 0.0},
    ]
    baseline = [{"question_id": "a", "recall_at_5": 0.0}]

    with pytest.raises(ValueError, match="unique scored question IDs in candidate"):
        paired_v2_bootstrap(candidate, baseline, iterations=8)


def test_harness_cli_keeps_legacy_default_and_offers_opt_in_v2_artifacts(tmp_path, capsys):
    artifact = tmp_path / "run.json"
    report_path = tmp_path / "private-report.json"
    harness_main([
        "--dataset", str(DATASET), "--v2", "--artifact", str(artifact),
        "--report", str(report_path),
        "--bootstrap-iterations", "2",
    ])
    assert artifact.exists() and artifact.with_name("run.json.sha256").exists()
    assert report_path.exists()
    assert not report_path.with_name("private-report.json.sha256").exists()
    assert '"schema": "engraphis-benchmark/v2"' in capsys.readouterr().out
    with pytest.raises(SystemExit, match="2"):
        harness_main(["--dataset", str(DATASET), "--canonical"])


def test_harness_baselines_execute_declared_retrieval_arms(monkeypatch):
    from engraphis.core.recall import RecallEngine

    seen = []
    original = RecallEngine.recall

    def observe(self, *args, **kwargs):
        seen.append(kwargs.get("arm_config"))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(RecallEngine, "recall", observe)
    data = load_dataset(str(DATASET))
    for label in ("full_hybrid", "dense_only", "lexical_only", "dense_lexical_rrf", "no_graph"):
        run(data, k=2, baseline_label=label)
        config = seen[-1]
        expected = executable_baseline(label)
        assert (config.vector, config.lexical, config.graph, config.code) == (
            expected.vector, expected.lexical, expected.graph, False,
        )

    no_retrieval = run(data, k=2, baseline_label="no_retrieval")
    assert no_retrieval["baseline_execution"]["no_retrieval"] is True
    assert all(not item["retrieved_ids"] for item in no_retrieval["detail"])


def test_harness_baseline_matrix_and_canonical_labels_fail_closed():
    data = load_dataset(str(DATASET))
    matrix = run_baseline_matrix(
        data, baseline_labels=("dense_only", "lexical_only", "no_graph", "full_hybrid"), k=2,
    )
    assert set(matrix) == {"dense_only", "lexical_only", "no_graph", "full_hybrid"}
    assert matrix["dense_only"]["baseline_execution"]["arms"] == {
        "vector": True, "lexical": False, "graph": False, "code": False,
    }
    assert matrix["lexical_only"]["baseline_execution"]["arms"] == {
        "vector": False, "lexical": True, "graph": False, "code": False,
    }
    assert matrix["no_graph"]["baseline_execution"]["arms"] == {
        "vector": True, "lexical": True, "graph": False, "code": False,
    }
    rrf = run(data, baseline_label="dense_lexical_rrf")
    assert rrf["baseline_execution"]["equivalent_to"] == "no_graph"
    with pytest.raises(ValueError, match="requires a non-empty document"):
        run(data, baseline_label="whole_document")
    with pytest.raises(ValueError, match="requires ordered non-empty memories"):
        run_baseline_matrix(
            [{"id": "document-only", "document": "source", "questions": []}],
            baseline_labels=("full_history",),
        )


def test_harness_executes_corpus_and_temporal_baselines_only_when_representable():
    data = load_dataset(str(DATASET))
    history = run(data, baseline_label="full_history")
    assert history["baseline_execution"]["mode"] == "full_history"
    assert history["detail"][0]["usage"]["saved_tokens"] == 0
    assert set(history["detail"][0]["retrieved_ids"]) == {"f1", "f2", "f3", "f4"}

    document = [{
        "id": "doc", "document": "The billing export is in the settings menu.",
        "questions": [{"q": "where is the billing export", "evidence": "settings menu"}],
    }]
    whole = run(document, baseline_label="whole_document")
    assert whole["baseline_execution"]["mode"] == "whole_document"
    assert whole["detail"][0]["retrieved_ids"] == ["whole_document"]
    assert whole["detail"][0]["usage"]["saved_tokens"] == 0

    whole_with_sources = run([{
        "id": "document-with-sources",
        "document": "The billing export is in the settings menu.",
        "memories": [{"tag": "billing-settings", "text": "The billing export is in settings."}],
        "questions": [{
            "q": "where is the billing export", "supporting": ["billing-settings"],
        }],
    }], baseline_label="whole_document")
    assert whole_with_sources["detail"][0]["retrieved_ids"] == ["billing-settings"]
    assert whole_with_sources["detail"][0]["recall_at_k"] == 1.0

    temporal = [{
        "id": "temporal",
        "memories": [
            {"tag": "old", "text": "The plan price is ten dollars.", "subject_key": "plan-price",
             "claim_kind": "price", "valid_from": 1.0},
            {"tag": "new", "text": "The plan price is twenty dollars.", "subject_key": "plan-price",
             "claim_kind": "price", "valid_from": 2.0},
        ],
        "questions": [{"q": "what is the plan price", "supporting": ["new"]}],
    }]
    temporal_report = run(temporal, k=5, baseline_label="no_temporal_resolution")
    assert temporal_report["baseline_execution"]["temporal_resolution"] == "disabled"
    assert {"old", "new"} <= set(temporal_report["detail"][0]["retrieved_ids"])


def test_harness_no_reranker_requires_and_disables_a_real_reranker():
    class ReverseReranker:
        def __init__(self):
            self.calls = 0

        def rerank(self, query, candidates, k):
            self.calls += 1
            return list(reversed(candidates))[:k]

    with pytest.raises(ValueError, match="non-identity reranker"):
        run(load_dataset(str(DATASET)), baseline_label="no_reranker")
    reranker = ReverseReranker()
    report = run(load_dataset(str(DATASET)), baseline_label="no_reranker", reranker=reranker)
    assert reranker.calls == 0
    assert report["baseline_execution"]["reranker"] == "disabled"


def test_harness_v2_grounded_and_abstention_metrics_are_available_or_explicitly_unavailable():
    data = [{
        "id": "grounded",
        "memories": [{"tag": "fact", "text": "The release train leaves on Tuesday."}],
        "questions": [
            {"id": "answerable", "q": "when does the release train leave", "supporting": ["fact"],
             "answerable": True},
            {"id": "unanswerable", "q": "what is the moon made of", "supporting": [],
             "answerable": False},
        ],
    }]
    unavailable = run(data, v2=True, dataset_path=str(DATASET), bootstrap_iterations=2)
    assert unavailable["metrics"]["grounded"] == {
        "available": False, "reason": "grounded_recall_not_run", "n": 2,
    }
    assert unavailable["metrics"]["grounded_f1"] == {
        "available": False, "reason": "grounded_recall_not_run", "n": 2,
    }
    assert unavailable["protocol"]["n_scored"] == 1

    available = run(data, v2=True, dataset_path=str(DATASET), bootstrap_iterations=2, grounded=True)
    assert available["metrics"]["grounded"]["available"] is True
    assert available["metrics"]["abstention"]["available"] is True
    assert available["metrics"]["grounded"]["n"] == 2
    assert available["metrics"]["abstention"]["n"] == 2


def test_harness_rejects_explicitly_answerable_question_without_gold_evidence():
    data = [{
        "id": "broken",
        "memories": [{"tag": "fact", "text": "The release train leaves on Tuesday."}],
        "questions": [{
            "id": "no-gold",
            "q": "when does the release train leave",
            "supporting": [],
            "answerable": True,
        }],
    }]
    with pytest.raises(ValueError, match="answer evidence or supporting memory tags"):
        run(data, v2=True, dataset_path=str(DATASET), bootstrap_iterations=2)


def test_harness_scores_answer_variants_without_inventing_retrieval_gold():
    data = [{
        "id": "answer-only",
        "memories": [{"tag": "fact", "text": "The deployment region is US East."}],
        "questions": [{
            "id": "region",
            "q": "which deployment region",
            "answer": "us-east-1",
            "answer_variants": ["us-east-1", "US East"],
            "supporting": [],
        }],
    }]

    report = run(data, v2=True, dataset_path=str(DATASET), bootstrap_iterations=2)

    assert report["metrics"]["retrieval_scored_questions"] == 0
    assert report["metrics"]["answer_token_recall_n"] == 1
    assert report["records"][0]["retrieval_scored"] is False
    assert report["records"][0]["answer_scored"] is True
    assert report["metrics"]["answer_token_recall"] == 1.0



def test_retrieval_metrics_are_duplicate_safe_and_fail_closed():
    from eval import metrics

    assert metrics.recall_at_k(["gold", "gold"], ["gold", "gold"]) == 1.0
    assert metrics.ndcg_at_k(["gold", "gold"], ["gold"], 2) == 1.0
    assert metrics.reciprocal_rank(["noise", "gold", "gold"], ["gold"]) == pytest.approx(0.5)
    assert metrics.answer_token_recall([], "") == 0.0
    assert metrics.answer_token_recall(
        ["The deployment region is US East."],
        ["us-east-1", "US East"],
    ) == 1.0
    with pytest.raises(ValueError, match="string or a sequence"):
        metrics.answer_token_recall(["text"], [None])
    assert metrics.answer_token_recall(
        ["The deployment region is US East."],
        ["US East", "us-east-1"],
    ) == 1.0
    with pytest.raises(ValueError, match="retrieved_texts"):
        metrics.answer_token_recall([None], "text")
    with pytest.raises(ValueError, match="positive integers"):
        metrics.retrieval_metrics_at_depths(["gold"], ["gold"], depths=(0,))
    with pytest.raises(ValueError, match="strings"):
        metrics.recall_at_k(["gold"], [None])


def test_dataset_loader_reports_malformed_json_and_partial_records(tmp_path):
    malformed = tmp_path / "malformed.jsonl"
    malformed.write_text('{"id": "ok", "questions": []}\nnot-json\n', encoding="utf-8")
    with pytest.raises(ValueError, match=r"line 2"):
        load_dataset(str(malformed))

    partial = tmp_path / "partial.jsonl"
    partial.write_text(
        '{"id": "broken", "memories": [{"tag": "fact"}], "questions": []}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="requires non-empty text"):
        load_dataset(str(partial))


def test_empty_dataset_is_a_valid_zero_sized_evaluation():
    report = run([], k=1)
    assert report["questions"] == 0
    assert report["scored_questions"] == 0
    assert report["recall_at_k"] == 0.0
    assert report["hit_at_k"] == 0.0
    assert report["detail"] == []

CODEMEM_DATASET = Path(__file__).resolve().parent.parent / "eval" / "datasets" / "codemem.jsonl"


def test_codemem_dataset_meets_release_floor_at_k5():
    """The coding-agent wedge must hold the same 0.9 recall/hit floor as sample."""
    report = run(load_dataset(str(CODEMEM_DATASET)), k=5)
    assert report["recall_at_k"] >= 0.9
    assert report["hit_at_k"] >= 0.9


def test_sample_dataset_meets_release_floor_at_k5():
    report = run(load_dataset(str(DATASET)), k=5)
    assert report["recall_at_k"] >= 0.9
    assert report["hit_at_k"] >= 0.9


def test_harness_main_enforces_metric_floors(monkeypatch, capsys):
    """A gated dataset below its floor exits 1 with a clear failure line."""
    import eval.harness as harness

    monkeypatch.setitem(harness._METRIC_FLOORS, "sample", {"recall_at_k": 1.1})
    with pytest.raises(SystemExit) as excinfo:
        harness_main(["--dataset", str(DATASET), "--k", "5"])
    assert excinfo.value.code == 1
    assert "FLOOR VIOLATION" in capsys.readouterr().err


def test_harness_main_passes_ungated_datasets_without_a_floor(monkeypatch, tmp_path):
    """Datasets absent from the floor registry keep the old always-exit-0 behavior."""
    ungated = tmp_path / "ungated.jsonl"
    ungated.write_text(
        json.dumps({
            "id": "ungated",
            "memories": [{"tag": "deploy", "text": "The deploy marker is coral."}],
            "questions": [{"q": "what is the deploy marker?", "supporting": ["deploy"]}],
        }) + "\n",
        encoding="utf-8",
    )
    assert harness_main(["--dataset", str(ungated), "--k", "3"]) is None