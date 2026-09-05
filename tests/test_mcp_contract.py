"""Published schemas and shipped integrations must follow the registered server."""
import json

import pytest

pytest.importorskip("mcp")
from scripts.export_mcp_contract import ROOT, artifacts, build_contract


def test_generated_artifacts_match_registered_tools():
    contract = build_contract()
    for path, expected in artifacts(contract).items():
        assert path.read_text(encoding="utf-8") == expected, str(path)
    assert contract["schema"] == "engraphis-mcp-contract/v1"
    assert len(contract["sha256"]) == 64
    schemas = {item["name"]: item["inputSchema"] for item in contract["surfaces"]["smart"]}
    assert schemas["engraphis_recall_context"]["properties"]["k"]["default"] == 50
    assert schemas["engraphis_recall_context"]["properties"]["format"]["default"] == "full"
    assert {"subject_key", "claim_kind"} <= schemas["engraphis_remember"]["properties"].keys()
    classic = {item["name"] for item in contract["surfaces"]["classic"]}
    assert "engraphis_recall" in classic
    assert "engraphis_discover_actions" not in classic


def test_contract_is_secret_free_public_metadata():
    text = (ROOT / "docs/MCP_CONTRACT.json").read_text(encoding="utf-8")
    contract = json.loads(text)
    assert all(set(tool) == {"name", "description", "inputSchema", "annotations"}
               for surface in contract["surfaces"].values() for tool in surface)

@pytest.mark.parametrize(("surface", "tool_name"), [
    ("classic", "engraphis_start_session"),
    ("smart", "engraphis_update_memory"),
])
def test_contract_normalizes_description_margin_without_losing_nested_text(
    monkeypatch, surface, tool_name,
):
    from engraphis.mcp_server import classic_mcp, smart_mcp

    server = {"classic": classic_mcp, "smart": smart_mcp}[surface]
    tool = server._tool_manager._tools[tool_name]
    description = (
        "Return the approved facts.\n\n"
        "Returns:\n"
        "    A result with citations.\n\n"
        "Example:\n"
        "    if approved:\n"
        "        recall()"
    )
    # Python 3.12 preserves this margin in __doc__; 3.13+ removes it.
    indented = "\n".join(
        line if n == 0 else "    " + line
        for n, line in enumerate(description.split("\n"))
    ) + "\n    "
    monkeypatch.setattr(tool, "description", indented)
    old_runtime = build_contract()
    monkeypatch.setattr(tool, "description", description)
    new_runtime = build_contract()

    assert old_runtime == new_runtime
    exported = next(item for item in old_runtime["surfaces"][surface]
                    if item["name"] == tool_name)
    assert exported["description"] == description


@pytest.mark.parametrize("field", ["description", "parameters", "annotations"])
def test_contract_check_still_rejects_meaningful_registered_tool_drift(
    monkeypatch, capsys, field,
):
    from copy import deepcopy
    from engraphis.mcp_server import smart_mcp
    from scripts import export_mcp_contract

    tool = smart_mcp._tool_manager._tools["engraphis_recall_context"]
    if field == "description":
        changed = tool.description + "\nExplicit approval is required."
    elif field == "parameters":
        changed = deepcopy(tool.parameters)
        changed["properties"]["k"]["default"] += 1
    else:
        changed = tool.annotations.model_copy(
            update={"readOnlyHint": not tool.annotations.readOnlyHint}
        )
    monkeypatch.setattr(tool, field, changed)
    monkeypatch.setattr(export_mcp_contract.sys, "argv", ["export_mcp_contract.py", "--check"])

    assert export_mcp_contract.main() == 1
    output = capsys.readouterr().out
    assert "MCP contract drift:" in output
    assert "MCP_CONTRACT.json" in output
