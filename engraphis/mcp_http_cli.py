"""Console entry for a local loopback MCP-over-HTTP server.

This is intentionally a generic MCP transport, not an integration with any particular
agent host. Remote MCP access belongs behind the authenticated dashboard ``/mcp``
mount; a standalone FastMCP transport has no Engraphis authentication middleware.
"""
from __future__ import annotations

import argparse
import importlib.util
import ipaddress
import os
import sys

_TRANSPORTS = ("streamable-http", "sse")


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


def _loopback_host(value: str) -> str:
    host = value.strip()
    # Accept 'localhost' as a synonym for 127.0.0.1 — standard network tool behavior.
    if host.lower() == "localhost":
        return "127.0.0.1"
    try:
        if ipaddress.ip_address(host).is_loopback:
            return host
    except ValueError:
        pass
    raise argparse.ArgumentTypeError(
        "standalone MCP-over-HTTP accepts loopback hosts only; use the authenticated "
        "dashboard /mcp endpoint for remote access"
    )


def _transport_security(host: str, port: int):
    """Build the SDK's Host/Origin allowlist for the address this launcher binds."""
    from mcp.server.transport_security import TransportSecuritySettings

    address = ipaddress.ip_address(host)
    authority = f"[{address.compressed}]" if address.version == 6 else address.compressed
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[authority, f"{authority}:{port}"],
        allowed_origins=[f"http://{authority}", f"http://{authority}:{port}"],
    )


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        prog="engraphis-mcp-http",
        description="Run a loopback-only Engraphis MCP server over HTTP.",
        epilog=(
            "Use the authenticated dashboard /mcp endpoint for remote clients. "
            "Configuration also honors ENGRAPHIS_DB_PATH and the normal .env settings."
        ),
    )
    ap.add_argument(
        "--host",
        type=_loopback_host,
        default=os.environ.get("ENGRAPHIS_HTTP_HOST", "127.0.0.1"),
        help="loopback address to bind (default: ENGRAPHIS_HTTP_HOST or 127.0.0.1)",
    )
    ap.add_argument(
        "--port",
        type=_port,
        default=os.environ.get("ENGRAPHIS_HTTP_PORT", "8711"),
        help="TCP port to bind (default: ENGRAPHIS_HTTP_PORT or 8711)",
    )
    ap.add_argument(
        "--transport",
        choices=_TRANSPORTS,
        default=os.environ.get("ENGRAPHIS_HTTP_TRANSPORT", "streamable-http"),
        help="MCP transport (default: ENGRAPHIS_HTTP_TRANSPORT or streamable-http)",
    )
    ap.add_argument(
        "--classic",
        action="store_true",
        help=(
            "serve the legacy 34 direct-tool surface; normal use defaults to the compact "
            "Smart gateway"
        ),
    )
    args = ap.parse_args(argv)
    if args.transport not in _TRANSPORTS:
        ap.error("ENGRAPHIS_HTTP_TRANSPORT must be streamable-http or sse")

    error = _dependency_error()
    if error:
        raise SystemExit(error)

    # Import only after --help and dependency validation: FastMCP registers tools at
    # module import time, so importing it eagerly would make even help unusable.
    from engraphis.mcp_server import mcp

    # The eager exact-backend check may be absent from test mocks that replace
    # engraphis.mcp_server with a minimal stand-in; fall back to a no-op so
    # those tests stay green while production callers always run the check.
    try:
        from engraphis.mcp_server import _eager_exact_backend_check
    except ImportError:
        _eager_exact_backend_check = lambda: None  # noqa: E731

    server = mcp
    if args.classic:
        from engraphis.mcp_server import classic_mcp

        server = classic_mcp
    server.settings.host = args.host
    server.settings.port = args.port
    server.settings.transport_security = _transport_security(args.host, args.port)
    _eager_exact_backend_check()
    server.run(transport=args.transport)


if __name__ == "__main__":
    main()
