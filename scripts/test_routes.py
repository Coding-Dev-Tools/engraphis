"""Smoke test the canonical v2 HTTP API against a running local service.

Usage::

    python -m scripts.start_server  # or: engraphis-dashboard --no-open
    python -m scripts.test_routes

The smoke fixture is retired through the normal temporal ``/api/forget`` path at
the end; it never calls legacy v1 routes or deletes a workspace.
"""
from __future__ import annotations

import time

import httpx

from engraphis.config import settings

BASE = settings.base_url.rstrip("/")
PASS = 0
FAIL = 0


def _ok(name: str) -> None:
    global PASS
    PASS += 1
    print(f"  [ok] {name}")


def _fail(name: str, err: Exception | str) -> None:
    global FAIL
    FAIL += 1
    print(f"  [FAIL] {name}: {err}")


def _expect(response: httpx.Response) -> dict:
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict):
        raise AssertionError("expected an object response")
    return body


def run() -> None:
    print(f"Testing Engraphis v2 at {BASE}")
    print()
    workspace = f"smoke-{int(time.time())}"
    memory_id = ""

    with httpx.Client(base_url=BASE, timeout=30) as client:
        try:
            health = _expect(client.get("/api/health"))
            assert health["engine"] == "v2"
            _ok("health (v2)")
        except Exception as exc:  # noqa: BLE001 - CLI should report a useful failed check
            _fail("health", exc)
            return

        try:
            stored = _expect(client.post("/api/remember", json={
                "content": "The v2 HTTP smoke test keeps temporary memories scoped.",
                "workspace": workspace,
                "title": "v2 smoke fixture",
                "source": "scripts.test_routes",
                "dedupe": False,
            }))
            memory_id = str(stored["id"])
            _ok("remember")

            recalled = _expect(client.get("/api/recall", params={
                "q": "what does the HTTP smoke test keep", "workspace": workspace, "k": 5,
            }))
            assert any(memory["id"] == memory_id for memory in recalled["memories"])
            _ok("recall")

            listed = _expect(client.get("/api/memories", params={"workspace": workspace}))
            assert any(memory["id"] == memory_id for memory in listed["memories"])
            _ok("list memories")

            stats = _expect(client.get("/api/stats", params={"workspace": workspace}))
            assert int(stats.get("memories", 0)) >= 1
            _ok("stats")
        except Exception as exc:  # noqa: BLE001 - continue to cleanup and summarize failures
            _fail("v2 API", exc)
        finally:
            if memory_id:
                try:
                    _expect(client.post("/api/forget", json={
                        "id": memory_id,
                        "workspace": workspace,
                        "reason": "v2 HTTP smoke cleanup",
                    }))
                    _ok("forget smoke fixture")
                except Exception as exc:  # noqa: BLE001 - cleanup failure must be visible
                    _fail("forget smoke fixture", exc)

    print()
    print(f"Results: {PASS} passed, {FAIL} failed")
    if FAIL:
        raise SystemExit(1)


if __name__ == "__main__":
    run()
