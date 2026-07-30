from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from engraphis.service import MemoryService
from scripts.release_evidence import (
    EvidenceError,
    build_evidence,
    canonical_json_bytes,
    check_manifest,
)


COMMIT = "a" * 40
TAG = "v1.2.3"
ROOT = Path(__file__).resolve().parents[1]


def _root(tmp_path):
    (tmp_path / "eval" / "datasets").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "engraphis"\nversion = "1.2.3"\n', encoding="utf-8"
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


def _sbom(root):
    path = root / "release-evidence" / "engraphis-1.2.3.cdx.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "bomFormat": "CycloneDX",
                "specVersion": "1.6",
                "components": [{"type": "library", "name": "engraphis", "version": "1.2.3"}],
            }
        ),
        encoding="utf-8",
    )
    return path


def _check_ids(root):
    return [entry["id"] for group in check_manifest(root).values() for entry in group]


def test_release_evidence_is_canonical_and_contains_only_public_release_inputs(tmp_path):
    root = _root(tmp_path)
    evidence = build_evidence(
        root, _dist(root), commit=COMMIT, tag=TAG, sbom=_sbom(root), verified_checks=_check_ids(root)
    )

    first = canonical_json_bytes(evidence)
    second = canonical_json_bytes(
        build_evidence(
            root, root / "dist", commit=COMMIT, tag=TAG,
            sbom=root / "release-evidence" / "engraphis-1.2.3.cdx.json",
            verified_checks=_check_ids(root),
        )
    )
    assert first == second
    assert json.loads(first) == evidence
    assert evidence["format"] == "engraphis-release-evidence/2"
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
    assert evidence["sbom"]["format"] == "CycloneDX"
    assert evidence["provenance"]["builder"]["sbom_generator"]["version"] == "7.3.0"
    assert evidence["provenance"]["builder"]["job"] == "release-evidence"
    assert evidence["provenance"]["builder"]["completed_gate_jobs"] == [
        "build", "python-matrix", "browser-accessibility", "docker-smoke"
    ]
    assert len(evidence["limitations"]) == 3
    assert "exported_at" not in evidence


def test_release_evidence_fails_closed_when_checks_are_missing_or_unknown(tmp_path):
    root = _root(tmp_path)
    with pytest.raises(EvidenceError, match="verified checks"):
        build_evidence(root, _dist(root), commit=COMMIT, tag=TAG, sbom=_sbom(root), verified_checks=["ruff"])
    with pytest.raises(EvidenceError, match="unexpected"):
        build_evidence(
            root, root / "dist", commit=COMMIT, tag=TAG, sbom=_sbom(root),
            verified_checks=_check_ids(root) + ["made-up"],
        )


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
        build_evidence(root, dist, commit=COMMIT, tag=TAG, sbom=_sbom(root), verified_checks=_check_ids(root))


def test_release_evidence_rejects_secret_like_values_even_in_package_filenames(tmp_path):
    root = _root(tmp_path)
    dist = _dist(root)
    (dist / ("engraphis-1.2.3-sk_" + "a" * 16 + ".whl")).write_bytes(b"not safe")
    with pytest.raises(EvidenceError, match="secret-like values"):
        build_evidence(root, dist, commit=COMMIT, tag=TAG, sbom=_sbom(root), verified_checks=_check_ids(root))


def test_release_evidence_fails_closed_for_unmatched_tags_and_invalid_sboms(tmp_path):
    root = _root(tmp_path)
    dist = _dist(root)
    with pytest.raises(EvidenceError, match="tag"):
        build_evidence(root, dist, commit=COMMIT, tag="v1.2.4", sbom=_sbom(root), verified_checks=_check_ids(root))

    sbom = root / "release-evidence" / "engraphis-1.2.3.cdx.json"
    sbom.write_text('{"bomFormat":"not-cyclonedx"}', encoding="utf-8")
    with pytest.raises(EvidenceError, match="CycloneDX"):
        build_evidence(root, dist, commit=COMMIT, tag=TAG, sbom=sbom, verified_checks=_check_ids(root))


@pytest.mark.skipif(shutil.which("cyclonedx-py") is None, reason="release-only CycloneDX tool")
def test_release_environment_command_emits_a_cyclonedx_sbom(tmp_path):
    output = tmp_path / "engraphis.cdx.json"
    result = subprocess.run(
        [
            "cyclonedx-py", "environment", "--output-reproducible", "--of", "JSON",
            "--pyproject", "pyproject.toml", "-o", str(output),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["bomFormat"] == "CycloneDX"
    assert isinstance(payload["components"], list)


def test_release_workflow_publishes_evidence_separately_from_package_artifacts():
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    build = workflow.split("  build:\n", 1)[1].split("  python-matrix:\n", 1)[0]
    evidence_job = workflow.split("  release-evidence:\n", 1)[1].split("  publish:\n", 1)[0]
    browser_job = workflow.split("  browser-accessibility:\n", 1)[1].split("  docker-smoke:\n", 1)[0]
    github_release = workflow.split("  github-release:\n", 1)[1].split(
        "  github-release-repair:\n", 1
    )[0]
    repair = workflow.split("  github-release-repair:\n", 1)[1]

    assert "cyclonedx-bom==7.3.0" in workflow
    assert "cyclonedx-py environment --output-reproducible --of JSON" in workflow
    assert "python scripts/release_evidence.py --dist dist --commit \"$GITHUB_SHA\"" in workflow
    assert "--tag \"$GITHUB_REF_NAME\"" in workflow
    assert "--sbom \"$sbom\"" in workflow
    assert "--verified-check retrieval-ablation" in workflow
    for check_id in (
        "privacy-boundary", "token-efficiency", "benchmark-schema-evidence", "browser-e2e",
        "dependency-audit", "container-smoke",
    ):
        assert "--verified-check " + check_id in evidence_job
    assert "needs: [build, python-matrix, browser-accessibility, docker-smoke]" in evidence_job
    assert "name: Download distributions" in evidence_job
    assert "npm run test:e2e" in browser_job
    assert "Generate public release evidence" not in build
    assert "needs: release-evidence" in workflow
    assert "name: public-release-evidence" in workflow
    assert "path: release-evidence/" in workflow
    assert "Download public release evidence" in github_release
    assert "dist/* release-evidence/release-evidence.json release-evidence/*.cdx.json" in github_release
    assert "--name public-release-evidence" in repair
    assert "dist/* release-evidence/release-evidence.json release-evidence/*.cdx.json" in repair


def test_receipt_export_has_a_stable_canonical_verification_view():
    service = MemoryService.create(":memory:")
    service.remember("The production deploy window is Friday.", workspace="acme")

    first = service.export_receipts(workspace="acme")
    second = service.export_receipts(workspace="acme")

    assert first["verification"]["valid"] is True
    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    encoded = canonical_json_bytes(first).decode("utf-8")
    assert "production deploy window" not in encoded
    assert "acme" not in encoded
