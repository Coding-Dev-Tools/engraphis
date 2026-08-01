"""End-to-end agent productivity benchmark for context strategies.

Unlike retrieval-only evaluations, this benchmark runs a complete answer loop:
context selection, an agent attempt, outcome scoring, and (when needed) one
full-history correction attempt.  It reports task completion, first-attempt
errors, abstentions, corrections, agent turns, memory calls, latency, and all
model-facing input/output tokens under a named counter.

The bundled agent is deterministic and offline.  It selects the most
question-relevant evidence sentence without seeing the expected answer.  Callers
can inject a real agent callable with the same ``(question, context) -> answer``
shape; the report records the implementation so proxy and model results cannot
be confused.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import time
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Optional, Union

from engraphis.backends import DeterministicEmbedder, NumpyVectorIndex
from engraphis.backends.reranker import IdentityReranker
from engraphis.core.adaptive_context import fit_recent_history
from engraphis.core.context import DeterministicContextPacker, RegexTokenCounter
from engraphis.core.engine import MemoryEngine
from engraphis.core.interfaces import MemoryType, Scope
from engraphis.core.store import Store
from engraphis.core.textutil import tokenize
from eval.harness import _seed_case_graph, load_dataset


DEFAULT_MAX_CONTEXT_TOKENS = 512
DEFAULT_RETRIEVAL_TOKENS = 256
DEFAULT_K = 5
STRATEGIES = ("full_history", "retrieval", "adaptive")
TOKEN_COUNTER_IDENTITY = RegexTokenCounter.identity
_QUESTION_TERMS = frozenset({
    "a", "an", "are", "did", "do", "does", "for", "how", "in", "is", "it",
    "of", "on", "the", "to", "was", "were", "what", "when", "where", "which",
    "who", "why",
})
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+|\n+")
_CORRECTION_PROMPT = "Correct the answer using the wider history."
_PUBLIC_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@:+-]{0,199}$")
_SENSITIVE_MODEL_SEGMENT = re.compile(
    r"(?i)(?:^|[-_.])(api|auth|bearer|credential|key|password|secret|sk|token)(?:[-_.]|$)"
)


@dataclass(frozen=True)
class AgentTurn:
    """Optional provider telemetry returned by a real task agent.

    Plain strings remain the stable offline-agent contract.  Hosted adapters may
    return this object instead; provider counters are kept separate from the
    deterministic regex counter used by the fixture.
    """

    answer: str
    input_tokens: Optional[int] = None
    cached_input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    reasoning_output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    latency_ms: Optional[float] = None
    model: Optional[str] = None


def _turn(value: Union[str, AgentTurn, None]) -> AgentTurn:
    if isinstance(value, AgentTurn):
        normalized: dict[str, Union[int, float]] = {}
        for field in (
            "input_tokens", "cached_input_tokens", "output_tokens",
            "reasoning_output_tokens", "total_tokens",
        ):
            metric = getattr(value, field)
            if metric is None:
                continue
            if (
                isinstance(metric, bool)
                or type(metric) not in (int, float)
                or not math.isfinite(float(metric))
                or int(metric) != metric
                or metric < 0
            ):
                raise ValueError(f"agent {field} must be a non-negative integer")
            normalized[field] = int(metric)
        if value.latency_ms is not None:
            latency = value.latency_ms
            if (
                isinstance(latency, bool)
                or type(latency) not in (int, float)
                or not math.isfinite(float(latency))
                or latency < 0
            ):
                raise ValueError("agent latency_ms must be a finite non-negative number")
            normalized["latency_ms"] = float(latency)
        return replace(value, **normalized)
    return AgentTurn(answer=str(value or ""))


def _public_model_identifier(value: object) -> Optional[str]:
    """Return a bounded public model ID without publishing credential-shaped input."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    if _PUBLIC_MODEL_ID.fullmatch(raw) and _SENSITIVE_MODEL_SEGMENT.search(raw) is None:
        return raw
    return "redacted_sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _prepare_agent_attempt(
    agent: object, *, strategy: str, task_ordinal: int, turn_ordinal: int,
) -> None:
    """Give stateful hosted adapters a content-free, stable attempt identity."""
    prepare = getattr(agent, "prepare_attempt", None)
    if callable(prepare):
        prepare(
            strategy=strategy,
            task_ordinal=task_ordinal,
            turn_ordinal=turn_ordinal,
        )


class DeterministicTaskAgent:
    """Offline evidence-selection agent that never receives the gold answer."""

    identity = "engraphis.deterministic-task-agent.v1"
    deterministic = True

    def __call__(self, question: str, context: str) -> str:
        query_terms = tokenize(question) - _QUESTION_TERMS
        sentences = [
            sentence.strip()
            for sentence in _SENTENCE_RE.split(str(context or ""))
            if sentence.strip() and not sentence.lstrip().startswith("[")
        ]
        if not sentences:
            return ""

        def score(item: tuple[int, str]) -> tuple[float, float, int]:
            index, sentence = item
            terms = tokenize(sentence)
            overlap = len(query_terms & terms)
            coverage = overlap / max(1, len(query_terms))
            density = overlap / max(1, len(terms))
            # Recent evidence is the deterministic tie-break, matching the raw
            # history fallback's task-state preservation policy.
            return (coverage + 0.25 * density, density, index)

        return max(enumerate(sentences), key=score)[1]


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return ordered[index]


AnswerEvaluator = Callable[[str, dict, tuple[str, ...]], bool]


def _normalized_answer(value: object) -> str:
    """Return a punctuation-insensitive canonical answer for fixture comparison."""
    return " ".join(re.findall(r"[\w-]+", str(value or "").casefold()))


def _completed(response: str, question: dict, supporting_evidence: tuple[str, ...]) -> bool:
    """Evaluate task success against a case's explicit answer and source evidence.

    Productivity completion is a correctness metric, not a retrieval metric: token
    containment lets statements such as ``the release manager does not approve``
    count as a successful answer to ``release manager``. The offline oracle accepts
    only a case's canonical answer, an explicitly listed acceptable answer, or an
    exact supporting evidence sentence. Hosted or paraphrasing benchmarks can
    inject an ``answer_evaluator`` into :func:`run` with richer semantics.
    """
    normalized_response = _normalized_answer(response)
    expected = str(question.get("answer", question.get("evidence", "")))
    if not _normalized_answer(expected):
        return bool(normalized_response)
    acceptable = [expected, *supporting_evidence]
    configured = question.get("acceptable_answers", ())
    if isinstance(configured, (list, tuple)):
        acceptable.extend(str(value) for value in configured)
    return normalized_response in {
        candidate for value in acceptable if (candidate := _normalized_answer(value))
    }


def _seed_case(
    case: dict,
    *,
    embedder: object,
    counter: Callable[[str], int],
) -> tuple[Store, MemoryEngine, str, str, str]:
    store = Store(":memory:")
    workspace_id = store.get_or_create_workspace("productivity")
    repo_id = store.get_or_create_repo(workspace_id, str(case.get("id", "case")))
    engine = MemoryEngine(
        store,
        embedder,
        NumpyVectorIndex(store),
        IdentityReranker(),
    )
    engine.recall_engine.context_packer = DeterministicContextPacker(
        token_counter=counter,
        token_counter_identity=TOKEN_COUNTER_IDENTITY,
    )
    _seed_case_graph(store, workspace_id=workspace_id, repo_id=repo_id, case=case)
    source_texts = []
    for memory in case.get("memories", []):
        content = str(memory.get("text", ""))
        source_texts.append(content)
        engine.remember(
            content,
            workspace_id=workspace_id,
            repo_id=repo_id,
            mtype=MemoryType.EPISODIC,
            scope=Scope.REPO,
            title=str(memory.get("title", "")),
            valid_from=memory.get("valid_from"),
            subject_key=str(memory.get("subject_key", "")),
            claim_kind=str(memory.get("claim_kind", "")),
            resolve_conflicts=False,
        )
    return store, engine, workspace_id, repo_id, "\n\n".join(source_texts)


def _attempt(
    *,
    method: str,
    question: str,
    history: str,
    engine: MemoryEngine,
    workspace_id: str,
    repo_id: str,
    k: int,
    max_context_tokens: int,
    retrieval_token_budget: int,
    confidence_floor: float,
    count_tokens: Callable[[str], int],
) -> tuple[str, str, int, str]:
    if method == "full_history":
        context, truncated = fit_recent_history(
            history,
            token_budget=max_context_tokens,
            count_tokens=count_tokens,
        )
        return (
            context,
            "full_history",
            0,
            "full history was capped to the prompt budget" if truncated else "",
        )
    if method == "retrieval":
        recalled = engine.recall(
            question,
            workspace_id=workspace_id,
            repo_id=repo_id,
            k=k,
            token_budget=retrieval_token_budget,
            candidate_depth="adaptive",
            reinforce=False,
        )
        return recalled.context, "retrieval", 1, ""
    adaptive = engine.adaptive_context(
        question,
        history,
        workspace_id=workspace_id,
        repo_id=repo_id,
        k=k,
        max_context_tokens=max_context_tokens,
        retrieval_token_budget=retrieval_token_budget,
        confidence_floor=confidence_floor,
        reinforce=False,
    )
    return (
        adaptive.context,
        adaptive.mode,
        int(adaptive.retrieved),
        adaptive.reason,
    )


def _summary(rows: list[dict]) -> dict:
    count = len(rows)
    latencies = [float(row["latency_ms"]) for row in rows]
    modes = Counter(str(row["context_mode"]) for row in rows)
    provider = {}
    for field in (
        "input_tokens", "cached_input_tokens", "output_tokens",
        "reasoning_output_tokens", "total_tokens", "latency_ms",
    ):
        values = [row["provider"][field] for row in rows]
        provider[field] = sum(values) if all(value is not None for value in values) else None
    provider["models"] = sorted({
        model
        for row in rows
        for model in row["provider"].get("models", [])
    })
    return {
        "tasks": count,
        "tasks_completed": sum(int(row["completed"]) for row in rows),
        "completion_rate": round(
            sum(int(row["completed"]) for row in rows) / max(1, count), 6
        ),
        "first_attempt_errors": sum(int(row["first_attempt_error"]) for row in rows),
        "mistakes": sum(int(row["wrong_answer"]) for row in rows),
        "wrong_answers": sum(int(row["wrong_answer"]) for row in rows),
        "abstentions": sum(int(row["abstained"]) for row in rows),
        "corrections": sum(int(row["correction_attempted"]) for row in rows),
        "successful_corrections": sum(
            int(row["successful_correction"]) for row in rows
        ),
        "final_failures": sum(not bool(row["completed"]) for row in rows),
        "agent_turns": sum(int(row["agent_turns"]) for row in rows),
        "memory_calls": sum(int(row["memory_calls"]) for row in rows),
        "input_tokens": sum(int(row["input_tokens"]) for row in rows),
        "output_tokens": sum(int(row["output_tokens"]) for row in rows),
        "total_tokens": sum(int(row["total_tokens"]) for row in rows),
        "latency_ms": {
            "mean": round(sum(latencies) / max(1, count), 6),
            "p50": round(_percentile(latencies, 0.50), 6),
            "p95": round(_percentile(latencies, 0.95), 6),
        },
        "context_modes": dict(sorted(modes.items())),
        "provider_usage": provider,
    }


def run(
    dataset: list[dict],
    *,
    k: int = DEFAULT_K,
    max_context_tokens: int = DEFAULT_MAX_CONTEXT_TOKENS,
    retrieval_token_budget: int = DEFAULT_RETRIEVAL_TOKENS,
    confidence_floor: float = 0.25,
    dim: int = 256,
    embedder: Optional[object] = None,
    agent: Optional[Callable[[str, str], Union[str, AgentTurn]]] = None,
    answer_evaluator: Optional[AnswerEvaluator] = None,
    clock: Callable[[], float] = time.perf_counter,
    strategy_order: tuple[str, ...] = STRATEGIES,
) -> dict:
    """Run full-history, retrieval-only, and adaptive agent task loops."""
    for value, name, positive in (
        (k, "k", True),
        (max_context_tokens, "max_context_tokens", False),
        (retrieval_token_budget, "retrieval_token_budget", False),
        (dim, "dim", True),
    ):
        if isinstance(value, bool):
            raise ValueError(f"{name} must be {'positive' if positive else 'non-negative'}")
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"{name} must be {'positive' if positive else 'non-negative'}"
            ) from exc
        if (positive and parsed <= 0) or (not positive and parsed < 0):
            raise ValueError(
                f"{name} must be {'positive' if positive else 'non-negative'}"
            )
    k = int(k)
    max_context_tokens = int(max_context_tokens)
    retrieval_token_budget = int(retrieval_token_budget)
    dim = int(dim)
    if retrieval_token_budget > max_context_tokens:
        raise ValueError("retrieval_token_budget cannot exceed max_context_tokens")
    try:
        confidence_floor = float(confidence_floor)
    except (TypeError, ValueError) as exc:
        raise ValueError("confidence_floor must be between 0 and 1") from exc
    if not math.isfinite(confidence_floor) or not 0 <= confidence_floor <= 1:
        raise ValueError("confidence_floor must be between 0 and 1")
    if (
        not isinstance(strategy_order, tuple)
        or len(strategy_order) != len(STRATEGIES)
        or set(strategy_order) != set(STRATEGIES)
    ):
        raise ValueError("strategy_order must contain each productivity strategy exactly once")

    counter = RegexTokenCounter()
    selected_embedder = embedder or DeterministicEmbedder(dim=dim)
    selected_agent = agent or DeterministicTaskAgent()
    selected_answer_evaluator = answer_evaluator or _completed
    rows = {name: [] for name in STRATEGIES}
    task_offset = 0

    for case in dataset:
        evidence_by_tag = {
            str(memory.get("tag")): str(memory.get("text", ""))
            for memory in case.get("memories", [])
        }
        # Each strategy gets an independently seeded engine. This keeps recall
        # caches, reinforcement bugs, or future mutable read state from making a
        # later strategy look artificially faster or more accurate.
        for method in strategy_order:
            store, engine, workspace_id, repo_id, history = _seed_case(
                case,
                embedder=selected_embedder,
                counter=counter,
            )
            try:
                for number, question_row in enumerate(case.get("questions", [])):
                    question = str(question_row.get("q", ""))
                    supporting_evidence = tuple(
                        evidence_by_tag[str(tag)]
                        for tag in question_row.get("supporting", [])
                        if str(tag) in evidence_by_tag
                    )
                    task_id = str(
                        question_row.get("id") or f"{case.get('id', 'case')}:{number}"
                    )
                    started = clock()
                    context, mode, memory_calls, reason = _attempt(
                        method=method,
                        question=question,
                        history=history,
                        engine=engine,
                        workspace_id=workspace_id,
                        repo_id=repo_id,
                        k=k,
                        max_context_tokens=max_context_tokens,
                        retrieval_token_budget=retrieval_token_budget,
                        confidence_floor=confidence_floor,
                        count_tokens=counter,
                    )
                    task_ordinal = task_offset + number
                    _prepare_agent_attempt(
                        selected_agent,
                        strategy=method,
                        task_ordinal=task_ordinal,
                        turn_ordinal=0,
                    )
                    first_turn = _turn(selected_agent(question, context))
                    first_response = first_turn.answer
                    first_completed = selected_answer_evaluator(
                        first_response, question_row, supporting_evidence
                    )
                    first_abstained = not first_response.strip()
                    agent_turns = 1
                    input_tokens = counter(question) + counter(context)
                    output_tokens = counter(first_response)
                    correction_attempted = not first_completed
                    successful_correction = False
                    final_response = first_response
                    if correction_attempted:
                        corrected_question = f"{_CORRECTION_PROMPT}\n{question}"
                        correction_history, _ = fit_recent_history(
                            history,
                            token_budget=max_context_tokens,
                            count_tokens=counter,
                        )
                        _prepare_agent_attempt(
                            selected_agent,
                            strategy=method,
                            task_ordinal=task_ordinal,
                            turn_ordinal=1,
                        )
                        corrected_turn = _turn(
                            selected_agent(corrected_question, correction_history)
                        )
                        corrected_response = corrected_turn.answer
                        successful_correction = selected_answer_evaluator(
                            corrected_response, question_row, supporting_evidence
                        )
                        final_response = corrected_response
                        agent_turns += 1
                        input_tokens += counter(corrected_question) + counter(correction_history)
                        output_tokens += counter(corrected_response)
                    completed = selected_answer_evaluator(
                        final_response, question_row, supporting_evidence
                    )
                    elapsed_ms = max(0.0, (clock() - started) * 1000.0)
                    provider_turns = [first_turn]
                    if correction_attempted:
                        provider_turns.append(corrected_turn)
                    provider = {}
                    for field in (
                        "input_tokens", "cached_input_tokens", "output_tokens",
                        "reasoning_output_tokens", "total_tokens", "latency_ms",
                    ):
                        values = [getattr(turn, field) for turn in provider_turns]
                        provider[field] = (
                            sum(values) if all(value is not None for value in values) else None
                        )
                    provider["models"] = sorted({
                        model
                        for turn in provider_turns
                        if (model := _public_model_identifier(turn.model)) is not None
                    })
                    rows[method].append({
                        "task_id": task_id,
                        "completed": completed,
                        "first_attempt_error": not first_completed,
                        "wrong_answer": not first_completed and not first_abstained,
                        "abstained": first_abstained,
                        "correction_attempted": correction_attempted,
                        "successful_correction": successful_correction,
                        "agent_turns": agent_turns,
                        "memory_calls": memory_calls,
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "total_tokens": input_tokens + output_tokens,
                        "latency_ms": round(elapsed_ms, 6),
                        "context_mode": mode,
                        "context_tokens": counter(context),
                        "routing_reason": reason,
                        "provider": provider,
                    })
            finally:
                store.close()
        task_offset += len(case.get("questions", []))

    reported_models = sorted({
        model
        for method_rows in rows.values()
        for row in method_rows
        for model in row["provider"].get("models", [])
    })
    return {
        "benchmark": {
            "name": "engraphis-agent-productivity/v1",
            "offline": isinstance(selected_embedder, DeterministicEmbedder)
            and bool(getattr(selected_agent, "deterministic", False)),
            "agent": {
                "implementation": type(selected_agent).__name__,
                "identity": getattr(selected_agent, "identity", None),
                "deterministic": bool(
                    getattr(selected_agent, "deterministic", False)
                ),
                "reported_models": reported_models,
            },
            "embedder": {
                "implementation": type(selected_embedder).__name__,
                "model_id": getattr(selected_embedder, "model_name", None),
                "revision": getattr(selected_embedder, "revision", None),
                "dimension": getattr(selected_embedder, "dim", None),
            },
            "token_counter": TOKEN_COUNTER_IDENTITY,
            "token_scope": (
                "Question, selected context, correction instruction, and agent output "
                "for every attempt; excludes system prompts and provider billing semantics."
            ),
            "latency_scope": (
                "Wall-clock context routing plus agent execution and correction attempts."
            ),
            "max_context_tokens": max_context_tokens,
            "retrieval_token_budget": retrieval_token_budget,
            "confidence_floor": confidence_floor,
            "k": k,
            "strategy_order": list(strategy_order),
        },
        "workload": {
            "cases": len(dataset),
            "tasks": sum(
                len(case.get("questions", []))
                for case in dataset
            ),
        },
        "methods": {
            method: _summary(method_rows)
            for method, method_rows in rows.items()
        },
        "detail": rows,
    }


def _public_report(report: dict) -> dict:
    return {
        "benchmark": report["benchmark"],
        "workload": report["workload"],
        "methods": report["methods"],
    }


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        description="Run the offline end-to-end Engraphis agent productivity benchmark."
    )
    parser.add_argument(
        "--dataset",
        default=str(Path(__file__).resolve().parent / "datasets" / "codemem.jsonl"),
    )
    parser.add_argument("--k", type=int, default=DEFAULT_K)
    parser.add_argument(
        "--max-context-tokens",
        type=int,
        default=DEFAULT_MAX_CONTEXT_TOKENS,
    )
    parser.add_argument(
        "--retrieval-token-budget",
        type=int,
        default=DEFAULT_RETRIEVAL_TOKENS,
    )
    parser.add_argument("--confidence-floor", type=float, default=0.25)
    parser.add_argument("--dim", type=int, default=256)
    args = parser.parse_args(argv)
    try:
        report = run(
            load_dataset(args.dataset),
            k=args.k,
            max_context_tokens=args.max_context_tokens,
            retrieval_token_budget=args.retrieval_token_budget,
            confidence_floor=args.confidence_floor,
            dim=args.dim,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True))
        raise SystemExit(2) from exc
    print(json.dumps(_public_report(report), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
