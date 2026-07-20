from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_distribution_configuration_excludes_runtime_bytecode():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")

    assert "include-package-data = false" in pyproject
    assert '"*" = ["*.pyc", "*.pyo", "__pycache__/*"]' in pyproject
    assert "global-exclude *.pyc" in manifest
    assert "global-exclude *.pyo" in manifest


def test_distribution_configuration_includes_external_dashboard_assets():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    package_data = pyproject[pyproject.index('[tool.setuptools.package-data]'):
                             pyproject.index('[tool.setuptools.exclude-package-data]')]
    for pattern in ('"*.html"', '"*.css"', '"*.js"'):
        assert pattern in package_data


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
    assert "Trademark Policy" not in notice
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "license does not grant trademark rights" in readme
    assert "license does not grant trademark rights" in notice


def test_manual_release_dispatch_cannot_publish():
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert workflow.count(
        "if: github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')"
    ) == 2
    assert "Require tag and package version to match" in workflow
    assert "python -m twine check dist/*" in workflow
    assert "python -m pip_audit --local" in workflow
    assert "github-release:" in workflow
    assert "needs: publish" in workflow
    assert "contents: write" in workflow
    assert 'gh release create "$GITHUB_REF_NAME" dist/*' in workflow
    assert "--verify-tag" in workflow


def test_source_tree_version_matches_pyproject():
    """The ``PackageNotFoundError`` fallback in ``engraphis/__init__.py`` must equal the
    ``[project] version``. It only shows up in an uninstalled source tree, so a stale
    value survives every test run on an installed checkout and then leaks into the API
    index and ``--version`` output of anyone running from a clone."""
    import re

    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    init = (ROOT / "engraphis" / "__init__.py").read_text(encoding="utf-8")
    declared = re.search(r'^version = "([^"]+)"', pyproject, re.M)
    fallback = re.search(r'^    __version__ = "([^"]+)"', init, re.M)
    assert declared and fallback, "version declarations moved — update this test"
    assert declared.group(1) == fallback.group(1)


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


def test_example_config_preserves_platform_database_default():
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    active = [
        line for line in example.splitlines()
        if line.startswith("ENGRAPHIS_DB_PATH=")
    ]
    assert active == []
    assert "platform user-data directory" in example


def test_customer_sync_relay_docs_keep_vendor_license_service_separate():
    hosting = (ROOT / "docs" / "HOSTING_RAILWAY.md").read_text(encoding="utf-8")
    template = (ROOT / "docs" / "RAILWAY_TEMPLATE.md").read_text(encoding="utf-8")
    for document in (hosting, template):
        assert "ENGRAPHIS_RELAY_URL" in document
        assert "ENGRAPHIS_CLOUD_URL" in document
        assert "https://license.engraphis.com" in document
        assert "ENGRAPHIS_RELAY_URL=https://license.engraphis.com" not in document
