"""Acceptance metrics must catch lost writes, including false duplicate decisions."""
import json
from types import SimpleNamespace

from engraphis.core.engine import MemoryEngine
from engraphis.core.interfaces import MemoryRecord
from engraphis.core.resolve import ResolutionOp, resolve
from eval import resolver_reworded_corrections as evaluator


def _dataset(tmp_path, rows):
    path = tmp_path / "resolver.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    return path


def test_negative_noop_is_an_error_and_fails_cli(tmp_path, monkeypatch):
    path = _dataset(tmp_path, [{
        "id": "distinct", "neighbor": "Old claim.",
        "candidate": "Different claim.", "expected": "add",
    }])
    monkeypatch.setattr(evaluator, "resolve", lambda *a, **k: SimpleNamespace(
        op=ResolutionOp.NOOP, reason="incorrect duplicate",
    ))
    report = evaluator.evaluate(path)
    assert report["false_invalidations"] == 0
    assert report["false_noops"] == 1
    assert report["distinct_fact_error_rate"] == 1
    assert evaluator.main(["--dataset", str(path)]) == 1


def test_end_to_end_evaluator_uses_real_writes_and_not_injected_similarity(tmp_path, monkeypatch):
    path = _dataset(tmp_path, [
        {"id": "correction", "neighbor": "The cache TTL is 30 seconds.",
         "candidate": "The cache TTL is 90 seconds.", "expected": "invalidate",
         "subject_key": "cache", "claim_kind": "ttl"},
        {"id": "distinct", "neighbor": "The production worker runs daily.",
         "candidate": "The staging worker runs daily.", "expected": "add"},
    ])

    def no_unit_shortcut(*args, **kwargs):
        raise AssertionError("production evaluation must use the real write path")

    monkeypatch.setattr(evaluator, "resolve", no_unit_shortcut)
    report = evaluator.evaluate(path, end_to_end=True)
    assert report["execution"] == "production_write_path"
    assert report["similarity_injected"] is False
    assert report["correction_recall"] == 1
    assert report["distinct_fact_error_rate"] == 0
    assert report["lost_distinct_facts"] == 0


def test_reordered_environment_bindings_keep_distinct_unkeyed_facts():
    old = "The staging database holds 300 connections in production environment."
    new = "The production database holds 300 connections in staging environment."
    result = resolve(new, [(0.9, MemoryRecord(id="mem_old", content=old))])
    assert result.op == ResolutionOp.RELATE
    engine = MemoryEngine.create(":memory:")
    try:
        workspace = engine.store.get_or_create_workspace("acceptance")
        first = engine.remember_with_resolution(old, workspace_id=workspace)
        second = engine.remember_with_resolution(new, workspace_id=workspace)
        assert first["id"] != second["id"]
        assert engine.store.get_memory(first["id"]).valid_to is None
        assert engine.store.get_memory(second["id"]).content == new
    finally:
        engine.store.close()


def test_repeated_identical_environment_bindings_still_deduplicate():
    text = "The staging database holds 300 connections in production environment."
    result = resolve(text, [(0.9, MemoryRecord(id="mem_old", content=text))])
    assert result.op == ResolutionOp.NOOP


def test_acceptance_json_reports_complete_counts_without_source_text(tmp_path, capsys):
    old = "The production cache expires after 30 seconds."
    new = "The production cache expires after 90 seconds."
    path = _dataset(tmp_path, [{
        "id": "correction", "neighbor": old, "candidate": new,
        "expected": "invalidate",
    }])
    assert evaluator.main(["--dataset", str(path), "--end-to-end", "--json"]) == 0
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["protocol"]["n_total"] == report["protocol"]["n_scored"] == 1
    assert report["metrics"]["correction_precision"] == 1
    assert report["metrics"]["similarity_injected"] is False
    assert old not in output and new not in output and str(tmp_path) not in output
    assert report["suite"]["sha256"]
    assert report["system"]["config_sha256"]
    assert report["protocol"]["command"] == [
        "python", "-m", "eval.resolver_reworded_corrections",
        "--dataset", path.name, "--json", "--end-to-end",
    ]
    assert {"engine.py", "store.py", "embedder_deterministic.py"} <= {
        source["name"] for source in report["suite"]["sources"]
    }


def test_repo_dataset_and_audit_mode_are_replayable(capsys):
    assert evaluator.main(["--dataset", str(evaluator.DATASET), "--json", "--audit-only"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["protocol"]["command"] == [
        "python", "-m", "eval.resolver_reworded_corrections",
        "--dataset", "eval/datasets/resolver_reworded_corrections.jsonl", "--json", "--audit-only",
    ]
