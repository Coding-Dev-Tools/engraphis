import json

import pytest

from eval import benchmark_analysis as analysis
from eval.benchmark import canonical_json, sha256_file, sha256_text


def test_case_bootstrap_keeps_question_weights_and_matches_mean():
    rows = [{"question_id": f"a:{i}", "retrieval_scored": True, "value": 1.0} for i in range(9)]
    rows.append({"question_id": "b:0", "retrieval_scored": True, "value": 0.0})
    result = analysis.clustered_interval(rows, "value")
    assert result["point"] == .9
    assert result["source_cases"] == 2
    assert result["scored_questions"] == 10
    assert result["low"] == 0 and result["high"] == 1


def test_case_bootstrap_prefers_explicit_case_identity_over_question_id_shape():
    rows = [
        {"question_id": "upstream:shared:q:0", "case": "conversation-a",
         "retrieval_scored": True, "value": 1.0},
        {"question_id": "upstream:shared:q:1", "case": "conversation-a",
         "retrieval_scored": True, "value": 0.0},
        {"question_id": "upstream:shared:q:0", "case": "conversation-b",
         "retrieval_scored": True, "value": 1.0},
    ]

    result = analysis.clustered_interval(rows, "value")

    assert result["source_cases"] == 2
    assert result["scored_questions"] == 3


def test_one_source_case_has_no_manufactured_interval():
    result = analysis.clustered_interval([{"question_id": "a:0", "retrieval_scored": True, "value": .5}], "value")
    assert result["point"] == .5
    assert result["low"] is result["high"] is None


def test_paired_difference_carries_source_case_into_bootstrap_rows(tmp_path, monkeypatch):
    baseline = tmp_path / "baseline.json"
    candidate = tmp_path / "candidate.json"
    records = [
        {
            "question_id": "upstream:shared:q:0",
            "case": "conversation-a",
            "retrieval_scored": True,
            "supporting_ids": ["mem-a"],
            "packed_recall_at_k": 0.5,
        },
        {
            "question_id": "upstream:shared:q:1",
            "case": "conversation-a",
            "retrieval_scored": True,
            "supporting_ids": ["mem-b"],
            "packed_recall_at_k": 0.0,
        },
    ]
    before = {
        "suite": {"sha256": "dataset"},
        "models": {"model": "deterministic"},
        "protocol": {"config": {"token_budget": 12, "format": "locomo"}},
        "records": records,
    }
    after = {
        **before,
        "records": [
            {**records[0], "packed_recall_at_k": 1.0},
            {**records[1], "packed_recall_at_k": 0.5},
        ],
    }
    monkeypatch.setattr(
        analysis,
        "_read_verified_snapshot",
        lambda path: (before if path == baseline else after, path.name),
    )
    monkeypatch.setattr(analysis, "sha256_file", lambda path: path.name)
    captured = {}
    original = analysis.clustered_interval

    def spy(rows, field, **kwargs):
        captured["rows"] = rows
        return original(rows, field, **kwargs)

    monkeypatch.setattr(analysis, "clustered_interval", spy)

    result = analysis.paired_difference(baseline, candidate)

    assert result["packed_recall_delta"]["source_cases"] == 1
    assert [row["case"] for row in captured["rows"]] == [
        "conversation-a", "conversation-a",
    ]


def test_analysis_requires_artifact_sidecar(tmp_path):
    path = tmp_path / "report.json"
    path.write_text("{}")
    with pytest.raises(ValueError, match="checksum"):
        analysis.read_verified(path)


def _copy_diagnostic(path):
    source = analysis.Path(__file__).parents[1] / "docs/benchmark-evidence/locomo-full-20260916.json"
    path.write_bytes(source.read_bytes())
    checksum = sha256_file(path)
    path.with_suffix(".json.sha256").write_text(checksum, encoding="utf-8")
    return checksum


def test_analysis_digest_identifies_the_verified_bytes_even_if_input_changes(tmp_path, monkeypatch):
    path = tmp_path / "report.json"
    original_digest = _copy_diagnostic(path)
    original = analysis.validate_report

    def mutate_after_read(report):
        path.write_text("{}", encoding="utf-8")
        return original(report)

    monkeypatch.setattr(analysis, "validate_report", mutate_after_read)
    result = analysis.summarize(path)
    assert result["input_sha256"] == original_digest
    assert result["input_sha256"] != sha256_file(path)
    assert result["questions"] > 0


def test_analysis_parses_the_same_bytes_it_checksums(tmp_path, monkeypatch):
    path = tmp_path / "report.json"
    _copy_diagnostic(path)
    original = analysis.Path.read_text

    def mutate_after_checksum_read(target, *args, **kwargs):
        value = original(target, *args, **kwargs)
        if target == path.with_suffix(".json.sha256"):
            path.write_text("{}", encoding="utf-8")
        return value

    monkeypatch.setattr(analysis.Path, "read_text", mutate_after_checksum_read)
    assert analysis.read_verified(path)["records"]


def test_analysis_cli_reuses_input_snapshots_for_summary_and_pairing(tmp_path, monkeypatch):
    paths = [tmp_path / name for name in ("baseline.json", "candidate.json")]
    digests = [_copy_diagnostic(path) for path in paths]
    original = analysis._summarize_snapshot

    def mutate_after_read(*args):
        for path in paths:
            path.write_text("{}", encoding="utf-8")
        return original(*args)

    monkeypatch.setattr(analysis, "_summarize_snapshot", mutate_after_read)
    output = tmp_path / "analysis.json"
    assert analysis.main(["--reports", *map(str, paths), "--compare", "--output", str(output)]) == 0
    result = json.loads(output.read_text(encoding="utf-8"))
    assert [report["input_sha256"] for report in result["reports"]] == digests
    assert result["comparison"]["baseline_sha256"] == digests[0]
    assert result["comparison"]["candidate_sha256"] == digests[1]


def test_analysis_rejects_empty_checksum_file(tmp_path):
    path = tmp_path / "report.json"
    _copy_diagnostic(path)
    path.with_suffix(".json.sha256").write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="checksum"):
        analysis.read_verified(path)


@pytest.mark.parametrize("field,value", [
    ("repair_manifest_sha256", "b" * 64),
    ("repair_manifest_sha256", None),
    ("format", "longmemeval"),
    ("format", None),
])
def test_paired_difference_rejects_changed_normalization_bindings(tmp_path, field, value):
    source = analysis.Path(__file__).parents[1] / "docs/benchmark-evidence/locomo-full-20260916.json"
    before = json.loads(source.read_text(encoding="utf-8"))
    after = json.loads(json.dumps(before))
    after["protocol"]["config"][field] = value
    after["system"]["config_sha256"] = sha256_text(canonical_json(after["protocol"]["config"]))
    paths = [tmp_path / name for name in ("baseline.json", "candidate.json")]
    for path, report in zip(paths, (before, after)):
        path.write_text(json.dumps(report), encoding="utf-8")
        path.with_suffix(".json.sha256").write_text(sha256_file(path), encoding="utf-8")

    with pytest.raises(ValueError, match="normalized-corpus bindings|repair manifest source binding"):
        analysis.paired_difference(*paths)


def test_paired_difference_accepts_same_normalized_corpus_with_different_k(tmp_path):
    source = analysis.Path(__file__).parents[1] / "docs/benchmark-evidence/locomo-full-20260916.json"
    before = json.loads(source.read_text(encoding="utf-8"))
    after = json.loads(json.dumps(before))
    after["protocol"]["config"]["k"] += 1
    after["system"]["config_sha256"] = sha256_text(canonical_json(after["protocol"]["config"]))
    paths = [tmp_path / name for name in ("baseline.json", "candidate.json")]
    for path, report in zip(paths, (before, after)):
        path.write_text(json.dumps(report), encoding="utf-8")
        path.with_suffix(".json.sha256").write_text(sha256_file(path), encoding="utf-8")

    assert analysis.paired_difference(*paths)["packed_recall_delta"]["point"] == 0


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


@pytest.mark.parametrize("mutation", ["changed_repair", "missing_repair", "changed_producer"])
def test_analysis_binds_repair_sources_without_blocking_producer_changes(tmp_path, mutation):
    baseline = tmp_path / "baseline.json"
    candidate = tmp_path / "candidate.json"
    _copy_diagnostic(baseline)
    report = json.loads(baseline.read_text(encoding="utf-8"))
    repair = report["protocol"]["config"]["repair_manifest_sha256"]
    sources = report["suite"]["sources"]
    repair_source = next(source for source in sources if source["sha256"] == repair)
    if mutation == "missing_repair":
        sources.remove(repair_source)
    elif mutation == "changed_repair":
        repair_source["sha256"] = "0" * 64
    else:
        next(source for source in sources if source["sha256"] != repair)["sha256"] = "0" * 64
    candidate.write_text(json.dumps(report), encoding="utf-8")
    candidate.with_suffix(".json.sha256").write_text(sha256_file(candidate))
    if mutation == "changed_producer":
        assert analysis.paired_difference(baseline, candidate)["packed_recall_delta"]["point"] == 0.0
    else:
        with pytest.raises(ValueError, match="repair manifest source binding"):
            analysis.paired_difference(baseline, candidate)
