"""Offline contracts for aggregate-only hosted benchmark evidence."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pytest

from eval.hosted_evidence import (
    aggregate_reports,
    build_public_evidence,
    canonical_json,
    dataset_provenance,
    paired_bootstrap_95,
    public_json,
    repository_provenance,
)


def _row(task_id, *, completed, mistake, tokens, latency, cached=2):
    return {
        "task_id": task_id,
        "completed": completed,
        "first_attempt_error": mistake,
        "wrong_answer": mistake,
        "correction_attempted": mistake,
        "memory_calls": 0,
        "agent_turns": 2 if mistake else 1,
        "prompt": "private prompt must not escape",
        "answer": "private answer must not escape",
        "context": "private context must not escape",
        "provider": {
            "input_tokens": tokens - 10,
            "cached_input_tokens": cached,
            "output_tokens": 10,
            "reasoning_output_tokens": 3,
            "total_tokens": tokens,
            "latency_ms": latency,
        },
    }


def _report(repetition=0):
    base = 100 + repetition
    return {
        "detail": {
            "full_history": [
                _row("private-task-a", completed=True, mistake=False, tokens=base, latency=1000),
                _row("private-task-b", completed=False, mistake=True, tokens=base + 100, latency=2000),
            ],
            "retrieval": [
                _row("private-task-a", completed=True, mistake=False, tokens=base - 30, latency=800),
                _row("private-task-b", completed=True, mistake=False, tokens=base + 20, latency=1500),
            ],
            "adaptive": [
                _row("private-task-a", completed=True, mistake=False, tokens=base - 10, latency=900),
                _row("private-task-b", completed=False, mistake=True, tokens=base + 5, latency=1700),
            ],
        }
    }


def test_aggregate_is_paired_deterministic_and_content_free():
    report = aggregate_reports([_report(), {"private": _report(1), "public": {"methods": {}}}],
                               iterations=40, seed=7)

    assert report["repetitions"] == 2
    assert report["strategies"]["full_history"]["observations"] == 4
    assert report["strategies"]["retrieval"]["completion_rate"] == 1.0
    assert report["strategies"]["retrieval"]["usage_coverage"]["total_tokens"]["rate"] == 1.0
    delta = report["paired_bootstrap"]["retrieval"]
    assert delta["completion_rate"]["delta"] == 0.5
    assert delta["completion_rate"]["n"] == 2
    assert "median_delta" in delta["completion_rate"]
    assert delta["total_tokens"]["delta"] < 0
    assert delta["latency_ms"]["delta"] < 0
    assert report["strategies"]["retrieval"]["provider_usage_median"]["total_tokens"] is not None
    text = canonical_json(report)
    assert "private-task" not in text
    assert "private prompt" not in text
    assert "private answer" not in text
    assert "private context" not in text


def test_required_provider_counter_missing_fails_closed():
    report = _report()
    report["detail"]["retrieval"][0]["provider"]["total_tokens"] = None

    with pytest.raises(ValueError, match="required provider usage counters are missing"):
        aggregate_reports([report], iterations=10)


def test_pairing_rejects_a_missing_private_task_without_disclosing_it():
    report = _report()
    report["detail"]["adaptive"].pop()

    with pytest.raises(ValueError, match="matched task IDs"):
        aggregate_reports([report], iterations=10)


def test_bootstrap_is_deterministic_and_is_a_95_percent_interval():
    pairs = [(1.0, 0.0), (0.0, 0.0), (1.0, 1.0)]
    first = paired_bootstrap_95(pairs, iterations=80, seed=4)
    second = paired_bootstrap_95(pairs, iterations=80, seed=4)

    assert first == second
    assert first["confidence_level"] == 0.95
    assert first["low"] <= first["delta"] <= first["high"]


def test_repeated_tasks_are_resampled_as_clusters():
    report = aggregate_reports([_report(0), _report(1), _report(2)], iterations=80, seed=4)

    interval = report["paired_bootstrap"]["retrieval"]["completion_rate"]
    assert interval["n"] == 2
    assert interval["delta"] == 0.5
    assert interval["low"] == 0.0
    assert interval["high"] == 1.0


def test_repetitions_require_the_same_task_clusters():
    later = _report(1)
    for rows in later["detail"].values():
        rows.pop()

    with pytest.raises(ValueError, match="same task IDs"):
        aggregate_reports([_report(), later], iterations=10)


def test_public_artifact_has_provenance_checksum_and_no_private_content(tmp_path, monkeypatch):
    dataset = tmp_path / "private-dataset.jsonl"
    dataset.write_text('{"prompt":"do not disclose"}\n', encoding="utf-8")
    monkeypatch.setattr(
        "eval.hosted_evidence.repository_provenance",
        lambda _: {"commit": "a" * 40, "dirty": False, "dirty_patch_sha256": "b" * 64},
    )
    monkeypatch.setattr(
        "eval.hosted_evidence.environment_provenance",
        lambda: {"python": "3.11.0", "implementation": "CPython", "platform": "test", "openai_codex": "0.144.4"},
    )
    evidence = build_public_evidence(
        [_report()], dataset_path=dataset, config={"model": "gpt-5.6-luna", "secret": "not-public"},
        repo_path=tmp_path, iterations=20, timestamp=datetime(2026, 7, 31, tzinfo=timezone.utc),
    )

    assert evidence["created_at"] == "2026-07-31T00:00:00Z"
    assert evidence["provenance"]["dataset"]["sha256"] == hashlib.sha256(dataset.read_bytes()).hexdigest()
    assert "private-dataset" not in canonical_json(evidence)
    assert "not-public" not in canonical_json(evidence)
    assert "private-task" not in canonical_json(evidence)
    encoded = public_json(evidence)
    assert json.loads(encoded)["sha256"] == evidence["sha256"]
    evidence["baseline"] = "tampered"
    with pytest.raises(ValueError, match="checksum"):
        public_json(evidence)


def test_public_serializer_refuses_task_level_fields_even_with_a_valid_checksum(tmp_path, monkeypatch):
    dataset = tmp_path / "dataset.jsonl"
    dataset.write_bytes(b"[]")
    monkeypatch.setattr(
        "eval.hosted_evidence.repository_provenance",
        lambda _: {"commit": "a" * 40, "dirty": False, "dirty_patch_sha256": "b" * 64},
    )
    evidence = build_public_evidence(
        [_report()], dataset_path=dataset, config={}, repo_path=tmp_path, iterations=5,
        timestamp=datetime(2026, 7, 31, tzinfo=timezone.utc),
    )
    evidence["detail"] = "private answer"
    unsigned = dict(evidence)
    unsigned.pop("sha256")
    evidence["sha256"] = hashlib.sha256(canonical_json(unsigned).encode("utf-8")).hexdigest()
    with pytest.raises(ValueError, match="unexpected top-level"):
        public_json(evidence)


def test_public_serializer_rejects_nested_content_fields_with_a_valid_checksum(
    tmp_path, monkeypatch,
):
    dataset = tmp_path / "dataset.jsonl"
    dataset.write_bytes(b"[]")
    monkeypatch.setattr(
        "eval.hosted_evidence.repository_provenance",
        lambda _: {"commit": "a" * 40, "dirty": False, "dirty_patch_sha256": "b" * 64},
    )
    evidence = build_public_evidence(
        [_report()],
        dataset_path=dataset,
        config={},
        repo_path=tmp_path,
        iterations=5,
        timestamp=datetime(2026, 7, 31, tzinfo=timezone.utc),
    )
    evidence["experiment"]["question"] = "private question"
    unsigned = dict(evidence)
    unsigned.pop("sha256")
    evidence["sha256"] = hashlib.sha256(
        canonical_json(unsigned).encode("utf-8")
    ).hexdigest()
    with pytest.raises(ValueError, match="unexpected experiment"):
        public_json(evidence)


def test_public_serializer_rejects_arbitrary_nested_provenance(tmp_path, monkeypatch):
    dataset = tmp_path / "dataset.jsonl"
    dataset.write_bytes(b"[]")
    monkeypatch.setattr(
        "eval.hosted_evidence.repository_provenance",
        lambda _: {"commit": "a" * 40, "dirty": False, "dirty_patch_sha256": "b" * 64},
    )
    evidence = build_public_evidence(
        [_report()],
        dataset_path=dataset,
        config={},
        repo_path=tmp_path,
        iterations=5,
        timestamp=datetime(2026, 7, 31, tzinfo=timezone.utc),
    )
    evidence["provenance"]["environment"]["secret"] = "must not publish"
    unsigned = dict(evidence)
    unsigned.pop("sha256")
    evidence["sha256"] = hashlib.sha256(
        canonical_json(unsigned).encode("utf-8")
    ).hexdigest()
    with pytest.raises(ValueError, match="environment provenance"):
        public_json(evidence)


def test_dataset_and_repo_fingerprints_are_content_only(tmp_path):
    dataset = tmp_path / "dataset.jsonl"
    dataset.write_bytes(b"private bytes")
    assert dataset_provenance(dataset) == {
        "sha256": hashlib.sha256(b"private bytes").hexdigest(), "bytes": 13,
    }
    provenance = repository_provenance(tmp_path)
    assert set(provenance) == {"commit", "dirty", "dirty_patch_sha256"}
    assert len(provenance["dirty_patch_sha256"]) == 64
