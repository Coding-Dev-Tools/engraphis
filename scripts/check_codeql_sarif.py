"""Fail a CI job when a CodeQL SARIF directory contains any findings."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


MAX_REPORTED_FINDINGS = 50


def _location(result: dict[str, Any]) -> str:
    locations = result.get("locations")
    if not isinstance(locations, list) or not locations:
        return "<unknown>"
    physical = locations[0].get("physicalLocation", {})
    artifact = physical.get("artifactLocation", {})
    region = physical.get("region", {})
    path = artifact.get("uri", "<unknown>")
    line = region.get("startLine")
    return f"{path}:{line}" if isinstance(line, int) else str(path)


def findings_in(path: Path) -> list[str]:
    """Return bounded, human-readable findings from one SARIF file."""

    document = json.loads(path.read_text(encoding="utf-8"))
    findings: list[str] = []
    for run in document.get("runs", []):
        for result in run.get("results", []):
            rule = result.get("ruleId", "<unknown-rule>")
            message = result.get("message", {}).get("text", "<no message>")
            findings.append(f"{rule} at {_location(result)}: {message}")
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
