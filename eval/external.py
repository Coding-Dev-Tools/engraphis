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

    # The official LoCoMo JSON has three irrecoverable evidence-ID annotations. The
    # checked-in manifest is bound to the source hash and makes each repair auditable:
    python -m eval.external --dataset locomo10.json --format locomo --canonical \
        --locomo-repair-manifest eval/datasets/locomo10_repair_manifest.json

Both loaders normalize to the ``eval.harness`` case shape, so every metric and
resolution behaviour is identical to the CI gate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Optional

from engraphis.backends.embedder_st import get_embedder
from engraphis.core.secrets import redact_secrets
from eval.harness import run


# ── LoCoMo ─────────────────────────────────────────────────────────────────────

def load_locomo(
    path: str,
    *,
    limit: Optional[int] = None,
    repair_manifest: Optional[str] = None,
) -> list[dict]:
    """snap-research LoCoMo → harness cases.

    Each dialog turn becomes one memory tagged with its LoCoMo ``dia_id`` (e.g.
    ``D1:3``); each QA item's ``evidence`` lists the supporting ``dia_id``s.
    Adversarial items (category 5) are retained with ``answerable=False``.  A
    retrieval score is undefined for those items, but retaining them prevents a
    public report from silently changing the benchmark denominator. Unknown evidence
    IDs fail closed unless an exact dataset-hash-bound repair manifest accounts for them.
    """
    cases, _ = _load_locomo_with_integrity(
        path, limit=limit, repair_manifest=repair_manifest,
    )
    return cases


# ── LongMemEval ────────────────────────────────────────────────────────────────

_LOCOMO_DIA_ID = re.compile(r'^D\d+:\d+$')
_LOCOMO_DIA_ID_GROUP = re.compile(r'^D\d+:\d+(?:[;\s]+D\d+:\d+)+$')
_LOCOMO_REPAIR_SCHEMA = 'engraphis-locomo-repair/v1'


def _locomo_supporting_ids(value: object) -> list[str]:
    '''Split only unambiguous delimiter-joined LoCoMo dialogue identifiers.'''
    if value is None:
        return []
    values = value if isinstance(value, list) else [value]
    supporting: list[str] = []
    for item in values:
        if not isinstance(item, str):
            raise ValueError('LoCoMo evidence IDs must be strings')
        text = item.strip()
        if not text:
            continue
        if _LOCOMO_DIA_ID_GROUP.fullmatch(text):
            supporting.extend(_locomo_supporting_ids(re.split(r'[;\s]+', text)))
        elif match := re.fullmatch(r'D(\d+):0+(\d+)', text):
            supporting.append(f'D{int(match.group(1))}:{int(match.group(2))}')
        elif match := re.fullmatch(r'D:(\d+):(\d+)', text):
            supporting.append(f'D{int(match.group(1))}:{int(match.group(2))}')
        else:
            supporting.append(text)
    return supporting


def _locomo_evidence(value: object, *, case_id: str, question_number: int) -> list[str]:
    """Validate source evidence shape without applying ID normalization."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    raise ValueError(
        f'{case_id}:{question_number}: LoCoMo evidence must be a string or list of strings'
    )


def _load_locomo_repair_manifest(
    path: str,
    *,
    dataset_hash: str,
) -> tuple[dict[tuple[str, int, str], Optional[str]], dict[str, Any]]:
    manifest_path = Path(path)
    payload = json.loads(manifest_path.read_text(encoding='utf-8'))
    if not isinstance(payload, dict) or payload.get('schema') != _LOCOMO_REPAIR_SCHEMA:
        raise ValueError(f'LoCoMo repair manifest must use schema {_LOCOMO_REPAIR_SCHEMA!r}')
    if payload.get('dataset_sha256') != dataset_hash:
        raise ValueError('LoCoMo repair manifest does not match the source dataset SHA-256')
    rows = payload.get('repairs')
    if not isinstance(rows, list):
        raise ValueError('LoCoMo repair manifest repairs must be a list')

    repairs: dict[tuple[str, int, str], Optional[str]] = {}
    normalized_rows: list[dict[str, Any]] = []
    expected_fields = {'case_id', 'question_index', 'from', 'to'}
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != expected_fields:
            raise ValueError(f'LoCoMo repair manifest row {index} has invalid fields')
        case_id = row.get('case_id')
        question_index = row.get('question_index')
        source = row.get('from')
        target = row.get('to')
        if not isinstance(case_id, str) or not case_id:
            raise ValueError(f'LoCoMo repair manifest row {index} has an invalid case_id')
        if not isinstance(question_index, int) or isinstance(question_index, bool) \
                or question_index < 0:
            raise ValueError(f'LoCoMo repair manifest row {index} has an invalid question_index')
        if not isinstance(source, str) or not source:
            raise ValueError(f'LoCoMo repair manifest row {index} has an invalid source ID')
        if target is not None and (
            not isinstance(target, str) or _LOCOMO_DIA_ID.fullmatch(target) is None
        ):
            raise ValueError(f'LoCoMo repair manifest row {index} has an invalid target ID')
        key = (case_id, question_index, source)
        if key in repairs:
            raise ValueError(f'LoCoMo repair manifest contains duplicate repair {key!r}')
        repairs[key] = target
        normalized_rows.append({
            'case_id': case_id,
            'question_index': question_index,
            'from': source,
            'to': target,
        })

    return repairs, {
        'schema': _LOCOMO_REPAIR_SCHEMA,
        'path': str(manifest_path),
        'sha256': dataset_sha256(str(manifest_path)),
        'dataset_sha256': dataset_hash,
        'declared_repairs': normalized_rows,
    }


def _load_locomo_with_integrity(
    path: str,
    *,
    limit: Optional[int] = None,
    repair_manifest: Optional[str] = None,
) -> tuple[list[dict], dict[str, Any]]:
    if limit is not None and (
        isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0
    ):
        raise ValueError('limit must be a positive integer')
    raw = json.loads(Path(path).read_text(encoding='utf-8'))
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        raise ValueError('LoCoMo source must be a JSON object or list')

    source_hash = dataset_sha256(path)
    repairs: dict[tuple[str, int, str], Optional[str]] = {}
    manifest_info: Optional[dict[str, Any]] = None
    if repair_manifest:
        repairs, manifest_info = _load_locomo_repair_manifest(
            repair_manifest, dataset_hash=source_hash,
        )

    used_repairs: set[tuple[str, int, str]] = set()
    applied_repairs: list[dict[str, Any]] = []
    mechanical_normalizations = 0
    unknown: list[str] = []
    cases: list[dict] = []
    selected = raw[:limit] if limit is not None else raw
    for sample in selected:
        if not isinstance(sample, dict):
            raise ValueError('LoCoMo source cases must be JSON objects')
        raw_case_id = sample.get('sample_id')
        case_id = (
            raw_case_id.strip()
            if isinstance(raw_case_id, str) and raw_case_id.strip()
            else f'locomo-{len(cases)}'
        )
        conv = sample.get('conversation')
        if conv is None:
            conv = {}
        if not isinstance(conv, dict):
            raise ValueError(f'{case_id}: conversation must be a JSON object')
        memories: list[dict[str, str]] = []
        redactions = 0
        for key, turns in conv.items():
            if not isinstance(key, str):
                continue
            if not key.startswith('session_') or key.endswith('_date_time'):
                continue
            if not isinstance(turns, list):
                raise ValueError(f'{case_id}: {key} must be a list of dialogue turns')
            stamp = conv.get(f'{key}_date_time', '')
            for turn in turns:
                if not isinstance(turn, dict):
                    raise ValueError(f'{case_id}: {key} contains a non-object dialogue turn')
                tag_value = turn.get('dia_id')
                text_value = turn.get('text')
                if not isinstance(tag_value, str) or not tag_value.strip():
                    raise ValueError(f'{case_id}: dialogue turns require a non-empty dia_id')
                if not isinstance(text_value, str) or not text_value.strip():
                    raise ValueError(f'{case_id}: dialogue turns require non-empty text')
                tag = tag_value.strip()
                text = text_value.strip()
                speaker_value = turn.get('speaker', '')
                if speaker_value is None:
                    speaker = ''
                elif isinstance(speaker_value, str):
                    speaker = speaker_value.strip()
                else:
                    raise ValueError(f'{case_id}: dialogue speaker must be a string')
                prefix = f'[{stamp}] ' if stamp else ''
                raw_text = f'{prefix}{speaker}: {text}'
                safe_text = redact_secrets(raw_text)
                redactions += int(safe_text != raw_text)
                memories.append({'tag': tag, 'text': safe_text})

        memory_tags = {memory['tag'] for memory in memories}
        if len(memory_tags) != len(memories):
            raise ValueError(f'{case_id}: duplicate LoCoMo dia_id values')
        questions: list[dict[str, Any]] = []
        qa_rows = sample.get('qa')
        if qa_rows is None:
            qa_rows = []
        if not isinstance(qa_rows, list):
            raise ValueError(f'{case_id}: qa must be a list')
        for question_number, qa in enumerate(qa_rows):
            if not isinstance(qa, dict):
                raise ValueError(f'{case_id}:{question_number}: QA rows must be objects')
            source_supporting = _locomo_evidence(
                qa.get('evidence'),
                case_id=case_id,
                question_number=question_number,
            )
            supporting = _locomo_supporting_ids(source_supporting)
            mechanical_normalizations += int(supporting != source_supporting)
            repaired: list[str] = []
            for support_id in supporting:
                key = (case_id, question_number, support_id)
                if key not in repairs:
                    repaired.append(support_id)
                    continue
                if key in used_repairs:
                    raise ValueError(f'LoCoMo repair {key!r} matched more than once')
                used_repairs.add(key)
                target = repairs[key]
                applied = {
                    'case_id': case_id,
                    'question_index': question_number,
                    'from': support_id,
                    'to': target,
                }
                applied_repairs.append(applied)
                if target is not None:
                    repaired.append(target)
            supporting = repaired
            if len(set(supporting)) != len(supporting):
                raise ValueError(
                    f'{case_id}:{question_number}: duplicate supporting dialogue IDs after repair'
                )
            missing = sorted(set(supporting) - memory_tags)
            if missing:
                unknown.append(f'{case_id}:{question_number}: {", ".join(missing)}')
            category = str(qa.get('category') or 'unknown')
            questions.append({
                'id': f'{case_id}:{question_number}',
                'q': str(qa.get('question') or ''),
                'answer': str(qa.get('answer') or ''),
                'supporting': supporting,
                'category': category,
                'answerable': bool(supporting),
                'exclusion_reason': 'no_gold_evidence' if not supporting else '',
            })
        if memories and questions:
            cases.append({
                'id': case_id,
                'memories': memories,
                'questions': questions,
                'source_secret_redactions': redactions,
            })

    unused = sorted(set(repairs) - used_repairs)
    if unused:
        raise ValueError(f'LoCoMo repair manifest contains unused repairs: {unused!r}')
    if unknown:
        detail = '; '.join(unknown)
        hint = (
            ' Supply a dataset-hash-bound --locomo-repair-manifest.'
            if repair_manifest is None else ''
        )
        raise ValueError(f'LoCoMo has unknown supporting dialogue IDs: {detail}.{hint}')

    integrity: dict[str, Any] = {
        'mechanically_normalized_questions': mechanical_normalizations,
        'repair_manifest': None,
    }
    if manifest_info is not None:
        manifest_info['applied_repairs'] = applied_repairs
        integrity['repair_manifest'] = manifest_info
    return cases, integrity


def load_longmemeval(path: str, *, limit: Optional[int] = None) -> list[dict]:
    """LongMemEval (S/M) → harness cases.

    Each haystack *session* becomes one memory (turns joined, newline-separated),
    tagged with its session id; ``answer_session_ids`` are the supporting evidence.
    Abstention instances (id ending ``_abs``) are retained with their question
    type and an explicit ``answerable=False`` marker.
    """
    if limit is not None and (
        isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0
    ):
        raise ValueError('limit must be a positive integer')
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        raise ValueError('LongMemEval source must be a JSON object or list')
    cases = []
    seen_question_ids: set[str] = set()
    selected = raw[:limit] if limit is not None else raw
    for instance_number, inst in enumerate(selected):
        if not isinstance(inst, dict):
            raise ValueError(f'LongMemEval instance {instance_number} must be a JSON object')
        raw_qid = inst.get('question_id')
        qid = (
            raw_qid.strip()
            if isinstance(raw_qid, str) and raw_qid.strip()
            else f'lme-{len(cases)}'
        )
        if qid in seen_question_ids:
            raise ValueError(f'duplicate LongMemEval question_id: {qid!r}')
        seen_question_ids.add(qid)
        session_ids = inst.get('haystack_session_ids')
        sessions = inst.get('haystack_sessions')
        dates = inst.get('haystack_dates')
        session_ids = [] if session_ids is None else session_ids
        sessions = [] if sessions is None else sessions
        dates = [] if dates is None else dates
        if not isinstance(session_ids, list) or not isinstance(sessions, list):
            raise ValueError(f'{qid}: haystack session fields must be lists')
        if not isinstance(dates, list):
            raise ValueError(f'{qid}: haystack_dates must be a list')
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
            if not isinstance(sid, str) or not sid.strip():
                raise ValueError(f'{qid}: haystack session IDs must be non-empty strings')
            if not isinstance(session, list):
                raise ValueError(f'{qid}: haystack session entries must be lists')
            session_id = sid.strip()
            date = dates[index] if dates else ""
            if date is not None and not isinstance(date, str):
                raise ValueError(f'{qid}: haystack_dates must contain strings')
            lines = []
            for turn in session:
                if not isinstance(turn, dict):
                    raise ValueError(f'{qid}: session {session_id!r} contains a non-object turn')
                content = turn.get('content')
                if not isinstance(content, str) or not content.strip():
                    raise ValueError(
                        f'{qid}: session {session_id!r} turns require non-empty content'
                    )
                role = turn.get('role', '')
                if role is not None and not isinstance(role, str):
                    raise ValueError(f'{qid}: session {session_id!r} turn role must be a string')
                lines.append(f"{role or ''}: {content.strip()}")
            if not lines:
                raise ValueError(f'{qid}: session {session_id!r} must contain a turn')
            content = "\n".join(lines)
            previous = memory_by_session_id.get(session_id)
            if previous is None:
                memory_by_session_id[session_id] = content
                prefix = f"[{date}] " if date else ""
                raw_text = prefix + content
                safe_text = redact_secrets(raw_text)
                redactions += int(safe_text != raw_text)
                memories.append({"tag": session_id, "text": safe_text})
            elif previous != content:
                raise ValueError(
                    f"{qid}: duplicate session id {session_id!r} has conflicting content"
                )
        answer_session_ids = inst.get('answer_session_ids')
        answer_session_ids = [] if answer_session_ids is None else answer_session_ids
        if not isinstance(answer_session_ids, list) or any(
            not isinstance(value, str) or not value.strip()
            for value in answer_session_ids
        ):
            raise ValueError(f'{qid}: answer_session_ids must be a list of non-empty strings')
        supporting = [value.strip() for value in answer_session_ids]
        unknown_support = sorted(set(supporting) - set(memory_by_session_id))
        if unknown_support:
            raise ValueError(
                f'{qid}: unknown answer_session_ids: {", ".join(unknown_support)}'
            )
        question = inst.get('question')
        if not isinstance(question, str) or not question.strip():
            raise ValueError(f'{qid}: question must be a non-empty string')
        if not memories:
            raise ValueError(f'{qid}: haystack must contain at least one usable session')
        cases.append({"id": qid, "memories": memories,
                      "source_secret_redactions": redactions,
                      "questions": [{"q": question.strip(),
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


_PINNED_REVISION = re.compile(r'[0-9a-f]{40}\Z')


def source_case_count(path: str) -> int:
    """Count source cases before normalization so canonical runs catch drops."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return 1 if isinstance(raw, dict) else len(raw) if isinstance(raw, list) else 0


def dataset_sha256(path: str) -> str:
    '''Return a content digest without treating a mutable path as benchmark provenance.'''
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


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
    ap.add_argument('--embed-revision', default=None,
                    help='Optional immutable model revision; required by --canonical.')
    ap.add_argument(
        '--locomo-repair-manifest', default=None,
        help='Hash-bound evidence-reference repairs for a LoCoMo source file.',
    )
    args = ap.parse_args(argv)
    if args.k <= 0:
        ap.error('--k must be a positive integer')
    if args.limit is not None and args.limit <= 0:
        ap.error('--limit must be a positive integer')
    if args.canonical and args.limit is not None:
        ap.error("--canonical rejects --limit; canonical artifacts must score every source case")

    if args.canonical and args.offline:
        ap.error('--canonical requires a pinned semantic embedder; --offline is plumbing only')
    if args.canonical and (
        not args.embed_revision or _PINNED_REVISION.fullmatch(args.embed_revision) is None
    ):
        ap.error('--canonical requires --embed-revision as a lowercase 40-character commit')
    if args.locomo_repair_manifest and args.format != 'locomo':
        ap.error('--locomo-repair-manifest is valid only with --format locomo')

    dataset_integrity: Optional[dict[str, Any]] = None
    try:
        if args.format == 'locomo':
            cases, dataset_integrity = _load_locomo_with_integrity(
                args.dataset,
                limit=args.limit,
                repair_manifest=args.locomo_repair_manifest,
            )
        else:
            cases = load_longmemeval(args.dataset, limit=args.limit)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f'external dataset rejected: {redact_secrets(str(exc))}', file=sys.stderr)
        return 2
    try:
        source_cases = source_case_count(args.dataset)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f'external dataset rejected: {redact_secrets(str(exc))}', file=sys.stderr)
        return 2
    if args.canonical and len(cases) != source_cases:
        print("canonical run rejected: normalization excluded source cases", file=sys.stderr)
        return 2
    if not cases:
        print("no usable cases found — is the file the right format?")
        return 2
    n_mem = sum(len(c["memories"]) for c in cases)
    n_q = sum(len(c["questions"]) for c in cases)
    source_secret_redactions = sum(int(c.get("source_secret_redactions", 0)) for c in cases)
    try:
        embedder = get_embedder(
            None if args.offline else args.embed_model,
            revision=args.embed_revision,
            require_immutable_models=bool(args.canonical),
        )
    except Exception as exc:
        print(
            f'external evaluation could not load the embedder ({type(exc).__name__})',
            file=sys.stderr,
        )
        return 2
    embedder_name = type(embedder).__name__
    if not args.offline and not bool(getattr(embedder, 'supports_semantic_search', False)):
        print(
            'external evaluation refused: the requested semantic embedder was unavailable; '
            'install sentence-transformers/model dependencies or use --offline for plumbing only',
            file=sys.stderr,
        )
        return 2
    print(f"{args.format}: {len(cases)} cases · {n_mem} memories · {n_q} questions "
          f"· embedder={embedder_name} · k={args.k}")
    if args.offline or embedder_name == "DeterministicEmbedder":
        print("NOTE: deterministic embedder — this validates plumbing; it is NOT a "
              "publishable retrieval number.")

    try:
        t0 = time.time()
        report = run(cases, k=args.k, embedder=embedder,
                     resolve_conflicts=not args.no_resolve)
        dt = time.time() - t0
        report['embedding'] = {
            'model_id': getattr(embedder, 'model_name', None),
            'revision': getattr(embedder, 'revision', None),
            'dimension': getattr(embedder, 'dim', None),
        }
        report['dataset_sha256'] = dataset_sha256(args.dataset)
    except Exception as exc:
        print(f'external evaluation failed ({type(exc).__name__})', file=sys.stderr)
        return 2
    report['source_cases'] = source_cases
    report['normalized_cases'] = len(cases)
    report['configuration'] = {
        'k': args.k,
        'limit': args.limit,
        'resolve_conflicts': not args.no_resolve,
    }
    if dataset_integrity is not None:
        report['dataset_integrity'] = dataset_integrity
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
        try:
            slim = {k: v for k, v in report.items() if k != "detail"}
            Path(args.json_out).write_text(json.dumps(slim, indent=2), encoding="utf-8")
        except (OSError, TypeError, ValueError) as exc:
            print(f'external report could not be written: {exc}', file=sys.stderr)
            return 2
        print(f"  report written      : {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
