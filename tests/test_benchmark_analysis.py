import json

import pytest

from eval import benchmark_analysis as analysis
from eval.benchmark import sha256_file


def test_case_bootstrap_keeps_question_weights_and_matches_mean():
    rows = [{"question_id": f"a:{i}", "retrieval_scored": True, "value": 1.0} for i in range(9)]
    rows.append({"question_id": "b:0", "retrieval_scored": True, "value": 0.0})
    result = analysis.clustered_interval(rows, "value")
    assert result["point"] == .9
    assert result["source_cases"] == 2
    assert result["scored_questions"] == 10
    assert result["low"] == 0 and result["high"] == 1


def test_one_source_case_has_no_manufactured_interval():
    result = analysis.clustered_interval([{"question_id": "a:0", "retrieval_scored": True, "value": .5}], "value")
    assert result["point"] == .5
    assert result["low"] is result["high"] is None


def test_analysis_requires_artifact_sidecar(tmp_path):
    path = tmp_path / "report.json"
    path.write_text("{}")
    with pytest.raises(ValueError, match="checksum"):
        analysis.read_verified(path)


def test_analysis_rechecks_counts_even_with_recomputed_checksum(tmp_path):
    source = analysis.Path(__file__).parents[1] / "docs/benchmark-evidence/locomo-full-20260916.json"
    if not source.exists():
        pytest.skip("full retained diagnostic is not included in this source distribution")
    path = tmp_path / "modified.json"
    report = json.loads(source.read_text(encoding="utf-8"))
    report["records"][0]["packed_recall_at_k"] = .2
    path.write_text(json.dumps(report), encoding="utf-8")
    path.with_suffix(".json.sha256").write_text(sha256_file(path), encoding="utf-8")
    with pytest.raises(ValueError, match="metric"):
        analysis.read_verified(path)
