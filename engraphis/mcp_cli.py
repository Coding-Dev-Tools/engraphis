"""Console entry for ``engraphis-mcp``.

A thin shim so ``engraphis-mcp --help`` renders WITHOUT the optional ``mcp``
dependency: ``engraphis.mcp_server`` needs FastMCP at import time (tools register by
decorator at module scope), so the dependency gate fires on import — argparse must run
first, the import second. Keeps the actionable install hint either way."""
from __future__ import annotations

import argparse
import importlib.util
import sys


def _dependency_error() -> str:
    if sys.version_info < (3, 10):
        return (
            "The Engraphis MCP server requires Python 3.10 or newer.\n"
            "Create a Python 3.10+ environment, then run: pip install \"engraphis[mcp]\""
        )
    if importlib.util.find_spec("mcp") is None:
        return (
            "The 'mcp' package is required to run the Engraphis MCP server.\n"
            "Install it with: pip install \"engraphis[mcp]\""
        )
    return ""


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        prog="engraphis-mcp",
        description="Run the Engraphis MCP server over stdio - plugs Engraphis into "
                    "Claude Code, Cursor, Cline, Zed, and any MCP-capable client.",
        epilog="Configuration comes from the environment / .env (ENGRAPHIS_DB_PATH, "
               "ENGRAPHIS_WORKSPACES, ...). Generate a client config with: engraphis-init",
    )
    ap.parse_args(argv)
    error = _dependency_error()
    if error:
        raise SystemExit(error)
    # Import AFTER argparse: raises a helpful SystemExit (with the pip install hint)
    # when the optional dependency is absent — see engraphis/mcp_server.py.
    from engraphis.mcp_server import main as run
    run()


if __name__ == "__main__":
    main()
