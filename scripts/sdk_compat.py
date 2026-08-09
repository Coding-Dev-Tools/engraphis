"""Helper — talk to a local Engraphis server from Python.

Engraphis exposes a self-describing REST API (schema at ``/openapi.json``). This helper
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
    """Quick demo using httpx directly against the current local REST API."""
    import httpx

    url = base_url()
    print(f"Engraphis server: {url}")
    with httpx.Client(base_url=url, timeout=60) as client:
        health_response = client.get("/api/health")
        health_response.raise_for_status()
        print("Health:", health_response.json().get("status"))

        remember_response = client.post(
            "/api/remember",
            json={
                "content": "The user prefers dark mode.",
                "workspace": "default",
                "subject_key": "demo-user",
                "claim_kind": "theme-preference",
            },
        )
        remember_response.raise_for_status()
        stored = remember_response.json()
        memory_id = stored.get("id")
        if not memory_id:
            raise RuntimeError("Engraphis did not return a memory id")
        print("Stored:", memory_id)

        recall_response = client.get(
            "/api/recall",
            params={
                "workspace": "default",
                "q": "what theme does the user prefer?",
                "k": 3,
            },
        )
        recall_response.raise_for_status()
        memories = recall_response.json().get("memories") or []
        recalled = next(
            (
                str(memory.get("content") or memory.get("summary") or "")
                for memory in memories
                if isinstance(memory, dict)
            ),
            "",
        )
        if not recalled:
            raise RuntimeError("Engraphis recall returned no demo memory")
        print("Recall:", recalled[:200])


if __name__ == "__main__":
    demo()
