"""Offline adapters for agent-memory benchmark datasets.

This module intentionally does not vendor or import any benchmark repository.
It translates public JSON/JSONL exports into the ``eval.harness`` case schema
and runs the shipped Engraphis write/recall pipeline with the deterministic
embedder by default.

Supported formats:

* ``memoryagentbench`` — the public ``{"data": [...]}`` export or Hugging Face
  dataset-server ``{"rows": [{"row": ...}]}`` envelope containing a long
  ``context`` plus aligned ``questions``/``answers`` lists. Optional structured
  ``memory_events`` preserve incremental order and conflict keys.
* ``locomo_plus`` — the public unified-input records (``input_prompt``,
  ``trigger``, ``evidence``, ``category``).  It measures retrieval of the
  earlier cue, not the repository's LLM-as-judge answer score.
* ``mem2actbench`` — the paired public ``qa_dataset.jsonl`` and
  ``toolmem_conversation.jsonl`` exports.  It measures whether packed memory
  covers the expected tool-call arguments; Engraphis does not itself generate
  a tool call, so this is explicitly not action-success accuracy.

All loaders are strict: malformed or unmappable records raise ``ValueError``
instead of silently dropping benchmark rows.  The included fixtures are
deterministic plumbing/contract checks, not external leaderboard results.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
from typing import Any, Callable, Optional

from engraphis.backends import DeterministicEmbedder
from engraphis.backends.embedder_st import get_embedder
from eval.benchmark import report_envelope, write_canonical_artifact
from eval.harness import run


_PINNED_EMBED_REVISION = re.compile(r"[0-9a-f]{40}\Z")


def _read_records(path: str) -> list[dict[str, Any]]:
    """Read a JSON list/object or JSONL file, rejecting non-object rows."""
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"could not read {source}: {exc}") from exc
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        rows = []
        for number, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{source}:{number} is not valid JSON") from exc
            rows.append(row)
        parsed = rows
    if isinstance(parsed, dict) and isinstance(parsed.get("rows"), list):
        rows = parsed["rows"]
        if not all(
            isinstance(item, dict) and isinstance(item.get("row"), dict)
            for item in rows
        ):
            raise ValueError(f"{source} has a malformed Hugging Face rows envelope")
        return [item["row"] for item in rows]
    if isinstance(parsed, dict):
        return [parsed]
    if not isinstance(parsed, list) or not all(isinstance(row, dict) for row in parsed):
        raise ValueError(f"{source} must contain a JSON object, JSON list of objects, or JSONL objects")
    return parsed


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def _as_texts(value: Any, label: str) -> list[str]:
    if isinstance(value, str):
        return [_text(value, label)]
    if not isinstance(value, list) or not value:
        raise ValueError(f"{label} must be a non-empty string or list of strings")
    return [_text(item, label) for item in value]


def _answer_rows(value: Any, label: str) -> list[tuple[str, list[str]]]:
    """Normalize one answer or a list of accepted answer variants per question."""
    if not isinstance(value, list) or not value:
        raise ValueError(f"{label} must be a non-empty list")
    output = []
    for number, item in enumerate(value):
        variants = _as_texts(item, f"{label}[{number}]")
        output.append((variants[0], variants))
    return output


def _chunks(text: str, prefix: str, *, max_chars: int = 900) -> list[dict[str, str]]:
    """Make stable, dialogue-safe context records without external tokenizers."""
    paragraphs = [
        part.strip()
        for part in text.replace("\r\n", "\n").split("\n\n")
        if part.strip()
    ]
    if not paragraphs:
        raise ValueError("context has no non-empty paragraphs")
    output: list[dict[str, str]] = []

    def emit(value: str) -> None:
        output.append({"tag": f"{prefix}:{len(output)}", "text": value})

    for paragraph in paragraphs:
        pending = ""
        for unit in (line.strip() for line in paragraph.splitlines() if line.strip()):
            candidate = f"{pending}\n{unit}" if pending else unit
            if len(candidate) <= max_chars:
                pending = candidate
                continue
            if pending:
                emit(pending)
                pending = ""
            while len(unit) > max_chars:
                cut = unit.rfind(" ", 0, max_chars)
                cut = cut if cut > max_chars // 2 else max_chars
                emit(unit[:cut].strip())
                unit = unit[cut:].strip()
            pending = unit
        if pending:
            emit(pending)
    return output


def _case_id(row: dict[str, Any], prefix: str, number: int) -> str:
    for key in ("id", "case_id", "sample_id", "session_id", "question_id"):
        if row.get(key) is not None and str(row[key]).strip():
            return str(row[key]).strip()
    return f"{prefix}-{number}"


def _limited_rows(rows: list[dict[str, Any]], limit: Optional[int]) -> list[dict[str, Any]]:
    """Apply an explicit positive limit without Python's surprising negative slices."""
    if limit is None:
        return rows
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("limit must be a positive integer when supplied")
    return rows[:limit]


def load_memoryagentbench(path: str, *, limit: Optional[int] = None) -> list[dict]:
    """Load MemoryAgentBench's public context/question export.

    Its upstream conversation creator accepts a top-level ``data`` array, then
    reads ``context`` and aligned ``questions``/``answers`` fields.  Some
    downstream exports preserve individual memory events; when present we use
    them directly so ``subject_key``/``claim_kind`` can exercise the actual
    conflict-resolution write path.
    """
    roots = _read_records(path)
    if len(roots) == 1 and isinstance(roots[0].get("data"), list):
        rows = roots[0]["data"]
    else:
        rows = roots
    if not all(isinstance(row, dict) for row in rows):
        raise ValueError("MemoryAgentBench data must be objects")
    cases = []
    for number, row in enumerate(_limited_rows(rows, limit)):
        case_id = _case_id(row, "mab", number)
        events = row.get("memory_events")
        if events is not None:
            if not isinstance(events, list) or not events:
                raise ValueError(f"MemoryAgentBench {case_id}: memory_events must be a non-empty list")
            memories = []
            for event_number, event in enumerate(events):
                if not isinstance(event, dict):
                    raise ValueError(f"MemoryAgentBench {case_id}: memory_events[{event_number}] must be an object")
                memories.append({
                    "tag": str(event.get("id") or f"{case_id}:event:{event_number}"),
                    "text": _text(event.get("text") or event.get("content"), "memory event text"),
                    "valid_from": float(event_number),
                    "subject_key": str(event.get("subject_key") or ""),
                    "claim_kind": str(event.get("claim_kind") or ""),
                })
        else:
            memories = _chunks(_text(row.get("context"), f"MemoryAgentBench {case_id}.context"), case_id)

        questions = _as_texts(row.get("questions"), f"MemoryAgentBench {case_id}.questions")
        answers = _answer_rows(row.get("answers"), f"MemoryAgentBench {case_id}.answers")
        if len(questions) != len(answers):
            raise ValueError(f"MemoryAgentBench {case_id}: questions and answers must have equal length")
        metadata = row.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        ids = (
            row.get("qa_pair_ids")
            or row.get("question_ids")
            or metadata.get("qa_pair_ids")
            or metadata.get("question_ids")
            or []
        )
        if ids and (not isinstance(ids, list) or len(ids) != len(questions)):
            raise ValueError(f"MemoryAgentBench {case_id}: qa_pair_ids must align with questions")
        supporting_rows = row.get("supporting_ids") or row.get("evidence_ids") or []
        if supporting_rows and (not isinstance(supporting_rows, list) or len(supporting_rows) != len(questions)):
            raise ValueError(f"MemoryAgentBench {case_id}: supporting_ids must align with questions")
        normalized_questions = []
        for q_number, (question, answer_row) in enumerate(zip(questions, answers)):
            answer, answer_variants = answer_row
            if supporting_rows:
                source_ids = supporting_rows[q_number]
                if not isinstance(source_ids, list) or not all(str(item).strip() for item in source_ids):
                    raise ValueError(
                        f"MemoryAgentBench {case_id}: supporting_ids[{q_number}] must be a list of IDs"
                    )
                supporting = [str(item) for item in source_ids]
            else:
                supporting = [
                    memory["tag"]
                    for memory in memories
                    if any(
                        variant.casefold() in memory["text"].casefold()
                        for variant in answer_variants
                    )
                ]
            normalized_questions.append({
                "id": str(ids[q_number]) if ids else f"{case_id}:q:{q_number}",
                "q": question,
                "answer": answer,
                "answer_variants": answer_variants,
                "supporting": supporting,
                "category": str(
                    row.get("sub_dataset")
                    or row.get("dataset")
                    or metadata.get("source")
                    or "memoryagentbench"
                ),
                # Upstream exports do not always expose evidence IDs.  Keep
                # answer-token coverage scored while publishing that caveat.
                "gold_evidence_available": bool(supporting),
            })
        cases.append({"id": case_id, "memories": memories, "questions": normalized_questions})
    if not cases:
        raise ValueError("MemoryAgentBench source contained no cases")
    return cases


def _evidence_matches(memories: list[dict[str, str]], evidence: list[str], label: str) -> list[str]:
    tags = []
    for needle in evidence:
        lines = [line.strip() for line in needle.splitlines() if line.strip()]
        fragments = []
        for line in lines or [needle]:
            # Official unified LoCoMo-Plus evidence is rendered as
            # ``Speaker：utterance`` while input_prompt renders
            # ``Speaker said, "utterance"``. Match the evidence-bearing
            # utterance rather than requiring the formatting wrapper.
            parts = re.split(r"[:：]", line, maxsplit=1)
            fragment = (parts[1] if len(parts) == 2 else parts[0]).strip(" \t\"'")
            if fragment:
                fragments.append(fragment)
        for fragment in fragments:
            folded = fragment.casefold()
            matched = [
                memory["tag"]
                for memory in memories
                if folded in memory["text"].casefold()
            ]
            if not matched:
                raise ValueError(f"{label}: evidence text did not occur in input_prompt")
            tags.extend(matched)
    return list(dict.fromkeys(tags))


def load_locomo_plus(
    path: str,
    *,
    limit: Optional[int] = None,
    include_original_locomo: bool = False,
) -> list[dict]:
    """Load Locomo-Plus unified input and score cue retrieval deterministically.

    The official unified file also contains the five original LoCoMo categories.
    The default selects only the new Cognitive category so a run measures implicit
    cue-to-trigger memory instead of quietly becoming another factual LoCoMo run.
    """
    rows = _read_records(path)
    if len(rows) == 1 and isinstance(rows[0].get("data"), list):
        rows = rows[0]["data"]
    if not include_original_locomo:
        rows = [
            row
            for row in rows
            if str(row.get("category") or "").strip().casefold() == "cognitive"
        ]
    cases = []
    for number, row in enumerate(_limited_rows(rows, limit)):
        case_id = _case_id(row, "locomo-plus", number)
        prompt = _text(row.get("input_prompt"), f"Locomo-Plus {case_id}.input_prompt")
        trigger = _text(row.get("trigger"), f"Locomo-Plus {case_id}.trigger")
        evidence = _as_texts(row.get("evidence"), f"Locomo-Plus {case_id}.evidence")
        memories = _chunks(prompt, case_id)
        supporting = _evidence_matches(memories, evidence, f"Locomo-Plus {case_id}")
        cases.append({
            "id": case_id,
            "memories": memories,
            "questions": [{
                "id": str(row.get("question_id") or f"{case_id}:trigger"),
                "q": trigger,
                # Cognitive examples may intentionally omit a reference answer.
                # Evidence-token coverage remains a reproducible retrieval measure.
                "answer": _text(row.get("answer"), f"Locomo-Plus {case_id}.answer")
                if row.get("answer") else " ".join(evidence),
                "supporting": supporting,
                "category": str(row.get("category") or "Cognitive"),
            }],
        })
    if not cases:
        raise ValueError("Locomo-Plus source contained no cases")
    return cases


def _tool_call_text(call: dict[str, Any], label: str) -> str:
    if not isinstance(call, dict):
        raise ValueError(f"{label}.tool_call must be an object")
    name = _text(call.get("name"), f"{label}.tool_call.name")
    arguments = call.get("arguments")
    if not isinstance(arguments, dict):
        raise ValueError(f"{label}.tool_call.arguments must be an object")
    return json.dumps({"name": name, "arguments": arguments}, ensure_ascii=False, sort_keys=True)


def load_mem2actbench(qa_path: str, conversation_path: str, *, limit: Optional[int] = None) -> list[dict]:
    """Load Mem2ActBench's paired QA/session JSONL exports."""
    sessions = _read_records(conversation_path)
    by_source: dict[str, list[dict[str, str]]] = {}
    for number, session in enumerate(sessions):
        session_id = _case_id(session, "mem2act-session", number)
        source_ids = session.get("original_conversation_ids")
        turns = session.get("turns")
        if not isinstance(source_ids, list) or not source_ids or not isinstance(turns, list) or not turns:
            raise ValueError(f"Mem2Act session {session_id} requires original_conversation_ids and turns")
        grouped: dict[str, list[str]] = {str(source): [] for source in source_ids}
        for turn_number, turn in enumerate(turns):
            if not isinstance(turn, dict):
                raise ValueError(f"Mem2Act session {session_id}: turns[{turn_number}] must be an object")
            content = turn.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            source = str(turn.get("source_id") or "")
            if source in grouped:
                grouped[source].append(f"{turn.get('role', 'unknown')}: {content.strip()}")
        for source, lines in grouped.items():
            if lines:
                by_source.setdefault(source, []).append({"tag": source, "text": "\n".join(lines)})

    cases = []
    for number, qa in enumerate(_limited_rows(_read_records(qa_path), limit)):
        qa_id = _case_id(qa, "mem2act", number)
        source_ids = qa.get("source_conversation_ids")
        if not isinstance(source_ids, list) or not source_ids:
            raise ValueError(f"Mem2Act QA {qa_id}: source_conversation_ids must be a non-empty list")
        memories = [memory for source in source_ids for memory in by_source.get(str(source), [])]
        if not memories:
            raise ValueError(f"Mem2Act QA {qa_id}: no session turns matched source_conversation_ids")
        call = qa.get("tool_call")
        expected = _tool_call_text(call, f"Mem2Act QA {qa_id}")
        complexity = qa.get("complexity_metadata") or {}
        cases.append({
            "id": qa_id,
            "memories": memories,
            "questions": [{
                "id": qa_id,
                "q": _text(qa.get("query"), f"Mem2Act QA {qa_id}.query"),
                "answer": expected,
                "supporting": [str(source) for source in source_ids],
                "category": str(complexity.get("level") or "tool_argument_grounding"),
            }],
        })
    if not cases:
        raise ValueError("Mem2Act source contained no QA rows")
    return cases


LOADERS: dict[str, Callable[..., list[dict]]] = {
    "memoryagentbench": load_memoryagentbench,
    "locomo_plus": load_locomo_plus,
    "mem2actbench": load_mem2actbench,
}


def _claim_boundary(fmt: str) -> str:
    if fmt == "mem2actbench":
        return ("Retrieval/context coverage of expected tool-call JSON only; Engraphis is not a "
                "tool-calling agent, so this is not end-to-end action success.")
    if fmt == "locomo_plus":
        return ("Cue-evidence retrieval only; this is not Locomo-Plus LLM-as-judge answer scoring.")
    return ("Retrieval and answer-token context coverage only; upstream answer/Judge metrics are "
            "not reproduced by this offline adapter.")


_RETRIEVAL_METRICS = (
    "recall_at_k",
    "hit_at_k",
    "mrr_at_k",
    "ndcg_at_k",
    "recall_at_1",
    "recall_at_5",
    "recall_at_10",
    "hit_at_1",
    "hit_at_5",
    "hit_at_10",
    "mrr_at_1",
    "mrr_at_5",
    "mrr_at_10",
    "ndcg_at_1",
    "ndcg_at_5",
    "ndcg_at_10",
)


def _separate_unlabeled_retrieval(report: dict) -> None:
    """Do not award perfect retrieval to questions with no gold evidence IDs."""
    detail = list(report.get("detail") or [])
    retrieval_rows = []
    for row in detail:
        if row.get("supporting_ids"):
            retrieval_rows.append(row)
        else:
            row["retrieval_excluded"] = "no_gold_evidence"
            for field in _RETRIEVAL_METRICS:
                row.pop(field, None)
    report["retrieval_scored_questions"] = len(retrieval_rows)
    for field in ("recall_at_k", "hit_at_k", "mrr_at_k", "ndcg_at_k"):
        report[field] = (
            round(
                sum(float(row[field]) for row in retrieval_rows)
                / len(retrieval_rows),
                4,
            )
            if retrieval_rows
            else None
        )


def public_artifact(
    report: dict,
    *,
    fmt: str,
    dataset: str,
    conversations: Optional[str],
    k: int,
    limit: Optional[int],
    embed_model: Optional[str],
    embed_revision: Optional[str],
    include_original_locomo: bool,
    embedder: Optional[object],
    resolve_conflicts: bool,
) -> dict:
    """Build a redacted immutable envelope from a private adapter report."""
    if bool(embed_model) != bool(embed_revision):
        raise ValueError("embed_model and embed_revision must be used together")
    if embed_revision and _PINNED_EMBED_REVISION.fullmatch(embed_revision) is None:
        raise ValueError("embed_revision must be an immutable lowercase 40-character commit")
    detail = list(report.get("detail") or [])
    first_usage = detail[0].get("usage") if detail else {}
    first_usage = first_usage if isinstance(first_usage, dict) else {}
    token_identity = str(first_usage.get("token_counter") or "unspecified")
    metric_names = (
        "questions",
        "scored_questions",
        "retrieval_scored_questions",
        "recall_at_k",
        "hit_at_k",
        "mrr_at_k",
        "ndcg_at_k",
        "answer_token_recall",
    )
    metrics = {name: report[name] for name in metric_names if name in report}
    metrics["claim_boundary"] = _claim_boundary(fmt)
    source_paths = [dataset, *([conversations] if conversations else [])]
    command = [
        "python", "-m", "eval.agent_benchmarks",
        "--dataset", "<dataset>",
        "--format", fmt,
        "--k", str(k),
    ]
    if limit is not None:
        command.extend(["--limit", str(limit)])
    if embed_model:
        command.extend(["--embed-model", embed_model])
        command.extend(["--embed-revision", str(embed_revision)])
    if not resolve_conflicts:
        command.append("--no-resolve")
    if include_original_locomo:
        command.append("--include-original-locomo")
    if conversations:
        command.extend(["--conversations", "<conversations>"])
    selected_embedder = embedder or DeterministicEmbedder()
    model_id = getattr(selected_embedder, "model_name", type(selected_embedder).__name__)
    revision = getattr(selected_embedder, "revision", None)
    return report_envelope(
        suite=f"Engraphis {fmt}",
        dataset_path=dataset,
        source_paths=source_paths,
        config={
            "format": fmt,
            "k": k,
            "limit": limit,
            "embed_model": model_id,
            "embedder_revision": revision,
            "resolve_conflicts": resolve_conflicts,
            "include_original_locomo": bool(
                report.get("include_original_locomo")
            ),
        },
        command=command,
        token_accounting={
            "identity": token_identity,
            "revision": None,
            "scope": "packed_retrieved_memory_context",
            "method": str(
                detail[0].get("context_token_method")
                if detail else "unspecified"
            ),
        },
        models={
            "embedder": {
                "model_id": model_id,
                "revision": revision,
            },
        },
        records=detail,
        metrics=metrics,
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run offline agent-memory benchmark adapters.")
    parser.add_argument("--dataset", required=True, help="Benchmark JSON or JSONL export.")
    parser.add_argument("--format", required=True, choices=sorted(LOADERS))
    parser.add_argument("--conversations", help="Mem2ActBench toolmem_conversation.jsonl (required there).")
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--embed-model", default=None, help="Optional sentence-transformers model.")
    parser.add_argument(
        "--embed-revision",
        default=None,
        help="Required immutable 40-character commit when --embed-model is selected.",
    )
    parser.add_argument("--no-resolve", action="store_true", help="Disable write-path resolution.")
    parser.add_argument(
        "--include-original-locomo",
        action="store_true",
        help="For locomo_plus, include the five original LoCoMo categories too.",
    )
    parser.add_argument("--json", dest="json_out", default=None, help="Write JSON report to this path.")
    parser.add_argument(
        "--artifact",
        default=None,
        help="Write a redacted immutable evidence envelope and adjacent SHA256 file.",
    )
    args = parser.parse_args(argv)
    try:
        if args.k <= 0:
            raise ValueError("k must be a positive integer")
        if bool(args.embed_model) != bool(args.embed_revision):
            raise ValueError("--embed-model and --embed-revision must be used together")
        if args.embed_revision and _PINNED_EMBED_REVISION.fullmatch(args.embed_revision) is None:
            raise ValueError("--embed-revision must be an immutable lowercase 40-character commit")
        if args.format == "mem2actbench":
            if not args.conversations:
                raise ValueError("--conversations is required for mem2actbench")
            cases = load_mem2actbench(args.dataset, args.conversations, limit=args.limit)
        elif args.format == "locomo_plus":
            cases = load_locomo_plus(
                args.dataset,
                limit=args.limit,
                include_original_locomo=args.include_original_locomo,
            )
        else:
            cases = LOADERS[args.format](args.dataset, limit=args.limit)
        embedder = (
            get_embedder(args.embed_model, revision=args.embed_revision)
            if args.embed_model else None
        )
        report = run(cases, k=args.k, embedder=embedder, resolve_conflicts=not args.no_resolve)
    except ValueError as exc:
        parser.error(str(exc))
    report.update({
        "format": args.format,
        "dataset": args.dataset,
        "offline": embedder is None or isinstance(embedder, DeterministicEmbedder),
        "embedder": {
            "model_id": getattr(embedder, "model_name", None)
            if embedder is not None else "DeterministicEmbedder",
            "revision": getattr(embedder, "revision", None),
            "implementation": type(embedder).__name__ if embedder is not None else "DeterministicEmbedder",
        },
        "include_original_locomo": bool(args.include_original_locomo),
        "limit": args.limit,
        "measures": _claim_boundary(args.format),
    })
    _separate_unlabeled_retrieval(report)
    output = json.dumps(report, indent=2, sort_keys=True)
    print(output)
    if args.json_out:
        Path(args.json_out).write_text(output + "\n", encoding="utf-8")
    if args.artifact:
        artifact = public_artifact(
            report,
            fmt=args.format,
            dataset=args.dataset,
            conversations=args.conversations,
            k=args.k,
            limit=args.limit,
            embed_model=args.embed_model,
            embed_revision=args.embed_revision,
            include_original_locomo=bool(args.include_original_locomo),
            embedder=embedder,
            resolve_conflicts=not args.no_resolve,
        )
        write_canonical_artifact(artifact, args.artifact)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
