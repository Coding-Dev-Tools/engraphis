"""Helper — use the official tinyhumansai SDK pointed at your local server.

If you have the upstream SDK installed (`pip install tinyhumansai`), this shows
how to point it at your local Engraphis server instead of the cloud API.

    export TINYHUMANS_BASE_URL="http://127.0.0.1:8700"
    export TINYHUMANS_TOKEN="local-dev"   # any non-empty string works

Then your existing code using `tinyhumansai` works unchanged:
    import tinyhumansai as api
    client = api.TinyHumansMemoryClient(token="local-dev")
    client.insert_memory(item={...})
    ctx = client.recall_memory(namespace="...", prompt="...")
"""
from __future__ import annotations

import os
import sys

from engraphis.config import settings


def configure_env() -> None:
    """Set env vars so the upstream SDK (if installed) targets this local server."""
    os.environ["TINYHUMANS_BASE_URL"] = settings.base_url
    os.environ.setdefault("TINYHUMANS_TOKEN", "local-dev")


def demo() -> None:
    """Quick demo using httpx directly (no upstream SDK needed)."""
    import httpx

    configure_env()
    print(f"Engraphis server: {settings.base_url}")
    print("If you have the upstream `tinyhumansai` SDK installed,")
    print("set these env vars and your existing code works unchanged:\n")
    print(f'  $env:TINYHUMANS_BASE_URL = "{settings.base_url}"')
    print(f'  $env:TINYHUMANS_TOKEN = "local-dev"\n')

    with httpx.Client(base_url=settings.base_url, timeout=60) as c:
        r = c.get("/memory/health")
        print(f"Health check: {r.json()['data']}")


if __name__ == "__main__":
    demo()
