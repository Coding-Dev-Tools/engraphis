"""Fail a CI job when a CodeQL SARIF directory contains any findings."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


MAX_REPORTED_FINDINGS = 50


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


def findings_in(path: Path) -> list[str]:
    """Return bounded, human-readable findings from one SARIF file."""

    document = json.loads(path.read_text(encoding="utf-8"))
    findings: list[str] = []
    for run in document.get("runs", []):
        for result in run.get("results", []):
            rule = result.get("ruleId", "<unknown-rule>")
            message = result.get("message", {}).get("text", "<no message>")
            flow = _code_flows(result)
            suffix = f" [flow: {'; '.join(flow)}]" if flow else ""
            findings.append(f"{rule} at {_location(result)}: {message}{suffix}")
    return findings


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: check_codeql_sarif.py <SARIF directory>", file=sys.stderr)
        return 2
    directory = Path(args[0])
    sarif_files = sorted(directory.rglob("*.sarif"))
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
