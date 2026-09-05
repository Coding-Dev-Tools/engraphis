from eval.vector_scan_plan import run_comparison


def test_join_order_ablation_preserves_selective_and_full_vectors():
    report = run_comparison([11], dim=4, batch_size=3)
    cells = report["metrics"]["cells"]
    assert len(cells) == 24
    for offset in range(0, len(cells), 4):
        baseline, optimized, adaptive, scoped_first = cells[offset:offset + 4]
        assert baseline["result_sha256"] == optimized["result_sha256"]
        assert baseline["result_sha256"] == adaptive["result_sha256"]
        assert baseline["result_sha256"] == scoped_first["result_sha256"]
        assert baseline["rows"] == optimized["rows"]
        assert not any("TEMP B-TREE" in line for line in optimized["query_plan"])
    assert report["metrics"]["source_stable"]
