"""Concurrent writers must not defeat conflict resolution.

The resolve path is read-decide-write (vector-index neighbor lookup → deterministic
resolve → insert). Before the engine-level write lock, two concurrent remembers of the
same content could BOTH observe "no neighbor" and BOTH resolve ADD, storing duplicates —
the store's per-statement serialization cannot span the sequence. These tests hammer the
path with a thread barrier to make the interleaving real, not theoretical.
"""
import threading
from concurrent.futures import ThreadPoolExecutor

from engraphis.core.engine import MemoryEngine
from engraphis.core.interfaces import SearchFilter

THREADS = 8


def test_concurrent_identical_remembers_yield_one_add():
    eng = MemoryEngine.create(":memory:")            # offline defaults
    wid = eng.store.get_or_create_workspace("w")
    barrier = threading.Barrier(THREADS)

    def write(_):
        barrier.wait()
        return eng.remember_with_resolution(
            "The deploy pipeline uses GitHub Actions and pushes to AWS ECS.",
            workspace_id=wid, title="deploy")

    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        results = list(pool.map(write, range(THREADS)))

    ops = sorted(r["op"] for r in results)
    assert ops.count("add") == 1, f"exactly one writer may ADD, got {ops}"
    assert ops.count("noop") == THREADS - 1
    # And every NOOP points at the single live record.
    live_id = next(r["id"] for r in results if r["op"] == "add")
    assert all(r["id"] == live_id for r in results)
    recs = eng.store.list_memories(SearchFilter(workspace_id=wid), limit=50)
    assert len(recs) == 1


def test_concurrent_contradictions_never_duplicate_live_facts():
    """Same subject, contradicting values, N writers: whatever interleaving wins, at
    most ONE record may stay live — duplicates mean the resolve decision raced."""
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    eng.remember("The API rate limit is 100 requests per minute.",
                 workspace_id=wid, title="rate limit")
    barrier = threading.Barrier(THREADS)

    def write(i):
        barrier.wait()
        return eng.remember_with_resolution(
            f"The API rate limit is {200 + i} requests per minute.",
            workspace_id=wid, title="rate limit")

    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        list(pool.map(write, range(THREADS)))

    live = eng.store.list_memories(SearchFilter(workspace_id=wid), limit=50)
    assert len(live) == 1, (
        f"expected exactly one live fact after concurrent supersession, got "
        f"{[(r.id, r.content) for r in live]}")


def test_sequential_behaviour_unchanged_by_the_lock():
    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    first = eng.remember_with_resolution("Standup is at 09:30.", workspace_id=wid)
    dup = eng.remember_with_resolution("Standup is at 09:30.", workspace_id=wid)
    assert first["op"] == "add" and dup["op"] == "noop" and dup["id"] == first["id"]
