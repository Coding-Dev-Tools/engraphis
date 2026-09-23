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
from collections import Counter
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PureWindowsPath
import re
from typing import Any, Callable, Optional, Union

from engraphis.backends import DeterministicEmbedder
from engraphis.backends.embedder_st import get_embedder
from eval.benchmark import report_envelope, sha256_file, verify_report_snapshot, write_canonical_artifact
from eval.external_checkpoints import producer_snapshot, run_resumable
from eval.harness import run


_PINNED_EMBED_REVISION = re.compile(r"[0-9a-f]{40}\Z")


@dataclass(frozen=True)
class _RecordsSnapshot:
    """Normalized records and their digest from one immutable byte read."""

    path: Path
    records: list[dict[str, Any]]
    sha256: str


def _read_records_snapshot(path: Union[str, Path]) -> _RecordsSnapshot:
    """Read a JSON list/object or JSONL file, rejecting non-object rows."""
    source = Path(path)
    try:
        payload = source.read_bytes()
    except OSError as exc:
        raise ValueError(f"could not read {source}: {exc}") from exc
    text = payload.decode("utf-8")
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
        records = [item["row"] for item in rows]
    elif isinstance(parsed, dict):
        records = [parsed]
    elif isinstance(parsed, list) and all(isinstance(row, dict) for row in parsed):
        records = parsed
    else:
        raise ValueError(f"{source} must contain a JSON object, JSON list of objects, or JSONL objects")
    return _RecordsSnapshot(
        path=source,
        records=records,
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def _read_records(
    path: str,
    *,
    snapshot: Optional[_RecordsSnapshot] = None,
) -> list[dict[str, Any]]:
    return (snapshot or _read_records_snapshot(path)).records


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
        # Upstream exports repeat accepted answers. Exact deduplication changes
        # no accepted response, preserves order, and leaves source bytes intact.
        variants = list(dict.fromkeys(_as_texts(item, f"{label}[{number}]")))
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


def load_memoryagentbench(
    path: str,
    *,
    limit: Optional[int] = None,
    snapshot: Optional[_RecordsSnapshot] = None,
) -> list[dict]:
    """Load MemoryAgentBench's public context/question export.

    Its upstream conversation creator accepts a top-level ``data`` array, then
    reads ``context`` and aligned ``questions``/``answers`` fields.  Some
    downstream exports preserve individual memory events; when present we use
    them directly so ``subject_key``/``claim_kind`` can exercise the actual
    conflict-resolution write path.
    """
    roots = _read_records(path, snapshot=snapshot)
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
        label_source = "explicit_ids" if supporting_rows else "derived_answer_substring"
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
            label_provenance = label_source if supporting else "unlabeled"
            normalized_questions.append({
                "id": str(ids[q_number]) if ids else f"{case_id}:q:{q_number}",
                "q": question,
                "answer": answer,
                "answer_variants": answer_variants,
                "supporting": supporting,
                # MAB exports frequently lack adjudicated source IDs.  Preserve
                # the derivation method so a high-cardinality substring label is
                # never mistaken for an authoritative sufficient-evidence set.
                "evidence_label_provenance": label_provenance,
                "evidence_label_count": len(set(supporting)),
                "evidence_label_method": (
                    "provided_source_ids" if label_provenance == "explicit_ids"
                    else "answer_variant_substring" if label_provenance == "derived_answer_substring"
                    else "none"
                ),
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
    # Upstream reuses QA IDs across context-length/source variants. They are
    # distinct observations; qualify colliding IDs instead of dropping rows.
    counts = Counter(question["id"] for case in cases for question in case["questions"])
    for case in cases:
        for ordinal, question in enumerate(case["questions"]):
            if counts[question["id"]] > 1:
                question["source_question_id"] = question["id"]
                question["id"] = f"{case['id']}:q:{ordinal}:{question['id']}"
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
    snapshot: Optional[_RecordsSnapshot] = None,
) -> list[dict]:
    """Load Locomo-Plus unified input and score cue retrieval deterministically.

    The official unified file also contains the five original LoCoMo categories.
    The default selects only the new Cognitive category so a run measures implicit
    cue-to-trigger memory instead of quietly becoming another factual LoCoMo run.
    """
    rows = _read_records(path, snapshot=snapshot)
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


def load_mem2actbench(
    qa_path: str,
    conversation_path: str,
    *,
    limit: Optional[int] = None,
    qa_snapshot: Optional[_RecordsSnapshot] = None,
    conversation_snapshot: Optional[_RecordsSnapshot] = None,
) -> list[dict]:
    """Load Mem2ActBench's paired QA/session JSONL exports."""
    sessions = _read_records(conversation_path, snapshot=conversation_snapshot)
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
    for number, qa in enumerate(
        _limited_rows(_read_records(qa_path, snapshot=qa_snapshot), limit)
    ):
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
_PACKED_RETRIEVAL_METRICS = (
    "packed_recall_at_k", "packed_hit_at_k", "packed_mrr_at_k", "packed_ndcg_at_k",
)


def _producer_snapshot() -> dict:
    return {**producer_snapshot(), "eval/agent_benchmarks.py": sha256_file(Path(__file__))}


def _producer_source_manifest(
    root: Path, producer_digests: dict[str, str],
) -> tuple[list[Path], list[str]]:
    """Resolve producer paths and retain only stable public source names."""
    paths: list[Path] = []
    names: list[str] = []
    for name in producer_digests:
        candidate = Path(name)
        windows_candidate = PureWindowsPath(name)
        if candidate.is_absolute() or windows_candidate.is_absolute():
            # Production snapshots are repository-relative. A basename-only
            # fallback keeps private/test-injected snapshots from leaking an
            # absolute path; the shared envelope rejects collisions/unsafe names.
            paths.append(candidate)
            names.append(
                windows_candidate.name if windows_candidate.is_absolute() else candidate.name
            )
        else:
            paths.append(root / candidate)
            names.append(name)
    return paths, names


def _separate_unlabeled_retrieval(report: dict) -> None:
    """Do not award perfect retrieval to questions with no gold evidence IDs."""
    detail = list(report.get("detail") or [])
    retrieval_rows = []
    for row in detail:
        row["retrieval_scored"] = bool(row.get("supporting_ids")) and row.get("retrieval_scored") is not False
        if row["retrieval_scored"]:
            retrieval_rows.append(row)
        else:
            row["retrieval_excluded"] = "no_gold_evidence"
            for field in _RETRIEVAL_METRICS + _PACKED_RETRIEVAL_METRICS:
                row.pop(field, None)
    report["retrieval_scored_questions"] = len(retrieval_rows)
    for field in ("recall_at_k", "hit_at_k", "mrr_at_k", "ndcg_at_k") + _PACKED_RETRIEVAL_METRICS:
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
    token_budget: int = 1500,
    source_snapshot: Optional[dict[str, str]] = None,
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
        "answer_scored_questions", "packed_recall_at_k", "packed_hit_at_k",
        "packed_mrr_at_k", "packed_ndcg_at_k", "packed_answer_token_recall",
        "sufficient_evidence_proxy_rate", "sufficient_evidence_proxy_questions",
        "sufficient_evidence_proxy_boundary",
        "evidence_label_provenance", "evidence_label_cardinality",
        "checkpoint_status", "completed_cases", "expected_cases", "explicit_local_restarts",
        "case_wall_seconds", "query_latency_ms_sum", "latency_boundary",
    )
    metrics = {name: report[name] for name in metric_names if name in report}
    metrics["claim_boundary"] = _claim_boundary(fmt)
    root = Path(__file__).resolve().parents[1]
    producer_digests = source_snapshot if source_snapshot is not None else _producer_snapshot()
    producer_paths, producer_names = _producer_source_manifest(root, producer_digests)
    source_paths = [Path(dataset), *([Path(conversations)] if conversations else []),
                    *producer_paths]
    source_names = ["inputs/dataset", *(["inputs/conversations"] if conversations else []),
                    *producer_names]
    dataset_digest = sha256_file(dataset)
    expected_sources = [("inputs/dataset", dataset_digest)]
    if conversations:
        expected_sources.append(("inputs/conversations", sha256_file(conversations)))
    expected_sources.extend(zip(producer_names, producer_digests.values()))
    command = [
        "python", "-m", "eval.agent_benchmarks",
        "--dataset", "<dataset>",
        "--format", fmt,
        "--k", str(k),
        "--token-budget", str(token_budget),
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
    envelope = report_envelope(
        suite=f"Engraphis {fmt}",
        dataset_path=dataset,
        source_paths=source_paths,
        source_names=source_names,
        config={
            "measurement_scope": "retrieval_only",
            "source_case_identity": "explicit",
            "format": fmt,
            "k": k,
            "token_budget": token_budget,
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
    return verify_report_snapshot(envelope, dataset_sha256=dataset_digest, sources=expected_sources)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run offline agent-memory benchmark adapters.")
    parser.add_argument("--dataset", required=True, help="Benchmark JSON or JSONL export.")
    parser.add_argument("--format", required=True, choices=sorted(LOADERS))
    parser.add_argument("--conversations", help="Mem2ActBench toolmem_conversation.jsonl (required there).")
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--token-budget", type=int, default=1500)
    parser.add_argument("--checkpoint-dir", type=Path,
                        help="Private per-case recovery directory; immutable source/model/config binding.")
    parser.add_argument("--restart-interrupted", action="store_true",
                        help="Explicitly retain and restart an interrupted local-only case.")
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
        if args.k <= 0 or args.token_budget <= 0:
            raise ValueError("k and token budget must be positive integers")
        if args.restart_interrupted and not args.checkpoint_dir:
            raise ValueError("restart-interrupted requires a checkpoint directory")
        if bool(args.embed_model) != bool(args.embed_revision):
            raise ValueError("--embed-model and --embed-revision must be used together")
        if args.embed_revision and _PINNED_EMBED_REVISION.fullmatch(args.embed_revision) is None:
            raise ValueError("--embed-revision must be an immutable lowercase 40-character commit")
        source_before = _producer_snapshot()
        data_snapshots = {
            name: _read_records_snapshot(name)
            for name in (args.dataset, args.conversations) if name
        }
        data_before = {name: snapshot.sha256 for name, snapshot in data_snapshots.items()}
        if args.format == "mem2actbench":
            if not args.conversations:
                raise ValueError("--conversations is required for mem2actbench")
            cases = load_mem2actbench(
                args.dataset,
                args.conversations,
                limit=args.limit,
                qa_snapshot=data_snapshots[args.dataset],
                conversation_snapshot=data_snapshots[args.conversations],
            )
        elif args.format == "locomo_plus":
            cases = load_locomo_plus(
                args.dataset,
                limit=args.limit,
                include_original_locomo=args.include_original_locomo,
                snapshot=data_snapshots[args.dataset],
            )
        else:
            cases = LOADERS[args.format](
                args.dataset, limit=args.limit, snapshot=data_snapshots[args.dataset]
            )
        embedder = (
            get_embedder(args.embed_model, revision=args.embed_revision)
            if args.embed_model else None
        )
        if args.checkpoint_dir:
            report = run_resumable(
                cases, directory=args.checkpoint_dir,
                binding={"format": args.format, "data_sha256": data_before,
                         "limit": args.limit, "include_original_locomo": args.include_original_locomo,
                         "embed_model": getattr(embedder, "model_name", "DeterministicEmbedder"),
                         "embed_revision": getattr(embedder, "revision", None)},
                embedder=embedder if embedder is not None else DeterministicEmbedder(),
                k=args.k, token_budget=args.token_budget,
                resolve_conflicts=not args.no_resolve, snapshot=_producer_snapshot,
                restart_interrupted=args.restart_interrupted,
            )
        else:
            report = run(cases, k=args.k, token_budget=args.token_budget,
                         embedder=embedder, resolve_conflicts=not args.no_resolve)
        if (source_before != _producer_snapshot()
                or data_before != {name: sha256_file(name) for name in data_before}):
            raise ValueError("diagnostic producer or data changed during execution")
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
        try:
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
                token_budget=args.token_budget,
                source_snapshot=source_before,
            )
        except ValueError as exc:
            parser.error(f"diagnostic artifact cannot bind the evaluated producer or data snapshots: {exc}")
        # Envelope construction reads files again. Verify the completed envelope
        # against the snapshots that actually bounded this evaluation, including
        # changes made while the private report was serialized or printed.
        _, producer_names = _producer_source_manifest(
            Path(__file__).resolve().parents[1], source_before,
        )
        expected_sources = [("inputs/dataset", data_before[args.dataset])]
        if args.conversations:
            expected_sources.append(("inputs/conversations", data_before[args.conversations]))
        expected_sources.extend(zip(producer_names, source_before.values()))
        observed_sources = [(item["name"], item["sha256"])
                            for item in artifact["suite"]["sources"]]
        if (artifact["suite"]["sha256"] != data_before[args.dataset]
                or observed_sources != expected_sources):
            parser.error("diagnostic artifact does not match the evaluated producer or data snapshots")
        write_canonical_artifact(artifact, args.artifact)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
