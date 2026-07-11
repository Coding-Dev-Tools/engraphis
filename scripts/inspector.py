#!/usr/bin/env python3
"""Redirect port 8710 → 8700 — the Inspector lives on the main dashboard now.

    engraphis-inspector           # redirect :8710 → :8700

The Memory Inspector was merged into the main Engraphis WebUI. Old bookmarks and
shortcuts pointing at :8710 are forwarded to :8700.
"""
from __future__ import annotations

import argparse
import os
import sys
import webbrowser


def main() -> None:
    ap = argparse.ArgumentParser(description="Start the Engraphis redirect (8710 → 8700).")
    ap.add_argument("--host", default=os.environ.get("ENGRAPHIS_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("ENGRAPHIS_INSPECTOR_PORT", "8710")))
    ap.add_argument("--no-open", action="store_true",
                    help="Do not open the browser on startup.")
    args = ap.parse_args()

    url = f"http://{args.host}:{args.port}"
    target = f"http://{args.host}:8700"
    print(f"Engraphis redirect — {url} → {target}")
    print("  The Memory Inspector and dashboard now live together on :8700.")
    print("  Press Ctrl+C to stop.")
    sys.stdout.flush()

    if not args.no_open:
        try:
            webbrowser.open(target)
        except Exception:
            pass

    import uvicorn
    uvicorn.run("engraphis.redirector:app", host=args.host, port=args.port,
                log_level="warning")


if __name__ == "__main__":
    main()
