#!/usr/bin/env python3
"""Deprecated launcher for the retired standalone Inspector (:8710).

Local memory inspection now lives in the unified dashboard on :8700. Paid analytics,
automatic dreaming/consolidation, cloud sync, and Team administration are hosted cloud
services and are not implemented by this legacy launcher.
"""
from __future__ import annotations

import argparse
import sys

_MSG = (
    "\n  The standalone Engraphis Inspector (:8710) has been retired.\n"
    "  Local inspection now lives in the unified dashboard on http://127.0.0.1:8700.\n"
    "  Team administration, paid analytics, cloud sync, and automatic maintenance\n"
    "  are hosted Engraphis Cloud services.\n\n"
    "  Start the local dashboard with:\n\n"
    "      python -m scripts.start_dashboard        (or: engraphis-dashboard)\n\n"
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="engraphis-inspector",
        description="Retired launcher; use engraphis-dashboard instead.",
    )
    parser.parse_args(argv)
    sys.stderr.write(_MSG)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
