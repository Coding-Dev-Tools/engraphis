import json

import pytest

from eval import external, harness
from eval.benchmark import sha256_file, validate_report


def dataset_file(tmp_path):
    path = tmp_path / "locomo.json"
    path.write_text(json.dumps([{
        "sample_id": "conversation-1",
        "conversation": {
            "session_1_date_time": "2026-08-04",
            "session_1": [{"speaker": "A", "dia_id": "D1:1",
                           "text": "Private project Mercury releases Tuesday."}],
        },
        "qa": [{"question": "When does Private project Mercury release?",
                "answer": "Tuesday", "evidence": ["D1:1"], "category": 1}],
    }]), encoding="utf-8")
    return path


def test_external_export_is_checksummed_redacted_and_not_qa(tmp_path):
    dataset = dataset_file(tmp_path)
    artifact = tmp_path / "public.json"
    assert external.main([
        "--dataset", str(dataset), "--format", "locomo", "--offline",
        "--artifact", str(artifact),
    ]) == 0
    raw = artifact.read_text(encoding="utf-8")
    report = json.loads(raw)
    assert validate_report(report) == []
    assert report["suite"]["sha256"] == sha256_file(dataset)
    assert report["metrics"]["official_qa_complete"] is False
    assert report["metrics"]["semantic_embedding"] is False
    assert report["metrics"]["questions"] == 1
    assert report["metrics"]["source_case_coverage_complete"] is True
    assert report["protocol"]["config"]["source_case_identity"] == "explicit"
    assert report["records"][0]["case"] == "conversation-1"
    assert "packed_recall_at_k" in report["records"][0]
    assert "Private project Mercury" not in raw
    assert str(tmp_path) not in raw
    assert artifact.with_suffix(".json.sha256").read_text().split()[0] == sha256_file(artifact)


def test_dataset_drift_after_loading_prevents_export(tmp_path, monkeypatch):
    dataset = dataset_file(tmp_path)
    artifact = tmp_path / "public.json"
    original = external._load_locomo_with_integrity

    def drifting_load(*args, **kwargs):
        result = original(*args, **kwargs)
        dataset.write_text(dataset.read_text() + "\n", encoding="utf-8")
        return result

    monkeypatch.setattr(external, "_load_locomo_with_integrity", drifting_load)
    assert external.main([
        "--dataset", str(dataset), "--format", "locomo", "--offline",
        "--artifact", str(artifact),
    ]) == 2
    assert not artifact.exists()


def test_repair_manifest_drift_after_normalization_prevents_export(tmp_path, monkeypatch):
    dataset = dataset_file(tmp_path)
    manifest = tmp_path / "repair.json"
    manifest.write_text(json.dumps({
        "schema": "engraphis-locomo-repair/v2",
        "dataset_sha256": sha256_file(dataset),
        "repairs": [],
        "deduplications": [],
    }), encoding="utf-8")
    artifact = tmp_path / "public.json"
    original = external._load_locomo_with_integrity

    def drifting_load(*args, **kwargs):
        result = original(*args, **kwargs)
        manifest.write_text(manifest.read_text() + "\n", encoding="utf-8")
        return result

    monkeypatch.setattr(external, "_load_locomo_with_integrity", drifting_load)
    assert external.main([
        "--dataset", str(dataset), "--format", "locomo", "--offline",
        "--locomo-repair-manifest", str(manifest), "--artifact", str(artifact),
    ]) == 2
    assert not artifact.exists()


def test_export_rejects_changed_dataset_bytes(tmp_path):
    dataset = dataset_file(tmp_path)
    with pytest.raises(ValueError, match="dataset changed"):
        external.diagnostic_artifact({"dataset_sha256": "0" * 64}, dataset=str(dataset))


@pytest.mark.parametrize("changed", ["dataset", "manifest", "producer"])
@pytest.mark.parametrize("stage", ["printing", "envelope"])
def test_completed_artifact_matches_frozen_evaluation_snapshots(tmp_path, monkeypatch, changed, stage):
    dataset = dataset_file(tmp_path)
    manifest = tmp_path / "repair.json"
    manifest.write_text(json.dumps({
        "schema": "engraphis-locomo-repair/v2", "dataset_sha256": sha256_file(dataset),
        "repairs": [], "deduplications": [],
    }))
    producer = tmp_path / "producer.py"
    producer.write_text("# frozen producer\n")
    monkeypatch.setattr(external, "producer_snapshot", lambda: {str(producer): sha256_file(producer)})
    target = {"dataset": dataset, "manifest": manifest, "producer": producer}[changed]

    def mutate():
        target.write_bytes(target.read_bytes() + b"\n")

    if stage == "printing":
        def late_print(*args, **kwargs):
            if args and str(args[0]).startswith("\nEngraphis"):
                mutate()
        monkeypatch.setattr(external, "print", late_print, raising=False)
    else:
        original = external.report_envelope

        def late_envelope(*args, **kwargs):
            mutate()
            return original(*args, **kwargs)
        monkeypatch.setattr(external, "report_envelope", late_envelope)
    artifact = tmp_path / "artifact.json"
    assert external.main([
        "--dataset", str(dataset), "--format", "locomo", "--offline",
        "--locomo-repair-manifest", str(manifest), "--artifact", str(artifact),
    ]) == 2
    assert not artifact.exists()
    assert not artifact.with_suffix(".json.sha256").exists()


def test_packed_metrics_do_not_credit_unadmitted_candidate_text(tmp_path):
    dataset = dataset_file(tmp_path)
    report = harness.run(external.load_locomo(str(dataset)), token_budget=0)
    assert report["recall_at_k"] == 1.0
    assert report["answer_token_recall"] == 1.0
    assert report["packed_recall_at_k"] == 0.0
    assert report["packed_answer_token_recall"] == 0.0
    assert report["detail"][0]["packed_ids"] == []


def test_duplicate_evidence_requires_exact_hash_bound_v2_declaration(tmp_path):
    dataset = dataset_file(tmp_path)
    raw = json.loads(dataset.read_text())
    raw[0]["qa"][0]["evidence"] = ["D1:1", "D1:1"]
    dataset.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate supporting"):
        external.load_locomo(str(dataset))
    manifest = tmp_path / "repair.json"
    declared = {"case_id": "conversation-1", "question_index": 0, "id": "D1:1", "occurrences": 2}
    payload = {"schema": "engraphis-locomo-repair/v2", "dataset_sha256": sha256_file(dataset),
               "repairs": [], "deduplications": [declared]}
    manifest.write_text(json.dumps(payload))
    cases, integrity = external._load_locomo_with_integrity(str(dataset), repair_manifest=str(manifest))
    assert cases[0]["questions"][0]["supporting"] == ["D1:1"]
    assert integrity["repair_manifest"]["applied_deduplications"] == [declared]
    payload["deduplications"][0]["occurrences"] = 3
    manifest.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="occurrence count"):
        external.load_locomo(str(dataset), repair_manifest=str(manifest))


def test_longmemeval_omits_only_declared_empty_nonanswer_turns(tmp_path):
    dataset = tmp_path / "longmem.json"
    raw = [{"question_id": "q1", "question": "When?", "answer": "Tuesday",
            "haystack_session_ids": ["s1"], "haystack_dates": ["2026-01-01"],
            "haystack_sessions": [[{"role": "assistant", "content": ""},
                                   {"role": "user", "content": "Tuesday", "has_answer": True}]],
            "answer_session_ids": ["s1"]}]
    dataset.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="non-empty"):
        external.load_longmemeval(str(dataset))
    declaration = {"schema": "engraphis-longmemeval-repair/v1", "dataset_sha256": sha256_file(dataset),
                   "empty_turn_omissions": [{"question_id": "q1", "session_id": "s1", "turn_index": 0}]}
    manifest = tmp_path / "repairs.json"
    manifest.write_text(json.dumps(declaration))
    cases = external.load_longmemeval(str(dataset), repair_manifest=str(manifest))
    assert len(cases) == 1 and len(cases[0]["questions"]) == 1
    assert cases[0]["questions"][0]["supporting"] == ["s1"]
    assert "Tuesday" in cases[0]["memories"][0]["text"]
    declaration["empty_turn_omissions"].append({"question_id": "q1", "session_id": "s1", "turn_index": 1})
    manifest.write_text(json.dumps(declaration))
    with pytest.raises(ValueError, match="empty, non-answer"):
        external.load_longmemeval(str(dataset), repair_manifest=str(manifest))
