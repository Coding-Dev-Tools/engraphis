from pathlib import Path

from eval.harness import load_dataset, run


def test_adversarial_fixture_keeps_off_topic_queries_out_of_retrieval_denominator():
    path = Path(__file__).resolve().parents[1] / "eval" / "datasets" / "adversarial.jsonl"
    report = run(load_dataset(str(path)), k=2)
    assert report["questions"] == 2
    assert report["scored_questions"] == 1
    assert report["recall_at_k"] == 1.0
    assert report["exclusions"] == [{
        "question_id": "sourdough-off-topic",
        "reason": "off_topic_no_gold_evidence",
        "detail": "",
    }]
