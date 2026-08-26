"""Console entry point: ``engraphis-prime-agent check|status|register|install|version``.

Exit codes (convention used across subcommands):
  0 - success
  1 - the MCP server was reachable but is misconfigured (e.g. wrong tool set)
  2 - dependency missing on the host (binary not on PATH, install script
      reported a config problem, or a transitive module is unavailable)
  3 - the MCP server could not be reached at all (subprocess error, IO,
      timeout, JSON-RPC handshake failure)
  64 - command-line usage error (argparse default)
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import runpy
import shutil
import sys
from pathlib import Path
from typing import Any

from .agent import PrimeAgentFleet
from .config import build_runtime_config
from .mcp_client import EngraphisCompatibilityError, EngraphisMcpClient

#: Exit code used when the configured MCP command is not on PATH.
EXIT_MISSING_BINARY = 2
#: Exit code used when the MCP server is reachable but its tool surface is
#: incompatible with what this integration expects.
EXIT_INCOMPATIBLE = 1
#: Exit code used for any other transport / connect / IO failure.
EXIT_TRANSPORT = 3
#: Exit code used when the install/uninstall script reports a config error.
EXIT_INSTALL_FAILED = 2

#: Hint printed when ``shutil.which(config.command)`` comes back empty.
_MISSING_BINARY_HINT = (
    "The Engraphis MCP console script was not found on PATH. "
    "Install the Smart MCP extra with: pip install \"engraphis[mcp]>=1.5,<2\""
)


def _json_default(value: Any) -> Any:
    """``json`` default that handles ``bytes`` (base64) and falls back to ``str``."""
    if isinstance(value, bytes):
        return {"__type__": "bytes", "base64": base64.b64encode(value).decode("ascii")}
    return str(value)


def _print_json(obj: Any) -> None:
    json.dump(obj, sys.stdout, indent=2, sort_keys=True, default=_json_default)
    sys.stdout.write("\n")


def _print_human_check(result: dict[str, Any]) -> None:
    if result.get("ok"):
        status = result.get("status") or {}
        print(
            f"ok: engraphis-mcp reachable, {status.get('toolCount', '?')} tools "
            f"(server={status.get('server')!r})"
        )
    else:
        print(f"error: {result.get('error')}")
        hint = result.get("hint")
        if hint:
            print(f"hint:  {hint}")


def _print_human_status(result: dict[str, Any]) -> None:
    agents = result.get("agents") or []
    print(f"workspace: {result.get('workspace')}")
    print(f"agents:    {len(agents)}")
    for entry in agents:
        sid = entry.get("session_id") or "-"
        print(f"  - {entry.get('name'):<11} session_id={sid}")


def _check(as_json: bool) -> int:
    """Boot ``engraphis-mcp`` once and report status.

    Returns 0 on success, 1 on a compatibility error (server reachable but
    missing tools), 2 if the binary is not on PATH, 3 on any other failure.
    """
    config = build_runtime_config()
    binary_path = shutil.which(config.command)
    if binary_path is None:
        # Don't even try to spawn: report an actionable error and a distinct
        # exit code so a wrapper script can tell "binary missing" apart from
        # "server reachable but wrong tool set".
        result = {
            "ok": False,
            "error": f"command not found on PATH: {config.command!r}",
            "hint": _MISSING_BINARY_HINT,
            "command": config.command,
        }
        if as_json:
            _print_json(result)
        else:
            _print_human_check(result)
        return EXIT_MISSING_BINARY

    print(f"command: {config.command} -> {binary_path}", file=sys.stderr)

    async def _run() -> tuple[dict[str, Any], int]:
        client = EngraphisMcpClient(config)
        try:
            await client.connect()
            status = await client.status()
            return {"ok": True, "command": config.command, "binary": binary_path, "status": status}, 0
        except EngraphisCompatibilityError as exc:
            return (
                {
                    "ok": False,
                    "error": str(exc),
                    "hint": (
                        "The server is reachable but is missing the Smart 9-tool "
                        "surface. Upgrade with: pip install --upgrade "
                        "\"engraphis[mcp]>=1.5,<2\""
                    ),
                    "command": config.command,
                    "binary": binary_path,
                },
                EXIT_INCOMPATIBLE,
            )
        except Exception as exc:  # noqa: BLE001 — surface to user
            hint = client.diagnostic_hint()
            return (
                {
                    "ok": False,
                    "error": str(exc),
                    "hint": hint,
                    "command": config.command,
                    "binary": binary_path,
                },
                EXIT_TRANSPORT,
            )
        finally:
            await client.close()

    result, exit_code = asyncio.run(_run())
    if as_json:
        _print_json(result)
    else:
        _print_human_check(result)
    return exit_code


def _status(as_json: bool) -> int:
    async def _run() -> tuple[dict[str, Any], int]:
        config = build_runtime_config()
        # Fail fast (and actionably) if the MCP command isn't on PATH, so the
        # user doesn't have to read a stack trace to know the remedy.
        if shutil.which(config.command) is None:
            return (
                {
                    "ok": False,
                    "error": f"command not found on PATH: {config.command!r}",
                    "hint": _MISSING_BINARY_HINT,
                },
                EXIT_MISSING_BINARY,
            )
        try:
            async with PrimeAgentFleet(workspace="prime-agent-cli") as fleet:
                return {"ok": True, **fleet.status()}, 0
        except FileNotFoundError as exc:
            return (
                {
                    "ok": False,
                    "error": str(exc),
                    "hint": (
                        f"Could not launch {config.command!r}. "
                        "Install it with: pip install \"engraphis[mcp]>=1.5,<2\""
                    ),
                },
                EXIT_MISSING_BINARY,
            )
        except EngraphisCompatibilityError as exc:
            return (
                {
                    "ok": False,
                    "error": str(exc),
                    "hint": (
                        "The server is reachable but is missing the Smart 9-tool "
                        "surface. Upgrade with: pip install --upgrade "
                        "\"engraphis[mcp]>=1.5,<2\""
                    ),
                },
                EXIT_INCOMPATIBLE,
            )
        except Exception as exc:  # noqa: BLE001 — surface to user
            return (
                {"ok": False, "error": str(exc), "errorType": type(exc).__name__},
                EXIT_TRANSPORT,
            )

    result, exit_code = asyncio.run(_run())
    if as_json:
        _print_json(result)
    else:
        if result.get("ok"):
            _print_human_status(result)
        else:
            print(f"error: {result.get('error')}")
            hint = result.get("hint")
            if hint:
                print(f"hint:  {hint}")
    return exit_code


def _register(as_json: bool) -> int:
    """Print the prime-agent config snippet to stdout."""
    snippet = {
        "tools": {
            "engraphis": {
                "package": "engraphis-prime-agent",
                "import": "engraphis_prime_agent",
                "entry": "PrimeAgentFleet",
            }
        }
    }
    if as_json:
        _print_json(snippet)
    else:
        # Human-readable view of the same snippet.
        print("# Drop this into your prime-agent config (e.g. tools section):")
        print(json.dumps(snippet["tools"], indent=2, sort_keys=True))
    return 0


def _install(uninstall: bool = False, config_path: str | None = None) -> int:
    """Delegate to the top-level ``scripts/install_prime_agent.py``.

    ``runpy.run_path`` is the standard-library way to execute a script by
    path while sharing the current process — preferred over a subprocess so
    the installer can validate the file path next to the package without a
    hard dependency on the script being on PATH.
    """
    script = Path(__file__).resolve().parents[4] / "scripts" / "install_prime_agent.py"
    if not script.exists():
        message = f"installer not found at {script}"
        print(f"error: {message}", file=sys.stderr)
        _print_json({"ok": False, "error": message, "action": "install" if not uninstall else "uninstall"})
        return EXIT_INSTALL_FAILED

    # The installer reads sys.argv, so we set it before invoking and restore
    # on the way out (success or failure) so callers see a clean process.
    saved_argv = sys.argv
    saved_env = os.environ.get("PRIME_AGENT_CONFIG_PATH")
    argv: list[str] = ["install_prime_agent.py"]
    if uninstall:
        argv.append("--uninstall")
    if config_path:
        argv.extend(["--config-path", config_path])
        os.environ["PRIME_AGENT_CONFIG_PATH"] = config_path
    sys.argv = argv
    try:
        runpy.run_path(str(script), run_name="__main__")
        return 0
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        if code != 0:
            print(
                f"error: installer exited with status {code}",
                file=sys.stderr,
            )
        return code
    except Exception as exc:  # noqa: BLE001 — surface to user
        print(f"error: installer raised {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_INSTALL_FAILED
    finally:
        sys.argv = saved_argv
        if config_path is not None:
            if saved_env is None:
                os.environ.pop("PRIME_AGENT_CONFIG_PATH", None)
            else:
                os.environ["PRIME_AGENT_CONFIG_PATH"] = saved_env


def _version() -> int:
    """Print the package version (single source of truth: ``__version__``)."""
    from . import __version__

    print(__version__)
    return 0


def _add_json_flag(parser: argparse.ArgumentParser) -> None:
    """Add ``--json``/``--no-json`` to a subcommand.

    JSON is the default and matches the historical behavior; the flag exists
    so wrapper scripts can be explicit, and so users can request a
    human-readable view with ``--no-json`` where it makes sense.
    """
    parser.add_argument(
        "--json",
        action=argparse.BooleanOptionalAction,
        default=True,
        dest="as_json",
        help="Emit machine-readable JSON (default: true; use --no-json for text).",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="engraphis-prime-agent",
        description=(
            "Engraphis Smart MCP integration for PrimeIntellect's prime-agent. "
            "Use one of the subcommands below; --json is the default output "
            "format for all subcommands."
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    check_parser = sub.add_parser(
        "check",
        help="Start engraphis-mcp once and report status.",
        description=(
            "Boot the configured engraphis-mcp console script, list its tools, "
            "and print a JSON status. Exit codes: 0 ok, 1 incompatible tool "
            "surface, 2 binary missing, 3 transport error."
        ),
    )
    _add_json_flag(check_parser)

    status_parser = sub.add_parser(
        "status",
        help="Boot the 8-agent fleet and print session/agent state.",
        description=(
            "Construct the 8-agent PrimeAgentFleet, start an MCP session, "
            "and print per-agent state. Fails with an actionable error if "
            "engraphis-mcp is not installed."
        ),
    )
    _add_json_flag(status_parser)

    register_parser = sub.add_parser(
        "register",
        help="Print the prime-agent tool registration snippet.",
        description=(
            "Print the JSON snippet that registers the engraphis tool with "
            "a prime-agent installation. Pipe the output into your config."
        ),
    )
    _add_json_flag(register_parser)

    install_parser = sub.add_parser(
        "install",
        help="Idempotently install the integration into prime-agent.",
        description=(
            "Idempotently register the integration with prime-agent by writing "
            "the tools.engraphis entry into its config file. Use --uninstall to "
            "remove the entry. --config-path overrides the target file (the "
            "PRIME_AGENT_CONFIG_PATH env var is also respected)."
        ),
    )
    install_parser.add_argument(
        "--uninstall",
        action="store_true",
        help="Remove the engraphis entry from the prime-agent config instead of installing it.",
    )
    install_parser.add_argument(
        "--config-path",
        default=None,
        metavar="PATH",
        help="Override the prime-agent config file path (defaults to $PRIME_AGENT_CONFIG_PATH or ~/.config/prime-agent/config.json).",
    )

    version_parser = sub.add_parser(
        "version",
        help="Print the engraphis-prime-agent version and exit.",
        description="Print the installed engraphis-prime-agent __version__ and exit.",
    )
    # The version subcommand prints a single line; --json is a no-op there
    # but kept for symmetry with the other subcommands.
    _add_json_flag(version_parser)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    as_json = bool(getattr(args, "as_json", True))
    if args.cmd == "check":
        return _check(as_json=as_json)
    if args.cmd == "status":
        return _status(as_json=as_json)
    if args.cmd == "register":
        return _register(as_json=as_json)
    if args.cmd == "install":
        return _install(
            uninstall=bool(getattr(args, "uninstall", False)),
            config_path=getattr(args, "config_path", None),
        )
    if args.cmd == "version":
        return _version()
    parser.error(f"unknown subcommand: {args.cmd}")
    return 64  # unreachable, but keeps type-checkers happy


if __name__ == "__main__":
    sys.exit(main())
