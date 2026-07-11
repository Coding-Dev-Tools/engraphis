#!/usr/bin/env python3
"""DEPRECATED launcher — the standalone Engraphis Inspector (:8710) was RETIRED.

Its memory-inspection features (rich analytics + a shareable HTML report, version-chain
word diffs, the offline knowledge graph, and a readiness probe) now live in the single
unified dashboard on :8700. Run that instead:

    python -m scripts.start_dashboard        # or the `engraphis-dashboard` command

This shim remains only so old shortcuts/commands print a clear redirect instead of a
stack trace. The Inspector's thin API layer (``engraphis.inspector.app``) lives on as an
internal library exercised by the test suite; it is no longer a shipped product surface.
The original launcher is archived at
``_archive/engraphis-inspector-retired-20260710/scripts_inspector.py.bak``.
"""
from __future__ import annotations

import sys

_MSG = (
    "\n  The standalone Engraphis Inspector (:8710) has been retired.\n"
    "  Everything it did now lives in the unified dashboard on http://127.0.0.1:8700\n"
    "  (rich analytics + HTML report, version-chain diffs, offline graph, and more).\n\n"
    "  Start it with:\n\n"
    "      python -m scripts.start_dashboard        (or: engraphis-dashboard)\n\n"
)


def main() -> int:
    sys.stderr.write(_MSG)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
