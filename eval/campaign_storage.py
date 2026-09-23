"""Disposable, pinned local graph service owned by one campaign invocation."""
from __future__ import annotations

from contextlib import contextmanager
import subprocess
import time
import uuid
from typing import Iterator


NEO4J_IMAGE = "neo4j@sha256:5a015e53de1895e7eee1574ae0325cf8c4b89587222778108c594bdd45a474b5"


@contextmanager
def graph_store(enabled: bool) -> Iterator[None]:
    if not enabled:
        yield
        return
    # A port collision fails; an arbitrary already-running database is never
    # adopted. No persistent volume or user database is attached.
    name = f"engraphis-campaign-neo4j-{uuid.uuid4().hex}"
    subprocess.run(["docker", "image", "inspect", NEO4J_IMAGE], check=True, capture_output=True, timeout=30)
    created = False
    try:
        subprocess.run(["docker", "run", "--detach", "--name", name, "--rm", "--memory", "2g", "--cpus", "2",
                        "--publish", "127.0.0.1:17687:7687", "--env", "NEO4J_AUTH=none",
                        "--env", "NEO4J_dbms_usage__report_enabled=false", NEO4J_IMAGE],
                       check=True, capture_output=True, timeout=45)
        created = True
        deadline = time.monotonic() + 60
        while True:
            result = subprocess.run(["docker", "exec", name, "cypher-shell", "RETURN 1"],
                                    capture_output=True, timeout=15, check=False)
            if result.returncode == 0:
                break
            if time.monotonic() >= deadline:
                raise ValueError("disposable graph service did not become ready")
            time.sleep(2)
        yield
    finally:
        if created:
            subprocess.run(["docker", "rm", "--force", name], capture_output=True, timeout=30, check=False)
