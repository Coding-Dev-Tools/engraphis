from eval.fts_insert_scaling import run_comparison


def test_counterfactual_preserves_canonical_vector_and_mirror_counts():
    report = run_comparison([3, 8], dim=4, batch_size=2)
    cells = report["metrics"]["cells"]
    assert len(cells) == 4
    assert {cell["strategy"] for cell in cells} == {"forced_legacy_delete", "new_row_insert"}
    for cell in cells:
        assert set(cell["verified_row_counts"].values()) == {cell["corpus_size"]}
        assert cell["elapsed_seconds"] > 0
        assert cell["disk"]["database_bytes"] > 0
    assert report["metrics"]["source_stable"] is True
