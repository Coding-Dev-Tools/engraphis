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
