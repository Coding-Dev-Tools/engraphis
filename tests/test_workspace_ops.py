"""Backend tests for merge_workspaces() and reorder_memories() — two auth-reachable,
multi-table SQL operations that shipped (commit 7e0795f) without any coverage.

merge_workspaces does direct repo/entity/edge/memory remapping across workspaces;
reorder_memories writes a per-memory sort_order. Both are reachable from the
authenticated dashboard API, so a silent regression here corrupts or leaks data.
"""
import pytest

from engraphis.service import MemoryService, ValidationError


def _svc():
    return MemoryService.create(":memory:")


def _wsid(svc, name):
    return svc._lookup_workspace(name)


def _mem_ids(svc, name):
    wid = _wsid(svc, name)
    if wid is None:
        return []
    return {r["id"] for r in svc.store.conn.execute(
        "SELECT id FROM memories WHERE workspace_id=?", (wid,))}


# ── merge_workspaces ────────────────────────────────────────────────────────
def test_merge_folds_memories_and_removes_source():
    svc = _svc()
    a1 = svc.remember("Alpha one fact.", workspace="a", scope="workspace")["id"]
    a2 = svc.remember("Alpha two fact.", workspace="a", scope="workspace")["id"]
    b1 = svc.remember("Bravo fact.", workspace="b", scope="workspace")["id"]

    out = svc.merge_workspaces("a", "b")
    assert out["memories_moved"] == 2 and out["target"] == "b"
    # source workspace is gone
    assert _wsid(svc, "a") is None
    # every id is preserved and now lives under target — lossless, nothing dropped
    assert _mem_ids(svc, "b") == {a1, a2, b1}
    # content is untouched
    assert svc.store.get_memory(a1).content == "Alpha one fact."


def test_merge_folds_colliding_repos_without_duplicating():
    svc = _svc()
    svc.remember("A web note.", workspace="a", repo="web", scope="repo")
    svc.remember("B web note.", workspace="b", repo="web", scope="repo")
    svc.merge_workspaces("a", "b")
    wid_b = _wsid(svc, "b")
    repos = [r["name"] for r in svc.store.conn.execute(
        "SELECT name FROM repos WHERE workspace_id=?", (wid_b,))]
    # the two same-named "web" repos fold into one under the target
    assert repos.count("web") == 1
    # both memories survived the fold
    assert len(_mem_ids(svc, "b")) == 2
    # no orphaned repos/memories left pointing at the deleted source workspace
    assert _wsid(svc, "a") is None
    assert svc.store.conn.execute(
        "SELECT COUNT(*) AS n FROM memories WHERE workspace_id IS NULL").fetchone()["n"] == 0


def test_merge_rejects_same_and_missing_workspaces():
    svc = _svc()
    svc.remember("x", workspace="a", scope="workspace")
    with pytest.raises(ValidationError):
        svc.merge_workspaces("a", "a")           # same src/dst
    with pytest.raises(ValidationError):
        svc.merge_workspaces("a", "nope")        # missing target
    with pytest.raises(ValidationError):
        svc.merge_workspaces("ghost", "a")       # missing source


# ── reorder_memories ────────────────────────────────────────────────────────
def test_reorder_persists_sort_order_in_given_order():
    svc = _svc()
    # distinct content + resolve_conflicts=False so the resolver can't collapse them.
    contents = ["Postgres 16 is the primary database.",
                "Deploys run Fridays at noon.",
                "The staging host is eu-west-1."]
    ids = [svc.remember(c, workspace="a", scope="workspace",
                        resolve_conflicts=False)["id"] for c in contents]
    assert len(set(ids)) == 3, "precondition: three distinct memories"
    new_order = [ids[2], ids[0], ids[1]]
    out = svc.reorder_memories(new_order, workspace="a")
    assert out["reordered"] == 3
    got = {r["id"]: r["sort_order"] for r in svc.store.conn.execute(
        "SELECT id, sort_order FROM memories")}
    assert got[new_order[0]] == 0.0
    assert got[new_order[1]] == 1.0
    assert got[new_order[2]] == 2.0


def test_reorder_rejects_empty_foreign_and_oversized():
    svc = _svc()
    a = svc.remember("Alpha workspace note.", workspace="a", scope="workspace",
                     resolve_conflicts=False)["id"]
    foreign = svc.remember("Bravo workspace note.", workspace="b", scope="workspace",
                           resolve_conflicts=False)["id"]
    with pytest.raises(ValidationError):
        svc.reorder_memories([], workspace="a")                 # empty
    with pytest.raises(ValidationError):
        svc.reorder_memories([foreign], workspace="a")          # id owned by workspace b
    with pytest.raises(ValidationError):
        svc.reorder_memories([str(i) for i in range(1001)], workspace="a")  # > 1000
    # the valid id alone still works (control)
    assert svc.reorder_memories([a], workspace="a")["reordered"] == 1
