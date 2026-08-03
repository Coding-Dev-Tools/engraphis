import json

from eval.longmemeval_v2_matrix import BASE_CONFIGS, prepare


def test_official_matrix_materializes_four_ablations_at_five_budgets(tmp_path):
    manifest = prepare(tmp_path)

    assert len(manifest["runs"]) == 20
    assert set(manifest["ablations"]) == set(BASE_CONFIGS)
    assert manifest["token_budgets"] == [256, 512, 1024, 2048, 4096]
    for row in manifest["runs"]:
        config = json.loads((tmp_path / row["config"]).read_text(encoding="utf-8"))
        assert config["memory_params"]["max_context_tokens"] == row["token_budget"]
        assert len(row["sha256"]) == 64
