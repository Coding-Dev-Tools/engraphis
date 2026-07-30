"""Create a deterministic, content-safe manifest for a public release candidate.

The evidence is deliberately limited to files and commands in this repository.  It is
not an operational attestation for the hosted control plane, payment provider, or a
customer deployment.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Iterable, Optional

try:  # Python 3.11+
    import tomllib
except ImportError:  # pragma: no cover - supported Python 3.9/3.10
    tomllib = None


FORMAT = "engraphis-release-evidence/2"
PACKAGE = "engraphis"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_TAG = re.compile(r"v([0-9]+\.[0-9]+\.[0-9]+)\Z")
_SAFE_PATH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*\Z")
_SECRET_NAME = re.compile(
    r"(?:secret|token|password|credential|api[-_]?key|private[-_]?key)", re.IGNORECASE
)
_SECRET_VALUE = re.compile(
    r"(?:-----BEGIN [A-Z ]*PRIVATE KEY-----|\b(?:sk|rk|pk)_[A-Za-z0-9_-]{16,}\b|"
    r"\bgh[pous]_[A-Za-z0-9_]{16,}\b|\bgithub_pat_[A-Za-z0-9_]{16,}\b|"
    r"\bAKIA[0-9A-Z]{16}\b|\bengr_(?:ct|rt|at)_[A-Za-z0-9_-]{12,}\b)",
    re.IGNORECASE,
)


class EvidenceError(ValueError):
    """A release-evidence input is malformed, incomplete, or unsafe to publish."""


def canonical_json_bytes(value: Any) -> bytes:
    """Return one stable UTF-8 encoding suitable for a reproducible artifact."""
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode(
        "utf-8"
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _relative_path(root: Path, path: Path) -> str:
    try:
        relative = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise EvidenceError("evidence inputs must stay within the repository") from exc
    if not _SAFE_PATH.fullmatch(relative) or _SECRET_NAME.search(relative):
        raise EvidenceError("evidence input path is unsafe to publish")
    return relative


def _reject_secret_like(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise EvidenceError("evidence object keys must be strings")
            if _SECRET_NAME.search(key):
                raise EvidenceError("evidence must not include secret-like fields")
            _reject_secret_like(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_secret_like(item)
    elif isinstance(value, str) and _SECRET_VALUE.search(value):
        raise EvidenceError("evidence must not include secret-like values")


def _file_input(root: Path, relative: str) -> dict[str, str]:
    path = root / relative
    if not path.is_file():
        raise EvidenceError("required release input is missing: %s" % relative)
    return {"path": _relative_path(root, path), "sha256": _sha256(path)}


def project_version(root: Path) -> str:
    pyproject = root / "pyproject.toml"
    try:
        raw = pyproject.read_text(encoding="utf-8")
        if tomllib is not None:
            version = tomllib.loads(raw)["project"]["version"]
        else:
            project = re.search(r"(?ms)^\[project\]\s*(.*?)(?=^\[|\Z)", raw)
            match = (
                re.search(r'(?m)^version\s*=\s*"([^"]+)"\s*$', project.group(1))
                if project else None
            )
            if match is None:
                raise KeyError("project.version")
            version = match.group(1)
    except (KeyError, OSError, ValueError) as exc:
        raise EvidenceError("pyproject project.version is required") from exc
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise EvidenceError("project.version must use stable semantic version syntax")
    return version


def git_commit(root: Path) -> str:
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise EvidenceError("could not determine the release commit") from exc
    return validate_commit(commit)


def validate_commit(commit: str) -> str:
    if not isinstance(commit, str) or not _COMMIT.fullmatch(commit):
        raise EvidenceError("release commit must be a lowercase 40-character SHA-1")
    return commit


def validate_tag(tag: str, version: str) -> str:
    """Require a canonical release tag that exactly names the package version."""
    if not isinstance(tag, str):
        raise EvidenceError("release tag must be a stable semantic version tag")
    match = _TAG.fullmatch(tag)
    if match is None or match.group(1) != version:
        raise EvidenceError("release tag must exactly match the package version")
    return tag


def distribution_artifacts(directory: Path, version: str) -> list[dict[str, Any]]:
    if not directory.is_dir():
        raise EvidenceError("distribution directory is missing")
    allowed = (".whl", ".tar.gz")
    paths = sorted(path for path in directory.iterdir() if path.is_file())
    if not paths:
        raise EvidenceError("distribution directory is empty")
    artifacts = []
    for path in paths:
        name = path.name
        if not name.endswith(allowed) or not _SAFE_PATH.fullmatch(name) or _SECRET_NAME.search(name):
            raise EvidenceError("distribution directory contains an unsafe non-package file")
        if not name.startswith(PACKAGE + "-" + version + ".") and not name.startswith(
            PACKAGE + "-" + version + "-"
        ):
            raise EvidenceError("distribution filename does not match package version")
        artifacts.append({"filename": name, "bytes": path.stat().st_size, "sha256": _sha256(path)})
    return artifacts


def sbom_artifact(root: Path, path: Path) -> dict[str, Any]:
    """Validate and fingerprint the generated CycloneDX SBOM before publishing it."""
    if not path.is_file():
        raise EvidenceError("SBOM is missing")
    relative = _relative_path(root, path)
    if not path.name.endswith(".cdx.json"):
        raise EvidenceError("SBOM filename must use the .cdx.json suffix")
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceError("SBOM must be valid UTF-8 JSON") from exc
    if not isinstance(parsed, dict) or parsed.get("bomFormat") != "CycloneDX":
        raise EvidenceError("SBOM must be a CycloneDX JSON document")
    if not isinstance(parsed.get("specVersion"), str) or not isinstance(parsed.get("components"), list):
        raise EvidenceError("SBOM is missing required CycloneDX fields")
    _reject_secret_like(parsed)
    return {
        "format": "CycloneDX",
        "spec_version": parsed["specVersion"],
        "filename": path.name,
        "path": relative,
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def check_manifest(root: Path) -> dict[str, list[dict[str, Any]]]:
    """Return the exact public checks represented by this evidence format."""
    return {
        "tests": [
            {"id": "ruff", "command": ["ruff", "check", "."], "inputs": []},
            {
                "id": "pytest",
                "command": ["python", "-m", "pytest", "-o", "addopts=", "tests/", "-q", "-rs"],
                "inputs": [],
            },
            {
                "id": "privacy-boundary",
                "command": [
                    "python", "-m", "pytest", "-o", "addopts=",
                    "tests/test_public_research_boundary.py", "-q",
                ],
                "inputs": [],
            },
            {
                "id": "token-efficiency",
                "command": [
                    "python", "-m", "pytest", "-o", "addopts=",
                    "tests/test_compact_recall.py", "tests/test_eval_performance.py", "-q",
                ],
                "inputs": [],
            },
            {
                "id": "benchmark-schema-evidence",
                "command": [
                    "python", "-m", "pytest", "-o", "addopts=",
                    "tests/test_eval_harness.py", "tests/test_benchmark_evidence.py", "-q",
                ],
                "inputs": [],
            },
            {
                "id": "browser-e2e",
                "command": ["npm", "run", "test:e2e"],
                "workflow_job": "browser-accessibility",
                "inputs": [],
            },
            {
                "id": "dependency-audit",
                "command": ["python", "-m", "pip_audit", "--local"],
                "inputs": [],
            },
            {
                "id": "container-smoke",
                "command": ["docker", "build", "-t", "engraphis:release", "."],
                "workflow_job": "docker-smoke",
                "workflow_steps": [
                    "Verify production image OCR runtime",
                    "Audit production image dependencies",
                    "Run customer-mode readiness smoke",
                ],
                "inputs": [],
            },
        ],
        "evaluations": [
            {
                "id": "retrieval-sample",
                "command": [
                    "python", "-m", "eval.harness", "--dataset", "eval/datasets/sample.jsonl", "--k", "5"
                ],
                "inputs": [_file_input(root, "eval/datasets/sample.jsonl")],
            },
            {
                "id": "retrieval-codemem",
                "command": [
                    "python", "-m", "eval.harness", "--dataset", "eval/datasets/codemem.jsonl", "--k", "5"
                ],
                "inputs": [_file_input(root, "eval/datasets/codemem.jsonl")],
            },
            {
                "id": "retrieval-ablation",
                "command": ["python", "-m", "eval.ablation"],
                "inputs": [
                    _file_input(root, "eval/datasets/sample.jsonl"),
                    _file_input(root, "eval/datasets/graph_multihop.jsonl"),
                ],
            },
        ],
    }


def _verified_check_ids(manifest: dict[str, list[dict[str, Any]]]) -> set[str]:
    return {check["id"] for group in manifest.values() for check in group}


def build_evidence(
    root: Path,
    distribution_directory: Path,
    *,
    commit: str,
    tag: str,
    sbom: Path,
    verified_checks: Iterable[str] = (),
) -> dict[str, Any]:
    """Build deterministic evidence; callers state which fixed checks they ran."""
    root = root.resolve()
    version = project_version(root)
    manifest = check_manifest(root)
    expected = _verified_check_ids(manifest)
    verified = sorted(set(verified_checks))
    if any(not isinstance(item, str) for item in verified) or set(verified) != expected:
        missing = sorted(expected - set(verified))
        unexpected = sorted(set(verified) - expected)
        details = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if unexpected:
            details.append("unexpected=" + ",".join(unexpected))
        raise EvidenceError("verified checks must exactly match the public manifest (" + "; ".join(details) + ")")
    checked_commit = validate_commit(commit)
    checked_tag = validate_tag(tag, version)
    evidence = {
        "format": FORMAT,
        "package": {"name": PACKAGE, "version": version},
        "commit": checked_commit,
        "tag": checked_tag,
        "provenance": {
            "source": {"commit": checked_commit, "tag": checked_tag},
            "builder": {
                "workflow": ".github/workflows/release.yml",
                "job": "release-evidence",
                "completed_gate_jobs": [
                    "build", "python-matrix", "browser-accessibility", "docker-smoke",
                ],
                "sbom_generator": {
                    "name": "cyclonedx-bom",
                    "version": "7.3.0",
                    "command": [
                        "cyclonedx-py", "environment", "--output-reproducible", "--of", "JSON",
                        "--pyproject", "pyproject.toml",
                    ],
                },
            },
        },
        "source_inputs": [
            _file_input(root, "pyproject.toml"),
            _file_input(root, "LICENSE"),
            _file_input(root, "NOTICE"),
        ],
        "artifacts": distribution_artifacts(distribution_directory, version),
        "sbom": sbom_artifact(root, sbom),
        "checks": manifest,
        "verified_checks": verified,
        "limitations": [
            "This evidence attests only to the named source inputs, distributions, SBOM, and checks.",
            "It does not attest to publication, release hosting, hosted services, payments, deployments, or runtime data.",
            "The SBOM describes the Python environment used for this build; it is not an operating-system or container SBOM.",
        ],
    }
    _reject_secret_like(evidence)
    return evidence


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, required=True, help="directory containing wheel and sdist")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--commit", help="release commit; defaults to git HEAD")
    parser.add_argument("--tag", required=True, help="release tag matching pyproject project.version")
    parser.add_argument("--sbom", type=Path, required=True, help="generated CycloneDX JSON SBOM")
    parser.add_argument("--verified-check", action="append", default=[], help="one completed public check id")
    parser.add_argument("--output", type=Path, help="write canonical JSON instead of stdout")
    args = parser.parse_args(argv)
    try:
        root = args.root.resolve()
        evidence = build_evidence(
            root, args.dist.resolve(), commit=args.commit or git_commit(root),
            tag=args.tag, sbom=args.sbom.resolve(),
            verified_checks=args.verified_check,
        )
        encoded = canonical_json_bytes(evidence)
        if args.output:
            args.output.write_bytes(encoded)
        else:
            __import__("sys").stdout.buffer.write(encoded)
    except EvidenceError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
