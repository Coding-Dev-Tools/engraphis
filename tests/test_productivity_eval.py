"""End-to-end task, correction, latency, and token benchmark contracts."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from engraphis.core.context import RegexTokenCounter
from eval.productivity import (
    AgentTurn,
    DeterministicTaskAgent,
    TOKEN_COUNTER_IDENTITY,
    _public_report,
    _turn,
    main,
    run,
)
from eval.harness import load_dataset


ROOT = Path(__file__).resolve().parents[1]


def _small_dataset() -> list[dict]:
    return [{
        "id": "small",
        "memories": [
            {
                "tag": "owner",
                "text": "The release manager owns deployment approval.",
            },
            {"tag": "noise", "text": "Lunch begins at noon."},
        ],
        "questions": [{
            "id": "approval",
            "q": "Who owns deployment approval?",
            "answer": "release manager",
            "supporting": ["owner"],
        }],
    }]


def test_productivity_report_measures_outcomes_corrections_turns_and_all_tokens() -> None:
    report = run(
        _small_dataset(),
        max_context_tokens=128,
        retrieval_token_budget=0,
    )

    assert report["benchmark"]["name"] == "engraphis-agent-productivity/v1"
    assert report["benchmark"]["token_counter"] == TOKEN_COUNTER_IDENTITY
    assert report["workload"] == {"cases": 1, "tasks": 1}
    full = report["methods"]["full_history"]
    retrieval = report["methods"]["retrieval"]
    adaptive = report["methods"]["adaptive"]
    assert full["completion_rate"] == 1.0
    assert full["first_attempt_errors"] == 0
    assert retrieval["completion_rate"] == 1.0
    assert retrieval["first_attempt_errors"] == 1
    assert retrieval["mistakes"] == 0
    assert retrieval["abstentions"] == 1
    assert retrieval["corrections"] == 1
    assert retrieval["successful_corrections"] == 1
    assert retrieval["agent_turns"] == 2
    assert retrieval["memory_calls"] == 1
    assert retrieval["total_tokens"] == (
        retrieval["input_tokens"] + retrieval["output_tokens"]
    )
    assert adaptive["completion_rate"] == 1.0
    assert adaptive["first_attempt_errors"] == 0
    assert adaptive["memory_calls"] == 0
    assert adaptive["context_modes"] == {"history_bypass": 1}


def test_productivity_completion_oracle_rejects_a_negated_answer() -> None:
    class NegatingAgent:
        def __call__(self, question, context):
            del question, context
            return "The release manager does not own deployment approval."

    report = run(_small_dataset(), agent=NegatingAgent())

    for method in report["methods"].values():
        assert method["completion_rate"] == 0.0
        assert method["wrong_answers"] == 1
        assert method["corrections"] == 1
        assert method["successful_corrections"] == 0


def test_productivity_accepts_an_injected_case_aware_answer_evaluator() -> None:
    def evaluator(response, question, supporting_evidence):
        return response == question["answer"].upper() and not supporting_evidence

    data = _small_dataset()
    data[0]["questions"][0].pop("supporting")

    report = run(
        data,
        agent=lambda _question, _context: "RELEASE MANAGER",
        answer_evaluator=evaluator,
    )

    assert all(method["completion_rate"] == 1.0 for method in report["methods"].values())


def test_large_history_routes_between_strong_retrieval_and_weak_widening() -> None:
    memories = [
        {
            "tag": "owner",
            "text": "The release manager owns deployment approval.",
        },
    ] + [
        {
            "tag": f"noise-{number}",
            "text": f"Operational note {number} records a green background status.",
        }
        for number in range(40)
    ]
    dataset = [{
        "id": "large",
        "memories": memories,
        "questions": [{
            "q": "Who owns deployment approval?",
            "answer": "release manager",
            "supporting": ["owner"],
        }],
    }]

    strong = run(
        dataset,
        max_context_tokens=80,
        retrieval_token_budget=32,
        confidence_floor=0.25,
    )
    weak = run(
        dataset,
        max_context_tokens=80,
        retrieval_token_budget=32,
        confidence_floor=0.99,
    )

    assert strong["methods"]["adaptive"]["context_modes"] == {"retrieval": 1}
    assert weak["methods"]["adaptive"]["context_modes"] == {"history_fallback": 1}
    assert weak["methods"]["adaptive"]["memory_calls"] == 1


def test_productivity_caps_full_history_and_correction_attempt_contexts() -> None:
    dataset = [{
        "id": "large-history",
        "memories": [{"text": " ".join(["background"] * 80)}],
        "questions": [{"q": "Who owns deployment?", "answer": "release manager"}],
    }]
    attempts = []

    class AbstainingAgent:
        def __call__(self, question, context):
            attempts.append((question, context))
            return ""

    budget = 8
    run(
        dataset,
        agent=AbstainingAgent(),
        max_context_tokens=budget,
        retrieval_token_budget=budget,
    )

    counter = RegexTokenCounter()
    assert attempts
    assert any(question.startswith("Correct the answer") for question, _ in attempts)
    assert all(counter(context) <= budget for _, context in attempts)


def test_latency_uses_injected_clock_and_agent_identity_is_explicit() -> None:
    class Agent:
        identity = "test-agent"
        deterministic = True

        def __call__(self, question, context):
            return DeterministicTaskAgent()(question, context)

    ticks = iter(number / 1000 for number in range(20))
    report = run(
        _small_dataset(),
        agent=Agent(),
        clock=lambda: next(ticks),
        max_context_tokens=128,
        retrieval_token_budget=32,
    )

    assert report["benchmark"]["agent"] == {
        "implementation": "Agent",
        "identity": "test-agent",
        "deterministic": True,
        "reported_models": [],
    }
    for method in report["methods"].values():
        assert method["latency_ms"] == {"mean": 1.0, "p50": 1.0, "p95": 1.0}


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"k": 0}, "k"),
        ({"max_context_tokens": -1}, "max_context_tokens"),
        (
            {"max_context_tokens": 8, "retrieval_token_budget": 9},
            "retrieval_token_budget",
        ),
        ({"confidence_floor": float("inf")}, "confidence_floor"),
    ],
)
def test_productivity_benchmark_rejects_invalid_policy_values(kwargs, message) -> None:
    with pytest.raises(ValueError, match=message):
        run(_small_dataset(), **kwargs)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("input_tokens", -0.5),
        ("cached_input_tokens", "7"),
        ("output_tokens", float("inf")),
        ("reasoning_output_tokens", True),
    ],
)
def test_provider_token_telemetry_requires_finite_non_negative_integers(field, value) -> None:
    with pytest.raises(ValueError, match=field):
        _turn(AgentTurn(answer="answer", **{field: value}))


def test_provider_telemetry_records_safe_model_provenance() -> None:
    class Agent:
        identity = "hosted-test-agent"

        def __call__(self, question, context):
            return AgentTurn(
                answer="release manager",
                input_tokens=7,
                cached_input_tokens=2,
                output_tokens=3,
                reasoning_output_tokens=1,
                total_tokens=10,
                latency_ms=4.5,
                model="example/agent@0123456789abcdef0123456789abcdef01234567",
            )

    report = run(_small_dataset(), agent=Agent())
    expected_model = "example/agent@0123456789abcdef0123456789abcdef01234567"

    assert report["benchmark"]["agent"]["reported_models"] == [expected_model]
    for method in report["methods"].values():
        assert method["provider_usage"]["input_tokens"] == 7
        assert method["provider_usage"]["models"] == [expected_model]
    assert report["detail"]["full_history"][0]["provider"]["models"] == [expected_model]

    class CredentialShapedModel(Agent):
        def __call__(self, question, context):
            turn = super().__call__(question, context)
            return AgentTurn(**{**turn.__dict__, "model": "api_key=not-for-publication"})

    redacted = _public_report(run(_small_dataset(), agent=CredentialShapedModel()))
    assert "not-for-publication" not in json.dumps(redacted)
    assert redacted["benchmark"]["agent"]["reported_models"][0].startswith(
        "redacted_sha256:"
    )


def test_cli_prints_aggregate_report_without_private_task_or_source_data(
    tmp_path, capsys,
) -> None:
    private = _small_dataset()
    private[0]["id"] = "PRIVATE-CASE"
    private[0]["questions"][0]["id"] = "PRIVATE-TASK"
    private[0]["memories"][0]["text"] += " PRIVATE-SOURCE"
    path = tmp_path / "private.jsonl"
    path.write_text(json.dumps(private[0]) + "\n", encoding="utf-8")

    main([
        "--dataset",
        str(path),
        "--max-context-tokens",
        "128",
        "--retrieval-token-budget",
        "32",
    ])

    output = capsys.readouterr().out
    payload = json.loads(output)
    assert "detail" not in payload
    assert "PRIVATE-CASE" not in output
    assert "PRIVATE-TASK" not in output
    assert "PRIVATE-SOURCE" not in output


def test_codemem_small_history_bypass_marketing_numbers_are_reproducible() -> None:
    report = run(
        load_dataset(str(ROOT / "eval" / "datasets" / "codemem.jsonl")),
        max_context_tokens=512,
        retrieval_token_budget=256,
    )

    assert report["workload"] == {"cases": 14, "tasks": 26}
    assert report["methods"]["full_history"]["tasks_completed"] == 24
    assert report["methods"]["full_history"]["total_tokens"] == 1942
    assert report["methods"]["retrieval"]["tasks_completed"] == 24
    assert report["methods"]["retrieval"]["total_tokens"] == 2194
    assert report["methods"]["retrieval"]["memory_calls"] == 26
    assert report["methods"]["adaptive"]["tasks_completed"] == 24
    assert report["methods"]["adaptive"]["total_tokens"] == 1942
    assert report["methods"]["adaptive"]["memory_calls"] == 0
    assert report["methods"]["adaptive"]["context_modes"] == {
        "history_bypass": 26,
    }


def test_strategy_order_is_explicit_and_stateful_agent_attempts_are_identified() -> None:
    class PreparedAgent:
        identity = "prepared-fake"
        deterministic = True

        def __init__(self):
            self.attempts = []

        def prepare_attempt(self, **identity):
            self.attempts.append(identity)

        def __call__(self, question, context):
            return "release manager"

    agent = PreparedAgent()
    order = ("adaptive", "full_history", "retrieval")
    report = run(_small_dataset(), agent=agent, strategy_order=order)

    assert report["benchmark"]["strategy_order"] == list(order)
    assert [attempt["strategy"] for attempt in agent.attempts] == list(order)
    assert all(attempt["task_ordinal"] == 0 for attempt in agent.attempts)
    assert all(attempt["turn_ordinal"] == 0 for attempt in agent.attempts)
