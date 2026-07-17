#!/usr/bin/env python3
"""Launch the Engraphis WebUI (Inspector + dashboard).

    engraphis-dashboard                        # opens http://127.0.0.1:8700
    engraphis-dashboard --no-open              # starts without opening the browser
    engraphis-dashboard --port 9000            # custom port
    engraphis-dashboard --install-shortcuts    # Desktop + Start Menu icons

The WebUI serves the Memory Inspector at ``/`` and the legacy dashboard at
``/legacy``, both over the same v2 engine and ``/api/*`` route set.
"""
from __future__ import annotations

import argparse
import os
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


def main() -> None:
    ap = argparse.ArgumentParser(description="Start the Engraphis WebUI.")
    ap.add_argument("--host", default=os.environ.get("ENGRAPHIS_HOST", "127.0.0.1"))
    # Prefer the platform-injected ``PORT`` (Railway/Fly/Heroku set it and route + health-
    # check to exactly that port). Falling back to ``ENGRAPHIS_PORT`` then 8700 keeps local
    # and docker-compose runs unchanged. Binding a fixed 8700 while the platform expected
    # ``$PORT`` was half of the 2026-07-16 Railway healthcheck failure.
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("PORT")
                                or os.environ.get("ENGRAPHIS_PORT", "8700")))
    ap.add_argument("--no-open", action="store_true",
                    help="Do not open the browser on startup.")
    ap.add_argument("--install-shortcuts", action="store_true",
                    help="Install desktop and Start Menu shortcuts, then exit.")
    ap.add_argument("--install-shortcuts-silent", action="store_true",
                    help="Same as --install-shortcuts but non-interactive.")
    ap.add_argument("--icon", default="", help="Icon path for shortcuts.")
    args = ap.parse_args()

    if args.install_shortcuts or args.install_shortcuts_silent:
        _run_shortcut_install(silent=args.install_shortcuts_silent, icon=args.icon)
        return

    os.environ["ENGRAPHIS_EMBED_MODEL"] = _embed_model_from_environment()
    os.environ["ENGRAPHIS_HOST"] = args.host
    os.environ["ENGRAPHIS_PORT"] = str(args.port)

    # netutil (stdlib-only, config-free) keeps this import safe BEFORE the env writes
    # above are re-read by engraphis.config inside uvicorn's import of the app. It maps
    # a wildcard bind (0.0.0.0/::) to loopback and brackets IPv6 for the printed URL.
    from engraphis.netutil import display_base_url
    url = display_base_url(args.host, args.port)
    db = os.environ.get("ENGRAPHIS_DB_PATH", "./engraphis.db")
    print(f"Engraphis WebUI — {url}")
    print(f"  Inspector :  {url}/")
    print(f"  Dashboard :  {url}/legacy")
    print(f"  Database  :  {db}")
    print("  Press Ctrl+C to stop.")
    sys.stdout.flush()

    if not args.no_open:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    import uvicorn
    # Trust the fronting TLS proxy's forwarded headers so the session cookie's Secure
    # flag is set correctly behind Railway/Fly/nginx (see start_server.py).
    uvicorn.run("engraphis.dashboard_app:app", host=args.host, port=args.port,
                proxy_headers=True,
                forwarded_allow_ips=os.environ.get("ENGRAPHIS_FORWARDED_ALLOW_IPS", "127.0.0.1"))


if __name__ == "__main__":
    main()
