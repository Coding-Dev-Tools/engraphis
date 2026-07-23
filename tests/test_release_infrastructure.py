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
    local_api = template["variables"]["ENGRAPHIS_API_TOKEN"]
    assert local_api["value"] == "${{ secret(48) }}"
    assert local_api["secret"] is True
    assert local_api["required"] is True
    for removed in (
        "ENGRAPHIS_DEPLOYMENT_TOKEN",
        "ENGRAPHIS_LICENSE_KEY",
        "ENGRAPHIS_TEAM_MODE",
    ):
        assert removed not in template["variables"]


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


def test_release_builds_one_portable_open_core_wheel():
    release = _text(".github/workflows/release.yml")
    pyproject = _text("pyproject.toml")

    assert 'requires-python = ">=3.9"' in pyproject
    for version in ("3.9", "3.10", "3.11", "3.12"):
        assert f'"Programming Language :: Python :: {version}"' in pyproject
    assert not (ROOT / ".github/workflows/build-compiled-wheels.yml").exists()
    assert "cython" not in pyproject.lower()
    assert "cibuildwheel" not in release
    assert "run: python -m build\n" in release
    assert "Build compiled wheels" not in release
    assert "name: Assemble distributions" not in release
    assert "needs: [build, python-matrix, browser-accessibility, docker-smoke]" in release
    assert "name: python-package-distributions" in release


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
    assert "--json databaseId,headBranch,headSha,event,createdAt" in repair
    assert ".headBranch == $tag" in repair
    assert ".headSha == $sha" in repair
    assert '.event == "push"' in repair
    assert "sort_by(.createdAt)" in repair
    assert '.name == "Build distributions"' in repair
    assert '.name == "Publish to PyPI"' in repair
    assert '.name == "Assemble distributions"' not in repair
    assert repair.count('.conclusion == "success"') >= 2
    assert 'gh run download "$run_id"' in repair
    assert '--repo "$GH_REPO"' in repair
    assert '.conclusion == "failure"' in repair
    assert repair.count("scripts/verify_release_artifacts.py") == 2
    assert "--allow-subset" in repair
    assert "--retries 18 --delay 10" in repair
    assert repair.count("Freeze verified distribution set") == 1
    assert "--dist verified-dist" in repair
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


def test_public_capability_and_support_docs_match_the_shipped_tree():
    server = _text("engraphis/mcp_server.py")
    tools = re.findall(r'@mcp\.tool\(\s*name="(engraphis_[^"]+)"', server)
    assert len(tools) == len(set(tools)) == 29

    readme = _text("README.md")
    architecture = _text("docs/ARCHITECTURE_V3.md")
    skill = _text("skills/engraphis-memory/SKILL.md")
    skill_tools = _text("skills/engraphis-memory/references/TOOLS.md")
    skill_scoping = _text("skills/engraphis-memory/references/SCOPING.md")
    for content in (readme, architecture, skill):
        assert "28 MCP tools" not in content
        assert "28-tool" not in content
        assert "(28 of them)" not in content
    assert "29 MCP tools" in architecture
    assert "(29 of them)" in skill
    assert "`engraphis_check_update`" in readme
    for content in (skill, skill_tools, skill_scoping):
        assert "force_new" in content
        assert "reused" in content
    assert "(workspace, repo, authenticated user, agent, goal)" in skill_tools

    changelog = _text("CHANGELOG.md")
    assert "ForceGraph + D3 renderer" in changelog
    assert "## [1.0.1] - 2026-07-23" in changelog
    assert "Public 1.0.1 client reliability release." in changelog
    assert "## [1.0.0] - 2026-07-23" in changelog
    assert "## [1.0.0] - 2026-07-19" not in changelog
    assert "Public 1.0.0 open-core GA release." in changelog

    public_paths = [
        ROOT / name for name in (
            ".env.example", "AGENTS.md", "CHANGELOG.md", "NOTICE", "README.md",
            "SECURITY.md", "engraphis/config.py", "engraphis/routes/v2_api.py",
            "engraphis/static/dashboard.js", "engraphis/static/index.html",
        )
    ]
    public_paths.extend((ROOT / "docs").rglob("*.md"))
    public_paths.extend((ROOT / "skills").rglob("*.md"))
    for path in public_paths:
        content = path.read_text(encoding="utf-8").lower()
        assert "sigma" not in content, path
        assert "graphology" not in content, path
        assert "typescript graph worker" not in content, path
        assert "engraphis_graph_ui_v2" not in content, path
        assert "graph_ui_v2" not in content, path

    security = _text("SECURITY.md")
    normalized_security = re.sub(r"\s+", " ", security)
    normalized_readme = re.sub(r"\s+", " ", readme)
    assert "Private hosted service boundary" in security
    assert "latest published stable release is the supported line" in security
    assert "0.9.x) releases are no longer maintained" not in security
    assert "signing keys" in normalized_security
    assert "whole-database encryption" not in readme
    assert "Pro and Team are GA in v1.0.0" not in readme
    assert "Pro and Team are services" in readme
    assert "img.shields.io/badge/version-1.0.0" not in readme
    assert "img.shields.io/pypi/v/engraphis.svg" in readme
    assert "official hosted service" in readme
    assert "are generally available" not in readme
    assert "private repository" in normalized_readme
    assert not (ROOT / "docs" / "COMMERCIAL_OPERATIONS.md").exists()
    assert not (ROOT / ".github" / "workflows" / "commercial-backup.yml").exists()
