"""Deterministic workload-level context economy benchmark.

This benchmark measures *reader context* under three executable strategies over
the ordinary ``eval.harness`` JSONL schema:

* ``full_history`` replays the complete case corpus for every question;
* ``recency_window`` admits the newest source memories that fit the same token
  budget given to Engraphis; and
* ``engraphis`` uses the shipped hybrid recall pipeline and context packer.

By default it is an offline, token-accounting benchmark, not a provider
billing model; callers may inject a real embedder for a retrieval comparison.
Counts use the named ``engraphis.regex.v1`` counter exactly;
they exclude system prompts, question text, output/completion tokens, provider
tokenizer differences, cached-input pricing, and any compute or storage costs.
The indexing-inclusive total conservatively adds one complete source-corpus
token pass once to Engraphis query context.  That makes the break-even point
explicit without pretending it is a dollar or invoice estimate.

The input format is the existing harness schema::

    {"id": "case", "memories": [{"tag": "f1", "text": "..."}],
     "questions": [{"q": "...", "answer": "...", "supporting": ["f1"]}]}

Run with ``python -m eval.context_economy --dataset ...``.  stdout is always a
single JSON document so the command is safe to consume from automation.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Callable, Optional

from engraphis.backends import DeterministicEmbedder, NumpyVectorIndex
from engraphis.backends.embedder_st import get_embedder
from engraphis.backends.reranker import IdentityReranker
from engraphis.core.context import DeterministicContextPacker, RegexTokenCounter
from engraphis.core.engine import MemoryEngine
from engraphis.core.interfaces import MemoryType, Scope
from engraphis.core.store import Store
from eval import metrics
from eval.external import LOADERS
from eval.harness import _seed_case_graph, load_dataset


DEFAULT_TOKEN_BUDGET = 512
DEFAULT_K = 5
TOKEN_COUNTER_IDENTITY = RegexTokenCounter.identity
NON_BILLING_SCOPE = (
    "This is deterministic reader-context token accounting, not provider billing. "
    "It excludes system prompts, question text, completion tokens, provider tokenizer "
    "differences, cached-input pricing, and compute or storage costs."
)


def _mean(rows: list[dict], key: str) -> float:
    return round(sum(float(row.get(key, 0.0)) for row in rows) / max(len(rows), 1), 6)


def _scored(question: dict) -> bool:
    """Match the harness convention: unanswerable rows consume context but not quality."""
    return question.get("answerable") is not False


def _truncate_to_budget(text: str, budget: int, counter: Callable[[str], int]) -> str:
    """Return the longest regex-token prefix that fits ``budget`` exactly.

    The benchmark pins the built-in regex counter, so preserving the whitespace
    between source tokens is sufficient and deterministic.  A whole source that
    fits is returned byte-for-byte to avoid inventing a recency summarizer.
    """
    source = str(text or "")
    if budget <= 0:
        return ""
    if counter(source) <= budget:
        return source
    import re

    matches = list(re.finditer(r"\w+|[^\w\s]", source, re.UNICODE))
    if not matches:
        return ""
    limit = min(len(matches), budget)
    return source[:matches[limit - 1].end()].rstrip()


def _recency_context(
    memories: list[dict], *, budget: int, counter: Callable[[str], int],
) -> tuple[str, list[str]]:
    """Select newest raw source records under the common reader-context budget."""
    remaining = max(0, int(budget))
    selected: list[tuple[str, str]] = []
    for memory in reversed(memories):
        text = str(memory.get("text", ""))
        tokens = counter(text)
        if tokens <= remaining:
            selected.append((str(memory.get("tag", "")), text))
            remaining -= tokens
        elif not selected and remaining:
            excerpt = _truncate_to_budget(text, remaining, counter)
            if excerpt:
                selected.append((str(memory.get("tag", "")), excerpt))
            remaining = 0
        else:
            # A recency *window* is contiguous: once the next older source
            # cannot fit, it must not skip backward into an older record.
            break
        if remaining <= 0:
            break
    # A conversation window is presented chronologically even though it is
    # selected from the newest end of the source sequence.
    selected.reverse()
    return "\n\n".join(text for _, text in selected), [tag for tag, _ in selected if tag]


def _quality(
    *, retrieved_tags: list[str], retrieved_texts: list[str], question: dict,
) -> dict:
    """Quality of evidence actually placed into the reader's context."""
    supporting = [str(tag) for tag in question.get("supporting", [])]
    answer = str(question.get("answer", question.get("evidence", "")))
    return {
        "retrieval_recall": metrics.recall_at_k(retrieved_tags, supporting),
        "retrieval_hit": metrics.hit_at_k(retrieved_tags, supporting),
        "answer_token_recall": metrics.answer_token_recall(retrieved_texts, answer),
    }


def _method_summary(rows: list[dict]) -> dict:
    scored = [row for row in rows if row["scored"]]
    context_tokens = sum(int(row["context_tokens"]) for row in rows)
    return {
        "queries": len(rows),
        "scored_queries": len(scored),
        "cumulative_query_context_tokens": context_tokens,
        "mean_query_context_tokens": round(context_tokens / max(len(rows), 1), 6),
        "quality": {
            "retrieval_recall": _mean(scored, "retrieval_recall"),
            "retrieval_hit_rate": _mean(scored, "retrieval_hit"),
            "answer_token_recall": _mean(scored, "answer_token_recall"),
        },
    }


def _engraphis_rows(
    case: dict,
    *,
    k: int,
    token_budget: int,
    embedder: object,
    counter: Callable[[str], int],
    resolve_conflicts: bool,
) -> list[dict]:
    """Run production ingestion, hybrid recall, and packing for one case."""
    store = Store(":memory:")
    try:
        workspace_id = store.get_or_create_workspace("context-economy")
        repo_id = store.get_or_create_repo(workspace_id, str(case.get("id", "case")))
        engine = MemoryEngine(
            store,
            embedder,
            NumpyVectorIndex(store),
            IdentityReranker(),
        )
        # Pin both strategy accounting and shipped packing to the same named,
        # offline counter.  This makes budgets directly comparable.
        engine.recall_engine.context_packer = DeterministicContextPacker(
            token_counter=counter, token_counter_identity=TOKEN_COUNTER_IDENTITY,
        )
        _seed_case_graph(store, workspace_id=workspace_id, repo_id=repo_id, case=case)

        id_to_tags: dict[str, list[str]] = {}
        for memory in case.get("memories", []):
            content = str(memory.get("text", ""))
            memory_id = engine.remember(
                content,
                workspace_id=workspace_id,
                repo_id=repo_id,
                mtype=MemoryType.EPISODIC,
                scope=Scope.REPO,
                title=str(memory.get("title", "")),
                valid_from=memory.get("valid_from"),
                subject_key=str(memory.get("subject_key", "")),
                claim_kind=str(memory.get("claim_kind", "")),
                resolve_conflicts=resolve_conflicts,
            )
            tag = memory.get("tag")
            if tag is not None:
                id_to_tags.setdefault(memory_id, []).append(str(tag))

        rows = []
        for number, question in enumerate(case.get("questions", [])):
            result = engine.recall(
                str(question.get("q", "")), workspace_id=workspace_id,
                repo_id=repo_id, k=k, token_budget=token_budget,
            )
            # ``chunks`` can contain candidates omitted by packing.  Reader
            # quality must only receive the chunks actually admitted to context.
            packed_ids = [chunk.id for chunk in result.packed_chunks]
            tags = [tag for memory_id in packed_ids for tag in id_to_tags.get(memory_id, [])]
            texts = [chunk.excerpt for chunk in result.packed_chunks]
            quality = _quality(retrieved_tags=tags, retrieved_texts=texts, question=question)
            rows.append({
                "question_id": str(question.get("id") or f"{case.get('id')}:{number}"),
                "case_id": str(case.get("id", "case")),
                "scored": _scored(question),
                "context_tokens": counter(result.context),
                "retrieved_tags": tags,
                **quality,
            })
        return rows
    finally:
        store.close()


def run(
    dataset: list[dict], *, k: int = DEFAULT_K, token_budget: int = DEFAULT_TOKEN_BUDGET,
    dim: int = 256, embedder: Optional[object] = None, resolve_conflicts: bool = True,
) -> dict:
    """Benchmark aggregate workload context and evidence quality.

    ``token_budget`` applies identically to the recency and Engraphis methods.
    Full history is intentionally uncapped: it is the complete-corpus replay
    comparator whose query-context cost the other two methods seek to avoid.
    The default embedder is deterministic/offline; callers may inject a real
    implementation without changing the named reader-token accounting.
    """
    if isinstance(token_budget, bool) or int(token_budget) < 0:
        raise ValueError("token_budget must be a non-negative integer")
    if isinstance(k, bool) or int(k) <= 0:
        raise ValueError("k must be a positive integer")
    if isinstance(dim, bool) or int(dim) <= 0:
        raise ValueError("dim must be a positive integer")

    token_budget, k, dim = int(token_budget), int(k), int(dim)
    counter = RegexTokenCounter()
    selected_embedder = embedder if embedder is not None else DeterministicEmbedder(dim=dim)
    is_offline = isinstance(selected_embedder, DeterministicEmbedder)
    full_history_rows: list[dict] = []
    recency_rows: list[dict] = []
    engraphis_rows: list[dict] = []
    indexing_tokens = 0

    for case in dataset:
        memories = list(case.get("memories", []))
        source_texts = [str(memory.get("text", "")) for memory in memories]
        source_tags = [str(memory.get("tag", "")) for memory in memories if memory.get("tag") is not None]
        full_context = "\n\n".join(source_texts)
        full_tokens = counter(full_context)
        indexing_tokens += sum(counter(text) for text in source_texts)
        per_case_engraphis = _engraphis_rows(
            case,
            k=k,
            token_budget=token_budget,
            embedder=selected_embedder,
            counter=counter,
            resolve_conflicts=bool(resolve_conflicts),
        )
        engraphis_rows.extend(per_case_engraphis)

        for number, question in enumerate(case.get("questions", [])):
            question_id = str(question.get("id") or f"{case.get('id')}:{number}")
            common = {
                "question_id": question_id,
                "case_id": str(case.get("id", "case")),
                "scored": _scored(question),
            }
            full_quality = _quality(
                retrieved_tags=source_tags, retrieved_texts=source_texts, question=question,
            )
            full_history_rows.append({
                **common, "context_tokens": full_tokens, "retrieved_tags": source_tags, **full_quality,
            })
            recency_context, recency_tags = _recency_context(
                memories, budget=token_budget, counter=counter,
            )
            recency_quality = _quality(
                retrieved_tags=recency_tags, retrieved_texts=[recency_context], question=question,
            )
            recency_rows.append({
                **common,
                "context_tokens": counter(recency_context),
                "retrieved_tags": recency_tags,
                **recency_quality,
            })

    methods = {
        "full_history": _method_summary(full_history_rows),
        "recency_window": _method_summary(recency_rows),
        "engraphis": _method_summary(engraphis_rows),
    }
    full_tokens = methods["full_history"]["cumulative_query_context_tokens"]
    engraphis_tokens = methods["engraphis"]["cumulative_query_context_tokens"]
    saved_tokens = full_tokens - engraphis_tokens
    savings_ratio = (saved_tokens / full_tokens) if full_tokens else 0.0
    per_query_savings = (
        methods["full_history"]["mean_query_context_tokens"]
        - methods["engraphis"]["mean_query_context_tokens"]
    )
    if per_query_savings > 0:
        break_even: Optional[int] = max(1, int(-(-indexing_tokens // per_query_savings)))
    else:
        break_even = None
    indexing_inclusive_total = indexing_tokens + engraphis_tokens

    return {
        "benchmark": {
            "name": "engraphis-context-economy/v1",
            "offline": is_offline,
            "embedder": {
                "name": type(selected_embedder).__name__,
                "model_id": getattr(selected_embedder, "model_name", None),
                "revision": getattr(selected_embedder, "revision", None),
                "dimension": getattr(selected_embedder, "dim", None),
            },
            "token_counter": TOKEN_COUNTER_IDENTITY,
            "token_budget": token_budget,
            "k": k,
            "resolve_conflicts": bool(resolve_conflicts),
            "non_billing_scope": NON_BILLING_SCOPE,
            "indexing_assumption": (
                "One complete source-memory token pass is charged once to Engraphis; "
                "this is an intentionally conservative accounting proxy, not a provider price."
            ),
        },
        "workload": {
            "cases": len(dataset),
            "queries": len(full_history_rows),
            "scored_queries": sum(1 for row in full_history_rows if row["scored"]),
            "one_time_indexing_tokens": indexing_tokens,
        },
        "methods": methods,
        "engraphis_vs_full_history": {
            "cumulative_query_context_tokens": engraphis_tokens,
            "query_context_tokens_saved": saved_tokens,
            "query_context_savings_ratio": round(savings_ratio, 6),
            "one_time_indexing_inclusive_total_tokens": indexing_inclusive_total,
            "indexing_inclusive_tokens_saved": full_tokens - indexing_inclusive_total,
            "break_even_query_count": break_even,
            "break_even_definition": (
                "Smallest whole query count where one source-corpus indexing pass plus "
                "Engraphis mean reader context is no greater than full-history mean reader context; "
                "null means Engraphis does not save reader-context tokens per query."
            ),
        },
        "detail": {
            "full_history": full_history_rows,
            "recency_window": recency_rows,
            "engraphis": engraphis_rows,
        },
    }


def _console_report(evaluation: dict) -> dict:
    """Return the aggregate-only report that the command-line interface may print.

    ``run`` intentionally retains per-question source tags for in-process evaluation.  Those
    identifiers can be private dataset content, so command-line output is restricted to the
    reproducible aggregate evidence rather than logging the detailed rows.
    """
    benchmark = evaluation["benchmark"]
    return {
        "benchmark": {
            "name": benchmark["name"],
            "offline": benchmark["offline"],
            "embedder": benchmark["embedder"],
            "token_counter": benchmark["token_counter"],
            "token_budget": benchmark["token_budget"],
            "k": benchmark["k"],
            "resolve_conflicts": benchmark["resolve_conflicts"],
            "indexing_assumption": benchmark["indexing_assumption"],
            "dataset_format": benchmark["dataset_format"],
        },
        "workload": {
            "cases": evaluation["workload"]["cases"],
            "queries": evaluation["workload"]["queries"],
            "scored_queries": evaluation["workload"]["scored_queries"],
            "one_time_indexing_tokens": evaluation["workload"]["one_time_indexing_tokens"],
        },
        "methods": {
            name: {
                "queries": result["queries"],
                "scored_queries": result["scored_queries"],
                "cumulative_query_context_tokens": result["cumulative_query_context_tokens"],
                "mean_query_context_tokens": result["mean_query_context_tokens"],
                "quality": result["quality"],
            }
            for name, result in evaluation["methods"].items()
        },
        "engraphis_vs_full_history": evaluation["engraphis_vs_full_history"],
    }


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Run the offline Engraphis context economy benchmark.")
    parser.add_argument(
        "--dataset",
        default=str(Path(__file__).resolve().parent / "datasets" / "sample.jsonl"),
    )
    parser.add_argument(
        "--format", choices=("harness",) + tuple(sorted(LOADERS)), default="harness",
        help="dataset format: harness JSONL (default), locomo, or longmemeval",
    )
    parser.add_argument("--token-budget", type=int, default=DEFAULT_TOKEN_BUDGET)
    parser.add_argument("--k", type=int, default=DEFAULT_K)
    parser.add_argument("--dim", type=int, default=256)
    parser.add_argument(
        "--embed-model", default=None,
        help="optional sentence-transformers model; defaults to the deterministic offline embedder",
    )
    parser.add_argument(
        "--no-resolve",
        action="store_true",
        help="keep repeated turn-level memories separate instead of running write resolution",
    )
    args = parser.parse_args(argv)
    try:
        embedder = get_embedder(args.embed_model, args.dim) if args.embed_model else None
        dataset = (
            load_dataset(args.dataset)
            if args.format == "harness"
            else LOADERS[args.format](args.dataset)
        )
        evaluation = run(
            dataset, k=args.k, token_budget=args.token_budget, dim=args.dim,
            embedder=embedder, resolve_conflicts=not args.no_resolve,
        )
        evaluation["benchmark"]["dataset_format"] = args.format
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        # Keep stdout machine-readable even for automation failures.  argparse
        # still owns malformed flag syntax, which is its conventional contract.
        print(json.dumps({"error": str(exc)}, sort_keys=True))
        raise SystemExit(2)
    print(json.dumps(_console_report(evaluation), sort_keys=True))


if __name__ == "__main__":
    main()
