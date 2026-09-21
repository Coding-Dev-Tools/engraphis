import json

import pytest

from eval import benchmark_analysis as analysis
from eval.benchmark import canonical_json, sha256_file, sha256_text, validate_report


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


def _scoring_flag_report(*, retrieval_scored=True, answer_scored=True):
    config = {"format": "locomo", "token_budget": 8}
    record = {
        "question_id": "q0",
        "category": "test",
        "retrieved_ids": ["memory-0"],
        "packed_ids": ["memory-0"],
        "supporting_ids": ["memory-0"] if retrieval_scored is True else [],
        "context_tokens": 0,
        "recall_at_k": 1.0,
        "packed_recall_at_k": 1.0,
        "answer_token_recall": 1.0,
        "packed_answer_token_recall": 1.0,
        "retrieval_scored": retrieval_scored,
        "answer_scored": answer_scored,
    }
    return {
        "schema": "engraphis-benchmark/v2",
        "suite": {"name": "test", "dataset": "test.jsonl", "sha256": "a" * 64},
        "system": {
            "git_commit": "unknown",
            "config_sha256": sha256_text(canonical_json(config)),
        },
        "environment": {},
        "protocol": {"config": config, "n_total": 1, "n_scored": 1},
        "metrics": {
            "claim_boundary": (
                "evidence retrieval diagnostic; not generated-answer accuracy"
            ),
            "checkpoint_status": "COMPLETE",
            "questions": 1,
            "recall_at_k": 1.0 if retrieval_scored is True else None,
            "packed_recall_at_k": 1.0 if retrieval_scored is True else None,
            "answer_token_recall": 1.0 if answer_scored is True else None,
            "packed_answer_token_recall": 1.0 if answer_scored is True else None,
        },
        "models": {},
        "exclusions": [],
        "records": [record],
    }


def _write_scoring_flag_report(path, report):
    path.write_text(canonical_json(report), encoding="utf-8")
    path.with_suffix(path.suffix + ".sha256").write_text(
        sha256_file(path) + "\n", encoding="utf-8"
    )


@pytest.mark.parametrize("field", ["retrieval_scored", "answer_scored"])
@pytest.mark.parametrize("value", ["false", 0, 1, None, [], {}])
def test_validate_report_rejects_non_boolean_scoring_flags(field, value):
    report = _scoring_flag_report()
    report["records"][0][field] = value
    errors = validate_report(report)
    assert any(field in error and "boolean" in error for error in errors)


def test_validate_report_preserves_omitted_historical_scoring_flags():
    report = _scoring_flag_report()
    report["records"][0].pop("retrieval_scored")
    report["records"][0].pop("answer_scored")
    assert validate_report(report) == []


@pytest.mark.parametrize("flag", ["false", 1, None, {}])
def test_direct_interval_rejects_malformed_eligibility(flag):
    with pytest.raises(ValueError, match="eligibility.*boolean"):
        analysis.clustered_interval([{"question_id": "case:0", "retrieval_scored": flag,
                                      "value": 1.0}], "value")


def test_direct_interval_preserves_omitted_and_false_eligibility():
    result = analysis.clustered_interval([
        {"question_id": "case:0", "value": 1000},
        {"question_id": "case:1", "retrieval_scored": False, "value": 1000},
        {"question_id": "case:2", "retrieval_scored": True, "value": 0.5},
    ], "value")
    assert result["point"] == 0.5
    assert result["scored_questions"] == 1


@pytest.mark.parametrize(
    ("retrieval_scored", "answer_scored"),
    [(True, True), (True, False), (False, True), (False, False)],
)
def test_external_analysis_accepts_explicit_boolean_scoring_flags(
    tmp_path, retrieval_scored, answer_scored
):
    path = tmp_path / "diagnostic.json"
    _write_scoring_flag_report(
        path,
        _scoring_flag_report(
            retrieval_scored=retrieval_scored, answer_scored=answer_scored
        ),
    )
    report, _ = analysis._read_verified_snapshot(path)
    assert report["records"][0]["retrieval_scored"] is retrieval_scored
    assert report["records"][0]["answer_scored"] is answer_scored


@pytest.mark.parametrize("field", ["retrieval_scored", "answer_scored"])
@pytest.mark.parametrize("value", ["false", 0, 1, None, [], {}])
def test_external_analysis_rejects_non_boolean_scoring_flags(tmp_path, field, value):
    path = tmp_path / "malformed.json"
    report = _scoring_flag_report()
    report["records"][0][field] = value
    _write_scoring_flag_report(path, report)
    with pytest.raises(ValueError, match="boolean"):
        analysis._read_verified_snapshot(path)


def test_external_analysis_rejects_omitted_scoring_flags(tmp_path):
    path = tmp_path / "historical.json"
    report = _scoring_flag_report()
    report["records"][0].pop("retrieval_scored")
    report["records"][0].pop("answer_scored")
    _write_scoring_flag_report(path, report)
    with pytest.raises(ValueError, match="explicit boolean"):
        analysis._read_verified_snapshot(path)


def _write_report(path, report):
    path.write_text(json.dumps(report), encoding="utf-8")
    path.with_suffix(".json.sha256").write_text(sha256_file(path), encoding="utf-8")


@pytest.mark.parametrize("artifact", [
    "locomo-full-20260916.json",
    "longmemeval-full-20260916.json",
])
def test_analysis_accepts_supported_historical_repair_source_labels(tmp_path, artifact):
    source = analysis.Path(__file__).parents[1] / "docs/benchmark-evidence" / artifact
    target = tmp_path / artifact
    target.write_bytes(source.read_bytes())
    target.with_suffix(".json.sha256").write_text(sha256_file(target), encoding="utf-8")

    assert analysis.read_verified(target)["records"]


def test_analysis_accepts_current_repair_source_role(tmp_path):
    source = analysis.Path(__file__).parents[1] / "docs/benchmark-evidence/locomo-full-20260916.json"
    report = json.loads(source.read_text(encoding="utf-8"))
    repair_digest = report["protocol"]["config"]["repair_manifest_sha256"]
    repair_source = next(item for item in report["suite"]["sources"] if item["sha256"] == repair_digest)
    repair_source["name"] = "inputs/repair_manifest"
    target = tmp_path / "current.json"
    _write_report(target, report)

    assert analysis.read_verified(target)["records"]


def test_analysis_rejects_repair_digest_hidden_in_unrelated_producer(tmp_path):
    source = analysis.Path(__file__).parents[1] / "docs/benchmark-evidence/locomo-full-20260916.json"
    report = json.loads(source.read_text(encoding="utf-8"))
    repair_digest = report["protocol"]["config"]["repair_manifest_sha256"]
    report["suite"]["sources"] = [
        item for item in report["suite"]["sources"] if item["sha256"] != repair_digest
    ]
    report["suite"]["sources"][0]["sha256"] = repair_digest
    target = tmp_path / "forged-producer.json"
    _write_report(target, report)

    with pytest.raises(ValueError, match="repair manifest source binding"):
        analysis.read_verified(target)


def test_analysis_rejects_wrong_named_repair_source(tmp_path):
    source = analysis.Path(__file__).parents[1] / "docs/benchmark-evidence/locomo-full-20260916.json"
    report = json.loads(source.read_text(encoding="utf-8"))
    repair_digest = report["protocol"]["config"]["repair_manifest_sha256"]
    repair_source = next(item for item in report["suite"]["sources"] if item["sha256"] == repair_digest)
    repair_source["name"] = "wrong-repair-manifest.json"
    target = tmp_path / "wrong-name.json"
    _write_report(target, report)

    with pytest.raises(ValueError, match="repair manifest source binding"):
        analysis.read_verified(target)


@pytest.mark.parametrize("configured", [None, "not-a-sha256"])
def test_analysis_rejects_missing_or_malformed_repair_binding(tmp_path, configured):
    source = analysis.Path(__file__).parents[1] / "docs/benchmark-evidence/locomo-full-20260916.json"
    report = json.loads(source.read_text(encoding="utf-8"))
    report["protocol"]["config"]["repair_manifest_sha256"] = configured
    report["system"]["config_sha256"] = sha256_text(canonical_json(report["protocol"]["config"]))
    target = tmp_path / "invalid-binding.json"
    _write_report(target, report)

    with pytest.raises(ValueError, match="repair manifest"):
        analysis.read_verified(target)


def test_analysis_rejects_duplicate_repair_source_identities(tmp_path):
    source = analysis.Path(__file__).parents[1] / "docs/benchmark-evidence/locomo-full-20260916.json"
    report = json.loads(source.read_text(encoding="utf-8"))
    repair_digest = report["protocol"]["config"]["repair_manifest_sha256"]
    repair_source = next(item for item in report["suite"]["sources"] if item["sha256"] == repair_digest)
    report["suite"]["sources"].append(dict(repair_source))
    target = tmp_path / "duplicate-binding.json"
    _write_report(target, report)

    with pytest.raises(ValueError, match="ambiguous repair manifest"):
        analysis.read_verified(target)


@pytest.mark.parametrize("retained", [None, {}, [], "manifest", {"sha256": None},
                                     {"sha256": "invalid"}, {"sha256": "b" * 64}])
def test_analysis_rejects_conflicting_or_malformed_retained_repair(tmp_path, retained):
    path = tmp_path / "diagnostic.json"
    _copy_diagnostic(path)
    report = json.loads(path.read_text(encoding="utf-8"))
    report["metrics"]["dataset_integrity"]["repair_manifest"] = retained
    _write_report(path, report)

    with pytest.raises(ValueError, match="repair manifest"):
        analysis.read_verified(path)


def test_analysis_rejects_retained_repair_without_configured_binding(tmp_path):
    path = tmp_path / "diagnostic.json"
    _copy_diagnostic(path)
    report = json.loads(path.read_text(encoding="utf-8"))
    report["protocol"]["config"]["repair_manifest_sha256"] = None
    report["system"]["config_sha256"] = sha256_text(canonical_json(report["protocol"]["config"]))
    report["suite"]["sources"] = [item for item in report["suite"]["sources"]
                                  if item["name"] != "locomo10_repair_manifest_v2.json"]
    _write_report(path, report)

    with pytest.raises(ValueError, match="repair manifest"):
        analysis.read_verified(path)


@pytest.mark.parametrize("integrity", [None, {}, {"repair_manifest": None}])
def test_analysis_accepts_unrepaired_diagnostics(tmp_path, integrity):
    report = _scoring_flag_report()
    report["metrics"]["dataset_integrity"] = integrity
    path = tmp_path / "diagnostic.json"
    _write_report(path, report)

    assert analysis.read_verified(path)["records"]


def test_analysis_accepts_legacy_omitted_repair_metadata(tmp_path):
    path = tmp_path / "diagnostic.json"
    _copy_diagnostic(path)
    report = json.loads(path.read_text(encoding="utf-8"))
    report["metrics"].pop("dataset_integrity")
    _write_report(path, report)

    assert analysis.read_verified(path)["records"]


@pytest.mark.parametrize("format_name", [[], {}, 1, False, None])
def test_analysis_rejects_malformed_dataset_format_cleanly(tmp_path, format_name):
    report = _scoring_flag_report()
    report["protocol"]["config"]["format"] = format_name
    report["system"]["config_sha256"] = sha256_text(canonical_json(report["protocol"]["config"]))
    path = tmp_path / "diagnostic.json"
    _write_report(path, report)

    with pytest.raises(ValueError, match="dataset format"):
        analysis.read_verified(path)
