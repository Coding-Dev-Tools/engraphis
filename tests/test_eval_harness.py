from pathlib import Path

from eval.harness import load_dataset, run

DATASET = Path(__file__).resolve().parent.parent / "eval" / "datasets" / "sample.jsonl"


def test_harness_runs_and_scores():
    report = run(load_dataset(str(DATASET)), k=3)
    assert report["questions"] == 4
    # The deterministic embedder should retrieve supporting facts for these
    # lexically-grounded questions; demand non-trivial recall so a regression trips CI.
    assert report["hit_at_k"] >= 0.75
    assert report["recall_at_k"] > 0.5


def test_metrics_edges():
    from eval import metrics
    assert metrics.recall_at_k([], []) == 1.0
    assert metrics.recall_at_k(["a", "b"], ["b"]) == 1.0
    assert metrics.hit_at_k(["a"], ["b"]) == 0.0
    assert metrics.answer_token_recall(["redis lock around stock decrement"],
                                       "Redis lock") == 1.0
