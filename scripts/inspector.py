#!/usr/bin/env python3
"""Launch the Engraphis Memory Inspector (v2 UI) — http://127.0.0.1:8710 by default.

    python -m scripts.inspector                     # uses ENGRAPHIS_DB_PATH
    python -m scripts.inspector --db engraphis.db --port 8710

Optional auth: set ENGRAPHIS_API_TOKEN and every /api/* call requires
``Authorization: Bearer <token>`` (the page prompts for it once).
"""
from __future__ import annotations

import argparse
import os


def main() -> None:
    ap = argparse.ArgumentParser(description="Run the Engraphis Memory Inspector.")
    ap.add_argument("--db", default=None, help="v2 database path (default: ENGRAPHIS_DB_PATH).")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.environ.get("ENGRAPHIS_INSPECTOR_PORT", "8710")))
    args = ap.parse_args()

    if args.db:
        os.environ["ENGRAPHIS_DB_PATH"] = args.db
        # settings is a module-level singleton; re-import after the env change
        import importlib

        import engraphis.config
        importlib.reload(engraphis.config)

    import uvicorn

    from engraphis.inspector import create_app
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
