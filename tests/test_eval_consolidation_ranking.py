from eval.consolidation_ranking import evaluate


def test_consolidation_bonus_is_measured_without_source_regressions():
    report = evaluate()
    summary = next(
        item for item in report["results"] if item["expected_role"] == "digest"
    )

    assert report["cases"] == 2
    assert report["bonus_probe_count"] == 2
    assert report["ranking_changed_rate"] == 0.5
    assert report["bonus_probe_digest_top1_rate"] == 1.0
    assert report["baseline_bonus_probe_digest_top1_rate"] == 0.0
    assert report["bonus_probe_source_top1_rate"] == 1.0
    assert report["bonus_probe_source_regressions"] == []
    assert report["summary_digest_top1_rate"] >= (
        report["baseline_summary_digest_top1_rate"]
    )
    assert report["production_trace_ranking_changed_rate"] >= 0.5
    assert summary["digest_improved"] is True
    assert summary["digest_score"] > summary["baseline_digest_score"]
    assert report["expected_hit_at_k"] == 1.0
    assert report["raw_detail_hit_at_k"] == 1.0
    assert report["source_hit_at_k"] == 1.0
