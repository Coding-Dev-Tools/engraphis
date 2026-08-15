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
from urllib.parse import parse_qs, urlsplit

try:  # Python 3.11+
    import tomllib
except ImportError:  # pragma: no cover - supported Python 3.9/3.10
    tomllib = None


FORMAT = "engraphis-release-evidence/3"
PACKAGE = "engraphis"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_TAG = re.compile(r"v([0-9]+\.[0-9]+(?:\.[0-9]+)?)\Z")
_SAFE_PATH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*\Z")
_PACKAGE_LOCK_LINE = re.compile(r"([A-Za-z0-9][A-Za-z0-9_.-]*)==([^\s]+)\Z")
_IMAGE_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_BUILDER_IMAGE = "github-hosted:ubuntu-latest/python-3.11"
_BUILDER_TOOLCHAIN = {
    "build": "1.5.0",
    "pip": "26.2",
    "setuptools": "83.0.0",
    "wheel": "0.47.0",
}
_GRYPE_VERSION = "0.110.0"
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
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+(?:\.[0-9]+)?", version):
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


def repair_run_candidates(runs: Any, tag: str, commit: str) -> list[str]:
    """Return matching push-run IDs newest first; artifact viability is checked by the caller."""
    if _TAG.fullmatch(tag) is None:
        raise EvidenceError("repair tag must use stable semantic version syntax")
    validate_commit(commit)
    if not isinstance(runs, list):
        raise EvidenceError("workflow runs must be a JSON array")
    matches = []
    for run in runs:
        if not isinstance(run, dict):
            continue
        run_id = run.get("databaseId")
        created_at = run.get("createdAt")
        if (
            run.get("headBranch") == tag
            and run.get("headSha") == commit
            and run.get("event") == "push"
            and isinstance(run_id, int)
            and isinstance(created_at, str)
            and created_at
        ):
            matches.append((created_at, str(run_id)))
    return [run_id for _, run_id in sorted(matches, reverse=True)]


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
        if path.is_symlink() or not path.is_file() or (
                not name.endswith(allowed) or not _SAFE_PATH.fullmatch(name)
                or _SECRET_NAME.search(name)
        ):
            raise EvidenceError("distribution directory contains an unsafe non-package file")
        if not name.startswith(PACKAGE + "-" + version + ".") and not name.startswith(
            PACKAGE + "-" + version + "-"
        ):
            raise EvidenceError("distribution filename does not match package version")
        if _SECRET_VALUE.search(name):
            raise EvidenceError("evidence must not include secret-like values")
        artifacts.append({"filename": name, "bytes": path.stat().st_size, "sha256": _sha256(path)})
    if (
        sum(item["filename"].endswith(".whl") for item in artifacts) != 1
        or sum(item["filename"].endswith(".tar.gz") for item in artifacts) != 1
    ):
        raise EvidenceError(
            "distribution directory must contain exactly one wheel and one source distribution"
        )
    return artifacts


def _json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"{label} must be valid UTF-8 JSON") from exc
    if not isinstance(parsed, dict):
        raise EvidenceError(f"{label} must be a JSON object")
    return parsed


def _canonical_package_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _python_sbom_packages(document: dict[str, Any]) -> set[tuple[str, str]]:
    packages = set()
    metadata_component = document.get("metadata", {}).get("component")
    if isinstance(metadata_component, dict):
        purl = metadata_component.get("purl")
        name = metadata_component.get("name")
        version = metadata_component.get("version")
        if (
            isinstance(purl, str)
            and purl.startswith("pkg:pypi/")
            and isinstance(name, str)
            and isinstance(version, str)
        ):
            packages.add((_canonical_package_name(name), version))
    for component in document.get("components", []):
        if not isinstance(component, dict):
            continue
        purl = component.get("purl")
        name = component.get("name")
        version = component.get("version")
        if (
            isinstance(purl, str)
            and purl.startswith("pkg:pypi/")
            and isinstance(name, str)
            and isinstance(version, str)
        ):
            packages.add((_canonical_package_name(name), version))
    return packages


def sbom_artifact(root: Path, path: Path) -> dict[str, Any]:
    """Validate and fingerprint the build-captured Python CycloneDX SBOM."""
    if not path.is_file():
        raise EvidenceError("SBOM is missing")
    relative = _relative_path(root, path)
    if not path.name.endswith(".cdx.json"):
        raise EvidenceError("SBOM filename must use the .cdx.json suffix")
    parsed = _json_object(path, "SBOM")
    if parsed.get("bomFormat") != "CycloneDX":
        raise EvidenceError("SBOM must be a CycloneDX JSON document")
    if not isinstance(parsed.get("specVersion"), str) or not isinstance(
            parsed.get("components"), list):
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


def environment_lock_artifact(root: Path, path: Path, sbom: Path) -> dict[str, Any]:
    """Require the exact build freeze to equal the Python SBOM package closure."""
    if not path.is_file() or path.is_symlink():
        raise EvidenceError("build environment lock is missing")
    relative = _relative_path(root, path)
    packages: set[tuple[str, str]] = set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise EvidenceError("build environment lock must be UTF-8 text") from exc
    if not lines:
        raise EvidenceError("build environment lock is empty")
    for line in lines:
        match = _PACKAGE_LOCK_LINE.fullmatch(line)
        if match is None:
            raise EvidenceError("build environment lock must contain exact name==version lines")
        package = (_canonical_package_name(match.group(1)), match.group(2))
        if package in packages:
            raise EvidenceError("build environment lock contains a duplicate package")
        packages.add(package)
    sbom_packages = _python_sbom_packages(_json_object(sbom, "SBOM"))
    if not sbom_packages.issubset(packages):
        raise EvidenceError("build environment lock and Python SBOM package closure differ")
    return {
        "filename": path.name,
        "path": relative,
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
        "package_count": len(packages),
    }


def container_sbom_artifact(
        root: Path, path: Path, image_digest: str) -> dict[str, Any]:
    """Validate a whole-image SBOM bound to one immutable production image."""
    if not _IMAGE_DIGEST.fullmatch(image_digest):
        raise EvidenceError("production image digest must be a lowercase sha256 digest")
    if not path.is_file() or path.is_symlink():
        raise EvidenceError("container SBOM is missing")
    relative = _relative_path(root, path)
    document = _json_object(path, "container SBOM")
    if document.get("bomFormat") != "CycloneDX" or not isinstance(
            document.get("components"), list):
        raise EvidenceError("container SBOM must be a CycloneDX document")
    metadata = document.get("metadata")
    component = metadata.get("component") if isinstance(metadata, dict) else None
    properties = component.get("properties") if isinstance(component, dict) else None
    digest_properties = {
        item.get("value")
        for item in properties or []
        if isinstance(item, dict) and item.get("name") == "engraphis:image-digest"
    }
    if digest_properties != {image_digest}:
        raise EvidenceError("container SBOM must bind the production image digest")
    purls: list[str] = []
    for item in document["components"]:
        if isinstance(item, dict):
            purl = item.get("purl")
            if isinstance(purl, str):
                purls.append(purl)
    os_packages = sum(purl.startswith("pkg:deb/") for purl in purls)
    python_packages = sum(purl.startswith("pkg:pypi/") for purl in purls)
    if not os_packages or not python_packages:
        raise EvidenceError("container SBOM must inventory both OS and Python packages")
    _reject_secret_like(document)
    return {
        "format": "CycloneDX",
        "filename": path.name,
        "path": relative,
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
        "image_digest": image_digest,
        "os_package_count": os_packages,
        "python_package_count": python_packages,
    }


def container_scan_artifact(root: Path, path: Path) -> dict[str, Any]:
    """Validate and fingerprint a pinned-Grype report with an identified database."""
    if not path.is_file() or path.is_symlink():
        raise EvidenceError("container vulnerability report is missing")
    relative = _relative_path(root, path)
    document = _json_object(path, "container vulnerability report")
    descriptor = document.get("descriptor")
    if not isinstance(descriptor, dict) or descriptor.get("name") != "grype":
        raise EvidenceError("container vulnerability report must identify Grype")
    if descriptor.get("version") != _GRYPE_VERSION:
        raise EvidenceError("container vulnerability report used an unexpected Grype version")
    database = descriptor.get("db")
    if not isinstance(database, dict):
        raise EvidenceError("container vulnerability report must identify its database")
    built = database.get("built")
    schema_version = database.get("schemaVersion")
    checksum = database.get("checksum")
    if not isinstance(checksum, str):
        source = database.get("from")
        if isinstance(source, str):
            checksum = parse_qs(urlsplit(source).query).get("checksum", [None])[0]
    schema_identified = (
        isinstance(schema_version, (str, int))
        and not isinstance(schema_version, bool)
        and str(schema_version)
    )
    if (
        not isinstance(built, str)
        or not built
        or not schema_identified
        or not isinstance(checksum, str)
        or not _IMAGE_DIGEST.fullmatch(checksum)
    ):
        raise EvidenceError("container vulnerability database identity is incomplete")
    _reject_secret_like(document)
    return {
        "format": "Grype JSON",
        "filename": path.name,
        "path": relative,
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
        "scanner_version": descriptor["version"],
        "database": {
            "built": built,
            "schema_version": schema_version,
            "checksum": checksum,
        },
    }


def reproducibility_artifact(
        root: Path,
        path: Path,
        expected_artifacts: dict[str, str],
) -> dict[str, Any]:
    """Validate two independent pinned builders against the shipped digests."""
    if not path.is_file() or path.is_symlink():
        raise EvidenceError("independent reproducibility evidence is missing")
    relative = _relative_path(root, path)
    document = _json_object(path, "independent reproducibility evidence")
    if document.get("format") != "engraphis-independent-reproducibility/v1":
        raise EvidenceError("independent reproducibility evidence has the wrong format")
    builders = document.get("builders")
    if not isinstance(builders, list) or len(builders) != 2:
        raise EvidenceError("independent reproducibility evidence requires two builders")
    names = set()
    environment_digests = set()
    for builder in builders:
        if not isinstance(builder, dict):
            raise EvidenceError("independent builder metadata must be an object")
        names.add(builder.get("name"))
        if builder.get("image") != _BUILDER_IMAGE:
            raise EvidenceError("independent builder image digest is not approved")
        if builder.get("python") != "3.11":
            raise EvidenceError("independent builder Python identity is incomplete")
        if builder.get("artifacts") != expected_artifacts:
            raise EvidenceError("independent builder artifacts differ from the release")
        if builder.get("toolchain") != _BUILDER_TOOLCHAIN:
            raise EvidenceError("independent builder toolchain identity is incomplete")
        environment_digest = builder.get("environment_lock_sha256")
        if not isinstance(environment_digest, str) or not _SHA256.fullmatch(environment_digest):
            raise EvidenceError("independent builder environment lock digest is invalid")
        environment_digests.add(environment_digest)
    if len(names) != 2 or None in names:
        raise EvidenceError("independent reproducibility builders must be distinct")
    if len(environment_digests) != 1:
        raise EvidenceError("independent builder environment locks differ")
    _reject_secret_like(document)
    return {
        "format": document["format"],
        "filename": path.name,
        "path": relative,
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
        "builder_image": _BUILDER_IMAGE,
        "builder_count": 2,
        "environment_lock_sha256": next(iter(environment_digests)),
    }


def check_manifest(root: Path) -> dict[str, list[dict[str, Any]]]:
    """Return the exact public checks represented by this evidence format."""
    return {
        "tests": [
            {"id": "ruff", "command": ["ruff", "check", "."], "inputs": []},
            {
                "id": "pyright-core-backends",
                "command": ["pyright"],
                "workflow_job": "build",
                "workflow_steps": ["Full release gate"],
                "inputs": [],
            },
            {
                "id": "codeql",
                "command": [
                    "python", "scripts/check_codeql_sarif.py", "codeql-results",
                ],
                "workflow_job": "code-security",
                "workflow_steps": [
                    "Initialize CodeQL",
                    "Analyze complete source tree",
                    "Require clean CodeQL results",
                ],
                "inputs": [],
            },
            {
                "id": "pytest",
                "command": ["python", "-m", "pytest", "-o", "addopts=", "tests/", "-q", "-rs"],
                "inputs": [],
            },
            {
                "id": "reproducible-distributions",
                "command": [
                    "python", "-c",
                    "compare two independent builder artifact SHA-256 maps",
                ],
                "workflow_job": "reproducibility-check",
                "workflow_steps": ["Compare independent distribution builders"],
                "inputs": [],
            },
            {
                "id": "installed-artifact-smoke",
                "command": ["python", "-m", "scripts.smoke_entry_points", "--timeout", "20"],
                "workflow_job": "build",
                "workflow_steps": ["Smoke installed wheel and source distribution"],
                "inputs": [],
            },
            {
                "id": "installed-artifact-smoke-py39",
                "command": [
                    "python", "-m", "pip", "install", "<downloaded-wheel-or-sdist>",
                ],
                "workflow_job": "artifact-core-py39",
                "workflow_steps": [
                    "Download exact release distributions",
                    "Install, verify, and smoke wheel and source distribution",
                ],
                "inputs": [],
            },
            {
                "id": "installed-artifact-platform-smoke",
                "command": [
                    "python", "-m", "scripts.smoke_entry_points", "--timeout", "20",
                ],
                "workflow_job": "installed-artifact-platform-smoke",
                "workflow_steps": [
                    "Install and smoke the downloaded wheel on Windows and macOS",
                ],
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
                "id": "encryption-at-rest",
                "command": [
                    "python", "-m", "pytest", "-o", "addopts=",
                    "tests/test_encrypted_store.py", "-q", "-rs",
                ],
                "workflow_job": "encryption",
                "inputs": [],
            },
            {
                "id": "browser-e2e",
                "command": ["npm", "run", "test:e2e"],
                "workflow_job": "browser-accessibility",
                "inputs": [],
            },
            {
                "id": "pi-extension",
                "command": ["npm", "run", "verify"],
                "workflow_job": "pi-extension",
                "workflow_steps": [
                    "Verify the publishable Pi package and live bridge",
                ],
                "inputs": [],
            },
            {
                "id": "dependency-audit",
                "command": ["python", "-m", "pip_audit", "--local", "--skip-editable"],
                "inputs": [],
            },
            {
                "id": "browser-dependency-audit",
                "command": ["npm", "audit", "--audit-level=high"],
                "workflow_job": "browser-accessibility",
                "workflow_steps": ["Audit the root browser dependency lock"],
                "inputs": [],
            },
            {
                "id": "container-smoke",
                "command": [
                    "docker", "buildx", "build", "--pull", "--load",
                    "-t", "engraphis:release", ".",
                ],
                "workflow_job": "docker-smoke",
                "workflow_steps": [
                    "Validate Compose configuration",
                    "Verify production image OCR runtime",
                    "Generate whole-image SBOM",
                    "Scan whole production image",
                    "Run customer-mode readiness smoke",
                    "Record immutable production image digest",
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
            {
                "id": "adversarial-memory-security",
                "command": ["python", "-m", "eval.adversarial_memory_security"],
                "inputs": [],
            },
            {
                "id": "reinforcement-state-transition",
                "command": ["python", "-m", "eval.reinforcement"],
                "inputs": [],
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
    environment_lock: Path,
    image_sbom: Path,
    image_digest: str,
    image_scan: Path,
    reproducibility: Path,
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
        raise EvidenceError(
            "verified checks must exactly match the public manifest ("
            + "; ".join(details) + ")"
        )
    checked_commit = validate_commit(commit)
    checked_tag = validate_tag(tag, version)
    artifacts = distribution_artifacts(distribution_directory, version)
    artifact_digests = {item["filename"]: item["sha256"] for item in artifacts}
    python_sbom = sbom_artifact(root, sbom)
    environment = environment_lock_artifact(root, environment_lock, sbom)
    container_sbom = container_sbom_artifact(root, image_sbom, image_digest)
    container_scan = container_scan_artifact(root, image_scan)
    reproducibility_record = reproducibility_artifact(
        root, reproducibility, artifact_digests,
    )
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
                    "build", "reproducibility-build", "reproducibility-check",
                    "python-matrix", "artifact-core-py39", "installed-artifact-platform-smoke",
                    "encryption", "browser-accessibility", "pi-extension", "docker-smoke",
                    "code-security",
                ],
                "python_environment_capture": {
                    "job": "build",
                    "sbom_generator": {
                        "name": "cyclonedx-bom",
                        "version": "7.3.0",
                        "command": [
                            "cyclonedx-py", "environment", "--output-reproducible",
                            "--of", "JSON", "--pyproject", "pyproject.toml",
                        ],
                    },
                },
            },
        },
        "source_inputs": [
            _file_input(root, "pyproject.toml"),
            _file_input(root, "LICENSE"),
            _file_input(root, "NOTICE"),
        ],
        "artifacts": artifacts,
        "sbom": python_sbom,
        "environment_lock": environment,
        "container": {
            "image_digest": image_digest,
            "sbom": container_sbom,
            "vulnerability_scan": container_scan,
        },
        "reproducibility": reproducibility_record,
        "checks": manifest,
        "verified_checks": verified,
        "limitations": [
            "This evidence attests only to the named source inputs, distributions, "
            "captured build environment, production image, and checks.",
            "It does not attest to publication, release hosting, hosted services, "
            "payments, deployments, or runtime data.",
            "The vulnerability result is a point-in-time scan bound to the recorded "
            "Grype version and database identity; later disclosures require rescanning.",
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
    parser.add_argument(
        "--environment-lock", type=Path, required=True,
        help="pip freeze captured in the build job",
    )
    parser.add_argument(
        "--image-sbom", type=Path, required=True,
        help="CycloneDX SBOM generated from the production image",
    )
    parser.add_argument(
        "--image-digest", required=True,
        help="immutable sha256 digest of the production image",
    )
    parser.add_argument(
        "--image-scan", type=Path, required=True,
        help="pinned Grype JSON report for the production image",
    )
    parser.add_argument(
        "--reproducibility", type=Path, required=True,
        help="two-builder reproducibility evidence",
    )
    parser.add_argument("--verified-check", action="append", default=[], help="one completed public check id")
    parser.add_argument("--output", type=Path, help="write canonical JSON instead of stdout")
    args = parser.parse_args(argv)
    try:
        root = args.root.resolve()
        evidence = build_evidence(
            root, args.dist.resolve(), commit=args.commit or git_commit(root),
            tag=args.tag, sbom=args.sbom.resolve(),
            environment_lock=args.environment_lock.resolve(),
            image_sbom=args.image_sbom.resolve(),
            image_digest=args.image_digest,
            image_scan=args.image_scan.resolve(),
            reproducibility=args.reproducibility.resolve(),
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
