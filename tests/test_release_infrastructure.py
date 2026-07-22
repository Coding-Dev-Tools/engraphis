"""Static release-infrastructure invariants that must not drift silently."""
from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_published_image_and_railway_template_fail_safe_to_customer_mode():
    dockerfile = _text("Dockerfile")
    template = json.loads(_text("deploy/railway-template.json"))
    railway = json.loads(_text("railway.json"))

    assert "ENGRAPHIS_SERVICE_MODE=customer" in dockerfile
    assert railway["$schema"] == "https://railway.com/railway.schema.json"
    assert template["format"] == "engraphis-railway-template-composer-source/v1"
    assert template["variables"]["ENGRAPHIS_SERVICE_MODE"]["value"] == "customer"
    assert template["service"]["healthcheck"] == "/api/ready"
    assert template["service"]["volume"]["mount_path"] == "/data"
    deployment = template["variables"]["ENGRAPHIS_DEPLOYMENT_TOKEN"]
    assert deployment["value"] == "${{ secret(48) }}"
    assert deployment["secret"] is True
    assert deployment["required"] is True


def test_ci_and_release_audit_production_image_dependencies():
    ci = _text(".github/workflows/ci.yml")
    release = _text(".github/workflows/release.yml")

    assert "Audit the exact production image dependency set" in ci
    assert "docker run --rm --entrypoint sh engraphis:ci" in ci
    assert "python -m pip_audit --local" in ci
    assert "tesseract-ocr" in _text("Dockerfile")
    assert "Verify production image OCR runtime" in ci
    assert "Verify production image OCR runtime" in release
    assert "docker-entrypoint\\.sh" in ci
    assert "railway\\.json" in ci
    assert "deploy/" in ci
    assert '".[all,test]"' in release
    assert "Audit production image dependencies" in release
    assert "Browser accessibility release gate" in release
    assert "Require release tag commit to be on protected main" in release
    for version in ('"3.9"', '"3.10"', '"3.11"', '"3.12"'):
        assert version in release


def test_compiled_wheels_cover_declared_python_and_mainstream_platforms():
    wheel_workflow = _text(".github/workflows/build-compiled-wheels.yml")
    release = _text(".github/workflows/release.yml")
    pyproject = _text("pyproject.toml")

    assert 'requires-python = ">=3.9"' in pyproject
    for version in ("3.9", "3.10", "3.11", "3.12"):
        assert f'"Programming Language :: Python :: {version}"' in pyproject
    assert 'os: [ubuntu-latest, windows-latest, macos-latest]' in wheel_workflow
    assert 'CIBW_BUILD: "cp39-* cp310-* cp311-* cp312-*"' in wheel_workflow
    assert 'CIBW_ARCHS_LINUX: "x86_64"' in wheel_workflow
    assert 'CIBW_ARCHS_WINDOWS: "AMD64"' in wheel_workflow
    assert 'CIBW_ARCHS_MACOS: "x86_64 arm64"' in wheel_workflow
    assert '"setuptools>=77; python_version < \'3.10\'"' in wheel_workflow
    assert '"setuptools>=83; python_version >= \'3.10\'"' in wheel_workflow
    assert "python -m build --sdist" in release
    assert "run: python -m build\n" not in release
    assert "Build compiled wheels (${{ matrix.os }})" in release
    assert "name: Assemble distributions" in release
    assert "needs: [assemble, python-matrix, browser-accessibility, docker-smoke]" in release
    assert "name: python-package-distributions" in release
    assert "Publish compiled wheels to PyPI" not in wheel_workflow
    assert "push:\n    tags:" not in wheel_workflow


def test_all_workflow_actions_are_pinned_to_full_commit_shas():
    workflows = ROOT / ".github" / "workflows"
    for path in workflows.glob("*.yml"):
        for line_number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            if not stripped.startswith("uses:") and "- uses:" not in stripped:
                continue
            reference = stripped.split("uses:", 1)[1].strip().split()[0]
            assert "@" in reference, f"{path.name}:{line_number} has no action ref"
            revision = reference.rsplit("@", 1)[1]
            assert len(revision) == 40 and all(c in "0123456789abcdef" for c in revision), (
                f"{path.name}:{line_number} action is not pinned to a full commit SHA"
            )


def test_ci_and_release_default_to_read_only_repository_permissions():
    for workflow in (
            ".github/workflows/ci.yml",
            ".github/workflows/release.yml"):
        header = _text(workflow).split("\njobs:", 1)[0]
        assert "\npermissions:\n  contents: read\n" in header


def test_release_repair_requires_tag_sha_successful_build_publish_and_pypi_identity():
    repair = _text(".github/workflows/release.yml").split(
        "github-release-repair:", 1
    )[1]

    assert '[[ "$RELEASE_TAG" =~ ^v[0-9]+\\.[0-9]+\\.[0-9]+$ ]]' in repair
    assert "github.ref == 'refs/heads/main'" in repair
    assert '"repos/${GH_REPO}/git/ref/tags/${RELEASE_TAG}"' in repair
    assert '"repos/${GH_REPO}/git/tags/${tag_sha}"' in repair
    assert 'test "$object_type" = "commit"' in repair
    assert "--json databaseId,headBranch,headSha,event" in repair
    assert ".headBranch == $tag" in repair
    assert ".headSha == $sha" in repair
    assert '.event == "push"' in repair
    assert '.name == "Build distributions"' in repair
    assert '.name == "Publish to PyPI"' in repair
    assert '.name == "Assemble distributions"' in repair
    assert repair.count('.conclusion == "success"') >= 2
    assert 'gh run download "$run_id"' in repair
    assert '--repo "$GH_REPO"' in repair
    assert '.conclusion == "failure"' in repair
    assert repair.count("scripts/verify_release_artifacts.py") == 2
    assert "--allow-subset" in repair
    assert "--retries 18 --delay 10" in repair
    assert "skip-existing: true" in repair
    assert "id-token: write" in repair


def test_primary_github_release_targets_repository_without_checkout():
    release_job = _text(".github/workflows/release.yml").split(
        "github-release:", 1
    )[1].split("github-release-repair:", 1)[0]

    assert 'gh release view "$GITHUB_REF_NAME" --repo "$GH_REPO"' in release_job
    assert 'gh release create "$GITHUB_REF_NAME" dist/*' in release_job
    assert 'gh release upload "$GITHUB_REF_NAME" dist/*' in release_job
    assert '--repo "$GH_REPO"' in release_job
    assert "--clobber" in release_job

    repair_job = _text(".github/workflows/release.yml").split(
        "github-release-repair:", 1
    )[1]
    assert 'gh release upload "$RELEASE_TAG" dist/*' in repair_job
    assert "--clobber" in repair_job


