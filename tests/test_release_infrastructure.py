"""Static release-infrastructure invariants that must not drift silently."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest


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
    assert template["variables"]["ENGRAPHIS_HOST"]["value"] == "0.0.0.0"
    assert (
        template["variables"]["ENGRAPHIS_ENV_FILE"]["value"]
        == "/data/.engraphis/config.env"
    )
    assert template["service"]["healthcheck"] == "/api/ready"
    assert template["service"]["volume"]["mount_path"] == "/data"
    local_api = template["variables"]["ENGRAPHIS_API_TOKEN"]
    assert local_api["value"] == "${{ secret(48) }}"
    assert local_api["secret"] is True
    assert local_api["required"] is True
    # Railway supplies this system variable for the service's generated/custom public
    # domain.  Feeding it into the fixed dashboard URL lets MCP-over-HTTP accept the
    # real public Origin/Host without weakening its DNS-rebinding guard to a wildcard.
    dashboard_url = template["variables"]["ENGRAPHIS_DASHBOARD_URL"]
    assert dashboard_url["value"] == "https://${{RAILWAY_PUBLIC_DOMAIN}}"
    assert dashboard_url["required"] is False
    # Managed-compute consent travels with the cloud account. A template that shipped a
    # hard-coded value would override that for every deployment made from it -- "0" would
    # silently opt a connected deployment back out -- so the default must stay blank.
    managed_consent = template["variables"]["ENGRAPHIS_MANAGED_COMPUTE_CONSENT"]
    assert not managed_consent["value"]
    assert managed_consent["required"] is False
    for removed in (
        "ENGRAPHIS_DEPLOYMENT_TOKEN",
        "ENGRAPHIS_LICENSE_KEY",
        "ENGRAPHIS_TEAM_MODE",
        "RESEND_API_KEY",
    ):
        assert removed not in template["variables"]


def test_dependency_automation_covers_every_root_ecosystem():
    dependabot = _text(".github/dependabot.yml")
    ecosystems = re.findall(r'package-ecosystem:\s*"([^"]+)"', dependabot)

    assert sorted(ecosystems) == ["docker", "github-actions", "npm", "pip"]
    assert len(ecosystems) == len(set(ecosystems))
    assert "npm audit --audit-level=high" in _text(".github/workflows/ci.yml")
    assert "npm audit --audit-level=high" in _text(".github/workflows/release.yml")
    assert "npm ci --ignore-scripts --omit=optional" in _text(".github/workflows/ci.yml")
    assert "npm ci --ignore-scripts --omit=optional" in _text(
        ".github/workflows/release.yml"
    )
    package = json.loads(_text("package.json"))
    lock = json.loads(_text("package-lock.json"))
    version = package["devDependencies"]["impeccable"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", version)
    assert lock["packages"][""]["devDependencies"]["impeccable"] == version
    assert lock["packages"]["node_modules/impeccable"]["version"] == version


def test_playwright_server_never_opens_the_developers_configured_database():
    config = _text("playwright.config.js")

    assert "ENGRAPHIS_DB_PATH: ':memory:'" in config
    assert "reuseExistingServer: false" in config


def test_all_public_launchers_converge_on_the_v2_service():
    compose = _text("docker-compose.yml")
    readme = _text("README.md")
    docker_docs = _text("docs/DOCKER.md")
    dockerfile = _text("Dockerfile")
    launcher = _text("scripts/start_server.py")

    assert "engraphis-api:" not in compose
    assert "engraphis_v1.db" not in compose
    assert 'command: ["engraphis-dashboard", "--no-open"]' in compose
    assert '"127.0.0.1:${ENGRAPHIS_COMPOSE_PORT:-8700}:${ENGRAPHIS_COMPOSE_PORT:-8700}"' in compose
    assert '"url": "http://<host-LAN-IP>:8700/mcp/"' in docker_docs
    assert '".[server,mcp,documents,cloud-sync]"' in dockerfile
    assert (
            "[Docker deployment](https://github.com/Coding-Dev-Tools/engraphis/blob/522def372c45a26d46bc7a05829964ae0240f3a9/"
            "docs/DOCKER.md)"
        in readme
    )
    assert "The Docker image includes the streamable HTTP MCP endpoint" in docker_docs
    assert "ENGRAPHIS_API_TOKEN=<a-long-random-secret>" in docker_docs
    assert "docker-compose.lan.yml" in docker_docs
    assert "LAN overlay refuses to render" in docker_docs
    assert "ENGRAPHIS_DASHBOARD_URL" in docker_docs
    assert "ENGRAPHIS_COMPOSE_PORT" in docker_docs
    assert "start_dashboard.main(args)" in launcher
    assert "engraphis.app" not in launcher
    assert "engraphis-dashboard" in readme


def test_native_vector_backend_compatibility_stays_in_architecture_docs():
    readme = _text("README.md")
    architecture = _text("docs/ARCHITECTURE_V3.md")
    guidance = "`MemoryEngine.create()` and `MemoryService.create()` default to the exact NumPy index"

    assert guidance not in readme
    assert guidance in architecture


def test_advanced_query_planning_stays_in_architecture_docs():
    readme = _text("README.md")
    architecture = _text("docs/ARCHITECTURE_V3.md")
    guidance = "`planning=\"auto\"` keeps the original query"

    assert (
            "[Architecture and query planning](https://github.com/Coding-Dev-Tools/engraphis/blob/522def372c45a26d46bc7a05829964ae0240f3a9/"
            "docs/ARCHITECTURE_V3.md#query-planning)"
        in readme
    )
    assert guidance not in readme
    assert guidance in architecture
    assert "LLMQueryPlanner(my_llm)" in architecture


def test_pi_and_public_write_review_details_stay_in_supporting_docs():
    readme = _text("README.md")
    pi_guide = _text("integrations/pi/README.md")
    review_guide = _text("docs/WRITE_REVIEW.md")

    assert (
            "[Pi extension](https://github.com/Coding-Dev-Tools/engraphis/blob/522def372c45a26d46bc7a05829964ae0240f3a9/"
            "integrations/pi/README.md)"
        in readme
    )
    assert "pi install npm:@engraphis/pi" not in readme
    assert "Every advanced state-changing action requires an explicit Pi confirmation dialog" in pi_guide

    review_gate = "Normal local-agent memory creation is immediate"
    assert review_gate not in readme
    assert review_gate in review_guide
    assert "python -m scripts.rescan_poisoning --db engraphis.db --apply" in review_guide


def test_compose_keeps_container_safety_defaults_and_has_an_explicit_port_override():
    """Generic desktop .env values must not break the published container contract."""

    compose = _text("docker-compose.yml")
    readme = _text("README.md")
    docker_docs = _text("docs/DOCKER.md")

    lan_compose = _text("docker-compose.lan.yml")
    assert '"127.0.0.1:${ENGRAPHIS_COMPOSE_PORT:-8700}:${ENGRAPHIS_COMPOSE_PORT:-8700}"' in compose
    assert "ENGRAPHIS_HOST: 0.0.0.0" in compose
    assert "ENGRAPHIS_COMPOSE_HOST" not in compose
    assert "PORT: ${ENGRAPHIS_COMPOSE_PORT:-8700}" in compose
    assert "ENGRAPHIS_PORT: ${ENGRAPHIS_COMPOSE_PORT:-8700}" in compose
    assert "ENGRAPHIS_DB_PATH: /data/engraphis.db" in compose
    assert "ENGRAPHIS_STATE_DIR: /data/.engraphis" in compose
    assert "ports: !override" in lan_compose
    assert '"0.0.0.0:${ENGRAPHIS_COMPOSE_PORT:-8700}:${ENGRAPHIS_COMPOSE_PORT:-8700}"' in lan_compose
    assert "ENGRAPHIS_API_TOKEN: ${ENGRAPHIS_API_TOKEN:?Set a strong ENGRAPHIS_API_TOKEN for LAN use}" in lan_compose
    assert (
            "[Docker deployment](https://github.com/Coding-Dev-Tools/engraphis/blob/522def372c45a26d46bc7a05829964ae0240f3a9/"
            "docs/DOCKER.md)"
        in readme
    )
    assert "ENGRAPHIS_COMPOSE_PORT=8787" in docker_docs


def test_ci_and_release_audit_production_image_dependencies():
    ci = _text(".github/workflows/ci.yml")
    release = _text(".github/workflows/release.yml")
    release_build = release.split("  build:\n", 1)[1].split("  python-matrix:\n", 1)[0]
    release_docker = release.split("  docker-smoke:\n", 1)[1].split(
        "  release-evidence:\n", 1
    )[0]
    release_evidence = release.split("  release-evidence:\n", 1)[1].split(
        "  publish:\n", 1
    )[0]
    publish = release.split("  publish:\n", 1)[1].split("  github-release:\n", 1)[0]

    assert "Audit the exact production image dependency set" in ci
    assert "Validate Compose configuration" in ci
    assert "docker compose config --quiet" in ci
    assert "docker run --rm --entrypoint sh engraphis:ci" in ci
    ci_build = ci.split("  build:\n", 1)[1].split("  installed-journeys:\n", 1)[0]
    ci_docker = ci.split("  docker-smoke:\n", 1)[1].split("  build:\n", 1)[0]
    for audit_step, site in (
        (ci_build, "$AUDIT_SITE"),
        (ci_docker, "$audit_dir"),
        (release_docker, "$audit_dir"),
    ):
        assert "set -euo pipefail" in audit_step
        assert f'python scripts/pin_installed_audit_requirements.py "{site}"' in audit_step
        assert f'python -m pip_audit --vulnerability-service osv --path "{site}"' in audit_step
        assert (
            'python -m pip_audit --vulnerability-service pypi --no-deps '
            '--disable-pip -r "$RUNNER_TEMP/published-audit-requirements.txt"'
        ) in audit_step
    assert 'docker cp "$container:$site_packages/."' in ci
    legacy_audit_path = 'docker cp "$container":/usr/local/lib/python3.11/site-packages/.'
    assert legacy_audit_path not in ci
    assert "tesseract-ocr" in _text("Dockerfile")
    assert (
        _text("Dockerfile").splitlines()[1]
        == "FROM python:3.15.0-slim@sha256:"
        "f5b92f2a669a4c347044f29367f31983280b648250ca94dea66f98c53bc87e58 AS base"
    )
    assert "Verify production image OCR runtime" in ci
    assert "Verify production image OCR runtime" in release
    assert "docker-entrypoint\\.sh" in ci
    assert "docker-compose(\\.lan)?\\.yml" in ci
    assert "railway\\.json" in ci
    assert "deploy/" in ci
    for workflow in (ci, release):
        assert "Reject unauthenticated LAN Compose overlay" in workflow
        assert "env -u ENGRAPHIS_API_TOKEN docker compose -f docker-compose.yml -f docker-compose.lan.yml config --quiet" in workflow
        assert "ENGRAPHIS_API_TOKEN: ci-lan-overlay-token" in workflow
        assert "Validate token-protected LAN Compose overlay" in workflow
    assert 'pip setuptools wheel build twine pip-audit cyclonedx-bom ".[all,test]"' in release_build
    assert "python -m pip_audit --local --skip-editable" in release_build
    assert "python scripts/normalize_sdist.py dist/*.tar.gz" in release_build
    assert "pip list --format=freeze" in release_build
    assert "name: build-environment-evidence" in release_build
    assert "engine.store.close()" in release_build
    assert "engine.close()" not in release_build
    assert "docker buildx build --pull --load" in release_docker
    assert "--metadata-file container-evidence/build-metadata.json" in release_docker
    assert '"containerimage.digest"' in release_docker
    assert "Validate Compose configuration" in release_docker
    assert "docker compose config --quiet" in release_docker
    assert "Audit production image dependencies" in release_docker
    assert "pip-audit==2.10.1" in release_docker
    assert "grype-version: v0.110.0" in release_docker
    assert 'docker create --name "$container" engraphis:release' in release_docker
    assert 'docker cp "$container:$site_packages/."' in release_docker
    assert legacy_audit_path not in release_docker
    assert "reproducibility-check" in release_evidence.split("needs:", 1)[1].splitlines()[0]
    assert "installed-artifact-platform-smoke" in (
        release_evidence.split("needs:", 1)[1].splitlines()[0]
    )
    assert "needs: release-evidence" in publish
    assert "Browser accessibility release gate" in release
    assert "Require release tag commit to be on protected main" in release
    for version in ('"3.9"', '"3.10"', '"3.11"', '"3.12"'):
        assert version in release


def test_ci_and_release_never_hide_skips_or_lose_the_full_stack_silently():
    """The configured ``-q`` plus a workflow ``-q`` used to hide counts and skips."""

    for path in (".github/workflows/ci.yml", ".github/workflows/release.yml"):
        workflow = _text(path)
        assert "python -m pytest tests/ -q" not in workflow
        assert 'python -m pytest -o addopts="" tests/ -q -rs' in workflow
    required = 'import fastapi, httpx, mcp, multipart, pydantic, uvicorn'
    assert required in _text(".github/workflows/ci.yml")
    assert required in _text(".github/workflows/release.yml")


def test_release_runs_the_offline_evaluations_required_by_ci():
    """A protected-main CI gate must remain a gate on the exact release source."""

    ci = _text(".github/workflows/ci.yml")
    release = _text(".github/workflows/release.yml")
    build = release.split("  build:\n", 1)[1].split("  reproducibility-build:\n", 1)[0]
    evaluations = re.findall(r"run:\s*(python -m eval\.[^\n]+)", ci)
    assert "python -m eval.spec_crawl" in evaluations
    for command in evaluations:
        assert command in build, f"release build omits the CI evaluation: {command}"


def test_sqlcipher_driver_has_a_dedicated_short_lived_integration_gate():
    """A bundled SQLCipher extension must not leak into the general test process.

    Its at-rest contract still runs for every supported full-stack Python version;
    separating the native driver avoids a cross-extension GC crash without making
    encryption coverage optional.
    """
    pyproject = _text("pyproject.toml")
    general_test = pyproject.split("test = [", 1)[1].split("\n]", 1)[0]
    encryption = pyproject.split("encryption = [", 1)[1].split("\n]", 1)[0]

    assert "sqlcipher3-binary" not in general_test
    assert "sqlcipher3-binary" in encryption
    for path in (".github/workflows/ci.yml", ".github/workflows/release.yml"):
        workflow = _text(path)
        assert "encryption:" in workflow
        assert 'pip install -e ".[test,encryption]"' in workflow
        assert "tests/test_encrypted_store.py" in workflow
        assert 'python-version: ["3.10", "3.11", "3.12", "3.13", "3.14"]' in workflow

    release = _text(".github/workflows/release.yml")
    release_evidence = release.split("  release-evidence:\n", 1)[1].split(
        "  publish:\n", 1,
    )[0]
    assert "reproducibility-check" in release_evidence
    assert "encryption" in release_evidence.split("needs:", 1)[1].splitlines()[0]


def test_release_builds_one_portable_open_core_wheel():
    ci = _text(".github/workflows/ci.yml")
    release = _text(".github/workflows/release.yml")
    pyproject = _text("pyproject.toml")

    assert 'requires-python = ">=3.9"' in pyproject
    for version in ("3.9", "3.10", "3.11", "3.12", "3.13", "3.14"):
        assert f'"Programming Language :: Python :: {version}"' in pyproject
    assert 'python-version: ["3.10", "3.11", "3.12", "3.13", "3.14"]' in ci
    assert (
        'python-version: ["3.9", "3.10", "3.11", "3.12", "3.13", "3.14"]'
        in release
    )
    assert not (ROOT / ".github/workflows/build-compiled-wheels.yml").exists()
    assert "cython" not in pyproject.lower()
    assert "cibuildwheel" not in release
    assert release.count("python -m build") == 2
    assert "python -m build --outdir dist-repeat" not in release
    assert 'builder: ["a", "b"]' in release
    assert "github-hosted:ubuntu-latest/python-3.11" in release
    assert "Compare independent distribution builders" in release
    assert "python scripts/verify_distribution_contents.py dist/*" in release
    assert "Build compiled wheels" not in release
    assert "name: Assemble distributions" not in release
    assert "  release-evidence:\n" in release
    assert "needs: release-evidence" in release
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


def test_workflows_and_shell_scripts_are_lf_normalized():
    paths = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
    paths += sorted((ROOT / ".github" / "workflows").glob("*.yaml"))
    paths += sorted(ROOT.rglob("*.sh"))

    assert paths
    for path in paths:
        assert b"\r\n" not in path.read_bytes(), (
            f"{path.relative_to(ROOT)} contains CRLF line endings"
        )
    attributes = _text(".gitattributes")
    assert ".github/workflows/*.yml text eol=lf" in attributes
    assert ".github/workflows/*.yaml text eol=lf" in attributes
    assert "*.sh text eol=lf" in attributes
    assert "deploy/*.lock text eol=lf" in attributes
    assert "deploy/*.licenses.json text eol=lf" in attributes


def test_ci_and_release_default_to_read_only_repository_permissions():
    for workflow in (
            ".github/workflows/ci.yml",
            ".github/workflows/release.yml"):
        header = _text(workflow).split("\njobs:", 1)[0]
        assert "\npermissions:\n  contents: read\n" in header


def test_codeql_workflow_fails_when_sarif_contains_findings():
    workflow = _text(".github/workflows/codeql.yml")

    # CodeQL's PR default is diff-informed and omits findings outside the
    # patch. The release gate must instead inspect complete raw SARIF.
    assert 'CODEQL_ACTION_DIFF_INFORMED_QUERIES: "false"' in workflow
    assert "id: analyze" in workflow
    assert "output: codeql-results" in workflow
    assert (
        'python scripts/check_codeql_sarif.py '
        '"${{ steps.analyze.outputs.sarif-output }}"'
    ) in workflow


def test_tag_release_binds_codeql_reproducibility_and_installed_artifact_smokes():
    ci = _text(".github/workflows/ci.yml")
    release = _text(".github/workflows/release.yml")
    constraints = _text(".github/release-constraints.txt")
    codeql = release.split("  code-security:\n", 1)[1].split(
        "  release-evidence:\n", 1
    )[0]
    build = release.split("  build:\n", 1)[1].split("  python-matrix:\n", 1)[0]
    evidence = release.split("  release-evidence:\n", 1)[1].split(
        "  publish:\n", 1
    )[0]

    assert "PIP_CONSTRAINT: ${{ github.workspace }}/.github/release-constraints.txt" in build
    assert "PIP_BUILD_CONSTRAINT: ${{ github.workspace }}/.github/release-constraints.txt" in build
    for pin in (
        "pip==26.2",
        "setuptools==84.1.0",
        "wheel==0.47.0",
        "build==1.5.0",
        "twine==6.2.0",
        "pip-audit==2.10.1",
        "cyclonedx-bom==7.3.0",
    ):
        assert pin in constraints
    assert 'language: ["python", "javascript-typescript"]' in codeql
    assert 'CODEQL_ACTION_DIFF_INFORMED_QUERIES: "false"' in codeql
    assert "contents: read\n      security-events: write" in codeql
    assert "github/codeql-action/init@" in codeql
    assert "github/codeql-action/analyze@" in codeql
    assert "upload: never" in codeql
    assert "scripts/check_codeql_sarif.py" in codeql
    assert "Smoke installed wheel and source distribution" in build
    assert '"$venv/bin/python" -m scripts.smoke_entry_points --timeout 20' in build
    assert "pathlib.Path(sys.prefix).resolve() in package.parents" in build
    assert "Python 3.9 installed release artifacts" in release
    py39_artifacts = release.split("  artifact-core-py39:\n", 1)[1].split(
        "  encryption:\n", 1
    )[0]
    assert "needs: build" in py39_artifacts
    assert 'python-version: "3.9"' in py39_artifacts
    assert "Download exact release distributions" in py39_artifacts
    assert "name: python-package-distributions" in py39_artifacts
    assert '"$venv/bin/python" -m pip install --disable-pip-version-check "$artifact"' in py39_artifacts
    assert '"$venv/bin/python" -m pip check' in py39_artifacts
    assert '"$venv/bin/engraphis-cli" --help' in py39_artifacts
    assert "--verified-check codeql" in evidence
    assert "--verified-check reproducible-distributions" in evidence
    assert "--verified-check installed-artifact-smoke" in evidence
    assert "--verified-check installed-artifact-smoke-py39" in evidence
    assert "--verified-check installed-artifact-platform-smoke" in evidence
    ci_build = ci.split("  build:\n", 1)[1]
    assert "Install pinned build and audit tooling" in ci_build
    assert '"build==1.5.0" "pip-audit==2.10.1"' in ci_build
    ci_docker = ci.split("  docker-smoke:\n", 1)[1].split("  build:\n", 1)[0]
    assert "python -m pip install --disable-pip-version-check --no-cache-dir" in ci_docker
    assert "pip-audit==2.10.1" in ci_docker
    ci_py39 = ci.split("  core-py39:\n", 1)[1].split("  coverage:\n", 1)[0]
    assert "Build and smoke installed core artifacts" in ci_py39
    assert '"build==1.2.2"' in ci_py39
    assert '"$venv/bin/python" -m pip install --disable-pip-version-check "$artifact"' in ci_py39
    assert '"$venv/bin/python" -m pip check' in ci_py39


def test_ci_linter_is_bounded_to_the_verified_release_series():
    pyproject = _text("pyproject.toml")

    # A version bound alone never made the linter deterministic: ruff's *default* rule set
    # is not stable across minor releases -- 0.16 widened it from 59 rules to 413, which
    # would have turned `ruff check .` red on unchanged code. Pinning `select` explicitly
    # is what actually bounds CI, so the bound and the rule set are asserted together.
    assert pyproject.count('"ruff>=0.15.22,<0.18"') == 2
    assert 'select = ["E4", "E7", "E9", "F"]' in pyproject


def test_pyright_core_backend_ratchet_is_pinned_and_runs_in_ci_and_release():
    pyproject = _text("pyproject.toml")
    ci = _text(".github/workflows/ci.yml")
    release = _text(".github/workflows/release.yml")

    # Keep the dev and test extras on one exact, Dependabot-updatable Pyright pin.
    pyright_pins = re.findall(r'"(pyright==[^"\n]+)"', pyproject)
    assert len(pyright_pins) == 2
    assert pyright_pins[0] == pyright_pins[1]
    assert '"engraphis/core",\n    "engraphis/backends",' in pyproject
    assert '"eval/harness.py",\n    "eval/external.py",' in pyproject
    assert 'pythonVersion = "3.9"' in pyproject
    assert 'typeCheckingMode = "basic"' in pyproject
    typecheck = ci.split("  typecheck:\n", 1)[1].split("  encryption:\n", 1)[0]
    assert "core + backends typecheck (Python 3.11)" in typecheck
    assert 'python-version: "3.11"' in typecheck
    assert 'pip install -e ".[test]"' in typecheck
    assert "run: pyright" in typecheck
    build = release.split("  build:\n", 1)[1].split("  python-matrix:\n", 1)[0]
    assert "          pyright" in build
    assert "--verified-check pyright-core-backends" in release

def test_release_repair_requires_tag_sha_successful_build_publish_and_pypi_identity():
    repair = _text(".github/workflows/release.yml").split(
        "github-release-repair:", 1
    )[1]

    assert '[[ "$RELEASE_TAG" =~ ^v[0-9]+\\.[0-9]+(\\.[0-9]+)?$ ]]' in repair
    assert "github.ref == 'refs/heads/main'" in repair
    assert '"repos/${GH_REPO}/git/ref/tags/${RELEASE_TAG}"' in repair
    assert '"repos/${GH_REPO}/git/tags/${tag_sha}"' in repair
    assert 'test "$object_type" = "commit"' in repair
    assert "--json databaseId,headBranch,headSha,event,createdAt" in repair
    assert "repair_run_candidates" in repair
    assert "while IFS= read -r candidate" in repair
    assert '.name == "Build distributions"' in repair
    assert '.name == "Publish to PyPI"' in repair
    assert '.name == "Generate public release evidence"' in repair
    assert '.name == "Assemble distributions"' not in repair
    assert repair.count('.conclusion == "success"') >= 2
    assert 'gh run download "$candidate"' in repair
    assert 'evidence_root / "release-evidence.json"' in repair
    assert 'assert evidence.get("tag") == tag' in repair
    assert 'assert evidence.get("commit") == commit' in repair
    assert 'assert evidence.get("package", {}).get("version") == tag.removeprefix("v")' in repair
    assert 'hashlib.sha256(path.read_bytes()).hexdigest()' in repair
    assert 'assert expected == actual' in repair
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
    assert 'gh release upload "$RELEASE_TAG" verified-dist/*' in repair_job
    assert 'gh release create "$RELEASE_TAG" verified-dist/*' in repair_job
    assert '"$RELEASE_TAG" dist/*' not in repair_job
    assert "--clobber" in repair_job


def test_public_capability_and_support_docs_match_the_shipped_tree():
    server = _text("engraphis/mcp_server.py")
    tools = re.findall(r'@mcp\.tool\(\s*name="(engraphis_[^"]+)"', server)
    assert len(tools) == len(set(tools)) == 43

    readme = _text("README.md")
    licensing = _text("docs/LICENSING.md")
    architecture = _text("docs/ARCHITECTURE_V3.md")
    skill = _text("skills/engraphis-memory/SKILL.md")
    skill_tools = _text("skills/engraphis-memory/references/TOOLS.md")
    skill_scoping = _text("skills/engraphis-memory/references/SCOPING.md")
    for content in (readme, architecture, skill):
        assert "28 MCP tools" not in content
        assert "28-tool" not in content
        assert "(28 of them)" not in content
    assert "Smart MCP (9 tools)" in architecture
    assert "Classic MCP (43 tools)" in architecture
    assert "default Smart MCP surface has nine" in skill
    assert "Classic direct-tool guide" in skill
    assert "engraphis-mcp-classic" in skill
    assert "recall_context (compact)" in architecture
    assert "engraphis_recall_context" in readme
    mcp_reference = _text("docs/MCP_TOOLS.md")
    assert (
        "[MCP tool reference](https://github.com/Coding-Dev-Tools/engraphis/blob/522def372c45a26d46bc7a05829964ae0240f3a9/"
        "docs/MCP_TOOLS.md)" in readme
    )
    assert "`engraphis_check_update`" in mcp_reference
    for content in (skill, skill_tools, skill_scoping):
        assert "force_new" in content
        assert "reused" in content
    assert "(workspace, repo, authenticated user, agent, goal)" in skill_tools

    changelog = _text("CHANGELOG.md")
    evidence = _text("eval/EVIDENCE.md")
    runbook = _text("docs/PUBLIC_BENCHMARK_RUNBOOK.md")
    seed_script = _text("scripts/seed_from_obsidian.py")
    assert changelog.count("## [1.3.0] - 2026-08-01") == 1
    assert "ENGRAPHIS_EVIDENCE_RUN_DIR=/path/to/restricted/longmemeval-v2" in evidence
    assert "/private/longmemeval-v2" not in evidence
    assert "ENGRAPHIS_BENCHMARK_RUN_DIR=/path/to/restricted/benchmark-run" in runbook
    assert "private/point.json" not in runbook
    assert "private/comparison-series.json" not in runbook
    assert "C:/Users/home/" not in seed_script
    assert "ForceGraph + D3 renderer" in changelog
    assert "## [1.1.0] - 2026-07-26" in changelog
    assert "Public 1.1.0 hosted-connect and graph-experience release." in changelog
    assert "## [1.0.1] - 2026-07-24" in changelog
    assert "Public 1.0.1 client reliability release." in changelog
    assert "## [1.0.0] - 2026-07-23" in changelog
    assert "## [1.0.0] - 2026-07-19" not in changelog
    assert "Public 1.0.0 open-core GA release." in changelog

    public_paths = [
        ROOT / name for name in (
            ".env.example", "AGENTS.md", "CHANGELOG.md", "NOTICE", "README.md",
            "SECURITY.md", "engraphis/config.py", "engraphis/routes/v2_api.py",
            "engraphis/dashboard_assets/index.html",
            "engraphis/dashboard_assets/ledger.css",
            "engraphis/dashboard_assets/ledger.js",
            # First-party Ledger scripts split out of ledger.js.
            "engraphis/dashboard_assets/action-dialog.js",
            "engraphis/dashboard_assets/graph-explorer.js",
            "engraphis/dashboard_assets/import-sources.js",
            "engraphis/dashboard_assets/markdown.js",
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
    assert "Private hosted service boundary" in security
    assert "latest published stable release is the supported line" in security
    assert "0.9.x) releases are no longer maintained" not in security
    assert "signing keys" in normalized_security
    assert "whole-database encryption" not in readme
    assert "Pro and Team are GA in v1.0.0" not in readme
    assert "The hosted control plane and managed services are private services" in readme
    assert "img.shields.io/badge/version-1.0.0" not in readme
    assert (
        "[![PyPI version](https://img.shields.io/pypi/v/engraphis.svg)]"
        "(https://pypi.org/project/engraphis/)"
        in readme
    )
    assert (
        'src="https://raw.githubusercontent.com/Coding-Dev-Tools/engraphis/522def372c45a26d46bc7a05829964ae0240f3a9/'
        'docs/images/knowledge-graph.png"' in readme
    )
    assert re.search(
        r'src="https://raw\.githubusercontent\.com/Coding-Dev-Tools/engraphis/(?:[0-9a-f]{40}|v1\.7\.10)/'
        r'docs/images/context-efficiency\.svg"',
        readme,
    )
    assert "official hosted service" in licensing
    assert "are generally available" not in readme
    assert "private repository" in licensing
    assert not (ROOT / "docs" / "COMMERCIAL_OPERATIONS.md").exists()
    assert not (ROOT / ".github" / "workflows" / "commercial-backup.yml").exists()


def test_grype_configuration_ignores_upstream_cpython_cves():
    grype = _text(".grype.yaml")
    assert 'vulnerability: "CVE-2026-82049"' in grype
    assert 'name: "stdlib"' in grype
    assert 'type: "go-module"' in grype


def _job_block(workflow: str, job: str) -> str:
    match = re.search(rf"(?ms)^  {re.escape(job)}:\n(.*?)(?=^  \S[^:\n]*:\n|\Z)", workflow)
    assert match is not None, f"release job {job!r} is missing"
    return match.group(1)


def test_python39_jobs_use_a_runner_with_the_supported_toolcache():
    """Ubuntu 26.04 does not provide Python 3.9 through actions/setup-python."""
    for workflow, jobs in (
        (".github/workflows/ci.yml", ("core-py39",)),
        (".github/workflows/release.yml", ("python-matrix", "artifact-core-py39")),
    ):
        content = _text(workflow)
        for job in jobs:
            body = _job_block(content, job)
            assert re.search(r"(?m)^    runs-on: ubuntu-24\.04$", body), job


def test_release_transitive_dependency_security_floors_cannot_regress():
    package = json.loads(_text("integrations/pi/package.json"))
    lock = json.loads(_text("integrations/pi/npm-shrinkwrap.json"))
    expected_overrides = {
        "@hono/node-server": "^2.1.8",
        "body-parser": "^2.4.0",
        "fast-uri": "^3.1.9",
        "hono": "^4.13.18",
        "minimatch": "^11.0.3",
        "proxy-addr": "2.0.8",
        "undici": "^8.12.3",
        "zod": "^4.6.6",
    }
    for name, floor in expected_overrides.items():
        assert package["overrides"][name] == floor
        min_version = tuple(map(int, floor.removeprefix("^").split(".")))
        top_key = f"node_modules/{name}"
        assert top_key in lock["packages"]
        suffix = f"/{top_key}"
        for pkg_path, entry in lock["packages"].items():
            if pkg_path == top_key or pkg_path.endswith(suffix):
                assert tuple(map(int, entry["version"].split("."))) >= min_version, pkg_path

    # The Pi test host is pinned to a release whose own shrinkwrap resolves the patched
    # leaves. Earlier releases needed a post-install repair, which could not clear the
    # lock-based audit; the overrides now do, so CI audits without rewriting node_modules.
    assert package["devDependencies"]["@earendil-works/pi-coding-agent"] == "1.4.0"
    ci = _text(".github/workflows/ci.yml")
    assert "brace-expansion@" not in ci
    assert "npm audit --omit=dev" in ci
    assert "npm audit --no-package-lock --include=dev" in ci
    assert 'pip install --upgrade "filelock>=4.2.1,<5"' in _text("Dockerfile")


def test_privileged_publication_jobs_use_no_dependency_cache():
    """A privileged publication job must not be fed by a shared dependency cache.

    ``actions/setup-python``'s pip cache key is derived only from the runner
    image, the interpreter version, and the hashed dependency files — not from
    the ref — so a job that holds the PyPI OIDC token or release write
    permission can be served by a cache that branch CI also writes.  The
    privileged jobs therefore declare no cache at all.
    """
    release = _text(".github/workflows/release.yml")
    for job in ("publish", "github-release", "github-release-repair"):
        body = _job_block(release, job)
        assert "cache:" not in body, f"{job} declares a dependency cache"
        assert "cache-dependency-path:" not in body, f"{job} declares a cache path"

    # The non-privileged verification jobs may still cache, and their cache
    # key is bound to the pinned release constraints so it cannot be reused
    # for a different dependency set.
    build = _job_block(release, "build")
    assert "cache: pip" in build
    assert ".github/release-constraints.txt" in build

    # Nothing in the publication path may run on an untrusted pull request.
    assert "pull_request_target" not in release
    assert "pull_request" not in release


def test_release_publication_verifies_signed_qualification_when_opted_in():
    """Environment approval always gates publication; the signed envelope is opt-in.

    With ``REQUIRE_SIGNED_QUALIFICATION=true`` a tag run without the qualification
    secrets must stop before publishing. Unset, the verifier step is skipped and the
    protected environment approval plus the build gates remain the requirement.
    """
    release = _text(".github/workflows/release.yml")
    publish = _job_block(release, "publish")
    release_job = _job_block(release, "github-release")

    for body in (publish, release_job):
        assert "environment: release-qualification" in body
        step = body.split("- name: Require signed full-product qualification before ", 1)[1]
        step = step.split("\n      - ", 1)[0]
        assert "\n        if: vars.REQUIRE_SIGNED_QUALIFICATION == 'true'\n" in step
        assert "scripts.verify_release_qualification" in step
        assert "continue-on-error" not in step
        for secret in (
            "ENGRAPHIS_RELEASE_QUALIFICATION",
            "ENGRAPHIS_RELEASE_VERIFY_KEY",
            "ENGRAPHIS_RELEASE_CANDIDATE_ID",
            "ENGRAPHIS_RELEASE_LEDGER_SHA256",
        ):
            assert f"secrets.{secret}" in body, secret
        qualification = body.index("scripts.verify_release_qualification")
        assert "pypa/gh-action-pypi-publish" not in body[:qualification]
        assert "gh release create" not in body[:qualification]

    # The qualification gate runs before the publisher and before the release.
    publish_qualification = publish.index("scripts.verify_release_qualification")
    assert publish.index("pypa/gh-action-pypi-publish") > publish_qualification
    assert publish.index("Freeze verified distribution set") < publish_qualification
    release_qualification = release_job.index("scripts.verify_release_qualification")
    assert release_job.index("Create GitHub Release") > release_qualification

    # The publisher is the only step that can write to PyPI, and the exact
    # published file set is re-verified afterwards.
    assert publish.count("pypa/gh-action-pypi-publish") == 1
    assert "skip-existing: true" in publish
    assert "Require the exact complete PyPI file set" in publish
    assert "--retries 18 --delay 10" in publish


def test_release_tag_must_be_pushed_from_protected_main():
    """A tag on an unmerged commit must never reach the publication jobs."""
    release = _text(".github/workflows/release.yml")
    build = _job_block(release, "build")

    assert "Require tag and package version to match" in build
    assert 'test "$GITHUB_REF_NAME" = "v$actual"' in build
    assert "Require release tag commit to be on protected main" in build
    assert "git fetch --no-tags origin main:refs/remotes/origin/main" in build
    assert 'git merge-base --is-ancestor "$GITHUB_SHA" origin/main' in build

    # The trigger accepts version tags only, and never a branch push.
    assert re.search(r"(?ms)^on:\n  push:\n    tags:\n      - \"v\*\.\*\"\n", release)
    assert "branches:" not in release.split("jobs:", 1)[0]


def test_release_workflow_permissions_are_least_privilege():
    """Only the jobs that need a write scope may request one."""
    release = _text(".github/workflows/release.yml")

    publish = _job_block(release, "publish")
    assert "id-token: write" in publish
    assert "contents: read" in publish
    assert "contents: write" not in publish

    release_job = _job_block(release, "github-release")
    assert "contents: write" in release_job
    assert "id-token: write" not in release_job

    repair = _job_block(release, "github-release-repair")
    assert "contents: write" in repair
    assert "id-token: write" in repair

    # The verification jobs stay read-only.
    for job in ("build", "reproducibility-build", "python-matrix", "release-evidence"):
        body = _job_block(release, job)
        assert "contents: write" not in body, job
        assert "id-token: write" not in body, job


def _powershell_shells() -> list[str]:
    """Return installed PowerShell executables (`pwsh`, `powershell`), deduplicated."""
    shells: list[str] = []
    seen: set[str] = set()
    for name in ("pwsh", "powershell"):
        exe = shutil.which(name)
        if not exe:
            continue
        key = str(Path(exe).resolve()).lower()
        if key not in seen:
            seen.add(key)
            shells.append(exe)
    return shells


def _write_preflight_fixture(tmp_path: Path, *, include_datasets: bool = True) -> dict[str, Path]:
    """Create an isolated repo + cross-platform CLI shims to run `run_preflight.ps1`."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "tests").mkdir(parents=True)
    shutil.copy2(ROOT / "run_preflight.ps1", repo / "run_preflight.ps1")
    for script_name in ("check_commercial_manifest.py", "externalize_dashboard_assets.py"):
        (repo / "scripts" / script_name).write_text("raise SystemExit(0)\n", encoding="utf-8")
    for test_name in ("test_adaptive_context.py", "test_v2_service_binding.py"):
        (repo / "tests" / test_name).write_text("", encoding="utf-8")
    if include_datasets:
        (repo / "eval" / "datasets").mkdir(parents=True)
        for dataset_name in ("sample.jsonl", "codemem.jsonl"):
            (repo / "eval" / "datasets" / dataset_name).write_text("{}\n", encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    trace_file = tmp_path / "trace.jsonl"
    driver = tmp_path / "shim_driver.py"
    driver.write_text(
        """import json
import os
import sys
from pathlib import Path

tool = sys.argv[1]
args = sys.argv[2:]
trace_path = Path(os.environ["PREFLIGHT_TRACE_FILE"])
with trace_path.open("a", encoding="utf-8") as fh:
    fh.write(json.dumps({
        "tool": tool,
        "args": args,
        "cwd": os.getcwd(),
        "ENGRAPHIS_INDEX_ROOTS": os.environ.get("ENGRAPHIS_INDEX_ROOTS"),
        "PYTEST_ADDOPTS": os.environ.get("PYTEST_ADDOPTS"),
    }) + "\\n")

if os.environ.get("PREFLIGHT_STDERR_NOISE"):
    sys.stderr.write("import time: simulated stderr noise\\n")

if tool == "python" and len(args) >= 2 and args[0] == "-c":
    code = args[1]
    if "version_info" in code:
        sys.stdout.write(os.environ.get("PREFLIGHT_PY_VERSION", "3.11") + "\\n")
        raise SystemExit(0)
    if "gettempdir" in code:
        sys.stdout.write(os.environ.get("PREFLIGHT_PY_TEMPDIR", str(trace_path.parent)) + "\\n")
        raise SystemExit(0)
    if "import_module" in code and len(args) >= 3:
        missing = {m for m in os.environ.get("PREFLIGHT_MISSING_MODULES", "").split(",") if m}
        raise SystemExit(1 if args[2] in missing else 0)

if tool == "python" and len(args) >= 2 and args[0] == "-m" and args[1] == "pytest":
    rc = int(os.environ.get("PREFLIGHT_PYTEST_EXIT", "0"))
    if rc != 0:
        sys.stdout.write("stdout noise before failure\\n")
    raise SystemExit(rc)

raise SystemExit(0)
""",
        encoding="utf-8",
    )

    for tool in ("python", "ruff", "pyright"):
        if os.name == "nt":
            shim = bin_dir / f"{tool}.cmd"
            shim.write_text(
                f'@echo off\r\n"{sys.executable}" "{driver}" {tool} %*\r\n',
                encoding="utf-8",
            )
        else:
            shim = bin_dir / tool
            shim.write_text(
                f'#!/bin/sh\nexec "{sys.executable}" "{driver}" {tool} "$@"\n',
                encoding="utf-8",
            )
            shim.chmod(0o755)

    return {
        "repo": repo,
        "bin_dir": bin_dir,
        "outside_dir": outside_dir,
        "trace_file": trace_file,
    }


def _invoke_preflight(
    shell: str,
    fixture: dict[str, Path],
    ps_args: str,
    extra_env: dict[str, str],
) -> tuple[subprocess.CompletedProcess[str], list[dict[str, Any]], dict[str, Any]]:
    """Run `run_preflight.ps1` via PowerShell and return result, trace, and caller state."""
    if fixture["trace_file"].exists():
        fixture["trace_file"].unlink()
    env = os.environ.copy()
    env["PATH"] = os.pathsep.join([str(fixture["bin_dir"]), env.get("PATH", "")])
    env["PREFLIGHT_TRACE_FILE"] = str(fixture["trace_file"])
    for key in (
        "PREFLIGHT_STDERR_NOISE",
        "PREFLIGHT_PY_VERSION",
        "PREFLIGHT_MISSING_MODULES",
        "PREFLIGHT_PYTEST_EXIT",
    ):
        env.pop(key, None)
    env.update(extra_env)

    script_escaped = str(fixture["repo"] / "run_preflight.ps1").replace("'", "''")
    cmd = (
        f"& '{script_escaped}' {ps_args}; "
        "$exitCode = $LASTEXITCODE; "
        "Write-Output ('__CALLER_STATE__=' + ((@{ "
        "cwd = (Get-Location).Path; "
        "index = $env:ENGRAPHIS_INDEX_ROOTS; "
        "addopts = $env:PYTEST_ADDOPTS; "
        "exit_code = $exitCode "
        "}) | ConvertTo-Json -Compress))"
    )
    proc = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-Command", cmd],
        cwd=str(fixture["outside_dir"]),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    traces: list[dict[str, Any]] = []
    if fixture["trace_file"].exists():
        traces = [
            json.loads(line)
            for line in fixture["trace_file"].read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    state: dict[str, Any] = {}
    for line in proc.stdout.splitlines():
        if line.startswith("__CALLER_STATE__="):
            state = json.loads(line.split("=", 1)[1])
    return proc, traces, state


def test_run_preflight_ps1_aligns_with_ci_gates_and_checks_dependencies():
    """The local pre-flight must run the offline CI gates, not a comfortable subset."""
    script = _text("run_preflight.ps1")

    for command in (
        "ruff check .",
        "scripts/check_commercial_manifest.py",
        "scripts/externalize_dashboard_assets.py",
        "pyright",
        "pytest",
    ):
        assert command in script, command

    # Every eval module the offline CI job runs, and both JSONL datasets it feeds the
    # harness. A dropped module silently shrinks the gate instead of failing it.
    assert set(re.findall(r'"eval\.([a-z_]+)"', script)) == {
        "harness",
        "ablation",
        "reinforcement",
        "adversarial_memory_security",
        "grounded",
        "code_arm",
        "spec_crawl",
        "evidence_contracts",
    }
    assert len(re.findall(r'"eval\.harness"', script)) == 2
    assert '"eval/datasets/sample.jsonl"' in script
    assert '"eval/datasets/codemem.jsonl"' in script

    assert 'Invoke-GateStep "Pyright Type Check"' in script
    assert 'Invoke-GateStep "Offline Unit Test Suite"' in script

    # A missing dataset must never be folded into `continue`, which would skip green.
    assert re.search(r"-not\s*\(\s*Test-Path[^\n]*\bcontinue\b", script) is None
    assert "importlib.import_module(sys.argv[1])" in script
    assert "$env:ENGRAPHIS_INDEX_ROOTS" in script
    assert "$env:PYTEST_ADDOPTS" in script

    # CI neutralizes ini addopts, keeps skip reasons visible, and isolates basetemp.
    for flag in ('"-o"', '"addopts="', '"-q"', '"-rs"', '"--basetemp"'):
        assert flag in script, flag

    # A venv without the full-stack extras would skip server/MCP tests silently, while
    # ".[all,dev]" drags in heavy optional ML extras that the offline gate does not need.
    assert '"fastapi", "httpx", "mcp", "multipart", "pydantic", "sqlite_vec", "uvicorn"' in script
    assert 'pip install -e ".[test]"' in script

    # Derive the parity list from the CI job itself so a new offline gate cannot be added
    # to CI and silently left out of the local pre-flight.
    for job in ("test", "core-py39"):
        block = _job_block(_text(".github/workflows/ci.yml"), job)
        for dataset in re.findall(r"eval/datasets/[\w.-]+\.jsonl", block):
            assert f'"{dataset}"' in script, dataset
        for module in re.findall(r"python -m (eval\.[a-z_]+)", block):
            assert f'"{module}"' in script, module


def test_run_preflight_ps1_parses_without_powershell_syntax_errors():
    shells = _powershell_shells()
    if not shells:
        pytest.skip("pwsh/powershell is not installed")
    script_path = str(ROOT / "run_preflight.ps1").replace("'", "''")
    cmd = (
        "$errs = $null; [void][System.Management.Automation.Language.Parser]::ParseFile("
        f"'{script_path}', $null, [ref]$errs); Write-Output $errs.Count"
    )
    for shell in shells:
        proc = subprocess.run(
            [shell, "-NoProfile", "-NonInteractive", "-Command", cmd],
            capture_output=True,
            text=True,
            check=True,
        )
        assert proc.stdout.strip().splitlines()[-1] == "0", shell


def test_run_preflight_ps1_tolerates_stderr_restores_caller_state_and_fails_on_noisy_gate(
    tmp_path: Path,
):
    shells = _powershell_shells()
    if not shells:
        pytest.skip("pwsh/powershell is not installed")
    shell = shells[0]
    fixture = _write_preflight_fixture(tmp_path)
    base_env = {
        "PREFLIGHT_STDERR_NOISE": "1",
        "PREFLIGHT_PY_VERSION": "3.11",
        "ENGRAPHIS_INDEX_ROOTS": "caller-sentinel",
        "PYTEST_ADDOPTS": "--caller-addopt",
    }

    # Passing -Fast run with noisy Python stderr and caller env vars set.
    proc_ok, traces_ok, state_ok = _invoke_preflight(shell, fixture, "-Fast", base_env)
    assert state_ok["exit_code"] == 0, proc_ok.stdout + proc_ok.stderr
    assert Path(state_ok["cwd"]).resolve() == fixture["outside_dir"].resolve()
    assert state_ok["index"] == "caller-sentinel"
    assert state_ok["addopts"] == "--caller-addopt"

    pytest_calls = [t for t in traces_ok if t["tool"] == "python" and t["args"][:2] == ["-m", "pytest"]]
    assert len(pytest_calls) == 1
    assert pytest_calls[0]["PYTEST_ADDOPTS"] is None
    assert str(fixture["repo"].resolve()) in str(pytest_calls[0]["ENGRAPHIS_INDEX_ROOTS"])
    assert not any(t["tool"] == "pyright" for t in traces_ok)

    # Regression for Invoke-GateStep false-green: stdout + exit 3 must fail with exit 1
    # and still restore caller CWD / env variables.
    fail_env = dict(base_env, PREFLIGHT_PYTEST_EXIT="3")
    proc_fail, _, state_fail = _invoke_preflight(shell, fixture, "-Fast", fail_env)
    assert state_fail["exit_code"] == 1
    assert "[FAIL] Offline Unit Test Suite exited with code 3" in proc_fail.stdout
    assert Path(state_fail["cwd"]).resolve() == fixture["outside_dir"].resolve()
    assert state_fail["index"] == "caller-sentinel"
    assert state_fail["addopts"] == "--caller-addopt"


def test_run_preflight_ps1_enforces_python_version_extras_and_datasets(tmp_path: Path):
    shells = _powershell_shells()
    if not shells:
        pytest.skip("pwsh/powershell is not installed")
    shell = shells[0]
    fixture = _write_preflight_fixture(tmp_path, include_datasets=False)

    # Python 3.9 without -Fast must explain -Fast before failing on missing Pyright/extras.
    proc_py39, traces_py39, state_py39 = _invoke_preflight(
        shell, fixture, "-SkipEval", {"PREFLIGHT_PY_VERSION": "3.9"}
    )
    assert state_py39["exit_code"] == 1
    assert "found Python 3.9" in proc_py39.stdout
    assert "-Fast" in proc_py39.stdout
    assert not any(t["tool"] == "pyright" for t in traces_py39)

    # Python 3.11 missing a [test] extra passes Pyright (with --pythonpath) and fails Step 5 loudly.
    proc_missing_mod, traces_mod, state_mod = _invoke_preflight(
        shell,
        fixture,
        "-SkipEval -Workers 2",
        {"PREFLIGHT_PY_VERSION": "3.11", "PREFLIGHT_MISSING_MODULES": "fastapi,xdist"},
    )
    assert state_mod["exit_code"] == 1
    assert "fastapi" in proc_missing_mod.stdout
    assert 'pip install -e ".[test]"' in proc_missing_mod.stdout
    pyright_calls = [t for t in traces_mod if t["tool"] == "pyright"]
    assert len(pyright_calls) == 1
    assert "--pythonpath" in pyright_calls[0]["args"]

    # Python 3.11 with all [test] extras but missing xdist logs -Workers fallback and passes -SkipEval.
    proc_no_xdist, _, state_no_xdist = _invoke_preflight(
        shell,
        fixture,
        "-SkipEval -Workers 2",
        {"PREFLIGHT_PY_VERSION": "3.11", "PREFLIGHT_MISSING_MODULES": "xdist"},
    )
    assert state_no_xdist["exit_code"] == 0, proc_no_xdist.stdout + proc_no_xdist.stderr
    assert "[pytest] -Workers ignored because pytest-xdist is not installed" in proc_no_xdist.stdout

    # Missing required eval dataset in full mode must fail with exit 1 instead of skipping.
    proc_missing_ds, _, state_ds = _invoke_preflight(
        shell, fixture, "", {"PREFLIGHT_PY_VERSION": "3.11"}
    )
    assert state_ds["exit_code"] == 1
    assert "Missing required eval dataset 'eval/datasets/sample.jsonl'" in proc_missing_ds.stdout

