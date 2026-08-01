from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from eval.public_readiness import (
    _main,
    assert_manifest_ready,
    assert_public_ready,
    validate_manifest,
    validate_public_readiness,
)
from eval.hosted_evidence import build_public_evidence


def _artifact() -> dict:
    config = {"measurement_scope": "retrieval_only", "token_budget": 1024}
    config_hash = hashlib.sha256(b'{"measurement_scope":"retrieval_only","token_budget":1024}').hexdigest()
    return {
        "schema": "engraphis-benchmark/v2",
        "suite": {"name": "fixture", "dataset": "questions.json", "sha256": "a" * 64},
        "system": {"git_commit": "commit-123", "config_sha256": config_hash},
        "environment": {
            "python": "3.11.0",
            "implementation": "CPython",
            "platform": "test",
            "machine": "test-machine",
        },
        "protocol": {
            "command": ["python", "-m", "eval.harness"],
            "config": config,
            "token_accounting": {
                "identity": "regex-v1",
                "scope": "memory-content",
                "method": "deterministic",
            },
            "n_total": 1,
            "n_scored": 1,
        },
        "privacy": {"raw_query_policy": "redacted_sha256"},
        "metrics": {"evidence_hit_rate": 0.9},
        "records": [{"question_id": "q1"}],
    }


def _claim(artifact: dict, text: str, **overrides: object) -> dict:
    claim = {
        "text": text,
        "evidence_scope": "retrieval_only",
        "metrics": ["evidence_hit_rate"],
        "provenance": {
            "schema": artifact["schema"],
            "dataset": artifact["suite"]["dataset"],
            "dataset_sha256": artifact["suite"]["sha256"],
            "git_commit": artifact["system"]["git_commit"],
            "config_sha256": artifact["system"]["config_sha256"],
        },
    }
    claim.update(overrides)
    return claim


def _manifest() -> dict:
    return {
        "schema": "engraphis-public-benchmark-series/v1",
        "source": {"git_commit": "b" * 40, "git_dirty": False},
        "benchmark": {
            "baselines": [
                "no_retrieval",
                "lexical_only",
                "dense_only",
                "dense_lexical_rrf",
                "full_hybrid",
                "full_history",
                "no_graph",
                "no_reranker",
                "no_temporal_resolution",
                "whole_document",
            ],
            "token_budgets": [256, 512, 1024, 2048, 4096],
            "holdout": True,
        },
        "profile": {
            "benchmark": {
                "repository_revision": "c" * 40,
                "dataset_revision": "d" * 40,
            },
            "reader": {"revision": "e" * 40},
            "embedding": {"revision": "f" * 40},
            "token_budgets": [256, 512, 1024, 2048, 4096],
        },
        "artifacts": {"private": "results/private.jsonl", "public": "artifacts/public.json"},
    }


def test_valid_retrieval_artifact_and_claim_pass() -> None:
    artifact = _artifact()
    claim = _claim(artifact, "Evidence hit rate was 90% at a fixed budget.")

    assert validate_public_readiness(artifact, [claim]) == []
    assert_public_ready(artifact, [claim])


def test_hosted_evidence_routes_to_its_strict_schema_validator(tmp_path: Path) -> None:
    dataset = tmp_path / "dataset.jsonl"
    dataset.write_text('{"id":"fixture"}\n', encoding="utf-8")
    row = {
        "task_id": "private-task",
        "completed": True,
        "first_attempt_error": False,
        "wrong_answer": False,
        "correction_attempted": False,
        "memory_calls": 0,
        "agent_turns": 1,
        "provider": {
            "input_tokens": 5,
            "cached_input_tokens": 0,
            "output_tokens": 1,
            "reasoning_output_tokens": 0,
            "total_tokens": 6,
            "latency_ms": 1.0,
        },
    }
    report = {"detail": {name: [dict(row)] for name in (
        "full_history", "retrieval", "adaptive",
    )}}
    evidence = build_public_evidence(
        [report],
        dataset_path=dataset,
        config={"stage": "full", "model": "fixture"},
        repo_path=tmp_path,
        iterations=5,
        timestamp=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )

    assert validate_public_readiness(evidence) == []
    evidence["sha256"] = "0" * 64
    assert "checksum" in " ".join(validate_public_readiness(evidence))


def test_hosted_evidence_rejects_untyped_public_claims() -> None:
    artifact = {"schema": "engraphis-hosted-evidence/v1"}

    errors = validate_public_readiness(artifact, [{"text": "unsupported"}])

    assert any(
        "hosted evidence claims require a hosted claim schema" in error
        for error in errors
    )


def test_missing_provenance_and_scope_fail_closed() -> None:
    artifact = _artifact()
    del artifact["system"]["config_sha256"]
    artifact["protocol"]["config"].pop("measurement_scope")
    errors = validate_public_readiness(artifact)

    assert "artifact.system.config_sha256 must be a lowercase SHA-256 digest" in errors
    assert any("measurement scope" in error for error in errors)


def test_config_digest_is_recomputed_and_scored_count_is_bounded() -> None:
    artifact = _artifact()
    artifact["protocol"]["config"]["token_budget"] = 2048
    artifact["protocol"]["n_scored"] = 2

    errors = validate_public_readiness(artifact)

    assert "artifact.system.config_sha256 must match artifact.protocol.config" in errors
    assert (
        "artifact.protocol.n_scored must not exceed artifact.protocol.n_total" in errors
    )


def test_raw_content_and_credential_fields_fail_publication_guard() -> None:
    artifact = _artifact()
    artifact["records"][0]["question"] = "private source question"
    artifact["protocol"]["config"]["api_key"] = "must-not-publish"
    artifact["system"]["config_sha256"] = hashlib.sha256(
        json.dumps(
            artifact["protocol"]["config"],
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    errors = validate_public_readiness(artifact)

    assert "artifact.records[0].question must not contain raw benchmark content" in errors
    assert "artifact.protocol.config.api_key must not contain credential material" in errors


def test_retrieval_only_claim_cannot_overstate_answer_quality_cost_or_latency() -> None:
    artifact = _artifact()
    claim = _claim(artifact, "This makes answers more accurate, faster, and cheaper.")

    errors = validate_public_readiness(artifact, [claim])

    assert errors == [
        "claims[0]: retrieval_only claims cannot assert answer quality, task outcomes, cost, or latency"
    ]


def test_limitation_can_name_unmeasured_boundaries() -> None:
    artifact = _artifact()
    claim = _claim(
        artifact,
        "This retrieval-only result does not measure answer quality, cost, or latency.",
        claim_kind="limitation",
    )

    assert validate_public_readiness(artifact, [claim]) == []


def test_claim_provenance_and_metric_must_match_artifact() -> None:
    artifact = _artifact()
    claim = _claim(
        artifact,
        "Evidence hit rate was measured.",
        metrics=["recall_at_5"],
        provenance={"schema": artifact["schema"]},
    )

    errors = validate_public_readiness(artifact, [claim])

    assert "claims[0]: claim.provenance.dataset must match the artifact" in errors
    assert "claims[0]: claim.provenance.config_sha256 must match the artifact" in errors
    assert "claims[0]: claim metric is absent from artifact.metrics: recall_at_5" in errors


def test_claim_scope_must_match_artifact() -> None:
    artifact = _artifact()
    claim = _claim(artifact, "The complete task succeeded.", evidence_scope="end_to_end")

    errors = validate_public_readiness(artifact, [claim])

    assert "claims[0]: claim.evidence_scope must match the artifact measurement scope" in errors


def test_assert_public_ready_reports_all_errors() -> None:
    with pytest.raises(ValueError, match="public benchmark readiness failed"):
        assert_public_ready(_artifact(), [{"text": "incomplete claim"}])


def test_valid_manifest_passes_and_assertion_is_backward_independent() -> None:
    manifest = _manifest()

    assert validate_manifest(manifest) == []
    assert_manifest_ready(manifest)


def test_manifest_requires_clean_source_holdout_matrix_baselines_and_paths() -> None:
    manifest = _manifest()
    manifest["source"]["git_dirty"] = True
    manifest["benchmark"]["holdout"] = False
    manifest["benchmark"]["baselines"] = ["dense_only"]
    manifest["benchmark"]["token_budgets"] = [1024]
    manifest["artifacts"]["public"] = ""

    errors = validate_manifest(manifest)

    assert "manifest.source.git_dirty must be false" in errors
    assert "manifest.benchmark.holdout must be true" in errors
    assert "manifest.benchmark.token_budgets must be the canonical fixed budgets" in errors
    assert "manifest.benchmark.baselines is missing required baseline: full_hybrid" in errors
    assert "manifest.artifacts.public must be an explicit non-empty path" in errors


def test_manifest_rejects_mutable_revisions_duplicate_baselines_and_shared_paths() -> None:
    manifest = _manifest()
    manifest["source"]["git_commit"] = "main"
    manifest["profile"]["reader"]["revision"] = "latest"
    manifest["benchmark"]["baselines"].append("dense_only")
    manifest["artifacts"]["public"] = manifest["artifacts"]["private"]

    errors = validate_manifest(manifest)

    assert "manifest.source.git_commit must be an immutable lowercase 40-character commit" in errors
    assert "manifest.profile.reader.revision must be an immutable lowercase 40-character revision" in errors
    assert "manifest.benchmark.baselines must not contain duplicates" in errors
    assert "manifest.artifacts.private and public paths must differ" in errors


def test_manifest_is_fail_closed_for_non_objects_and_assertion_reports_errors() -> None:
    assert validate_manifest(None) == ["manifest must be an object"]

    with pytest.raises(ValueError, match="public benchmark series validation failed"):
        assert_manifest_ready({})


def test_cli_accepts_a_series_without_an_artifact(tmp_path) -> None:
    path = tmp_path / "series.json"
    path.write_text(json.dumps(_manifest()), encoding="utf-8")

    assert _main(["--series", str(path)]) == 0
