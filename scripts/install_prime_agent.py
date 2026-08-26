# -*- coding: utf-8 -*-
"""Thin wrapper around the package-distributed installer.

The canonical implementation lives at
``engraphis_prime_agent.installer`` so it ships with the wheel and works
after ``pip install engraphis-prime-agent``. This wrapper remains at the
repo root for source-tree developers who run ``python
scripts/install_prime_agent.py`` directly.

Usage:
    python scripts/install_prime_agent.py
    python scripts/install_prime_agent.py --uninstall
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow importing the package from a source checkout without an editable
# install. The integration package is three directories up from this
# script: scripts/ -> engraphis/ -> integrations/prime_agent/ -> src/.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "integrations" / "prime_agent" / "src"
if _SRC.is_dir():
    sys.path.insert(0, str(_SRC))

from engraphis_prime_agent.installer import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
