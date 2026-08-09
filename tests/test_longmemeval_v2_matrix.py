import json

from eval.longmemeval_v2_matrix import BASE_CONFIGS, prepare


def test_official_matrix_materializes_cap_and_context_comparators(tmp_path):
    manifest = prepare(tmp_path)

    assert len(manifest["runs"]) == 30
    assert set(manifest["ablations"]) == set(BASE_CONFIGS)
    assert manifest["token_budgets"] == [256, 512, 1024, 2048, 4096]
    assert manifest["comparators"] == {
        "episodic_cap_2": "context_k_2",
        "planner_episodic_cap_2": "planner_context_k_2",
    }
    for row in manifest["runs"]:
        config = json.loads((tmp_path / row["config"]).read_text(encoding="utf-8"))
        params = config["memory_params"]
        assert params["max_context_tokens"] == row["token_budget"]
        assert params["context_k"] == row["context_k"]
        if row["ablation"] in {"context_k_2", "planner_context_k_2"}:
            assert params["context_k"] == 2
            assert params.get("mtype_limits", {}) == {}
        elif "episodic_cap_2" in row["ablation"]:
            assert params["context_k"] == 8
            assert params["mtype_limits"] == {"episodic": 2}
        assert len(row["sha256"]) == 64
