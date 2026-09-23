"""Public exports must retain source clusters independently of question ID syntax."""
import json
from pathlib import Path

import pytest

from eval import agent_benchmarks, benchmark_analysis
from eval.benchmark import redact_public_record, report_envelope, validate_report


def test_public_record_retains_case_without_exporting_source_content():
    record = redact_public_record({"question_id": "ruler_qa1_197K_no0", "case": "ruler_qa1_197K",
                                   "question": "private question", "context": "private context"})
    assert record == {"question_id": "ruler_qa1_197K_no0", "case": "ruler_qa1_197K"}


def test_agent_export_preserves_clusters_for_unstructured_question_ids(tmp_path):
    source = tmp_path / "mab.json"
    source.write_text(json.dumps({"data": [
        {"case_id": name, "memory_events": [{"id": "fact", "text": "Release is Tuesday."}],
         "questions": ["When is release?"] * len(ids), "answers": ["Tuesday"] * len(ids),
         "qa_pair_ids": ids, "supporting_ids": [["fact"]] * len(ids)}
        for name, ids in (("conversation-a", ["upstream_7", "upstream_20"]),
                          ("conversation-b", ["upstream_19"]))
    ]}), encoding="utf-8")
    artifact = tmp_path / "public.json"
    assert agent_benchmarks.main(["--format", "memoryagentbench", "--dataset", str(source),
                                 "--artifact", str(artifact)]) == 0
    report = json.loads(artifact.read_text(encoding="utf-8"))
    assert validate_report(report) == []
    assert report["protocol"]["config"]["source_case_identity"] == "explicit"
    assert [row["case"] for row in report["records"]] == ["conversation-a", "conversation-a", "conversation-b"]
    interval = benchmark_analysis.clustered_interval(report["records"], "recall_at_k")
    assert interval["source_cases"] == 2
    assert interval["scored_questions"] == 3


@pytest.mark.parametrize("case", [None, "", "  ", 7, {}, " padded "])
def test_new_diagnostics_require_explicit_valid_source_case(tmp_path, case):
    source = tmp_path / "fixture.json"
    source.write_text("{}")
    record = {"question_id": "unstructured_id"}
    if case is not None:
        record["case"] = case
    report = report_envelope(suite="fixture", dataset_path=source,
                             config={"source_case_identity": "explicit"}, records=[record])
    assert any("case identity" in error for error in validate_report(report))


def test_historical_diagnostic_without_explicit_case_remains_readable():
    path = Path(__file__).parents[1] / "docs/benchmark-evidence/locomo-full-20260916.json"
    report = benchmark_analysis.read_verified(path)
    assert all("case" not in row for row in report["records"])
    assert benchmark_analysis.clustered_interval(report["records"], "recall_at_k")["source_cases"] == 10
