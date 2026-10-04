"""Render an artifact-backed context and retrieval report as compact SVG.

The renderer accepts either the public offline fixture envelope or a selected
performance report.  It never runs an evaluator.  A report must identify the
artifact that supplied its numbers; values for packed-context quality are shown
only when the selected report contains the additive ``packed_quality`` section.

Examples::

    python scripts/render_benchmark_report.py \
        --report docs/benchmark-evidence/offline-fixtures-v115.json \
        --output docs/images/context-efficiency.svg \
        --png-output docs/images/context-efficiency.png

    python scripts/render_benchmark_report.py \
        --report artifacts/performance-report.json \
        --output docs/images/context-efficiency.svg
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Optional, Union
from xml.sax.saxutils import escape


WIDTH = 1103
HEIGHT = 670
BACKGROUND = "#0e1114"
GRID = "#252c36"
BAR = "#445367"
GREEN = "#00b889"
OFFLINE_FIXTURE_SCHEMA = "engraphis-public-offline-fixtures/v1"
PERFORMANCE_SCHEMA = "engraphis-performance/v1"
BENCHMARK_ENVELOPE_SCHEMA = "engraphis-benchmark/v2"
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _number(value: Any, *, default: Optional[float] = None) -> Optional[float]:
    if value is None:
        return default
    if type(value) not in {int, float} or not math.isfinite(float(value)) or value < 0:
        raise ValueError("number must be a finite non-negative value")
    return float(value)


def _integer(value: Any, *, default: Optional[int] = None) -> Optional[int]:
    if value is None:
        return default
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("count must be a finite non-negative exact integer")
    if type(value) is not int or value < 0:
        raise ValueError("count must be an exact non-negative integer")
    return value


def _tokens(value: Any) -> str:
    number = _integer(value)
    return f"{number:,}" if number is not None else "Not reported"


def _decimal(value: Any, places: int = 3) -> str:
    number = _number(value)
    return f"{number:.{places}f}" if number is not None else "Not reported"


def _percent(value: Any, places: int = 2) -> str:
    number = _number(value)
    return f"{number * 100:.{places}f}%" if number is not None else "Not reported"


def _require_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{label} must be a 64-character SHA-256")
    return value.lower()


def _check_sidecar(path: Path, artifact_sha256: str) -> None:
    """Verify an adjacent sha256sum file when an artifact supplies one."""
    sidecar = Path(f"{path}.sha256")
    if not sidecar.exists():
        return
    try:
        fields = sidecar.read_text(encoding="ascii").strip().split()
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"cannot read checksum sidecar {sidecar}: {exc}") from exc
    if len(fields) < 2 or _require_sha256(fields[0], "checksum sidecar digest") != artifact_sha256:
        raise ValueError("checksum sidecar does not match the report file")
    if Path(fields[-1]).name != path.name:
        raise ValueError("checksum sidecar names a different report file")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _validate_offline_fixture(raw: dict[str, Any]) -> None:
    if raw.get("schema") != OFFLINE_FIXTURE_SCHEMA:
        raise ValueError(f"offline fixture schema must equal {OFFLINE_FIXTURE_SCHEMA}")
    runs = raw.get("runs")
    if not isinstance(runs, list) or not runs:
        raise ValueError("offline fixture runs must be a non-empty array")
    seen: set[str] = set()
    for run in runs:
        if not isinstance(run, dict) or not isinstance(run.get("id"), str) or not run["id"]:
            raise ValueError("offline fixture runs require non-empty string IDs")
        if run["id"] in seen:
            raise ValueError("offline fixture run IDs must be unique")
        seen.add(run["id"])
        if not isinstance(run.get("result"), dict):
            raise ValueError(f"offline fixture run {run['id']} requires an object result")
        command = run.get("command")
        digest = run.get("config_digest")
        if command is not None or digest is not None:
            if not isinstance(command, str) or not command:
                raise ValueError(f"offline fixture run {run['id']} requires a command")
            if not isinstance(digest, str) or _require_sha256(digest, "config_digest") != hashlib.sha256(
                command.encode("utf-8")
            ).hexdigest():
                raise ValueError(f"offline fixture run {run['id']} has a forged config digest")
            if run.get("config_digest_method") not in (None, "sha256(UTF-8 exact command)"):
                raise ValueError(f"offline fixture run {run['id']} has an unsupported digest method")
    if not {"offline-chunking", "offline-performance"} <= seen:
        raise ValueError("offline fixture must contain chunking and performance runs")

    suite = raw.get("suite")
    if suite is None:
        return
    if not isinstance(suite, dict):
        raise ValueError("offline fixture suite must be an object")
    files = suite.get("files")
    digest = suite.get("digest")
    if not isinstance(files, dict) or not files:
        raise ValueError("offline fixture suite.files must be a non-empty object")
    for name, file_digest in files.items():
        if not isinstance(name, str) or not name:
            raise ValueError("offline fixture suite file names must be non-empty strings")
        _require_sha256(file_digest, f"offline fixture suite.files[{name!r}]")
    if digest is not None:
        expected = hashlib.sha256(_canonical_json(files).encode("utf-8")).hexdigest()
        if _require_sha256(digest, "offline fixture suite.digest") != expected:
            raise ValueError("offline fixture suite digest does not match suite.files")
    if suite.get("digest_method") not in (
        None,
        "sha256(canonical compact JSON mapping each sorted path to its file SHA-256)",
    ):
        raise ValueError("offline fixture suite has an unsupported digest method")


def _validate_envelope(raw: dict[str, Any]) -> None:
    schema = raw.get("schema")
    if isinstance(raw.get("runs"), list):
        _validate_offline_fixture(raw)
    elif schema == BENCHMARK_ENVELOPE_SCHEMA:
        # Keep the renderer dependency-light for flat rows, but reuse the
        # canonical public-envelope validator when the caller supplies one.
        from eval.benchmark import validate_report

        errors = validate_report(raw)
        if errors:
            raise ValueError("invalid benchmark report envelope: " + "; ".join(errors))
    elif schema not in (None, PERFORMANCE_SCHEMA):
        raise ValueError(f"unsupported benchmark report schema: {schema!r}")


def _source_binding(raw: dict[str, Any], report_path: Path, actual: str) -> dict[str, Any]:
    supplied_source = raw.get("source")
    if supplied_source is not None and not isinstance(supplied_source, dict):
        raise ValueError("report.source must be an object")
    source = dict(supplied_source or {})
    supplied = source.get("artifact_sha256")
    top_level = raw.get("artifact_sha256")
    if supplied is not None and top_level is not None:
        if _require_sha256(supplied, "report.source.artifact_sha256") != _require_sha256(
            top_level, "report.artifact_sha256"
        ):
            raise ValueError("report source hashes disagree")
    if supplied is None:
        supplied = top_level
    if supplied is not None and _require_sha256(supplied, "report.source.artifact_sha256") != actual:
        raise ValueError("report.source.artifact_sha256 does not match the report file SHA-256")
    source["artifact_sha256"] = actual
    _check_sidecar(report_path, actual)
    return source


def _validate_rate(value: Any, label: str) -> Optional[float]:
    number = _number(value)
    if number is not None and number > 1.0:
        raise ValueError(f"{label} must be between 0 and 1")
    return number


def _validate_counts(mapping: dict[str, Any], fields: tuple[str, ...], label: str) -> None:
    for field in fields:
        if field in mapping:
            _integer(mapping[field])


def _validate_numbers(mapping: dict[str, Any], fields: tuple[str, ...], label: str) -> None:
    for field in fields:
        if field in mapping:
            _number(mapping[field])


def _assert_derived(value: float, derived: float, label: str, *, places: int) -> None:
    tolerance = (0.5 * (10 ** -places)) + 1e-12
    if abs(value - derived) > tolerance:
        raise ValueError(f"{label} contradicts the recomputed value {derived:.{places}f}")


def _derive_chunk_reduction(chunking: dict[str, Any]) -> Optional[float]:
    provided = _number(chunking.get("context_reduction_pct"))
    whole = chunking.get("whole") if isinstance(chunking.get("whole"), dict) else {}
    chunked = chunking.get("chunked") if isinstance(chunking.get("chunked"), dict) else {}
    whole_mean = _number(whole.get("mean_context_tokens"))
    chunked_mean = _number(chunked.get("mean_context_tokens"))
    if provided is not None and provided > 100.0:
        raise ValueError("context_reduction_pct must be between 0 and 100")
    if whole_mean is None or chunked_mean is None:
        if provided is not None:
            raise ValueError("context_reduction_pct requires whole and chunked mean context counts")
        return None
    if chunked_mean > whole_mean:
        raise ValueError("chunked mean context cannot exceed whole mean context")
    derived = 0.0 if whole_mean == 0 else (whole_mean - chunked_mean) / whole_mean * 100.0
    if provided is not None:
        _assert_derived(provided, derived, "context_reduction_pct", places=1)
    return derived


def _derive_payload_savings(context: dict[str, Any]) -> tuple[Optional[int], Optional[float]]:
    full = _integer(context.get("full_serialized_payload_tokens"))
    compact = _integer(context.get("compact_serialized_payload_tokens"))
    saved = _integer(context.get("saved_serialized_payload_tokens"))
    ratio = _number(context.get("serialized_payload_savings_ratio"))
    if ratio is not None and ratio > 1.0:
        raise ValueError("serialized_payload_savings_ratio must be between 0 and 1")
    if full is None or compact is None:
        if saved is not None or ratio is not None:
            raise ValueError("payload savings require full and compact payload counts")
        return None, None
    if compact > full:
        raise ValueError("compact payload count cannot exceed full payload count")
    derived_saved = full - compact
    derived_ratio = 0.0 if full == 0 else derived_saved / full
    if saved is not None and saved != derived_saved:
        raise ValueError(f"saved_serialized_payload_tokens contradicts {derived_saved}")
    if ratio is not None:
        _assert_derived(ratio, derived_ratio, "serialized_payload_savings_ratio", places=4)
    return derived_saved, derived_ratio


def _normalize_grounded(value: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Normalize the registered grounded-eval summary and verify its counts."""
    if not value:
        return {}
    answerable = _integer(value.get("answerable", value.get("n_answerable")))
    grounded = _integer(value.get("grounded", value.get("grounded_hits")))
    off_topic = _integer(value.get("off_topic", value.get("n_unanswerable")))
    abstained = _integer(value.get("abstained", value.get("abstain_hits")))
    quarantined = _integer(value.get("quarantined", value.get("n_quarantine")))
    quarantine_hits = _integer(value.get("quarantine_hits"))
    supplied_accuracy = _validate_rate(value.get("decision_accuracy", value.get("accuracy")), "grounded.decision_accuracy")
    required = (answerable, grounded, off_topic, abstained, quarantined, quarantine_hits)
    if any(item is None for item in required):
        raise ValueError("grounded fixture requires exact answerable, grounded, off_topic, abstained, quarantined, and quarantine_hits counts")
    assert answerable is not None and grounded is not None and off_topic is not None
    assert abstained is not None and quarantined is not None and quarantine_hits is not None
    if grounded > answerable or abstained > off_topic or quarantined > off_topic or quarantine_hits > quarantined:
        raise ValueError("grounded fixture result counts exceed their registered populations")
    total = answerable + off_topic
    accuracy = 0.0 if total == 0 else (grounded + abstained) / total
    if supplied_accuracy is not None:
        _assert_derived(supplied_accuracy, accuracy, "grounded.decision_accuracy", places=3)
    return {
        "answerable": answerable,
        "grounded": grounded,
        "off_topic": off_topic,
        "abstained": abstained,
        "quarantined": quarantined,
        "quarantine_hits": quarantine_hits,
        "decision_accuracy": accuracy,
        "decision_count": total,
    }


def _validate_payload_boundary(performance: dict[str, Any]) -> None:
    if "payload_boundary" not in performance:
        return
    boundary = _require_mapping(performance["payload_boundary"], "performance.payload_boundary")
    if "transport_measured" in boundary and type(boundary["transport_measured"]) is not bool:
        raise ValueError("performance.payload_boundary.transport_measured must be a JSON boolean")


def _validate_normalized(report: dict[str, Any]) -> None:
    chunking = _require_mapping(report.get("chunking"), "report.chunking")
    performance = _require_mapping(report.get("performance"), "report.performance")
    _validate_payload_boundary(performance)
    whole = _require_mapping(chunking.get("whole"), "report.chunking.whole")
    chunked = _require_mapping(chunking.get("chunked"), "report.chunking.chunked")
    context = _require_mapping(performance.get("context"), "report.performance.context")
    _validate_counts(chunking, ("questions", "documents"), "report.chunking")
    _validate_counts(whole, ("memories", "max_stored_tokens"), "report.chunking.whole")
    _validate_counts(chunked, ("memories", "max_stored_tokens"), "report.chunking.chunked")
    _validate_numbers(
        whole,
        ("mean_context_tokens", "mean_evidence_tokens"),
        "report.chunking.whole",
    )
    _validate_numbers(
        chunked,
        ("mean_context_tokens", "mean_evidence_tokens"),
        "report.chunking.chunked",
    )
    for value in (whole.get("recall_at_k"), chunked.get("recall_at_k")):
        if value is not None:
            _validate_rate(value, "chunking recall_at_k")
    _derive_chunk_reduction(chunking)

    corpus = performance.get("corpus") if isinstance(performance.get("corpus"), dict) else {}
    run = performance.get("run") if isinstance(performance.get("run"), dict) else {}
    _validate_counts(corpus, ("dataset_cases", "memories", "questions"), "report.performance.corpus")
    _validate_counts(
        run,
        ("k", "candidate_k", "timed_recalls", "cold_timed_recalls", "warm_timed_recalls", "token_budget"),
        "report.performance.run",
    )
    _validate_counts(
        context,
        ("max_tokens", "full_serialized_payload_tokens", "compact_serialized_payload_tokens", "saved_serialized_payload_tokens"),
        "report.performance.context",
    )
    _validate_numbers(
        context,
        ("mean_tokens", "mean_source_tokens", "median_serialized_payload_savings_ratio"),
        "report.performance.context",
    )
    _derive_payload_savings(context)
    ratio = context.get("serialized_payload_savings_ratio")
    if ratio is not None:
        _validate_rate(ratio, "serialized_payload_savings_ratio")

    for name in ("quality", "packed_quality"):
        quality = performance.get(name)
        if not isinstance(quality, dict):
            continue
        for metric in ("recall_at_k", "hit_at_k", "answer_token_recall"):
            if metric in quality:
                _validate_rate(quality[metric], f"report.performance.{name}.{metric}")
        if "sample_count" in quality:
            _integer(quality["sample_count"])

    grounded = report.get("grounded")
    if grounded is not None:
        _normalize_grounded(_require_mapping(grounded, "report.grounded"))


def _normalize_chunking(value: Optional[dict[str, Any]]) -> dict[str, Any]:
    if not value:
        return {}
    reports = value.get("reports") if isinstance(value.get("reports"), dict) else value
    whole = reports.get("whole") if isinstance(reports, dict) else None
    chunked = reports.get("chunked") if isinstance(reports, dict) else None
    return {
        "whole": whole if isinstance(whole, dict) else {},
        "chunked": chunked if isinstance(chunked, dict) else {},
        "questions": value.get("questions"),
        "documents": value.get("documents"),
        "context_reduction_pct": value.get("context_reduction_pct"),
    }


def _normalize_performance(value: dict[str, Any]) -> dict[str, Any]:
    """Accept both the nested performance report and the flattened v9 registry row."""
    _validate_payload_boundary(value)
    if isinstance(value.get("context"), dict):
        return value
    context = {
        "mean_tokens": value.get("mean_context_tokens"),
        "max_tokens": value.get("max_context_tokens"),
        "full_serialized_payload_tokens": value.get(
            "full_serialized_payload_tokens"
        ),
        "compact_serialized_payload_tokens": value.get(
            "compact_serialized_payload_tokens"
        ),
        "saved_serialized_payload_tokens": value.get(
            "saved_serialized_payload_tokens"
        ),
        "serialized_payload_savings_ratio": value.get(
            "serialized_payload_savings_ratio"
        ),
        "token_counter": value.get("token_counter", "engraphis.regex.v1"),
    }
    quality_keys = ("recall_at_k", "hit_at_k", "answer_token_recall")
    quality = {key: value[key] for key in quality_keys if key in value}
    packed_quality = value.get("packed_quality")
    if not isinstance(packed_quality, dict):
        packed_quality = (
            {
                "recall_at_k": value["packed_recall_at_k"],
                "hit_at_k": value["packed_hit_at_k"],
                "answer_token_recall": value["packed_answer_token_recall"],
                "sample_count": value.get(
                    "packed_sample_count", value.get("questions", 0)
                ),
            }
            if all(key in value for key in (
                "packed_recall_at_k", "packed_hit_at_k", "packed_answer_token_recall",
            ))
            else {}
        )
    return {
        **value,
        "context": context,
        "quality": quality,
        "packed_quality": packed_quality,
        "corpus": {
            "dataset_cases": value.get("dataset_cases"),
            "memories": value.get("memories"),
            "questions": value.get("questions"),
        },
        "run": {
            "k": value.get("k"),
            "timed_recalls": value.get("timed_recalls"),
            "token_budget": value.get("token_budget"),
        },
        "payload_boundary": {
            "kind": "serialized_json_shape_proxy",
            "transport_measured": False,
            "mcp_envelope_serialized": False,
            "token_counter": value.get("token_counter", "engraphis.regex.v1"),
        },
    }


def load_report(path: Union[str, Path]) -> dict[str, Any]:
    """Load and normalize a report without executing benchmark code."""
    return load_report_snapshot(path)[0]


def load_report_snapshot(path: Union[str, Path]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return normalized and raw views bound to the same artifact bytes."""
    report_path = Path(path)
    try:
        payload = report_path.read_bytes()
        raw = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read benchmark report {report_path}: {exc}") from exc
    raw = _require_mapping(raw, "report")
    _validate_envelope(raw)
    source = _source_binding(raw, report_path, hashlib.sha256(payload).hexdigest())

    if isinstance(raw.get("runs"), list):
        runs = {
            item.get("id"): item.get("result")
            for item in raw["runs"]
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        performance = runs.get("offline-performance")
        chunking = runs.get("offline-chunking")
        if not isinstance(performance, dict):
            raise ValueError("fixture report is missing offline-performance")
        normalized = {
            "schema": raw.get("schema"),
            "source": source,
            "registered_fixture_count": len(raw["runs"]),
            "chunking": _normalize_chunking(chunking),
            "performance": _normalize_performance(performance),
            "grounded": _normalize_grounded(
                runs.get("offline-grounded") if isinstance(runs.get("offline-grounded"), dict) else None
            ),
        }
        _validate_normalized(normalized)
        return normalized, raw

    performance = raw.get("performance", raw)
    performance = _normalize_performance(_require_mapping(performance, "performance"))
    chunking = raw.get("chunking")
    normalized = {
        "schema": raw.get("schema"),
        "source": source,
        "registered_fixture_count": None,
        "chunking": _normalize_chunking(chunking if isinstance(chunking, dict) else None),
        "performance": performance,
        "grounded": {},
    }
    _validate_normalized(normalized)
    return normalized, raw


def _text(
    x: int,
    y: int,
    value: Any,
    *,
    size: float = 14.3,
    class_name: str = "",
    anchor: str = "start",
) -> str:
    classes = f' class="{class_name}"' if class_name else ""
    return (
        f'<text x="{x}" y="{y}" font-size="{size:g}"{classes} '
        f'text-anchor="{anchor}">{escape(str(value))}</text>'
    )


def _bar(
    value: Optional[float],
    maximum: Optional[float],
    *,
    x: int,
    y: int,
    width: int = 424,
    color: str = GREEN,
) -> str:
    track_width = width
    value_width = 0.0
    if value is not None and maximum is not None and maximum > 0:
        value_width = max(0.0, min(float(track_width), float(track_width) * value / maximum))
    return (
        f'<rect x="{x}" y="{y}" width="{track_width}" height="8" fill="#1d2530"/>'
        f'<rect x="{x}" y="{y}" width="{value_width:.2f}" height="8" fill="{color}"/>'
    )


def _quality(report: dict[str, Any], name: str) -> dict[str, Any]:
    value = report.get(name)
    return value if isinstance(value, dict) else {}


def render_report(report: dict[str, Any]) -> str:
    """Render a normalized report to deterministic SVG text."""
    report = _require_mapping(report, "report")
    source = _require_mapping(report.get("source"), "report.source")
    source_hash = _require_sha256(source.get("artifact_sha256"), "report.source.artifact_sha256")

    chunking = _require_mapping(report.get("chunking"), "report.chunking")
    performance = _require_mapping(report.get("performance"), "report.performance")
    _validate_normalized(report)
    whole = _require_mapping(chunking.get("whole"), "report.chunking.whole")
    chunked = _require_mapping(chunking.get("chunked"), "report.chunking.chunked")
    context = _require_mapping(performance.get("context"), "report.performance.context")
    retrieved = _quality(performance, "quality")
    packed = _quality(performance, "packed_quality")
    packed_available = bool(packed) and packed.get("sample_count", 1) != 0
    grounded = _normalize_grounded(report.get("grounded"))
    corpus = performance.get("corpus") if isinstance(performance.get("corpus"), dict) else {}
    run = performance.get("run") if isinstance(performance.get("run"), dict) else {}
    payload_boundary = performance.get("payload_boundary")
    if not isinstance(payload_boundary, dict):
        payload_boundary = {"transport_measured": False}
    transport_measured = payload_boundary.get("transport_measured", False) is True
    transport_label = "MCP transport measured" if transport_measured else "MCP transport not measured"
    payload_scope_label = "JSON proxy plus transport" if transport_measured else "JSON proxy only"
    transport_description = "MCP transport was measured" if transport_measured else "MCP transport was not measured"
    whole_context = _number(whole.get("mean_context_tokens"))
    chunked_context = _number(chunked.get("mean_context_tokens"))
    maximum_context = max(value for value in (whole_context, chunked_context, 1.0) if value is not None)
    full_proxy = _integer(context.get("full_serialized_payload_tokens"))
    compact_proxy = _integer(context.get("compact_serialized_payload_tokens"))
    maximum_proxy = max(value for value in (full_proxy, compact_proxy, 1.0) if value is not None)
    questions = _integer(corpus.get("questions"))
    timed_recalls = _integer(run.get("timed_recalls"))
    chunk_questions = _integer(chunking.get("questions"))
    chunk_documents = _integer(chunking.get("documents"))
    chunk_reduction = _derive_chunk_reduction(chunking)
    _, payload_savings_ratio = _derive_payload_savings(context)
    whole_recall = whole.get("recall_at_k")
    chunked_recall = chunked.get("recall_at_k")
    recall_label = (
        f"Recall@5 {_decimal(whole_recall)} both modes"
        if whole_recall is not None and whole_recall == chunked_recall
        else f"Recall@5 whole/chunked {_decimal(whole_recall)}/{_decimal(chunked_recall)}"
    )
    chunk_population = (
        f"{_tokens(chunk_documents)} documents · " if chunk_documents is not None else ""
    )
    fixture_count = report.get("registered_fixture_count")
    if fixture_count is not None:
        fixture_count = _integer(fixture_count)
    fixture_label = (
        f"{fixture_count} REGISTERED OFFLINE FIXTURES"
        if fixture_count is not None else "SELECTED REPORT"
    )
    packed_quality_desc = (
        f"Packed context quality is Recall@5 {_decimal(packed.get('recall_at_k'))}, "
        f"Hit@5 {_decimal(packed.get('hit_at_k'))}, and answer-token recall "
        f"{_decimal(packed.get('answer_token_recall'))} across "
        f"{_tokens(packed.get('sample_count'))} questions."
        if packed_available else "Packed-context quality is not included in this report."
    )
    grounded_desc = (
        f"Grounded checks score {grounded['grounded']} of {grounded['answerable']} "
        f"answerable queries grounded and {grounded['abstained']} of {grounded['off_topic']} "
        f"abstention queries rejected, including {grounded['quarantine_hits']} of "
        f"{grounded['quarantined']} quarantined-evidence checks. "
        f"Decision accuracy is {_decimal(grounded['decision_accuracy'])} across "
        f"{grounded['decision_count']} decisions."
        if grounded else "Grounded checks are not included in this selected report."
    )
    desc = " ".join((
        "Artifact-driven offline benchmark report with context efficiency, retrieval quality, and grounded behavior reported separately.",
        f"Structure-aware chunking reports {_decimal(whole_context, 1)} to {_decimal(chunked_context, 1)} retrieved tokens per question.",
        f"The JSON-shape payload proxy reports {_tokens(full_proxy)} full versus {_tokens(compact_proxy)} compact tokens; {transport_description}; provider billing was not measured.",
        f"Retrieved candidate quality is Recall@5 {_decimal(retrieved.get('recall_at_k'))}, Hit@5 {_decimal(retrieved.get('hit_at_k'))}, and answer-token recall {_decimal(retrieved.get('answer_token_recall'))} across {_tokens(questions)} questions.",
        packed_quality_desc,
        grounded_desc,
        f"Source artifact SHA-256 {source_hash}.",
    ))
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" '
        f'viewBox="0 0 {WIDTH} {HEIGHT}" role="img" aria-labelledby="title desc">',
        '<title id="title">Offline context, retrieval, and grounded benchmark results</title>',
        f'<desc id="desc">{escape(desc)}</desc>',
        '<style>text{font-family:Consolas,monospace;fill:#b9c8dc} '
        '.heading{font-family:Segoe UI,sans-serif;font-weight:700;fill:#f2f5f9} '
        '.green{fill:#00c896}.muted{fill:#7589a7}</style>',
        f'<rect x="3" y="2" width="1094" height="664" fill="{BACKGROUND}" stroke="{GRID}"/>',
        '<rect x="22" y="20" width="224" height="21" fill="#1c222b" stroke="#303a47"/>',
        _text(31, 35, "REGISTERED OFFLINE EVIDENCE", size=12.5),
        _text(23, 75, "Offline benchmark results", size=32, class_name="heading"),
        _text(1080, 36, fixture_label, size=13.1, class_name="muted", anchor="end"),
        _text(23, 99, "Context efficiency, retrieval quality, and grounded checks are reported separately.", size=13.2, class_name="muted"),
        '<rect x="4" y="112" width="1092" height="27" fill="#141920" stroke="#252c36"/>',
        _text(19, 131, "01  CONTEXT EFFICIENCY", size=12.5, class_name="green"),
        _text(1080, 131, "CHUNKING + SERIALIZED JSON-SHAPE PROXY", size=12.5, class_name="muted", anchor="end"),
        f'<rect x="4" y="139" width="1092" height="166" fill="{BACKGROUND}" stroke="{GRID}"/>',
        '<rect x="550" y="139" width="1" height="166" fill="#252c36"/>',
    ]
    lines.extend([
        _text(19, 165, "Retrieved context after chunking", size=17, class_name="heading"),
        _text(19, 186, f"offline-chunking · {chunk_population}{_tokens(chunk_questions)} questions · {recall_label}", size=12.2, class_name="muted"),
        _text(19, 215, f"{_percent((chunk_reduction or 0) / 100, places=1)} fewer retrieved tokens per question" if chunk_reduction is not None else "Retrieved context per question", size=17, class_name="green"),
        _text(20, 245, "Whole", size=12.5),
        _bar(whole_context, maximum_context, x=85, y=237, width=330),
        _text(525, 245, f"{_decimal(whole_context, 1)} tokens", size=12.5, anchor="end"),
        _text(20, 278, "Chunked", size=12.5, class_name="green"),
        _bar(chunked_context, maximum_context, x=85, y=270, width=330),
        _text(525, 278, f"{_decimal(chunked_context, 1)} tokens", size=12.5, class_name="green", anchor="end"),
        _text(568, 165, "Serialized JSON-shape payload proxy", size=17, class_name="heading"),
        _text(568, 186, f"offline-performance · {_tokens(questions)} payload samples · {_tokens(timed_recalls)} timed recalls", size=12.2, class_name="muted"),
        _text(568, 215, f"{_percent(payload_savings_ratio)} fewer proxy tokens", size=17, class_name="green"),
        _text(570, 245, "Full", size=12.5),
        _bar(full_proxy, maximum_proxy, x=625, y=237, width=335),
        _text(1078, 245, f"{_tokens(full_proxy)} tokens", size=12.5, anchor="end"),
        _text(570, 278, "Compact", size=12.5, class_name="green"),
        _bar(compact_proxy, maximum_proxy, x=625, y=270, width=335),
        _text(1078, 278, f"{_tokens(compact_proxy)} tokens", size=12.5, class_name="green", anchor="end"),
        '<rect x="4" y="316" width="1092" height="27" fill="#141920" stroke="#252c36"/>',
        _text(19, 335, "02  RETRIEVAL QUALITY", size=12.5, class_name="green"),
        _text(1080, 335, "CANDIDATES BEFORE PACKING VS READER-ADMITTED CONTEXT", size=12.5, class_name="muted", anchor="end"),
        f'<rect x="4" y="343" width="1092" height="136" fill="{BACKGROUND}" stroke="{GRID}"/>',
        '<rect x="550" y="343" width="1" height="136" fill="#252c36"/>',
        _text(22, 370, "Retrieved candidate quality", size=16.5, class_name="heading"),
        _text(22, 390, f"offline-performance · before packing · n={_tokens(questions)}", size=12.2, class_name="muted"),
        _text(22, 416, "Recall@5", size=12.5),
        _text(194, 416, "Hit@5", size=12.5),
        _text(363, 416, "Answer-token recall", size=12.5),
        _text(22, 451, _decimal(retrieved.get("recall_at_k")), size=24, class_name="heading"),
        _text(194, 451, _decimal(retrieved.get("hit_at_k")), size=24, class_name="heading"),
        _text(363, 451, _decimal(retrieved.get("answer_token_recall")), size=24, class_name="heading"),
        _text(571, 370, "Packed context quality", size=16.5, class_name="green"),
        _text(571, 390, f"reader-admitted context · n={_tokens(packed.get('sample_count'))}" if packed_available else "Not included in selected report", size=12.2, class_name="muted"),
        _text(571, 416, "Recall@5", size=12.5),
        _text(743, 416, "Hit@5", size=12.5),
        _text(912, 416, "Answer-token recall", size=12.5),
        _text(571, 451, _decimal(packed.get("recall_at_k")), size=24, class_name="green"),
        _text(743, 451, _decimal(packed.get("hit_at_k")), size=24, class_name="green"),
        _text(912, 451, _decimal(packed.get("answer_token_recall")), size=24, class_name="green"),
    ])

    if grounded:
        lines.extend([
            '<rect x="4" y="489" width="1092" height="27" fill="#141920" stroke="#252c36"/>',
            _text(19, 508, "03  GROUNDED CHECKS", size=12.5, class_name="green"),
            _text(1080, 508, "OFFLINE DETERMINISTIC FIXTURE", size=12.5, class_name="muted", anchor="end"),
            f'<rect x="4" y="516" width="1092" height="109" fill="{BACKGROUND}" stroke="{GRID}"/>',
            '<rect x="367" y="516" width="1" height="77" fill="#252c36"/>',
            '<rect x="732" y="516" width="1" height="77" fill="#252c36"/>',
            _text(22, 541, "Answerable queries", size=13, class_name="heading"),
            _text(22, 575, f"{grounded['grounded']} / {grounded['answerable']} grounded", size=21, class_name="green"),
            _text(386, 541, "Abstention queries", size=13, class_name="heading"),
            _text(386, 575, f"{grounded['abstained']} / {grounded['off_topic']} rejected", size=21, class_name="green"),
            _text(750, 541, "Quarantined-evidence probe", size=13, class_name="heading"),
            _text(750, 575, f"{grounded['quarantine_hits']} / {grounded['quarantined']} abstained", size=21, class_name="green"),
            _text(22, 610, f"Decision accuracy {_decimal(grounded['decision_accuracy'])} ({grounded['grounded'] + grounded['abstained']} / {grounded['decision_count']}); the quarantine probe is included in the abstention total.", size=12.1, class_name="muted"),
        ])
    lines.extend([
        _text(22, 651, f"SOURCE SHA-256  {source_hash[:12]}", size=12.2, class_name="muted"),
        _text(1080, 651, f"{payload_scope_label}; {transport_label}; provider billing not measured.", size=12.2, class_name="muted", anchor="end"),
        "</svg>",
    ])
    return "\n".join(lines) + "\n"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Render an artifact-backed benchmark report as SVG and optional PNG.")
    parser.add_argument("--report", required=True, help="JSON artifact or selected performance report")
    parser.add_argument("--output", required=True, help="destination SVG path")
    parser.add_argument("--png-output", help="optional PNG export path (requires CairoSVG)")
    args = parser.parse_args(argv)
    report = load_report(args.report)
    svg = render_report(report)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(svg)
    if args.png_output:
        try:
            import cairosvg
        except ImportError as exc:
            raise SystemExit("--png-output requires CairoSVG; install it with `pip install cairosvg`") from exc
        png_output = Path(args.png_output)
        png_output.parent.mkdir(parents=True, exist_ok=True)
        cairosvg.svg2png(
            bytestring=svg.encode("utf-8"),
            write_to=str(png_output),
            output_width=WIDTH,
            output_height=HEIGHT,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
