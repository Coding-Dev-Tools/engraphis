import csv
import hashlib
import json
import re
import shlex
import subprocess
import sys
import tarfile
import types
import zipfile
from pathlib import Path

import pytest

from scripts.verify_distribution_contents import (
    REQUIRED_COMMON,
    REQUIRED_SDIST,
    verify_distribution,
)


ROOT = Path(__file__).resolve().parents[1]


def test_mcp_cli_module_entrypoint_renders_help():
    result = subprocess.run(
        [sys.executable, "-m", "engraphis.mcp_cli", "--help"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "usage: engraphis-mcp" in result.stdout
    assert "Run the Engraphis MCP server over stdio" in result.stdout


def test_http_mcp_cli_module_entrypoint_renders_help():
    result = subprocess.run(
        [sys.executable, "-m", "engraphis.mcp_http_cli", "--help"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "usage: engraphis-mcp-http" in result.stdout
    assert "loopback-only Engraphis MCP server over HTTP" in result.stdout


def test_http_mcp_cli_rejects_non_loopback_host():
    from engraphis import mcp_http_cli

    for host in ("0.0.0.0", "localhost"):
        with pytest.raises(SystemExit) as exc:
            mcp_http_cli.main(["--host", host])

        assert exc.value.code == 2


def test_http_mcp_cli_configures_the_packaged_transport(monkeypatch):
    from engraphis import mcp_http_cli

    calls = []
    security_calls = []
    transport_security = object()
    fake_mcp = types.SimpleNamespace(
        settings=types.SimpleNamespace(host=None, port=None),
        run=lambda *, transport: calls.append(transport),
    )
    monkeypatch.setattr(mcp_http_cli, "_dependency_error", lambda: "")
    monkeypatch.setattr(
        mcp_http_cli,
        "_transport_security",
        lambda host, port: security_calls.append((host, port)) or transport_security,
    )
    monkeypatch.setitem(sys.modules, "engraphis.mcp_server", types.SimpleNamespace(mcp=fake_mcp))

    mcp_http_cli.main(["--host", "::1", "--port", "9876", "--transport", "sse"])

    assert fake_mcp.settings.host == "::1"
    assert fake_mcp.settings.port == 9876
    assert fake_mcp.settings.transport_security is transport_security
    assert security_calls == [("::1", 9876)]
    assert calls == ["sse"]


def test_http_mcp_console_entrypoint_is_packaged_and_client_neutral():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    agent_connect = (ROOT / "docs" / "AGENT_CONNECT.md").read_text(encoding="utf-8")
    launcher = (ROOT / "scripts" / "mcp_server_http.py").read_text(encoding="utf-8")

    assert 'engraphis-mcp-http = "engraphis.mcp_http_cli:main"' in pyproject
    assert "engraphis-mcp-http" in agent_connect
    assert "Hermes" not in agent_connect + launcher


def test_git_plugin_release_version_and_asset_hashes_are_exact():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    declared = re.search(r'^version = "([^"]+)"', pyproject, re.M)
    assert declared, "project version declaration moved — update this test"

    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(
        encoding="utf-8"
    ))
    marketplace = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(
        encoding="utf-8"
    ))
    entries = [entry for entry in marketplace["plugins"]
               if entry["name"] == plugin["name"]]
    assert len(entries) == 1
    assert plugin["name"] == "engraphis-memory"
    assert entries[0]["source"] == "./"
    assert plugin["version"] == entries[0]["version"] == declared.group(1)

    skill_root = ROOT / "skills" / "engraphis-memory"
    portable_files = sorted(
        list((ROOT / ".claude-plugin").glob("*.json"))
        + list(skill_root.rglob("*.md"))
    )
    expected = {path.relative_to(ROOT).as_posix() for path in portable_files}
    assert "\nname: engraphis-memory\n" in (
        skill_root / "SKILL.md"
    ).read_text(encoding="utf-8")

    checksums = {}
    manifest = ROOT / ".claude-plugin" / "skill-assets.sha256"
    for line_number, line in enumerate(manifest.read_text(encoding="utf-8").splitlines(), 1):
        digest, separator, relative = line.partition("  ")
        assert separator and re.fullmatch(r"[0-9a-f]{64}", digest), (
            f"invalid checksum line {line_number}"
        )
        assert relative not in checksums, f"duplicate checksum for {relative}"
        checksums[relative] = digest

    assert set(checksums) == expected
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    for rule in (
        ".claude-plugin/*.json text eol=lf",
        ".claude-plugin/skill-assets.sha256 text eol=lf",
        "skills/engraphis-memory/*.md text eol=lf",
        "skills/engraphis-memory/references/*.md text eol=lf",
    ):
        assert rule in attributes
    for relative, digest in checksums.items():
        actual = hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
        assert actual == digest, f"stale plugin asset checksum: {relative}"


def test_distribution_has_no_compiled_local_license_gate():
    setup = (ROOT / "setup.py").read_text(encoding="utf-8")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert "Cython" not in setup + pyproject
    assert "cython" not in setup + pyproject
    assert "Extension(" not in setup
    assert not (ROOT / "engraphis" / "cloud_license.py").exists()


def test_distribution_configuration_excludes_runtime_bytecode():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")

    assert "include-package-data = false" in pyproject
    assert '"*" = ["*.pyc", "*.pyo", "__pycache__/*"]' in pyproject
    assert "global-exclude *.pyc" in manifest
    assert "global-exclude *.pyo" in manifest


def test_native_vector_extra_uses_delete_safe_sqlitevec_floor():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert 'vector = [\n    "sqlite-vec>=0.1.9,<0.2",\n]' in pyproject
    test_extra = pyproject[pyproject.index("test = ["):]
    assert '"sqlite-vec>=0.1.9,<0.2"' in test_extra
    all_extra = pyproject[pyproject.index("all = ["):pyproject.index("dev = [")]
    assert "sqlite-vec" not in all_extra


def test_release_test_tooling_excludes_vulnerable_pytest_and_uses_private_temp_roots():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert pyproject.count(
        '"pytest>=9.0.3; python_version >= \'3.10\'"'
    ) == 2
    for relative in (".github/workflows/ci.yml", ".github/workflows/release.yml"):
        workflow = (ROOT / relative).read_text(encoding="utf-8")
        pytest_lines = [
            line for line in workflow.splitlines() if "python -m pytest" in line
        ]
        assert pytest_lines
        assert all('--basetemp="${RUNNER_TEMP}/engraphis-pytest"' in line
                   for line in pytest_lines)


def test_migration_backups_are_ignored_for_database_paths_without_extensions():
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "*.pre-migration-v*.bak" in ignore


def test_distribution_configuration_includes_external_dashboard_assets():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    package_data = pyproject[pyproject.index('[tool.setuptools.package-data]'):
                             pyproject.index('[tool.setuptools.exclude-package-data]')]
    for pattern in ('"*.html"', '"*.css"', '"*.js"'):
        assert pattern in package_data


def test_distribution_configuration_includes_public_evidence_tools():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    package_data = pyproject[pyproject.index('[tool.setuptools.package-data]'):
                             pyproject.index('[tool.setuptools.exclude-package-data]')]
    assert 'include = ["engraphis*", "scripts*", "eval*"]' in pyproject
    assert '"datasets/locomo10_repair_manifest.json"' in package_data
    for rule in (
        "include LICENSE NOTICE README.md CHANGELOG.md BENCHMARKS.md",
        "include docs/RECALL_RECOVERY.md",
        "include docs/DOCUMENT_IMPORT.md docs/OBSIDIAN_IMPORT.md",
        "include docs/images/context-efficiency.svg",
        "include docker-entrypoint.sh Dockerfile docker-compose.yml docker-compose.lan.yml",
        "recursive-include eval *.py",
        "include deploy/force-graph-1.51.4.licenses.json",
        "include deploy/force-graph-1.51.4.yarn.lock",
        "include eval/BASELINES.md",
        "include eval/EVIDENCE.md",
        "recursive-include eval/configs *.json",
        "recursive-include eval/datasets *.jsonl",
        "include eval/datasets/locomo10_repair_manifest.json",
    ):
        assert rule in manifest
    assert "docker-compose.lan.yml" in REQUIRED_SDIST
    assert "docs/DOCUMENT_IMPORT.md" in REQUIRED_SDIST
    assert "docs/OBSIDIAN_IMPORT.md" in REQUIRED_SDIST
    assert "deploy/force-graph-1.51.4.licenses.json" in REQUIRED_SDIST
    assert "deploy/force-graph-1.51.4.yarn.lock" in REQUIRED_SDIST
    assert '"deploy/force-graph-1.51.4.licenses.json"' in pyproject
    assert '"deploy/force-graph-1.51.4.yarn.lock"' in pyproject


def test_distribution_archive_verifier_requires_evidence_and_rejects_internal_material(
        tmp_path):
    wheel = tmp_path / "engraphis-1.0.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name in sorted(REQUIRED_COMMON):
            archive.writestr(name, b"public")
    verify_distribution(wheel)

    unsafe_wheel = tmp_path / "engraphis-1.0.1-py3-none-any.whl"
    with zipfile.ZipFile(unsafe_wheel, "w") as archive:
        for name in sorted(REQUIRED_COMMON):
            archive.writestr(name, b"public")
        archive.writestr("notes/internal-material.md", b"private")
        archive.writestr("notes/internal-material/findings.md", b"private")
    with pytest.raises(ValueError, match="private or generated"):
        verify_distribution(unsafe_wheel)

    sdist = tmp_path / "engraphis-1.0.0.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        for name in sorted(REQUIRED_SDIST):
            path = tmp_path / name.replace("/", "_")
            path.write_bytes(b"public")
            archive.add(path, arcname=f"engraphis-1.0.0/{name}")
    verify_distribution(sdist)


def _yarn_v1_runtime_closure(
        lock_text: str, direct_dependencies: dict[str, str]) -> set[tuple[str, str]]:
    selectors = {}
    current = None
    in_dependencies = False
    for raw in lock_text.splitlines():
        if raw and not raw.startswith((" ", "#")) and raw.endswith(":"):
            names = next(csv.reader([raw[:-1]], skipinitialspace=True))
            current = {"dependencies": {}}
            for selector in names:
                selectors[selector] = current
            in_dependencies = False
        elif current is not None and raw.startswith("  version "):
            current["version"] = shlex.split(raw.strip())[1]
            in_dependencies = False
        elif current is not None and raw == "  dependencies:":
            in_dependencies = True
        elif current is not None and in_dependencies and raw.startswith("    "):
            parts = shlex.split(raw.strip())
            if len(parts) == 2:
                current["dependencies"][parts[0]] = parts[1]
        elif raw and not raw.startswith("    "):
            in_dependencies = False

    closure = set()
    pending = list(direct_dependencies.items())
    while pending:
        name, version_range = pending.pop()
        package = selectors[f"{name}@{version_range}"]
        identity = (name, package["version"])
        if identity in closure:
            continue
        closure.add(identity)
        pending.extend(package["dependencies"].items())
    return closure


def test_force_graph_bundle_has_exact_locked_license_closure():
    report_path = ROOT / "deploy" / "force-graph-1.51.4.licenses.json"
    lock_path = ROOT / "deploy" / "force-graph-1.51.4.yarn.lock"
    bundle_path = ROOT / "engraphis" / "static" / "vendor" / "force-graph.min.js"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    root_lock = json.loads((ROOT / "package-lock.json").read_text(encoding="utf-8"))

    assert report["format"] == "engraphis-bundled-license-report/v1"
    assert report["bundle"]["sha256"] == hashlib.sha256(bundle_path.read_bytes()).hexdigest()
    assert report["source_lock"]["sha256"] == hashlib.sha256(lock_path.read_bytes()).hexdigest()
    assert report["source_lock"]["canonical_lf_sha256"] == hashlib.sha256(
        lock_path.read_text(encoding="utf-8").replace("\r\n", "\n").encode("utf-8")
    ).hexdigest()
    assert report["bundle"]["upstream_commit"] == (
        "baa20a92bbe5628034d771abaf33a2dbb65d22eb"
    )

    closure = _yarn_v1_runtime_closure(
        lock_path.read_text(encoding="utf-8"),
        report["bundle"]["direct_dependencies"],
    )
    closure.add(("force-graph", "1.51.4"))
    package_entries = root_lock["packages"]
    expected = {
        (
            name,
            version,
            package_entries[f"node_modules/{name}"]["license"],
        )
        for name, version in closure
    }
    actual = {
        (item["name"], item["version"], item["license"])
        for item in report["dependencies"]
    }
    assert actual == expected
    assert all(item["copyright"] for item in report["dependencies"])
    assert all(len(item["license_text"]) > 500 for item in report["dependencies"])
    force_graph = package_entries["node_modules/force-graph"]
    assert force_graph["version"] == "1.51.4"
    assert force_graph["integrity"] == report["bundle"]["npm_integrity"]


def test_every_vendored_browser_library_has_redistribution_notice():
    vendor = ROOT / "engraphis" / "static" / "vendor"
    required = {
        "d3.min.js": "d3.LICENSE",
        "marked.min.js": "marked.LICENSE",
        "force-graph.min.js": "force-graph.LICENSE",
        "purify.min.js": None,  # Apache-2.0 header points at the packaged root LICENSE
    }
    for script, license_name in required.items():
        assert (vendor / script).is_file()
        if license_name:
            text = (vendor / license_name).read_text(encoding="utf-8")
            assert "Copyright" in text and len(text) > 500
    notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
    assert all(name in notice for name in (
        "D3 7.9.0", "Marked 12.0.2", "force-graph 1.51.4", "DOMPurify 3.4.11",
    ))
    assert "galaxy-dependencies.json" not in notice
    assert "galaxy-vendor.LICENSE.txt" not in notice
    assert "Trademark Policy" not in notice
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "license does not grant trademark rights" in readme
    assert "license does not grant trademark rights" in notice


def test_manual_release_dispatch_cannot_publish():
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert workflow.count(
        "if: github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')"
    ) == 3
    for job in ("release-evidence", "publish", "github-release"):
        match = re.search(rf"(?ms)^  {re.escape(job)}:\n(.*?)(?=^  \S[^:\n]*:\n|\Z)", workflow)
        assert match is not None
        body = match.group(1)
        assert "if: github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')" in body
    assert "Require tag and package version to match" in workflow
    assert "python -m twine check dist/*" in workflow
    assert "python -m pip_audit --local --skip-editable" in workflow
    assert "github-release:" in workflow
    assert "needs: publish" in workflow
    assert "contents: write" in workflow
    assert 'gh release create "$GITHUB_REF_NAME" dist/*' in workflow
    assert "--verify-tag" in workflow


def test_source_tree_version_matches_pyproject():
    """Both source-tree versions must equal the ``[project] version``."""
    import re

    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    init = (ROOT / "engraphis" / "__init__.py").read_text(encoding="utf-8")
    declared = re.search(r'^version = "([^"]+)"', pyproject, re.M)
    source = re.search(r'^_SOURCE_VERSION = "([^"]+)"', init, re.M)
    fallback = re.search(r'^    __version__ = "([^"]+)"', init, re.M)
    assert declared and source and fallback, "version declarations moved — update this test"
    assert declared.group(1) == source.group(1) == fallback.group(1)


def test_release_version_surfaces_are_synchronized():
    """Repo-distributed integrations and release filters track the package version."""
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    declared = re.search(r'^version = "([^"]+)"', pyproject, re.M)
    assert declared, "project version declaration moved — update this test"
    version = declared.group(1)

    commercial = json.loads(
        (ROOT / "engraphis" / "commercial_manifest.json").read_text(encoding="utf-8")
    )
    assert commercial["version"] == version

    hermes = (ROOT / "integrations" / "hermes" / "engraphis" / "plugin.yaml").read_text(
        encoding="utf-8"
    )
    hermes_version = re.search(r"^version:\s*(\S+)\s*$", hermes, re.M)
    assert hermes_version, "Hermes version declaration moved — update this test"
    expected_hermes = version if version.count(".") >= 2 else f"{version}.0"
    assert hermes_version.group(1) == expected_hermes

    ledger = (ROOT / "engraphis" / "dashboard_assets" / "ledger.js").read_text(
        encoding="utf-8"
    )
    assert re.findall(r"release_version=([0-9]+(?:\.[0-9]+)*)", ledger) == [version]

    static = (ROOT / "engraphis" / "static" / "dashboard.js").read_bytes()
    classic = (ROOT / "engraphis" / "classic_assets" / "dashboard.js").read_bytes()
    assert static == classic
    static_text = static.decode("utf-8")
    assert re.findall(r"p\.set\('release_version','([^']+)'\)", static_text) == [version]


def test_release_version_has_a_dated_changelog_section():
    """A tagged package must not ship its release notes only as ``Unreleased``."""
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    declared = re.search(r'^version = "([^"]+)"', pyproject, re.M)
    assert declared, "project version declaration moved — update this test"

    heading = re.compile(
        rf"^## \[{re.escape(declared.group(1))}\] - \d{{4}}-\d{{2}}-\d{{2}}$",
        re.M,
    )
    assert len(heading.findall(changelog)) == 1


def test_extras_stay_resolvable_on_the_lowest_supported_python():
    """A 3.10-only floor must carry a 3.10 marker, or its extra cannot install on 3.9.

    ``requires-python`` is ``>=3.9``, so an UNMARKED ``fastapi>=0.133.1`` makes
    ``pip install engraphis[server]`` fail to resolve on 3.9 with "no matching
    distribution" — and that is the exact command the launchers print when the extra is
    missing, so the user is sent in a circle. With the marker the install succeeds and
    ``scripts/start_server.py`` states the 3.10 requirement in prose instead.
    """
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'requires-python = ">=3.9"' in pyproject
    marker = "; python_version >= '3.10'"
    for spec in ("fastapi>=0.133.1,<1", "starlette>=1.3.1,<2", "python-multipart>=0.0.31"):
        lines = [line.strip() for line in pyproject.splitlines()
                 if spec in line and not line.lstrip().startswith("#")]
        assert lines, "%s no longer appears in pyproject.toml — update this test" % spec
        for line in lines:
            assert marker in line, "%s needs %r: %s" % (spec, marker, line)


def test_dependency_floors_exclude_known_vulnerable_and_breaking_releases():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    expected = '"mcp>=1.28.1,<2; python_version >= \'3.10\'"'
    assert pyproject.count(expected) == 3
    combined = pyproject + requirements
    assert "mcp>=1.28.1,<2" in requirements
    assert "mcp>=1.14.0" not in combined
    assert "python-multipart>=0.0.31" in combined
    assert "starlette>=1.3.1,<2" in combined
    assert "Pillow>=12.3.0" in pyproject
    assert pyproject.count("cryptography>=50.0.0") == 4
    assert "cryptography>=50.0.0" in requirements
    assert "cryptography>=48.0.1" not in combined


def test_example_config_preserves_platform_database_default():
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    active = [
        line for line in example.splitlines()
        if line.startswith("ENGRAPHIS_DB_PATH=")
    ]
    assert active == []
    assert "platform user-data directory" in example


def test_customer_hosting_docs_do_not_claim_private_cloud_authority():
    hosting = (ROOT / "docs" / "HOSTING_RAILWAY.md").read_text(encoding="utf-8")
    template = (ROOT / "docs" / "RAILWAY_TEMPLATE.md").read_text(encoding="utf-8")
    combined = hosting + template
    normalized = " ".join(combined.replace("**", "").split())

    assert "free single-user" in combined
    assert "does not" in normalized
    assert "license issuer" in combined
    assert "ENGRAPHIS_CLOUD_CONTROL_URL" in hosting
    assert "ENGRAPHIS_CLOUD_COMPUTE_URL" in hosting
    # Managed compute follows the cloud account now. The hosting docs may still document
    # the operator override, but they must never present hand-setting it as the way a
    # customer turns managed compute on.
    assert "ENGRAPHIS_MANAGED_COMPUTE_CONSENT=1" not in combined
