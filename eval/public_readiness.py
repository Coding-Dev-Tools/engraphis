"""Dependency-free CI guard for public benchmark evidence and claims.

This module deliberately does not score a benchmark.  It checks that a public
artifact identifies the inputs and execution that produced it, and that prose
claims stay within the artifact's measurement boundary.  In particular,
retrieval-only evidence cannot be presented as answer quality, cost, or
latency evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_HOSTED_EVIDENCE_SCHEMA = "engraphis-hosted-evidence/v1"
_SCOPES = frozenset({"retrieval_only", "end_to_end", "provider_observed"})
_MANIFEST_SCHEMA = "engraphis-public-benchmark-series/v1"
_CANONICAL_TOKEN_BUDGETS = (256, 512, 1024, 2048, 4096)
_REQUIRED_BASELINES = frozenset({
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
})
_IMMUTABLE_REVISION_FIELDS = (
    ("benchmark", "repository_revision"),
    ("benchmark", "dataset_revision"),
    ("reader", "revision"),
    ("embedding", "revision"),
)
_IMMUTABLE_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
_PROVENANCE_FIELDS = (
    "schema",
    "dataset",
    "dataset_sha256",
    "git_commit",
    "config_sha256",
)
_RETRIEVAL_ONLY_OVERCLAIM_RE = re.compile(
    r"\b(?:answer(?:s|ed|ing)?|accuracy|correct(?:ness)?|ground(?:ed|ing)?|"
    r"task\s+completion|cost(?:s)?|pricing|dollars?|latenc(?:y|ies)|"
    r"faster|speed|throughput)\b",
    re.IGNORECASE,
)
_FORBIDDEN_CONTENT_FIELDS = frozenset({
    "answer",
    "answer_gold",
    "answer_variants",
    "assistant_response",
    "completion",
    "context",
    "memory_context",
    "messages",
    "model_output",
    "output",
    "prompt",
    "prompt_messages",
    "q",
    "query",
    "question",
    "question_text",
    "response",
    "response_raw",
    "retrieved_context",
})
_SECRET_FIELD_RE = re.compile(
    r"(?:^|[-_])(?:api[-_]?key|access[-_]?token|authorization|bearer|credential|"
    r"password|passwd|secret|private[-_]?key)(?:[-_]|$)",
    re.IGNORECASE,
)


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and bool(_SHA256_RE.fullmatch(value))


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _nonnegative_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _artifact_scope(artifact: Mapping[str, Any]) -> Any:
    """Read the explicit measurement boundary from supported public envelopes."""
    if "measurement_scope" in artifact:
        return artifact["measurement_scope"]
    protocol = artifact.get("protocol")
    if isinstance(protocol, Mapping):
        config = protocol.get("config")
        if isinstance(config, Mapping):
            if "measurement_scope" in config:
                return config["measurement_scope"]
            if "claim_boundary" in config:
                return config["claim_boundary"]
    metrics = artifact.get("metrics")
    if isinstance(metrics, Mapping):
        return metrics.get("measurement_scope") or metrics.get("claim_boundary")
    return None


def _artifact_provenance(artifact: Mapping[str, Any]) -> dict[str, Any]:
    suite = artifact.get("suite")
    system = artifact.get("system")
    return {
        "schema": artifact.get("schema"),
        "dataset": suite.get("dataset") if isinstance(suite, Mapping) else None,
        "dataset_sha256": suite.get("sha256") if isinstance(suite, Mapping) else None,
        "git_commit": system.get("git_commit") if isinstance(system, Mapping) else None,
        "config_sha256": system.get("config_sha256") if isinstance(system, Mapping) else None,
    }


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _unsafe_public_fields(value: Any, *, path: str = "artifact") -> list[str]:
    """Return paths that can expose benchmark content or credentials."""
    errors: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            label = str(key)
            field_path = f"{path}.{label}"
            lowered = label.casefold()
            if lowered in _FORBIDDEN_CONTENT_FIELDS:
                errors.append(f"{field_path} must not contain raw benchmark content")
            if _SECRET_FIELD_RE.search(lowered):
                errors.append(f"{field_path} must not contain credential material")
            errors.extend(_unsafe_public_fields(item, path=field_path))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            errors.extend(_unsafe_public_fields(item, path=f"{path}[{index}]"))
    return errors


def validate_artifact(artifact: Any) -> list[str]:
    """Return deterministic errors for a public benchmark artifact.

    The guard routes each supported schema to its own strict validator. The
    retrieval-oriented ``engraphis-benchmark/v2`` envelope additionally requires
    an explicit measurement scope. Hosted productivity evidence uses its own
    aggregate-only schema and checksum contract.
    """
    errors: list[str] = []
    if not isinstance(artifact, Mapping):
        return ["artifact must be an object"]
    if artifact.get("schema") == _HOSTED_EVIDENCE_SCHEMA:
        try:
            from eval.hosted_evidence import public_json

            public_json(artifact)
        except (TypeError, ValueError) as exc:
            return [f"hosted evidence validation failed: {exc}"]
        return []
    if artifact.get("schema") != "engraphis-benchmark/v2":
        return [
            "artifact.schema must equal engraphis-benchmark/v2 or "
            f"{_HOSTED_EVIDENCE_SCHEMA}"
        ]

    suite = artifact.get("suite")
    if not isinstance(suite, Mapping):
        errors.append("artifact.suite must be an object")
    else:
        for field in ("name", "dataset"):
            if not _nonempty_string(suite.get(field)):
                errors.append(f"artifact.suite.{field} must be a non-empty string")
        if not _is_sha256(suite.get("sha256")):
            errors.append("artifact.suite.sha256 must be a lowercase SHA-256 digest")

    system = artifact.get("system")
    if not isinstance(system, Mapping):
        errors.append("artifact.system must be an object")
    else:
        if not _nonempty_string(system.get("git_commit")):
            errors.append("artifact.system.git_commit must be a non-empty string")
        declared_config_sha256 = system.get("config_sha256")
        if not _is_sha256(declared_config_sha256):
            errors.append("artifact.system.config_sha256 must be a lowercase SHA-256 digest")

    environment = artifact.get("environment")
    if not isinstance(environment, Mapping):
        errors.append("artifact.environment must be an object")
    else:
        for field in ("python", "implementation", "platform", "machine"):
            if not _nonempty_string(environment.get(field)):
                errors.append(f"artifact.environment.{field} must be a non-empty string")

    protocol = artifact.get("protocol")
    if not isinstance(protocol, Mapping):
        errors.append("artifact.protocol must be an object")
    else:
        command = protocol.get("command")
        if (
            not isinstance(command, list)
            or not command
            or not all(_nonempty_string(item) for item in command)
        ):
            errors.append("artifact.protocol.command must be a non-empty string array")
        config = protocol.get("config")
        if not isinstance(config, Mapping):
            errors.append("artifact.protocol.config must be an object")
        else:
            try:
                actual_config_sha256 = _canonical_sha256(dict(config))
            except (TypeError, ValueError):
                errors.append("artifact.protocol.config must be strict JSON")
            else:
                if isinstance(system, Mapping) and (
                    system.get("config_sha256") != actual_config_sha256
                ):
                    errors.append(
                        "artifact.system.config_sha256 must match artifact.protocol.config"
                    )
        accounting = protocol.get("token_accounting")
        if not isinstance(accounting, Mapping):
            errors.append("artifact.protocol.token_accounting must be an object")
        else:
            for field in ("identity", "scope", "method"):
                if not _nonempty_string(accounting.get(field)):
                    errors.append(f"artifact.protocol.token_accounting.{field} is required")

    scope = _artifact_scope(artifact)
    if scope not in _SCOPES:
        errors.append(
            "artifact measurement scope must be one of: " + ", ".join(sorted(_SCOPES))
        )

    records = artifact.get("records")
    if not isinstance(records, list):
        errors.append("artifact.records must be an array")
    if isinstance(protocol, Mapping) and isinstance(records, list):
        for field in ("n_total", "n_scored"):
            if not _nonnegative_integer(protocol.get(field)):
                errors.append(f"artifact.protocol.{field} must be a non-negative integer")
        if _nonnegative_integer(protocol.get("n_total")) and protocol["n_total"] != len(records):
            errors.append("artifact.protocol.n_total must equal artifact.records length")
        if (
            _nonnegative_integer(protocol.get("n_total"))
            and _nonnegative_integer(protocol.get("n_scored"))
            and protocol["n_scored"] > protocol["n_total"]
        ):
            errors.append("artifact.protocol.n_scored must not exceed artifact.protocol.n_total")

    privacy = artifact.get("privacy")
    if not isinstance(privacy, Mapping) or privacy.get("raw_query_policy") != "redacted_sha256":
        errors.append("artifact.privacy.raw_query_policy must be redacted_sha256")
    errors.extend(_unsafe_public_fields(artifact))
    return errors


def validate_claim(claim: Any, artifact: Any) -> list[str]:
    """Return errors when one public claim lacks provenance or exceeds its scope."""
    errors: list[str] = []
    if not isinstance(claim, Mapping):
        return ["claim must be an object"]
    if not _nonempty_string(claim.get("text")):
        errors.append("claim.text must be a non-empty string")
    scope = claim.get("evidence_scope")
    if scope not in _SCOPES:
        errors.append("claim.evidence_scope must be one of: " + ", ".join(sorted(_SCOPES)))
    if not isinstance(artifact, Mapping):
        errors.append("claim cannot be checked without an artifact object")
        return errors
    artifact_errors = validate_artifact(artifact)
    if artifact_errors:
        errors.extend(f"artifact: {error}" for error in artifact_errors)
    artifact_scope = _artifact_scope(artifact)
    if scope in _SCOPES and artifact_scope in _SCOPES and scope != artifact_scope:
        errors.append("claim.evidence_scope must match the artifact measurement scope")

    provenance = claim.get("provenance")
    if not isinstance(provenance, Mapping):
        errors.append("claim.provenance must be an object")
    else:
        expected = _artifact_provenance(artifact)
        for field in _PROVENANCE_FIELDS:
            if provenance.get(field) != expected.get(field):
                errors.append(f"claim.provenance.{field} must match the artifact")

    metrics = claim.get("metrics")
    artifact_metrics = artifact.get("metrics")
    if metrics is not None:
        if (
            not isinstance(metrics, list)
            or not metrics
            or not all(_nonempty_string(item) for item in metrics)
        ):
            errors.append("claim.metrics must be a non-empty string array when supplied")
        elif not isinstance(artifact_metrics, Mapping):
            errors.append("claim.metrics requires artifact.metrics")
        else:
            for metric in metrics:
                if metric not in artifact_metrics:
                    errors.append(f"claim metric is absent from artifact.metrics: {metric}")

    claim_kind = claim.get("claim_kind", "result")
    if claim_kind not in {"result", "limitation"}:
        errors.append("claim.claim_kind must be result or limitation")
    if (
        scope == "retrieval_only"
        and claim_kind == "result"
        and isinstance(claim.get("text"), str)
        and _RETRIEVAL_ONLY_OVERCLAIM_RE.search(claim["text"])
    ):
        errors.append(
            "retrieval_only claims cannot assert answer quality, task outcomes, cost, or latency"
        )
    return errors


def validate_manifest(manifest: Any) -> list[str]:
    """Validate the comparative series that authorizes public publication.

    A publication series is intentionally separate from an individual execution
    manifest and an evidence artifact. It describes the complete protected
    comparison that produced the artifacts, while :func:`validate_artifact`
    checks one public evidence envelope. The contract mirrors the canonical
    profile in ``eval.benchmark`` but stays standard-library-only so it can be
    used before the benchmark stack is installed.

    The expected shape is ``engraphis-public-benchmark-series/v1`` with ``source``,
    ``benchmark``, ``profile``, and ``artifacts`` objects.  ``profile`` contains
    the immutable upstream revisions, ``benchmark`` contains the holdout,
    baseline, and budget declarations, and ``artifacts`` names both private and
    public output paths.  Paths are metadata only; this validator does not read
    or create them.
    """
    errors: list[str] = []
    if not isinstance(manifest, Mapping):
        return ["manifest must be an object"]
    if manifest.get("schema") != _MANIFEST_SCHEMA:
        errors.append(f"manifest.schema must equal {_MANIFEST_SCHEMA}")

    source = manifest.get("source")
    if not isinstance(source, Mapping):
        errors.append("manifest.source must be an object")
    else:
        commit = source.get("git_commit")
        if not isinstance(commit, str) or not _IMMUTABLE_REVISION_RE.fullmatch(commit):
            errors.append(
                "manifest.source.git_commit must be an immutable lowercase 40-character commit"
            )
        if source.get("git_dirty") is not False:
            errors.append("manifest.source.git_dirty must be false")

    benchmark = manifest.get("benchmark")
    if not isinstance(benchmark, Mapping):
        errors.append("manifest.benchmark must be an object")
        benchmark = {}

    baselines = benchmark.get("baselines")
    if not isinstance(baselines, list) or not baselines or not all(
        isinstance(item, str) and bool(item.strip()) for item in baselines
    ):
        errors.append("manifest.benchmark.baselines must be a non-empty string array")
    else:
        if len(baselines) != len(set(baselines)):
            errors.append("manifest.benchmark.baselines must not contain duplicates")
        for label in sorted(_REQUIRED_BASELINES.difference(baselines)):
            errors.append(f"manifest.benchmark.baselines is missing required baseline: {label}")

    if benchmark.get("token_budgets") != list(_CANONICAL_TOKEN_BUDGETS):
        errors.append(
            "manifest.benchmark.token_budgets must be the canonical fixed budgets"
        )
    if benchmark.get("holdout") is not True:
        errors.append("manifest.benchmark.holdout must be true")

    profile = manifest.get("profile")
    if not isinstance(profile, Mapping):
        errors.append("manifest.profile must be an object")
    else:
        for section, field in _IMMUTABLE_REVISION_FIELDS:
            section_value = profile.get(section)
            value = section_value.get(field) if isinstance(section_value, Mapping) else None
            if not isinstance(value, str) or not _IMMUTABLE_REVISION_RE.fullmatch(value):
                errors.append(
                    f"manifest.profile.{section}.{field} must be an immutable lowercase "
                    "40-character revision"
                )
        if profile.get("token_budgets") != list(_CANONICAL_TOKEN_BUDGETS):
            errors.append(
                "manifest.profile.token_budgets must be the canonical fixed budgets"
            )

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, Mapping):
        errors.append("manifest.artifacts must be an object")
    else:
        private_path = artifacts.get("private")
        public_path = artifacts.get("public")
        if not _nonempty_string(private_path):
            errors.append("manifest.artifacts.private must be an explicit non-empty path")
        if not _nonempty_string(public_path):
            errors.append("manifest.artifacts.public must be an explicit non-empty path")
        if (
            _nonempty_string(private_path)
            and _nonempty_string(public_path)
            and private_path.strip() == public_path.strip()
        ):
            errors.append("manifest.artifacts.private and public paths must differ")

    errors.extend(_unsafe_public_fields(manifest, path="manifest"))
    return errors


def assert_manifest_ready(manifest: Any) -> None:
    """Raise ``ValueError`` when a benchmark series is not publication-ready."""
    errors = validate_manifest(manifest)
    if errors:
        raise ValueError("public benchmark series validation failed:\n" + "\n".join(errors))


def validate_public_readiness(artifact: Any, claims: Sequence[Any] = ()) -> list[str]:
    """Validate an artifact and all claims, preserving stable error ordering."""
    errors = validate_artifact(artifact)
    if (
        isinstance(artifact, Mapping)
        and artifact.get("schema") == _HOSTED_EVIDENCE_SCHEMA
        and claims
    ):
        errors.append(
            "hosted evidence claims require a hosted claim schema; no claims are supported here"
        )
        return errors
    for index, claim in enumerate(claims):
        errors.extend(f"claims[{index}]: {error}" for error in validate_claim(claim, artifact))
    return errors


def assert_public_ready(artifact: Any, claims: Sequence[Any] = ()) -> None:
    """Raise ``ValueError`` when an artifact or claim is not public-ready."""
    errors = validate_public_readiness(artifact, claims)
    if errors:
        raise ValueError("public benchmark readiness failed:\n" + "\n".join(errors))


def _load_claims(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, Mapping) and isinstance(value.get("claims"), list):
        return value["claims"]
    raise ValueError("claims input must be an array or an object with a claims array")


def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate public benchmark evidence boundaries.")
    parser.add_argument("--artifact", help="JSON benchmark artifact")
    parser.add_argument("--claims", help="JSON array of public claims")
    parser.add_argument(
        "--series",
        help="JSON manifest for the complete comparative public benchmark series",
    )
    args = parser.parse_args(argv)
    try:
        if not args.artifact and not args.series:
            raise ValueError("one of --artifact or --series is required")
        if args.claims and not args.artifact:
            raise ValueError("--claims requires --artifact")
        errors: list[str] = []
        if args.series:
            series = json.loads(Path(args.series).read_text(encoding="utf-8"))
            errors.extend(f"series: {error}" for error in validate_manifest(series))
        if args.artifact:
            artifact = json.loads(Path(args.artifact).read_text(encoding="utf-8"))
            claims = []
            if args.claims:
                claims = _load_claims(json.loads(Path(args.claims).read_text(encoding="utf-8")))
            errors.extend(validate_public_readiness(artifact, claims))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"public readiness error: {exc}", file=sys.stderr)
        return 2
    if errors:
        print("public benchmark readiness failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("public benchmark readiness: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
