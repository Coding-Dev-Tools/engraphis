"""Command Code SessionStart hook for Engraphis durable memory.

Reads the SessionStart payload from stdin, opens a session against the local
Engraphis MCP server, and emits hook JSON whose ``additionalContext`` carries
bounded recalled memory into the new session's first turn. Fails open: any
error, timeout, or empty context prints nothing and exits 0.
"""

import json
import os
import sys
import time
import urllib.request

MCP_URL = os.environ.get("ENGRAPHIS_MCP_URL", "http://127.0.0.1:8711/mcp")
BUDGET_SECONDS = float(os.environ.get("ENGRAPHIS_HOOK_BUDGET_S", "4.0"))
MAX_CONTEXT_CHARS = int(os.environ.get("ENGRAPHIS_HOOK_MAX_CHARS", "1500"))
CONTEXT_HEADER = (
    "Durable memory (engraphis, workspace {workspace}) relevant to this repo:\n"
)
CONTEXT_FOOTER = "\nUse mcp__engraphis__ tools for more recall."

# Localhost server; never route through a configured system proxy.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def post(url, payload, timeout):
    """POST one JSON-RPC message; return its decoded JSON or SSE response."""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        method="POST",
    )
    with OPENER.open(request, timeout=timeout) as response:
        body = response.read().decode("utf-8", errors="replace")
    try:
        return json.loads(body)
    except ValueError:
        pass
    candidates = []
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        try:
            candidates.append(json.loads(line[5:].strip()))
        except ValueError:
            continue
    responses = [c for c in candidates if isinstance(c, dict) and "result" in c]
    return responses[-1] if responses else None


def rpc(method, params, rpc_id, deadline):
    """Issue one JSON-RPC request within the shared time budget."""
    remaining = deadline - time.monotonic()
    if remaining <= 0.05:
        raise TimeoutError("time budget exhausted")
    response = post(MCP_URL, {"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params}, remaining)
    if isinstance(response, dict) and "result" in response:
        return response["result"]
    return None


def notify_initialized(deadline):
    """Best-effort notifications/initialized; stateless servers reply 202/empty."""
    remaining = deadline - time.monotonic()
    if remaining <= 0.05:
        return
    try:
        post(MCP_URL, {"jsonrpc": "2.0", "method": "notifications/initialized"}, remaining)
    except Exception:
        pass


def extract_context(result):
    """Defensively pull the context text out of a tools/call result."""
    content = result.get("content") if isinstance(result, dict) else None
    if not content or not isinstance(content[0], dict):
        return ""
    text = content[0].get("text")
    if not isinstance(text, str) or not text.strip():
        return ""
    try:
        parsed = json.loads(text)
    except ValueError:
        return text.strip()
    if isinstance(parsed, dict) and isinstance(parsed.get("context"), str):
        return parsed["context"].strip()
    return ""


def session_context(repo, workspace, deadline):
    """initialize -> initialized -> tools/call engraphis_session(action=start)."""
    rpc(
        "initialize",
        {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "cc-hook", "version": "1.0"},
        },
        1,
        deadline,
    )
    notify_initialized(deadline)
    result = rpc(
        "tools/call",
        {
            "name": "engraphis_session",
            "arguments": {
                "action": "start",
                "workspace": workspace,
                "repo": repo,
                # Context is only returned when a goal is supplied.
                "goal": (
                    "Resume work on this repository: surface relevant durable "
                    "decisions, preferences, procedures, and open threads."
                ),
            },
        },
        2,
        deadline,
    )
    return extract_context(result)


def resolve_workspace(cwd, env):
    """Honor ENGRAPHIS_HOOK_WORKSPACE; otherwise fall back to the repo basename.

    Workspace names are bounded (``_clean_name`` in the service refuses empties and
    overlong inputs), so we drop the override silently if it would be rejected.
    """
    override = (env.get("ENGRAPHIS_HOOK_WORKSPACE") or "").strip()
    if override:
        return override
    return os.path.basename(os.path.normpath(str(cwd)))


def build_additional_context(context, workspace):
    header = CONTEXT_HEADER.format(workspace=workspace)
    footer = CONTEXT_FOOTER
    body_budget = MAX_CONTEXT_CHARS - len(header) - len(footer)
    if body_budget <= 0:
        # Header+footer already exceed the budget. Truncate the header so the
        # final payload stays within MAX_CONTEXT_CHARS and the agent still gets
        # a recognisable prompt header for the workspace.
        return (header + footer)[:MAX_CONTEXT_CHARS]
    return (header + context[:body_budget] + footer)[:MAX_CONTEXT_CHARS]


def main():
    deadline = time.monotonic() + BUDGET_SECONDS
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return 0
    if not isinstance(payload, dict):
        return 0
    name = payload.get("hook_event_name")
    if name is not None and name != "SessionStart":
        return 0
    cwd = payload.get("cwd") or os.environ.get("COMMANDCODE_PROJECT_DIR") or os.getcwd()
    repo = os.path.basename(os.path.normpath(str(cwd)))
    workspace = resolve_workspace(cwd, os.environ)
    try:
        context = session_context(repo, workspace, deadline)
    except Exception:
        return 0
    if not context:
        return 0
    output = {
        "suppressOutput": False,
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": build_additional_context(context, workspace),
        },
    }
    sys.stdout.write(json.dumps(output))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:
        sys.exit(0)
