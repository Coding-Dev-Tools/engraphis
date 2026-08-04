"""External benchmark adapter — run LoCoMo / LongMemEval through the real engine.

The fixture evals (``eval.harness`` on ``sample.jsonl``/``codemem.jsonl``) are a
pipeline-correctness gate, not a public benchmark claim. This adapter loads each
benchmark and pushes it through the shipped ``MemoryEngine`` write path (conflict
resolution, evolution) and hybrid recall.

It measures **retrieval** (evidence recall@k / hit@k), not end-to-end QA accuracy.
An official answering model and evaluator are required before reporting QA accuracy.
Credential-shaped source text is redacted before the fixture reaches the engine;
the report records the number of affected source records.

Usage::

    # LoCoMo (https://github.com/snap-research/locomo → data/locomo10.json)
    python -m eval.external --dataset locomo10.json --format locomo \
        --embed-model sentence-transformers/all-MiniLM-L6-v2 --k 10

    # LongMemEval (https://github.com/xiaowu0162/LongMemEval → longmemeval_s.json)
    python -m eval.external --dataset longmemeval_s.json --format longmemeval --k 10

    # Plumbing check without the model download (deterministic embedder):
    python -m eval.external --dataset locomo10.json --format locomo --offline --limit 2

    # A canonical run refuses --limit so its denominator cannot be partial:
    python -m eval.external --dataset longmemeval_s.json --format longmemeval --canonical

Both loaders normalize to the ``eval.harness`` case shape, so every metric and
resolution behaviour is identical to the CI gate.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Optional

from engraphis.backends.embedder_st import get_embedder
from engraphis.core.secrets import redact_secrets
from eval.harness import run


# ── LoCoMo ─────────────────────────────────────────────────────────────────────

def load_locomo(path: str, *, limit: Optional[int] = None) -> list[dict]:
    """snap-research LoCoMo → harness cases.

    Each dialog turn becomes one memory tagged with its LoCoMo ``dia_id`` (e.g.
    ``D1:3``); each QA item's ``evidence`` lists the supporting ``dia_id``s.
    Adversarial items (category 5) are retained with ``answerable=False``.  A
    retrieval score is undefined for those items, but retaining them prevents a
    public report from silently changing the benchmark denominator.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = [raw]
    cases = []
    selected = raw[:limit] if limit is not None else raw
    for sample in selected:
        conv = sample.get("conversation") or {}
        memories = []
        redactions = 0
        for key, turns in conv.items():
            if not key.startswith("session_") or key.endswith("_date_time") or not isinstance(turns, list):
                continue
            stamp = conv.get(f"{key}_date_time", "")
            for turn in turns:
                if not isinstance(turn, dict):
                    continue
                tag = str(turn.get("dia_id") or "").strip()
                text = str(turn.get("text") or "").strip()
                speaker = str(turn.get("speaker") or "").strip()
                if not tag or not text:
                    continue
                prefix = f"[{stamp}] " if stamp else ""
                raw_text = f"{prefix}{speaker}: {text}"
                safe_text = redact_secrets(raw_text)
                redactions += int(safe_text != raw_text)
                memories.append({"tag": tag, "text": safe_text})
        questions = []
        for question_number, qa in enumerate(sample.get("qa") or []):
            supporting = [str(e).strip() for e in (qa.get("evidence") or []) if str(e).strip()]
            category = str(qa.get("category") or "unknown")
            questions.append({
                "id": f"{sample.get('sample_id') or len(cases)}:{question_number}",
                "q": str(qa.get("question") or ""),
                "answer": str(qa.get("answer") or ""),
                "supporting": supporting,
                "category": category,
                "answerable": bool(supporting),
                "exclusion_reason": "no_gold_evidence" if not supporting else "",
            })
        if memories and questions:
            cases.append({"id": str(sample.get("sample_id") or f"locomo-{len(cases)}"),
                          "memories": memories, "questions": questions,
                          "source_secret_redactions": redactions})
    return cases


# ── LongMemEval ────────────────────────────────────────────────────────────────

def load_longmemeval(path: str, *, limit: Optional[int] = None) -> list[dict]:
    """LongMemEval (S/M) → harness cases.

    Each haystack *session* becomes one memory (turns joined, newline-separated),
    tagged with its session id; ``answer_session_ids`` are the supporting evidence.
    Abstention instances (id ending ``_abs``) are retained with their question
    type and an explicit ``answerable=False`` marker.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    cases = []
    selected = raw[:limit] if limit is not None else raw
    for inst in selected:
        qid = str(inst.get("question_id") or f"lme-{len(cases)}")
        session_ids = inst.get("haystack_session_ids") or []
        sessions = inst.get("haystack_sessions") or []
        dates = inst.get("haystack_dates") or []
        if len(session_ids) != len(sessions):
            raise ValueError(
                f"{qid}: haystack_session_ids and haystack_sessions must have equal lengths"
            )
        if dates and len(dates) != len(sessions):
            raise ValueError(f"{qid}: haystack_dates must be empty or align with haystack_sessions")
        memories = []
        redactions = 0
        # The cleaned LongMemEval-S release repeats a small number of session IDs,
        # always with identical conversation content but occasionally a different
        # haystack date label. A benchmark memory needs a unique source identity, so
        # retain the first occurrence. Different conversation content under one source
        # ID remains ambiguous and fails closed.
        memory_by_session_id: dict[str, str] = {}
        for index, (sid, session) in enumerate(zip(session_ids, sessions)):
            if not isinstance(session, list):
                continue
            date = dates[index] if dates else ""
            lines = [f"{t.get('role', '')}: {t.get('content', '')}"
                     for t in session if isinstance(t, dict) and t.get("content")]
            if not lines:
                continue
            prefix = f"[{date}] " if date else ""
            session_id = str(sid)
            content = "\n".join(lines)
            previous = memory_by_session_id.get(session_id)
            if previous is None:
                memory_by_session_id[session_id] = content
                raw_text = prefix + content
                safe_text = redact_secrets(raw_text)
                redactions += int(safe_text != raw_text)
                memories.append({"tag": session_id, "text": safe_text})
            elif previous != content:
                raise ValueError(
                    f"{qid}: duplicate session id {session_id!r} has conflicting content"
                )
        supporting = [str(s) for s in (inst.get("answer_session_ids") or [])]
        if memories:
            cases.append({"id": qid, "memories": memories,
                          "source_secret_redactions": redactions,
                          "questions": [{"q": str(inst.get("question") or ""),
                                         "answer": str(inst.get("answer") or ""),
                                         "supporting": supporting,
                                         "id": qid,
                                         "category": ("abstention" if qid.endswith("_abs")
                                                      else str(inst.get("question_type") or "unknown")),
                                         "answerable": not qid.endswith("_abs"),
                                         "question_date": str(inst.get("question_date") or ""),
                                         "exclusion_reason": (
                                             "abstention_no_gold_evidence"
                                             if qid.endswith("_abs") else ""
                                         )}]})
    return cases


LOADERS = {"locomo": load_locomo, "longmemeval": load_longmemeval}


def source_case_count(path: str) -> int:
    """Count source cases before normalization so canonical runs catch drops."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return 1 if isinstance(raw, dict) else len(raw) if isinstance(raw, list) else 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Run an external memory benchmark through Engraphis.")
    ap.add_argument("--dataset", required=True, help="Path to the benchmark JSON file.")
    ap.add_argument("--format", required=True, choices=sorted(LOADERS),
                    help="Benchmark format.")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--limit", type=int, default=None, help="Cap the number of cases.")
    ap.add_argument(
        "--canonical", action="store_true",
        help="Require a full official-dataset run; rejects --limit/partial input.",
    )
    ap.add_argument("--embed-model", default="sentence-transformers/all-MiniLM-L6-v2",
                    help="sentence-transformers model for real numbers.")
    ap.add_argument("--offline", action="store_true",
                    help="Use the deterministic embedder (plumbing check, not a claim).")
    ap.add_argument("--no-resolve", action="store_true",
                    help="Disable write-path conflict resolution (repeats stay separate; "
                         "recommended for turn-level dialogue datasets).")
    ap.add_argument("--json", dest="json_out", default=None,
                    help="Also write the full JSON report to this path.")
    args = ap.parse_args(argv)
    if args.canonical and args.limit is not None:
        ap.error("--canonical rejects --limit; canonical artifacts must score every source case")

    cases = LOADERS[args.format](args.dataset, limit=args.limit)
    if args.canonical and len(cases) != source_case_count(args.dataset):
        print("canonical run rejected: normalization excluded source cases", file=sys.stderr)
        return 2
    if not cases:
        print("no usable cases found — is the file the right format?")
        return 2
    n_mem = sum(len(c["memories"]) for c in cases)
    n_q = sum(len(c["questions"]) for c in cases)
    source_secret_redactions = sum(int(c.get("source_secret_redactions", 0)) for c in cases)
    embedder = get_embedder(None if args.offline else args.embed_model)
    embedder_name = type(embedder).__name__
    print(f"{args.format}: {len(cases)} cases · {n_mem} memories · {n_q} questions "
          f"· embedder={embedder_name} · k={args.k}")
    if args.offline or embedder_name == "DeterministicEmbedder":
        print("NOTE: deterministic embedder — this validates plumbing; it is NOT a "
              "publishable retrieval number.")

    t0 = time.time()
    report = run(cases, k=args.k, embedder=embedder,
                 resolve_conflicts=not args.no_resolve)
    dt = time.time() - t0
    report["dataset"] = str(args.dataset)
    report["format"] = args.format
    report["embedder"] = embedder_name
    report["measures"] = "retrieval (evidence recall@k), not end-to-end QA accuracy"
    report["wall_seconds"] = round(dt, 1)
    report["canonical"] = bool(args.canonical)
    report["source_secret_redactions"] = source_secret_redactions

    print(f"\nEngraphis × {args.format} — {report['questions']} questions @ k={args.k} "
          f"({dt:.1f}s)")
    print(f"  evidence recall@k   : {report['recall_at_k']:.3f}")
    print(f"  evidence hit@k      : {report['hit_at_k']:.3f}")
    print(f"  answer_token_recall : {report['answer_token_recall']:.3f}")
    print(f"  retrieval scored    : {report['scored_questions']}/{report['questions']} "
          f"(exclusions={len(report['exclusions'])})")
    if source_secret_redactions:
        print(f"  source redactions   : {source_secret_redactions} credential-shaped records")
    if args.json_out:
        slim = {k: v for k, v in report.items() if k != "detail"}
        Path(args.json_out).write_text(json.dumps(slim, indent=2), encoding="utf-8")
        print(f"  report written      : {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
