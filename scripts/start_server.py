"""Launch the legacy Engraphis reference server with uvicorn."""
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
    ap = argparse.ArgumentParser(
        prog="engraphis-server",
        description="Start the legacy Engraphis reference API server.",
    )
    ap.add_argument("--host", default=os.environ.get("ENGRAPHIS_HOST", "127.0.0.1"))
    ap.add_argument(
        "--port", type=_port,
        default=os.environ.get("PORT") or os.environ.get("ENGRAPHIS_PORT", "8700"),
    )
    ap.add_argument("--reload", action="store_true", help="Reload when source files change.")
    args = ap.parse_args(argv)
    os.environ["ENGRAPHIS_HOST"] = args.host
    os.environ["ENGRAPHIS_PORT"] = str(args.port)

    try:
        import uvicorn
        from engraphis.config import settings
        from engraphis.observability import configure_structured_logging
        if args.reload:
            app_target = "engraphis.app:app"
        else:
            from engraphis.app import app
            app_target = app
        structured_logs = configure_structured_logging()
    except (ImportError, ModuleNotFoundError):
        ap.exit(1, "Error: the server extra is required: pip install \"engraphis[server]\""
                   " (needs Python 3.10+)\n")
    except (Exception, SystemExit):  # noqa: BLE001
        ap.exit(1, "Error: server initialization failed; run engraphis-init --check\n")

    print(f"Engraphis - starting on {args.host}:{args.port}")
    print(f"  Database:     {settings.db_path}")
    print(f"  Embed model:  {settings.embed_model}")
    print(f"  LLM provider: {settings.llm_provider} / {settings.llm_model}")
    print(f"  Loop interval: {settings.loop_interval}s")
    print(f"  SDK base URL: {settings.base_url}")
    print(f"  OpenAPI:      {settings.base_url}/openapi.json")
    print()
    run_options = {
        "host": args.host,
        "port": args.port,
        "reload": args.reload,
        # Keep the socket peer intact; Engraphis validates trusted forwarded headers and
        # the rightmost hop itself (see engraphis.netutil.client_ip).
        "proxy_headers": False,
    }
    if structured_logs:
        # Preserve the redacting formatter installed by the app/launcher.
        run_options["log_config"] = None
    uvicorn.run(app_target, **run_options)


if __name__ == "__main__":
    main()
