"""Ensure the repo root is importable when running ``python -m pytest`` from anywhere."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The update reminder reaches out to the network to look up the newest release. Keep the
# offline test suite network-inert by default; tests that exercise the feature opt back in
# explicitly via monkeypatch (see tests/test_update_check.py).
os.environ.setdefault("ENGRAPHIS_UPDATE_CHECK", "0")

# Owner machines may configure ENGRAPHIS_EXTRACTOR=llm (via ~/.engraphis/config.env),
# which would make every ingest-path test block on live LLM extraction calls. The unit
# suite is offline-inert by contract (AGENTS.md §1 "primary offline gate"); tests that
# exercise extraction opt back in explicitly via monkeypatch.setenv. setdefault keeps a
# real shell override working, matching how config.env itself defers to the environment.
os.environ.setdefault("ENGRAPHIS_EXTRACTOR", "none")

# The legacy scripts/test_*.py files are HTTP smoke tests (need a running server +
# httpx), not unit tests. Keep pytest focused on the tests/ suite.
collect_ignore_glob = ["scripts/*"]
