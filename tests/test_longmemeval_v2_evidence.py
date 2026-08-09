import hashlib
import json
from pathlib import Path

import pytest

from eval.benchmark import validate_report, write_canonical_artifact
from eval.longmemeval_v2_evidence import build_evidence_report
from eval.public_readiness import validate_public_readiness
from eval.run_longmemeval_v2 import (
    PINNED_LONGMEMEVAL_V2_REVISION,
    PINNED_READER_MODEL,
    PINNED_READER_REVISION,
    write_execution_manifest,
)


def _write_json(path, value):
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture(
    tmp_path,
    *,
    ablation="balanced",
    planning="off",
    mtype_limits=None,
    inserted_counts=None,
    metadata_overrides=None,
    row_overrides=None,
):
    tmp_path.mkdir(parents=True, exist_ok=True)
    limits = dict(mtype_limits or {})
    questions = _write_json(tmp_path / "questions.json", [{"question_id": "q1"}])
    haystack = _write_json(tmp_path / "haystack.json", {"q1": ["trajectory-1"]})
    trajectories = _write_json(
        tmp_path / "trajectories.json",
        [{"id": "trajectory-1"}],
    )
    memory_config_value = {
        "memory_type": "engraphis",
        "memory_params": {
            "context_k": 8,
            "max_context_tokens": 1024,
            "reader_tokenizer_model": PINNED_READER_MODEL,
            "reader_tokenizer_revision": PINNED_READER_REVISION,
            "retrieval_profile": "balanced",
            "planning": planning,
            "mtype_limits": limits,
            "embed_model": "Qwen/Qwen3-Embedding-8B",
            "embed_revision": "1d8ad4ca9b3dd8059ad90a75d4983776a23d44af",
            "vector_backend": "numpy",
        },
    }
    memory_config = _write_json(tmp_path / "memory.json", memory_config_value)
    matrix = _write_json(
        tmp_path / "matrix.json",
        {
            "name": "engraphis-longmemeval-v2-planned-recall-matrix/v2",
            "reader_model": PINNED_READER_MODEL,
            "reader_revision": PINNED_READER_REVISION,
            "runs": [
                {
                    "ablation": ablation,
                    "token_budget": 1024,
                    "config": memory_config.name,
                    "sha256": _sha256(memory_config),
                }
            ],
        },
    )
    metadata = {
        "tokenizer": f"{PINNED_READER_MODEL}@{PINNED_READER_REVISION}",
        "token_budget_method": "pinned_reader_content_tokenizer",
        "retrieval_profile": "balanced",
        "planning": planning,
        "mtype_limits": limits,
        "source_ids": ["mem_1"],
        "inserted_memory_type_counts": inserted_counts or {"semantic": 1},
        "retrieved_memory_type_counts": {"semantic": 1},
        "returned_context_tokens": 9,
        "usage": {"context_tokens": 9},
    }
    metadata.update(metadata_overrides or {})
    private = {
        "question_id": "q1",
        "category": "static",
        "question_text": "private question text",
        "answer_gold": "private gold answer",
        "response_raw": "private reader answer",
        "response_parsed_boxed": "private boxed answer",
        "memory_context": [{"type": "text", "value": "private retrieved context"}],
        "prompt_messages": [{"role": "user", "content": "private prompt"}],
        "memory_query_duration_seconds": 0.0125,
        "memory_context_original_token_count": 19,
        "memory_context_token_count": 11,
        "memory_post_query_metadata": metadata,
        "usage": {"prompt_tokens": 101, "completion_tokens": 7},
        "is_abstention_problem": False,
        "is_unknown": False,
        "score": 1.0,
        "score_bool": True,
    }
    private.update(row_overrides or {})
    per_question = tmp_path / "per_question.jsonl"
    per_question.write_text(json.dumps(private) + "\n", encoding="utf-8")
    execution_manifest = tmp_path / "execution.json"
    write_execution_manifest(
        execution_manifest,
        checkout={
            "revision": PINNED_LONGMEMEVAL_V2_REVISION,
            "dirty": False,
            "dirty_state_sha256": hashlib.sha256(b"").hexdigest(),
        },
        per_question=per_question,
        questions=questions,
        haystack=haystack,
        trajectories=trajectories,
        memory_config=memory_config,
        matrix_manifest=matrix,
        seed=42,
        delegated_argv=["--memory-type", "engraphis"],
    )
    return {
        "per_question_path": per_question,
        "questions_path": questions,
        "haystack_path": haystack,
        "trajectories_path": trajectories,
        "memory_config_path": memory_config,
        "execution_manifest_path": execution_manifest,
        "upstream_revision": PINNED_LONGMEMEVAL_V2_REVISION,
        "matrix_manifest_path": matrix,
        "ablation": ablation,
        "token_budget": 1024,
        "seed": 42,
    }


def test_official_v2_evidence_export_is_redacted_and_run_bound(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(
        "eval.benchmark.git_provenance",
        lambda: {
            "commit": "a" * 40,
            "dirty": False,
            "dirty_state_sha256": hashlib.sha256(b"").hexdigest(),
        },
    )
    kwargs = _fixture(tmp_path)
    report = build_evidence_report(**kwargs)

    assert validate_report(report) == []
    record = report["records"][0]
    assert validate_public_readiness(report) == []
    for field in (
        "question_text",
        "answer_gold",
        "response_raw",
        "response_parsed_boxed",
        "memory_context",
        "prompt_messages",
        "query_sha256",
        "answer_or_response_sha256",
        "context_or_prompt_sha256",
    ):
        assert field not in record
    assert record["retrieved_ids"] == ["mem_1"]
    assert record["inserted_memory_type_counts"] == {"semantic": 1}
    assert report["metrics"]["official_qa"]["mean_score"] == 1.0
    assert report["protocol"]["config"]["measurement_scope"] == "end_to_end"
    assert report["protocol"]["config"]["execution_binding"]["verified"] is True
    execution = json.loads(Path(kwargs["execution_manifest_path"]).read_text("utf-8"))
    assert report["environment"] == execution["environment"]
    assert report["protocol"]["source_questions"] == 1
    assert report["privacy"]["content_fingerprint_policy"] == "omitted"
    assert {item["name"] for item in report["suite"]["sources"]} == {
        "per_question.jsonl",
        "haystack.json",
        "trajectories.json",
        "memory.json",
        "matrix.json",
        "execution.json",
    }
    serialized = json.dumps(report)
    for private_value in (
        "private question text",
        "private gold answer",
        "private reader answer",
        "private retrieved context",
        "private prompt",
    ):
        assert private_value not in serialized

    artifact = tmp_path / "public.json"
    written = write_canonical_artifact(report, artifact)
    assert written["sha256"] in artifact.with_name("public.json.sha256").read_text("ascii")


def test_official_v2_evidence_rejects_dirty_execution_attestation(tmp_path):
    kwargs = _fixture(tmp_path)
    manifest_path = kwargs["execution_manifest_path"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["official_checkout"]["dirty"] = True
    _write_json(manifest_path, manifest)

    with pytest.raises(ValueError, match="clean official checkout"):
        build_evidence_report(**kwargs)


def test_official_v2_evidence_requires_recorded_run_environment(tmp_path):
    kwargs = _fixture(tmp_path)
    manifest_path = kwargs["execution_manifest_path"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    del manifest["environment"]
    _write_json(manifest_path, manifest)

    with pytest.raises(ValueError, match="official run environment"):
        build_evidence_report(**kwargs)


def test_official_v2_evidence_rejects_tampered_command_receipt(tmp_path):
    kwargs = _fixture(tmp_path)
    manifest_path = kwargs["execution_manifest_path"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["delegated_argv"].append("--tampered")
    _write_json(manifest_path, manifest)

    with pytest.raises(ValueError, match="delegated_argv_sha256"):
        build_evidence_report(**kwargs)


@pytest.mark.parametrize(
    ("metadata_overrides", "message"),
    [
        ({"token_budget_method": "estimated"}, "exact token accounting"),
        ({"planning": "auto"}, "planning"),
        ({"mtype_limits": {"episodic": 2}}, "mtype_limits"),
        ({"returned_context_tokens": 1025}, "exceed"),
        ({"retrieved_memory_type_counts": {}}, "match source_ids"),
    ],
)
def test_official_v2_evidence_rejects_unexecuted_controls(
    tmp_path,
    metadata_overrides,
    message,
):
    kwargs = _fixture(tmp_path, metadata_overrides=metadata_overrides)

    with pytest.raises(ValueError, match=message):
        build_evidence_report(**kwargs)


def test_official_v2_evidence_rejects_partial_source_coverage(tmp_path):
    kwargs = _fixture(tmp_path)
    questions = kwargs["questions_path"]
    _write_json(questions, [{"question_id": "q1"}, {"question_id": "q2"}])
    receipt = json.loads(kwargs["execution_manifest_path"].read_text(encoding="utf-8"))
    receipt["questions_sha256"] = _sha256(questions)
    receipt["source_question_count"] = 2
    _write_json(kwargs["execution_manifest_path"], receipt)

    with pytest.raises(ValueError, match="cover the source question IDs exactly"):
        build_evidence_report(**kwargs)


def test_official_v2_evidence_rejects_unknown_output_id(tmp_path):
    kwargs = _fixture(tmp_path)
    row = json.loads(
        Path(kwargs["per_question_path"]).read_text(encoding="utf-8")
    )
    row["question_id"] = "unknown"
    Path(kwargs["per_question_path"]).write_text(
        json.dumps(row) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="cover the source question IDs exactly"):
        build_evidence_report(**kwargs)


def test_official_v2_evidence_accepts_complete_two_question_run(tmp_path):
    kwargs = _fixture(tmp_path)
    per_question = Path(kwargs["per_question_path"])
    first = json.loads(per_question.read_text(encoding="utf-8"))
    second = {**first, "question_id": "q2"}
    per_question.write_text(
        json.dumps(first) + "\n" + json.dumps(second) + "\n",
        encoding="utf-8",
    )
    questions = Path(kwargs["questions_path"])
    _write_json(questions, [{"question_id": "q1"}, {"question_id": "q2"}])
    execution_manifest = Path(kwargs["execution_manifest_path"])
    execution_manifest.unlink()
    write_execution_manifest(
        execution_manifest,
        checkout={
            "revision": PINNED_LONGMEMEVAL_V2_REVISION,
            "dirty": False,
            "dirty_state_sha256": hashlib.sha256(b"").hexdigest(),
        },
        per_question=per_question,
        questions=questions,
        haystack=kwargs["haystack_path"],
        trajectories=kwargs["trajectories_path"],
        memory_config=kwargs["memory_config_path"],
        matrix_manifest=kwargs["matrix_manifest_path"],
        seed=42,
        delegated_argv=["--memory-type", "engraphis"],
    )

    report = build_evidence_report(**kwargs)

    assert report["protocol"]["source_questions"] == 2
    assert report["protocol"]["n_total"] == 2


def test_memory_type_cap_evidence_requires_two_populated_types(tmp_path):
    accepted = _fixture(
        tmp_path / "accepted",
        ablation="episodic_cap_2",
        mtype_limits={"episodic": 2},
        inserted_counts={"episodic": 1, "semantic": 1},
    )
    assert build_evidence_report(**accepted)["records"][0][
        "inserted_memory_type_counts"
    ] == {"episodic": 1, "semantic": 1}

    # Single-type cap does not require cross-type evidence
    single_type = _fixture(
        tmp_path / "single_type",
        ablation="episodic_cap_2",
        mtype_limits={"episodic": 2},
        inserted_counts={"episodic": 1},
    )
    report = build_evidence_report(**single_type)
    assert report["records"][0]["inserted_memory_type_counts"] == {"episodic": 1}

    # Multi-type cap still requires at least two populated types
    multi_type_rejected = _fixture(
        tmp_path / "multi_type_rejected",
        ablation="multi_cap",
        mtype_limits={"episodic": 2, "semantic": 3},
        inserted_counts={"episodic": 1},
    )
    with pytest.raises(ValueError, match="at least two populated memory types"):
        build_evidence_report(**multi_type_rejected)


def test_memory_type_cap_evidence_rejects_observed_cap_breaches(tmp_path):
    kwargs = _fixture(
        tmp_path,
        ablation="episodic_cap_2",
        mtype_limits={"episodic": 2},
        inserted_counts={"episodic": 1, "semantic": 1},
        metadata_overrides={
            "source_ids": ["mem_1", "mem_2", "mem_3"],
            "retrieved_memory_type_counts": {"episodic": 3},
        },
    )

    with pytest.raises(ValueError, match="exceeds mtype_limits"):
        build_evidence_report(**kwargs)
