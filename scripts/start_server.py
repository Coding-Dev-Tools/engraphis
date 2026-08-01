"""Compatibility launcher for the canonical v2 server.

``engraphis-server`` and ``engraphis server`` used to start the incompatible v1
reference application.  Keeping that public entry point created two retention and
recall contracts.  It now starts the same v2 dashboard/API application as
``engraphis-dashboard``, while retaining its historical headless behaviour.
"""
from __future__ import annotations

import sys

from scripts import start_dashboard

# Kept for callers and the lightweight entry-point regression tests.
_port = start_dashboard._port


def main(argv=None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    # The former server command was automation-oriented.  Preserve that quality while
    # converging all public HTTP launches on one v2 service and one decay model.
    if "--no-open" not in args:
        args.append("--no-open")
    start_dashboard.main(args)


if __name__ == "__main__":
    main()
