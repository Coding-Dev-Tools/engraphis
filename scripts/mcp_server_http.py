#!/usr/bin/env python3
"""Backward-compatible module launcher for ``engraphis-mcp-http``.

The supported command lives in :mod:`engraphis.mcp_http_cli`. Keeping this module
lets existing source-checkout invocations continue to work without naming or
endorsing a particular MCP client.
"""
from engraphis.mcp_http_cli import main


if __name__ == "__main__":
    main()
