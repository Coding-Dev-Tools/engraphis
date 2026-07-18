"""Backend tests for merge_workspaces(), copy_workspace() and reorder_memories() —
auth-reachable, multi-table SQL operations. merge_workspaces does direct repo/entity/
edge/memory remapping across workspaces; copy_workspace clones the same tables under
fresh ids into a new workspace; reorder_memories writes a per-memory sort_order. All
three are reachable from the authenticated dashboard API, so a silent regression here
corrupts or leaks data.
"""
import pytest

from engraphis.core.interfaces import Edge, GraphLayer, Node
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


def test_workspace_counts_exclude_superseded_memories():
    svc = _svc()
    old = svc.remember("The deploy region is iad.", workspace="team-alpha",
                       scope="workspace")["id"]
    svc.store.close_validity(old)
    svc.remember("The deploy region is fra.", workspace="team-alpha", scope="workspace")

    listed = {w["name"]: w for w in svc.list_workspaces()["workspaces"]}
    assert listed["team-alpha"]["memories"] == 1


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
def test_delete_removes_code_file_state_and_memory_bridges():
    svc = _svc()
    memory_id = svc.remember(
        "The deploy helper ships releases.", workspace="a", repo="web", scope="repo"
    )["id"]
    c = svc.store.conn
    repo_id = c.execute(
        "SELECT id FROM repos WHERE workspace_id=? AND name='web'", (_wsid(svc, "a"),)
    ).fetchone()["id"]
    symbol_id = svc.store.upsert_symbol(
        repo_id=repo_id, kind="function", name="deploy", fqname="deploy",
        file="deploy.py", span="1:1-2:1", lang="python",
    )
    svc.store.upsert_code_file(
        repo_id=repo_id, file="deploy.py", lang="python",
        content_hash="file-hash", size_bytes=20, mtime_ns=20, backend="regex",
    )
    svc.store.link_memory_symbol(
        repo_id=repo_id, symbol_id=symbol_id, memory_id=memory_id,
    )

    svc.delete_workspace("a")

    for table in ("code_memory_links", "code_files", "symbols", "repos"):
        assert c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


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


def test_merge_remaps_code_files_and_memory_links_for_colliding_repos():
    svc = _svc()
    source_memory = svc.remember(
        "Source deploy helper.", workspace="a", repo="web", scope="repo"
    )["id"]
    svc.remember("Target deploy helper.", workspace="b", repo="web", scope="repo")
    c = svc.store.conn
    src_repo = c.execute(
        "SELECT id FROM repos WHERE workspace_id=? AND name='web'", (_wsid(svc, "a"),)
    ).fetchone()["id"]
    dst_repo = c.execute(
        "SELECT id FROM repos WHERE workspace_id=? AND name='web'", (_wsid(svc, "b"),)
    ).fetchone()["id"]
    symbol = svc.store.upsert_symbol(
        repo_id=src_repo, kind="function", name="deploy", fqname="deploy",
        file="deploy.py", span="1:1-2:1", docstring="Deploy the application.",
        lang="python", content_hash="source-symbol",
    )
    svc.store.upsert_code_file(
        repo_id=src_repo, file="deploy.py", lang="python",
        content_hash="newer-source", size_bytes=20, mtime_ns=20,
        backend="regex",
    )
    svc.store.upsert_code_file(
        repo_id=dst_repo, file="deploy.py", lang="python",
        content_hash="older-target", size_bytes=10, mtime_ns=10,
        backend="regex",
    )
    c.execute(
        "UPDATE code_files SET indexed_at=20 WHERE repo_id=? AND file='deploy.py'",
        (src_repo,),
    )
    c.execute(
        "UPDATE code_files SET indexed_at=10 WHERE repo_id=? AND file='deploy.py'",
        (dst_repo,),
    )
    svc.store.link_memory_symbol(
        repo_id=src_repo, symbol_id=symbol, memory_id=source_memory,
        relation="implements", confidence=0.8,
    )

    svc.merge_workspaces("a", "b")

    assert c.execute("SELECT 1 FROM repos WHERE id=?", (src_repo,)).fetchone() is None
    file_row = c.execute(
        "SELECT content_hash FROM code_files WHERE repo_id=? AND file='deploy.py'",
        (dst_repo,),
    ).fetchone()
    assert file_row["content_hash"] == "newer-source"
    assert c.execute(
        "SELECT COUNT(*) FROM code_files WHERE repo_id=?", (src_repo,)
    ).fetchone()[0] == 0
    link = c.execute(
        "SELECT repo_id, symbol_id, memory_id, relation FROM code_memory_links "
        "WHERE memory_id=?",
        (source_memory,),
    ).fetchone()
    assert dict(link) == {
        "repo_id": dst_repo,
        "symbol_id": symbol,
        "memory_id": source_memory,
        "relation": "implements",
    }
    graph = svc.export_code_graph(workspace="b", repo="web")["graph"]
    assert len(graph["files"]) == 1
    assert len(graph["memory_links"]) == 1


def test_merge_does_not_duplicate_symbols_for_overlapping_files():
    """Both workspaces independently indexed the same file in a same-named repo:
    the fold keeps exactly the winning snapshot's symbols/edges — never a
    duplicate pair of stale + fresh rows for one file — and memory links from
    the losing snapshot are re-pointed at the surviving same-fqname symbol
    instead of dropped (PR #19 review follow-up)."""
    svc = _svc()
    a_mem = svc.remember("A web note.", workspace="a", repo="web", scope="repo")["id"]
    b_mem = svc.remember("B web note.", workspace="b", repo="web", scope="repo")["id"]
    c = svc.store.conn
    src_repo = c.execute(
        "SELECT id FROM repos WHERE workspace_id=? AND name='web'", (_wsid(svc, "a"),)
    ).fetchone()["id"]
    dst_repo = c.execute(
        "SELECT id FROM repos WHERE workspace_id=? AND name='web'", (_wsid(svc, "b"),)
    ).fetchone()["id"]
    symbol_ids = {}
    for repo_id, marker, indexed_at in ((src_repo, "src", 20), (dst_repo, "dst", 10)):
        symbol_ids[marker] = svc.store.upsert_symbol(
            repo_id=repo_id, kind="function", name="deploy", fqname="deploy",
            file="deploy.py", span="1:1-2:1", content_hash=f"{marker}-symbol",
        )
        svc.store.upsert_code_file(
            repo_id=repo_id, file="deploy.py", lang="python",
            content_hash=f"{marker}-hash", size_bytes=10, mtime_ns=10, backend="regex",
        )
        c.execute("UPDATE code_files SET indexed_at=? WHERE repo_id=? AND file='deploy.py'",
                  (indexed_at, repo_id))
    # Each side linked its own memory to its own snapshot of the symbol.
    svc.store.link_memory_symbol(repo_id=src_repo, symbol_id=symbol_ids["src"],
                                 memory_id=a_mem, relation="mentions")
    svc.store.link_memory_symbol(repo_id=dst_repo, symbol_id=symbol_ids["dst"],
                                 memory_id=b_mem, relation="mentions")

    svc.merge_workspaces("a", "b")  # src is newer → its snapshot wins

    rows = c.execute(
        "SELECT content_hash FROM symbols WHERE repo_id=? AND file='deploy.py'",
        (dst_repo,),
    ).fetchall()
    assert [r["content_hash"] for r in rows] == ["src-symbol"]
    assert c.execute(
        "SELECT content_hash FROM code_files WHERE repo_id=? AND file='deploy.py'",
        (dst_repo,),
    ).fetchone()["content_hash"] == "src-hash"
    # BOTH memories keep their code provenance, now against the surviving symbol.
    links = {row["memory_id"]: dict(row) for row in c.execute(
        "SELECT memory_id, symbol_id, repo_id FROM code_memory_links")}
    assert set(links) == {a_mem, b_mem}
    for link in links.values():
        assert link["symbol_id"] == symbol_ids["src"]
        assert link["repo_id"] == dst_repo

    # And the mirror case: when the surviving repo's snapshot is newer, the
    # incoming stale symbols are dropped instead of duplicated.
    svc.remember("C web note.", workspace="c", repo="web", scope="repo")
    stale_repo = c.execute(
        "SELECT id FROM repos WHERE workspace_id=? AND name='web'", (_wsid(svc, "c"),)
    ).fetchone()["id"]
    svc.store.upsert_symbol(
        repo_id=stale_repo, kind="function", name="deploy", fqname="deploy",
        file="deploy.py", span="1:1-2:1", content_hash="stale-symbol",
    )
    svc.store.upsert_code_file(
        repo_id=stale_repo, file="deploy.py", lang="python",
        content_hash="stale-hash", size_bytes=10, mtime_ns=10, backend="regex",
    )
    c.execute("UPDATE code_files SET indexed_at=5 WHERE repo_id=? AND file='deploy.py'",
              (stale_repo,))

    svc.merge_workspaces("c", "b")

    rows = c.execute(
        "SELECT content_hash FROM symbols WHERE repo_id=? AND file='deploy.py'",
        (dst_repo,),
    ).fetchall()
    assert [r["content_hash"] for r in rows] == ["src-symbol"]
    assert c.execute(
        "SELECT content_hash FROM code_files WHERE repo_id=? AND file='deploy.py'",
        (dst_repo,),
    ).fetchone()["content_hash"] == "src-hash"


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
    svc.link(
        m1, m2, workspace="a", relation="related",
        layer="causal", reason="deployment depends on the database",
    )
    src_repo_id = svc.store.conn.execute(
        "SELECT id FROM repos WHERE workspace_id=?", (_wsid(svc, "a"),)
    ).fetchone()["id"]
    svc.store.upsert_code_file(
        repo_id=src_repo_id, file="deploy.py", lang="python",
        content_hash="file-hash", size_bytes=128, mtime_ns=123,
        backend="regex",
    )
    src_symbol = svc.store.upsert_symbol(
        repo_id=src_repo_id, kind="function", name="deploy", fqname="deploy",
        file="deploy.py", span="1:1-3:1", signature="deploy()",
        docstring="Deploy the application.", lang="python",
        content_hash="symbol-hash",
    )
    svc.store.add_code_edge(
        repo_id=src_repo_id, src=src_symbol, dst="helper",
        relation="calls", layer=GraphLayer.CAUSAL, file="deploy.py", line=2,
    )
    svc.store.link_memory_symbol(
        repo_id=src_repo_id, symbol_id=src_symbol, memory_id=m1,
        relation="implements", confidence=0.75,
    )
    wid_src = _wsid(svc, "a")
    database = svc.store.upsert_entity(Node(
        id="", name="Postgres", ntype="database",
        workspace_id=wid_src, repo_id=src_repo_id,
    ))
    deploy = svc.store.upsert_entity(Node(
        id="", name="Deploy", ntype="process",
        workspace_id=wid_src, repo_id=src_repo_id,
    ))
    svc.store.upsert_edge(Edge(
        id="", src=deploy, dst=database, relation="depends_on",
        layer=GraphLayer.CAUSAL, workspace_id=wid_src, repo_id=src_repo_id,
    ))

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
    assert repos[0]["id"] != src_repo_id
    # every copied memory points at the cloned repo, not the source repo
    repo_ids = {c.execute("SELECT repo_id FROM memories WHERE id=?", (r["id"],)).fetchone()["repo_id"]
                for r in new_mem}
    assert repo_ids == {repos[0]["id"]}

    # the mem_links row was cloned onto the two new memory ids
    id_map = {r["content"]: r["id"] for r in new_mem}
    new_a, new_b = id_map["Postgres 16 is the primary database."], id_map["Deploys run Fridays at noon."]
    linked = c.execute(
        "SELECT layer, reason FROM mem_links WHERE (a=? AND b=?) OR (a=? AND b=?)",
        (new_a, new_b, new_b, new_a)).fetchone()
    assert linked is not None
    assert linked["layer"] == "causal"
    assert linked["reason"] == "deployment depends on the database"

    copied_entities = {
        (row["name"], row["etype"]): row["id"] for row in c.execute(
            "SELECT id, name, etype FROM entities WHERE workspace_id=?", (wid_dst,)
        )
    }
    assert {("Postgres", "database"), ("Deploy", "process")} <= copied_entities.keys()
    assert set(copied_entities.values()).isdisjoint({database, deploy})
    copied_graph_edge = c.execute(
        "SELECT src, dst, relation, layer FROM edges "
        "WHERE workspace_id=? AND relation='depends_on'",
        (wid_dst,),
    ).fetchone()
    assert dict(copied_graph_edge) == {
        "src": copied_entities[("Deploy", "process")],
        "dst": copied_entities[("Postgres", "database")],
        "relation": "depends_on",
        "layer": "causal",
    }

    # Incremental file state, symbol documentation, layered code edges, and the
    # memory↔symbol bridge all point exclusively at copied ids.
    copied_repo = repos[0]["id"]
    code_file = c.execute(
        "SELECT file, content_hash, size_bytes, mtime_ns, backend FROM code_files "
        "WHERE repo_id=?",
        (copied_repo,),
    ).fetchone()
    assert dict(code_file) == {
        "file": "deploy.py",
        "content_hash": "file-hash",
        "size_bytes": 128,
        "mtime_ns": 123,
        "backend": "regex",
    }
    copied_symbol = c.execute(
        "SELECT id, docstring FROM symbols WHERE repo_id=? AND fqname='deploy'",
        (copied_repo,),
    ).fetchone()
    assert copied_symbol["id"] != src_symbol
    assert copied_symbol["docstring"] == "Deploy the application."
    copied_edge = c.execute(
        "SELECT src, relation, layer FROM code_edges WHERE repo_id=?",
        (copied_repo,),
    ).fetchone()
    assert dict(copied_edge) == {
        "src": copied_symbol["id"],
        "relation": "calls",
        "layer": "causal",
    }
    copied_bridge = c.execute(
        "SELECT symbol_id, memory_id, relation, confidence FROM code_memory_links "
        "WHERE repo_id=?",
        (copied_repo,),
    ).fetchone()
    assert copied_bridge["symbol_id"] == copied_symbol["id"]
    assert copied_bridge["memory_id"] == id_map[
        "Postgres 16 is the primary database."
    ]
    assert copied_bridge["relation"] == "implements"
    assert copied_bridge["confidence"] == pytest.approx(0.75)
    graph = svc.export_code_graph(workspace="a2", repo="infra")["graph"]
    assert len(graph["files"]) == 1
    assert len(graph["memory_links"]) == 1


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


# ── inspect() supersession chain — workspace isolation ─────────────────────────
def test_inspect_chain_forward_pointer_does_not_cross_workspace():
    """metadata is caller-supplied and reaches storage intact, so a writer in
    workspace b can plant metadata.supersedes naming an id it doesn't own. inspect()
    only _check_owns'd the root id; the forward LIKE scan (_successor_of) had no
    workspace filter, so b's record rode the forged pointer into a's chain."""
    svc = _svc()
    a_id = svc.remember("Workspace A secret fact.", workspace="a",
                        scope="workspace")["id"]
    svc.remember("Forged successor claiming to supersede A's memory.", workspace="b",
                scope="workspace", metadata={"supersedes": [a_id]})

    result = svc.inspect(a_id, workspace="a")
    assert {m["id"] for m in result["chain"]} == {a_id}


def test_inspect_chain_backward_pointer_does_not_cross_workspace():
    """Same boundary on the backward walk: a's own record forges a supersedes
    pointer naming a real memory that belongs to workspace b."""
    svc = _svc()
    b_id = svc.remember("Workspace B fact.", workspace="b", scope="workspace")["id"]
    forged_id = svc.remember("A's record forging a backward pointer into B.",
                             workspace="a", scope="workspace",
                             metadata={"supersedes": [b_id]})["id"]

    result = svc.inspect(forged_id, workspace="a")
    assert {m["id"] for m in result["chain"]} == {forged_id}
