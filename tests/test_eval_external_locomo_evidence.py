import json

import pytest

from eval import external


def _write_locomo(tmp_path, evidence, *, second_question_evidence=None):
    qa = [{
        "question": "Which release is active?",
        "answer": "blue",
        "evidence": evidence,
        "category": 1,
    }]
    if second_question_evidence is not None:
        qa.append({
            "question": "Which fallback is active?",
            "answer": "green",
            "evidence": second_question_evidence,
            "category": 1,
        })
    data = [{
        "sample_id": "conv-test",
        "conversation": {
            "session_1": [
                {"speaker": "A", "dia_id": "D1:1", "text": "Blue is active."},
                {"speaker": "B", "dia_id": "D1:2", "text": "Green is fallback."},
            ],
            "session_1_date_time": "2026-08-04",
        },
        "qa": qa,
    }]
    path = tmp_path / "locomo.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _write_manifest(tmp_path, dataset, repairs, *, source_hash=None):
    path = tmp_path / "repairs.json"
    path.write_text(json.dumps({
        "schema": "engraphis-locomo-repair/v1",
        "dataset_sha256": source_hash or external.dataset_sha256(str(dataset)),
        "repairs": repairs,
    }), encoding="utf-8")
    return path


def test_grouped_and_mechanical_locomo_ids_normalize_without_masking_garbage():
    assert external._locomo_supporting_ids([
        "D8:6; D9:17", "D9:1 D4:4", "D30:05", "D:11:26",
    ]) == ["D8:6", "D9:17", "D9:1", "D4:4", "D30:5", "D11:26"]
    assert external._locomo_supporting_ids(["D:not-an-id"]) == ["D:not-an-id"]


def test_locomo_loader_aggregates_unknown_gold_references(tmp_path):
    dataset = _write_locomo(
        tmp_path, ["D1:1", "D9:9"], second_question_evidence=["D8:8"],
    )

    with pytest.raises(ValueError, match=r"conv-test:0: D9:9.*conv-test:1: D8:8"):
        external.load_locomo(str(dataset))


def test_locomo_loader_rejects_duplicate_final_gold_references(tmp_path):
    dataset = _write_locomo(tmp_path, ["D1:1", "D1:1"])

    with pytest.raises(ValueError, match="duplicate supporting dialogue IDs"):
        external.load_locomo(str(dataset))


def test_hash_bound_manifest_applies_exact_repair_and_records_provenance(tmp_path):
    dataset = _write_locomo(tmp_path, ["BROKEN", "D1:1"])
    repairs = [{
        "case_id": "conv-test", "question_index": 0,
        "from": "BROKEN", "to": "D1:2",
    }]
    manifest = _write_manifest(tmp_path, dataset, repairs)

    cases, integrity = external._load_locomo_with_integrity(
        str(dataset), repair_manifest=str(manifest),
    )

    assert cases[0]["questions"][0]["supporting"] == ["D1:2", "D1:1"]
    provenance = integrity["repair_manifest"]
    assert provenance["sha256"] == external.dataset_sha256(str(manifest))
    assert provenance["dataset_sha256"] == external.dataset_sha256(str(dataset))
    assert provenance["applied_repairs"] == repairs


def test_repair_manifest_can_remove_a_stray_token_without_dropping_the_question(tmp_path):
    dataset = _write_locomo(tmp_path, ["D1:1", "D"])
    repairs = [{
        "case_id": "conv-test", "question_index": 0, "from": "D", "to": None,
    }]
    manifest = _write_manifest(tmp_path, dataset, repairs)

    cases = external.load_locomo(str(dataset), repair_manifest=str(manifest))

    assert cases[0]["questions"][0]["supporting"] == ["D1:1"]
    assert cases[0]["questions"][0]["answerable"] is True


def test_repair_manifest_fails_closed_on_hash_mismatch_unused_or_bad_target(tmp_path):
    dataset = _write_locomo(tmp_path, ["BROKEN"])
    repair = {
        "case_id": "conv-test", "question_index": 0,
        "from": "BROKEN", "to": "D1:1",
    }
    wrong_hash = _write_manifest(tmp_path, dataset, [repair], source_hash="0" * 64)
    with pytest.raises(ValueError, match="does not match the source dataset"):
        external.load_locomo(str(dataset), repair_manifest=str(wrong_hash))

    unused_dir = tmp_path / "unused"
    unused_dir.mkdir()
    unused = _write_manifest(unused_dir, dataset, [{**repair, "question_index": 9}])
    with pytest.raises(ValueError, match="unused repairs"):
        external.load_locomo(str(dataset), repair_manifest=str(unused))

    target_dir = tmp_path / "target"
    target_dir.mkdir()
    bad_target = _write_manifest(target_dir, dataset, [{**repair, "to": "D7:7"}])
    with pytest.raises(ValueError, match=r"conv-test:0: D7:7"):
        external.load_locomo(str(dataset), repair_manifest=str(bad_target))


def test_external_report_includes_applied_manifest_integrity(tmp_path):
    dataset = _write_locomo(tmp_path, ["BROKEN"])
    repairs = [{
        "case_id": "conv-test", "question_index": 0,
        "from": "BROKEN", "to": "D1:1",
    }]
    manifest = _write_manifest(tmp_path, dataset, repairs)
    report_path = tmp_path / "report.json"

    assert external.main([
        "--dataset", str(dataset), "--format", "locomo", "--offline",
        "--locomo-repair-manifest", str(manifest), "--json", str(report_path),
    ]) == 0

    report = json.loads(report_path.read_text(encoding="utf-8"))
    integrity = report["dataset_integrity"]
    assert integrity["repair_manifest"]["applied_repairs"] == repairs
    assert integrity["repair_manifest"]["sha256"] == external.dataset_sha256(str(manifest))
    assert report["questions"] == report["scored_questions"] == 1


def test_external_rejects_invalid_locomo_before_loading_an_embedder(tmp_path, monkeypatch):
    dataset = _write_locomo(tmp_path, ["UNKNOWN"])
    monkeypatch.setattr(
        external, "get_embedder",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not load")),
    )

    assert external.main([
        "--dataset", str(dataset), "--format", "locomo", "--offline",
    ]) == 2
