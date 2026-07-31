"""Contracts for the deterministic workload context economy benchmark."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from engraphis.core.context import RegexTokenCounter
from engraphis.backends import DeterministicEmbedder
from eval.context_economy import TOKEN_COUNTER_IDENTITY, main, run
from eval.harness import load_dataset


ROOT = Path(__file__).resolve().parents[1]


def _dataset() -> list[dict]:
    return [{
        "id": "economy",
        "memories": [
            {"tag": "policy", "text": "The deployment approval owner is the release manager."},
            {"tag": "noise-1", "text": "The cafeteria menu rotates every Monday."},
            {"tag": "noise-2", "text": "The office plant watering schedule is Friday morning."},
            {"tag": "noise-3", "text": "The team lunch reservation is at noon on Thursday."},
        ],
        "questions": [{
            "id": "owner",
            "q": "Who owns deployment approval?",
            "answer": "release manager",
            "supporting": ["policy"],
        }] * 3,
    }]


def test_workload_report_compares_full_history_recency_and_shipped_packing() -> None:
    report = run(_dataset(), token_budget=24, k=3)

    assert report["benchmark"]["token_counter"] == TOKEN_COUNTER_IDENTITY
    assert "not provider billing" in report["benchmark"]["non_billing_scope"]
    assert set(report["methods"]) == {"full_history", "recency_window", "engraphis"}
    assert report["workload"] == {
        "cases": 1,
        "queries": 3,
        "scored_queries": 3,
        "one_time_indexing_tokens": sum(
            RegexTokenCounter()(memory["text"]) for memory in _dataset()[0]["memories"]
        ),
    }
    assert report["methods"]["full_history"]["quality"] == {
        "retrieval_recall": 1.0,
        "retrieval_hit_rate": 1.0,
        "answer_token_recall": 1.0,
    }
    assert report["methods"]["recency_window"]["quality"]["retrieval_recall"] == 0.0
    assert report["methods"]["engraphis"]["quality"]["retrieval_recall"] == 1.0
    assert all(
        row["context_tokens"] <= 24
        for method in ("recency_window", "engraphis")
        for row in report["detail"][method]
    )
    comparison = report["engraphis_vs_full_history"]
    assert comparison["one_time_indexing_inclusive_total_tokens"] == (
        report["workload"]["one_time_indexing_tokens"]
        + report["methods"]["engraphis"]["cumulative_query_context_tokens"]
    )
    assert comparison["break_even_query_count"] is not None


def test_report_is_deterministic_and_unanswerable_queries_are_not_quality_scored() -> None:
    data = _dataset()
    data[0]["questions"].append({
        "id": "off-topic", "q": "What is the moon made of?", "supporting": [], "answerable": False,
    })

    first = run(data, token_budget=24, k=3)
    second = run(data, token_budget=24, k=3)

    assert first == second
    assert first["workload"]["queries"] == 4
    assert first["workload"]["scored_queries"] == 3
    assert first["methods"]["engraphis"]["scored_queries"] == 3


def test_zero_budget_has_no_engraphis_break_even_and_rejects_invalid_parameters() -> None:
    report = run(_dataset(), token_budget=0)

    assert report["methods"]["engraphis"]["cumulative_query_context_tokens"] == 0
    assert report["methods"]["recency_window"]["cumulative_query_context_tokens"] == 0
    assert report["engraphis_vs_full_history"]["break_even_query_count"] is not None
    with pytest.raises(ValueError, match="token_budget"):
        run(_dataset(), token_budget=-1)
    with pytest.raises(ValueError, match="positive"):
        run(_dataset(), k=0)


def test_conflict_resolution_mode_is_explicit_in_report() -> None:
    resolved = run(_dataset(), token_budget=24)
    raw_turns = run(_dataset(), token_budget=24, resolve_conflicts=False)

    assert resolved["benchmark"]["resolve_conflicts"] is True
    assert raw_turns["benchmark"]["resolve_conflicts"] is False


def test_injected_embedder_is_used_and_recorded_without_changing_token_accounting() -> None:
    class InjectedEmbedder:
        model_name = "test/semantic-embedder"
        revision = "test-revision"

        def __init__(self) -> None:
            self.delegate = DeterministicEmbedder(96)
            self.calls = 0

        @property
        def dim(self) -> int:
            return self.delegate.dim

        def embed(self, texts, *, kind="text"):
            self.calls += 1
            return self.delegate.embed(texts, kind=kind)

    embedder = InjectedEmbedder()
    report = run(_dataset(), token_budget=24, k=3, embedder=embedder)

    assert embedder.calls > 0
    assert report["benchmark"]["offline"] is False
    assert report["benchmark"]["embedder"] == {
        "name": "InjectedEmbedder",
        "model_id": "test/semantic-embedder",
        "revision": "test-revision",
        "dimension": 96,
    }
    assert report["benchmark"]["token_counter"] == TOKEN_COUNTER_IDENTITY


def test_cli_emits_one_json_document(tmp_path, capsys) -> None:
    path = tmp_path / "dataset.jsonl"
    path.write_text(json.dumps(_dataset()[0]) + "\n", encoding="utf-8")

    main(["--dataset", str(path), "--token-budget", "24", "--k", "3"])

    output = json.loads(capsys.readouterr().out)
    assert output["benchmark"]["name"] == "engraphis-context-economy/v1"
    assert output["benchmark"]["dataset_format"] == "harness"
    assert output["workload"]["queries"] == 3
    assert "detail" not in output
    assert "non_billing_scope" not in output["benchmark"]


def test_cli_never_prints_dataset_identifiers_or_source_text(tmp_path, capsys) -> None:
    private = {
        "id": "private-case",
        "memories": [{"tag": "private-tag", "text": "private source text"}],
        "questions": [{
            "id": "private-question", "q": "private question", "answer": "private answer",
            "supporting": ["private-tag"],
        }],
    }
    path = tmp_path / "dataset.jsonl"
    path.write_text(json.dumps(private) + "\n", encoding="utf-8")

    main(["--dataset", str(path)])

    output = capsys.readouterr().out
    assert "private-case" not in output
    assert "private-tag" not in output
    assert "private source text" not in output


@pytest.mark.parametrize(
    ("format_name", "payload"),
    [
        ("locomo", [{
            "sample_id": "locomo-case",
            "conversation": {
                "session_1": [{
                    "speaker": "Ava", "dia_id": "D1:1", "text": "The release is Tuesday.",
                }],
            },
            "qa": [{
                "question": "When is the release?", "answer": "Tuesday", "evidence": ["D1:1"],
            }],
        }]),
        ("longmemeval", [{
            "question_id": "lme-case",
            "question": "What day is the release?",
            "answer": "Tuesday",
            "haystack_session_ids": ["s1"],
            "haystack_dates": ["2024-01-01"],
            "haystack_sessions": [[{
                "role": "user", "content": "The release is Tuesday.",
            }]],
            "answer_session_ids": ["s1"],
        }]),
    ],
)
def test_cli_selects_established_external_loader(format_name, payload, tmp_path, capsys) -> None:
    path = tmp_path / f"{format_name}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    main([
        "--dataset", str(path), "--format", format_name, "--token-budget", "32",
        "--no-resolve",
    ])

    output = json.loads(capsys.readouterr().out)
    assert output["benchmark"]["dataset_format"] == format_name
    assert output["benchmark"]["resolve_conflicts"] is False
    assert output["workload"]["cases"] == 1
    assert output["workload"]["queries"] == 1


def test_cli_embed_model_uses_factory_without_downloading(tmp_path, capsys, monkeypatch) -> None:
    path = tmp_path / "dataset.jsonl"
    path.write_text(json.dumps(_dataset()[0]) + "\n", encoding="utf-8")
    selected = DeterministicEmbedder(80)
    seen = []

    def fake_get_embedder(model_name, dim):
        seen.append((model_name, dim))
        return selected

    monkeypatch.setattr("eval.context_economy.get_embedder", fake_get_embedder)
    main(["--dataset", str(path), "--embed-model", "test/local-model", "--dim", "80"])

    output = json.loads(capsys.readouterr().out)
    assert seen == [("test/local-model", 80)]
    assert output["benchmark"]["offline"] is True
    assert output["benchmark"]["embedder"]["dimension"] == 80


def test_codemem_public_no_break_even_boundary_is_reproducible() -> None:
    dataset = load_dataset(str(ROOT / "eval" / "datasets" / "codemem.jsonl"))

    tight = run(dataset, token_budget=64, k=5)
    roomy = run(dataset, token_budget=512, k=5)

    assert tight["workload"] == {
        "cases": 14,
        "queries": 26,
        "scored_queries": 26,
        "one_time_indexing_tokens": 631,
    }
    assert tight["methods"]["full_history"]["cumulative_query_context_tokens"] == 1180
    assert tight["methods"]["recency_window"]["cumulative_query_context_tokens"] == 1180
    assert tight["methods"]["engraphis"]["cumulative_query_context_tokens"] == 1375
    assert roomy["methods"]["engraphis"]["cumulative_query_context_tokens"] == 1377
    for report in (tight, roomy):
        for method in report["methods"].values():
            assert method["quality"] == {
                "retrieval_recall": 1.0,
                "retrieval_hit_rate": 1.0,
                "answer_token_recall": 1.0,
            }
        assert report["engraphis_vs_full_history"]["break_even_query_count"] is None
