"""Contract checks for the optional external agent-memory adapters."""
from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest

from eval import agent_benchmarks
from eval.agent_benchmarks import (
    load_locomo_plus,
    load_mem2actbench,
    load_memoryagentbench,
    main,
)
from engraphis.backends import DeterministicEmbedder
from eval.harness import run


def _write_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return str(path)


def _write_jsonl(path, rows):
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return str(path)


def test_agent_loader_uses_the_same_bytes_as_the_initial_digest(tmp_path, monkeypatch):
    source = Path(_write_json(tmp_path / "plus.json", [{
        "id": "cognitive-original",
        "input_prompt": "Morgan previously said oat milk is required.",
        "trigger": "What milk should Morgan receive?",
        "evidence": "Morgan previously said oat milk is required.",
        "category": "Cognitive",
    }]))
    original = source.read_bytes()
    replacement = json.dumps([{
        "id": "cognitive-replacement",
        "input_prompt": "A replacement source with different evidence.",
        "trigger": "What is in the replacement source?",
        "evidence": "A replacement source with different evidence.",
        "category": "Cognitive",
    }]).encode("utf-8")
    read_bytes = Path.read_bytes
    reads = 0

    def replace_after_read(path, *args, **kwargs):
        nonlocal reads
        payload = read_bytes(path)
        if path == source:
            reads += 1
            source.write_bytes(replacement)
        return payload

    monkeypatch.setattr(Path, "read_bytes", replace_after_read)
    snapshot = agent_benchmarks._read_records_snapshot(str(source))
    cases = load_locomo_plus(str(source), snapshot=snapshot)

    assert reads == 1
    assert snapshot.sha256 == hashlib.sha256(original).hexdigest()
    assert cases[0]["id"] == "cognitive-original"


def test_memoryagentbench_context_export_and_structured_conflict_events(tmp_path):
    path = _write_json(tmp_path / "mab.json", {
        "data": [{
            "case_id": "conflict",
            "memory_events": [
                {"id": "city-old", "text": "Ada's home city is Portland.",
                 "subject_key": "ada.home_city", "claim_kind": "fact"},
                {"id": "city-new", "text": "Ada's home city is Boston.",
                 "subject_key": "ada.home_city", "claim_kind": "fact"},
            ],
            "questions": ["What is Ada's current home city?"],
            "answers": ["Boston"],
            "qa_pair_ids": ["conflict-q"],
            "supporting_ids": [["city-new"]],
        }, {
            "case_id": "plain-context",
            "context": "First remembered paragraph about rabbits.\n\nSecond paragraph about otters.",
            "questions": ["What animal is mentioned second?"],
            "answers": ["otters"],
        }],
    })
    cases = load_memoryagentbench(path)
    assert len(cases) == 2
    assert cases[0]["memories"][1]["subject_key"] == "ada.home_city"
    assert cases[0]["questions"][0]["supporting"] == ["city-new"]
    assert cases[0]["questions"][0]["evidence_label_provenance"] == "explicit_ids"
    assert cases[0]["questions"][0]["evidence_label_method"] == "provided_source_ids"
    assert len(cases[1]["memories"]) == 2
    report = run(cases[:1], k=3)
    assert report["questions"] == 1
    assert report["recall_at_k"] == 1.0


def test_memoryagentbench_rejects_misaligned_questions_and_answers(tmp_path):
    path = _write_json(tmp_path / "bad.json", {
        "data": [{"context": "one paragraph", "questions": ["a", "b"], "answers": ["a"]}],
    })
    with pytest.raises(ValueError, match="equal length"):
        load_memoryagentbench(path)


def test_memoryagentbench_accepts_official_nested_answers_and_metadata(tmp_path):
    path = _write_json(tmp_path / "mab-official.json", [{
        "context": "The preferred deployment region is us-east-1.",
        "questions": ["Which region is preferred?"],
        "answers": [["us-east-1", "US East"]],
        "metadata": {
            "qa_pair_ids": ["official-q"],
            "source": "factconsolidation_sh_32k",
        },
    }])

    case = load_memoryagentbench(path)[0]
    question = case["questions"][0]

    assert question["id"] == "official-q"
    assert question["answer"] == "us-east-1"
    assert question["answer_variants"] == ["us-east-1", "US East"]
    assert question["supporting"]
    assert question["evidence_label_provenance"] == "derived_answer_substring"
    assert question["evidence_label_method"] == "answer_variant_substring"
    assert question["category"] == "factconsolidation_sh_32k"


def test_memoryagentbench_scores_any_accepted_answer_variant(tmp_path):
    path = _write_json(tmp_path / "mab-variants.json", [{
        "context": "The deployment office is called NYC.",
        "questions": ["Which deployment office is used?"],
        "answers": [["New York", "NYC"]],
        "metadata": {"qa_pair_ids": ["variant-q"]},
    }])

    report = run(load_memoryagentbench(path), k=1)

    assert report["answer_token_recall"] == 1.0
    assert report["evidence_label_cardinality"]["max"] >= 1
    assert "sufficient_evidence_proxy_rate" in report


def test_memoryagentbench_no_gold_is_answer_scored_but_retrieval_excluded(
    tmp_path, capsys,
):
    path = _write_json(tmp_path / "mab-no-gold.json", [{
        "context": "The deployment note contains no region name.",
        "questions": ["Which region is preferred?"],
        "answers": [["us-east-1", "US East"]],
        "metadata": {"qa_pair_ids": ["no-gold-q"]},
    }])
    artifact_path = tmp_path / "no-gold-artifact.json"

    assert main([
        "--dataset", path,
        "--format", "memoryagentbench",
        "--artifact", str(artifact_path),
    ]) == 0
    capsys.readouterr()
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    metrics = artifact["metrics"]
    record = artifact["records"][0]

    assert metrics["retrieval_scored_questions"] == 0
    assert all(
        metrics[field] is None
        for field in ("recall_at_k", "hit_at_k", "mrr_at_k", "ndcg_at_k")
    )
    assert record["retrieval_excluded"] == "no_gold_evidence"
    assert record["answer_scored"] is True
    assert record["answer_token_recall"] == 0.0


def test_memoryagentbench_accepts_hugging_face_rows_envelope(tmp_path):
    path = _write_json(tmp_path / "mab-hf-rows.json", {
        "rows": [{
            "row": {
                "context": "The preferred deployment region is us-east-1.",
                "questions": ["Which region is preferred?"],
                "answers": [["us-east-1", "US East"]],
                "metadata": {"qa_pair_ids": ["hf-q"], "source": "official-split"},
            },
        }],
    })

    question = load_memoryagentbench(path)[0]["questions"][0]

    assert question["id"] == "hf-q"
    assert question["supporting"]


def test_locomo_plus_unified_input_maps_cue_evidence_to_source_chunks(tmp_path):
    path = _write_json(tmp_path / "plus.json", [{
        "id": "cognitive-1",
        "input_prompt": (
            "Session one: Morgan told Lee that she prefers oat milk in coffee.\n\n"
            "Session two: They discussed a new project deadline."
        ),
        "trigger": "What milk should Morgan receive with her coffee?",
        "evidence": "Morgan：Morgan told Lee that she prefers oat milk in coffee.",
        "category": "Cognitive",
    }])
    cases = load_locomo_plus(path)
    assert cases[0]["questions"][0]["supporting"] == ["cognitive-1:0"]
    assert cases[0]["questions"][0]["answer"].endswith("coffee.")
    assert run(cases, k=2)["recall_at_k"] == 1.0


def test_locomo_plus_keeps_dialogue_evidence_out_of_character_boundaries(tmp_path):
    cue = "When my toddler walked away, I found him safely by the fountain."
    path = _write_json(tmp_path / "dialogue-plus.json", [{
        "id": "dialogue-boundary",
        "input_prompt": f"{'Earlier context. ' * 58}\nEvan said, \"{cue}\"\nLater context.",
        "trigger": "Where was Evan's toddler found?",
        "evidence": f"Evan：{cue}",
        "category": "Cognitive",
    }])

    case = load_locomo_plus(path)[0]

    assert case["questions"][0]["supporting"]
    assert any(
        cue in memory["text"]
        for memory in case["memories"]
        if memory["tag"] in case["questions"][0]["supporting"]
    )


def test_locomo_plus_defaults_to_only_the_new_cognitive_category(tmp_path):
    path = _write_json(tmp_path / "mixed-plus.json", [{
        "id": "original",
        "input_prompt": "Original factual evidence.",
        "trigger": "What is original?",
        "evidence": "Original factual evidence.",
        "category": "single-hop",
    }, {
        "id": "cognitive",
        "input_prompt": "Earlier cue: Morgan requires oat milk.",
        "trigger": "What should Morgan receive later?",
        "evidence": "Morgan requires oat milk.",
        "category": "Cognitive",
    }])

    assert [case["id"] for case in load_locomo_plus(path)] == ["cognitive"]
    assert [case["id"] for case in load_locomo_plus(
        path, include_original_locomo=True,
    )] == ["original", "cognitive"]


def test_locomo_plus_rejects_evidence_not_present_in_unified_input(tmp_path):
    path = _write_json(tmp_path / "bad-plus.json", [{
        "input_prompt": "Only an unrelated conversation.",
        "trigger": "What happened?",
        "evidence": "missing cue",
        "category": "Cognitive",
    }])
    with pytest.raises(ValueError, match="did not occur"):
        load_locomo_plus(path)


def test_mem2actbench_pairs_source_sessions_and_scores_tool_argument_coverage(tmp_path):
    conversations = _write_jsonl(tmp_path / "sessions.jsonl", [{
        "session_id": "s-1",
        "original_conversation_ids": ["source-1", "other"],
        "turns": [
            {"role": "user", "source_id": "source-1", "content": "My preferred city is Boston."},
            {"role": "assistant", "source_id": "source-1", "content": "I will remember Boston."},
            {"role": "user", "source_id": "other", "content": "Unrelated note."},
        ],
    }])
    qa = _write_jsonl(tmp_path / "qa.jsonl", [{
        "qa_id": "tool-q",
        "source_conversation_ids": ["source-1"],
        "query": "Book a trip using my saved preferred city.",
        "tool_call": {"name": "book_trip", "arguments": {"city": "Boston"}},
        "complexity_metadata": {"level": "L2"},
    }])
    cases = load_mem2actbench(qa, conversations)
    assert cases[0]["memories"][0]["tag"] == "source-1"
    assert cases[0]["questions"][0]["answer"] == (
        '{"arguments": {"city": "Boston"}, "name": "book_trip"}'
    )
    report = run(cases, k=3)
    assert report["recall_at_k"] == 1.0
    assert report["answer_token_recall"] > 0.0


def test_mem2actbench_requires_a_matching_session_source(tmp_path):
    conversations = _write_jsonl(tmp_path / "sessions.jsonl", [{
        "session_id": "s-1", "original_conversation_ids": ["known"],
        "turns": [{"role": "user", "source_id": "known", "content": "fact"}],
    }])
    qa = _write_jsonl(tmp_path / "qa.jsonl", [{
        "qa_id": "missing", "source_conversation_ids": ["unknown"], "query": "q",
        "tool_call": {"name": "tool", "arguments": {}},
    }])
    with pytest.raises(ValueError, match="no session turns matched"):
        load_mem2actbench(qa, conversations)


def test_cli_requires_mem2act_conversations_before_running(tmp_path):
    path = _write_jsonl(tmp_path / "qa.jsonl", [])
    with pytest.raises(SystemExit) as error:
        main(["--dataset", path, "--format", "mem2actbench"])
    assert error.value.code == 2


def test_external_loader_rejects_non_positive_limits(tmp_path):
    path = _write_json(tmp_path / "plus.json", [{
        "input_prompt": "Earlier cue: Morgan requires oat milk.",
        "trigger": "What should Morgan receive later?",
        "evidence": "Morgan requires oat milk.",
        "category": "Cognitive",
    }])

    with pytest.raises(ValueError, match="limit must be a positive integer"):
        load_locomo_plus(path, limit=0)
    with pytest.raises(ValueError, match="limit must be a positive integer"):
        load_locomo_plus(path, limit=-1)


def test_cli_writes_redacted_immutable_artifact(tmp_path, capsys):
    query = "What milk should Morgan receive with coffee?"
    path = _write_json(tmp_path / "plus.json", [{
        "id": "cognitive-1",
        "input_prompt": "Morgan previously said oat milk is required.",
        "trigger": query,
        "evidence": "Morgan previously said oat milk is required.",
        "category": "Cognitive",
    }])
    artifact_path = tmp_path / "artifact.json"

    assert main([
        "--dataset", path,
        "--format", "locomo_plus",
        "--artifact", str(artifact_path),
    ]) == 0
    capsys.readouterr()

    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    serialized = json.dumps(artifact)
    assert query not in serialized
    assert "query_sha256" not in artifact["records"][0]
    assert artifact["suite"]["sources"][0]["name"] == "plus.json"
    assert artifact["metrics"]["claim_boundary"].startswith("Cue-evidence retrieval")
    assert "not generated-answer correctness" in artifact["metrics"]["sufficient_evidence_proxy_boundary"]
    assert artifact["protocol"]["config"]["limit"] is None
    assert "--limit" not in artifact["protocol"]["command"]
    assert artifact_path.with_name("artifact.json.sha256").is_file()


@pytest.mark.parametrize("changed", ["dataset", "conversations", "producer"])
def test_cli_rejects_artifact_drift_after_evaluation(tmp_path, monkeypatch, capsys, changed):
    conversations = _write_jsonl(tmp_path / "sessions.jsonl", [{
        "session_id": "s-1", "original_conversation_ids": ["source-1"],
        "turns": [{"role": "user", "source_id": "source-1", "content": "My city is Boston."}],
    }])
    dataset = _write_jsonl(tmp_path / "qa.jsonl", [{
        "qa_id": "tool-q", "source_conversation_ids": ["source-1"],
        "query": "Book a trip to my city.",
        "tool_call": {"name": "book_trip", "arguments": {"city": "Boston"}},
    }])
    producer = tmp_path / "producer.py"
    producer.write_text("# evaluated producer\n")
    monkeypatch.setattr(agent_benchmarks, "_producer_snapshot",
                        lambda: {str(producer): agent_benchmarks.sha256_file(producer)})
    changed_path = {"dataset": agent_benchmarks.Path(dataset),
                    "conversations": agent_benchmarks.Path(conversations), "producer": producer}[changed]

    def late_print(*args, **kwargs):
        changed_path.write_bytes(changed_path.read_bytes() + b"\n")

    monkeypatch.setattr(agent_benchmarks, "print", late_print, raising=False)
    artifact = tmp_path / "artifact.json"
    with pytest.raises(SystemExit) as error:
        main(["--dataset", dataset, "--conversations", conversations,
              "--format", "mem2actbench", "--artifact", str(artifact)])

    assert error.value.code == 2
    assert "evaluated producer or data snapshots" in capsys.readouterr().err
    assert not artifact.exists()
    assert not artifact.with_suffix(".json.sha256").exists()


def test_cli_records_a_selected_limit_in_its_public_artifact(tmp_path, capsys):
    path = _write_json(tmp_path / "plus.json", [{
        "id": f"cognitive-{index}",
        "input_prompt": f"Earlier cue {index}.",
        "trigger": f"What happened at {index}?",
        "evidence": f"Earlier cue {index}.",
        "category": "Cognitive",
    } for index in range(2)])
    artifact_path = tmp_path / "limited-artifact.json"

    assert main([
        "--dataset", path, "--format", "locomo_plus", "--limit", "1",
        "--artifact", str(artifact_path),
    ]) == 0
    capsys.readouterr()

    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert artifact["protocol"]["config"]["limit"] == 1
    assert artifact["protocol"]["command"][-2:] == ["--limit", "1"]


def test_cli_records_selected_modes_in_its_public_artifact(tmp_path, capsys, monkeypatch):
    path = _write_json(tmp_path / "plus.json", [{
        "id": "cognitive-1",
        "input_prompt": "Earlier cue.",
        "trigger": "What happened?",
        "evidence": "Earlier cue.",
        "category": "Cognitive",
    }])
    artifact_path = tmp_path / "modes-artifact.json"
    selected_revision = "a" * 40
    loaded = {}

    def factory(model, *, revision):
        loaded.update(model=model, revision=revision)
        return DeterministicEmbedder()

    monkeypatch.setattr(
        "eval.agent_benchmarks.get_embedder",
        factory,
    )

    assert main([
        "--dataset", path, "--format", "locomo_plus", "--embed-model", "test/model",
        "--embed-revision", selected_revision,
        "--no-resolve", "--include-original-locomo", "--artifact", str(artifact_path),
    ]) == 0
    capsys.readouterr()

    assert loaded == {"model": "test/model", "revision": selected_revision}
    command = json.loads(artifact_path.read_text(encoding="utf-8"))["protocol"]["command"]
    assert ["--embed-model", "test/model"] == command[command.index("--embed-model"):][:2]
    assert ["--embed-revision", selected_revision] == (
        command[command.index("--embed-revision"):][:2]
    )
    assert "--no-resolve" in command
    assert "--include-original-locomo" in command


def test_cli_reports_embedder_factory_fallback_honestly(tmp_path, capsys, monkeypatch):
    path = _write_json(tmp_path / "plus.json", [{
        "input_prompt": "Morgan previously said oat milk is required.",
        "trigger": "What milk should Morgan receive?",
        "evidence": "Morgan previously said oat milk is required.",
        "category": "Cognitive",
    }])
    monkeypatch.setattr(
        "eval.agent_benchmarks.get_embedder",
        lambda _model, *, revision: DeterministicEmbedder(),
    )

    assert main([
        "--dataset", path, "--format", "locomo_plus", "--embed-model", "unavailable/model",
        "--embed-revision", "b" * 40,
    ]) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["offline"] is True
    assert report["embedder"]["implementation"] == "DeterministicEmbedder"


def test_cli_requires_a_pinned_embedder_revision(tmp_path):
    path = _write_json(tmp_path / "plus.json", [{
        "input_prompt": "Earlier cue.",
        "trigger": "What happened?",
        "evidence": "Earlier cue.",
        "category": "Cognitive",
    }])

    with pytest.raises(SystemExit) as error:
        main(["--dataset", path, "--format", "locomo_plus", "--embed-model", "test/model"])

    assert error.value.code == 2


def test_unlabeled_sources_do_not_get_packed_retrieval_credit(tmp_path, capsys):
    source = _write_json(tmp_path / "missing.json", [{
        "context": "A blue bicycle is stored here.",
        "questions": ["Which city is mentioned?"], "answers": ["Stockholm"],
    }])
    artifact = tmp_path / "artifact.json"
    assert main(["--dataset", source, "--format", "memoryagentbench", "--artifact", str(artifact)]) == 0
    capsys.readouterr()
    report = json.loads(artifact.read_text())
    assert report["metrics"]["retrieval_scored_questions"] == 0
    assert report["metrics"]["packed_recall_at_k"] is None
    assert report["records"][0]["retrieval_scored"] is False
    assert "packed_recall_at_k" not in report["records"][0]
    assert report["records"][0]["answer_scored"] is True


def test_repeated_accepted_answers_preserve_meaning_and_validate(tmp_path):
    from eval.harness import _validate_dataset

    source = _write_json(tmp_path / "variants.json", [{
        "context": "The setting is 42.", "questions": ["What is the setting?"],
        "answers": [["42", "42", "forty two", "42"]],
    }])
    cases = load_memoryagentbench(source)
    _validate_dataset(cases)
    assert cases[0]["questions"][0]["answer_variants"] == ["42", "forty two"]


def test_reused_upstream_question_ids_keep_all_context_variants(tmp_path):
    from eval.harness import _validate_dataset

    source = _write_json(tmp_path / "contexts.json", [{
        "id": f"context-{i}", "context": f"The setting is {i}.",
        "questions": ["What is the setting?"], "answers": [str(i)],
        "qa_pair_ids": ["shared-upstream-question"],
    } for i in range(2)])
    cases = load_memoryagentbench(source)
    _validate_dataset(cases)
    questions = [case["questions"][0] for case in cases]
    assert len({q["id"] for q in questions}) == 2
    assert {q["source_question_id"] for q in questions} == {"shared-upstream-question"}
    assert [q["answer"] for q in questions] == ["0", "1"]


def test_checkpoint_reuses_cases_and_rejects_source_drift(tmp_path, capsys, monkeypatch):
    from eval import agent_benchmarks as module
    from eval.external_checkpoints import run_resumable

    source = _write_json(tmp_path / "plus.json", [{
        "id": "cognitive-1", "input_prompt": "Morgan needs oat milk.",
        "trigger": "What milk does Morgan need?", "evidence": "Morgan needs oat milk.",
        "category": "Cognitive",
    }])
    executions = []

    def measured_runner(cases, **kwargs):
        executions.append(len(cases))
        return run(cases, **kwargs)

    def checkpointed(cases, **kwargs):
        return run_resumable(cases, runner=measured_runner, **kwargs)

    monkeypatch.setattr(module, "run_resumable", checkpointed)
    arguments = ["--dataset", source, "--format", "locomo_plus", "--token-budget", "64",
                 "--checkpoint-dir", str(tmp_path / "private")]
    assert main(arguments) == 0
    assert main(arguments) == 0
    capsys.readouterr()
    assert executions == [1]
    with open(source, "a", encoding="utf-8") as handle:
        handle.write("\n")
    with pytest.raises(SystemExit) as error:
        main(arguments)
    assert error.value.code == 2
