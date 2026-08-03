from pathlib import Path

from eval.harness import load_dataset
from eval.resource_hierarchy import TOKEN_BUDGETS, run


def test_resource_hierarchy_remains_evaluation_only_until_gate_passes():
    root = Path(__file__).resolve().parents[1]
    report = run(load_dataset(str(root / "eval" / "datasets" / "longdoc.jsonl")))

    assert report["benchmark"]["evaluation_only"] is True
    assert report["workload"]["documents"] == 4
    assert report["benchmark"]["split"]["heldout_ids"] == [
        "memory-engine",
        "billing",
        "onboarding",
        "data-pipeline",
    ]
    assert set(report["methods"]) == {"flat", "hierarchy"}
    assert set(report["methods"]["flat"]) == {str(value) for value in TOKEN_BUDGETS}
    gate = report["production_gate"]
    if gate["passed"]:
        assert gate["schema_action"] == "bump_to_8"
    else:
        assert gate["schema_action"] == "retain_7"
