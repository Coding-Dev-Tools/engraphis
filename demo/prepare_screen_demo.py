"""Seed a real in-memory Engraphis flow and export the screen-demo payload.

The recording is intentionally short and deterministic at the UI level, but its
claims come from the v1 service facade used by the dashboard: a previous session
is ended, a new one boots from that handoff, a same-subject fact is superseded,
and the resulting recall/why/timeline/inspect responses are exported for the
visual layer.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from engraphis.service import MemoryService


WORKSPACE = "default"
REPO = "engraphis"
AGENT = "demo-agent"


def build_payload() -> dict:
    svc = MemoryService.create(":memory:", embed_model="")

    seeded = svc.start_session(
        WORKSPACE,
        repo=REPO,
        agent=AGENT,
        goal="Seed the continuity story for the screen demo",
    )
    svc.end_session(
        seeded["session_id"],
        summary=(
            "v2 is the current scoped, bi-temporal architecture: build new capability "
            "in engraphis/core and engraphis/backends. The dashboard exposes recall "
            "and history."
        ),
        outcome="Seeded the handoff for the next agent session.",
        open_threads=["Show why the architecture context was retrieved."],
    )

    session = svc.start_session(
        WORKSPACE,
        repo=REPO,
        agent=AGENT,
        goal="Record the 56-second memory continuity demo",
    )
    assert session["bootstrap"]["summary"], "new session did not receive a handoff"

    architecture = svc.remember(
        (
            "New Engraphis capability belongs in engraphis/core and engraphis/backends; "
            "engraphis/app.py is the v1 legacy reference server."
        ),
        workspace=WORKSPACE,
        repo=REPO,
        session_id=session["session_id"],
        title="Where to build",
        importance=0.95,
        source="demo-seed",
        kind="demo_fixture",
    )

    old_endpoint = svc.remember(
        "The screen demo records against the standard dashboard port 8700.",
        workspace=WORKSPACE,
        repo=REPO,
        session_id=session["session_id"],
        title="Demo configuration",
        importance=0.80,
        source="demo-seed",
        kind="demo_fixture",
    )
    current_endpoint = svc.remember(
        (
            "The screen demo records against port 8790 so it does not collide with "
            "a developer dashboard."
        ),
        workspace=WORKSPACE,
        repo=REPO,
        session_id=session["session_id"],
        title="Demo configuration",
        importance=0.90,
        source="demo-seed",
        kind="demo_fixture",
    )
    assert current_endpoint["op"] == "invalidate", current_endpoint

    recall = svc.recall(
        "where should new Engraphis capability live?",
        workspace=WORKSPACE,
        repo=REPO,
        k=5,
        reinforce=False,
    )
    assert recall["count"] >= 1, "architecture context was not recallable"
    recalled = next(
        (memory for memory in recall["memories"] if memory["id"] == architecture["id"]),
        None,
    )
    assert recalled is not None, recall

    why = svc.why("demo configuration", workspace=WORKSPACE, repo=REPO)
    timeline = svc.timeline("demo configuration", workspace=WORKSPACE, repo=REPO)
    inspected = svc.inspect(
        current_endpoint["id"], workspace=WORKSPACE, repo=REPO
    )
    history = timeline["history"]
    past = next(
        (item for item in history if item["valid_to"] is not None or item["expired_at"] is not None),
        None,
    )
    live = next(
        (item for item in history if item["valid_to"] is None and item["expired_at"] is None),
        None,
    )
    assert len(why["supersedes"]) == 1, why
    assert why["supersedes"][0]["id"] == old_endpoint["id"], why
    assert len(history) == 2, timeline
    assert len(inspected["chain"]) == 2, inspected
    assert past is not None, timeline
    assert live is not None, timeline

    return {
        "workspace": WORKSPACE,
        "repo": REPO,
        "session": session,
        "recall": {
            "query": recall["query"],
            "memory": recalled,
        },
        "why": {
            "current": why["answer"][0],
            "supersedes": why["supersedes"],
        },
        "timeline": [past, live],
        "inspection": {
            "chain": inspected["chain"],
            "events": [event for item in inspected["chain"] for event in item["events"]],
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default="demo/generated/screen_demo_payload.json",
        help="JSON payload path (created at runtime; ignored by git).",
    )
    args = parser.parse_args()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(build_payload(), indent=2), encoding="utf-8")
    print(f"Prepared screen-demo payload: {output}")


if __name__ == "__main__":
    main()
