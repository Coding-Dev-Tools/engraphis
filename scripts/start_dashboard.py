#!/usr/bin/env python3
"""Launch the Engraphis dashboard WebUI (v1 look, v2 engine).

    engraphis-dashboard                        # opens http://127.0.0.1:8700
    engraphis-dashboard --no-open              # starts without opening the browser
    engraphis-dashboard --port 9000            # custom port
    engraphis-dashboard --install-shortcuts    # Desktop + Start Menu icons

The dashboard is the full single-user WebUI: Overview analytics, Memories,
Recall with score breakdowns, Knowledge Graph, Timeline, consolidation, audit
trail, Chat, Import, and license management — all over the v2 engine.
"""
from __future__ import annotations

import argparse
import os
import sys
import webbrowser


def _run_shortcut_install(silent: bool = False, icon: str = "") -> None:
    """Run the shortcut installer via subprocess so it works from pip entry points."""
    cmd = [sys.executable, "-m", "scripts.install_shortcuts"]
    if silent:
        cmd.append("--silent")
    if icon:
        cmd.extend(["--icon", icon])
    import subprocess
    subprocess.run(cmd, check=False)


def main() -> None:
    ap = argparse.ArgumentParser(description="Start the Engraphis dashboard WebUI.")
    ap.add_argument("--host", default=os.environ.get("ENGRAPHIS_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("ENGRAPHIS_PORT", "8700")))
    ap.add_argument("--no-open", action="store_true",
                    help="Do not open the browser on startup.")
    ap.add_argument("--install-shortcuts", action="store_true",
                    help="Install desktop and Start Menu shortcuts, then exit.")
    ap.add_argument("--install-shortcuts-silent", action="store_true",
                    help="Same as --install-shortcuts but non-interactive.")
    ap.add_argument("--icon", default="", help="Icon path for shortcuts.")
    args = ap.parse_args()

    # Shortcut installation path — runs the dedicated installer and exits.
    if args.install_shortcuts or args.install_shortcuts_silent:
        _run_shortcut_install(silent=args.install_shortcuts_silent, icon=args.icon)
        return

    # The data was embedded with 384-dim all-MiniLM. Some environments (e.g. the
    # hermes runtime) export ENGRAPHIS_EMBED_MODEL="" which _env() reads as an empty
    # string and silently falls back to the 256-dim deterministic embedder, breaking
    # semantic search. Restore the real model whenever it is blank.
    os.environ["ENGRAPHIS_EMBED_MODEL"] = (
        os.environ.get("ENGRAPHIS_EMBED_MODEL", "").strip()
        or "sentence-transformers/all-MiniLM-L6-v2")
    os.environ["ENGRAPHIS_HOST"] = args.host
    os.environ["ENGRAPHIS_PORT"] = str(args.port)

    url = f"http://{args.host}:{args.port}"
    db = os.environ.get("ENGRAPHIS_DB_PATH", "./engraphis.db")
    print(f"Engraphis dashboard WebUI — {url}")
    print(f"  Database: {db}")
    print("  Press Ctrl+C to stop.")
    sys.stdout.flush()

    if not args.no_open:
        try:
            webbrowser.open(url)
        except Exception:
            pass  # best-effort; the dashboard still starts

    import uvicorn
    uvicorn.run("engraphis.dashboard_app:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
