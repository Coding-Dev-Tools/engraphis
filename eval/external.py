"""External benchmark adapter — run LoCoMo / LongMemEval through the real engine.

The fixture evals (``eval.harness`` on ``sample.jsonl``/``codemem.jsonl``) are a
pipeline-correctness gate, not a competitive claim. This adapter loads the two
benchmarks the field actually quotes and pushes them through the *same*
``MemoryEngine`` write path (conflict resolution, evolution) and hybrid recall that
ships — so the number you get is about the product, not a bare index.

What it measures — honestly: **retrieval** (evidence recall@k / hit@k), not
end-to-end QA accuracy. Published LoCoMo/LongMemEval scores from mem0/Zep also hinge
on an answering LLM + judge; this harness isolates the part Engraphis owns, needs no
API key, and states exactly that in the report. Add an answering model on top for a
QA-accuracy number when you want one.

Usage::

    # LoCoMo (https://github.com/snap-research/locomo → data/locomo10.json)
    python -m eval.external --dataset locomo10.json --format locomo \
        --embed-model sentence-transformers/all-MiniLM-L6-v2 --k 10

    # LongMemEval (https://github.com/xiaowu0162/LongMemEval → longmemeval_s.json)
    python -m eval.external --dataset longmemeval_s.json --format longmemeval --k 10

    # Plumbing check without the model download (deterministic embedder):
    python -m eval.external --dataset locomo10.json --format locomo --offline --limit 2

Both loaders normalize to the ``eval.harness`` case shape, so every metric and
resolution behaviour is identical to the CI gate.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Optional

from engraphis.backends.embedder_st import get_embedder
from eval.harness import run


# ── LoCoMo ─────────────────────────────────────────────────────────────────────

def load_locomo(path: str, *, limit: Optional[int] = None) -> list[dict]:
    """snap-research LoCoMo → harness cases.

    Each dialog turn becomes one memory tagged with its LoCoMo ``dia_id`` (e.g.
    ``D1:3``); each QA item's ``evidence`` lists the supporting ``dia_id``s.
    Adversarial items (category 5) have no evidence and are skipped — retrieval
    recall is undefined for "unanswerable".
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = [raw]
    cases = []
    for sample in raw[: limit or len(raw)]:
        conv = sample.get("conversation") or {}
        memories = []
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
                memories.append({"tag": tag, "text": f"{prefix}{speaker}: {text}"})
        questions = []
        for qa in sample.get("qa") or []:
            supporting = [str(e).strip() for e in (qa.get("evidence") or []) if str(e).strip()]
            if not supporting:
                continue
            questions.append({"q": str(qa.get("question") or ""),
                              "answer": str(qa.get("answer") or ""),
                              "supporting": supporting})
        if memories and questions:
            cases.append({"id": str(sample.get("sample_id") or f"locomo-{len(cases)}"),
                          "memories": memories, "questions": questions})
    return cases


# ── LongMemEval ────────────────────────────────────────────────────────────────

def load_longmemeval(path: str, *, limit: Optional[int] = None) -> list[dict]:
    """LongMemEval (S/M) → harness cases.

    Each haystack *session* becomes one memory (turns joined, newline-separated),
    tagged with its session id; ``answer_session_ids`` are the supporting evidence.
    Abstention instances (id ending ``_abs``) are skipped — same rationale as
    LoCoMo's adversarial category.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    cases = []
    for inst in raw[: limit or len(raw)]:
        qid = str(inst.get("question_id") or f"lme-{len(cases)}")
        if qid.endswith("_abs"):
            continue
        session_ids = inst.get("haystack_session_ids") or []
        sessions = inst.get("haystack_sessions") or []
        dates = inst.get("haystack_dates") or [""] * len(sessions)
        memories = []
        for sid, session, date in zip(session_ids, sessions, dates):
            if not isinstance(session, list):
                continue
            lines = [f"{t.get('role', '')}: {t.get('content', '')}"
                     for t in session if isinstance(t, dict) and t.get("content")]
            if not lines:
                continue
            prefix = f"[{date}] " if date else ""
            memories.append({"tag": str(sid), "text": prefix + "\n".join(lines)})
        supporting = [str(s) for s in (inst.get("answer_session_ids") or [])]
        if memories and supporting:
            cases.append({"id": qid, "memories": memories,
                          "questions": [{"q": str(inst.get("question") or ""),
                                         "answer": str(inst.get("answer") or ""),
                                         "supporting": supporting}]})
    return cases


LOADERS = {"locomo": load_locomo, "longmemeval": load_longmemeval}


def main() -> int:
    ap = argparse.ArgumentParser(description="Run an external memory benchmark through Engraphis.")
    ap.add_argument("--dataset", required=True, help="Path to the benchmark JSON file.")
    ap.add_argument("--format", required=True, choices=sorted(LOADERS),
                    help="Benchmark format.")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--limit", type=int, default=None, help="Cap the number of cases.")
    ap.add_argument("--embed-model", default="sentence-transformers/all-MiniLM-L6-v2",
                    help="sentence-transformers model for real numbers.")
    ap.add_argument("--offline", action="store_true",
                    help="Use the deterministic embedder (plumbing check, not a claim).")
    ap.add_argument("--no-resolve", action="store_true",
                    help="Disable write-path conflict resolution (repeats stay separate; "
                         "recommended for turn-level dialogue datasets).")
    ap.add_argument("--json", dest="json_out", default=None,
                    help="Also write the full JSON report to this path.")
    args = ap.parse_args()

    cases = LOADERS[args.format](args.dataset, limit=args.limit)
    if not cases:
        print("no usable cases found — is the file the right format?")
        return 2
    n_mem = sum(len(c["memories"]) for c in cases)
    n_q = sum(len(c["questions"]) for c in cases)
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

    print(f"\nEngraphis × {args.format} — {report['questions']} questions @ k={args.k} "
          f"({dt:.1f}s)")
    print(f"  evidence recall@k   : {report['recall_at_k']:.3f}")
    print(f"  evidence hit@k      : {report['hit_at_k']:.3f}")
    print(f"  answer_token_recall : {report['answer_token_recall']:.3f}")
    if args.json_out:
        slim = {k: v for k, v in report.items() if k != "detail"}
        Path(args.json_out).write_text(json.dumps(slim, indent=2), encoding="utf-8")
        print(f"  report written      : {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
