"""Backend tests for merge_workspaces(), copy_workspace() and reorder_memories() —
auth-reachable, multi-table SQL operations. merge_workspaces does direct repo/entity/
edge/memory remapping across workspaces; copy_workspace clones the same tables under
fresh ids into a new workspace; reorder_memories writes a per-memory sort_order. All
three are reachable from the authenticated dashboard API, so a silent regression here
corrupts or leaks data.
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


# ── create_workspace ─────────────────────────────────────────────────────────
def test_create_makes_empty_shared_workspace():
    svc = _svc()
    out = svc.create_workspace("team-alpha", "shared research notes")
    assert out["created"] and out["workspace"] == "team-alpha"
    # it exists, is empty, and is listed for everyone (shared, not per-user)
    assert _wsid(svc, "team-alpha") is not None
    assert _mem_ids(svc, "team-alpha") == set()
    listed = {w["name"]: w for w in svc.list_workspaces()["workspaces"]}
    assert listed["team-alpha"]["memories"] == 0
    assert listed["team-alpha"]["description"] == "shared research notes"
    # and a subsequent write lands in the pre-created folder (same id, not a new one)
    mid = svc.remember("Alpha fact.", workspace="team-alpha", scope="workspace")["id"]
    assert mid in _mem_ids(svc, "team-alpha")


def test_create_rejects_duplicate_name():
    svc = _svc()
    svc.create_workspace("dupe")
    with pytest.raises(ValidationError):
        svc.create_workspace("dupe")
    # a folder minted lazily by a write is just as much a duplicate
    svc.remember("x", workspace="lazy", scope="workspace")
    with pytest.raises(ValidationError):
        svc.create_workspace("lazy")


def test_create_respects_workspace_binding():
    """A bound instance (ENGRAPHIS_WORKSPACES) must refuse folders outside its allow-list —
    the create path can't become a hole in the isolation boundary every read/write honors."""
    svc = MemoryService.create(":memory:", allowed_workspaces=["allowed"])
    assert svc.create_workspace("allowed")["created"]
    with pytest.raises(ValidationError):
        svc.create_workspace("intruder")


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


# ── copy_workspace ───────────────────────────────────────────────────────────
def test_copy_auto_names_and_leaves_source_untouched():
    svc = _svc()
    a1 = svc.remember("Alpha one fact.", workspace="a", scope="workspace")["id"]
    a2 = svc.remember("Alpha two fact.", workspace="a", scope="workspace")["id"]

    out = svc.copy_workspace("a")
    assert out["workspace"] == "a copy"
    assert out["memories_copied"] == 2
    # source is untouched — same ids, same content, workspace still exists
    assert _wsid(svc, "a") is not None
    assert _mem_ids(svc, "a") == {a1, a2}
    assert svc.store.get_memory(a1).content == "Alpha one fact."
    # the copy has its own ids — nothing shared with the source
    copy_ids = _mem_ids(svc, "a copy")
    assert len(copy_ids) == 2
    assert copy_ids.isdisjoint({a1, a2})
    contents = {svc.store.get_memory(i).content for i in copy_ids}
    assert contents == {"Alpha one fact.", "Alpha two fact."}

    # copying again auto-increments past the first copy
    out2 = svc.copy_workspace("a")
    assert out2["workspace"] == "a copy 2"


def test_copy_clones_vectors_fts_links_entities_and_edges():
    svc = _svc()
    m1 = svc.remember("Postgres 16 is the primary database.", workspace="a",
                      repo="infra", scope="repo")["id"]
    m2 = svc.remember("Deploys run Fridays at noon.", workspace="a",
                      repo="infra", scope="repo")["id"]
    svc.link(m1, m2, workspace="a", relation="related")

    svc.copy_workspace("a", new_name="a2")
    wid_dst = _wsid(svc, "a2")
    c = svc.store.conn

    new_mem = [dict(r) for r in c.execute(
        "SELECT id, content FROM memories WHERE workspace_id=?", (wid_dst,))]
    assert len(new_mem) == 2
    new_ids = {r["id"] for r in new_mem}
    assert new_ids.isdisjoint({m1, m2})

    # full-text mirror copied under the new ids
    for r in new_mem:
        fts = c.execute("SELECT content FROM mem_fts WHERE id=?", (r["id"],)).fetchone()
        assert fts is not None and fts["content"] == r["content"]

    # vector mirror copied under the new ids (embedder is on by default)
    for nid in new_ids:
        assert c.execute("SELECT 1 FROM mem_vectors WHERE id=?", (nid,)).fetchone() is not None

    # the repo was cloned (fresh id, same name) rather than shared with the source
    repos = [dict(r) for r in c.execute(
        "SELECT id, name FROM repos WHERE workspace_id=?", (wid_dst,))]
    assert [r["name"] for r in repos] == ["infra"]
    src_repo_id = c.execute(
        "SELECT id FROM repos WHERE workspace_id=?", (_wsid(svc, "a"),)).fetchone()["id"]
    assert repos[0]["id"] != src_repo_id
    # every copied memory points at the cloned repo, not the source repo
    repo_ids = {c.execute("SELECT repo_id FROM memories WHERE id=?", (r["id"],)).fetchone()["repo_id"]
                for r in new_mem}
    assert repo_ids == {repos[0]["id"]}

    # the mem_links row was cloned onto the two new memory ids
    id_map = {r["content"]: r["id"] for r in new_mem}
    new_a, new_b = id_map["Postgres 16 is the primary database."], id_map["Deploys run Fridays at noon."]
    linked = c.execute(
        "SELECT 1 FROM mem_links WHERE (a=? AND b=?) OR (a=? AND b=?)",
        (new_a, new_b, new_b, new_a)).fetchone()
    assert linked is not None


def test_copy_rejects_missing_source_and_colliding_new_name():
    svc = _svc()
    svc.remember("x", workspace="a", scope="workspace")
    svc.remember("y", workspace="b", scope="workspace")
    with pytest.raises(ValidationError):
        svc.copy_workspace("ghost")                       # missing source
    with pytest.raises(ValidationError):
        svc.copy_workspace("a", new_name="b")              # explicit name collides


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
