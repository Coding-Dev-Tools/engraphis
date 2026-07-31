import json

import pytest

from eval.benchmark import validate_report, write_canonical_artifact
from eval.longmemeval_v2_evidence import build_evidence_report


def _write_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_official_v2_evidence_export_redacts_private_prompt_material(tmp_path):
    questions = _write_json(tmp_path / "questions.json", [{"question_id": "q1"}])
    haystack = _write_json(tmp_path / "haystack.json", {"q1": ["trajectory-1"]})
    trajectories = _write_json(tmp_path / "trajectories.json", [{"id": "trajectory-1"}])
    config = _write_json(tmp_path / "memory.json", {"memory_type": "engraphis"})
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
        "memory_post_query_metadata": {
            "tokenizer": "Qwen/Qwen3.5-9B@c202236235762e1c871ad0ccb60c8ee5ba337b9a",
            "source_ids": ["mem_1"],
            "usage": {"context_tokens": 9},
        },
        "usage": {"prompt_tokens": 101, "completion_tokens": 7},
        "is_abstention_problem": False,
        "is_unknown": False,
        "score": 1.0,
        "score_bool": True,
    }
    per_question = tmp_path / "per_question.jsonl"
    per_question.write_text(json.dumps(private) + "\n", encoding="utf-8")

    report = build_evidence_report(
        per_question_path=per_question,
        questions_path=questions,
        haystack_path=haystack,
        trajectories_path=trajectories,
        memory_config_path=config,
        evaluator_model="example/evaluator",
        evaluator_revision="b" * 40,
    )

    assert validate_report(report) == []
    record = report["records"][0]
    for field in (
        "question_text", "answer_gold", "response_raw", "response_parsed_boxed",
        "memory_context", "prompt_messages",
    ):
        assert field not in record
    assert len(record["query_sha256"]) == 64
    assert len(record["answer_or_response_sha256"]) == 64
    assert len(record["context_or_prompt_sha256"]) == 64
    assert record["retrieved_ids"] == ["mem_1"]
    assert report["metrics"]["official_qa"]["mean_score"] == 1.0
    assert report["protocol"]["token_accounting"]["method"] == (
        "official_harness_count_memory_context_tokens"
    )
    assert report["protocol"]["token_accounting"]["scope"] == (
        "official_harness_memory_context_item_content_excluding_prompt_framing"
    )
    assert {item["name"] for item in report["suite"]["sources"]} == {
        "per_question.jsonl", "haystack.json", "trajectories.json", "memory.json",
    }
    serialized = json.dumps(report)
    for private_value in (
        "private question text", "private gold answer", "private reader answer",
        "private retrieved context", "private prompt",
    ):
        assert private_value not in serialized

    artifact = tmp_path / "public.json"
    written = write_canonical_artifact(report, artifact)
    assert written["sha256"] in artifact.with_name("public.json.sha256").read_text("ascii")


def test_official_v2_evidence_export_rejects_unpinned_reader_metadata(tmp_path):
    source_paths = [
        _write_json(tmp_path / name, {} if name != "questions.json" else [])
        for name in ("questions.json", "haystack.json", "trajectories.json", "memory.json")
    ]
    per_question = tmp_path / "per_question.jsonl"
    per_question.write_text(json.dumps({
        "question_id": "q1",
        "memory_post_query_metadata": {"tokenizer": "engraphis.regex.v1"},
    }) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="pinned reader tokenizer"):
        build_evidence_report(
            per_question_path=per_question,
            questions_path=source_paths[0],
            haystack_path=source_paths[1],
            trajectories_path=source_paths[2],
            memory_config_path=source_paths[3],
        )


@pytest.mark.parametrize(
    ("evaluator_model", "evaluator_revision", "message"),
    [
        ("example/evaluator", None, "must be used together"),
        ("example/evaluator", "main", "immutable lowercase 40-character commit"),
    ],
)
def test_official_v2_evidence_export_requires_a_pinned_evaluator(
    evaluator_model, evaluator_revision, message,
):
    with pytest.raises(ValueError, match=message):
        build_evidence_report(
            per_question_path="unused.jsonl",
            questions_path="unused-questions.json",
            haystack_path="unused-haystack.json",
            trajectories_path="unused-trajectories.json",
            memory_config_path="unused-memory.json",
            evaluator_model=evaluator_model,
            evaluator_revision=evaluator_revision,
        )


def test_official_v2_evidence_export_rejects_malformed_measured_fields(tmp_path):
    source_paths = [
        _write_json(tmp_path / name, {} if name != "questions.json" else [])
        for name in ("questions.json", "haystack.json", "trajectories.json", "memory.json")
    ]
    per_question = tmp_path / "per_question.jsonl"
    per_question.write_text(json.dumps({
        "question_id": "q1",
        "memory_post_query_metadata": {
            "tokenizer": "Qwen/Qwen3.5-9B@c202236235762e1c871ad0ccb60c8ee5ba337b9a",
        },
        "memory_context_token_count": -1,
        "is_abstention_problem": False,
        "is_unknown": False,
        "score": 1.0,
        "score_bool": True,
        "memory_query_duration_seconds": 0.1,
    }) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="memory_context_token_count"):
        build_evidence_report(
            per_question_path=per_question,
            questions_path=source_paths[0],
            haystack_path=source_paths[1],
            trajectories_path=source_paths[2],
            memory_config_path=source_paths[3],
        )
