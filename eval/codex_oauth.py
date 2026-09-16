"""Exact-model benchmark reader through the native Codex ChatGPT login.

No credential files are read or copied. The pinned CLI owns OAuth and its HTTP
transport. Each request uses an ephemeral thread without environment access.
Codex does not expose a provider output-token cap here: the campaign limit is
an observed-usage acceptance threshold, never a subscription billing ceiling.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import threading
import time
from typing import Any, Callable, Mapping, Optional
import uuid

from eval.campaign_api import CampaignAPIError, MODEL, REASONING_EFFORT

PROVIDER = "engraphis_benchmark_oauth"
BASE_INSTRUCTIONS = "Answer the supplied benchmark request. Do not use tools or external information."
DISABLED_FEATURES = (
    "shell_tool", "unified_exec", "multi_agent", "multi_agent_v2", "apps", "plugins", "hooks",
    "memories", "memory_tool", "skill_search", "skill_mcp_dependency_install", "browser_use",
    "browser_use_external", "computer_use", "image_generation", "unbounded_connection_retries",
    "code_mode", "code_mode_host", "shell_snapshot", "js_repl",
    "apply_patch_freeform", "request_permissions_tool", "search_tool",
    "standalone_web_search", "tool_search", "tool_suggest", "responses_websockets",
    "responses_websockets_v2", "web_search", "web_search_cached", "web_search_request",
)


def oauth_environment(source: Mapping[str, str]) -> dict[str, str]:
    """Sanitize only the child process; leave the user's settings unchanged."""
    denied = {"CODEX_API_KEY", "CODEX_PROXY_URL", "CODEX_CHATGPT_BASE_URL",
              "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"}
    return {key: value for key, value in source.items()
            if not (key.upper().startswith(("OPENAI_", "AZURE_OPENAI_")) or key.upper() in denied)}


def instruction_fingerprint(sources: list[str]) -> str:
    """Bind native instruction bytes, without exporting their text or home paths."""
    rows = []
    for value in sources:
        path = Path(value)
        if not path.is_absolute() or not path.is_file():
            raise CampaignAPIError("Codex instruction source cannot be verified")
        rows.append({"name": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def oauth_settings(disabled_servers: tuple[str, ...] = ()) -> dict[str, Any]:
    settings: dict[str, Any] = {
        "forced_login_method": "chatgpt", "model_provider": PROVIDER,
        f"model_providers.{PROVIDER}.name": "Engraphis Codex OAuth benchmark",
        f"model_providers.{PROVIDER}.requires_openai_auth": True,
        f"model_providers.{PROVIDER}.request_max_retries": 0,
        f"model_providers.{PROVIDER}.stream_max_retries": 0,
        f"model_providers.{PROVIDER}.supports_websockets": False,
        "model": MODEL, "model_reasoning_effort": REASONING_EFFORT,
        "project_doc_max_bytes": 0, "web_search": "disabled", "history.persistence": "none",
        "skills.include_instructions": False, "include_apps_instructions": False,
        "include_collaboration_mode_instructions": False, "include_permissions_instructions": False,
        "developer_instructions": "", "approval_policy": "never",
        "tools.update_plan.enabled": False, "tools.experimental_request_user_input.enabled": False,
    }
    settings.update({f"features.{name}": False for name in DISABLED_FEATURES})
    for name in disabled_servers:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise CampaignAPIError("Codex MCP server name cannot be safely disabled")
        settings[f"mcp_servers.{name}.enabled"] = False
    return settings


def _check_account_config(rpc: Any, *, require_disabled: bool) -> tuple[str, ...]:
    account = rpc.request("account/read", {"refreshToken": False}).get("account")
    if not isinstance(account, dict) or account.get("type") != "chatgpt":
        raise CampaignAPIError("Codex OAuth requires an existing ChatGPT sign-in; API keys are rejected")
    config = rpc.request("config/read", {"includeLayers": False}).get("config", {})
    provider = config.get("model_providers", {}).get(PROVIDER, {})
    if (config.get("forced_login_method") != "chatgpt"
            or config.get("model_provider") != PROVIDER
            or provider.get("requires_openai_auth") is not True
            or provider.get("request_max_retries") != 0
            or provider.get("stream_max_retries") != 0
            or provider.get("supports_websockets") is not False
            or any(provider.get(key) for key in
                   ("base_url", "env_key", "experimental_bearer_token", "http_headers",
                    "env_http_headers", "query_params"))):
        raise CampaignAPIError("Codex OAuth provider configuration differs from the frozen native route")
    servers = config.get("mcp_servers", {})
    if require_disabled and any(value.get("enabled", True) for value in servers.values()):
        raise CampaignAPIError("Codex inherited an enabled MCP server")
    for key, expected in oauth_settings().items():
        # 0.149.0 does not echo the newer optional tools switches. Environment
        # access is independently disabled in both thread/start and turn/start;
        # every observed tool item is rejected by collect_turn.
        if key.startswith(("model_providers.", "tools.")):
            continue
        observed: Any = config
        for part in key.split("."):
            observed = observed.get(part) if isinstance(observed, dict) else None
        if observed != expected:
            raise CampaignAPIError(f"Codex effective reader setting differs: {key}")
    return tuple(sorted(servers))


class _NativeRPC:
    """Small stdio client; no account/config response is written to the journal."""

    def __init__(self, executable: str, directory: Path, settings: dict[str, Any], timeout: float):
        command = [executable, "app-server", "--stdio"]
        for key, value in settings.items():
            command.extend(["-c", f"{key}={json.dumps(value)}"])
        self.deadline = time.monotonic() + timeout
        self.messages: queue.Queue[Any] = queue.Queue()
        self.pending: list[dict[str, Any]] = []
        self.sequence = 0
        self.stderr = (directory / "stderr.log").open("x", encoding="utf-8")
        try:
            self.process = subprocess.Popen(
                command, cwd=directory, env=oauth_environment(os.environ), stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=self.stderr, text=True, encoding="utf-8", bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            self.stderr.close()
            raise
        threading.Thread(target=self._read, daemon=True).start()
        try:
            self.request("initialize", {"clientInfo": {"name": "engraphis_eval", "version": "1.0"},
                                        "capabilities": {"experimentalApi": True}})
            self.send({"method": "initialized"})
        except Exception:
            self.close()
            raise

    def _read(self) -> None:
        try:
            assert self.process.stdout is not None
            for line in self.process.stdout:
                self.messages.put(json.loads(line))
        except (ValueError, OSError):
            self.messages.put(None)
        finally:
            self.messages.put(None)

    def send(self, value: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(value) + "\n")
        self.process.stdin.flush()

    def receive(self) -> dict[str, Any]:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Codex OAuth attempt timed out; automatic retry is disabled")
        try:
            value = self.messages.get(timeout=remaining)
        except queue.Empty as exc:
            raise TimeoutError("Codex OAuth attempt timed out") from exc
        if value is None:
            raise EOFError("Codex OAuth exited without a durable turn completion")
        if "id" in value and "method" in value:
            self.send({"id": value["id"], "error": {"code": -32601,
                       "message": "Benchmark tool and approval requests are disabled"}})
            raise CampaignAPIError("Codex requested an unexpected tool or approval")
        return value

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.sequence += 1
        self.send({"id": self.sequence, "method": method, "params": params})
        while True:
            value = self.receive()
            if value.get("id") == self.sequence:
                if "error" in value:
                    raise CampaignAPIError(f"Codex rejected {method}; provider body withheld")
                return value["result"]
            self.pending.append(value)

    def next_event(self) -> dict[str, Any]:
        return self.pending.pop(0) if self.pending else self.receive()

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
        self.stderr.close()


def _thread(rpc: Any, directory: Path, settings: dict[str, Any], instructions: str) -> dict:
    result = rpc.request("thread/start", {
        "model": MODEL, "modelProvider": PROVIDER, "ephemeral": True,
        "cwd": str(directory), "sandbox": "read-only", "approvalPolicy": "never",
        "baseInstructions": BASE_INSTRUCTIONS, "developerInstructions": instructions,
        "environments": [], "runtimeWorkspaceRoots": [], "dynamicTools": [],
        "allowProviderModelFallback": False, "config": settings, "experimentalRawEvents": False,
    })
    if (result.get("model") != MODEL or result.get("modelProvider") != PROVIDER
            or result.get("reasoningEffort") != REASONING_EFFORT):
        raise CampaignAPIError("Codex returned a different model, provider or reasoning effort")
    return result


class CodexOAuthTransport:
    identity = "codex_oauth"
    billing_basis = "subscription_usage_api_price_proxy_not_invoice"

    def __init__(self, *, executable: str, expected_version: str,
                 expected_instruction_sha256: str, work_root: Path,
                 expected_executable_sha256: Optional[str] = None,
                 timeout_seconds: float = 180, rpc_factory: Optional[Callable[..., Any]] = None):
        self.executable = str(Path(executable).resolve())
        self.expected_version = expected_version
        self.expected_instruction_sha256 = expected_instruction_sha256
        self.expected_executable_sha256 = expected_executable_sha256
        self.work_root = Path(work_root).resolve()
        self.timeout = timeout_seconds
        self.rpc_factory = rpc_factory or _NativeRPC
        self.work_root.mkdir(parents=True, exist_ok=True)

    def _version(self) -> str:
        self._verify_executable()
        observed = subprocess.run([self.executable, "--version"], capture_output=True,
                                  text=True, check=True, timeout=15,
                                  env=oauth_environment(os.environ)).stdout.strip()
        if observed != self.expected_version:
            raise CampaignAPIError("Codex executable version changed after campaign freeze")
        return observed

    def _verify_executable(self) -> None:
        if self.expected_executable_sha256 is not None:
            observed = hashlib.sha256(Path(self.executable).read_bytes()).hexdigest()
            if observed != self.expected_executable_sha256:
                raise CampaignAPIError("Codex executable bytes changed after campaign freeze")

    def _server_names(self) -> tuple[str, ...]:
        self._verify_executable()
        # Native configuration listing does not create a thread or start MCP servers.
        # Its potentially sensitive config values stay in memory and are not journaled.
        command = [self.executable, "mcp", "list", "--json"]
        for key, value in oauth_settings().items():
            command.extend(["-c", f"{key}={json.dumps(value)}"])
        result = subprocess.run(command,
            capture_output=True, text=True, check=True, timeout=30,
            env=oauth_environment(os.environ), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        rows = json.loads(result.stdout)
        if not isinstance(rows, list) or any(not isinstance(row.get("name"), str) for row in rows):
            raise CampaignAPIError("Codex MCP configuration listing is malformed")
        return tuple(sorted(row["name"] for row in rows))

    def _open(self, directory: Path, settings: dict[str, Any]) -> Any:
        self._verify_executable()
        directory.mkdir(parents=True, exist_ok=False)
        return self.rpc_factory(self.executable, directory, settings, self.timeout)

    def _configured_rpc(self, directory: Path) -> tuple[Any, dict[str, Any]]:
        settings = oauth_settings(self._server_names())
        rpc = self._open(directory / "reader", settings)
        try:
            _check_account_config(rpc, require_disabled=True)
        except Exception:
            rpc.close()
            raise
        return rpc, settings

    def inspect(self) -> dict[str, Any]:
        """Readiness without a generation; return no credentials or account identity."""
        self._version()
        directory = self.work_root / f"readiness-{uuid.uuid4().hex}"
        rpc, settings = self._configured_rpc(directory)
        try:
            result = _thread(rpc, directory / "reader", settings, "")
            fingerprint = instruction_fingerprint(result.get("instructionSources", []))
            if self.expected_instruction_sha256 and fingerprint != self.expected_instruction_sha256:
                raise CampaignAPIError("Codex native instruction sources changed after campaign freeze")
            return {"transport": self.identity, "account_type": "chatgpt",
                    "executable": self.executable, "version": self.expected_version,
                    "instruction_sha256": fingerprint, "model": result["model"],
                    "reasoning_effort": result["reasoningEffort"], "automatic_retries": 0,
                    "billing_basis": self.billing_basis,
                    "output_token_limit": "observed_usage_acceptance_threshold"}
        finally:
            rpc.close()

    def create(self, **kwargs: Any) -> dict[str, Any]:
        if (kwargs.get("model") != MODEL
                or kwargs.get("reasoning") != {"effort": REASONING_EFFORT}
                or kwargs.get("tools") != [] or kwargs.get("store") is not False):
            raise CampaignAPIError("Codex OAuth request differs from the frozen reader contract")
        call_id = kwargs.get("benchmark_call_id")
        request_hash = kwargs.get("benchmark_request_sha256")
        if (not isinstance(call_id, str) or not re.fullmatch(r"[a-z][a-z0-9_.:-]{0,127}", call_id)
                or not isinstance(request_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", request_hash)):
            raise CampaignAPIError("Codex OAuth requires a durable campaign call binding")
        self._version()
        directory = self.work_root / f"attempt-{uuid.uuid4().hex}"
        rpc, settings = self._configured_rpc(directory)
        journal = directory / "events.jsonl"
        try:
            result = _thread(rpc, directory / "reader", settings, kwargs.get("instructions") or "")
            fingerprint = instruction_fingerprint(result.get("instructionSources", []))
            if fingerprint != self.expected_instruction_sha256:
                raise CampaignAPIError("Codex native instruction sources changed after campaign freeze")
            thread_id = result["thread"]["id"]
            prompt = kwargs["input"]
            if not isinstance(prompt, str):
                prompt = json.dumps(prompt, sort_keys=True, ensure_ascii=False)
            params: dict[str, Any] = {
                "threadId": thread_id, "input": [{"type": "text", "text": prompt}],
                "model": MODEL, "effort": REASONING_EFFORT, "environments": [],
                "runtimeWorkspaceRoots": [], "approvalPolicy": "never",
            }
            schema = (kwargs.get("text") or {}).get("format", {}).get("schema")
            if schema is not None:
                params["outputSchema"] = schema
            # This file survives a parent crash before the ledger completion.
            _journal(journal, {"event": "dispatch", "call_id": call_id, "request_sha256": request_hash,
                               "thread_id": thread_id, "model": result["model"],
                               "effort": result["reasoningEffort"], "instruction_sha256": fingerprint})
            turn = rpc.request("turn/start", params)["turn"]
            _journal(journal, {"event": "turn_binding", "call_id": call_id,
                               "request_sha256": request_hash, "thread_id": thread_id, "turn_id": turn["id"]})
            output, usage = collect_turn(rpc, thread_id, turn["id"], journal)
            return {"id": turn["id"], "model": result["model"], "output_text": output,
                    "usage": usage, "benchmark_provenance": {
                        "transport": self.identity, "billing_basis": self.billing_basis,
                        "transport_identity": self.identity, "requested_model": MODEL,
                        "effective_model": result["model"], "reasoning_effort": result["reasoningEffort"],
                        "model_verification": "native_thread_start_and_no_model_reroute",
                        "cli_version": self.expected_version, "instruction_sha256": fingerprint,
                        "native_journal_sha256": hashlib.sha256(journal.read_bytes()).hexdigest(),
                        "output_token_limit": "observed_usage_acceptance_threshold",
                        "provider_hard_output_cap": False, "automatic_retries": 0}}
        finally:
            rpc.close()


def _journal(path: Path, value: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, sort_keys=True, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def collect_turn(rpc: Any, thread_id: str, turn_id: str, journal: Path) -> tuple[str, dict]:
    """Reject reroutes, tool calls, compaction/retries, incomplete or unmetered turns."""
    output: dict[str, str] = {}
    usage = None
    global_events = {"remoteControl/status/changed", "account/rateLimits/updated",
                     "deprecationNotice", "warning"}
    thread_events = {"thread/started", "thread/status/changed"}
    turn_events = {"turn/started", "turn/completed", "item/started", "item/completed",
                   "item/agentMessage/delta", "thread/tokenUsage/updated",
                   "item/reasoning/summaryTextDelta", "item/reasoning/summaryPartAdded",
                   "item/reasoning/textDelta"}
    while True:
        event = rpc.next_event()
        method, params = event.get("method", ""), event.get("params", {})
        # Raw account/config payloads and reasoning content are deliberately omitted.
        _journal(journal, {"event": method, "thread_id": thread_id, "turn_id": turn_id})
        if "rerout" in method.lower():
            raise CampaignAPIError("Codex returned a different model through a reroute")
        if method == "error":
            raise CampaignAPIError("Codex reported a failed turn; automatic retry is disabled")
        if method not in global_events | thread_events | turn_events:
            raise CampaignAPIError("Codex emitted an unexpected native event")
        if method in thread_events | turn_events:
            observed_thread = (params.get("thread", {}).get("id") if method == "thread/started"
                               else params.get("threadId"))
            if observed_thread != thread_id:
                raise CampaignAPIError("Codex native event thread binding mismatch")
        if method in turn_events:
            observed_turn = (params.get("turn", {}).get("id") if method.startswith("turn/")
                             else params.get("turnId"))
            if observed_turn != turn_id:
                raise CampaignAPIError("Codex native event turn binding mismatch")
        if method in {"item/started", "item/completed"}:
            item = params.get("item", {})
            kind = item.get("type")
            if kind not in {"userMessage", "agentMessage", "reasoning"}:
                raise CampaignAPIError("Codex produced an unexpected tool, compaction or item")
            if method == "item/completed" and kind == "agentMessage":
                output[item["id"]] = item["text"]
                _journal(journal, {"event": "output", "text": item["text"]})
        if method == "thread/tokenUsage/updated":
            usage = params["tokenUsage"]["total"]
            keys = ("inputTokens", "cachedInputTokens", "outputTokens", "reasoningOutputTokens", "totalTokens")
            if (any(type(usage.get(key)) is not int or usage[key] < 0 for key in keys)
                    or usage["cachedInputTokens"] > usage["inputTokens"]
                    or usage["reasoningOutputTokens"] > usage["outputTokens"]
                    or usage["totalTokens"] < usage["inputTokens"] + usage["outputTokens"]):
                raise CampaignAPIError("Codex provider usage is malformed")
            _journal(journal, {"event": "usage", "usage": usage})
        if method == "turn/completed":
            turn = params["turn"]
            if turn.get("id") != turn_id or turn.get("status") != "completed":
                raise CampaignAPIError("Codex turn did not complete successfully")
            if not output or usage is None:
                raise CampaignAPIError("Codex completed without text or provider usage")
            return "\n".join(output.values()), {
                "input_tokens": usage["inputTokens"], "cached_input_tokens": usage["cachedInputTokens"],
                "output_tokens": usage["outputTokens"], "reasoning_tokens": usage["reasoningOutputTokens"],
                "total_tokens": usage["totalTokens"],
            }
