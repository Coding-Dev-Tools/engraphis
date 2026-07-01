"""Helper — talk to a local Engraphis server from Python.

Engraphis exposes a self-describing REST API (OpenAPI at ``/docs``). This helper
shows the minimal pattern for pointing any HTTP client at your local server. It
has no third-party SDK dependency — just ``httpx``.

    export ENGRAPHIS_BASE_URL="http://127.0.0.1:8700"

    python -m scripts.sdk_compat        # health-check + tiny insert/recall demo
"""
from __future__ import annotations

import os

from engraphis.config import settings


def base_url() -> str:
    """Resolve the server base URL (env override, else config default)."""
    return os.environ.get("ENGRAPHIS_BASE_URL", settings.base_url)


def demo() -> None:
    """Quick demo using httpx directly against the local REST API."""
    import httpx

    url = base_url()
    print(f"Engraphis server: {url}")
    with httpx.Client(base_url=url, timeout=60) as c:
        print("Health:", c.get("/memory/health").json().get("data"))
        c.post("/memory/insert", json={
            "key": "demo-pref",
            "content": "The user prefers dark mode.",
            "namespace": "demo",
        })
        r = c.post("/memory/query", json={
            "namespace": "demo",
            "query": "what theme does the user prefer?",
            "maxChunks": 3,
        })
        print("Recall:", r.json().get("data", {}).get("llmContextMessage", "")[:200])


if __name__ == "__main__":
    demo()
