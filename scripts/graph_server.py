"""Launch the optional read-only recall/repo-graph HTTP server."""
from __future__ import annotations

import argparse
import ipaddress
import os

from engraphis.read_only_api import create_read_only_app


def _loopback(host: str) -> bool:
    # An empty host string makes the socket layer bind ALL interfaces, so it is
    # emphatically not loopback; any unparseable hostname is treated as
    # non-loopback too (fail closed: a token is then required).
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="engraphis-graph-server")
    parser.add_argument("--host", default=os.environ.get("ENGRAPHIS_GRAPH_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("ENGRAPHIS_GRAPH_PORT", "8720"))
    )
    args = parser.parse_args(argv)
    token = os.environ.get(
        "ENGRAPHIS_GRAPH_TOKEN", os.environ.get("ENGRAPHIS_API_TOKEN", "")
    )
    if not _loopback(args.host) and not token:
        parser.error(
            "non-loopback graph serving requires ENGRAPHIS_GRAPH_TOKEN "
            "or ENGRAPHIS_API_TOKEN"
        )
    try:
        import uvicorn
    except ImportError as exc:
        raise SystemExit(
            "The server extra is required: pip install \"engraphis[server]\""
        ) from exc
    uvicorn.run(create_read_only_app(token=token), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
