"""Ensure the repo root is importable when running ``python -m pytest`` from anywhere."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The legacy scripts/test_*.py files are HTTP smoke tests (need a running server +
# httpx), not unit tests. Keep pytest focused on the tests/ suite.
collect_ignore_glob = ["scripts/*"]
