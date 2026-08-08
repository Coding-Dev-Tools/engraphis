from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import unquote, urlparse


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "Coding-Dev-Tools/engraphis"


def _project_version() -> str:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, flags=re.MULTILINE)
    assert match is not None
    return match.group(1)


def test_plugin_manifests_match_package_identity_and_version() -> None:
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    marketplace = json.loads(
        (ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
    )
    assert plugin["name"] == "engraphis-memory"
    assert plugin["version"] == _project_version()
    assert plugin["repository"] == f"https://github.com/{REPOSITORY}"
    assert len(marketplace["plugins"]) == 1
    listing = marketplace["plugins"][0]
    assert listing["name"] == plugin["name"]
    assert listing["version"] == plugin["version"]
    assert listing["source"] == "./"


def _readme_targets(readme: str) -> set[str]:
    markdown = re.findall(r"(?<!!)\[[^\]]+\]\(([^)\s]+)", readme)
    html = re.findall(r'\b(?:href|src)="([^"]+)"', readme)
    return set(markdown + html)


def _repository_path(url: str) -> Path | None:
    parsed = urlparse(url)
    if parsed.netloc == "github.com":
        prefix = f"/{REPOSITORY}/blob/main/"
        if parsed.path.startswith(prefix):
            return ROOT / unquote(parsed.path[len(prefix) :])
    if parsed.netloc == "raw.githubusercontent.com":
        prefix = f"/{REPOSITORY}/main/"
        if parsed.path.startswith(prefix):
            return ROOT / unquote(parsed.path[len(prefix) :])
    return None


def test_pypi_readme_has_only_absolute_repository_assets_and_links() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r'^readme\s*=\s*"README\.md"$', pyproject, flags=re.MULTILINE)
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    targets = _readme_targets(readme)
    relative = sorted(
        target
        for target in targets
        if not re.match(r"^(?:https?://|mailto:|#)", target)
    )
    assert relative == [], f"PyPI cannot resolve README-relative targets: {relative}"

    repository_targets = {
        target: local
        for target in targets
        if (local := _repository_path(target)) is not None
    }
    assert repository_targets, "README has no canonical repository assets or guide links"
    for target, local in repository_targets.items():
        assert local.exists(), f"{target} maps to missing repository path {local.relative_to(ROOT)}"

    assert (
        f"https://raw.githubusercontent.com/{REPOSITORY}/main/"
        "docs/images/knowledge-graph.png"
    ) in targets
    assert (
        f"https://raw.githubusercontent.com/{REPOSITORY}/main/"
        "docs/images/context-efficiency.svg"
    ) in targets


def test_retired_automation_screenshot_is_not_distributed_or_referenced() -> None:
    retired = ROOT / "docs" / "images" / "automation.png"
    assert not retired.exists()
    for root in (ROOT / "README.md", ROOT / "docs", ROOT / "skills"):
        paths = [root] if root.is_file() else root.rglob("*.md")
        for path in paths:
            assert "automation.png" not in path.read_text(encoding="utf-8"), path
