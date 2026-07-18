#!/usr/bin/env python3
"""Launch the Engraphis WebUI (Inspector + dashboard).

    engraphis-dashboard                        # opens http://127.0.0.1:8700
    engraphis-dashboard --no-open              # starts without opening the browser
    engraphis-dashboard --port 9000            # custom port
    engraphis-dashboard --install-shortcuts    # Desktop + Start Menu icons

The WebUI serves the dashboard single-page app at ``/`` over the v2 engine's
``/api/*`` route set (plus ``/mcp`` when the optional mcp extra is installed).
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import webbrowser


_DEFAULT_EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


def _embed_model_from_environment() -> str:
    """Use the production model by default, while preserving an explicit offline opt-out."""
    configured = os.environ.get("ENGRAPHIS_EMBED_MODEL")
    return _DEFAULT_EMBED_MODEL if configured is None else configured.strip()


def _run_shortcut_install(silent: bool = False, icon: str = "") -> None:
    cmd = [sys.executable, "-m", "scripts.install_shortcuts"]
    if silent:
        cmd.append("--silent")
    if icon:
        cmd.extend(["--icon", icon])
    import subprocess
    subprocess.run(cmd, check=False)


def _port(value: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("port must be an integer from 1 to 65535") from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be from 1 to 65535")
    return port


def _startup_error(exc: BaseException, db: str) -> str:
    if isinstance(exc, (ImportError, ModuleNotFoundError)):
        return ("The server extra is required: pip install \"engraphis[server]\""
                " (needs Python 3.10+)")
    if isinstance(exc, (sqlite3.Error, OSError)):
        return (
            "Could not open the Engraphis database at %s. Check that the path is a "
            "writable SQLite file, then run engraphis-init --check." % db
        )
    if isinstance(exc, RuntimeError):
        return str(exc)
    return "Dashboard initialization failed. Run engraphis-init --check for diagnostics."


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Start the Engraphis WebUI.")
    ap.add_argument("--host", default=os.environ.get("ENGRAPHIS_HOST", "127.0.0.1"),
                    help="Bind host (default: $ENGRAPHIS_HOST, else 127.0.0.1).")
    # Prefer the platform-injected ``PORT`` (Railway/Fly/Heroku set it and route + health-
    # check to exactly that port). Falling back to ``ENGRAPHIS_PORT`` then 8700 keeps local
    # and docker-compose runs unchanged. Binding a fixed 8700 while the platform expected
    # ``$PORT`` was half of the 2026-07-16 Railway healthcheck failure.
    ap.add_argument("--port", type=_port,
                    default=(os.environ.get("PORT")
                             or os.environ.get("ENGRAPHIS_PORT", "8700")),
                    help="Bind port (default: $PORT, else $ENGRAPHIS_PORT, else 8700).")
    ap.add_argument("--no-open", action="store_true",
                    help="Do not open the browser on startup.")
    ap.add_argument("--install-shortcuts", action="store_true",
                    help="Install desktop and Start Menu shortcuts, then exit.")
    ap.add_argument("--install-shortcuts-silent", action="store_true",
                    help="Same as --install-shortcuts but non-interactive.")
    ap.add_argument("--icon", default="", help="Icon path for shortcuts.")
    args = ap.parse_args(argv)

    if args.install_shortcuts or args.install_shortcuts_silent:
        _run_shortcut_install(silent=args.install_shortcuts_silent, icon=args.icon)
        return

    os.environ["ENGRAPHIS_EMBED_MODEL"] = _embed_model_from_environment()
    os.environ["ENGRAPHIS_HOST"] = args.host
    os.environ["ENGRAPHIS_PORT"] = str(args.port)

    # netutil (stdlib-only, config-free) keeps this import safe BEFORE the env writes
    # above are re-read by engraphis.config inside uvicorn's import of the app. It maps
    # a wildcard bind (0.0.0.0/::) to loopback and brackets IPv6 for the printed URL.
    db = os.environ.get("ENGRAPHIS_DB_PATH", "the default user-data location")
    try:
        from engraphis.netutil import display_base_url
        url = display_base_url(args.host, args.port)
        # Imported AFTER the env writes above: this snapshot and uvicorn's in-process
        # import of the app see the same values, so the banner reports the RESOLVED DB
        # path (installed builds use a per-user data dir, not "./engraphis.db").
        from engraphis.config import settings
        db = settings.db_path
        import uvicorn
        from engraphis.dashboard_app import app as dashboard_app
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - convert startup failures to UX
        ap.exit(1, "Error: %s\n" % _startup_error(exc, db))

    print(f"Engraphis WebUI - {url}")
    print(f"  Dashboard :  {url}/")
    print(f"  REST API  :  {url}/api")
    print(f"  Database  :  {db}")
    print("  Press Ctrl+C to stop.")
    sys.stdout.flush()

    if not args.no_open:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    # Preserve the actual socket peer. Engraphis validates trusted proxies and consumes
    # the rightmost forwarded hop itself; letting Uvicorn rewrite request.client first
    # destroys that evidence and makes a preseeded X-Forwarded-For spoofable.
    try:
        uvicorn.run(
            dashboard_app, host=args.host, port=args.port, proxy_headers=False,
        )
    except (Exception, SystemExit) as exc:  # noqa: BLE001
        ap.exit(1, "Error: %s\n" % _startup_error(exc, db))


if __name__ == "__main__":
    main()
