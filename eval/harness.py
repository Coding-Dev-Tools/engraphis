"""Eval runner: ingest fixture memories, query, score retrieval.

Routes both ingestion and querying through ``MemoryEngine`` — the same hybrid
vector+lexical+graph recall, six-term scoring, RRF fusion, and deterministic
conflict resolution that ships in production — not a bare vector-index lookup.
(Earlier versions of this harness called the vector index directly, which meant
the CI gate measured plumbing but never exercised the actual recall pipeline or
the write-path resolver; AGENTS.md §3.7 — "prove better with a number" — only
means something if the number is about what ships.)

Runs fully offline with the deterministic embedder + NumPy index, so it executes
anywhere (including CI) with no model download. The same harness will drive the
real backends — just pass a different ``Embedder`` in.

    python -m eval.harness --dataset eval/datasets/sample.jsonl --k 5

Dataset format (JSONL, one object per line):
    {
      "id": "case-1",
      "memories": [{"tag": "f1", "text": "..."}, ...],
      "questions": [{"q": "...", "answer": "...", "supporting": ["f1"]}]
    }

A memory's tag may be absent from the retrieved set without being "wrong": if its
text was resolved as a near-duplicate or superseded by a later memory in the same
case (conflict resolution — see ``core.resolve``), its tag now maps to whichever
memory *is* live, and that is what gets credited. This is intentional: the
"temporal-update" style fixtures rely on exactly this to test that superseded
facts stop being treated as current.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import subprocess
import time
from typing import Callable, Optional

from engraphis.backends import DeterministicEmbedder, NumpyVectorIndex
from engraphis.backends.reranker import IdentityReranker
from engraphis.core.engine import MemoryEngine
from engraphis.core.context import DeterministicContextPacker, RegexTokenCounter
from engraphis.core.grounded import build_grounded_answer
from engraphis.core.interfaces import (
    ContextUsage, Edge, MemoryRecord, MemoryType, Node, PackedChunk, Scope, SearchFilter,
)
from engraphis.core.recall import RecallResult
from engraphis.core.retrieval_policy import ProfileConfig
from engraphis.core.store import Store
from eval.benchmark import (
    CANONICAL_TOKEN_BUDGETS,
    canonical_benchmark_config,
    exclusion,
    paired_bootstrap_ci,
    question_record,
    report_envelope,
    sha256_text,
    stratified_bootstrap_ci,
    validate_canonical_profile,
    write_canonical_artifact,
)
from eval import metrics


class _PinnedReaderTokenCounter:
    """Count reader content tokens with a tokenizer loaded at one immutable revision."""

    def __init__(self, tokenizer: object, identity: str) -> None:
        self.tokenizer = tokenizer
        self.identity = identity

    def __call__(self, text: str) -> int:
        encode = getattr(self.tokenizer, "encode")
        try:
            return len(encode(text, add_special_tokens=False))
        except TypeError:
            return len(encode(text))


def _load_pinned_reader_token_counter(model: str, revision: str) -> Callable[[str], int]:
    """Load the canonical reader tokenizer without affecting the offline default."""
    try:
        from transformers import AutoProcessor
    except ImportError as exc:  # pragma: no cover - optional canonical benchmark dependency
        raise ValueError(
            "canonical output requires transformers and the pinned reader tokenizer"
        ) from exc
    processor = AutoProcessor.from_pretrained(model, revision=revision)
    tokenizer = getattr(processor, "tokenizer", processor)
    if not hasattr(tokenizer, "encode"):
        raise ValueError("canonical reader processor did not expose an encode-capable tokenizer")
    return _PinnedReaderTokenCounter(tokenizer, f"{model}@{revision}")


@dataclass(frozen=True)
class BaselineSpec:
    """One baseline with an explicit executable mode and recorded limitations."""

    label: str
    retrieval_profile: str
    vector: bool
    lexical: bool
    graph: bool
    no_retrieval: bool = False
    mode: str = "retrieval"
    disable_temporal_resolution: bool = False
    disable_reranker: bool = False
    requires_nonidentity_reranker: bool = False
    equivalent_to: Optional[str] = None

    @property
    def arm_config(self) -> ProfileConfig:
        return ProfileConfig(
            self.label, vector=self.vector, lexical=self.lexical,
            graph=self.graph, code=False,
        )

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "retrieval_profile": self.retrieval_profile,
            "arms": {
                "vector": self.vector,
                "lexical": self.lexical,
                "graph": self.graph,
                "code": False,
            },
            "no_retrieval": self.no_retrieval,
            "mode": self.mode,
            "temporal_resolution": "disabled" if self.disable_temporal_resolution else "enabled",
            "reranker": "disabled" if self.disable_reranker else "enabled",
            **({"equivalent_to": self.equivalent_to} if self.equivalent_to else {}),
        }


_EXECUTABLE_BASELINES = {
    "full_hybrid": BaselineSpec("full_hybrid", "balanced", True, True, True),
    "dense_only": BaselineSpec("dense_only", "balanced", True, False, False),
    "lexical_only": BaselineSpec("lexical_only", "lexical", False, True, False),
    # The current retrieval pipeline uses RRF whenever more than one arm is on.
    # With graph disabled this is operationally identical to ``no_graph``; retain
    # the published label, but make that equivalence visible in every artifact.
    "dense_lexical_rrf": BaselineSpec(
        "dense_lexical_rrf", "balanced", True, True, False, equivalent_to="no_graph",
    ),
    "full_history": BaselineSpec(
        "full_history", "balanced", False, False, False, mode="full_history",
    ),
    "no_graph": BaselineSpec("no_graph", "balanced", True, True, False),
    "no_reranker": BaselineSpec(
        "no_reranker", "balanced", True, True, True, disable_reranker=True,
        requires_nonidentity_reranker=True,
    ),
    "no_temporal_resolution": BaselineSpec(
        "no_temporal_resolution", "balanced", True, True, True,
        disable_temporal_resolution=True,
    ),
    "whole_document": BaselineSpec(
        "whole_document", "balanced", False, False, False, mode="whole_document",
    ),
    "no_retrieval": BaselineSpec("no_retrieval", "balanced", False, False, False, True),
}


def executable_baseline(label: str) -> BaselineSpec:
    """Return an honest harness baseline or fail before producing an artifact."""
    normalized = str(label or "").strip().casefold()
    if normalized not in _EXECUTABLE_BASELINES:
        supported = ", ".join(sorted(_EXECUTABLE_BASELINES))
        raise ValueError(
            f"baseline_label {label!r} is not executable by eval.harness; "
            f"supported labels: {supported}"
        )
    return _EXECUTABLE_BASELINES[normalized]


def _validate_baseline_dataset(dataset: list[dict], baseline: BaselineSpec, reranker: object) -> None:
    """Fail before an artifact when a claimed ablation has no representable input."""
    if baseline.mode == "whole_document" and not dataset:
        raise ValueError("whole_document requires a non-empty dataset")
    if baseline.mode == "whole_document" and not all(
        isinstance(case.get("document"), str) and case["document"].strip() for case in dataset
    ):
        raise ValueError("whole_document requires a non-empty document in every dataset case")
    if baseline.mode == "full_history" and not dataset:
        raise ValueError("full_history requires a non-empty dataset")
    if baseline.mode == "full_history" and not all(
        isinstance(case.get("memories"), list) and case["memories"] for case in dataset
    ):
        raise ValueError("full_history requires ordered non-empty memories in every dataset case")
    if baseline.disable_temporal_resolution:
        groups: list[list[dict]] = []
        for case in dataset:
            grouped: dict[tuple[str, str], list[dict]] = {}
            for item in case.get("memories", []):
                key = (str(item.get("subject_key", "")).strip(), str(item.get("claim_kind", "")).strip())
                if key[0]:
                    grouped.setdefault(key, []).append(item)
            groups.extend(grouped.values())
        representable = any(
            len(group) >= 2
            and len({str(item.get("text", "")) for item in group}) >= 2
            and all(item.get("valid_from") is not None for item in group)
            for group in groups
        )
        if not representable:
            raise ValueError(
                "no_temporal_resolution requires two memories with the same non-empty "
                "subject_key (and claim_kind)"
            )
    if baseline.requires_nonidentity_reranker and isinstance(reranker, IdentityReranker):
        raise ValueError("no_reranker requires a non-identity reranker to make the ablation meaningful")


def _whole_source_result(
    records: list[MemoryRecord],
    *,
    label: str,
    token_budget: Optional[int],
    token_counter: Optional[Callable[[str], int]] = None,
    token_counter_identity: Optional[str] = None,
) -> RecallResult:
    """Return exact source text for corpus baselines, never query-selecting or truncating it."""
    counter = token_counter or RegexTokenCounter()
    counter_identity = (
        token_counter_identity
        or getattr(counter, "identity", None)
        or type(counter).__name__
    )
    context = "\n\n".join(record.content for record in records)
    tokens = counter(context)
    if token_budget is not None and tokens > int(token_budget):
        raise ValueError(f"{label} cannot preserve complete source under token_budget={token_budget}")
    packed = [PackedChunk(
        id=record.id, excerpt=record.content, tokens=counter(record.content), reason=label,
    ) for record in records]
    usage = ContextUsage(
        budget_tokens=tokens if token_budget is None else int(token_budget),
        context_tokens=tokens, source_tokens=tokens, saved_tokens=0, savings_ratio=0.0,
        packed_count=len(packed), omitted_count=0, token_counter=counter_identity,
    )
    return RecallResult(
        chunks=[{"id": record.id, "title": record.title, "content": record.content}
                for record in records],
        context=context, count=len(records), packed_chunks=packed, usage=usage,
        retrieval_profile=label,
        token_counter=counter,
    )


def _recall_for_baseline(
    engine: MemoryEngine,
    query: str,
    *,
    workspace_id: str,
    repo_id: str,
    k: int,
    token_budget: Optional[int],
    baseline: BaselineSpec,
    source_records: Optional[list[MemoryRecord]] = None,
) -> RecallResult:
    """Run the declared arms directly, without expanding ``RetrievalPolicy``."""
    budget = engine.recall_engine.token_budget if token_budget is None else max(0, int(token_budget))
    flt = SearchFilter(workspace_id=workspace_id, repo_id=repo_id, include_ancestors=True)
    if baseline.mode in {"full_history", "whole_document"}:
        packer = engine.recall_engine.context_packer
        return _whole_source_result(
            source_records or [],
            label=baseline.label,
            token_budget=token_budget,
            token_counter=getattr(packer, "count_tokens", None),
            token_counter_identity=getattr(packer, "token_counter_identity", None),
        )
    if baseline.no_retrieval:
        context, packed, usage = engine.recall_engine.context_packer.pack(query, [], budget)
        return RecallResult(
            context=context, packed_chunks=packed, usage=usage,
            retrieval_profile=baseline.label,
            token_counter=getattr(engine.recall_engine.context_packer, "count_tokens", None),
        )
    return engine.recall_engine.recall(
        query, flt, k=k, token_budget=token_budget,
        retrieval_profile=baseline.retrieval_profile,
        arm_config=baseline.arm_config,
    )


def load_dataset(path: str) -> list[dict]:
    items = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            items.append(json.loads(line))
    return items


def _git_commit() -> str:
    """Return the checked-out commit when available, without making it a dependency."""
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip() or "unknown"
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _seed_case_graph(
    store: Store,
    *,
    workspace_id: str,
    repo_id: str,
    case: dict,
) -> None:
    """Persist an optional fixture graph before its memories are written.

    The ordinary harness previously ignored ``entities``/``edges`` even though
    graph fixtures declare them.  Seeding first also lets the production write
    path persist exact memory↔entity incidence, so the harness measures the
    shipped sparse graph arm rather than accidentally falling back to dense
    retrieval alone.
    """
    entity_ids: dict[str, str] = {}
    for entity in case.get("entities", []):
        name = str(entity[0])
        entity_ids[name] = store.upsert_entity(Node(
            id="",
            name=name,
            ntype=(str(entity[1]) if len(entity) > 1 else "concept"),
            workspace_id=workspace_id,
            repo_id=repo_id,
        ))
    for edge in case.get("edges", []):
        source = entity_ids.get(str(edge[0]))
        target = entity_ids.get(str(edge[1]))
        if source is None or target is None:
            raise ValueError(
                f"eval edge references an unknown entity: {edge[0]!r} -> {edge[1]!r}"
            )
        store.upsert_edge(Edge(
            id="",
            src=source,
            dst=target,
            relation=(str(edge[2]) if len(edge) > 2 else "rel"),
            workspace_id=workspace_id,
            repo_id=repo_id,
        ))


def _usage_dict(usage, *, budget: int) -> dict:
    """Keep the public v2 usage contract complete even for an empty recall."""
    if usage is not None:
        return asdict(usage)
    return {
        "budget_tokens": budget,
        "context_tokens": 0,
        "source_tokens": 0,
        "saved_tokens": 0,
        "savings_ratio": 0.0,
        "packed_count": 0,
        "omitted_count": 0,
        "token_counter": "unknown",
    }


def _mean(records: list[dict], field: str) -> float:
    return sum(float(item.get(field, 0.0)) for item in records) / max(len(records), 1)


def _v2_metrics(records: list[dict], *, bootstrap_iterations: int) -> dict:
    """Aggregate conventional retrieval scores plus deterministic uncertainty."""
    scored = [item for item in records if not item.get("excluded")]
    metric_fields = [
        "recall_at_1", "recall_at_5", "recall_at_10",
        "mrr_at_1", "mrr_at_5", "mrr_at_10",
        "ndcg_at_1", "ndcg_at_5", "ndcg_at_10",
    ]
    summary = {field: round(_mean(scored, field), 6) for field in metric_fields}
    summary["answer_token_recall"] = round(_mean(scored, "answer_token_recall"), 6)
    summary["confidence_intervals"] = {
        field: stratified_bootstrap_ci(
            scored,
            lambda rows, metric=field: _mean(list(rows), metric),
            iterations=bootstrap_iterations,
        )
        for field in metric_fields
    }
    # A paired interval is meaningful only when a baseline contains the same
    # question IDs.  Keep the stable field present so artifact consumers never
    # mistake an absent comparison for a zero-effect result.
    summary["paired_bootstrap"] = {
        "available": False,
        "reason": "baseline_records_not_supplied",
        "n": 0,
        "delta": None,
        "low": None,
        "high": None,
        "iterations": bootstrap_iterations,
    }
    labeled = [item for item in records if isinstance(item.get("answerable"), bool)]
    grounded = [item for item in labeled if "grounded" in item and "abstained" in item]
    if not labeled:
        summary["grounded"] = {
            "available": False, "reason": "no_answerability_labels", "n": 0,
        }
        summary["abstention"] = {
            "available": False, "reason": "no_answerability_labels", "n": 0,
        }
    elif len(grounded) != len(labeled):
        reason = "grounded_recall_not_run"
        summary["grounded"] = {"available": False, "reason": reason, "n": len(labeled)}
        summary["abstention"] = {"available": False, "reason": reason, "n": len(labeled)}
    else:
        answerable = [bool(item["answerable"]) for item in grounded]
        summary["grounded"] = {
            "available": True,
            **metrics.grounded_precision_recall_f1(
                [bool(item["grounded"]) for item in grounded], answerable,
            ),
        }
        summary["abstention"] = {
            "available": True,
            **metrics.abstention_precision_recall_f1(
                [bool(item["abstained"]) for item in grounded], answerable,
            ),
        }
    for source, target in (("grounded", "grounded_f1"), ("abstention", "abstention_f1")):
        measurement = summary[source]
        summary[target] = (
            measurement["f1"]
            if measurement["available"]
            else {"available": False, "reason": measurement["reason"], "n": measurement["n"]}
        )
    return summary


def paired_v2_bootstrap(
    candidate_records: list[dict], baseline_records: list[dict], *,
    metric: str = "recall_at_5", iterations: int = 1000,
) -> dict:
    """Compute a paired interval after requiring complete question-ID coverage.

    A partial baseline would make an apparent delta incomparable, so it is
    rejected rather than silently intersected away.  This helper lets a caller
    add a comparison after two independently written v2 runs.
    """
    def by_question_id(records: list[dict], label: str) -> dict:
        indexed = {}
        for item in records:
            if item.get("excluded"):
                continue
            question_id = item["question_id"]
            if question_id in indexed:
                raise ValueError(
                    f"paired bootstrap requires unique scored question IDs in {label}"
                )
            indexed[question_id] = item
        return indexed

    candidate = by_question_id(candidate_records, "candidate")
    baseline = by_question_id(baseline_records, "baseline")
    if set(candidate) != set(baseline):
        raise ValueError("paired bootstrap requires identical scored question IDs")
    result = paired_bootstrap_ci(
        [(float(candidate[qid].get(metric, 0.0)), float(baseline[qid].get(metric, 0.0)))
         for qid in sorted(candidate)],
        iterations=iterations,
    )
    return {"available": True, "metric": metric, **result}


def run(dataset: list[dict], *, k: int = 5, dim: int = 256,
        embedder: Optional[DeterministicEmbedder] = None,
        reranker: Optional[object] = None, grounded: bool = False,
        resolve_conflicts: bool = True, v2: bool = False,
        dataset_path: Optional[str] = None, token_budget: Optional[int] = None,
        canonical: bool = False, canonical_profile: Optional[dict] = None,
        bootstrap_iterations: int = 1000,
        baseline_label: str = "full_hybrid") -> dict:
    """Run the offline gate, or build the opt-in reproducible v2 envelope.

    The default output remains the original compact report.  ``v2=True`` is
    deliberately explicit because artifacts carry per-question measurements and
    immutable provenance rather than only the CI gate's aggregate fields.
    """
    if canonical and not v2:
        v2 = True
    if v2 and not dataset_path:
        raise ValueError("v2 output requires dataset_path so the dataset can be hashed")
    baseline = executable_baseline(baseline_label)
    configured_reranker = reranker or IdentityReranker()
    _validate_baseline_dataset(dataset, baseline, configured_reranker)
    if canonical:
        profile_errors = validate_canonical_profile(canonical_profile)
        if profile_errors:
            raise ValueError("canonical output requires pinned revisions: " + "; ".join(profile_errors))
        if canonical_profile["baseline_label"] != baseline.label:
            raise ValueError(
                "canonical_profile.baseline_label must match the executed baseline_label "
                f"({baseline.label})"
            )
        if not dataset:
            raise ValueError("canonical output requires a complete, non-empty dataset")
        if (
            not isinstance(bootstrap_iterations, int)
            or isinstance(bootstrap_iterations, bool)
            or bootstrap_iterations <= 0
        ):
            raise ValueError(
                "canonical output requires a positive bootstrap_iterations value"
            )
        reader_profile = canonical_profile["reader"]
        context_token_counter = _load_pinned_reader_token_counter(
            reader_profile["model"], reader_profile["revision"]
        )
        context_token_method = "pinned_reader_content_tokenizer"
        context_tokenizer_identity = (
            f"{reader_profile['model']}@{reader_profile['revision']}"
        )
    else:
        context_token_counter = None
        context_token_method = "deterministic_estimate"
        context_tokenizer_identity = None
    embedder = embedder or DeterministicEmbedder(dim=dim)
    per_q = []
    curve_measurements = {budget: [] for budget in CANONICAL_TOKEN_BUDGETS} if canonical else {}

    for case in dataset:
        store = Store(":memory:")
        wid = store.get_or_create_workspace("eval")
        rid = store.get_or_create_repo(wid, case.get("id", "case"))
        index = NumpyVectorIndex(store)
        engine = MemoryEngine(
            store, embedder, index,
            None if baseline.disable_reranker else configured_reranker,
        )
        if context_token_counter is not None:
            engine.recall_engine.context_packer = DeterministicContextPacker(
                token_counter=context_token_counter,
                token_counter_identity=context_tokenizer_identity,
            )
        _seed_case_graph(
            store,
            workspace_id=wid,
            repo_id=rid,
            case=case,
        )

        tag_to_id: dict[str, str] = {}
        id_to_tags: dict[str, list[str]] = {}
        id_to_text: dict[str, str] = {}
        document_record: Optional[MemoryRecord] = None
        if baseline.mode == "whole_document":
            document = str(case["document"])
            mid = engine.remember(
                document, workspace_id=wid, repo_id=rid, mtype=MemoryType.EPISODIC,
                scope=Scope.REPO, title=str(case.get("id", "whole_document")),
                resolve_conflicts=False,
            )
            document_record = store.get_memory(mid)
            if document_record is None:  # pragma: no cover - Store contract
                raise RuntimeError("whole_document ingestion did not create a memory")
            tag_to_id["whole_document"] = mid
            # A whole-document baseline injects the complete case without query
            # selection.  It therefore contains every source tag in the case,
            # not merely a synthetic document label.  Otherwise a fixture that
            # retains normal gold source IDs beside ``document`` would be
            # incorrectly reported as a retrieval failure.
            source_tags = [
                str(memory.get("tag"))
                for memory in case.get("memories", [])
                if memory.get("tag") is not None
            ]
            id_to_tags[mid] = source_tags or ["whole_document"]
            id_to_text[mid] = document
        else:
            for m in case["memories"]:
                mid = engine.remember(
                    m["text"], workspace_id=wid, repo_id=rid, mtype=MemoryType.EPISODIC,
                    scope=Scope.REPO, title=str(m.get("title", "")),
                    valid_from=m.get("valid_from"), subject_key=str(m.get("subject_key", "")),
                    claim_kind=str(m.get("claim_kind", "")),
                    resolve_conflicts=(False if baseline.disable_temporal_resolution else resolve_conflicts),
                )
                tag = m.get("tag")
                tag_to_id[tag] = mid
                id_to_tags.setdefault(mid, []).append(tag)
                id_to_text[mid] = m["text"]

        history_records = (
            store.list_memories(
                SearchFilter(workspace_id=wid, repo_id=rid, include_ancestors=True),
                include_invalid=True,
            ) if baseline.mode == "full_history" else None
        )
        if history_records is not None:
            history_records.sort(key=lambda record: (record.valid_from or record.ingested_at or 0.0, record.id))

        for question_number, q in enumerate(case["questions"]):
            question_id = str(q.get("id") or f"{case.get('id')}:{question_number}")
            started = time.perf_counter_ns()
            res = _recall_for_baseline(
                engine, q["q"], workspace_id=wid, repo_id=rid, k=k,
                token_budget=token_budget, baseline=baseline,
                source_records=(history_records if history_records is not None else
                                ([document_record] if document_record is not None else None)),
            )
            latency_ms = (time.perf_counter_ns() - started) / 1_000_000
            retrieved_ids = [c["id"] for c in res.chunks]
            retrieved_tags = [t for i in retrieved_ids for t in id_to_tags.get(i, [None])]
            retrieved_texts = [id_to_text.get(i, "") for i in retrieved_ids]
            supporting = q.get("supporting", ["whole_document"] if document_record else [])
            excluded = None
            if q.get("answerable") is False:
                excluded = exclusion(
                    str(q.get("id") or f"{case.get('id')}:{question_number}"),
                    str(q.get("exclusion_reason") or "no_gold_evidence"),
                )
            depth_metrics = metrics.retrieval_metrics_at_depths(
                retrieved_tags, supporting, depths=(1, 5, 10),
            )
            answerable = q.get("answerable")
            grounded_answer = (
                build_grounded_answer(q["q"], res, engine.embedder)
                if grounded and isinstance(answerable, bool) else None
            )
            usage = _usage_dict(
                res.usage,
                budget=(token_budget if token_budget is not None else 1500),
            )
            record = question_record(
                question_id,
                category=str(q.get("category") or "unknown"),
                retrieved_ids=[tag for tag in retrieved_tags if tag],
                supporting_ids=supporting,
                context_tokens=usage["context_tokens"],
                latency_ms=latency_ms,
                excluded=excluded,
                case=case.get("id"), q=q["q"],
                **({"answerable": answerable} if isinstance(answerable, bool) else {}),
                **({"grounded": grounded_answer.grounded,
                    "abstained": grounded_answer.abstained,
                    "grounded_support": round(grounded_answer.support, 6)}
                   if grounded_answer is not None else {}),
                recall_at_k=metrics.recall_at_k(retrieved_tags, supporting),
                hit_at_k=metrics.hit_at_k(retrieved_tags, supporting),
                mrr_at_k=metrics.mrr_at_k(retrieved_tags, supporting, k),
                ndcg_at_k=metrics.ndcg_at_k(retrieved_tags, supporting, k),
                answer_token_recall=metrics.answer_token_recall(
                    retrieved_texts, q.get("answer", q.get("evidence", "")),
                ),
                usage=usage,
                **depth_metrics,
            )
            # Public artifacts omit prompt-derived identifiers entirely.  An
            # unsalted question hash still permits offline membership testing
            # against a private or proprietary prompt corpus.
            record["context_token_method"] = context_token_method
            if context_tokenizer_identity is not None:
                record["context_tokenizer_identity"] = context_tokenizer_identity
            per_q.append(record)
            if canonical:
                for budget in CANONICAL_TOKEN_BUDGETS:
                    budget_result = _recall_for_baseline(
                        engine, q["q"], workspace_id=wid, repo_id=rid, k=k,
                        token_budget=budget, baseline=baseline,
                        source_records=(history_records if history_records is not None else
                                        ([document_record] if document_record is not None else None)),
                    )
                    # Fixed-budget quality is defined by evidence actually admitted
                    # to the packed context.  Scoring the uncapped retrieval list
                    # would credit gold memories that the reader never received.
                    budget_ids = [chunk.id for chunk in budget_result.packed_chunks]
                    budget_tags = [
                        tag for memory_id in budget_ids for tag in id_to_tags.get(memory_id, [None])
                    ]
                    budget_depth = metrics.retrieval_metrics_at_depths(
                        budget_tags, supporting, depths=(1, 5, 10),
                    )
                    budget_usage = _usage_dict(budget_result.usage, budget=budget)
                    curve_measurements[budget].append({
                        "question_id": question_id,
                        "excluded": bool(excluded),
                        "context_tokens": budget_usage["context_tokens"],
                        "context_token_method": context_token_method,
                        "context_tokenizer_identity": context_tokenizer_identity,
                        "retrieved_ids": [tag for tag in budget_tags if tag],
                        "supporting_ids": list(supporting),
                        **budget_depth,
                    })
        store.close()

    scored = [item for item in per_q if not item.get("excluded")]
    n = max(len(scored), 1)
    report = {
        "questions": len(per_q),
        "scored_questions": len(scored),
        "exclusions": [item["excluded"] for item in per_q if item.get("excluded")],
        "recall_at_k": round(sum(x["recall_at_k"] for x in scored) / n, 4),
        "hit_at_k": round(sum(x["hit_at_k"] for x in scored) / n, 4),
        "mrr_at_k": round(sum(x["mrr_at_k"] for x in scored) / n, 4),
        "ndcg_at_k": round(sum(x["ndcg_at_k"] for x in scored) / n, 4),
        "answer_token_recall": round(sum(x["answer_token_recall"] for x in scored) / n, 4),
        "k": k,
        "baseline_label": baseline.label,
        "baseline_execution": baseline.as_dict(),
        "grounded_recall": bool(grounded),
        "detail": per_q,
    }
    if not v2:
        return report

    profile = canonical_profile if canonical else None
    config = {
        "k": int(k),
        "dim": int(dim),
        "token_budget": token_budget,
        "resolve_conflicts": bool(resolve_conflicts),
        "grounded_recall": bool(grounded),
        "bootstrap_iterations": int(bootstrap_iterations),
        "baseline_label": baseline.label,
        "baseline_execution": baseline.as_dict(),
    }
    if canonical:
        config.update(canonical_benchmark_config(
            run_label="eval.harness", baseline_label=baseline.label,
            token_budgets=CANONICAL_TOKEN_BUDGETS, profile=profile,
        ))
    public_records = []
    for record in per_q:
        public_record = dict(record)
        public_record.pop("q", None)
        public_records.append(public_record)
    v2_metrics = _v2_metrics(per_q, bootstrap_iterations=max(0, int(bootstrap_iterations)))
    if canonical:
        v2_metrics["fixed_budget_curve"] = _measured_fixed_budget_curve(curve_measurements)
    envelope = report_envelope(
        suite="engraphis-harness",
        dataset_path=dataset_path,
        config=config,
        records=public_records,
        metrics=v2_metrics,
        exclusions=report["exclusions"],
        git_commit=_git_commit(),
    )
    model = {
        "name": type(embedder).__name__,
        "model_id": getattr(embedder, "model_name", None),
        "revision": getattr(embedder, "revision", None),
        "dimension": getattr(embedder, "dim", dim),
    }
    envelope["models"] = {"embedder": {**model, "sha256": sha256_text(json.dumps(model, sort_keys=True))}}
    envelope["legacy_summary"] = {key: value for key, value in report.items() if key != "detail"}
    if canonical:
        expected_embedding = profile["embedding"]
        if model["model_id"] != expected_embedding["model"] or model["revision"] != expected_embedding["revision"]:
            raise ValueError(
                "canonical output requires an embedder whose model_name and revision match "
                "canonical_profile.embedding"
            )
        envelope["protocol"]["complete_dataset"] = True
        envelope["protocol"]["source_questions"] = len(per_q)
    return envelope


def _measured_fixed_budget_curve(measurements: dict[int, list[dict]]) -> dict:
    """Summarize actual canonical budget reruns with their per-question evidence."""
    rows = []
    for budget in CANONICAL_TOKEN_BUDGETS:
        records = sorted(measurements.get(budget, []), key=lambda item: item["question_id"])
        scored = [item for item in records if not item.get("excluded")]
        row = {
            "token_budget": budget,
            "status": "measured",
            "n_total": len(records),
            "n_scored": len(scored),
            "records": records,
        }
        row.update({field: round(_mean(scored, field), 6) for field in (
            "recall_at_1", "recall_at_5", "recall_at_10",
            "mrr_at_1", "mrr_at_5", "mrr_at_10",
            "ndcg_at_1", "ndcg_at_5", "ndcg_at_10",
        )})
        rows.append(row)
    return {"available": True, "rows": rows}


def run_baseline_matrix(
    dataset: list[dict],
    *,
    baseline_labels: tuple[str, ...] = tuple(_EXECUTABLE_BASELINES),
    **kwargs,
) -> dict[str, dict]:
    """Run an explicit, reproducible matrix of executable harness baselines.

    A canonical artifact represents one declared method, so callers must run the
    rows separately with their matching pinned profile rather than claiming a
    multi-baseline canonical report.
    """
    if kwargs.get("canonical"):
        raise ValueError("run_baseline_matrix does not emit multi-baseline canonical artifacts")
    if "baseline_label" in kwargs:
        raise ValueError("pass labels through baseline_labels, not baseline_label")
    return {
        label: run(dataset, baseline_label=label, **kwargs)
        for label in baseline_labels
    }


def _print(report: dict) -> None:
    print(f"\nEngraphis eval — {report['questions']} questions @ k={report['k']}")
    print(f"  recall@k            : {report['recall_at_k']:.3f}")
    print(f"  hit@k               : {report['hit_at_k']:.3f}")
    print(f"  answer_token_recall : {report['answer_token_recall']:.3f}\n")


def main(argv: Optional[list[str]] = None) -> None:
    ap = argparse.ArgumentParser(description="Run the Engraphis retrieval eval.")
    ap.add_argument("--dataset", default=str(Path(__file__).resolve().parent / "datasets" / "sample.jsonl"))
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--dim", type=int, default=256)
    ap.add_argument("--token-budget", type=int, default=None,
                    help="packed-context token budget (recorded in v2 artifacts)")
    ap.add_argument("--json", action="store_true", help="print full JSON report")
    ap.add_argument("--v2", action="store_true",
                    help="emit the provenance-complete engraphis-benchmark/v2 envelope")
    ap.add_argument("--artifact", default=None,
                    help="immutably write a v2 JSON artifact and SHA-256 sidecar")
    ap.add_argument("--canonical", action="store_true",
                    help="require a complete dataset and a pinned canonical profile (implies --v2)")
    ap.add_argument("--canonical-profile", default=None,
                    help="JSON file with pinned benchmark, reader, and embedding revisions")
    ap.add_argument("--baseline-label", default="full_hybrid",
                    help="executable baseline: " + ", ".join(sorted(_EXECUTABLE_BASELINES)))
    ap.add_argument("--bootstrap-iterations", type=int, default=1000,
                    help="deterministic stratified-bootstrap iterations for v2 output")
    ap.add_argument("--grounded", action="store_true",
                    help="run deterministic grounded recall for rows declaring answerable")
    args = ap.parse_args(argv)

    if args.artifact and not (args.v2 or args.canonical):
        ap.error("--artifact requires --v2 (or --canonical)")
    if args.canonical and not args.canonical_profile:
        ap.error("--canonical requires --canonical-profile with pinned revisions")
    profile = None
    if args.canonical_profile:
        try:
            profile = json.loads(Path(args.canonical_profile).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            ap.error(f"could not read --canonical-profile: {exc}")

    try:
        report = run(
            load_dataset(args.dataset), k=args.k, dim=args.dim,
            v2=args.v2 or args.canonical, dataset_path=args.dataset,
            token_budget=args.token_budget, canonical=args.canonical,
            canonical_profile=profile, bootstrap_iterations=args.bootstrap_iterations,
            baseline_label=args.baseline_label, grounded=args.grounded,
        )
        if args.artifact:
            write_canonical_artifact(report, args.artifact, canonical=args.canonical)
    except ValueError as exc:
        ap.error(str(exc))
    if args.json or args.v2 or args.canonical:
        print(json.dumps(report, indent=2))
    else:
        _print(report)


if __name__ == "__main__":
    main()
