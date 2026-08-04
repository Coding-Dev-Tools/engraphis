from eval.consolidation_ranking import evaluate


def test_consolidation_bonus_is_measured_without_source_regressions():
    report = evaluate()
    summary = next(
        item for item in report["results"] if item["expected_role"] == "digest"
    )

    assert report["cases"] == 2
    assert report["summary_digest_top1_rate"] >= (
        report["baseline_summary_digest_top1_rate"]
    )
    assert summary["digest_score"] > summary["baseline_digest_score"]
    assert report["expected_hit_at_k"] == 1.0
    assert report["raw_detail_hit_at_k"] == 1.0
    assert report["source_hit_at_k"] == 1.0
