# Engraphis for prime-agent

`engraphis-prime-agent` is the first-party [PrimeIntellect prime-agent](https://github.com/PrimeIntellect-ai/prime-agent)
integration for durable, local-first Engraphis memory. It lazily launches the existing
`engraphis-mcp` server on stdio and exposes the same nine-tool Smart MCP surface
that every other Engraphis host uses, so a prime-agent fleet gets prompt-ready
context, durable facts, and governed governance actions through one shared local
gateway.

A `PrimeAgentFleet` of eight named sub-agents (`researcher`, `planner`, `coder`,
`reviewer`, `tester`, `documenter`, `monitor`, `integrator`) shares one stdio
subprocess. Each sub-agent starts its own Engraphis session on first tool use,
so memory stays isolated by session while the gateway stays single-process.

## Architecture

At runtime the integration has three layers:

1. **A shared stdio subprocess.** The first time a `PrimeAgentFleet` is entered
   it spawns one `engraphis-mcp` process over JSON-RPC stdio. Every tool call
   from every sub-agent goes through that one process.
2. **A shared `EngraphisMcpClient`.** Owns the subprocess, exposes the
   `engraphis-mcp-classic` and the new Smart nine-tool surface, and serializes
   concurrent calls through an `asyncio.Lock` at the JSON-RPC frame layer.
3. **Eight named `EngraphisPrimeAgent` sub-agents.** Each one holds its own
   session id, lazily started on first tool use, and the same nine tool
   bindings. Sub-agent identity doubles as the default `repo` scope, so
   per-role memory isolation is the default.

The eight fixed names — `researcher`, `planner`, `coder`, `reviewer`, `tester`,
`documenter`, `monitor`, `integrator` — match the prime-agent roles the
integration was designed around. A custom fleet can be built by passing
`agent_names=[...]` to `PrimeAgentFleet(...)`; the stdio subprocess and the
client are still shared.

## When to use this vs. the Pi extension vs. the commandcode hook

All three integrations expose the same nine-tool Smart MCP surface against the
local Engraphis gateway. Choose by host, not by feature set.

| Integration | Host | Best for | Concurrency | Install |
|---|---|---|---|---|
| `integrations/prime_agent/` (this package) | [PrimeIntellect prime-agent](https://github.com/PrimeIntellect-ai/prime-agent) fleets of 1–8 named sub-agents | Multi-role pipelines (`researcher` → `coder` → `reviewer` → `tester`) that need per-role session isolation but one local gateway | Eight sub-agents share one stdio subprocess; tool calls serialize at the JSON-RPC frame layer | `pip install ./integrations/prime_agent` |
| [Pi extension](https://github.com/Coding-Dev-Tools/engraphis/blob/main/integrations/pi/README.md) | The Pi coding agent | A single interactive coding loop with prompt-ready recall, durable notes, and governed governance actions | One agent, one stdio gateway | Pi extension marketplace / `pip install engraphis-pi` |
| [Command Code SessionStart hook](https://github.com/Coding-Dev-Tools/engraphis/blob/main/integrations/commandcode/) | A Command Code session | Warming a brand-new session with bounded, cited context on `SessionStart`; fails open on timeout | One hook per session | `python scripts/install_cc_hook.py` |

Pick the prime-agent integration when you already have or want a multi-role
pipeline and the per-role memory boundary is useful. Pick the Pi extension for
single-agent interactive work. Pick the commandcode hook when you want a
zero-config, one-shot context warm-up at session start.

## Install

Install Engraphis 1.5.x with Python 3.10 or later. Version 1.5 introduced the
nine-tool Smart MCP contract required by this integration:

```bash
python -m pip install --upgrade "engraphis[mcp]>=1.5,<2"
```

Install this package from a checkout of the engraphis repository:

```bash
pip install ./integrations/prime_agent
```

Or, once published:

```bash
pip install engraphis-prime-agent
```

## Quick start

```python
import asyncio
from engraphis_prime_agent import PrimeAgentFleet

async def main():
    async with PrimeAgentFleet(workspace="myrepo") as fleet:
        # Warm every sub-agent's session up front so the first real
        # tool call on each role never blocks on session bootstrap.
        await fleet.start_all_sessions()

        # 1. The researcher asks for prior decisions on a topic.
        research = await fleet["researcher"].call(
            "engraphis_recall_context",
            {"query": "decision: sqlite-vec KNN", "k": 5, "token_budget": 600},
        )

        # 2. Fan out: the planner and the coder both look up the procedure
        #    for rebuilding persistent vectors after an embedding swap.
        plans = await fleet.fan_out(
            "engraphis_recall_context",
            {
                "planner": {"query": "procedure: rebuild persistent vectors", "k": 5},
                "coder":   {"query": "procedure: rebuild persistent vectors", "k": 5},
            },
        )

        # 3. The documenter persists the durable decision the coder just made.
        #    The integration returns the pending review boundary; the memory
        #    is not prompt-eligible until a human approves it (see "Trust model").
        pending = await fleet["documenter"].call("engraphis_remember", {
            "content": "Prefer sqlite-vec KNN for <=1M vectors; rebuild after model swap.",
            "importance": 0.7,
            "mtype": "semantic",
        })

        # 4. The reviewer scans the inbox for any new conflicts.
        review = await fleet["reviewer"].call("engraphis_conflict_review", {"limit": 10})

        return research, plans, pending, review

asyncio.run(main())
```

The example uses four of the eight sub-agents and exercises `recall_context`,
`remember`, and `conflict_review`. The four untasked sub-agents (`tester`,
`monitor`, `integrator`, and the second role of the fan-out) can be invoked
the same way — they are ordinary `EngraphisPrimeAgent` instances behind the
fleet's dict interface.

## Registering with prime-agent

After the package is installed, register it with prime-agent's tool manager:

```bash
python scripts/install_prime_agent.py
```

The installer is idempotent: re-running updates the existing entry instead of
duplicating it. Use `--uninstall` to remove the entry.

If prime-agent expects a different tool-registration surface, the single
adapter point is `EngraphisPrimeAgent.register()`. Pass any object with a
`register_tool(name, fn, schema=...)` method; the integration registers all
nine Smart tools with that target. Override the method (or pass a thin
adapter) if prime-agent's real API differs.

## Configuration

| Variable | Purpose |
|---|---|
| `ENGRAPHIS_MCP_COMMAND` | Override the `engraphis-mcp` console-script path (e.g. an absolute path under a virtualenv or pipx). |
| `ENGRAPHIS_DB_PATH` | Path to the local Engraphis SQLite database. The integration inherits whatever the gateway sees, so the dashboard and the fleet share one store. |
| `ENGRAPHIS_WORKSPACE` | Default workspace name. The fleet's `workspace=` overrides this. |
| `ENGRAPHIS_REPO` | Default repo scope. The fleet's `repo=` overrides this. |
| `PRIME_AGENT_CONFIG_PATH` | Override the prime-agent config file path used by `scripts/install_prime_agent.py`. |

Only `ENGRAPHIS_*`, `PATH`, `Path`, `SystemRoot`, and `ComSpec` are forwarded to
the gateway subprocess — never the full environment.

## The nine Smart tools

| Tool | Purpose |
|---|---|
| `engraphis_session` | Start, resume, or end a session for the calling sub-agent. |
| `engraphis_recall_context` | Compact, cited, token-budgeted context for the current task. |
| `engraphis_remember` | Persist a durable fact, decision, preference, or procedure. |
| `engraphis_discover_actions` | Find a best-fit advanced capability with a version-bound schema. |
| `engraphis_execute_read` | Run a discovered read-only advanced capability. |
| `engraphis_execute_action` | Run a discovered write/admin/destructive advanced capability. |
| `engraphis_get_memory` | Read one governed memory record by id. |
| `engraphis_update_memory` | Edit one memory's title/type/importance/audit actor. |
| `engraphis_conflict_review` | List pending, quarantined, or conflicting memories for review. |

## Concurrency model

The fleet shares one `EngraphisMcpClient`, which owns one `engraphis-mcp`
subprocess. The stdio transport is a single connection, so concurrent tool
calls are serialized at the JSON-RPC frame layer through an `asyncio.Lock`.
Framework-level concurrency (eight sub-agents reasoning in parallel and
issuing one tool call each) is unaffected — the `fan_out()` helper
demonstrates the pattern via `asyncio.gather`.

> **For true parallel MCP**, run multiple fleets against **distinct
> databases** (different `ENGRAPHIS_DB_PATH` values). Sharing a single
> database across two fleets is safe at the SQL level, but the stdio
> frame lock means you would pay for the same serialization twice. The
> default `PrimeAgentFleet` is designed for one workspace, one local
> gateway, eight sub-agents.

This serialization is intentional. See the design discussion in
[issue #1: shared stdio frame serialization](https://github.com/Coding-Dev-Tools/engraphis/issues/1)
("For true parallel MCP, run multiple fleets against distinct databases") for
the trade-offs that drove the choice of a single subprocess.

## Trust model

The integration runs with your local user permissions. Install only the
official package or a reviewed checkout. `ENGRAPHIS_MCP_COMMAND` should point
only to a trusted local executable.

Engraphis MCP writes enter the normal pending-review boundary. A successful
`engraphis_remember` call does not make unreviewed text prompt-eligible;
approve it through the Engraphis dashboard or the interactive approval
command before expecting it in normal recall. This behavior is intentional
and shared with the Pi and commandcode integrations.

## Testing

The test suite includes a fake MCP server (`tests/conftest.py`) so the default
unit tests do not require a live `engraphis-mcp` binary.

Run the unit suite:

```bash
cd integrations/prime_agent
python -m pip install -e ".[test]"
pytest -q
```

Run a single test file or test id:

```bash
pytest -q tests/test_agent.py
pytest -q tests/test_agent.py::TestEngraphisPrimeAgent::test_register
```

Run the **live-gated** tests, which require a real `engraphis-mcp` on `PATH`
and a writable temporary database:

```bash
ENGRAPHIS_INTEGRATION_LIVE=1 pytest -q
```

Live tests are skipped without the flag and are the right place to add any
new test that exercises real subprocess behavior. Keep them small and
idempotent; the fake server in `conftest.py` is the right home for everything
else.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `ModuleNotFoundError: No module named 'mcp'` | The MCP Python SDK is not installed | `pip install "engraphis[mcp]"` (or `pip install -e ".[test]"` for development) |
| `ERROR: engraphis-prime-agent requires Python >=3.10` (or a hard `SyntaxError` on import) | The active interpreter is 3.9 or older | Use Python 3.10+. The Engraphis 1.5 MCP server and the MCP SDK both require 3.10+ |
| `engraphis-mcp` is on `PATH` but the server starts and the tool list is empty or the Smart nine tools are missing | The installed `engraphis` is older than 1.5 | `pip install --upgrade "engraphis[mcp]>=1.5,<2"`. Version 1.5 introduced the nine-tool Smart contract this integration depends on |
| `ConnectionRefusedError` / `FileNotFoundError` / `OSError: [Errno 2] No such file or directory: 'engraphis-mcp'` when the fleet enters | `engraphis-mcp` is not on `PATH` for the Python that imports the integration | Install `engraphis[mcp]` in the same environment, or set `ENGRAPHIS_MCP_COMMAND` to the absolute path of the `engraphis-mcp` console script (for example `.venv/bin/engraphis-mcp` or `~/.local/bin/engraphis-mcp`) |
| `engraphis_prime_agent.cli` returns exit code 2 with "binary not on PATH" | Same as above, surfaced by the CLI check | Install `engraphis[mcp]`, or `pipx install "engraphis[mcp]"` if you intentionally keep the integration in a different venv |
| `pytest` cannot import `engraphis_prime_agent` from the repo checkout | The package was not installed in editable mode | From `integrations/prime_agent/`, run `pip install -e ".[test]"` |
| `Pending` memories never show up in normal recall | This is expected, not a bug | New writes enter the pending review boundary. Approve them through the Engraphis dashboard or `engraphis-cli review approve` before expecting them in normal recall (see "Trust model") |

If a failure is not on this list, run `python -m engraphis_prime_agent check`
against your environment — it returns one of the documented exit codes
(`0` ok, `1` incompatible tool set, `2` missing binary / install failure,
`3` transport error) and prints the matching hint.

## Contributing

The integration has one adapter point. Everything else — the eight named
sub-agents, the shared `EngraphisMcpClient`, the nine Smart tool bindings,
the stdio subprocess lifecycle, and the per-agent session bootstrap — is
fixed and reviewed as a unit.

**The single adapter point is `EngraphisPrimeAgent.register()`** in
`src/engraphis_prime_agent/agent.py`. The assumed contract is
`target.register_tool(name, fn, schema=...)` (LangChain / CrewAI style). If
prime-agent's real API differs, override this method or pass a thin adapter
that exposes the same shape. The body of `register()` is intentionally short
so a port is a small, reviewable change.

Before opening a PR:

1. Read the design notes in
   [`~/.commandcode/plans/prime-agent-integration.md`](https://github.com/Coding-Dev-Tools/engraphis/blob/main/integrations/prime_agent/)
   (host-local) or, when the host plan is not available, the PR description
   that introduced the integration. The eight sub-agent names, the shared
   stdio subprocess, the per-agent session boundary, and the
   `ENGRAPHIS_*`-only environment forwarding are all deliberate choices
   called out there.
2. Run `pytest -q` from `integrations/prime_agent/`. Unit tests must pass
   without `ENGRAPHIS_INTEGRATION_LIVE=1`.
3. If you changed the adapter point, the CLI install/uninstall, or the tool
   surface, also run `ENGRAPHIS_INTEGRATION_LIVE=1 pytest -q`.
4. Keep new live tests small and idempotent; prefer extending the fake
   server in `tests/conftest.py` for anything that is not really testing the
   subprocess.

## License

Apache-2.0. See `LICENSE` and `NOTICE`.
