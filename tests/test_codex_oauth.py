"""OAuth route isolation and native protocol failure boundaries (no live calls)."""
from __future__ import annotations

import json

import pytest

from eval.campaign_api import CampaignAPIError, LunaResponsesClient
from eval.campaign_ledger import BudgetApproval, CampaignBinding, CampaignLedger
from eval.codex_oauth import (
    MODEL, PROVIDER, CodexOAuthTransport, _check_account_config, collect_turn,
    instruction_fingerprint, oauth_environment, oauth_settings,
)


def test_child_environment_rejects_gateway_and_keys_without_mutating_parent():
    source = {"OPENAI_API_KEY": "private", "OPENAI_BASE_URL": "https://invalid",
              "CODEX_API_KEY": "private", "CODEX_PROXY_URL": "https://invalid",
              "https_proxy": "https://invalid", "PATH": "bin", "USERPROFILE": "owner"}
    assert oauth_environment(source) == {"PATH": "bin", "USERPROFILE": "owner"}
    assert source["OPENAI_API_KEY"] == "private"


class ConfigRPC:
    def __init__(self):
        self.account = {"type": "chatgpt", "email": "private@example.invalid"}
        self.config = {
            "forced_login_method": "chatgpt", "model_provider": PROVIDER,
            "model_providers": {PROVIDER: {"requires_openai_auth": True,
                "request_max_retries": 0, "stream_max_retries": 0, "supports_websockets": False}},
            "mcp_servers": {"memory": {"enabled": False}},
        }
        for key, value in oauth_settings().items():
            target = self.config
            parts = key.split(".")
            for part in parts[:-1]:
                target = target.setdefault(part, {})
            target[parts[-1]] = value

    def request(self, method, params):
        return {"account": self.account} if method == "account/read" else {"config": self.config}


@pytest.mark.parametrize("field,value", [("base_url", "https://invalid"), ("env_key", "KEY"),
    ("experimental_bearer_token", "private"), ("request_max_retries", 1),
    ("stream_max_retries", 1), ("http_headers", {"Authorization": "private"})])
def test_gateway_or_retry_configuration_fails_closed(field, value):
    rpc = ConfigRPC()
    rpc.config["model_providers"][PROVIDER][field] = value
    with pytest.raises(CampaignAPIError, match="configuration"):
        _check_account_config(rpc, require_disabled=True)


def test_account_and_mcp_must_be_isolated():
    rpc = ConfigRPC()
    assert _check_account_config(rpc, require_disabled=True) == ("memory",)
    rpc.account["type"] = "apiKey"
    with pytest.raises(CampaignAPIError, match="API keys are rejected"):
        _check_account_config(rpc, require_disabled=True)
    rpc.account["type"] = "chatgpt"
    rpc.config["mcp_servers"]["memory"]["enabled"] = True
    with pytest.raises(CampaignAPIError, match="enabled MCP"):
        _check_account_config(rpc, require_disabled=True)


def test_settings_disable_native_tools_and_reject_unsafe_server_names():
    config = oauth_settings(("memory",))
    assert config["mcp_servers.memory.enabled"] is False
    assert config["features.multi_agent"] is False
    assert config["features.shell_tool"] is False
    assert config["tools.update_plan.enabled"] is False
    with pytest.raises(CampaignAPIError, match="server name"):
        oauth_settings(("quoted.server",))


class EventRPC:
    def __init__(self, events):
        self.events = iter(events)

    def next_event(self):
        return next(self.events)


def event(method, **params):
    return {"method": method, "params": {"threadId": "thread", "turnId": "turn", **params}}


def completed_events():
    return [event("item/completed", item={"type": "agentMessage", "id": "a", "text": '{"ok":true}'}),
            event("thread/tokenUsage/updated", tokenUsage={"total": {
                "inputTokens": 2345, "cachedInputTokens": 1000, "outputTokens": 24,
                "reasoningOutputTokens": 10, "totalTokens": 2369}}),
            event("turn/completed", turn={"id": "turn", "status": "completed"})]


def test_native_model_usage_is_retained_without_account_or_reasoning_text(tmp_path):
    log = tmp_path / "events.jsonl"
    events = [event("item/completed", item={"type": "reasoning", "text": "private reasoning"})]
    output, usage = collect_turn(EventRPC(events + completed_events()), "thread", "turn", log)
    assert json.loads(output) == {"ok": True}
    assert usage["input_tokens"] == 2345
    assert usage["reasoning_tokens"] == 10
    assert "private reasoning" not in log.read_text()


@pytest.mark.parametrize("bad_event", [
    event("model/rerouted", fromModel=MODEL, toModel="other"),
    event("item/started", item={"type": "commandExecution"}),
    event("item/started", item={"type": "mcpToolCall"}),
    event("item/started", item={"type": "contextCompaction"}),
    event("error", willRetry=True),
    event("turn/completed", turn={"id": "turn", "status": "failed"}),
    event("turn/completed", turn={"id": "turn", "status": "completed"}),
    {"method": "item/completed", "params": {"item": {"type": "mcpToolCall"}}},
    {"method": "turn/retry", "params": {}},
    {"method": "turn/error", "params": {}},
    {"method": "model/changed", "params": {}},
    {"method": "item/started", "params": {"threadId": "foreign", "turnId": "turn",
                                          "item": {"type": "agentMessage"}}},
])
def test_reroute_retry_tools_and_unmetered_output_are_rejected(tmp_path, bad_event):
    with pytest.raises(CampaignAPIError):
        collect_turn(EventRPC([bad_event] + completed_events()), "thread", "turn", tmp_path / "events")


def test_instruction_bytes_are_frozen_and_paths_stay_private(tmp_path):
    source = tmp_path / "AGENTS.md"
    source.write_text("Common native instructions", encoding="utf-8")
    frozen = instruction_fingerprint([str(source)])
    source.write_text("Changed", encoding="utf-8")
    assert frozen != instruction_fingerprint([str(source)])
    assert len(frozen) == 64
    with pytest.raises(CampaignAPIError):
        instruction_fingerprint(["relative/AGENTS.md"])


def test_invalid_request_cannot_launch_native_transport(tmp_path):
    def forbidden(*args):
        pytest.fail("invalid request reached native process")
    transport = CodexOAuthTransport(executable="codex", expected_version="pinned",
        expected_instruction_sha256="f" * 64, work_root=tmp_path, rpc_factory=forbidden)
    with pytest.raises(CampaignAPIError, match="reader contract"):
        transport.create(model="other", reasoning={"effort": "medium"}, tools=[], store=False)


def test_native_transport_integrates_with_durable_client_and_resume(tmp_path, monkeypatch):
    requests = []
    class ReaderRPC(ConfigRPC, EventRPC):
        def __init__(self, executable, directory, settings, timeout):
            ConfigRPC.__init__(self)
            EventRPC.__init__(self, completed_events())

        def request(self, method, params):
            requests.append((method, params))
            if method == "thread/start":
                assert params["environments"] == []
                assert params["dynamicTools"] == []
                assert params["allowProviderModelFallback"] is False
                return {"model": MODEL, "modelProvider": PROVIDER, "reasoningEffort": "medium",
                        "instructionSources": [], "thread": {"id": "thread"}}
            if method == "turn/start":
                return {"turn": {"id": "turn"}}
            return ConfigRPC.request(self, method, params)

        def close(self):
            pass

    transport = CodexOAuthTransport(executable="codex", expected_version="pinned",
        expected_instruction_sha256=instruction_fingerprint([]), work_root=tmp_path / "native",
        rpc_factory=ReaderRPC)
    monkeypatch.setattr(transport, "_version", lambda: "pinned")
    monkeypatch.setattr(transport, "_server_names", lambda: ("memory",))
    binding = CampaignBinding(campaign_id="oauth-test", model=MODEL, reasoning_effort="medium",
        dataset_sha256="a" * 64, config_sha256="b" * 64, repo_revision="test", pins_sha256="c" * 64)
    ledger = CampaignLedger(tmp_path / "ledger.jsonl", binding,
        BudgetApproval.create(max_calls=1, max_cost_micros=10000))
    client = LunaResponsesClient(ledger, transport=transport)
    response = client.complete(call_id="reader", kind="reader", input="request",
                               input_tokens=32768, max_output_tokens=128)
    assert json.loads(response.text) == {"ok": True}
    assert response.usage.as_dict()["billing_basis"] == transport.billing_basis
    again = client.complete(call_id="reader", kind="reader", input="request",
                            input_tokens=32768, max_output_tokens=128)
    assert again.text == response.text
    assert sum(method == "turn/start" for method, _ in requests) == 1
    assert ledger.lookup("reader").status == "completed"
