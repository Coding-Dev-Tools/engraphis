"""``start_session`` is idempotent per (workspace, repo, agent).

A second start in the same scope must return the *same* active session rather than
opening a second one — two live sessions on one scope means two concurrent writers on
the single-writer SQLite store (the "opens up 2 instances that trample on each other"
bug). ``force_new`` is the deliberate escape hatch, and ending a session lets the next
start open a fresh one.
"""
from engraphis.service import MemoryService


def _svc():
    return MemoryService.create(":memory:")


def test_repeat_start_reuses_active_session():
    svc = _svc()
    a = svc.start_session("w", repo="r", agent="claude-code", goal="first")
    b = svc.start_session("w", repo="r", agent="claude-code", goal="second")
    assert a["reused"] is False
    assert b["reused"] is True
    assert b["session_id"] == a["session_id"]
    # the reused session keeps its original goal, not the second call's
    assert b["goal"] == "first"


def test_force_new_branches_a_fresh_session():
    svc = _svc()
    a = svc.start_session("w", repo="r", agent="claude-code")
    b = svc.start_session("w", repo="r", agent="claude-code", force_new=True)
    assert b["reused"] is False
    assert b["session_id"] != a["session_id"]


def test_different_agent_is_a_different_session():
    svc = _svc()
    a = svc.start_session("w", repo="r", agent="claude-code")
    b = svc.start_session("w", repo="r", agent="cursor")
    assert b["reused"] is False
    assert b["session_id"] != a["session_id"]


def test_different_repo_is_a_different_session():
    svc = _svc()
    a = svc.start_session("w", repo="backend", agent="claude-code")
    b = svc.start_session("w", repo="frontend", agent="claude-code")
    assert b["reused"] is False
    assert b["session_id"] != a["session_id"]


def test_ended_session_is_not_reused():
    svc = _svc()
    a = svc.start_session("w", repo="r", agent="claude-code")
    svc.end_session(a["session_id"], summary="done", outcome="shipped",
                    open_threads=["follow up on X"])
    b = svc.start_session("w", repo="r", agent="claude-code")
    assert b["reused"] is False
    assert b["session_id"] != a["session_id"]
    # and the fresh session bootstraps from the one we just ended
    assert b["bootstrap"].get("outcome") == "shipped"
    assert "follow up on X" in b["bootstrap"].get("open_threads", [])
