import hashlib
import json
from pathlib import Path

import pytest

from eval import performance
from scripts.render_benchmark_report import (
    _normalize_chunking,
    _normalize_performance,
    load_report,
    render_report,
)


def _measurement() -> performance._Measurements:
    return performance._Measurements(
        cold_latencies_ms=[3.0],
        warm_latencies_ms=[2.0, 2.5],
        context_tokens=[12, 14],
        source_tokens=[20, 22],
        full_payload_tokens=[40, 44],
        compact_payload_tokens=[21, 23],
        candidate_depths=[5, 5],
        quality=[
            {
                "question": 0,
                "recall_at_k": 1.0,
                "hit_at_k": 1.0,
                "answer_token_recall": 0.75,
                "packed_recall_at_k": 0.5,
                "packed_hit_at_k": 1.0,
                "packed_answer_token_recall": 0.5,
            },
            {
                "question": 1,
                "recall_at_k": 0.5,
                "hit_at_k": 0.0,
                "answer_token_recall": 0.25,
                "packed_recall_at_k": 0.0,
                "packed_hit_at_k": 0.0,
                "packed_answer_token_recall": 0.0,
            },
        ],
    )


def _base() -> dict:
    return {
        "environment": {
            "backend_configuration": {"mode": "fixture"},
        },
        "corpus": {"dataset_cases": 1, "memories": 2, "questions": 2},
    }


def _resources() -> list[dict]:
    return [
        {
            "pid": 123,
            "rss_bytes": 100,
            "storage_bytes": 200,
            "startup_ms": 1.0,
            "ingestion_ms": 2.0,
            "populated_reopen_ms": None,
        }
    ]


def _report() -> dict:
    return performance._build_report(
        _base(),
        [_measurement()],
        k=5,
        candidate_k=50,
        candidate_depth="fixed",
        warmups=1,
        iterations=1,
        token_budget=128,
        retrieval_profile="balanced",
        config=performance.AcceptanceConfig(),
        question_count=2,
        resources=_resources(),
    )


def test_performance_report_preserves_retrieved_quality_and_adds_packed_quality():
    report = _report()

    assert report["quality"] == {
        "recall_at_k": 0.75,
        "hit_at_k": 0.5,
        "answer_token_recall": 0.5,
    }
    assert report["packed_quality"] == {
        "recall_at_k": 0.25,
        "hit_at_k": 0.5,
        "answer_token_recall": 0.25,
        "sample_count": 2,
    }
    assert "candidate chunks returned before context packing" in report["quality_scope"]["retrieved"]
    assert "chunks admitted to reader context" in report["quality_scope"]["packed"]
    assert report["payload_boundary"] == {
        "kind": "serialized_json_shape_proxy",
        "transport_measured": False,
        "mcp_envelope_serialized": False,
        "token_counter": "engraphis.regex.v1",
    }
    assert report["context"]["full_serialized_payload_tokens"] == 84
    assert report["context"]["compact_serialized_payload_tokens"] == 44


def _renderer_input() -> dict:
    report = _report()
    return {
        "schema": "engraphis-performance/v1",
        "source": {"artifact_sha256": "a" * 64},
        "chunking": {
            "questions": 2,
            "context_reduction_pct": 71.1,
            "whole": {"mean_context_tokens": 740.3, "mean_evidence_tokens": 162.2},
            "chunked": {"mean_context_tokens": 214.3, "mean_evidence_tokens": 42.4},
        },
        "performance": report,
    }


def test_renderer_consumes_selected_report_and_keeps_scope_labels():
    svg = render_report(_renderer_input())

    assert svg.startswith('<svg xmlns="http://www.w3.org/2000/svg"')
    assert "0.750" in svg
    assert "0.250" in svg
    assert "Packed context" in svg
    assert "MCP transport not measured" in svg
    assert "JSON proxy only" in svg
    assert "aaaaaaaaaaaa" in svg
    assert "49,915,394" not in svg
    assert "3 / 15" not in svg
    assert "10 / 10" not in svg


def test_renderer_reflects_a_report_that_measures_transport():
    payload = _renderer_input()
    payload["performance"]["payload_boundary"]["transport_measured"] = True

    svg = render_report(payload)

    assert "MCP transport measured" in svg
    assert "JSON proxy plus transport" in svg
    assert "MCP transport not measured" not in svg


@pytest.mark.parametrize("flag", ["false", "true", 0, 1, 0.0, 1.0, None, [], [False], {}, {"value": True}])
@pytest.mark.parametrize("form", ["render", "nested", "flat"])
def test_renderer_rejects_nonboolean_transport_flags(tmp_path, flag, form):
    payload = _renderer_input()
    payload["performance"]["payload_boundary"]["transport_measured"] = flag
    if form == "render":
        with pytest.raises(ValueError, match="transport_measured must be a JSON boolean"):
            render_report(payload)
        return
    payload.pop("source")
    if form == "flat":
        payload["performance"].pop("context")
    path = tmp_path / "report.json"
    data = json.dumps(payload).encode("utf-8")
    path.write_bytes(data)
    path.with_suffix(".json.sha256").write_text(hashlib.sha256(data).hexdigest() + "  report.json")
    with pytest.raises(ValueError, match="transport_measured must be a JSON boolean"):
        load_report(path)


@pytest.mark.parametrize("boundary", [None, False, [], "false"])
def test_renderer_rejects_malformed_payload_boundary(boundary):
    payload = _renderer_input()
    payload["performance"]["payload_boundary"] = boundary
    with pytest.raises(ValueError, match="payload_boundary must be a JSON object"):
        render_report(payload)


@pytest.mark.parametrize("omission", ["flag", "boundary"])
def test_legacy_transport_omission_never_claims_a_measurement(omission):
    payload = _renderer_input()
    if omission == "flag":
        payload["performance"]["payload_boundary"].pop("transport_measured")
    else:
        payload["performance"].pop("payload_boundary")
    svg = render_report(payload)
    assert "MCP transport not measured" in svg
    assert "JSON proxy only" in svg


def test_renderer_binds_an_unannotated_input_to_its_file_hash(tmp_path):
    payload = _renderer_input()
    payload.pop("source")
    report_path = tmp_path / "selected-report.json"
    report_path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = load_report(report_path)

    assert loaded["source"]["artifact_sha256"] == hashlib.sha256(
        report_path.read_bytes()
    ).hexdigest()
    assert loaded["performance"]["packed_quality"]["sample_count"] == 2


def test_loader_hashes_and_parses_one_snapshot_during_atomic_replacement(tmp_path, monkeypatch):
    payload = _renderer_input()
    payload.pop("source")
    report_path = tmp_path / "selected-report.json"
    original = json.dumps(payload).encode()
    report_path.write_bytes(original)
    digest = hashlib.sha256(original).hexdigest()
    report_path.with_suffix(".json.sha256").write_text(digest + "  " + report_path.name)
    payload["performance"]["quality"]["recall_at_k"] = 0.0
    replacement = json.dumps(payload).encode()
    original_bytes, original_text = Path.read_bytes, Path.read_text
    reads = []

    def replace_after_read(path, method, *args, **kwargs):
        result = method(path, *args, **kwargs)
        if path == report_path:
            reads.append(path)
            report_path.write_bytes(replacement)
        return result

    monkeypatch.setattr(Path, "read_bytes", lambda path: replace_after_read(path, original_bytes))
    monkeypatch.setattr(Path, "read_text", lambda path, *a, **kw: replace_after_read(path, original_text, *a, **kw))
    selected = load_report(report_path)
    assert selected["performance"]["quality"]["recall_at_k"] == 0.75
    assert selected["source"]["artifact_sha256"] == digest
    assert len(reads) == 1


def test_example_cards_use_the_validated_snapshot_after_source_replacement(tmp_path, monkeypatch):
    from scripts import render_benchmark_examples as examples

    fixture = Path(__file__).resolve().parents[1] / "docs/benchmark-evidence/offline-fixtures-v47.json"
    original = fixture.read_bytes()
    report_path = tmp_path / "fixtures.json"
    report_path.write_bytes(original)
    digest = hashlib.sha256(original).hexdigest()
    report_path.with_suffix(".json.sha256").write_text(digest + "  " + report_path.name)
    original_loader = examples.load_report_snapshot
    captured = {}

    def replace_after_validation(path):
        selected, raw = original_loader(path)
        replacement = json.loads(original)
        grounded = next(row["result"] for row in replacement["runs"] if row["id"] == "offline-grounded")
        captured["grounded"] = grounded["grounded"]
        captured["answerable"] = grounded["answerable"]
        grounded["grounded"] = 0
        report_path.write_text(json.dumps(replacement), encoding="utf-8")
        return selected, raw

    monkeypatch.setattr(examples, "load_report_snapshot", replace_after_validation)
    output = tmp_path / "examples.svg"
    examples.render(report_path, output)
    rendered = output.read_text(encoding="utf-8")
    assert f'{captured["grounded"]}/{captured["answerable"]} grounded' in rendered
    assert digest in rendered


def test_renderer_normalizes_flat_registry_context_fields():
    payload = {
        "source": {"artifact_sha256": "b" * 64},
        "chunking": _renderer_input()["chunking"],
        "performance": {
            "mean_context_tokens": 85.38,
            "max_context_tokens": 108,
            "full_serialized_payload_tokens": 23810,
            "compact_serialized_payload_tokens": 10982,
            "saved_serialized_payload_tokens": 12828,
            "serialized_payload_savings_ratio": 0.5388,
            "questions": 26,
            "timed_recalls": 260,
            "token_budget": 1500,
            "recall_at_k": 1.0,
            "hit_at_k": 1.0,
            "answer_token_recall": 1.0,
        },
    }

    loaded = {
        "source": payload["source"],
        "chunking": _normalize_chunking(payload["chunking"]),
        "performance": _normalize_performance(payload["performance"]),
    }

    assert loaded["performance"]["context"]["mean_tokens"] == 85.38
    assert loaded["performance"]["context"]["max_tokens"] == 108


def test_renderer_normalizes_flat_packed_quality_fields():
    flat = {
        "mean_context_tokens": 10,
        "max_context_tokens": 20,
        "full_serialized_payload_tokens": 30,
        "compact_serialized_payload_tokens": 15,
        "saved_serialized_payload_tokens": 15,
        "serialized_payload_savings_ratio": 0.5,
        "packed_recall_at_k": 0.75,
        "packed_hit_at_k": 1.0,
        "packed_answer_token_recall": 0.5,
        "questions": 3,
    }

    normalized = _normalize_performance(flat)

    assert normalized["packed_quality"] == {
        "recall_at_k": 0.75,
        "hit_at_k": 1.0,
        "answer_token_recall": 0.5,
        "sample_count": 3,
    }


def test_renderer_rejects_a_report_without_a_sha256_source_binding():
    payload = _renderer_input()
    payload["source"] = {"artifact_sha256": "not-a-sha"}

    with pytest.raises(ValueError, match="64-character SHA-256"):
        render_report(payload)


def test_loader_rejects_a_forged_supplied_file_sha(tmp_path):
    payload = _renderer_input()
    payload["source"] = {"artifact_sha256": "f" * 64}
    report_path = tmp_path / "selected-report.json"
    report_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="does not match the report file SHA-256"):
        load_report(report_path)


def test_renderer_rejects_noninteger_counts():
    payload = _renderer_input()
    payload["performance"]["corpus"]["questions"] = 2.5

    with pytest.raises(ValueError, match="exact non-negative integer"):
        render_report(payload)


def test_renderer_rejects_negative_counts():
    payload = _renderer_input()
    payload["performance"]["context"]["full_serialized_payload_tokens"] = -1

    with pytest.raises(ValueError, match="exact non-negative integer"):
        render_report(payload)


def test_renderer_rejects_nonfinite_values():
    payload = _renderer_input()
    payload["performance"]["context"]["compact_serialized_payload_tokens"] = float("nan")

    with pytest.raises(ValueError, match="finite non-negative"):
        render_report(payload)


def test_renderer_recomputes_and_rejects_context_percentage_mismatch():
    payload = _renderer_input()
    payload["chunking"]["context_reduction_pct"] = 12.0

    with pytest.raises(ValueError, match="context_reduction_pct contradicts"):
        render_report(payload)


def test_renderer_recomputes_and_rejects_payload_ratio_mismatch():
    payload = _renderer_input()
    payload["performance"]["context"]["serialized_payload_savings_ratio"] = 0.12

    with pytest.raises(ValueError, match="serialized_payload_savings_ratio contradicts"):
        render_report(payload)
