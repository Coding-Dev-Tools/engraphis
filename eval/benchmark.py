"""Small, dependency-free primitives for reproducible public benchmark reports.

This module intentionally owns only evaluation bookkeeping.  It does not call a
model, download a dataset, or import a production backend, which keeps the
offline CI path reproducible.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import platform
import random
import re
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Protocol, Sequence, Union
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from engraphis.core.textutil import estimate_tokens
from eval import metrics as retrieval_metrics


SCHEMA = "engraphis-benchmark/v2"
CANONICAL_TOKEN_BUDGETS = (256, 512, 1024, 2048, 4096)
CANONICAL_BASELINE_LABELS = (
    "no_retrieval",
    "lexical_only",
    "dense_only",
    "dense_lexical_rrf",
    "full_hybrid",
    "full_history",
    "no_graph",
    "no_reranker",
    "no_temporal_resolution",
    "whole_document",
)
# Names come from the official harness. Revisions are immutable upstream commits
# resolved from the official GitHub/Hugging Face repositories on 2026-07-29.
LONGMEMEVAL_V2_CANONICAL_PROFILE_TEMPLATE = {
    "benchmark": {
        "repository": "xiaowu0162/LongMemEval-V2",
        "repository_revision": "6f020ac2fc3275e46c706d3406e02c3ed79b7be2",
        "dataset_revision": "f152293e235517d504809563c833d7190b8c713b",
    },
    "reader": {
        "model": "Qwen/Qwen3.5-9B",
        "revision": "c202236235762e1c871ad0ccb60c8ee5ba337b9a",
    },
    "embedding": {
        "model": "Qwen/Qwen3-Embedding-8B",
        "revision": "1d8ad4ca9b3dd8059ad90a75d4983776a23d44af",
    },
    "baseline_label": "REQUIRED",
    "token_budgets": list(CANONICAL_TOKEN_BUDGETS),
}
_CANONICAL_COMMIT = "0123456789abcdef"
_CANONICAL_TOKEN_ACCOUNTING_METHOD = "pinned_reader_content_tokenizer"
_RANK_METRICS = tuple(
    f"{metric}_at_{depth}"
    for metric in ("recall", "mrr", "ndcg")
    for depth in (1, 5, 10)
)
_GROUNDED_METRICS = ("grounded_f1", "abstention_f1")


class Tokenizer(Protocol):
    """Minimal tokenizer contract accepted by :func:`count_tokens`."""

    def encode(self, text: str) -> Sequence[Any]:
        ...


def canonical_json(value: Any) -> str:
    """Serialize config deterministically so its hash is portable."""
    # JSON has no representation for NaN or infinities.  Rejecting them here
    # keeps a checksummed artifact valid for strict JSON readers instead of
    # silently emitting Python's non-standard ``NaN``/``Infinity`` literals.
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: Union[str, Path]) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def source_digest(path: Union[str, Path]) -> dict[str, Union[str, int]]:
    """Return content-only provenance for one benchmark input.

    Paths deliberately reduce to their basename: public evidence needs to prove
    the bytes used, not disclose an operator's directory layout.
    """
    resolved = Path(path)
    return {
        "name": resolved.name,
        "sha256": sha256_file(resolved),
        "bytes": resolved.stat().st_size,
    }


def git_provenance(cwd: Optional[Union[str, Path]] = None) -> dict[str, Union[str, bool]]:
    """Capture commit and dirty state without exposing changed filenames."""
    root = str(cwd or Path.cwd())
    try:
        commit = subprocess.check_output(
            ["git", "-C", root, "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
        status = subprocess.check_output(
            ["git", "-C", root, "status", "--porcelain=v1"], text=True,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return {"commit": "unknown", "dirty": True, "dirty_state_sha256": sha256_text("unavailable")}
    return {
        "commit": commit or "unknown",
        "dirty": bool(status.strip()),
        "dirty_state_sha256": sha256_text(status),
    }


def environment_provenance() -> dict[str, Any]:
    """Return a compact, JSON-safe execution environment fingerprint."""
    packages = {}
    for distribution in ("engraphis", "numpy", "sentence-transformers", "transformers", "torch"):
        try:
            packages[distribution] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            continue
    return {
        "python": sys.version.split()[0],
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "packages": packages,
    }


_PUBLIC_RECORD_FIELDS = frozenset({
    "question_id", "category", "retrieved_ids", "supporting_ids", "context_tokens",
    "latency_ms", "abstained", "excluded", "answerable", "grounded",
    "grounded_support", "answer_token_recall", "context_token_method",
    "context_tokenizer_identity", "qa_score", "qa_correct", "retrieval_excluded", "usage",
})
_PUBLIC_METRIC_PREFIXES = ("recall_at_", "hit_at_", "mrr_at_", "ndcg_at_")
_PUBLIC_USAGE_FIELDS = frozenset({
    "budget_tokens", "context_tokens", "source_tokens", "saved_tokens", "savings_ratio",
    "packed_count", "omitted_count", "answer_tokens", "token_counter",
    "memory_context_tokens", "memory_context_original_tokens", "reader_prompt_tokens",
    "reader_completion_tokens", "adapter_reported_context_tokens",
})
_RAW_QUERY_FIELDS = ("q", "query", "question", "question_text")
_RAW_ANSWER_FIELDS = (
    "answer", "answer_gold", "answer_variants", "response", "response_raw",
    "response_parsed_boxed", "output", "completion", "model_output", "assistant_response",
)
_RAW_CONTEXT_FIELDS = (
    "context", "memory_context", "messages", "prompt_messages", "retrieved_context",
)
_SECRET_NAME_RE = re.compile(
    r"(?:^|[-_])(?:api[-_]?key|access[-_]?token|auth(?:orization)?|bearer|credential|"
    r"password|passwd|secret|token|signature|sig|private[-_]?key)$",
    re.IGNORECASE,
)
_COMPOUND_SECRET_NAME_RE = re.compile(
    r"(?:^|[-_])(?:api[-_]?key|access[-_]?key|secret[-_]?(?:access[-_]?)?key|"
    r"authorization)(?:[-_]|$)",
    re.IGNORECASE,
)
_HEADER_OPTIONS = frozenset({"--header", "--headers"})
_USERINFO_OPTIONS = frozenset({"-u", "--user", "--user-name", "--password", "-p"})


def _is_secret_name(value: str) -> bool:
    """Recognise credential-bearing parameter names, without masking normal options."""
    name = value.strip().lstrip("-")
    return bool(_SECRET_NAME_RE.search(name) or _COMPOUND_SECRET_NAME_RE.search(name))


def _public_exclusion(value: Any) -> Optional[dict[str, Any]]:
    """Keep an exclusion's reason but never allow a free-form detail to leak content."""
    if not isinstance(value, dict):
        return None
    public = {
        key: deepcopy(value[key])
        for key in ("question_id", "reason")
        if key in value
    }
    detail = value.get("detail")
    if detail == "":
        public["detail"] = ""
    elif detail is not None:
        public["detail_sha256"] = sha256_text(canonical_json(detail))
    return public


def _public_usage(value: Any) -> Optional[dict[str, Any]]:
    if not isinstance(value, dict):
        return None
    return {
        key: deepcopy(item)
        for key, item in value.items()
        if key in _PUBLIC_USAGE_FIELDS
    }


def redact_public_record(record: dict[str, Any]) -> dict[str, Any]:
    """Project a private evaluation row onto the audited public evidence schema.

    Public artifacts are evidence, not a lossless export. An allowlist prevents a new
    adapter field from accidentally publishing prompts, contexts, model output, tool calls,
    or other raw payloads before this boundary is reviewed.
    """
    public: dict[str, Any] = {}
    groups = (
        (_RAW_QUERY_FIELDS, "query_sha256"),
        (_RAW_ANSWER_FIELDS, "answer_or_response_sha256"),
        (_RAW_CONTEXT_FIELDS, "context_or_prompt_sha256"),
    )
    for fields, digest_field in groups:
        values = [
            {"field": field, "value": record[field]}
            for field in fields
            if field in record
        ]
        if values:
            public[digest_field] = sha256_text(canonical_json(values))
    for key, value in record.items():
        if key == "excluded":
            redacted = _public_exclusion(value)
            if redacted is not None:
                public[key] = redacted
        elif key == "usage":
            redacted = _public_usage(value)
            if redacted is not None:
                public[key] = redacted
        elif key in _PUBLIC_RECORD_FIELDS or key.startswith(_PUBLIC_METRIC_PREFIXES):
            public[key] = deepcopy(value)
    return public


def _redact_url(value: str) -> str:
    """Remove URL userinfo and credential-like query values without hiding the endpoint."""
    try:
        parsed = urlsplit(value)
    except ValueError:
        # An invalid authority can still contain credentials. Without a trustworthy parse,
        # preserve neither the authority nor the rest of the URL in public evidence.
        return "<redacted>"
    if not parsed.scheme or not parsed.netloc:
        return value

    def redact_parameters(component: str) -> str:
        # Fragments are often ordinary anchors. Only treat a fragment as a parameter list when
        # it contains an assignment, preserving links such as ``#methodology`` verbatim.
        if "=" not in component:
            return component
        return urlencode([
            (key, "<redacted>" if _is_secret_name(key) else item)
            for key, item in parse_qsl(component, keep_blank_values=True)
        ])

    try:
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError:
        # A malformed port must not make a credential-bearing authority pass through unchanged.
        # Keep the malformed host:port for diagnostics, but remove anything before its final @.
        authority = parsed.netloc.rsplit("@", 1)[-1]
        netloc = f"<redacted>@{authority}" if "@" in parsed.netloc else authority
    else:
        if port is not None:
            host = f"{host}:{port}"
        netloc = f"<redacted>@{host}" if parsed.username is not None else parsed.netloc

    return urlunsplit((
        parsed.scheme,
        netloc,
        parsed.path,
        redact_parameters(parsed.query),
        redact_parameters(parsed.fragment),
    ))


def _command_assignment(value: str) -> Optional[tuple[str, str]]:
    """Return a shell-style assignment without mistaking URL query parameters for one."""
    separator = value.find("=")
    if separator <= 0 or "://" in value[:separator]:
        return None
    return value[:separator], value[separator + 1:]


def redact_command(command: Sequence[str]) -> list[str]:
    """Preserve a reproducible command shape without retaining credentials or raw headers."""
    public: list[str] = []
    redact_next = False
    for item in command:
        value = str(item)
        lowered = value.casefold()
        assignment = _command_assignment(value)
        if redact_next:
            public.append("<redacted>")
            redact_next = False
        elif any(lowered.startswith(option + "=") for option in _HEADER_OPTIONS):
            public.extend([value.split("=", 1)[0], "<redacted>"])
        elif lowered.startswith("--user="):
            public.extend([value.split("=", 1)[0], "<redacted>"])
        elif value == "-H" or lowered in _HEADER_OPTIONS or lowered in _USERINFO_OPTIONS:
            public.append(value)
            redact_next = True
        elif value.startswith("-H") and len(value) > 2:
            public.extend([value[:2], "<redacted>"])
        elif lowered.startswith("-u") and len(value) > 2:
            public.extend([value[:2], "<redacted>"])
        elif lowered.startswith("-p") and len(value) > 2:
            public.extend([value[:2], "<redacted>"])
        elif lowered.startswith("--") and _is_secret_name(lowered.split("=", 1)[0]):
            public.append(value.split("=", 1)[0] if "=" in value else value)
            if "=" in value:
                public.append("<redacted>")
            else:
                redact_next = True
        elif assignment:
            key, assigned = assignment
            public.append(
                f"{key}=<redacted>" if _is_secret_name(key) else f"{key}={_redact_url(assigned)}"
            )
        elif "://" in value:
            public.append(_redact_url(value))
        elif _is_secret_name(value.split(":", 1)[0]) and ":" in value:
            public.append(value.split(":", 1)[0] + ": <redacted>")
        else:
            public.append(_redact_url(value))
    return public


def canonical_benchmark_config(
    *,
    run_label: str,
    baseline_label: str,
    token_budgets: Sequence[int] = CANONICAL_TOKEN_BUDGETS,
    profile: Optional[dict] = None,
) -> dict:
    """Build the labeled fixed-budget configuration required for a canonical run."""
    resolved_profile = deepcopy(
        profile if profile is not None else LONGMEMEVAL_V2_CANONICAL_PROFILE_TEMPLATE
    )
    resolved_profile["baseline_label"] = baseline_label
    return {
        "run_label": run_label,
        "baseline_label": baseline_label,
        "token_budgets": [int(budget) for budget in token_budgets],
        "canonical_profile": resolved_profile,
    }


def _sha256_error(value: Any, field: str, errors: list[str]) -> None:
    if not isinstance(value, str) or len(value) != 64:
        errors.append(f"{field} must be a 64-character SHA-256 hex string")
        return
    try:
        int(value, 16)
    except ValueError:
        errors.append(f"{field} must be a 64-character SHA-256 hex string")


def _is_finite_number(value: Any) -> bool:
    """Return whether ``value`` is a real finite number, excluding booleans."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _is_nonnegative_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _reader_tokenizer_identity(profile: Any) -> Optional[str]:
    """Return the only tokenizer identity valid for a canonical profile."""
    if not isinstance(profile, dict) or not isinstance(profile.get("reader"), dict):
        return None
    model = profile["reader"].get("model")
    revision = profile["reader"].get("revision")
    if not isinstance(model, str) or not isinstance(revision, str):
        return None
    return f"{model}@{revision}"


def _metric_matches(value: Any, expected: float) -> bool:
    return _is_finite_number(value) and math.isclose(
        float(value), float(expected), rel_tol=0.0, abs_tol=1e-6
    )


def _rank_metrics_from_record(record: dict) -> Optional[dict[str, float]]:
    """Recompute rank metrics only when the public evidence IDs are complete."""
    retrieved = record.get("retrieved_ids")
    supporting = record.get("supporting_ids")
    if (
        not isinstance(retrieved, list)
        or not isinstance(supporting, list)
        or not all(isinstance(item, str) for item in retrieved)
        or not all(isinstance(item, str) for item in supporting)
    ):
        return None
    return retrieval_metrics.retrieval_metrics_at_depths(
        retrieved, supporting, depths=(1, 5, 10)
    )


def validate_canonical_profile(profile: Any) -> list[str]:
    """Return validation errors for an immutable LongMemEval-V2 profile."""
    errors: list[str] = []
    if not isinstance(profile, dict):
        return ["canonical_profile must be an object"]
    required = (
        ("benchmark", "repository"), ("benchmark", "repository_revision"),
        ("benchmark", "dataset_revision"), ("reader", "model"), ("reader", "revision"),
        ("embedding", "model"), ("embedding", "revision"),
    )
    for section, field in required:
        value = profile.get(section, {}) if isinstance(profile.get(section), dict) else {}
        item = value.get(field)
        if not isinstance(item, str) or not item.strip() or item == "REQUIRED":
            errors.append(f"canonical_profile.{section}.{field} is required")
        elif field.endswith("revision") and (
            len(item) != 40 or any(char not in "0123456789abcdef" for char in item)
        ):
            errors.append(
                f"canonical_profile.{section}.{field} must be an immutable 40-character commit"
            )
    baseline = profile.get("baseline_label")
    if baseline not in CANONICAL_BASELINE_LABELS:
        errors.append("canonical_profile.baseline_label must name a declared baseline")
    budgets = profile.get("token_budgets")
    if budgets != list(CANONICAL_TOKEN_BUDGETS):
        errors.append("canonical_profile.token_budgets must be the canonical fixed budgets")
    return errors


def validate_report(report: Any, *, canonical: bool = False) -> list[str]:
    """Deterministically validate a public ``engraphis-benchmark/v2`` envelope."""
    errors: list[str] = []
    if not isinstance(report, dict):
        return ["report must be an object"]
    if report.get("schema") != SCHEMA:
        errors.append(f"schema must equal {SCHEMA}")
    for field in ("suite", "system", "environment", "protocol", "metrics"):
        if not isinstance(report.get(field), dict):
            errors.append(f"{field} must be an object")
    records = report.get("records")
    exclusions = report.get("exclusions")
    if not isinstance(records, list):
        errors.append("records must be an array")
        records = []
    if not isinstance(exclusions, list):
        errors.append("exclusions must be an array")
        exclusions = []
    suite = report.get("suite") if isinstance(report.get("suite"), dict) else {}
    system = report.get("system") if isinstance(report.get("system"), dict) else {}
    protocol = report.get("protocol") if isinstance(report.get("protocol"), dict) else {}
    for field in ("name", "dataset"):
        if not isinstance(suite.get(field), str) or not suite[field]:
            errors.append(f"suite.{field} must be a non-empty string")
    _sha256_error(suite.get("sha256"), "suite.sha256", errors)
    if not isinstance(system.get("git_commit"), str) or not system["git_commit"]:
        errors.append("system.git_commit must be a non-empty string")
    elif canonical and (
        len(system["git_commit"]) != 40
        or any(char not in _CANONICAL_COMMIT for char in system["git_commit"])
    ):
        errors.append("canonical system.git_commit must be an immutable lowercase 40-character commit")
    declared_config_hash = system.get("config_sha256")
    _sha256_error(declared_config_hash, "system.config_sha256", errors)
    if "git_dirty" in system and not isinstance(system.get("git_dirty"), bool):
        errors.append("system.git_dirty must be boolean")
    if "dirty_state_sha256" in system:
        _sha256_error(system.get("dirty_state_sha256"), "system.dirty_state_sha256", errors)
    sources = suite.get("sources")
    if sources is not None:
        if not isinstance(sources, list):
            errors.append("suite.sources must be an array when supplied")
        else:
            for item in sources:
                if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                    errors.append("each suite source requires a name")
                    continue
                _sha256_error(item.get("sha256"), "suite source sha256", errors)
                if not _is_nonnegative_integer(item.get("bytes")):
                    errors.append("each suite source requires non-negative bytes")
    if not isinstance(protocol.get("config"), dict):
        errors.append("protocol.config must be an object")
    else:
        actual_config_hash = sha256_text(canonical_json(protocol["config"]))
        if declared_config_hash != actual_config_hash:
            errors.append(
                "system.config_sha256 must match the canonical protocol.config digest"
            )
    command = protocol.get("command")
    if command is not None and (
        not isinstance(command, list)
        or not command
        or not all(isinstance(item, str) and item for item in command)
    ):
        errors.append("protocol.command must be a non-empty string array when supplied")
    accounting = protocol.get("token_accounting")
    if accounting is not None:
        required_accounting = ("identity", "revision", "scope", "method")
        if not isinstance(accounting, dict) or any(field not in accounting for field in required_accounting):
            errors.append("protocol.token_accounting must name identity, revision, scope, and method")
        elif (
            not isinstance(accounting["identity"], str)
            or not isinstance(accounting["scope"], str)
            or not isinstance(accounting["method"], str)
            or not (accounting["revision"] is None or isinstance(accounting["revision"], str))
        ):
            errors.append("protocol.token_accounting fields have invalid types")
    record_ids: list[str] = []
    embedded_exclusions: dict[str, dict] = {}
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("question_id"), str):
            errors.append("each record requires a string question_id")
            continue
        question_id = record["question_id"]
        if not question_id:
            errors.append("each record question_id must be non-empty")
            continue
        record_ids.append(question_id)
        embedded = record.get("excluded")
        if embedded is not None:
            if not isinstance(embedded, dict) or embedded.get("question_id") != question_id:
                errors.append("each record exclusion must name that record question_id")
            else:
                embedded_exclusions[question_id] = embedded

    if len(record_ids) != len(set(record_ids)):
        errors.append("record question_id values must be unique")

    exclusion_ids: list[str] = []
    for item in exclusions:
        if not isinstance(item, dict) or not isinstance(item.get("question_id"), str):
            errors.append("each exclusion requires a string question_id")
            continue
        question_id = item["question_id"]
        if not question_id:
            errors.append("each exclusion question_id must be non-empty")
            continue
        exclusion_ids.append(question_id)
        if question_id not in record_ids:
            errors.append("each exclusion must name a reported question_id")
        elif embedded_exclusions.get(question_id) != item:
            errors.append("top-level exclusions must exactly match per-record exclusions")
    if len(exclusion_ids) != len(set(exclusion_ids)):
        errors.append("exclusion question_id values must be unique")
    if set(exclusion_ids) != set(embedded_exclusions):
        errors.append("top-level exclusions must exactly match per-record exclusions")

    if (
        not _is_nonnegative_integer(protocol.get("n_total"))
        or protocol.get("n_total") != len(records)
    ):
        errors.append("protocol.n_total must equal records length")
    if (
        not _is_nonnegative_integer(protocol.get("n_scored"))
        or protocol.get("n_scored") != len(records) - len(embedded_exclusions)
    ):
        errors.append("protocol.n_scored must equal records minus exclusions")
    if canonical:
        config = protocol.get("config") if isinstance(protocol.get("config"), dict) else {}
        if not isinstance(system.get("git_dirty"), bool):
            errors.append("canonical reports require system.git_dirty")
        elif system["git_dirty"]:
            errors.append("canonical reports require a clean git worktree")
        if not isinstance(protocol.get("command"), list) or not protocol.get("command"):
            errors.append("canonical reports require protocol.command")
        if not isinstance(protocol.get("token_accounting"), dict):
            errors.append("canonical reports require protocol.token_accounting")
        privacy = report.get("privacy")
        if not isinstance(privacy, dict) or privacy.get("raw_query_policy") != "redacted_sha256":
            errors.append("canonical reports require raw-query redaction metadata")
        if protocol.get("complete_dataset") is not True:
            errors.append("canonical protocol.complete_dataset must be true")
        source_questions = protocol.get("source_questions")
        if not _is_nonnegative_integer(source_questions) or source_questions == 0:
            errors.append("canonical protocol.source_questions must be a positive integer")
        elif source_questions != len(records) or source_questions != protocol.get("n_total"):
            errors.append(
                "canonical protocol.source_questions must equal protocol.n_total and records length"
            )
        if config.get("baseline_label") not in CANONICAL_BASELINE_LABELS:
            errors.append("protocol.config.baseline_label must name a declared baseline")
        if config.get("token_budgets") != list(CANONICAL_TOKEN_BUDGETS):
            errors.append("protocol.config.token_budgets must be the canonical fixed budgets")
        configured_budget = config.get("token_budget")
        if configured_budget is not None and not _is_nonnegative_integer(configured_budget):
            errors.append("canonical protocol.config.token_budget must be a non-negative integer or null")
        profile = config.get("canonical_profile")
        errors.extend(validate_canonical_profile(profile))
        if isinstance(profile, dict) and profile.get("baseline_label") != config.get(
            "baseline_label"
        ):
            errors.append(
                "protocol.config.baseline_label must match canonical_profile.baseline_label"
            )
        _validate_canonical_measurement_contract(report, records, profile, errors)
    return errors


def _validate_canonical_measurement_contract(
    report: dict,
    records: Sequence[dict],
    profile: Any,
    errors: list[str],
) -> None:
    """Require the per-question evidence that makes a canonical result auditable."""
    models = report.get("models")
    embedder = models.get("embedder") if isinstance(models, dict) else None
    expected_embedding = profile.get("embedding", {}) if isinstance(profile, dict) else {}
    if not isinstance(embedder, dict):
        errors.append("canonical reports require models.embedder provenance")
    else:
        for field in ("name", "model_id", "revision", "sha256"):
            if not isinstance(embedder.get(field), str) or not embedder[field]:
                errors.append(f"canonical models.embedder.{field} is required")
        _sha256_error(embedder.get("sha256"), "canonical models.embedder.sha256", errors)
        if embedder.get("model_id") != expected_embedding.get("model"):
            errors.append("canonical models.embedder.model_id must match canonical_profile.embedding.model")
        if embedder.get("revision") != expected_embedding.get("revision"):
            errors.append("canonical models.embedder.revision must match canonical_profile.embedding.revision")
    if not isinstance(report.get("metrics"), dict):
        return
    metrics = report["metrics"]
    expected_tokenizer_identity = _reader_tokenizer_identity(profile)
    configured_budget = (
        report.get("protocol", {}).get("config", {}).get("token_budget")
        if isinstance(report.get("protocol"), dict)
        and isinstance(report.get("protocol", {}).get("config"), dict)
        else None
    )
    for field in _RANK_METRICS:
        value = metrics.get(field)
        if not _is_finite_number(value) or not 0.0 <= float(value) <= 1.0:
            errors.append(f"canonical metrics.{field} must be a number in [0, 1]")
    confidence = metrics.get("confidence_intervals")
    _validate_confidence_intervals(confidence, metrics, records, errors)
    paired = metrics.get("paired_bootstrap")
    _validate_paired_bootstrap(paired, records, errors)
    _validate_grounded_metric_availability(metrics, records, errors)
    _validate_fixed_budget_curve(
        metrics, records, expected_tokenizer_identity, errors
    )
    for record in records:
        if not isinstance(record, dict):
            continue
        if "q" in record:
            errors.append("canonical records must not contain raw query text")
        if "question_sha256" in record:
            errors.append("canonical records must not contain question-derived hashes")
        latency = record.get("latency_ms")
        if not _is_finite_number(latency) or float(latency) < 0:
            errors.append("canonical records require non-negative latency_ms")
        context_tokens = record.get("context_tokens")
        if not _is_finite_number(context_tokens) or float(context_tokens) < 0:
            errors.append("canonical records require non-negative finite context_tokens")
        elif (
            _is_nonnegative_integer(configured_budget)
            and float(context_tokens) > configured_budget
        ):
            errors.append("canonical record context_tokens must not exceed protocol token_budget")
        usage = record.get("usage")
        if isinstance(usage, dict):
            usage_budget = usage.get("budget_tokens")
            usage_context = usage.get("context_tokens")
            if not _is_finite_number(usage_budget) or float(usage_budget) < 0:
                errors.append("canonical record usage.budget_tokens must be non-negative and finite")
            if not _is_finite_number(usage_context) or float(usage_context) < 0:
                errors.append("canonical record usage.context_tokens must be non-negative and finite")
            elif _is_finite_number(usage_budget) and float(usage_context) > float(usage_budget):
                errors.append("canonical record usage.context_tokens must not exceed usage.budget_tokens")
            if (
                _is_finite_number(context_tokens)
                and _is_finite_number(usage_context)
                and float(context_tokens) != float(usage_context)
            ):
                errors.append("canonical record context_tokens must equal usage.context_tokens")
            if (
                _is_nonnegative_integer(configured_budget)
                and _is_finite_number(usage_budget)
                and float(usage_budget) != configured_budget
            ):
                errors.append("canonical record usage.budget_tokens must equal protocol token_budget")
            for field in ("source_tokens", "saved_tokens"):
                value = usage.get(field)
                if field in usage and (
                    not _is_finite_number(value) or float(value) < 0
                ):
                    errors.append(
                        f"canonical record usage.{field} must be non-negative and finite"
                    )
            savings_ratio = usage.get("savings_ratio")
            if "savings_ratio" in usage and (
                not _is_finite_number(savings_ratio)
                or not 0.0 <= float(savings_ratio) <= 1.0
            ):
                errors.append("canonical record usage.savings_ratio must be a number in [0, 1]")
            for field in ("packed_count", "omitted_count"):
                if field in usage and not _is_nonnegative_integer(usage.get(field)):
                    errors.append(
                        f"canonical record usage.{field} must be a non-negative integer"
                    )
            if usage.get("token_counter") != expected_tokenizer_identity:
                errors.append(
                    "canonical record usage.token_counter must match canonical_profile.reader"
                )
        else:
            errors.append(
                "canonical records require usage with pinned reader token accounting"
            )
        for field in ("answerable", "grounded", "abstained"):
            if field in record and not isinstance(record.get(field), bool):
                errors.append(f"canonical record {field} must be boolean when present")
        method = record.get("context_token_method")
        if method != _CANONICAL_TOKEN_ACCOUNTING_METHOD:
            errors.append(
                "canonical records require "
                "context_token_method=pinned_reader_content_tokenizer"
            )
        if record.get("context_tokenizer_identity") != expected_tokenizer_identity:
            errors.append(
                "canonical record context_tokenizer_identity must match canonical_profile.reader"
            )
        recomputed = _rank_metrics_from_record(record)
        if recomputed is None:
            errors.append(
                "canonical records require string-array retrieved_ids and supporting_ids"
            )
        for field in _RANK_METRICS:
            value = record.get(field)
            if not _is_finite_number(value) or not 0.0 <= float(value) <= 1.0:
                errors.append(f"canonical records require {field} in [0, 1]")
            elif recomputed is not None and not _metric_matches(value, recomputed[field]):
                errors.append(
                    f"canonical record {field} must match retrieved_ids and supporting_ids"
                )
    _validate_rank_metric_aggregates(metrics, records, errors)


def _validate_rank_metric_aggregates(
    metrics: dict, records: Sequence[dict], errors: list[str]
) -> None:
    """Recompute every canonical aggregate from non-excluded question evidence."""
    scored = [
        record for record in records
        if isinstance(record, dict) and not record.get("excluded")
    ]
    recomputed = [_rank_metrics_from_record(record) for record in scored]
    if any(item is None for item in recomputed):
        return
    for field in _RANK_METRICS:
        expected = (
            sum(item[field] for item in recomputed if item is not None) / len(recomputed)
            if recomputed else 0.0
        )
        if not _metric_matches(metrics.get(field), expected):
            errors.append(
                f"canonical metrics.{field} must equal the non-excluded record mean"
            )


def _validate_confidence_intervals(
    confidence: Any,
    metrics: dict,
    records: Sequence[dict],
    errors: list[str],
) -> None:
    """Require complete, bounded confidence intervals tied to reported point estimates."""
    if not isinstance(confidence, dict) or set(confidence) != set(_RANK_METRICS):
        errors.append(
            "canonical metrics.confidence_intervals must exactly cover every rank metric"
        )
        return
    expected_keys = {
        "point", "low", "high", "n", "seed", "iterations", "strata_key",
    }
    n_scored = sum(
        1 for record in records
        if isinstance(record, dict) and not record.get("excluded")
    )
    for field in _RANK_METRICS:
        interval = confidence[field]
        prefix = f"canonical metrics.confidence_intervals.{field}"
        if not isinstance(interval, dict) or set(interval) != expected_keys:
            errors.append(f"{prefix} must match the canonical confidence interval schema")
            continue
        point = interval.get("point")
        low = interval.get("low")
        high = interval.get("high")
        if not all(
            _is_finite_number(value) and 0.0 <= float(value) <= 1.0
            for value in (point, low, high)
        ):
            errors.append(f"{prefix} point/low/high must be finite numbers in [0, 1]")
        elif not float(low) <= float(point) <= float(high):
            errors.append(f"{prefix} must satisfy low <= point <= high")
        aggregate = metrics.get(field)
        if (
            not _is_finite_number(aggregate)
            or not _metric_matches(point, float(aggregate))
        ):
            errors.append(f"{prefix}.point must match metrics.{field}")
        if not _is_nonnegative_integer(interval.get("n")) or interval["n"] != n_scored:
            errors.append(f"{prefix}.n must equal the non-excluded record count")
        if not _is_nonnegative_integer(interval.get("seed")):
            errors.append(f"{prefix}.seed must be a non-negative integer")
        iterations = interval.get("iterations")
        if not _is_nonnegative_integer(iterations) or iterations == 0:
            errors.append(f"{prefix}.iterations must be a positive integer")
        if interval.get("strata_key") != "category":
            errors.append(f"{prefix}.strata_key must equal category")


def _validate_paired_bootstrap(
    paired: Any, records: Sequence[dict], errors: list[str]
) -> None:
    """Validate exact available/unavailable paired-bootstrap payload shapes."""
    prefix = "canonical metrics.paired_bootstrap"
    if not isinstance(paired, dict) or not isinstance(paired.get("available"), bool):
        errors.append(f"{prefix} must explicitly state availability")
        return
    n_scored = sum(
        1 for record in records
        if isinstance(record, dict) and not record.get("excluded")
    )
    if paired["available"] is False:
        expected_keys = {
            "available", "reason", "n", "delta", "low", "high", "iterations",
        }
        if set(paired) != expected_keys:
            errors.append(f"{prefix} unavailable payload must match the canonical schema")
            return
        if not isinstance(paired.get("reason"), str) or not paired["reason"].strip():
            errors.append(f"{prefix}.reason must be a non-empty string when unavailable")
        if paired.get("n") != 0 or isinstance(paired.get("n"), bool):
            errors.append(f"{prefix}.n must be zero when unavailable")
        if any(paired.get(field) is not None for field in ("delta", "low", "high")):
            errors.append(f"{prefix} delta/low/high must be null when unavailable")
        iterations = paired.get("iterations")
        if not _is_nonnegative_integer(iterations) or iterations == 0:
            errors.append(f"{prefix}.iterations must be a positive integer")
        return

    expected_keys = {
        "available", "metric", "delta", "low", "high", "n", "seed", "iterations",
    }
    if set(paired) != expected_keys:
        errors.append(f"{prefix} available payload must match the canonical schema")
        return
    if paired.get("metric") not in _RANK_METRICS:
        errors.append(f"{prefix}.metric must name a canonical rank metric")
    delta = paired.get("delta")
    low = paired.get("low")
    high = paired.get("high")
    if not all(
        _is_finite_number(value) and -1.0 <= float(value) <= 1.0
        for value in (delta, low, high)
    ):
        errors.append(f"{prefix} delta/low/high must be finite numbers in [-1, 1]")
    elif not float(low) <= float(delta) <= float(high):
        errors.append(f"{prefix} must satisfy low <= delta <= high")
    if (
        not _is_nonnegative_integer(paired.get("n"))
        or paired["n"] == 0
        or paired["n"] != n_scored
    ):
        errors.append(f"{prefix}.n must equal the positive non-excluded record count")
    if not _is_nonnegative_integer(paired.get("seed")):
        errors.append(f"{prefix}.seed must be a non-negative integer")
    iterations = paired.get("iterations")
    if not _is_nonnegative_integer(iterations) or iterations == 0:
        errors.append(f"{prefix}.iterations must be a positive integer")


def _validate_grounded_metric_availability(
    metrics: dict, records: Sequence[dict], errors: list[str]
) -> None:
    """Require measured F1 or an explicit, machine-readable unavailable state."""
    specs = {
        "grounded_f1": ("grounded", retrieval_metrics.grounded_precision_recall_f1),
        "abstention_f1": ("abstained", retrieval_metrics.abstention_precision_recall_f1),
    }
    labeled = [
        record for record in records
        if isinstance(record, dict) and isinstance(record.get("answerable"), bool)
    ]
    for field, (prediction_field, score) in specs.items():
        value = metrics.get(field)
        if _is_finite_number(value):
            if not 0.0 <= float(value) <= 1.0:
                errors.append(f"canonical metrics.{field} must be a number in [0, 1]")
                continue
            if not labeled or not all(
                isinstance(record.get(prediction_field), bool) for record in labeled
            ):
                errors.append(
                    f"canonical metrics.{field} requires labeled per-question "
                    f"{prediction_field} values; otherwise use an unavailable reason"
                )
                continue
            expected = score(
                [record[prediction_field] for record in labeled],
                [record["answerable"] for record in labeled],
            )
            if not _metric_matches(value, float(expected["f1"])):
                errors.append(
                    f"canonical metrics.{field} must be recomputed from per-question labels"
                )
            summary_name = field.removesuffix("_f1")
            summary = metrics.get(summary_name)
            if not isinstance(summary, dict) or summary.get("available") is not True:
                errors.append(
                    f"canonical numeric metrics.{field} requires an available "
                    f"metrics.{summary_name} count summary"
                )
                continue
            for summary_field, expected_value in expected.items():
                reported = summary.get(summary_field)
                matches = (
                    reported == expected_value
                    and not isinstance(reported, bool)
                    if isinstance(expected_value, int)
                    else _metric_matches(reported, float(expected_value))
                )
                if not matches:
                    errors.append(
                        f"canonical metrics.{summary_name}.{summary_field} must be "
                        "recomputed from per-question labels"
                    )
            continue
        if (
            isinstance(value, dict)
            and value.get("available") is False
            and isinstance(value.get("reason"), str)
            and value["reason"].strip()
        ):
            continue
        errors.append(
            f"canonical metrics.{field} must be a number in [0, 1] or an unavailable reason"
        )


def _validate_fixed_budget_curve(
    metrics: dict,
    records: Sequence[dict],
    expected_tokenizer_identity: Optional[str],
    errors: list[str],
) -> None:
    """Require measured, per-question evidence at every canonical token budget.

    Merely declaring budgets in ``protocol.config`` does not show that retrieval was
    actually run at those budgets.  The curve therefore contains its own per-question
    rows, whose IDs and exclusion state must exactly match the report's public record
    set.  An unavailable curve is retained as a machine-readable status but cannot
    qualify as canonical evidence.
    """
    curve = metrics.get("fixed_budget_curve")
    if not isinstance(curve, dict):
        errors.append("canonical metrics.fixed_budget_curve must be an object")
        return
    if curve.get("available") is False:
        if not isinstance(curve.get("reason"), str) or not curve["reason"].strip():
            errors.append("canonical metrics.fixed_budget_curve unavailable state requires a reason")
        errors.append("canonical fixed-budget curve is unavailable and cannot qualify as evidence")
        return
    if curve.get("available") is not True:
        errors.append("canonical metrics.fixed_budget_curve must explicitly state availability")
        return
    rows = curve.get("rows")
    if not isinstance(rows, list):
        errors.append("canonical metrics.fixed_budget_curve.rows must be an array")
        return
    expected_ids = {record.get("question_id") for record in records if isinstance(record, dict)}
    expected_exclusions = {
        record.get("question_id"): bool(record.get("excluded"))
        for record in records
        if isinstance(record, dict)
    }
    by_budget: dict[int, dict] = {}
    for row in rows:
        if not isinstance(row, dict) or not _is_nonnegative_integer(row.get("token_budget")):
            errors.append("canonical fixed-budget curve rows require an integer token_budget")
            continue
        budget = row["token_budget"]
        if budget in by_budget:
            errors.append("canonical fixed-budget curve token_budget values must be unique")
            continue
        by_budget[budget] = row
    if set(by_budget) != set(CANONICAL_TOKEN_BUDGETS):
        errors.append("canonical fixed-budget curve must contain every canonical token budget")
    for budget in CANONICAL_TOKEN_BUDGETS:
        row = by_budget.get(budget)
        if row is None:
            continue
        if row.get("status") != "measured":
            errors.append(f"canonical fixed-budget curve {budget} must be measured")
        for field in _RANK_METRICS:
            value = row.get(field)
            if not _is_finite_number(value) or not 0.0 <= float(value) <= 1.0:
                errors.append(
                    f"canonical fixed-budget curve {budget} requires {field} in [0, 1]"
                )
        measurements = row.get("records")
        if not isinstance(measurements, list):
            errors.append(f"canonical fixed-budget curve {budget} requires per-question records")
            continue
        measurement_ids = []
        scored_metrics: list[dict[str, float]] = []
        for item in measurements:
            if not isinstance(item, dict) or not isinstance(item.get("question_id"), str):
                errors.append(f"canonical fixed-budget curve {budget} records require question_id")
                continue
            measurement_ids.append(item["question_id"])
            if bool(item.get("excluded")) != expected_exclusions.get(item["question_id"]):
                errors.append(
                    f"canonical fixed-budget curve {budget} records must preserve exclusion state"
                )
            context_tokens = item.get("context_tokens")
            if (
                not _is_finite_number(context_tokens)
                or not 0 <= float(context_tokens) <= budget
            ):
                errors.append(
                    f"canonical fixed-budget curve {budget} records require context_tokens within budget"
                )
            if item.get("context_token_method") != _CANONICAL_TOKEN_ACCOUNTING_METHOD:
                errors.append(
                    f"canonical fixed-budget curve {budget} records require "
                    "context_token_method=pinned_reader_content_tokenizer"
                )
            if item.get("context_tokenizer_identity") != expected_tokenizer_identity:
                errors.append(
                    f"canonical fixed-budget curve {budget} record tokenizer identity "
                    "must match canonical_profile.reader"
                )
            recomputed = _rank_metrics_from_record(item)
            if recomputed is None:
                errors.append(
                    f"canonical fixed-budget curve {budget} records require string-array "
                    "retrieved_ids and supporting_ids"
                )
            elif not item.get("excluded"):
                scored_metrics.append(recomputed)
            for field in _RANK_METRICS:
                value = item.get(field)
                if (
                    not _is_finite_number(value)
                    or not 0.0 <= float(value) <= 1.0
                ):
                    errors.append(
                        f"canonical fixed-budget curve {budget} records require {field} in [0, 1]"
                    )
                elif recomputed is not None and not _metric_matches(value, recomputed[field]):
                    errors.append(
                        f"canonical fixed-budget curve {budget} record {field} must match "
                        "retrieved_ids and supporting_ids"
                    )
        if len(measurement_ids) != len(set(measurement_ids)) or set(measurement_ids) != expected_ids:
            errors.append(
                f"canonical fixed-budget curve {budget} records must exactly cover report question_ids"
            )
        if not _is_nonnegative_integer(row.get("n_total")) or row.get("n_total") != len(records):
            errors.append(f"canonical fixed-budget curve {budget} n_total must equal report records")
        scored = sum(1 for item in measurements if isinstance(item, dict) and not item.get("excluded"))
        if not _is_nonnegative_integer(row.get("n_scored")) or row.get("n_scored") != scored:
            errors.append(f"canonical fixed-budget curve {budget} n_scored must match records")
        if len(scored_metrics) == scored:
            for field in _RANK_METRICS:
                expected = (
                    sum(item[field] for item in scored_metrics) / len(scored_metrics)
                    if scored_metrics else 0.0
                )
                if not _metric_matches(row.get(field), expected):
                    errors.append(
                        f"canonical fixed-budget curve {budget} {field} must equal "
                        "the non-excluded record mean"
                    )


def write_canonical_artifact(report: dict, output: Union[str, Path], *, canonical: bool = False) -> dict:
    """Write immutable canonical JSON and a SHA-256 sidecar after validation.

    Repeating an identical write is harmless. A different payload at the same
    path is rejected, avoiding accidental replacement of a public evidence run.
    """
    errors = validate_report(report, canonical=canonical)
    if errors:
        raise ValueError("invalid benchmark report: " + "; ".join(errors))
    payload = canonical_json(report).encode("utf-8") + b"\n"
    digest = hashlib.sha256(payload).hexdigest()
    artifact = Path(output)
    if artifact.exists() and artifact.read_bytes() != payload:
        raise FileExistsError(f"refusing to replace immutable artifact: {artifact}")
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(payload)
    sidecar = artifact.with_name(artifact.name + ".sha256")
    checksum = f"{digest}  {artifact.name}\n".encode("ascii")
    if sidecar.exists() and sidecar.read_bytes() != checksum:
        raise FileExistsError(f"refusing to replace immutable checksum: {sidecar}")
    sidecar.write_bytes(checksum)
    return {"artifact": str(artifact), "sha256": digest, "checksum": str(sidecar)}


def count_tokens(
    text: str, tokenizer: Optional[Union[Tokenizer, Callable[[str], int]]] = None
) -> dict:
    """Count tokens with an injected exact tokenizer or deterministic fallback.

    The caller must supply the tokenizer used by its reader to call this count
    ``exact``.  The fallback is intentionally labelled an estimate rather than
    pretending a whitespace heuristic is an LLM tokenizer.
    """
    if tokenizer is None:
        return {"tokens": estimate_tokens(text), "method": "deterministic_estimate"}
    if callable(tokenizer) and not hasattr(tokenizer, "encode"):
        return {"tokens": int(tokenizer(text)), "method": "injected"}
    return {"tokens": len(tokenizer.encode(text)), "method": "injected"}


def packed_context_tokens(
    chunks: Iterable[str],
    *,
    tokenizer: Optional[Union[Tokenizer, Callable[[str], int]]] = None,
) -> dict:
    """Count the exact injected context, including chunk separators."""
    return count_tokens("\n\n".join(chunk for chunk in chunks if chunk), tokenizer)


def exclusion(question_id: str, reason: str, *, detail: str = "") -> dict:
    return {"question_id": question_id, "reason": reason, "detail": detail}


def question_record(
    question_id: str,
    *,
    category: str = "unknown",
    retrieved_ids: Optional[Sequence[str]] = None,
    supporting_ids: Optional[Sequence[str]] = None,
    context_tokens: Optional[int] = None,
    latency_ms: Optional[float] = None,
    abstained: Optional[bool] = None,
    excluded: Optional[dict] = None,
    **metrics: Any,
) -> dict:
    """Create a stable per-question public record without storing raw corpora."""
    record: dict[str, Any] = {
        "question_id": question_id,
        "category": category,
        "retrieved_ids": list(retrieved_ids or []),
        "supporting_ids": list(supporting_ids or []),
    }
    if context_tokens is not None:
        record["context_tokens"] = int(context_tokens)
    if latency_ms is not None:
        record["latency_ms"] = round(float(latency_ms), 6)
    if abstained is not None:
        record["abstained"] = bool(abstained)
    if excluded is not None:
        record["excluded"] = excluded
    record.update(metrics)
    return record


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def stratified_bootstrap_ci(
    records: Sequence[dict],
    metric: Callable[[Sequence[dict]], float],
    *,
    strata_key: str = "category",
    iterations: int = 1000,
    seed: int = 20260729,
    alpha: float = 0.05,
) -> dict:
    """Deterministic percentile CI that resamples within each named stratum."""
    usable = [record for record in records if not record.get("excluded")]
    groups: dict[str, list[dict]] = {}
    for record in usable:
        groups.setdefault(str(record.get(strata_key, "unknown")), []).append(record)
    point = metric(usable)
    if not usable or iterations <= 0:
        return {"point": point, "low": point, "high": point, "n": len(usable), "seed": seed}
    rng = random.Random(seed)
    samples: list[float] = []
    for _ in range(iterations):
        sampled = [item for group in groups.values() for item in
                   (rng.choice(group) for _ in range(len(group)))]
        samples.append(metric(sampled))
    samples.sort()
    low_index = max(0, math.floor((alpha / 2) * (iterations - 1)))
    high_index = min(iterations - 1, math.ceil((1 - alpha / 2) * (iterations - 1)))
    return {
        "point": round(point, 6), "low": round(samples[low_index], 6),
        "high": round(samples[high_index], 6), "n": len(usable), "seed": seed,
        "iterations": iterations, "strata_key": strata_key,
    }


def paired_bootstrap_ci(
    pairs: Sequence[tuple[float, float]],
    *,
    iterations: int = 1000,
    seed: int = 20260729,
    alpha: float = 0.05,
) -> dict:
    """CI for mean(candidate - baseline) over paired benchmark observations."""
    deltas = [candidate - baseline for candidate, baseline in pairs]
    point = _mean(deltas)
    if not deltas or iterations <= 0:
        return {"delta": point, "low": point, "high": point, "n": len(deltas), "seed": seed}
    rng = random.Random(seed)
    sampled = []
    for _ in range(iterations):
        sampled.append(_mean([rng.choice(deltas) for _ in deltas]))
    sampled.sort()
    low_index = max(0, math.floor((alpha / 2) * (iterations - 1)))
    high_index = min(iterations - 1, math.ceil((1 - alpha / 2) * (iterations - 1)))
    return {
        "delta": round(point, 6), "low": round(sampled[low_index], 6),
        "high": round(sampled[high_index], 6), "n": len(deltas), "seed": seed,
        "iterations": iterations,
    }


def fixed_budget_curve(records: Sequence[dict], budgets: Sequence[int]) -> list[dict]:
    """Summarize evidence quality available at each packed-context token budget.

    Each record has ordered ``chunks`` of ``{"id", "tokens"}`` and
    ``supporting_ids``.  This records only retrieval/capping behavior; callers
    can attach reader quality separately.
    """
    result = []
    usable = [record for record in records if not record.get("excluded")]
    for budget in sorted(set(int(value) for value in budgets if value >= 0)):
        recalls, hits, used_tokens = [], [], []
        for record in usable:
            used = 0
            ids = []
            for chunk in record.get("chunks", []):
                tokens = int(chunk.get("tokens", 0))
                if used + tokens > budget:
                    continue
                used += tokens
                ids.append(str(chunk.get("id", "")))
            supporting = set(str(value) for value in record.get("supporting_ids", []))
            overlap = len(supporting.intersection(ids))
            recalls.append(overlap / len(supporting) if supporting else 1.0)
            hits.append(1.0 if overlap else 0.0)
            used_tokens.append(used)
        result.append({
            "token_budget": budget, "n": len(usable), "recall": round(_mean(recalls), 6),
            "hit_rate": round(_mean(hits), 6), "mean_packed_tokens": round(_mean(used_tokens), 3),
        })
    return result


def report_envelope(
    *,
    suite: str,
    dataset_path: Union[str, Path],
    config: dict,
    records: Sequence[dict],
    metrics: Optional[dict] = None,
    exclusions: Optional[Sequence[dict]] = None,
    git_commit: Optional[str] = None,
    command: Optional[Sequence[str]] = None,
    source_paths: Optional[Sequence[Union[str, Path]]] = None,
    models: Optional[dict] = None,
    token_accounting: Optional[dict] = None,
) -> dict:
    """Build a JSON-safe, provenance-complete public benchmark envelope.

    This is intentionally the one path through which public reports obtain
    provenance. It redacts raw question/answer/context fields before any caller
    can persist the returned envelope.
    """
    path = Path(dataset_path)
    observed_git = git_provenance()
    resolved_commit = git_commit if git_commit is not None else str(observed_git["commit"])
    public_records = [redact_public_record(dict(record)) for record in records]
    resolved_exclusions = [
        redacted
        for item in exclusions or ()
        if (redacted := _public_exclusion(item)) is not None
    ]
    resolved_exclusions.extend(
        record["excluded"] for record in public_records if record.get("excluded")
    )
    # An adapter may supply both top-level and per-record exclusions. Retain one
    # canonical representation so ``n_scored`` remains an honest denominator.
    unique_exclusions = []
    seen_exclusions = set()
    for item in resolved_exclusions:
        marker = canonical_json(item)
        if marker not in seen_exclusions:
            unique_exclusions.append(item)
            seen_exclusions.add(marker)
    return {
        "schema": SCHEMA,
        "suite": {
            "name": suite,
            "dataset": path.name,
            "sha256": sha256_file(path),
            "sources": [source_digest(item) for item in source_paths or ()],
        },
        "system": {
            "git_commit": resolved_commit,
            "git_dirty": observed_git["dirty"],
            "dirty_state_sha256": observed_git["dirty_state_sha256"],
            "config_sha256": sha256_text(canonical_json(config)),
        },
        "environment": environment_provenance(),
        "protocol": {
            "command": redact_command(command or ("in_process",)),
            "config": config,
            "token_accounting": dict(token_accounting or {
                "identity": "unspecified",
                "revision": None,
                "scope": "unspecified",
                "method": "unspecified",
            }),
            "n_total": len(public_records),
            "n_scored": len(public_records) - len(unique_exclusions),
        },
        "privacy": {
            "raw_query_policy": "redacted_sha256",
            "raw_answer_policy": "redacted_sha256",
            "raw_context_policy": "redacted_sha256",
            "digest_algorithm": "sha256",
        },
        "models": dict(models or {}),
        "metrics": metrics or {}, "exclusions": unique_exclusions,
        "records": public_records,
    }


def main(argv: Optional[list[str]] = None) -> int:
    """Validate an existing report and write its immutable canonical artifact."""
    parser = argparse.ArgumentParser(description="Validate and write an Engraphis benchmark artifact.")
    parser.add_argument("--input", required=True, help="JSON report envelope to validate.")
    parser.add_argument("--output", required=True, help="Canonical JSON artifact path.")
    parser.add_argument(
        "--canonical", action="store_true", help="Require the pinned LongMemEval-V2 profile."
    )
    args = parser.parse_args(argv)
    try:
        report = json.loads(Path(args.input).read_text(encoding="utf-8"))
        written = write_canonical_artifact(report, args.output, canonical=args.canonical)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"benchmark artifact error: {exc}", file=sys.stderr)
        return 2
    print(canonical_json(written))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
