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
    assert "[Docker deployment guide](docs/DOCKER.md)" in readme
    assert "The Docker image includes the streamable HTTP MCP endpoint" in docker_docs
    assert "ENGRAPHIS_API_TOKEN=<a-long-random-secret>" in docker_docs
    assert "docker-compose.lan.yml" in docker_docs
    assert "LAN overlay refuses to render" in docker_docs
    assert "ENGRAPHIS_DASHBOARD_URL" in docker_docs
    assert "ENGRAPHIS_COMPOSE_PORT" in docker_docs
    assert "start_dashboard.main(args)" in launcher
    assert "engraphis.app" not in launcher
    assert "same v2 service" in readme


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

    assert "[architecture guide](docs/ARCHITECTURE_V3.md#query-planning)" in readme
    assert guidance not in readme
    assert guidance in architecture
    assert "LLMQueryPlanner(my_llm)" in architecture


def test_pi_and_public_write_review_details_stay_in_supporting_docs():
    readme = _text("README.md")
    pi_guide = _text("integrations/pi/README.md")
    review_guide = _text("docs/WRITE_REVIEW.md")

    assert "[Pi extension guide](integrations/pi/README.md)" in readme
    assert "pi install npm:@engraphis/pi" not in readme
    assert "Every advanced state-changing action requires an explicit Pi confirmation dialog" in pi_guide

    review_gate = "Every public write enters review as `pending`"
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
    assert "[Docker deployment guide](docs/DOCKER.md)" in readme
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
    assert 'python -m pip_audit --path "$audit_dir"' in ci
    assert 'docker cp "$container":/usr/local/lib/python3.11/site-packages/.' in ci
    assert "tesseract-ocr" in _text("Dockerfile")
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
    assert 'build twine pip-audit ".[all,test]"' in release_build
    assert "python -m pip_audit --local" in release_build
    assert "docker build -t engraphis:release ." in release_docker
    assert "Validate Compose configuration" in release_docker
    assert "docker compose config --quiet" in release_docker
    assert "Audit production image dependencies" in release_docker
    assert 'python -m pip install --disable-pip-version-check --no-cache-dir pip-audit' in release_docker
    assert 'docker create --name "$container" engraphis:release' in release_docker
    assert 'docker cp "$container":/usr/local/lib/python3.11/site-packages/.' in release_docker
    assert 'python -m pip_audit --path "$audit_dir"' in release_docker
    assert "needs: [build, python-matrix, encryption, browser-accessibility, pi-extension, docker-smoke]" in release_evidence
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
    assert "needs: [build, python-matrix, encryption, browser-accessibility, pi-extension, docker-smoke]" in release


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
    assert release.count("python -m build") == 1
    assert "python scripts/verify_distribution_contents.py dist/*" in release
    assert "Build compiled wheels" not in release
    assert "name: Assemble distributions" not in release
    assert "needs: [build, python-matrix, encryption, browser-accessibility, pi-extension, docker-smoke]" in release
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


def test_ci_linter_is_bounded_to_the_verified_release_series():
    pyproject = _text("pyproject.toml")

    # A version bound alone never made the linter deterministic: ruff's *default* rule set
    # is not stable across minor releases -- 0.16 widened it from 59 rules to 413, which
    # would have turned `ruff check .` red on unchanged code. Pinning `select` explicitly
    # is what actually bounds CI, so the bound and the rule set are asserted together.
    assert pyproject.count('"ruff>=0.15.22,<0.17"') == 2
    assert 'select = ["E4", "E7", "E9", "F"]' in pyproject


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
    assert '.name == "Generate public release evidence"' in repair
    assert '.name == "Assemble distributions"' not in repair
    assert repair.count('.conclusion == "success"') >= 2
    assert 'gh run download "$run_id"' in repair
    assert 'open("release-evidence/release-evidence.json", encoding="utf-8")' in repair
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
    assert 'gh release upload "$RELEASE_TAG" dist/*' in repair_job
    assert "--clobber" in repair_job


def test_public_capability_and_support_docs_match_the_shipped_tree():
    server = _text("engraphis/mcp_server.py")
    tools = re.findall(r'@mcp\.tool\(\s*name="(engraphis_[^"]+)"', server)
    assert len(tools) == len(set(tools)) == 33

    readme = _text("README.md")
    architecture = _text("docs/ARCHITECTURE_V3.md")
    skill = _text("skills/engraphis-memory/SKILL.md")
    skill_tools = _text("skills/engraphis-memory/references/TOOLS.md")
    skill_scoping = _text("skills/engraphis-memory/references/SCOPING.md")
    for content in (readme, architecture, skill):
        assert "28 MCP tools" not in content
        assert "28-tool" not in content
        assert "(28 of them)" not in content
    assert "Smart MCP (9 tools)" in architecture
    assert "Classic MCP (33 tools)" in architecture
    assert "default Smart MCP surface has nine" in skill
    assert "Classic direct-tool guide" in skill
    assert "engraphis-mcp-classic" in skill
    assert "recall_context (compact)" in architecture
    assert "engraphis_recall_context" in readme
    assert "`engraphis_check_update`" in readme
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
            "engraphis/classic_assets/index.html",
            "engraphis/classic_assets/dashboard.js",
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
