"""Render an artifact-backed context and retrieval report as compact SVG.

The renderer accepts either the public offline fixture envelope or a selected
performance report.  It never runs an evaluator.  A report must identify the
artifact that supplied its numbers; values for packed-context quality are shown
only when the selected report contains the additive ``packed_quality`` section.

Examples::

    python scripts/render_benchmark_report.py \
        --report docs/benchmark-evidence/offline-fixtures-v9.json \
        --output docs/images/context-efficiency.svg

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
HEIGHT = 956
BACKGROUND = "#0e1114"
PANEL = "#141920"
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
    return f"{number:,}" if number is not None else "pending"


def _decimal(value: Any, places: int = 3) -> str:
    number = _number(value)
    return f"{number:.{places}f}" if number is not None else "pending"


def _percent(value: Any, places: int = 2) -> str:
    number = _number(value)
    return f"{number * 100:.{places}f}%" if number is not None else "pending"


def _artifact_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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


def _source_binding(raw: dict[str, Any], report_path: Path) -> dict[str, Any]:
    actual = _artifact_hash(report_path)
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


def _validate_normalized(report: dict[str, Any]) -> None:
    chunking = _require_mapping(report.get("chunking"), "report.chunking")
    performance = _require_mapping(report.get("performance"), "report.performance")
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
        "context_reduction_pct": value.get("context_reduction_pct"),
    }


def _normalize_performance(value: dict[str, Any]) -> dict[str, Any]:
    """Accept both the nested performance report and the flattened v9 registry row."""
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
    report_path = Path(path)
    try:
        raw = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read benchmark report {report_path}: {exc}") from exc
    raw = _require_mapping(raw, "report")
    _validate_envelope(raw)
    source = _source_binding(raw, report_path)

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
            "chunking": _normalize_chunking(chunking),
            "performance": _normalize_performance(performance),
        }
        _validate_normalized(normalized)
        return normalized

    performance = raw.get("performance", raw)
    performance = _normalize_performance(_require_mapping(performance, "performance"))
    chunking = raw.get("chunking")
    normalized = {
        "schema": raw.get("schema"),
        "source": source,
        "chunking": _normalize_chunking(chunking if isinstance(chunking, dict) else None),
        "performance": performance,
    }
    _validate_normalized(normalized)
    return normalized


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


def _panel(y: int, height: int) -> list[str]:
    return [
        f'<rect x="4" y="{y}" width="1092" height="{height}" fill="{BACKGROUND}" stroke="{GRID}"/>',
        f'<rect x="368" y="{y}" width="1" height="{height}" fill="{GRID}"/>',
        f'<rect x="823" y="{y}" width="1" height="{height}" fill="{GRID}"/>',
    ]


def _bar(value: Optional[float], maximum: Optional[float], *, x: int, y: int) -> str:
    width = 0.0
    if value is not None and maximum is not None and maximum > 0:
        width = max(0.0, min(424.0, 424.0 * value / maximum))
    return (
        f'<rect x="{x}" y="{y}" width="424" height="8" fill="#1d2530"/>'
        f'<rect x="{x}" y="{y}" width="{width:.2f}" height="8" fill="{GREEN}"/>'
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
    corpus = performance.get("corpus") if isinstance(performance.get("corpus"), dict) else {}
    run = performance.get("run") if isinstance(performance.get("run"), dict) else {}
    payload_boundary = performance.get("payload_boundary")
    if not isinstance(payload_boundary, dict):
        payload_boundary = {
            "kind": "serialized_json_shape_proxy",
            "transport_measured": False,
            "mcp_envelope_serialized": False,
        }
    transport_measured = bool(payload_boundary.get("transport_measured"))
    transport_label = (
        "MCP transport measured"
        if transport_measured
        else "MCP transport not measured"
    )
    payload_scope_label = (
        "JSON proxy plus transport"
        if transport_measured
        else "JSON proxy only"
    )
    transport_description = (
        "MCP transport is measured separately"
        if transport_measured
        else "the payload is not an MCP transport measurement"
    )

    whole_context = _number(whole.get("mean_context_tokens"))
    chunked_context = _number(chunked.get("mean_context_tokens"))
    maximum_context = max(value for value in (whole_context, chunked_context, 1.0) if value is not None)
    full_proxy = _integer(context.get("full_serialized_payload_tokens"))
    compact_proxy = _integer(context.get("compact_serialized_payload_tokens"))
    maximum_proxy = max(value for value in (full_proxy, compact_proxy, 1.0) if value is not None)
    questions = _integer(corpus.get("questions"))
    timed_recalls = _integer(run.get("timed_recalls"))
    budget = _integer(run.get("token_budget"))
    mean_context = context.get("mean_tokens")
    max_context = context.get("max_tokens")
    chunk_questions = _integer(chunking.get("questions"))
    chunk_reduction = _derive_chunk_reduction(chunking)
    _, payload_savings_ratio = _derive_payload_savings(context)

    desc = (
        "Artifact-driven local deterministic benchmark report. "
        f"Structure-aware chunks report {_decimal(whole_context, 1)} to "
        f"{_decimal(chunked_context, 1)} retrieved tokens per question. "
        f"The performance run reports {_tokens(full_proxy)} full-proxy versus "
        f"{_tokens(compact_proxy)} compact-proxy tokens. "
        "Retrieved-candidate quality and packed-context quality are separate views; "
        "packed quality is shown only when the selected report includes it. "
        "Payload counts are a serialized JSON-shape proxy; "
        f"{transport_description}. The report does not measure provider billing. "
        f"Packed context reports {_decimal(mean_context, 2)} mean and "
        f"{_tokens(max_context)} max under a {_tokens(budget)}-token cap. "
        f"Source artifact SHA-256 {source_hash}."
    )
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" '
        'viewBox="0 0 1103 956" role="img" aria-labelledby="title desc">',
        '<title id="title">Measured context and retrieval boundaries</title>',
        f'<desc id="desc">{escape(desc)}</desc>',
        '<style>text{font-family:Consolas,monospace;fill:#b9c8dc} '
        '.heading{font-family:Segoe UI,sans-serif;font-weight:700;fill:#f2f5f9} '
        '.green{fill:#00c896}.muted{fill:#7589a7}</style>',
        f'<rect x="3" y="2" width="1094" height="949" fill="{BACKGROUND}" stroke="{GRID}"/>',
        '<rect x="23" y="27" width="176" height="20" fill="#1c222b" stroke="#303a47"/>',
        _text(32, 41, "BENCHMARK FIXTURE REPORT", size=12.5),
        _text(225, 48, "Measured context and retrieval boundaries", size=33, class_name="heading"),
        _text(23, 75, "Registered local fixtures with explicit candidate, packed, and payload scopes.", size=13.2, class_name="muted"),
        '<rect x="4" y="90" width="1092" height="28" fill="#141920" stroke="#252c36"/>',
        _text(19, 109, "01 CONTEXT BOUNDARIES", size=12.5, class_name="green"),
        _text(1080, 109, "2 REGISTERED FIXTURES", size=12.5, class_name="muted", anchor="end"),
    ]

    lines.extend(_panel(118, 108))
    lines.append(
        f'<g aria-label="Structure-aware chunking; {_decimal(whole_context, 1)} to '
        f'{_decimal(chunked_context, 1)} tokens">'
    )
    lines.extend([
        _text(19, 149, "Retrieved context per question", size=17.4, class_name="heading"),
        _text(19, 170, f"{_tokens(chunk_questions)} questions / Recall@5 is fixture-bound", size=12.5, class_name="muted"),
        _text(19, 187, f"Smallest evidence: {_decimal(whole.get('mean_evidence_tokens'), 1)} to {_decimal(chunked.get('mean_evidence_tokens'), 1)} tokens", size=12.5, class_name="muted"),
        _text(19, 204, "Stored-memory content before context packing", size=12.5, class_name="muted"),
        _text(384, 146, "Whole documents", size=14.3),
        _text(808, 146, f"{_decimal(whole_context, 1)} tokens", size=14.3, anchor="end"),
        _bar(whole_context, maximum_context, x=384, y=154),
        _text(384, 182, "Structure-aware chunks", size=14.3, class_name="green"),
        _text(808, 182, f"{_decimal(chunked_context, 1)} tokens", size=14.3, class_name="green", anchor="end"),
        _bar(chunked_context, maximum_context, x=384, y=190),
        _text(1080, 161, "OUTCOME DIFFERENTIAL", size=12.5, class_name="muted", anchor="end"),
        _text(1080, 189, f"{_percent((chunk_reduction or 0) / 100, places=1)} lower" if chunk_reduction is not None else "pending", size=24, class_name="green", anchor="end"),
        '</g>',
    ])

    lines.extend(_panel(226, 93))
    lines.append(
        f'<g aria-label="Serialized JSON-shape payload proxy; {_tokens(full_proxy)} full; '
        f'{_tokens(compact_proxy)} compact">'
    )
    lines.extend([
        _text(19, 257, "Serialized recall payload proxy", size=17.4, class_name="heading"),
        _text(19, 278, f"{_tokens(questions)} payload samples / {_tokens(timed_recalls)} timed recalls", size=12.5, class_name="muted"),
        _text(384, 254, "Full JSON-shape proxy", size=14.3),
        _text(808, 254, f"{_tokens(full_proxy)} tokens", size=14.3, anchor="end"),
        _bar(full_proxy, maximum_proxy, x=384, y=262),
        _text(384, 290, "Compact JSON-shape proxy", size=14.3, class_name="green"),
        _text(808, 290, f"{_tokens(compact_proxy)} tokens", size=14.3, class_name="green", anchor="end"),
        _bar(compact_proxy, maximum_proxy, x=384, y=298),
        _text(1080, 269, "PROXY DIFFERENTIAL", size=12.5, class_name="muted", anchor="end"),
        _text(1080, 297, f"{_percent(payload_savings_ratio)} lower", size=24, class_name="green", anchor="end"),
        '</g>',
    ])

    lines.extend([
        '<rect x="4" y="319" width="1092" height="28" fill="#141920" stroke="#252c36"/>',
        _text(19, 338, "02 QUALITY SCOPES", size=12.5, class_name="green"),
        _text(1080, 338, "CANDIDATE VS PACKED", size=12.5, class_name="muted", anchor="end"),
    ])
    lines.extend(_panel(347, 94))
    packed_label = (
        f"Recall@5 {_decimal(packed.get('recall_at_k'))} / "
        f"hit@5 {_decimal(packed.get('hit_at_k'))} / "
        f"answer tokens {_decimal(packed.get('answer_token_recall'))}"
        if packed_available else "Pending selected report with packed_quality"
    )
    lines.append(
        '<g aria-label="Retrieved candidate quality and packed context quality">'
    )
    lines.extend([
        _text(19, 378, "Retrieved candidate quality", size=17.4, class_name="heading"),
        _text(19, 399, "Legacy fields score all candidate chunks before packing", size=12.5, class_name="muted"),
        _text(384, 375, "Retrieved candidates", size=14.3),
        _text(808, 375, f"Recall@5 {_decimal(retrieved.get('recall_at_k'))}", size=14.3, anchor="end"),
        _text(384, 403, f"hit@5 {_decimal(retrieved.get('hit_at_k'))} / answer tokens {_decimal(retrieved.get('answer_token_recall'))}", size=14.3),
        _text(384, 431, "Packed context", size=14.3, class_name="green"),
        _text(808, 431, packed_label, size=12.5, class_name="green", anchor="end"),
        '</g>',
    ])

    lines.extend(_panel(441, 94))
    lines.extend([
        _text(19, 472, "Packed prompt-context usage", size=17.4, class_name="heading"),
        _text(19, 493, "Context usage is separate from payload serialization", size=12.5, class_name="muted"),
        _text(19, 510, f"Mean {_decimal(mean_context, 2)} / max {_tokens(max_context)} tokens", size=12.5, class_name="muted"),
        _text(384, 469, "Configured hard budget", size=14.3),
        _text(808, 469, f"{_tokens(budget)} tokens", size=14.3, anchor="end"),
        _bar(_number(max_context), _number(budget) or 1.0, x=384, y=477),
        _text(384, 505, "Transport boundary", size=14.3, class_name="green"),
        _text(808, 505, transport_label, size=14.3, class_name="green", anchor="end"),
        _text(1080, 484, "SCOPE", size=12.5, class_name="muted", anchor="end"),
        _text(1080, 512, payload_scope_label, size=24, class_name="green", anchor="end"),
    ])

    lines.extend([
        '<rect x="4" y="535" width="1092" height="28" fill="#141920" stroke="#252c36"/>',
        _text(19, 554, "03 PENDING EVALUATION TRACKS", size=12.5, class_name="green"),
        _text(1080, 554, "NO UNREGISTERED SCORES", size=12.5, class_name="muted", anchor="end"),
    ])
    for y, title, detail in (
        (566, "Coding outcomes", "Project-authored tasks, paired graders, and held-out corrections"),
        (616, "External datasets", "Pinned LoCoMo and LongMemEval artifacts with answer evaluators"),
        (666, "Operational capacity", "Frozen 1/4/16 process matrix at staged memory counts"),
    ):
        lines.extend(_panel(y, 50))
        lines.extend([
            _text(19, y + 22, title, size=14.3, class_name="heading"),
            _text(384, y + 22, detail, size=13.2),
            _text(1080, y + 22, "PENDING", size=13.2, class_name="muted", anchor="end"),
        ])

    lines.extend([
        '<rect x="23" y="864" width="263" height="82" fill="#0e1114" stroke="#252c36"/>',
        _text(35, 885, "RETRIEVED QUALITY", size=12.5, class_name="muted"),
        _text(35, 913, f"{_decimal(retrieved.get('recall_at_k'))} Recall@5", size=20, class_name="heading"),
        _text(35, 935, "candidate page metric", size=12.5, class_name="muted"),
        '<rect x="287" y="864" width="263" height="82" fill="#0e1114" stroke="#252c36"/>',
        _text(299, 885, "PACKED QUALITY", size=12.5, class_name="muted"),
        _text(299, 913, f"{_decimal(packed.get('recall_at_k'))} Recall@5" if packed_available else "PENDING", size=20, class_name="heading"),
        _text(299, 935, "reader-admitted context", size=12.5, class_name="muted"),
        '<rect x="550" y="864" width="263" height="82" fill="#0e1114" stroke="#252c36"/>',
        _text(562, 885, "PAYLOAD BOUNDARY", size=12.5, class_name="muted"),
        _text(562, 913, payload_scope_label, size=20, class_name="green"),
        _text(562, 935, transport_label.lower(), size=12.5, class_name="muted"),
        '<rect x="814" y="864" width="263" height="82" fill="#0e1114" stroke="#252c36"/>',
        _text(826, 885, "SOURCE ARTIFACT", size=12.5, class_name="muted"),
        _text(826, 913, source_hash[:12], size=20, class_name="heading"),
        _text(826, 935, "SHA-256 prefix", size=12.5, class_name="muted"),
        _text(1080, 70, "Artifact-driven local measurements", size=18.7, class_name="muted", anchor="end"),
        "</svg>",
    ])
    return "\n".join(lines) + "\n"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Render an artifact-backed benchmark report as SVG.")
    parser.add_argument("--report", required=True, help="JSON artifact or selected performance report")
    parser.add_argument("--output", required=True, help="destination SVG path")
    args = parser.parse_args(argv)
    report = load_report(args.report)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(render_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
