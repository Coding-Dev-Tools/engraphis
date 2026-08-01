"""Explicit launcher for the internal Engraphis v1 compatibility reference.

The public ``engraphis-server`` and ``engraphis-dashboard`` commands run v2.  This
module exists only for controlled migrations and historical API checks, and requires
an independently named database so it cannot write the active v2 store.
"""
from __future__ import annotations

import argparse
import os


def _port(value: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("port must be an integer from 1 to 65535") from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be from 1 to 65535")
    return port


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.legacy_reference",
        description="Run the internal v1 compatibility reference against a separate database.",
    )
    parser.add_argument(
        "--legacy-db",
        required=True,
        help="Required SQLite path for v1 only. It must not be ENGRAPHIS_DB_PATH.",
    )
    parser.add_argument("--host", default=os.environ.get("ENGRAPHIS_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port",
        type=_port,
        default=os.environ.get("PORT") or os.environ.get("ENGRAPHIS_PORT", "8700"),
    )
    args = parser.parse_args(argv)

    try:
        import uvicorn
        from engraphis.app import create_legacy_reference_app

        reference_app = create_legacy_reference_app(legacy_db_path=args.legacy_db)
    except (ImportError, ModuleNotFoundError):
        parser.exit(1, "Error: the server extra is required: pip install 'engraphis[server]'\n")
    except RuntimeError as exc:
        parser.exit(2, "Error: %s\n" % exc)

    print("Engraphis v1 compatibility reference (separate database only)")
    print("  Legacy database: %s" % args.legacy_db)
    uvicorn.run(reference_app, host=args.host, port=args.port, proxy_headers=False)


if __name__ == "__main__":
    main()
