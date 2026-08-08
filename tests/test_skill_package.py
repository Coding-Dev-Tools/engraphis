from __future__ import annotations

import hashlib
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / "skills" / "engraphis-memory" / "references" / "TOOLS.md"


def _section(text: str, tool_name: str) -> str:
    heading = re.search(rf"^### `{re.escape(tool_name)}`[^\n]*$", text, flags=re.MULTILINE)
    assert heading is not None, f"portable reference omits {tool_name}"
    next_heading = re.search(r"^### `engraphis_[^`]+`", text[heading.end() :], flags=re.MULTILINE)
    end = heading.end() + next_heading.start() if next_heading else len(text)
    return text[heading.end() : end]


def test_portable_tool_reference_matches_registered_runtime_schemas() -> None:
    from engraphis.mcp_server import classic_mcp, smart_mcp

    reference = REFERENCE.read_text(encoding="utf-8")
    classic = classic_mcp._tool_manager._tools
    smart = smart_mcp._tool_manager._tools
    distinct = set(classic) | set(smart)
    overlap = set(classic) & set(smart)
    headings = set(re.findall(r"^### `(engraphis_[^`]+)`", reference, flags=re.MULTILINE))

    assert len(classic) == 34
    assert len(smart) == 9
    assert overlap == {"engraphis_remember", "engraphis_recall_context"}
    assert len(distinct) == 41
    assert headings == distinct
    assert "34 direct tools" in reference
    assert "nine" in reference
    assert "41 distinct public tool names" in reference

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    architecture = (ROOT / "docs" / "ARCHITECTURE_V3.md").read_text(encoding="utf-8")
    kilo = (ROOT / "docs" / "KILO_CODE_INTEGRATION.md").read_text(encoding="utf-8")
    assert "former 33 direct tool names" in readme
    assert "Classic 34-tool compatibility" in readme
    assert "34-tool Classic compatibility server" in readme
    assert "Smart MCP (9 tools) / Classic MCP (34 tools)" in architecture
    assert "Classic 34-tool inventory" in kilo

    for name, tool in classic.items():
        section = _section(reference, name)
        properties = (tool.parameters or {}).get("properties", {})
        for parameter in properties:
            assert re.search(rf"`{re.escape(parameter)}(?:\s|`|\()", section), (
                f"portable Classic reference omits {name}.{parameter}"
            )

    smart_contract = reference[
        reference.index("The two overlapping names") : reference.index("### `engraphis_session`")
    ]
    for name in sorted(overlap):
        properties = (smart[name].parameters or {}).get("properties", {})
        for parameter in properties:
            assert f"`{parameter}`" in smart_contract, (
                f"portable Smart overlap reference omits {name}.{parameter}"
            )

    critical_contracts = (
        '`source (str, "agent")`',
        '`trusted (bool, true)`',
        '`kind (str, None)`',
        '`planning (str, "off")`',
        '`mtype_limits (dict[str,int], None)`',
        '`max_response_tokens (int, None)`',
        '`token_budget (int, None)`',
        '`response_mode (str, "full")`',
        '`from_ts (float, None)`',
        '`to_ts (float, None)`',
        '`release_version (str, None)`',
        '`format (str, None)`',
        '`group_by (str, None)`',
        '`expected_head (str, None)`',
        '`expected_count (int, None)`',
        "total_rows",
        "prompt_eligibility",
        "embedding",
    )
    for contract in critical_contracts:
        assert contract in reference


def test_skill_asset_manifest_matches_exact_file_bytes() -> None:
    manifest_path = ROOT / ".claude-plugin" / "skill-assets.sha256"
    entries: dict[str, str] = {}
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, relative = line.split(maxsplit=1)
        entries[relative] = digest

    expected = {
        ".claude-plugin/marketplace.json",
        ".claude-plugin/plugin.json",
        "skills/engraphis-memory/SKILL.md",
        "skills/engraphis-memory/references/CONVENTIONS.md",
        "skills/engraphis-memory/references/SCOPING.md",
        "skills/engraphis-memory/references/TOOLS.md",
    }
    assert set(entries) == expected
    for relative, expected_digest in entries.items():
        payload = (ROOT / relative).read_bytes()
        assert hashlib.sha256(payload).hexdigest() == expected_digest, relative


def test_portable_skill_relative_links_resolve_within_package() -> None:
    skill_root = ROOT / "skills" / "engraphis-memory"
    markdown_files = [skill_root / "SKILL.md", *sorted((skill_root / "references").glob("*.md"))]
    pattern = re.compile(r"(?<!!)\[[^\]]+\]\(([^)#\s]+)(?:#[^)]+)?\)")

    for path in markdown_files:
        for target in pattern.findall(path.read_text(encoding="utf-8")):
            if re.match(r"^(?:https?://|mailto:)", target):
                continue
            resolved = (path.parent / target).resolve()
            assert resolved.is_relative_to(skill_root.resolve()), (path, target)
            assert resolved.exists(), (path, target)
