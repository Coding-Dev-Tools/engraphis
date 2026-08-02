from pathlib import Path

from eval.ablation import _arm_recall, _ordinary_recall_age_delta, _score
from eval.harness import load_dataset


def test_multihop_ablation_distinguishes_ppr_from_one_hop():
    dataset = load_dataset(
        str(Path(__file__).resolve().parents[1] / "eval" / "datasets" / "graph_multihop.jsonl")
    )

    assert _arm_recall(dataset, k=5, arm="graph1hop") == 0.0
    assert _arm_recall(dataset, k=5, arm="graphppr") == 1.0
def test_hybrid_ablation_requests_inspection_visibility_for_raw_fixture_rows():
    dataset = load_dataset(
        str(Path(__file__).resolve().parents[1] / "eval" / "datasets" / "sample.jsonl")
    )

    # eval.ablation seeds Store directly, which intentionally lacks prompt approval
    # metadata. The ablation must measure retrieval, not prompt-context eligibility.
    assert _score(dataset, k=5, hybrid=True, graph_mode="ppr") == 1.0


def test_ordinary_recall_age_ablation_has_no_second_age_penalty():
    assert _ordinary_recall_age_delta() == 0.0
