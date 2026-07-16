from pathlib import Path

from eval.ablation import _arm_recall
from eval.harness import load_dataset


def test_multihop_ablation_distinguishes_ppr_from_one_hop():
    dataset = load_dataset(
        str(Path(__file__).resolve().parents[1] / "eval" / "datasets" / "graph_multihop.jsonl")
    )

    assert _arm_recall(dataset, k=5, arm="graph1hop") == 0.0
    assert _arm_recall(dataset, k=5, arm="graphppr") == 1.0
