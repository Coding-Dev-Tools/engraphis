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

MCP_URL_DEFAULT = "http://127.0.0.1:8711/mcp"
BUDGET_SECONDS_DEFAULT = 4.0
MAX_CONTEXT_CHARS_DEFAULT = 1500
# Backwards-compatible aliases. The module-level constants previously
# crashed import when these env vars held malformed values; both are now
# resolved lazily inside main() so the hook keeps its fail-open
# contract. Tests and external callers that referenced the old names
# keep working.
MCP_URL = MCP_URL_DEFAULT
BUDGET_SECONDS = BUDGET_SECONDS_DEFAULT
MAX_CONTEXT_CHARS = MAX_CONTEXT_CHARS_DEFAULT
CONTEXT_HEADER = (
    "Durable memory (engraphis, workspace {workspace}) relevant to this repo:\n"
)
CONTEXT_FOOTER = "\nUse mcp__engraphis__ tools for more recall."

# Localhost server; never route through a configured system proxy.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _env_float(name: str, default: float) -> float:
    """Parse an env-var as float, falling back on any conversion error.

    The conversion happens inside the fail-open boundary so a malformed
    ENGRAPHIS_HOOK_BUDGET_S cannot crash the module at import time and
    cause every SessionStart to fail.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default

# Header name the MCP spec uses for the stateful session id. The bundled
# dashboard /mcp endpoint issues one on initialize and rejects subsequent
# requests that omit it; stateless servers ignore it.
MCP_SESSION_HEADER = "Mcp-Session-Id"


def post(url, payload, timeout, session_id: str | None = None):
    """POST one JSON-RPC message; return (decoded_body, response_session_id).

    The response_session_id is the Mcp-Session-Id returned by the server (or
    echoed from the request if the server didn't issue a new one) so the
    caller can thread the same value into subsequent requests on a
    stateful transport.
    """
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        method="POST",
    )
    if session_id:
        # State transports (the dashboard /mcp endpoint in particular) reject
        # requests that arrive without the session id they issued at
        # initialize. Forward the id so notifications/initialized and
        # tools/call stay on the same session.
        request.add_header(MCP_SESSION_HEADER, session_id)
    with OPENER.open(request, timeout=timeout) as response:
        body = response.read().decode("utf-8", errors="replace")
        response_session_id = response.headers.get(MCP_SESSION_HEADER) or session_id
    try:
        return json.loads(body), response_session_id
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
    return (responses[-1] if responses else None), response_session_id


def rpc(method, params, rpc_id, deadline, session_id: str | None = None,
         url: str = MCP_URL_DEFAULT):
    """Issue one JSON-RPC request within the shared time budget.

    ``session_id`` is threaded into the Mcp-Session-Id header on every
    request after initialize; stateful transports require it. When the
    server issues a fresh ``Mcp-Session-Id`` in the response (initialize
    is the canonical case), the returned id is propagated so the caller
    threads it into every subsequent request on the same session.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0.05:
        raise TimeoutError("time budget exhausted")
    response, response_session_id = post(
        url,
        {"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params},
        remaining,
        session_id=session_id,
    )
    # ``post`` echoes the request id when the server did not issue a new
    # one; otherwise the response carries the freshly-issued id. Forward
    # whichever the server gave us so stateful transports keep their
    # session open across the initialize -> initialized -> tools/call
    # handshake.
    next_session_id = response_session_id or session_id
    if isinstance(response, dict) and "result" in response:
        return response["result"], next_session_id
    return None, next_session_id


def notify_initialized(deadline, session_id: str | None = None,
                     url: str = MCP_URL_DEFAULT):
    """Best-effort notifications/initialized; stateless servers reply 202/empty."""
    remaining = deadline - time.monotonic()
    if remaining <= 0.05:
        return session_id
    try:
        _, response_session_id = post(
            url,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            remaining,
            session_id=session_id,
        )
        return response_session_id
    except Exception:
        return session_id


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


def session_context(repo, workspace, deadline, mcp_url: str = MCP_URL_DEFAULT):
    """initialize -> initialized -> tools/call engraphis_session(action=start).

    The Mcp-Session-Id returned by initialize is threaded into every
    subsequent request so a stateful transport (e.g. the dashboard /mcp
    endpoint) keeps the connection open and recognises the tool call as
    part of the same session.
    """
    _, session_id = rpc(
        "initialize",
        {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "cc-hook", "version": "1.0"},
        },
        1,
        deadline,
        url=mcp_url,
    )
    session_id = (
        notify_initialized(deadline, session_id=session_id, url=mcp_url) or session_id
    )
    result, _ = rpc(
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
        session_id=session_id,
        url=mcp_url,
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


def build_additional_context(context, workspace, max_context_chars=None):
    if max_context_chars is None:
        max_context_chars = MAX_CONTEXT_CHARS
    header = CONTEXT_HEADER.format(workspace=workspace)
    footer = CONTEXT_FOOTER
    body_budget = max_context_chars - len(header) - len(footer)
    if body_budget <= 0:
        # Header+footer already exceed the budget. Truncate the header so the
        # final payload stays within the limit and the agent still gets
        # a recognisable prompt header for the workspace.
        return (header + footer)[:max_context_chars]
    return (header + context[:body_budget] + footer)[:max_context_chars]


def main():
    mcp_url = os.environ.get("ENGRAPHIS_MCP_URL") or MCP_URL_DEFAULT
    budget_seconds = _env_float("ENGRAPHIS_HOOK_BUDGET_S", BUDGET_SECONDS_DEFAULT)
    max_context_chars = _env_int("ENGRAPHIS_HOOK_MAX_CHARS", MAX_CONTEXT_CHARS_DEFAULT)
    deadline = time.monotonic() + budget_seconds
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
        context = session_context(repo, workspace, deadline, mcp_url=mcp_url)
    except Exception:
        return 0
    if not context:
        return 0
    output = {
        "suppressOutput": False,
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": build_additional_context(
                context, workspace, max_context_chars
            ),
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
