import hashlib
import json
import re
import struct
from copy import deepcopy
from pathlib import Path
from xml.etree import ElementTree

import pytest

from eval import metrics
from eval import grounded as grounded_eval
from eval.benchmark import (
    SCHEMA,
    CANONICAL_TOKEN_BUDGETS,
    LONGMEMEVAL_V2_CANONICAL_PROFILE_TEMPLATE,
    canonical_benchmark_config,
    count_tokens,
    fixed_budget_curve,
    paired_bootstrap_ci,
    redact_command,
    redact_public_record,
    main,
    question_record,
    report_envelope,
    stratified_bootstrap_ci,
    validate_report,
    write_canonical_artifact,
)
from eval.chunking_eval import compare as compare_chunking, load as load_chunking


ROOT = Path(__file__).resolve().parents[1]


def test_public_facing_docs_do_not_use_em_dashes():
    """Published prose uses straightforward punctuation that renders consistently."""
    public_files = [
        *(ROOT / name for name in ("README.md", "BENCHMARKS.md", "CHANGELOG.md", "SECURITY.md")),
        *(ROOT / "docs").rglob("*.md"),
        *(ROOT / "docs" / "images").glob("*.svg"),
        *(ROOT / "skills" / "engraphis-memory").rglob("*.md"),
    ]
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in public_files
        if "—" in path.read_text(encoding="utf-8")
    ]

    assert not offenders, f"Public-facing files still contain em dashes: {offenders}"


class CharacterTokenizer:
    def encode(self, text):
        return list(text)


def test_public_record_redaction_uses_an_allowlist_for_raw_payload_aliases():
    record = redact_public_record({
        "question_id": "q1",
        "query": "private query",
        "answer_variants": ["private answer"],
        "model_output": "private completion",
        "context": "private context",
        "retrieved_context": "private retrieved context",
        "prompt": "private prompt",
        "input": "private input",
        "conversation": ["private conversation"],
        "history": ["private history"],
        "tool_calls": [{"arguments": "private tool input"}],
    })

    for field in (
        "query", "answer_variants", "model_output", "context", "retrieved_context",
    ):
        assert field not in record
    assert record["question_id"] == "q1"
    assert len(record["query_sha256"]) == 64
    assert len(record["answer_or_response_sha256"]) == 64
    assert len(record["context_or_prompt_sha256"]) == 64


def test_readme_distinguishes_every_current_token_context_measurement():
    """Public token-efficiency copy must preserve each metric's counting boundary."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    for evidence in (
        "## Measured token and context savings",
        "98.21 percent less long-history context",
        "73.0 percent less retrieved content per question",
        "73.9 percent fewer tokens in the smallest useful memory",
        "55.38 percent smaller memory response",
        "47.8 percent less repeated-memory context after consolidation",
        "<summary>See benchmark details and reproduce the results</summary>",
        "### Measurement details and reproducibility",
        "49,915,394** tokens → Engraphis: **891,857** tokens",
        "98.2133% lower",
        "808.8** tokens → structure-aware chunks: **218.4** tokens",
        "73.0% lower",
        "162.2** tokens → chunks: **42.4** tokens",
        "73.9% lower",
        "17,172** `engraphis.regex.v1` tokens → compact result: **7,663** tokens",
        "55.38% lower",
        "230** tokens → one digest: **120** tokens",
        "47.8% lower",
        "1,500** tokens; observed mean: **87.73**; observed maximum: **106**",
        "must not be added together",
        "not a storage-reduction claim",
        "There is no universal memory-count",
        "python -m eval.vector_scale",
        "vector_backend=\"sqlite-vec\"",
    ):
        assert evidence in readme


def test_readme_puts_external_evidence_boundary_beside_the_chart():
    """The external-result caveat must remain visible before collapsed details."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    benchmarks = (ROOT / "BENCHMARKS.md").read_text(encoding="utf-8")
    security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")

    boundary = "External LoCoMo-derived figures are not canonical."
    assert boundary in readme
    assert readme.index("</p>") < readme.index(boundary) < readme.index("<details>")
    assert "immutable rerun produces a validated" in readme
    assert "public artifact and checksum" in readme

    for detail in (
        "Unpinned, noncanonical workload diagnostic",
        "not answer quality or leaderboard accuracy",
        "### Choose a vector backend for your corpus",
        "python -m eval.redteam_poisoning",
        "[local and hosted plans]",
    ):
        assert detail not in readme

    assert "unpinned, noncanonical workload diagnostic" in benchmarks.lower()
    assert "NumPy vector scale envelope" in benchmarks
    assert "python -m eval.redteam_poisoning" in security


def test_readme_makes_agent_benefits_and_visual_evidence_scannable():
    """The public overview and its visual evidence must stay wired to real assets."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    for evidence in (
        "## What Engraphis gives an agent",
        "Remember a project across sessions",
        "Avoid confident guesses",
        "Avoid dragging the whole project into every prompt",
        "docs/images/engraphis-benefit-flow.png",
        "docs/images/context-efficiency.png",
        "### See the behavior in reproducible fixtures",
        "docs/images/evidence-backed-agent-examples.png",
        "Run `python -m eval.chunking_eval` and `python -m eval.grounded`",
        "Less repeated history means more room for the task, tools, and useful evidence",
    ):
        assert evidence in readme

    for filename in (
        "engraphis-benefit-flow.svg",
        "engraphis-benefit-flow.png",
        "context-efficiency.svg",
        "context-efficiency.png",
        "evidence-backed-agent-examples.svg",
        "evidence-backed-agent-examples.png",
    ):
        assert (ROOT / "docs" / "images" / filename).is_file()


def test_readme_visual_pngs_match_their_svg_canvas():
    """README image exports must not carry hidden screenshot padding."""
    image_dir = ROOT / "docs" / "images"

    for stem in (
        "engraphis-benefit-flow",
        "evidence-backed-agent-examples",
        "context-efficiency",
    ):
        svg = ElementTree.parse(image_dir / f"{stem}.svg").getroot()
        expected = (int(svg.attrib["width"]), int(svg.attrib["height"]))
        png_header = (image_dir / f"{stem}.png").read_bytes()[:24]

        assert png_header[:8] == b"\x89PNG\r\n\x1a\n"
        assert struct.unpack(">II", png_header[16:24]) == expected


def test_example_visual_uses_the_checked_in_offline_fixture_results():
    """The new examples must not drift away from the commands readers can run."""
    longdoc = ROOT / "eval" / "datasets" / "longdoc.jsonl"
    chunking = compare_chunking(load_chunking(str(longdoc)), k=5, embed_model=None)
    whole = chunking["reports"]["whole"]
    chunked = chunking["reports"]["chunked"]
    grounded = grounded_eval.run()
    visual = (ROOT / "docs" / "images" / "evidence-backed-agent-examples.svg").read_text(
        encoding="utf-8"
    )

    assert chunking["context_reduction_pct"] == 73.0
    assert f"{whole['mean_context_tokens']:.1f} → {chunked['mean_context_tokens']:.1f} tokens" in visual
    assert grounded == {
        "answer_rate": 1.0,
        "abstain_rate": 1.0,
        "accuracy": 1.0,
        "grounded_hits": 5,
        "abstain_hits": 5,
        "n_answerable": 5,
        "n_unanswerable": 5,
    }
    assert "5/5 answerable questions grounded" in visual
    assert "5/5 off-topic questions abstained" in visual


def test_context_savings_visual_is_plain_language_and_uses_measured_results():
    """The headline chart must stay simple and tied to the documented measurements."""
    visual = (ROOT / "docs" / "images" / "context-efficiency.svg").read_text(
        encoding="utf-8"
    )

    for evidence in (
        "Give your agent more room to think",
        "Replay everything · 49,915,394 tokens",
        "Engraphis · 891,857 tokens",
        "98.21% less",
        "Focused context; full-history recall was higher",
        "Whole documents · 808.8 tokens",
        "Focused chunks · 218.4 tokens",
        "73.0% less",
        "Whole document · 162.2 tokens",
        "Useful chunk · 42.4 tokens",
        "73.9% less",
        "Full response · 17,172 tokens",
        "Compact response · 7,663 tokens",
        "55.38% less",
        "Repeated memories · 230 tokens",
        "Consolidated digest · 120 tokens",
        "47.8% less",
        "53× more evidence",
        "INCLUDING INDEXING",
        "97.72% less total",
        "paid back by question 10",
        "87.7 average · 106 max",
        "percentages are not additive",
    ):
        assert evidence in visual

    text_sizes = {
        float(value)
        for value in re.findall(r'font-size="([^"]+)"', visual)
    }
    assert text_sizes == {13.2, 14.3, 17.6, 18.7, 25.3, 29.7, 33.0}


def _complete_canonical_report(dataset, config):
    """Minimal but fully auditable canonical envelope for validator coverage."""
    profile = config["canonical_profile"]
    tokenizer_identity = (
        f"{profile['reader']['model']}@{profile['reader']['revision']}"
    )
    record = question_record(
        "q1", category="state", context_tokens=3, latency_ms=1.25,
        retrieved_ids=["support"], supporting_ids=["support"],
        recall_at_1=1.0, recall_at_5=1.0, recall_at_10=1.0,
        mrr_at_1=1.0, mrr_at_5=1.0, mrr_at_10=1.0,
        ndcg_at_1=1.0, ndcg_at_5=1.0, ndcg_at_10=1.0,
        usage={
            "budget_tokens": config.get("token_budget") or 3,
            "context_tokens": 3,
            "token_counter": tokenizer_identity,
        },
    )
    record["context_token_method"] = "pinned_reader_content_tokenizer"
    record["context_tokenizer_identity"] = tokenizer_identity
    rank_metrics = {
        f"{metric}_at_{depth}": 1.0
        for metric in ("recall", "mrr", "ndcg")
        for depth in (1, 5, 10)
    }
    curve_record = {
        "question_id": "q1",
        "excluded": False,
        "context_tokens": 3,
        "context_token_method": "pinned_reader_content_tokenizer",
        "context_tokenizer_identity": tokenizer_identity,
        "retrieved_ids": ["support"],
        "supporting_ids": ["support"],
        **rank_metrics,
    }
    report = report_envelope(
        suite="fixture", dataset_path=dataset, config=config, records=[record],
        metrics={
            **rank_metrics,
            "confidence_intervals": {
                field: {
                    "point": 1.0,
                    "low": 1.0,
                    "high": 1.0,
                    "n": 1,
                    "seed": 20260729,
                    "iterations": 1,
                    "strata_key": "category",
                }
                for field in rank_metrics
            },
            "paired_bootstrap": {
                "available": False,
                "reason": "baseline_records_not_supplied",
                "n": 0,
                "delta": None,
                "low": None,
                "high": None,
                "iterations": 1,
            },
            "grounded_f1": {"available": False, "reason": "not_measured"},
            "abstention_f1": {"available": False, "reason": "not_measured"},
            "fixed_budget_curve": {
                "available": True,
                "rows": [{
                    "token_budget": budget,
                    "status": "measured",
                    "n_total": 1,
                    "n_scored": 1,
                    "records": [dict(curve_record)],
                    **rank_metrics,
                } for budget in CANONICAL_TOKEN_BUDGETS],
            },
        },
        git_commit="a" * 40,
    )
    report["system"]["git_dirty"] = False
    report["models"] = {"embedder": {
        "name": "FixtureEmbedder",
        "model_id": profile["embedding"]["model"],
        "revision": profile["embedding"]["revision"],
        "sha256": "b" * 64,
    }}
    report["protocol"]["complete_dataset"] = True
    report["protocol"]["source_questions"] = len(report["records"])
    return report


def test_metrics_cover_rank_sensitive_retrieval_quality():
    retrieved = ["noise", "evidence-a", "evidence-b"]
    supporting = ["evidence-a", "evidence-b"]
    assert metrics.mrr_at_k(retrieved, supporting, 3) == 0.5
    assert metrics.ndcg_at_k(retrieved, supporting, 3) > 0.6
    assert metrics.recall_at_k(retrieved[:1], supporting) == 0.0
    assert metrics.hit_at_k(retrieved[:1], supporting) == 0.0
    bundle = metrics.retrieval_metrics_at_depths(retrieved, supporting)
    assert bundle["recall_at_1"] == 0.0
    assert bundle["recall_at_5"] == 1.0
    assert bundle["mrr_at_5"] == 0.5


def test_envelope_hashes_dataset_config_and_retains_exclusions(tmp_path):
    dataset = tmp_path / "fixture.jsonl"
    dataset.write_text('{"id":"one"}\n', encoding="utf-8")
    excluded = {"question_id": "q2", "reason": "no_gold_evidence", "detail": ""}
    records = [
        question_record("q1", category="state", supporting_ids=["m1"]),
        question_record("q2", category="abstention", excluded=excluded),
    ]
    report = report_envelope(
        suite="fixture", dataset_path=dataset, config={"k": 5}, records=records,
        metrics={"recall": 1.0}, git_commit="abc123",
    )
    assert report["schema"] == SCHEMA
    assert report["suite"]["sha256"]
    assert report["system"]["config_sha256"]
    assert report["protocol"] == {
        "command": ["in_process"],
        "config": {"k": 5},
        "token_accounting": {
            "identity": "unspecified",
            "revision": None,
            "scope": "unspecified",
            "method": "unspecified",
        },
        "n_total": 2,
        "n_scored": 1,
    }
    assert report["exclusions"] == [excluded]
    assert json.loads(json.dumps(report))["schema"] == SCHEMA


def test_envelope_redacts_top_level_exclusion_detail(tmp_path):
    dataset = tmp_path / "fixture.jsonl"
    dataset.write_text('{"id":"one"}\n', encoding="utf-8")

    report = report_envelope(
        suite="fixture", dataset_path=dataset, config={"k": 5}, records=[],
        exclusions=[{
            "question_id": "q1", "reason": "invalid", "detail": "private prompt text",
        }],
    )

    assert report["exclusions"] == [{
        "question_id": "q1", "reason": "invalid",
        "detail_sha256": hashlib.sha256(b'"private prompt text"').hexdigest(),
    }]


def test_command_provenance_redacts_explicit_credential_arguments():
    assert redact_command([
        "python", "-m", "runner", "--api-key", "do-not-publish", "--token=value",
    ]) == [
        "python", "-m", "runner", "--api-key", "<redacted>", "--token", "<redacted>",
    ]


def test_command_provenance_redacts_assignment_header_and_url_credentials():
    assert redact_command([
        "API_KEY=super-secret", "--api_key", "also-secret",
        "-H", "Authorization: Bearer another-secret",
        "https://alice:password@example.test/run?access_token=last-secret&format=json",
    ]) == [
        "API_KEY=<redacted>", "--api_key", "<redacted>",
        "-H", "<redacted>",
        "https://<redacted>@example.test/run?access_token=%3Credacted%3E&format=json",
    ]
    assert redact_command([
        "-ualice:password", "-psecret", "--user=alice:password",
        "--header=Authorization: Bearer secret",
    ]) == [
        "-u", "<redacted>", "-p", "<redacted>", "--user", "<redacted>",
        "--header", "<redacted>",
    ]


def test_command_provenance_redacts_compound_credential_assignments():
    assert redact_command([
        "AWS_SECRET_ACCESS_KEY=do-not-publish",
        "AWS_ACCESS_KEY_ID=also-private",
        "HTTP_AUTHORIZATION=Bearer another-secret",
        "--token-budget", "512",
    ]) == [
        "AWS_SECRET_ACCESS_KEY=<redacted>",
        "AWS_ACCESS_KEY_ID=<redacted>",
        "HTTP_AUTHORIZATION=<redacted>",
        "--token-budget", "512",
    ]


def test_command_provenance_redacts_fragment_credentials_without_hiding_normal_options():
    assert redact_command([
        "--token-budget", "512", "--tokenizer-model", "reader-v1",
        "https://example.test/callback#access_token=do-not-publish&state=visible",
    ]) == [
        "--token-budget", "512", "--tokenizer-model", "reader-v1",
        "https://example.test/callback#access_token=%3Credacted%3E&state=visible",
    ]


def test_command_provenance_redacts_embedded_and_signed_url_credentials():
    assert redact_command([
        "DATASET_URL=https://example.test/data?access_token=do-not-publish",
        "--dataset-url=https://example.test/data?X-Amz-Signature=signed&sig=azure",
        "https://example.test/data?signature=generic",
    ]) == [
        "DATASET_URL=https://example.test/data?access_token=%3Credacted%3E",
        "--dataset-url=https://example.test/data?X-Amz-Signature=%3Credacted%3E&sig=%3Credacted%3E",
        "https://example.test/data?signature=%3Credacted%3E",
    ]


def test_command_provenance_redacts_userinfo_when_a_url_port_is_malformed():
    assert redact_command([
        "https://alice:password@example.test:notaport/path?access_token=do-not-publish",
    ]) == [
        "https://<redacted>@example.test:notaport/path?access_token=%3Credacted%3E",
    ]


def test_command_provenance_fails_closed_when_url_splitting_rejects_userinfo():
    assert redact_command(["https://user:password@[invalid/path"]) == ["<redacted>"]


def test_canonical_profile_validator_and_immutable_artifact_writer(tmp_path):
    dataset = tmp_path / "fixture.jsonl"
    dataset.write_text('{"id":"one"}\n', encoding="utf-8")
    profile = json.loads(json.dumps(LONGMEMEVAL_V2_CANONICAL_PROFILE_TEMPLATE))
    profile["benchmark"]["repository_revision"] = "a" * 40
    profile["benchmark"]["dataset_revision"] = "b" * 40
    profile["reader"]["revision"] = "c" * 40
    profile["embedding"]["revision"] = "d" * 40
    profile["baseline_label"] = "full_hybrid"
    config = canonical_benchmark_config(
        run_label="release-candidate", baseline_label="full_hybrid", profile=profile
    )
    report = _complete_canonical_report(dataset, config)
    assert validate_report(report, canonical=True) == []
    dirty = deepcopy(report)
    dirty["system"]["git_dirty"] = True
    assert "canonical reports require a clean git worktree" in validate_report(
        dirty, canonical=True
    )
    artifact = tmp_path / "artifacts" / "run.json"
    written = write_canonical_artifact(report, artifact, canonical=True)
    assert written["sha256"] in artifact.with_name("run.json.sha256").read_text("ascii")
    assert json.loads(artifact.read_text("utf-8"))["schema"] == SCHEMA
    assert write_canonical_artifact(report, artifact, canonical=True) == written
    changed = dict(report)
    changed["records"] = [dict(report["records"][0])]
    changed["records"][0]["latency_ms"] = 2.0
    with pytest.raises(FileExistsError):
        write_canonical_artifact(changed, artifact, canonical=True)


def test_report_validator_recomputes_embedded_config_digest(tmp_path):
    dataset = tmp_path / "fixture.jsonl"
    dataset.write_text('{"id":"one"}\n', encoding="utf-8")
    report = report_envelope(
        suite="fixture", dataset_path=dataset, config={"baseline_label": "full_hybrid"},
        records=[question_record("q1")], git_commit="abc123",
    )
    report["protocol"]["config"]["baseline_label"] = "dense_only"

    errors = validate_report(report)

    assert "system.config_sha256 must match the canonical protocol.config digest" in errors


def test_report_validator_rejects_inconsistent_or_duplicate_exclusions(tmp_path):
    dataset = tmp_path / "fixture.jsonl"
    dataset.write_text('{"id":"one"}\n', encoding="utf-8")
    excluded = {"question_id": "q2", "reason": "no_gold_evidence", "detail": ""}
    report = report_envelope(
        suite="fixture", dataset_path=dataset, config={"k": 5},
        records=[
            question_record("q1"),
            question_record("q2", excluded=excluded),
        ],
        git_commit="abc123",
    )
    assert validate_report(report) == []

    report["exclusions"] = [excluded, excluded]
    errors = validate_report(report)
    assert "exclusion question_id values must be unique" in errors

    report["exclusions"] = []
    errors = validate_report(report)
    assert "top-level exclusions must exactly match per-record exclusions" in errors


def test_default_canonical_profile_is_pinned_and_rejects_mutable_revisions(tmp_path):
    dataset = tmp_path / "fixture.jsonl"
    dataset.write_text('{"id":"one"}\n', encoding="utf-8")
    config = canonical_benchmark_config(
        run_label="release-candidate", baseline_label="full_hybrid"
    )
    report = _complete_canonical_report(dataset, config)
    assert validate_report(report, canonical=True) == []
    assert all(
        len(value) == 40
        for value in (
            config["canonical_profile"]["benchmark"]["repository_revision"],
            config["canonical_profile"]["benchmark"]["dataset_revision"],
            config["canonical_profile"]["reader"]["revision"],
            config["canonical_profile"]["embedding"]["revision"],
        )
    )
    assert config["token_budgets"] == list(CANONICAL_TOKEN_BUDGETS)

    config["canonical_profile"]["reader"]["revision"] = "main"
    errors = validate_report(report, canonical=True)
    assert any("reader.revision" in error and "immutable" in error for error in errors)


def test_canonical_validator_rejects_unpinned_commit_private_prompts_and_unlabeled_measurements(tmp_path):
    dataset = tmp_path / "fixture.jsonl"
    dataset.write_text('{"id":"one"}\n', encoding="utf-8")
    config = canonical_benchmark_config(
        run_label="release-candidate", baseline_label="full_hybrid"
    )
    report = _complete_canonical_report(dataset, config)
    report["system"]["git_commit"] = "not-a-commit"
    report["records"][0]["q"] = "private source question"
    report["records"][0]["question_sha256"] = "a" * 64
    report["records"][0].pop("context_token_method")
    report["metrics"].pop("recall_at_10")

    errors = validate_report(report, canonical=True)

    assert any("git_commit" in error for error in errors)
    assert "canonical records must not contain raw query text" in errors
    assert "canonical records must not contain question-derived hashes" in errors
    assert any("context_token_method" in error for error in errors)
    assert any("metrics.recall_at_10" in error for error in errors)

    config["canonical_profile"]["reader"]["revision"] = "C" * 40
    errors = validate_report(report, canonical=True)
    assert any("reader.revision" in error and "immutable" in error for error in errors)


def test_canonical_validator_requires_grounded_metrics_or_explicit_unavailability(tmp_path):
    dataset = tmp_path / "fixture.jsonl"
    dataset.write_text('{"id":"one"}\n', encoding="utf-8")
    config = canonical_benchmark_config(
        run_label="release-candidate", baseline_label="full_hybrid"
    )
    report = _complete_canonical_report(dataset, config)
    report["metrics"].pop("grounded_f1")
    report["metrics"]["abstention_f1"] = {"available": False}

    errors = validate_report(report, canonical=True)

    assert any("grounded_f1" in error and "unavailable reason" in error for error in errors)
    assert any("abstention_f1" in error and "unavailable reason" in error for error in errors)


def test_canonical_validator_requires_measured_rows_for_every_fixed_budget(tmp_path):
    dataset = tmp_path / "fixture.jsonl"
    dataset.write_text('{"id":"one"}\n', encoding="utf-8")
    config = canonical_benchmark_config(
        run_label="release-candidate", baseline_label="full_hybrid"
    )
    report = _complete_canonical_report(dataset, config)
    report["metrics"]["fixed_budget_curve"]["rows"].pop()

    errors = validate_report(report, canonical=True)

    assert "canonical fixed-budget curve must contain every canonical token budget" in errors
    report["metrics"]["fixed_budget_curve"] = {"available": False, "reason": "not_run"}
    errors = validate_report(report, canonical=True)
    assert "canonical fixed-budget curve is unavailable and cannot qualify as evidence" in errors

    report = _complete_canonical_report(dataset, config)
    report["metrics"]["fixed_budget_curve"]["rows"][0]["records"][0]["excluded"] = True
    errors = validate_report(report, canonical=True)
    assert "canonical fixed-budget curve 256 records must preserve exclusion state" in errors


def test_canonical_validator_requires_complete_dataset_cardinality(tmp_path):
    dataset = tmp_path / "fixture.jsonl"
    dataset.write_text('{"id":"one"}\n', encoding="utf-8")
    config = canonical_benchmark_config(
        run_label="release-candidate", baseline_label="full_hybrid"
    )
    valid = _complete_canonical_report(dataset, config)
    assert validate_report(valid, canonical=True) == []

    missing_complete = deepcopy(valid)
    missing_complete["protocol"].pop("complete_dataset")
    assert "canonical protocol.complete_dataset must be true" in validate_report(
        missing_complete, canonical=True
    )

    for invalid_count in (True, 0, 2):
        mismatched = deepcopy(valid)
        mismatched["protocol"]["source_questions"] = invalid_count
        errors = validate_report(mismatched, canonical=True)
        assert any("protocol.source_questions" in error for error in errors)


def test_canonical_validator_rejects_invalid_numeric_and_token_accounting(tmp_path):
    dataset = tmp_path / "fixture.jsonl"
    dataset.write_text('{"id":"one"}\n', encoding="utf-8")
    config = canonical_benchmark_config(
        run_label="release-candidate", baseline_label="full_hybrid"
    )
    config["token_budget"] = 4
    valid = _complete_canonical_report(dataset, config)
    valid["records"][0]["usage"] = {
        "budget_tokens": 4,
        "context_tokens": 3,
        "token_counter": valid["records"][0]["context_tokenizer_identity"],
    }
    assert validate_report(valid, canonical=True) == []

    mutations = (
        (("metrics", "recall_at_1"), True, "metrics.recall_at_1"),
        (("records", 0, "recall_at_1"), True, "records require recall_at_1"),
        (("records", 0, "latency_ms"), float("inf"), "latency_ms"),
        (("records", 0, "context_tokens"), float("nan"), "context_tokens"),
        (("records", 0, "context_tokens"), -1, "context_tokens"),
        (("records", 0, "context_tokens"), 5, "must not exceed protocol token_budget"),
        (
            ("records", 0, "usage", "context_tokens"),
            5,
            "usage.context_tokens must not exceed usage.budget_tokens",
        ),
        (
            ("records", 0, "usage", "budget_tokens"),
            5,
            "usage.budget_tokens must equal protocol token_budget",
        ),
        (
            ("records", 0, "usage", "source_tokens"),
            True,
            "usage.source_tokens must be non-negative and finite",
        ),
        (
            ("records", 0, "usage", "savings_ratio"),
            float("inf"),
            "usage.savings_ratio must be a number in [0, 1]",
        ),
        (
            ("metrics", "fixed_budget_curve", "rows", 0, "recall_at_1"),
            True,
            "fixed-budget curve 256 requires recall_at_1",
        ),
        (
            ("metrics", "fixed_budget_curve", "rows", 0, "records", 0, "context_tokens"),
            257,
            "context_tokens within budget",
        ),
    )
    for path, value, expected in mutations:
        report = deepcopy(valid)
        target = report
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        errors = validate_report(report, canonical=True)
        assert any(expected in error for error in errors), (path, errors)


def test_canonical_validator_rejects_tampered_confidence_intervals(tmp_path):
    dataset = tmp_path / "fixture.jsonl"
    dataset.write_text('{"id":"one"}\n', encoding="utf-8")
    config = canonical_benchmark_config(
        run_label="release-candidate", baseline_label="full_hybrid"
    )
    valid = _complete_canonical_report(dataset, config)
    assert validate_report(valid, canonical=True) == []

    mutations = (
        ("point", float("nan"), "point/low/high must be finite"),
        ("low", -0.1, "point/low/high must be finite"),
        ("high", 1.1, "point/low/high must be finite"),
        ("high", 0.5, "low <= point <= high"),
        ("point", 0.5, ".point must match metrics.recall_at_1"),
        ("n", 2, ".n must equal the non-excluded record count"),
        ("seed", -1, ".seed must be a non-negative integer"),
        ("iterations", 0, ".iterations must be a positive integer"),
        ("iterations", -1, ".iterations must be a positive integer"),
        ("iterations", True, ".iterations must be a positive integer"),
        ("strata_key", "topic", ".strata_key must equal category"),
    )
    for key, value, expected in mutations:
        report = deepcopy(valid)
        report["metrics"]["confidence_intervals"]["recall_at_1"][key] = value
        errors = validate_report(report, canonical=True)
        assert any(expected in error for error in errors), (key, value, errors)

    extra = deepcopy(valid)
    extra["metrics"]["confidence_intervals"]["recall_at_1"]["mean"] = 1.0
    errors = validate_report(extra, canonical=True)
    assert any("must match the canonical confidence interval schema" in error for error in errors)

    missing = deepcopy(valid)
    missing["metrics"]["confidence_intervals"].pop("recall_at_1")
    errors = validate_report(missing, canonical=True)
    assert (
        "canonical metrics.confidence_intervals must exactly cover every rank metric"
        in errors
    )


def test_canonical_validator_rejects_tampered_paired_bootstrap_payloads(tmp_path):
    dataset = tmp_path / "fixture.jsonl"
    dataset.write_text('{"id":"one"}\n', encoding="utf-8")
    config = canonical_benchmark_config(
        run_label="release-candidate", baseline_label="full_hybrid"
    )
    valid = _complete_canonical_report(dataset, config)

    unavailable_mutations = (
        ("reason", "", ".reason must be a non-empty string"),
        ("n", 1, ".n must be zero when unavailable"),
        ("delta", 0.0, "delta/low/high must be null when unavailable"),
        ("iterations", 0, ".iterations must be a positive integer"),
        ("iterations", True, ".iterations must be a positive integer"),
    )
    for key, value, expected in unavailable_mutations:
        report = deepcopy(valid)
        report["metrics"]["paired_bootstrap"][key] = value
        errors = validate_report(report, canonical=True)
        assert any(expected in error for error in errors), (key, value, errors)

    available = deepcopy(valid)
    available["metrics"]["paired_bootstrap"] = {
        "available": True,
        "metric": "recall_at_5",
        "delta": 0.25,
        "low": 0.0,
        "high": 0.5,
        "n": 1,
        "seed": 20260729,
        "iterations": 20,
    }
    assert validate_report(available, canonical=True) == []

    available_mutations = (
        ("metric", "recall_at_3", ".metric must name a canonical rank metric"),
        ("delta", float("inf"), "delta/low/high must be finite"),
        ("delta", 1.1, "delta/low/high must be finite"),
        ("low", -1.1, "delta/low/high must be finite"),
        ("high", 1.1, "delta/low/high must be finite"),
        ("low", 0.3, "low <= delta <= high"),
        ("n", 0, ".n must equal the positive non-excluded record count"),
        ("n", 2, ".n must equal the positive non-excluded record count"),
        ("seed", -1, ".seed must be a non-negative integer"),
        ("iterations", 0, ".iterations must be a positive integer"),
        ("iterations", True, ".iterations must be a positive integer"),
    )
    for key, value, expected in available_mutations:
        report = deepcopy(available)
        report["metrics"]["paired_bootstrap"][key] = value
        errors = validate_report(report, canonical=True)
        assert any(expected in error for error in errors), (key, value, errors)

    extra = deepcopy(available)
    extra["metrics"]["paired_bootstrap"]["mean"] = 0.25
    errors = validate_report(extra, canonical=True)
    assert any("available payload must match the canonical schema" in error for error in errors)


def test_canonical_validator_recomputes_all_rank_aggregates_from_record_ids(tmp_path):
    dataset = tmp_path / "fixture.jsonl"
    dataset.write_text('{"id":"one"}\n', encoding="utf-8")
    config = canonical_benchmark_config(
        run_label="release-candidate", baseline_label="full_hybrid"
    )
    valid = _complete_canonical_report(dataset, config)

    top_level = deepcopy(valid)
    top_level["metrics"]["recall_at_5"] = 0.5
    errors = validate_report(top_level, canonical=True)
    assert (
        "canonical metrics.recall_at_5 must equal the non-excluded record mean"
        in errors
    )

    curve_aggregate = deepcopy(valid)
    curve_aggregate["metrics"]["fixed_budget_curve"]["rows"][0]["ndcg_at_10"] = 0.5
    errors = validate_report(curve_aggregate, canonical=True)
    assert any(
        "fixed-budget curve 256 ndcg_at_10" in error
        and "non-excluded record mean" in error
        for error in errors
    )

    curve_measurement = deepcopy(valid)
    measurement = curve_measurement["metrics"]["fixed_budget_curve"]["rows"][0]["records"][0]
    measurement["retrieved_ids"] = []
    errors = validate_report(curve_measurement, canonical=True)
    assert any(
        "fixed-budget curve 256 record recall_at_1" in error
        and "retrieved_ids and supporting_ids" in error
        for error in errors
    )


def test_canonical_validator_derives_numeric_grounded_metrics_from_labels(tmp_path):
    dataset = tmp_path / "fixture.jsonl"
    dataset.write_text('{"id":"one"}\n', encoding="utf-8")
    config = canonical_benchmark_config(
        run_label="release-candidate", baseline_label="full_hybrid"
    )

    unlabeled = _complete_canonical_report(dataset, config)
    unlabeled["metrics"]["grounded_f1"] = 0.75
    unlabeled["metrics"]["abstention_f1"] = 0.75
    errors = validate_report(unlabeled, canonical=True)
    assert any(
        "metrics.grounded_f1 requires labeled per-question grounded values" in error
        and "unavailable reason" in error
        for error in errors
    )
    assert any(
        "metrics.abstention_f1 requires labeled per-question abstained values" in error
        and "unavailable reason" in error
        for error in errors
    )

    measured = _complete_canonical_report(dataset, config)
    measured["records"][0].update({
        "answerable": True,
        "grounded": True,
        "abstained": False,
    })
    measured["metrics"]["grounded"] = {
        "available": True,
        **metrics.grounded_precision_recall_f1([True], [True]),
    }
    measured["metrics"]["abstention"] = {
        "available": True,
        **metrics.abstention_precision_recall_f1([False], [True]),
    }
    measured["metrics"]["grounded_f1"] = 1.0
    measured["metrics"]["abstention_f1"] = 1.0
    assert validate_report(measured, canonical=True) == []

    bad_count = deepcopy(measured)
    bad_count["metrics"]["grounded"]["n"] = 2
    errors = validate_report(bad_count, canonical=True)
    assert (
        "canonical metrics.grounded.n must be recomputed from per-question labels"
        in errors
    )

    measured["metrics"]["grounded_f1"] = 0.0
    errors = validate_report(measured, canonical=True)
    assert (
        "canonical metrics.grounded_f1 must be recomputed from per-question labels"
        in errors
    )


def test_canonical_validator_requires_pinned_reader_tokenizer_identity(tmp_path):
    dataset = tmp_path / "fixture.jsonl"
    dataset.write_text('{"id":"one"}\n', encoding="utf-8")
    config = canonical_benchmark_config(
        run_label="release-candidate", baseline_label="full_hybrid"
    )
    valid = _complete_canonical_report(dataset, config)

    estimated = deepcopy(valid)
    estimated["records"][0]["context_token_method"] = "deterministic_estimate"
    estimated["metrics"]["fixed_budget_curve"]["rows"][0]["records"][0][
        "context_token_method"
    ] = "deterministic_estimate"
    errors = validate_report(estimated, canonical=True)
    assert any(
        "context_token_method=pinned_reader_content_tokenizer" in error
        for error in errors
    )
    assert any(
        "fixed-budget curve 256 records require" in error
        and "context_token_method=pinned_reader_content_tokenizer" in error
        for error in errors
    )

    mismatched = deepcopy(valid)
    mismatched["records"][0]["context_tokenizer_identity"] = "other/model@" + "e" * 40
    mismatched["records"][0]["usage"]["token_counter"] = "other/model@" + "e" * 40
    errors = validate_report(mismatched, canonical=True)
    assert any("context_tokenizer_identity must match" in error for error in errors)
    assert any("usage.token_counter must match" in error for error in errors)


def test_benchmark_cli_writes_canonical_json_and_checksum(tmp_path, capsys):
    dataset = tmp_path / "fixture.jsonl"
    dataset.write_text('{"id":"one"}\n', encoding="utf-8")
    report = report_envelope(
        suite="fixture", dataset_path=dataset, config={"k": 5},
        records=[question_record("q1")], git_commit="abc123",
    )
    source = tmp_path / "source.json"
    source.write_text(json.dumps(report), encoding="utf-8")
    artifact = tmp_path / "artifact.json"
    assert main(["--input", str(source), "--output", str(artifact)]) == 0
    assert artifact.exists() and artifact.with_name("artifact.json.sha256").exists()
    assert "sha256" in capsys.readouterr().out


def test_exact_tokenizer_fallback_budget_curves_and_deterministic_cis():
    assert count_tokens("abc", CharacterTokenizer()) == {"tokens": 3, "method": "injected"}
    assert count_tokens("one two")["method"] == "deterministic_estimate"
    records = [
        {"category": "a", "supporting_ids": ["m1"], "chunks": [
            {"id": "m1", "tokens": 3}, {"id": "m2", "tokens": 3}
        ]},
        {"category": "b", "supporting_ids": ["m2"], "chunks": [
            {"id": "m1", "tokens": 3}, {"id": "m2", "tokens": 3}
        ]},
    ]
    curve = fixed_budget_curve(records, [3, 6])
    assert curve[0]["recall"] == 0.5
    assert curve[1]["recall"] == 1.0
    def metric(rows):
        return sum(row["value"] for row in rows) / len(rows)
    ci_one = stratified_bootstrap_ci(
        [{"category": "a", "value": 1.0}, {"category": "b", "value": 0.0}],
        metric, iterations=40, seed=4,
    )
    ci_two = stratified_bootstrap_ci(
        [{"category": "a", "value": 1.0}, {"category": "b", "value": 0.0}],
        metric, iterations=40, seed=4,
    )
    assert ci_one == ci_two
    paired = paired_bootstrap_ci([(1.0, 0.0), (0.0, 0.0)], iterations=40, seed=4)
    assert paired["delta"] == 0.5 and paired["n"] == 2
