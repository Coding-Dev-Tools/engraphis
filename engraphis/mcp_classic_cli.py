"""Console entry for ``engraphis-mcp-classic``.

The normal ``engraphis-mcp`` entry point is the compact Smart MCP gateway. This
launcher preserves the historical direct-tool surface for clients that pin names.
"""
from __future__ import annotations

import argparse

from engraphis.mcp_cli import _dependency_error


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        prog="engraphis-mcp-classic",
        description="Run the legacy Engraphis MCP server over stdio with all direct tools.",
        epilog=(
            "Most agents should use engraphis-mcp instead: its Smart gateway discovers "
            "advanced actions automatically."
        ),
    )
    ap.parse_args(argv)
    error = _dependency_error()
    if error:
        raise SystemExit(error)

    # Import after argparse so --help works without the optional MCP dependency.
    from engraphis.mcp_server import _eager_exact_backend_check, classic_mcp

    _eager_exact_backend_check()
    classic_mcp.run()


if __name__ == "__main__":
    main()
