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
import sys
from pathlib import Path
from typing import Optional


def _resolve_inspector_db(cli_db: Optional[str], *, live: bool = False) -> str:
    explicit = cli_db or os.environ.get("ENGRAPHIS_DB_PATH")
    # Live mode: use the canonical store verbatim. The Inspector is an
    # authoritative curator (forget/pin/correct/consolidate are real writes),
    # so it must read AND write the live store that the MCP server reads. Lock
    # contention is no longer a concern now that all MCP clients go through the
    # singleton streamable-http server (:8720) — there is at most one other
    # writer, and the store layer is already WAL + busy_timeout=30.
    if live and explicit and explicit != ":memory:":
        return explicit
    if explicit and (explicit == ":memory:" or ".inspector" in explicit or explicit.endswith(".inspector.db")):
        return explicit
    base_path = Path(explicit or "engraphis.db")
    candidate_name = (
        base_path.name
        if ".inspector" in base_path.name or base_path.name.endswith(".inspector.db")
        else base_path.with_name("engraphis.inspector.db").name
    )
    candidate = base_path.with_name(candidate_name) if base_path.name != candidate_name else base_path
    if not candidate.exists() and base_path.exists() and base_path.name not in {candidate.name, ":memory:"}:
        import shutil
        shutil.copy2(base_path, candidate)
    return str(candidate)


def main() -> None:
    ap = argparse.ArgumentParser(description="Run the Engraphis Memory Inspector.")
    ap.add_argument("--db", default=None, help="v2 database path (default: ENGRAPHIS_DB_PATH).")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.environ.get("ENGRAPHIS_INSPECTOR_PORT", "8710")))
    ap.add_argument("--live", action="store_true",
                    help="Use ENGRAPHIS_DB_PATH verbatim (the live store) instead of an "
                         "isolated enggraphis.inspector.db snapshot. Set ENGRAPHIS_INSPECTOR_LIVE=1 "
                         "to enable via env. Recommended when the MCP singleton server is running.")
    args = ap.parse_args()

    live = args.live or os.environ.get("ENGRAPHIS_INSPECTOR_LIVE", "").lower() in {"1", "true", "yes"}
    resolved_db = _resolve_inspector_db(args.db, live=live)
    if live:
        print("[engraphis] inspector: live-store mode (no snapshot isolation)", file=sys.stderr)
    os.environ["ENGRAPHIS_DB_PATH"] = resolved_db
    import importlib

    import engraphis.config
    importlib.reload(engraphis.config)

    # Ship-safety: warn (don't block) if this instance is configured in a way that's fine
    # for local use but unsafe for selling — the dev signing key or a placeholder checkout.
    from engraphis import licensing
    for warning in licensing.production_warnings():
        print("[engraphis] ship-safety: %s" % warning, file=sys.stderr)

    import uvicorn

    from engraphis.inspector import create_app
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
