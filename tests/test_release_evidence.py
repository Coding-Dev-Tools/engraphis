from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import venv
from pathlib import Path

import pytest

from scripts.release_evidence import (
    EvidenceError,
    build_evidence,
    canonical_json_bytes,
    check_manifest,
    main as release_evidence_main,
    repair_run_candidates,
)


COMMIT = "a" * 40
TAG = "v1.2.3"
IMAGE_DIGEST = "sha256:" + "c" * 64
ROOT = Path(__file__).resolve().parents[1]


def _root(tmp_path):
    (tmp_path / "eval" / "datasets").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "engraphis"\nversion = "1.2.3"\ndependencies = ["alpha-package>=1.0"]\n',
        encoding="utf-8",
    )
    (tmp_path / "LICENSE").write_text("Apache-2.0\n", encoding="utf-8")
    (tmp_path / "NOTICE").write_text("Engraphis\n", encoding="utf-8")
    (tmp_path / "eval" / "datasets" / "sample.jsonl").write_text('{"id":"sample"}\n')
    (tmp_path / "eval" / "datasets" / "codemem.jsonl").write_text('{"id":"code"}\n')
    (tmp_path / "eval" / "datasets" / "graph_multihop.jsonl").write_text(
        '{"id":"graph"}\n', encoding="utf-8"
    )
    return tmp_path


def _dist(root):
    directory = root / "dist"
    directory.mkdir()
    (directory / "engraphis-1.2.3-py3-none-any.whl").write_bytes(b"wheel")
    (directory / "engraphis-1.2.3.tar.gz").write_bytes(b"sdist")
    return directory


def _release_inputs(root, dist):
    evidence_root = root / "release-evidence"
    evidence_root.mkdir(exist_ok=True)
    sbom = evidence_root / "engraphis-1.2.3.cdx.json"
    sbom.write_text(
        json.dumps(
            {
                "bomFormat": "CycloneDX",
                "specVersion": "1.6",
                "metadata": {
                    "component": {
                        "type": "application",
                        "name": "engraphis",
                        "version": "1.2.3",
                        "purl": "pkg:pypi/engraphis@1.2.3",
                    },
                },
                "dependencies": [
                    {
                        "ref": "pkg:pypi/engraphis@1.2.3",
                        "dependsOn": ["pkg:pypi/alpha-package@1.0"],
                    },
                    {"ref": "pkg:pypi/alpha-package@1.0", "dependsOn": []},
                ],
                "components": [
                    {
                        "type": "library",
                        "name": "alpha-package",
                        "version": "1.0",
                        "purl": "pkg:pypi/alpha-package@1.0",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    environment_lock = evidence_root / "environment.lock"
    environment_lock.write_text(
        "alpha-package==1.0\nengraphis==1.2.3\n", encoding="utf-8"
    )
    image_sbom = evidence_root / "engraphis-container.cdx.json"
    image_sbom.write_text(
        json.dumps(
            {
                "bomFormat": "CycloneDX",
                "specVersion": "1.6",
                "metadata": {
                    "component": {
                        "type": "container",
                        "name": "engraphis:release",
                        "properties": [
                            {"name": "engraphis:image-digest", "value": IMAGE_DIGEST},
                        ],
                    },
                },
                "components": [
                    {"name": "libc6", "version": "2.41", "purl": "pkg:deb/debian/libc6@2.41"},
                    {
                        "name": "engraphis",
                        "version": "1.2.3",
                        "purl": "pkg:pypi/engraphis@1.2.3",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    image_scan = evidence_root / "grype.json"
    image_scan.write_text(
        json.dumps(
            {
                "descriptor": {
                    "name": "grype",
                    "version": "0.110.0",
                    "db": {
                        "built": "2026-08-08T00:00:00Z",
                        "schemaVersion": 6,
                        "checksum": "sha256:" + "d" * 64,
                    },
                },
                "matches": [],
            }
        ),
        encoding="utf-8",
    )
    artifact_digests = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in dist.iterdir()
        if path.name.endswith((".whl", ".tar.gz"))
    }
    reproducibility = evidence_root / "reproducibility.json"
    builders = [
        {
            "name": name,
            "image": "github-hosted:ubuntu-latest/python-3.11",
            "python": "3.11",
            "environment_lock_sha256": "e" * 64,
            "toolchain": {
                "build": "1.5.0",
                "pip": "26.2",
                "setuptools": "83.0.0",
                "wheel": "0.47.0",
            },
            "artifacts": artifact_digests,
        }
        for name in ("a", "b")
    ]
    reproducibility.write_text(
        json.dumps(
            {
                "format": "engraphis-independent-reproducibility/v1",
                "builders": builders,
            }
        ),
        encoding="utf-8",
    )
    return {
        "sbom": sbom,
        "environment_lock": environment_lock,
        "image_sbom": image_sbom,
        "image_digest": IMAGE_DIGEST,
        "image_scan": image_scan,
        "reproducibility": reproducibility,
    }


def _check_ids(root):
    return [entry["id"] for group in check_manifest(root).values() for entry in group]


def _build(root, dist, *, inputs=None, **overrides):
    options = {
        "commit": COMMIT,
        "tag": TAG,
        "verified_checks": _check_ids(root),
        **(inputs or _release_inputs(root, dist)),
        **overrides,
    }
    return build_evidence(root, dist, **options)


def test_release_evidence_is_canonical_and_contains_only_public_release_inputs(tmp_path):
    root = _root(tmp_path)
    dist = _dist(root)
    evidence = _build(root, dist)

    first = canonical_json_bytes(evidence)
    second = canonical_json_bytes(_build(root, dist))
    assert first == second
    assert json.loads(first) == evidence
    assert evidence["format"] == "engraphis-release-evidence/3"
    assert evidence["package"] == {"name": "engraphis", "version": "1.2.3"}
    assert evidence["commit"] == COMMIT
    assert evidence["tag"] == TAG
    assert [item["filename"] for item in evidence["artifacts"]] == [
        "engraphis-1.2.3-py3-none-any.whl", "engraphis-1.2.3.tar.gz"
    ]
    assert evidence["artifacts"][0]["sha256"] == hashlib.sha256(b"wheel").hexdigest()
    assert [item["path"] for item in evidence["source_inputs"]] == [
        "pyproject.toml", "LICENSE", "NOTICE"
    ]
    assert evidence["checks"]["evaluations"][0]["inputs"][0]["path"] == (
        "eval/datasets/sample.jsonl"
    )
    assert [
        item["path"] for item in evidence["checks"]["evaluations"][2]["inputs"]
    ] == [
        "eval/datasets/sample.jsonl",
        "eval/datasets/graph_multihop.jsonl",
    ]
    assert evidence["sbom"]["filename"] == "engraphis-1.2.3.cdx.json"
    assert evidence["environment_lock"]["package_count"] == 2
    assert evidence["container"]["image_digest"] == IMAGE_DIGEST
    assert evidence["container"]["sbom"]["os_package_count"] == 1
    assert evidence["container"]["sbom"]["python_package_count"] == 1
    assert evidence["container"]["vulnerability_scan"]["scanner_version"] == "0.110.0"
    assert evidence["reproducibility"]["builder_count"] == 2
    builder = evidence["provenance"]["builder"]
    assert builder["python_environment_capture"]["job"] == "build"
    assert (
        builder["python_environment_capture"]["sbom_generator"]["version"]
        == "7.3.0"
    )
    assert builder["job"] == "release-evidence"
    assert builder["completed_gate_jobs"] == [
        "build", "reproducibility-build", "reproducibility-check",
        "python-matrix", "artifact-core-py39", "installed-artifact-platform-smoke",
        "encryption", "browser-accessibility", "pi-extension", "docker-smoke",
        "code-security",
    ]
    checks = {check["id"]: check for check in evidence["checks"]["tests"]}
    assert "pyright-core-backends" in checks
    assert checks["codeql"]["workflow_job"] == "code-security"
    assert checks["reproducible-distributions"]["workflow_steps"] == [
        "Compare independent distribution builders",
    ]
    assert checks["installed-artifact-smoke"]["workflow_steps"] == [
        "Smoke installed wheel and source distribution",
    ]
    assert checks["installed-artifact-smoke"]["command"] == [
        "python", "-m", "scripts.smoke_entry_points", "--timeout", "20",
    ]
    assert checks["installed-artifact-smoke-py39"]["workflow_job"] == "artifact-core-py39"
    assert checks["installed-artifact-platform-smoke"]["workflow_job"] == (
        "installed-artifact-platform-smoke"
    )
    assert checks["installed-artifact-platform-smoke"]["workflow_steps"] == [
        "Install and smoke the downloaded wheel on Windows and macOS",
    ]
    assert any(check["id"] == "encryption-at-rest" for check in evidence["checks"]["tests"])
    assert any(check["id"] == "pi-extension" for check in evidence["checks"]["tests"])
    assert checks["browser-dependency-audit"]["workflow_steps"] == [
        "Audit the root browser dependency lock",
    ]
    assert evidence["checks"]["tests"][-1]["workflow_steps"] == [
        "Validate Compose configuration",
        "Verify production image OCR runtime",
        "Generate whole-image SBOM",
        "Scan whole production image",
        "Run customer-mode readiness smoke",
        "Record immutable production image digest",
    ]
    assert len(evidence["limitations"]) == 3
    assert "exported_at" not in evidence


def test_release_evidence_cli_writes_the_complete_manifest(tmp_path):
    root = _root(tmp_path)
    dist = _dist(root)
    inputs = _release_inputs(root, dist)
    output = root / "release-evidence" / "release-evidence.json"
    arguments = [
        "--root", str(root),
        "--dist", str(dist),
        "--commit", COMMIT,
        "--tag", TAG,
        "--sbom", str(inputs["sbom"]),
        "--environment-lock", str(inputs["environment_lock"]),
        "--image-sbom", str(inputs["image_sbom"]),
        "--image-digest", inputs["image_digest"],
        "--image-scan", str(inputs["image_scan"]),
        "--reproducibility", str(inputs["reproducibility"]),
        "--output", str(output),
    ]
    for check_id in _check_ids(root):
        arguments.extend(["--verified-check", check_id])

    assert release_evidence_main(arguments) == 0
    evidence = json.loads(output.read_text(encoding="utf-8"))
    assert evidence["format"] == "engraphis-release-evidence/3"
    assert evidence["container"]["image_digest"] == IMAGE_DIGEST


def test_release_evidence_fails_closed_when_checks_are_missing_or_unknown(tmp_path):
    root = _root(tmp_path)
    dist = _dist(root)
    with pytest.raises(EvidenceError, match="verified checks"):
        _build(root, dist, verified_checks=["ruff"])
    with pytest.raises(EvidenceError, match="unexpected"):
        _build(root, dist, verified_checks=_check_ids(root) + ["made-up"])

def test_release_evidence_requires_one_wheel_and_one_source_distribution(tmp_path):
    root = _root(tmp_path)
    dist = _dist(root)
    (dist / "engraphis-1.2.3.tar.gz").unlink()
    with pytest.raises(EvidenceError, match="exactly one wheel"):
        _build(root, dist)


@pytest.mark.parametrize(
    ("filename", "message"),
    [
        ("engraphis-1.2.3-token.whl", "unsafe non-package file"),
        ("engraphis-1.2.3.tar.gz", "unsafe non-package file"),
    ],
)
def test_release_evidence_rejects_unsafe_distribution_inputs(tmp_path, filename, message):
    root = _root(tmp_path)
    dist = root / "dist"
    dist.mkdir()
    (dist / filename).write_bytes(b"candidate")
    if filename.endswith(".tar.gz"):
        (dist / "notes.txt").write_text("not a package")
    with pytest.raises(EvidenceError, match=message):
        _build(root, dist)


def test_release_evidence_rejects_secret_like_values_even_in_package_filenames(tmp_path):
    root = _root(tmp_path)
    dist = _dist(root)
    (dist / ("engraphis-1.2.3-sk_" + "a" * 16 + ".whl")).write_bytes(b"not safe")
    with pytest.raises(EvidenceError, match="secret-like values"):
        _build(root, dist)


def test_release_evidence_fails_closed_for_unmatched_tags_and_invalid_sboms(tmp_path):
    root = _root(tmp_path)
    dist = _dist(root)
    inputs = _release_inputs(root, dist)
    with pytest.raises(EvidenceError, match="tag"):
        _build(root, dist, inputs=inputs, tag="v1.2.4")

    inputs["sbom"].write_text('{"bomFormat":"not-cyclonedx"}', encoding="utf-8")
    with pytest.raises(EvidenceError, match="CycloneDX"):
        _build(root, dist, inputs=inputs)


def test_release_evidence_rejects_build_freeze_that_differs_from_python_sbom(tmp_path):
    root = _root(tmp_path)
    dist = _dist(root)
    inputs = _release_inputs(root, dist)
    inputs["environment_lock"].write_text("alpha-package==2.0\n", encoding="utf-8")

    with pytest.raises(EvidenceError, match="package closure differ"):
        _build(root, dist, inputs=inputs)


def test_release_evidence_rejects_lock_with_conflicting_versions(tmp_path):
    """A lock with the same package at two versions must fail."""
    root = _root(tmp_path)
    dist = _dist(root)
    inputs = _release_inputs(root, dist)
    inputs["environment_lock"].write_text(
        "alpha-package==1.0\nalpha-package==9.9\nengraphis==1.2.3\n",
        encoding="utf-8",
    )
    with pytest.raises(EvidenceError, match="conflicting versions"):
        _build(root, dist, inputs=inputs)


def test_release_evidence_rejects_lock_packages_missing_from_sbom(tmp_path):
    """A locked package absent from the SBOM means a truncated capture.

    cyclonedx-bom 7.3.0 (``cyclonedx-py environment``) inventories the whole
    build environment, including workflow-installed tooling such as pip and
    setuptools, so a captured lock and SBOM name-set must match after
    canonicalization; a lock-only entry is how a transitive-only package
    could vanish while every direct name check still succeeds.
    """
    root = _root(tmp_path)
    dist = _dist(root)
    inputs = _release_inputs(root, dist)
    inputs["environment_lock"].write_text(
        "alpha-package==1.0\nengraphis==1.2.3\npip==26.2\nsetuptools==83.0.0\n",
        encoding="utf-8",
    )
    with pytest.raises(EvidenceError, match="truncated closure"):
        _build(root, dist, inputs=inputs)


def test_release_evidence_accepts_cyclonedx_root_component_without_purl(tmp_path):
    """cyclonedx-py uses root-component without a root PyPI PURL."""
    root = _root(tmp_path)
    dist = _dist(root)
    inputs = _release_inputs(root, dist)
    sbom_doc = json.loads(inputs["sbom"].read_text(encoding="utf-8"))
    sbom_doc["metadata"]["component"]["bom-ref"] = "root-component"
    sbom_doc["metadata"]["component"].pop("purl")
    sbom_doc["components"][0]["bom-ref"] = "alpha-component"
    sbom_doc["dependencies"][0]["ref"] = "root-component"
    sbom_doc["dependencies"][0]["dependsOn"] = ["alpha-component"]
    sbom_doc["dependencies"][1]["ref"] = "alpha-component"
    inputs["sbom"].write_text(json.dumps(sbom_doc), encoding="utf-8")

    _build(root, dist, inputs=inputs)


def test_release_evidence_rejects_mismatched_root_purl(tmp_path):
    """An optional root PURL, when present, must still identify Engraphis."""
    root = _root(tmp_path)
    dist = _dist(root)
    inputs = _release_inputs(root, dist)
    sbom_doc = json.loads(inputs["sbom"].read_text(encoding="utf-8"))
    sbom_doc["metadata"]["component"]["purl"] = "pkg:pypi/other-project@1.2.3"
    inputs["sbom"].write_text(json.dumps(sbom_doc), encoding="utf-8")

    with pytest.raises(EvidenceError, match="metadata.component does not identify"):
        _build(root, dist, inputs=inputs)


def test_release_evidence_rejects_sbom_omitting_transitive_packages(tmp_path):
    """A truncated SBOM keeping root + direct deps but dropping a transitive
    package must fail even without any dependency graph present."""
    root = _root(tmp_path)
    dist = _dist(root)
    inputs = _release_inputs(root, dist)
    # beta-package reaches the environment only transitively through
    # alpha-package; the lock still lists it while the SBOM omits it.
    inputs["environment_lock"].write_text(
        "alpha-package==1.0\nbeta-package==0.9\nengraphis==1.2.3\n",
        encoding="utf-8",
    )
    with pytest.raises(EvidenceError, match="truncated closure"):
        _build(root, dist, inputs=inputs)


def test_release_evidence_rejects_dangling_dependency_graph_ref(tmp_path):
    """A dependsOn ref resolving to nothing must fail the dependency-graph check."""
    root = _root(tmp_path)
    dist = _dist(root)
    inputs = _release_inputs(root, dist)
    sbom_doc = json.loads(inputs["sbom"].read_text(encoding="utf-8"))
    sbom_doc["components"].append(
        {
            "type": "library",
            "name": "beta-package",
            "version": "0.9",
            "purl": "pkg:pypi/beta-package@0.9",
        }
    )
    sbom_doc["dependencies"] = [
        {
            "ref": "pkg:pypi/engraphis@1.2.3",
            "dependsOn": ["pkg:pypi/alpha-package@1.0"],
        },
        {
            "ref": "pkg:pypi/alpha-package@1.0",
            "dependsOn": ["pkg:pypi/ghost-package@9.9"],
        },
        {"ref": "pkg:pypi/beta-package@0.9", "dependsOn": []},
    ]
    inputs["sbom"].write_text(json.dumps(sbom_doc), encoding="utf-8")
    inputs["environment_lock"].write_text(
        "alpha-package==1.0\nbeta-package==0.9\nengraphis==1.2.3\n",
        encoding="utf-8",
    )
    with pytest.raises(EvidenceError, match="unknown component ref"):
        _build(root, dist, inputs=inputs)


def test_release_evidence_rejects_dependency_graph_entry_without_component(tmp_path):
    """A graph entry must identify the root or a listed SBOM component."""
    root = _root(tmp_path)
    dist = _dist(root)
    inputs = _release_inputs(root, dist)
    sbom_doc = json.loads(inputs["sbom"].read_text(encoding="utf-8"))
    sbom_doc["dependencies"] = [
        {
            "ref": "pkg:pypi/engraphis@1.2.3",
            "dependsOn": ["ghost-ref"],
        },
        {"ref": "ghost-ref", "dependsOn": ["pkg:pypi/alpha-package@1.0"]},
        {"ref": "pkg:pypi/alpha-package@1.0", "dependsOn": []},
    ]
    inputs["sbom"].write_text(json.dumps(sbom_doc), encoding="utf-8")

    with pytest.raises(EvidenceError, match="unknown component ref"):
        _build(root, dist, inputs=inputs)


def test_release_evidence_rejects_duplicate_dependency_graph_ref(tmp_path):
    """Repeated graph refs must not overwrite an earlier contradictory edge."""
    root = _root(tmp_path)
    dist = _dist(root)
    inputs = _release_inputs(root, dist)
    sbom_doc = json.loads(inputs["sbom"].read_text(encoding="utf-8"))
    sbom_doc["dependencies"] = [
        {
            "ref": "pkg:pypi/engraphis@1.2.3",
            "dependsOn": ["pkg:pypi/ghost-package@9.9"],
        },
        {
            "ref": "pkg:pypi/engraphis@1.2.3",
            "dependsOn": ["pkg:pypi/alpha-package@1.0"],
        },
        {"ref": "pkg:pypi/alpha-package@1.0", "dependsOn": []},
    ]
    inputs["sbom"].write_text(json.dumps(sbom_doc), encoding="utf-8")

    with pytest.raises(EvidenceError, match="duplicate ref"):
        _build(root, dist, inputs=inputs)


def test_release_evidence_rejects_component_ref_colliding_with_root(tmp_path):
    """A component ref must not alias a root ref and fabricate reachability."""
    root = _root(tmp_path)
    dist = _dist(root)
    inputs = _release_inputs(root, dist)
    sbom_doc = json.loads(inputs["sbom"].read_text(encoding="utf-8"))
    sbom_doc["metadata"]["component"]["bom-ref"] = "shared-root-ref"
    sbom_doc["components"][0]["bom-ref"] = "shared-root-ref"
    sbom_doc["dependencies"] = [
        {"ref": "pkg:pypi/engraphis@1.2.3", "dependsOn": []},
        {"ref": "pkg:pypi/alpha-package@1.0", "dependsOn": []},
    ]
    inputs["sbom"].write_text(json.dumps(sbom_doc), encoding="utf-8")

    with pytest.raises(EvidenceError, match="collides with root"):
        _build(root, dist, inputs=inputs)


def test_release_evidence_rejects_purl_graph_ref_when_bom_ref_is_present(tmp_path):
    """Dependency refs must use bom-ref when a component declares one."""
    root = _root(tmp_path)
    dist = _dist(root)
    inputs = _release_inputs(root, dist)
    sbom_doc = json.loads(inputs["sbom"].read_text(encoding="utf-8"))
    sbom_doc["metadata"]["component"]["bom-ref"] = "root-component"
    sbom_doc["components"][0]["bom-ref"] = "alpha-component"
    sbom_doc["dependencies"] = [
        {
            "ref": "root-component",
            "dependsOn": ["pkg:pypi/alpha-package@1.0"],
        },
        {"ref": "alpha-component", "dependsOn": []},
    ]
    inputs["sbom"].write_text(json.dumps(sbom_doc), encoding="utf-8")

    with pytest.raises(EvidenceError, match="unknown component ref"):
        _build(root, dist, inputs=inputs)


def test_release_evidence_rejects_unreachable_declared_dependency(tmp_path):
    """A declared dependency present in the SBOM but not reachable from the
    project root through the dependency graph must fail."""
    root = _root(tmp_path)
    dist = _dist(root)
    inputs = _release_inputs(root, dist)
    sbom_doc = json.loads(inputs["sbom"].read_text(encoding="utf-8"))
    sbom_doc["dependencies"] = [
        {"ref": "pkg:pypi/engraphis@1.2.3", "dependsOn": []},
        {"ref": "pkg:pypi/alpha-package@1.0", "dependsOn": []},
    ]
    inputs["sbom"].write_text(json.dumps(sbom_doc), encoding="utf-8")
    with pytest.raises(EvidenceError, match="unreachable from the SBOM root"):
        _build(root, dist, inputs=inputs)


def test_release_evidence_rejects_empty_sbom_package_set(tmp_path):
    """An SBOM with no Python components must not pass the subset check."""
    root = _root(tmp_path)
    dist = _dist(root)
    inputs = _release_inputs(root, dist)
    # Replace SBOM with one that has no pypi components
    inputs["sbom"].write_text(
        json.dumps(
            {
                "bomFormat": "CycloneDX",
                "specVersion": "1.6",
                "components": [
                    {"type": "library", "name": "libssl", "version": "3.0",
                     "purl": "pkg:deb/debian/libssl@3.0"},
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(EvidenceError, match="(no Python package components|lacks a valid PyPI PURL)"):
        _build(root, dist, inputs=inputs)


def test_release_evidence_rejects_sbom_missing_root_component(tmp_path):
    """A truncated SBOM that retains one matching dep but omits the root
    engraphis component must fail the lock comparison."""
    root = _root(tmp_path)
    dist = _dist(root)
    inputs = _release_inputs(root, dist)
    # SBOM has only alpha-package (which IS in the lock) but no engraphis root.
    inputs["sbom"].write_text(
        json.dumps(
            {
                "bomFormat": "CycloneDX",
                "specVersion": "1.6",
                "components": [
                    {
                        "type": "library",
                        "name": "alpha-package",
                        "version": "1.0",
                        "purl": "pkg:pypi/alpha-package@1.0",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(EvidenceError, match="metadata.component does not identify"):
        _build(root, dist, inputs=inputs)


def test_release_evidence_rejects_sbom_with_wrong_version_root_component(tmp_path):
    """An SBOM whose metadata.component names a different project version must fail."""
    root = _root(tmp_path)
    dist = _dist(root)
    inputs = _release_inputs(root, dist)
    inputs["sbom"].write_text(
        json.dumps(
            {
                "bomFormat": "CycloneDX",
                "specVersion": "1.6",
                "metadata": {
                    "component": {
                        "type": "application",
                        "name": "other-project",
                        "version": "9.9.9",
                        "purl": "pkg:pypi/other-project@9.9.9",
                    },
                },
                "components": [
                    {
                        "type": "library",
                        "name": "engraphis",
                        "version": "1.2.3",
                        "purl": "pkg:pypi/engraphis@1.2.3",
                    },
                    {
                        "type": "library",
                        "name": "alpha-package",
                        "version": "1.0",
                        "purl": "pkg:pypi/alpha-package@1.0",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(EvidenceError, match="metadata.component does not identify"):
        _build(root, dist, inputs=inputs)


def test_release_evidence_rejects_sbom_with_only_root_component(tmp_path):
    """An SBOM with valid metadata.component but no dependency components must fail."""
    root = _root(tmp_path)
    dist = _dist(root)
    inputs = _release_inputs(root, dist)
    inputs["sbom"].write_text(
        json.dumps(
            {
                "bomFormat": "CycloneDX",
                "specVersion": "1.6",
                "metadata": {
                    "component": {
                        "type": "application",
                        "name": "engraphis",
                        "version": "1.2.3",
                        "purl": "pkg:pypi/engraphis@1.2.3",
                    },
                },
                "components": [],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(EvidenceError, match="(no dependency components|missing declared dependencies)"):
        _build(root, dist, inputs=inputs)


def test_release_evidence_rejects_sbom_missing_declared_dependencies(tmp_path):
    """An SBOM missing a declared dependency must fail the closure check."""
    root = _root(tmp_path)
    dist = _dist(root)
    inputs = _release_inputs(root, dist)
    # SBOM has root + pip (not declared) but omits alpha-package (declared)
    inputs["sbom"].write_text(
        json.dumps(
            {
                "bomFormat": "CycloneDX",
                "specVersion": "1.6",
                "metadata": {
                    "component": {
                        "type": "application",
                        "name": "engraphis",
                        "version": "1.2.3",
                        "purl": "pkg:pypi/engraphis@1.2.3",
                    },
                },
                "components": [
                    {"type": "library", "name": "pip", "version": "26.2",
                     "purl": "pkg:pypi/pip@26.2"},
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(EvidenceError, match="missing declared dependencies"):
        _build(root, dist, inputs=inputs)


def test_release_evidence_rejects_sbom_version_violating_declared_constraint(tmp_path):
    """An SBOM whose version violates a declared specifier must fail."""
    root = _root(tmp_path)
    dist = _dist(root)
    inputs = _release_inputs(root, dist)
    # pyproject.toml declares alpha-package>=1.0, but SBOM has 0.5
    inputs["sbom"].write_text(
        json.dumps(
            {
                "bomFormat": "CycloneDX",
                "specVersion": "1.6",
                "metadata": {
                    "component": {
                        "type": "application",
                        "name": "engraphis",
                        "version": "1.2.3",
                        "purl": "pkg:pypi/engraphis@1.2.3",
                    },
                },
                "components": [
                    {"type": "library", "name": "alpha-package", "version": "0.5",
                     "purl": "pkg:pypi/alpha-package@0.5"},
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(EvidenceError, match="does not satisfy declared constraint"):
        _build(root, dist, inputs=inputs)


def test_release_evidence_combines_duplicate_declared_constraints(tmp_path):
    """Repeated requirements must enforce the intersection of their specifiers."""
    root = _root(tmp_path)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "engraphis"\nversion = "1.2.3"\n'
        'dependencies = ["alpha-package>=1.0"]\n'
        "[project.optional-dependencies]\n"
        'all = ["alpha-package>=2.0"]\n'
        'test = ["alpha-package<3.0"]\n',
        encoding="utf-8",
    )
    dist = _dist(root)
    inputs = _release_inputs(root, dist)
    inputs["environment_lock"].write_text(
        "alpha-package==1.5\nengraphis==1.2.3\n", encoding="utf-8"
    )
    inputs["sbom"].write_text(
        json.dumps(
            {
                "bomFormat": "CycloneDX",
                "specVersion": "1.6",
                "metadata": {
                    "component": {
                        "type": "application",
                        "name": "engraphis",
                        "version": "1.2.3",
                        "purl": "pkg:pypi/engraphis@1.2.3",
                    },
                },
                "components": [
                    {
                        "type": "library",
                        "name": "alpha-package",
                        "version": "1.5",
                        "purl": "pkg:pypi/alpha-package@1.5",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(EvidenceError, match="does not satisfy declared constraint"):
        _build(root, dist, inputs=inputs)


def test_release_evidence_rejects_prerelease_versions_against_pep440_floors(tmp_path):
    """A prerelease below the declared floor must not satisfy the constraint."""
    root = _root(tmp_path)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "engraphis"\nversion = "1.2.3"\n'
        'dependencies = ["alpha-package>=1.0", "numpy>=1.24"]\n',
        encoding="utf-8",
    )
    dist = _dist(root)
    inputs = _release_inputs(root, dist)
    inputs["environment_lock"].write_text(
        "alpha-package==1.0\nengraphis==1.2.3\nnumpy==1.24rc1\n",
        encoding="utf-8",
    )
    inputs["sbom"].write_text(
        json.dumps(
            {
                "bomFormat": "CycloneDX",
                "specVersion": "1.6",
                "metadata": {
                    "component": {
                        "type": "application",
                        "name": "engraphis",
                        "version": "1.2.3",
                        "purl": "pkg:pypi/engraphis@1.2.3",
                    },
                },
                "components": [
                    {
                        "type": "library",
                        "name": "alpha-package",
                        "version": "1.0",
                        "purl": "pkg:pypi/alpha-package@1.0",
                    },
                    {
                        "type": "library",
                        "name": "numpy",
                        "version": "1.24rc1",
                        "purl": "pkg:pypi/numpy@1.24rc1",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(EvidenceError, match="does not satisfy declared constraint"):
        _build(root, dist, inputs=inputs)


def test_release_evidence_requires_selected_extra_dependencies(tmp_path):
    """The closure must include requirements from the extras the workflow installs."""
    root = _root(tmp_path)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "engraphis"\nversion = "1.2.3"\n'
        'dependencies = ["alpha-package>=1.0"]\n'
        "[project.optional-dependencies]\n"
        "all = [\"extra-dep>=1.0; python_version >= '3.9'\"]\n"
        "test = [\"test-dep>=0.1\"]\n",
        encoding="utf-8",
    )
    dist = _dist(root)
    inputs = _release_inputs(root, dist)
    # SBOM and lock carry only the core dependency; extra-dep/test-dep are missing.
    with pytest.raises(EvidenceError, match="missing declared dependencies"):
        _build(root, dist, inputs=inputs)


def test_release_evidence_allows_unreachable_selected_extra_dependencies(tmp_path):
    """Selected extras must be captured, but need not be root-reachable in CycloneDX."""
    root = _root(tmp_path)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "engraphis"\nversion = "1.2.3"\n'
        'dependencies = ["alpha-package>=1.0"]\n'
        "[project.optional-dependencies]\n"
        'all = ["extra-dep>=1.0"]\n'
        'test = ["test-dep>=0.1"]\n',
        encoding="utf-8",
    )
    dist = _dist(root)
    inputs = _release_inputs(root, dist)
    sbom_doc = json.loads(inputs["sbom"].read_text(encoding="utf-8"))
    for name, version in (("extra-dep", "1.0"), ("test-dep", "0.1")):
        ref = f"pkg:pypi/{name}@{version}"
        sbom_doc["components"].append(
            {"type": "library", "name": name, "version": version, "purl": ref}
        )
        # The capture contains the selected extras, but the installed metadata
        # does not promise that those nodes are linked from the project root.
        sbom_doc["dependencies"].append({"ref": ref, "dependsOn": []})
    inputs["sbom"].write_text(json.dumps(sbom_doc), encoding="utf-8")
    inputs["environment_lock"].write_text(
        "alpha-package==1.0\nextra-dep==1.0\ntest-dep==0.1\nengraphis==1.2.3\n",
        encoding="utf-8",
    )

    evidence = _build(root, dist, inputs=inputs)

    assert evidence["environment_lock"]["package_count"] == 4


def test_release_evidence_ignores_extra_dependencies_with_inapplicable_markers(tmp_path):
    """Extras requirements whose environment marker excludes this interpreter are
    not required, mirroring what pip installs in the capture environment."""
    root = _root(tmp_path)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "engraphis"\nversion = "1.2.3"\n'
        'dependencies = ["alpha-package>=1.0"]\n'
        "[project.optional-dependencies]\n"
        "all = [\"future-dep>=1.0; python_version < '3.9'\"]\n"
        "test = [\"legacy-dep>=0.1; python_version < '3.9'\"]\n",
        encoding="utf-8",
    )
    dist = _dist(root)
    evidence = _build(root, dist)
    assert evidence["environment_lock"]["package_count"] == 2


def test_release_evidence_rejects_sbom_with_mismatched_root_purl(tmp_path):
    """An SBOM whose metadata.component PURL names a different package must fail."""
    root = _root(tmp_path)
    dist = _dist(root)
    inputs = _release_inputs(root, dist)
    inputs["sbom"].write_text(
        json.dumps(
            {
                "bomFormat": "CycloneDX",
                "specVersion": "1.6",
                "metadata": {
                    "component": {
                        "type": "application",
                        "name": "engraphis",
                        "version": "1.2.3",
                        "purl": "pkg:pypi/other-project@1.2.3",
                    },
                },
                "components": [
                    {
                        "type": "library",
                        "name": "alpha-package",
                        "version": "1.0",
                        "purl": "pkg:pypi/alpha-package@1.0",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(EvidenceError, match="metadata.component does not identify"):
        _build(root, dist, inputs=inputs)


def test_release_evidence_rejects_sbom_with_malformed_dependency_purl(tmp_path):
    """An SBOM component whose PURL names a different package than its name/version must fail."""
    root = _root(tmp_path)
    dist = _dist(root)
    inputs = _release_inputs(root, dist)
    inputs["sbom"].write_text(
        json.dumps(
            {
                "bomFormat": "CycloneDX",
                "specVersion": "1.6",
                "metadata": {
                    "component": {
                        "type": "application",
                        "name": "engraphis",
                        "version": "1.2.3",
                        "purl": "pkg:pypi/engraphis@1.2.3",
                    },
                },
                "components": [
                    {
                        "type": "library",
                        "name": "alpha-package",
                        "version": "1.0",
                        "purl": "pkg:pypi/other-project@1.0",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(EvidenceError, match="PURL does not match"):
        _build(root, dist, inputs=inputs)


def test_release_evidence_rejects_sbom_with_non_pypi_component_purl(tmp_path):
    """An SBOM component with a non-PyPI PURL (e.g. pkg:deb) must fail."""
    root = _root(tmp_path)
    dist = _dist(root)
    inputs = _release_inputs(root, dist)
    inputs["sbom"].write_text(
        json.dumps(
            {
                "bomFormat": "CycloneDX",
                "specVersion": "1.6",
                "metadata": {
                    "component": {
                        "type": "application",
                        "name": "engraphis",
                        "version": "1.2.3",
                        "purl": "pkg:pypi/engraphis@1.2.3",
                    },
                },
                "components": [
                    {
                        "type": "library",
                        "name": "alpha-package",
                        "version": "1.0",
                        "purl": "pkg:pypi/alpha-package@1.0",
                    },
                    {
                        "type": "library",
                        "name": "libssl",
                        "version": "3.0",
                        "purl": "pkg:deb/debian/libssl@3.0",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(EvidenceError, match="lacks a valid PyPI PURL"):
        _build(root, dist, inputs=inputs)


def test_release_evidence_rejects_sbom_component_missing_identity(tmp_path):
    """A component missing name/version must not disappear from the closure."""
    root = _root(tmp_path)
    dist = _dist(root)
    inputs = _release_inputs(root, dist)
    sbom_doc = json.loads(inputs["sbom"].read_text(encoding="utf-8"))
    sbom_doc["components"].append(
        {"type": "library", "purl": "pkg:pypi/ghost-package@9.9"}
    )
    inputs["sbom"].write_text(json.dumps(sbom_doc), encoding="utf-8")

    with pytest.raises(EvidenceError, match="must identify name and version"):
        _build(root, dist, inputs=inputs)


def test_release_evidence_rejects_partial_or_unbound_container_evidence(tmp_path):
    root = _root(tmp_path)
    dist = _dist(root)
    inputs = _release_inputs(root, dist)
    image_sbom = json.loads(inputs["image_sbom"].read_text(encoding="utf-8"))
    image_sbom["metadata"]["component"]["properties"][0]["value"] = "sha256:" + "f" * 64
    inputs["image_sbom"].write_text(json.dumps(image_sbom), encoding="utf-8")
    with pytest.raises(EvidenceError, match="bind the production image digest"):
        _build(root, dist, inputs=inputs)

    image_sbom["metadata"]["component"]["properties"][0]["value"] = IMAGE_DIGEST
    image_sbom["components"] = [
        item for item in image_sbom["components"] if not item["purl"].startswith("pkg:deb/")
    ]
    inputs["image_sbom"].write_text(json.dumps(image_sbom), encoding="utf-8")
    with pytest.raises(EvidenceError, match="both OS and Python"):
        _build(root, dist, inputs=inputs)


def test_release_evidence_rejects_mismatched_independent_builders(tmp_path):
    root = _root(tmp_path)
    dist = _dist(root)
    inputs = _release_inputs(root, dist)
    report = json.loads(inputs["reproducibility"].read_text(encoding="utf-8"))
    report["builders"][1]["artifacts"]["engraphis-1.2.3.tar.gz"] = "f" * 64
    inputs["reproducibility"].write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(EvidenceError, match="artifacts differ"):
        _build(root, dist, inputs=inputs)


def test_release_evidence_rejects_unpinned_scanner_or_unknown_database(tmp_path):
    root = _root(tmp_path)
    dist = _dist(root)
    inputs = _release_inputs(root, dist)
    report = json.loads(inputs["image_scan"].read_text(encoding="utf-8"))
    report["descriptor"]["version"] = "latest"
    inputs["image_scan"].write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(EvidenceError, match="unexpected Grype version"):
        _build(root, dist, inputs=inputs)



def test_release_evidence_accepts_identified_current_grype_database_shape(tmp_path):
    root = _root(tmp_path)
    dist = _dist(root)
    inputs = _release_inputs(root, dist)
    report = json.loads(inputs["image_scan"].read_text(encoding="utf-8"))
    database = report["descriptor"]["db"]
    database["schemaVersion"] = "v6.1.3"
    database["from"] = (
        "https://grype.anchore.io/databases/v6/latest.tar.zst"
        "?checksum=sha256%3A" + "d" * 64
    )
    del database["checksum"]
    inputs["image_scan"].write_text(json.dumps(report), encoding="utf-8")

    evidence = _build(root, dist, inputs=inputs)

    assert evidence["container"]["vulnerability_scan"]["database"] == {
        "built": "2026-08-08T00:00:00Z",
        "schema_version": "v6.1.3",
        "checksum": "sha256:" + "d" * 64,
    }


def test_release_evidence_accepts_grype_0110_nested_db_status_shape(tmp_path):
    root = _root(tmp_path)
    dist = _dist(root)
    inputs = _release_inputs(root, dist)
    report = json.loads(inputs["image_scan"].read_text(encoding="utf-8"))
    report["descriptor"]["db"] = {
        "status": {
            "schemaVersion": "v6.1.9",
            "from": (
                "https://grype.anchore.io/databases/v6/vulnerability-db_v6.1.9_"
                "2026-08-25T00:17:00Z_1787638635.tar.zst"
                "?checksum=sha256%3A" + "f" * 64
            ),
            "built": "2026-08-25T06:17:15Z",
            "path": "/home/runner/.cache/grype/db/6/vulnerability.db",
            "valid": True,
        },
        "providers": {"ubuntu": {"captured": "2026-08-25T00:19:07Z"}},
    }
    inputs["image_scan"].write_text(json.dumps(report), encoding="utf-8")

    evidence = _build(root, dist, inputs=inputs)

    assert evidence["container"]["vulnerability_scan"]["database"] == {
        "built": "2026-08-25T06:17:15Z",
        "schema_version": "v6.1.9",
        "checksum": "sha256:" + "f" * 64,
    }


def test_release_evidence_rejects_grype_0110_database_marked_invalid(tmp_path):
    root = _root(tmp_path)
    dist = _dist(root)
    inputs = _release_inputs(root, dist)
    report = json.loads(inputs["image_scan"].read_text(encoding="utf-8"))
    report["descriptor"]["db"] = {
        "status": {
            "schemaVersion": "v6.1.9",
            "from": (
                "https://grype.anchore.io/databases/v6/vulnerability-db_v6.1.9_"
                "2026-08-25T00:17:00Z_1787638635.tar.zst"
                "?checksum=sha256%3A" + "f" * 64
            ),
            "built": "2026-08-25T06:17:15Z",
            "path": "/home/runner/.cache/grype/db/6/vulnerability.db",
            "valid": False,
        },
    }
    inputs["image_scan"].write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(EvidenceError, match="marked invalid"):
        _build(root, dist, inputs=inputs)


def test_repair_run_candidates_are_newest_first_and_bound_to_tag_commit_event():
    runs = [
        {
            "databaseId": 10,
            "headBranch": TAG,
            "headSha": COMMIT,
            "event": "push",
            "createdAt": "2026-08-07T00:00:00Z",
        },
        {
            "databaseId": 30,
            "headBranch": TAG,
            "headSha": COMMIT,
            "event": "push",
            "createdAt": "2026-08-08T00:00:00Z",
        },
        {
            "databaseId": 40,
            "headBranch": TAG,
            "headSha": COMMIT,
            "event": "workflow_dispatch",
            "createdAt": "2026-08-09T00:00:00Z",
        },
        {
            "databaseId": 50,
            "headBranch": TAG,
            "headSha": "b" * 40,
            "event": "push",
            "createdAt": "2026-08-10T00:00:00Z",
        },
    ]

    assert repair_run_candidates(runs, TAG, COMMIT) == ["30", "10"]
    assert repair_run_candidates([], TAG, COMMIT) == []


@pytest.mark.skipif(shutil.which("cyclonedx-py") is None, reason="release-only CycloneDX tool")
def test_release_environment_command_emits_a_cyclonedx_sbom(tmp_path):
    output = tmp_path / "engraphis.cdx.json"
    environment = tmp_path / "sbom-environment"
    venv.EnvBuilder(with_pip=False).create(environment)
    interpreter = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    result = subprocess.run(
        [
            "cyclonedx-py", "environment", "--output-reproducible", "--of", "JSON",
            "--pyproject", "pyproject.toml", "-o", str(output), str(interpreter),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["bomFormat"] == "CycloneDX"
    assert payload["metadata"]["component"]["name"] == "engraphis"
    assert isinstance(payload.get("components", []), list)


def test_release_workflow_publishes_complete_captured_evidence():
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    build = workflow.split("  build:\n", 1)[1].split("  reproducibility-build:\n", 1)[0]
    reproducibility = workflow.split("  reproducibility-build:\n", 1)[1].split(
        "  python-matrix:\n", 1,
    )[0]
    platform_smoke = workflow.split("  installed-artifact-platform-smoke:\n", 1)[1].split(
        "  encryption:\n", 1,
    )[0]
    docker_job = workflow.split("  docker-smoke:\n", 1)[1].split(
        "  code-security:\n", 1,
    )[0]
    evidence_job = workflow.split("  release-evidence:\n", 1)[1].split("  publish:\n", 1)[0]
    browser_job = workflow.split("  browser-accessibility:\n", 1)[1].split(
        "  pi-extension:\n", 1,
    )[0]
    github_release = workflow.split("  github-release:\n", 1)[1].split(
        "  github-release-repair:\n", 1,
    )[0]
    repair = workflow.split("  github-release-repair:\n", 1)[1]

    constraints = (ROOT / ".github" / "release-constraints.txt").read_text(encoding="utf-8")
    assert "cyclonedx-bom==7.3.0" in constraints
    assert "pip list --format=freeze" in build
    assert "cyclonedx-py environment --output-reproducible --of JSON" in build
    assert "name: build-environment-evidence" in build
    assert "dist-repeat" not in build
    assert "Independent distribution builder ${{ matrix.builder }}" in reproducibility
    assert "github-hosted:ubuntu-latest/python-3.11" in reproducibility
    assert 'builder: ["a", "b"]' in reproducibility
    assert "Compare independent distribution builders" in reproducibility
    assert "name: independent-reproducibility" in reproducibility
    assert "os: [windows-latest, macos-latest]" in platform_smoke
    assert '"pip", "check"' in platform_smoke
    assert "scripts.smoke_entry_points" in platform_smoke
    assert "anchore/sbom-action@e22c389904149dbc22b58101806040fa8d37a610" in docker_job
    assert "anchore/scan-action@e1165082ffb1fe366ebaf02d8526e7c4989ea9d2" in docker_job
    assert "docker buildx build --pull --load" in docker_job
    assert '"containerimage.digest"' in docker_job
    assert "engraphis:image-digest" in docker_job
    assert "name: production-image-evidence" in docker_job
    assert "python scripts/release_evidence.py --dist dist --commit \"$GITHUB_SHA\"" in workflow
    for argument in (
        "--tag \"$GITHUB_REF_NAME\"",
        "--sbom \"$sbom\"",
        "--environment-lock release-evidence/environment.lock",
        "--image-sbom release-evidence/engraphis-container.cdx.json",
        "--image-scan release-evidence/grype.json",
        "--reproducibility release-evidence/reproducibility.json",
    ):
        assert argument in evidence_job
    for check_id in (
        "pyright-core-backends", "privacy-boundary", "token-efficiency",
        "benchmark-schema-evidence", "browser-e2e", "pi-extension", "dependency-audit",
        "browser-dependency-audit", "container-smoke", "codeql",
        "reproducible-distributions",
        "installed-artifact-smoke", "installed-artifact-smoke-py39",
        "installed-artifact-platform-smoke", "retrieval-ablation",
        "reinforcement-state-transition", "adversarial-memory-security",
    ):
        assert "--verified-check " + check_id in evidence_job
    workflow_check_ids = re.findall(r"--verified-check\s+([a-z0-9-]+)", evidence_job)
    manifest_check_ids = _check_ids(ROOT)
    assert len(workflow_check_ids) == len(set(workflow_check_ids))
    assert set(workflow_check_ids) == set(manifest_check_ids)
    assert "reproducibility-check" in evidence_job.split("needs:", 1)[1].splitlines()[0]
    assert "installed-artifact-platform-smoke" in (
        evidence_job.split("needs:", 1)[1].splitlines()[0]
    )
    assert "npm run test:e2e" in browser_job
    assert "Generate public release evidence" not in build
    assert "needs: release-evidence" in workflow
    assert "name: public-release-evidence" in workflow
    assert "Download public release evidence" in github_release
    assert "dist/* release-evidence/*" in github_release
    assert "--name public-release-evidence" in repair
    assert "verified-dist/* release-evidence/*" in repair
    assert "repair_run_candidates" in repair
    assert "while IFS= read -r candidate" in repair
    assert '"$RELEASE_TAG" dist/*' not in repair


def test_receipt_export_has_a_stable_canonical_verification_view():
    from engraphis.service import MemoryService

    service = MemoryService.create(":memory:")
    service.remember("The production deploy window is Friday.", workspace="acme")

    first = service.export_receipts(workspace="acme")
    second = service.export_receipts(workspace="acme")

    assert first["verification"]["valid"] is True
    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    encoded = canonical_json_bytes(first).decode("utf-8")
    assert "production deploy window" not in encoded
    assert "acme" not in encoded
