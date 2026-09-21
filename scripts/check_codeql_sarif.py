"""Fail a CI job when a CodeQL SARIF directory contains any findings."""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit


MAX_REPORTED_FINDINGS = 50
_APPROVED_WEAK_HASH_SITES = {
    "engraphis/backends/embedder_deterministic.py": (
        "_feature_hash",
        frozenset({36}),
        "sha1",
    ),
    "engraphis/backends/codegraph.py": (
        "_content_hash",
        frozenset({182, 183}),
        "sha1",
    ),
    "eval/benchmark_campaign.py": (
        "digest",
        frozenset({80}),
        "sha256",
    ),
}


def _physical_location(physical: Any) -> str:
    if not isinstance(physical, dict):
        return "<unknown>"
    artifact = physical.get("artifactLocation", {})
    region = physical.get("region", {})
    path = artifact.get("uri", "<unknown>")
    line = region.get("startLine")
    return f"{path}:{line}" if isinstance(line, int) else str(path)


def _location(result: dict[str, Any]) -> str:
    locations = result.get("locations")
    if not isinstance(locations, list) or not locations:
        return "<unknown>"
    return _physical_location(locations[0].get("physicalLocation"))


def _code_flows(result: dict[str, Any]) -> list[str]:
    """Return compact source-to-sink paths from a SARIF path-problem result."""
    flows: list[str] = []
    for code_flow in result.get("codeFlows", []):
        if not isinstance(code_flow, dict):
            continue
        for thread_flow in code_flow.get("threadFlows", []):
            if not isinstance(thread_flow, dict):
                continue
            locations = thread_flow.get("locations", [])
            if not isinstance(locations, list) or not locations:
                continue
            endpoints = []
            for location in (locations[0], locations[-1]):
                if not isinstance(location, dict):
                    continue
                entry = location.get("location", location)
                if isinstance(entry, dict):
                    endpoints.append(_physical_location(entry.get("physicalLocation")))
            if endpoints:
                flows.append(" -> ".join(endpoints))
    return flows


# CodeQL flags these intentional non-security hashes on some analyzer releases.
# The release gate waives only the exact source call expressions; every other
# result for the same rule remains release-blocking.


def _normalized_repository_path(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    parsed = urlsplit(value)
    if parsed.scheme and parsed.scheme != "file":
        return None
    path = unquote(parsed.path if parsed.scheme else value).replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    for approved in _APPROVED_WEAK_HASH_SITES:
        if path == approved or path.endswith("/" + approved):
            return approved
    return None

def _approved_source_identity(
    path: str, line: int, function_name: str, algorithm_name: str
) -> bool:
    """Confirm the waived result still names the intended non-security hash call."""
    try:
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return False
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != function_name:
            continue
        for candidate in ast.walk(node):
            if not isinstance(candidate, ast.Call):
                continue
            if not (
                    candidate.lineno <= line <= (candidate.end_lineno or candidate.lineno)):
                continue
            target = candidate.func
            if not (
                    isinstance(target, ast.Attribute)
                    and target.attr == algorithm_name
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "hashlib"):
                continue
            return any(
                keyword.arg == "usedforsecurity"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is False
                for keyword in candidate.keywords
            )
    return False


def _is_approved_weak_hash(result: dict[str, Any]) -> bool:
    if result.get("ruleId") != "py/weak-sensitive-data-hashing":
        return False
    locations = result.get("locations")
    if not isinstance(locations, list) or len(locations) != 1:
        return False
    physical = locations[0].get("physicalLocation")
    if not isinstance(physical, dict):
        return False
    artifact = physical.get("artifactLocation")
    region = physical.get("region")
    if not isinstance(artifact, dict) or not isinstance(region, dict):
        return False
    path = _normalized_repository_path(artifact.get("uri"))
    line = region.get("startLine")
    approved = _APPROVED_WEAK_HASH_SITES.get(path) if path is not None else None
    return (
        approved is not None
        and path is not None
        and isinstance(line, int)
        and line in approved[1]
        and _approved_source_identity(path, line, approved[0], approved[2])
    )


def findings_in(path: Path) -> list[str]:
    """Return bounded, human-readable findings from one SARIF file."""

    document = json.loads(path.read_text(encoding="utf-8"))
    findings: list[str] = []
    for run in document.get("runs", []):
        for result in run.get("results", []):
            rule = result.get("ruleId", "<unknown-rule>")
            if _is_approved_weak_hash(result):
                continue
            message = result.get("message", {}).get("text", "<no message>")
            flow = _code_flows(result)
            suffix = f" [flow: {'; '.join(flow)}]" if flow else ""
            findings.append(f"{rule} at {_location(result)}: {message}{suffix}")
    return findings


def _sarif_files(directory: Path) -> list[Path]:
    return sorted({
        *directory.rglob("*.sarif"),
        *directory.rglob("*.sarif.json"),
    })


def filter_approved_results(input_directory: Path, output_directory: Path) -> int:
    """Copy SARIF while removing only the exact approved non-security hashes.

    The CodeQL alert-suppression query records an in-source suppression in SARIF,
    but GitHub's code-scanning check still treats that raw result as a high alert.
    Keep the raw SARIF gate above this step release-blocking, then upload a copy
    with only the source-identity-verified non-security digest results removed.
    """
    sarif_files = _sarif_files(input_directory)
    if not sarif_files:
        print(
            f"CodeQL filter: no SARIF files found under {input_directory}",
            file=sys.stderr,
        )
        return 2
    output_directory.mkdir(parents=True, exist_ok=True)
    removed = 0
    for source in sarif_files:
        document = json.loads(source.read_text(encoding="utf-8"))
        for run in document.get("runs", []):
            if not isinstance(run, dict) or not isinstance(run.get("results"), list):
                continue
            kept = []
            for result in run["results"]:
                if isinstance(result, dict) and _is_approved_weak_hash(result):
                    removed += 1
                else:
                    kept.append(result)
            run["results"] = kept
        destination = output_directory / source.relative_to(input_directory)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(f"CodeQL filter: removed {removed} approved non-security result(s)")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args[:1] == ["--filter-approved"]:
        if len(args) != 3:
            print(
                "usage: check_codeql_sarif.py --filter-approved "
                "<input-directory> <output-directory>",
                file=sys.stderr,
            )
            return 2
        return filter_approved_results(Path(args[1]), Path(args[2]))
    if len(args) != 1:
        print(
            "usage: check_codeql_sarif.py <SARIF directory> | "
            "--filter-approved <input-directory> <output-directory>",
            file=sys.stderr,
        )
        return 2
    directory = Path(args[0])
    sarif_files = _sarif_files(directory)
    if not sarif_files:
        print(f"CodeQL gate: no SARIF files found under {directory}", file=sys.stderr)
        return 2
    findings = [
        finding
        for sarif_file in sarif_files
        for finding in findings_in(sarif_file)
    ]
    if findings:
        print(f"CodeQL gate: {len(findings)} finding(s)", file=sys.stderr)
        for finding in findings[:MAX_REPORTED_FINDINGS]:
            print(f"- {finding}", file=sys.stderr)
        hidden = len(findings) - MAX_REPORTED_FINDINGS
        if hidden > 0:
            print(f"- ... {hidden} additional finding(s) omitted", file=sys.stderr)
        return 1
    print(f"CodeQL gate: clean ({len(sarif_files)} SARIF file(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
