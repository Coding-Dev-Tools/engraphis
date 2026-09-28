"""Moves preserve complete history, fail atomically, and never weaken ownership."""
import time

import pytest

from engraphis.core.ids import new_id
from engraphis.core.interfaces import MemoryRecord, Scope
from engraphis.core.mutations import MemoryConflict
from engraphis.service import MemoryService, ValidationError, set_current_user


@pytest.fixture
def svc():
    service = MemoryService.create(":memory:")
    service.create_workspace("source")
    service.create_workspace("target")
    yield service
    set_current_user(None)
    service.store.close()


def remember(svc, text="A durable checkout convention.", workspace="source", repo=None, **kwargs):
    wid = svc.store.get_or_create_workspace(workspace)
    rid = svc.store.get_or_create_repo(wid, repo) if repo else None
    metadata = dict(kwargs.pop("metadata", {}), embed_model=svc.engine.embedding_space)
    rec = MemoryRecord(id=new_id("memory"), content=text, workspace_id=wid, repo_id=rid,
                       scope=kwargs.pop("scope", Scope.REPO if repo else Scope.WORKSPACE),
                       embedding=svc.engine.embedder.embed([text])[0],
                       provenance={"source": "test", "trusted": True, "review_state": "approved"},
                       metadata=metadata,
                       **kwargs)
    return svc.store.add_memory(rec)


def preview(svc, *mids):
    return svc.preview_memory_move(workspace="source", target_workspace="target",
                                   memory_ids=list(mids))


def move(svc, plan):
    return svc.move_memories(workspace="source", target_workspace="target",
                            memory_ids=plan["requested_ids"], preview_token=plan["preview_token"],
                            confirmed=True)


def test_preview_is_read_only_and_move_preserves_identity_content_scope_and_indexes(svc):
    mid = remember(svc, repo="website", title="Deploy", pinned=True, stability=15.0,
                   metadata={"rationale": "same deploy procedure"})
    unrelated = remember(svc, "Orchard soil mineral measurements.")
    before = svc.store.get_memory(mid)
    vector = bytes(svc.store.conn.execute("SELECT vector FROM mem_vectors WHERE id=?", (mid,)).fetchone()[0])
    count = svc.store.conn.total_changes
    plan = preview(svc, mid)
    assert plan["can_move"] and plan["count"] == 1 and plan["repos"] == ["website"]
    assert svc.store.conn.total_changes == count
    assert svc.store.conn.execute("SELECT 1 FROM repos WHERE workspace_id=?",
                                  (svc._lookup_workspace("target"),)).fetchone() is None
    result = move(svc, plan)
    assert result["moved"] == [mid]
    after = svc.store.get_memory(mid)
    assert after.workspace_id == svc._lookup_workspace("target")
    assert after.repo_id != before.repo_id and after.scope == before.scope
    for field in ("content", "title", "metadata", "provenance", "pinned", "stability",
                  "valid_from", "valid_to", "ingested_at", "expired_at"):
        assert getattr(after, field) == getattr(before, field)
    assert after.modified_hlc != before.modified_hlc
    assert svc.store.get_memory(unrelated).workspace_id == before.workspace_id
    assert bytes(svc.store.conn.execute("SELECT vector FROM mem_vectors WHERE id=?", (mid,)).fetchone()[0]) == vector
    assert svc.store.conn.execute("SELECT 1 FROM mem_fts WHERE id=?", (mid,)).fetchone()
    assert any(row["action"] == "workspace_move" for row in svc.store.conn.execute(
        "SELECT action FROM audit WHERE target=?", (mid,)))
    assert mid not in {hit["id"] for hit in svc.engine.recall("checkout convention", k=10,
                        workspace_id=before.workspace_id).chunks}
    assert mid in {hit["id"] for hit in svc.engine.recall("checkout convention", k=10,
                    workspace_id=after.workspace_id).chunks}


def test_incoming_and_outgoing_history_and_links_move_together(svc):
    old = remember(svc, "Prior deployment rule.", valid_to=time.time() - 10)
    new = remember(svc, "Updated deployment rule.", metadata={"supersedes": [old]})
    digest = remember(svc, "Consolidated procedure.", metadata={"provenance": {"consolidates": [new]}})
    linked = remember(svc, "Related deployment event.")
    svc.store.add_link(digest, linked, "related")
    plan = preview(svc, old)
    assert set(plan["memory_ids"]) == {old, new, digest, linked}
    assert plan["related_count"] == 3
    move(svc, plan)
    assert all(svc.store.get_memory(mid).workspace_id == svc._lookup_workspace("target")
               for mid in (old, new, digest, linked))
    assert svc.store.get_memory(old).valid_to is not None
    assert svc.store.get_memory(new).metadata["supersedes"] == [old]


def test_whole_closed_session_and_events_move_without_cloning_or_dropping_handoff(svc):
    sid = svc.start_session(workspace="source", repo="api", goal="checkout", agent="test")["session_id"]
    first = remember(svc, repo="api", session_id=sid)
    second = remember(svc, "Rollback procedure.", repo="api", session_id=sid)
    svc.record_event("decision", "Use staged rollout.", workspace="source", repo="api", session_id=sid)
    active = preview(svc, first)
    assert "active_session" in {item["code"] for item in active["blockers"]}
    svc.end_session(sid, summary="Preserve this handoff", outcome="done", open_threads=[])
    plan = preview(svc, first)
    assert set(plan["memory_ids"]) == {first, second} and plan["sessions"] == 1
    move(svc, plan)
    session = svc.store.get_session(sid)
    assert session["workspace_id"] == svc._lookup_workspace("target")
    assert session["summary"] == "Preserve this handoff"
    assert svc.store.get_memory(first).session_id == sid
    assert all(row["workspace_id"] == session["workspace_id"] for row in
               svc.store.conn.execute("SELECT * FROM events WHERE session_id=?", (sid,)))


@pytest.mark.parametrize("change", ["content", "new_history", "target_claim"])
def test_stale_preview_cannot_move_partial_or_changed_history(svc, change):
    mid = remember(svc, subject_key="release", claim_kind="region")
    plan = preview(svc, mid)
    if change == "content":
        svc.store.conn.execute("UPDATE memories SET title='Changed' WHERE id=?", (mid,))
        svc.store.conn.commit()
    elif change == "new_history":
        remember(svc, "Later correction", metadata={"corrects": mid})
    else:
        remember(svc, "Other region", workspace="target", subject_key="release", claim_kind="region")
    with pytest.raises(MemoryConflict, match="stale"):
        move(svc, plan)
    assert svc.store.get_memory(mid).workspace_id == svc._lookup_workspace("source")


@pytest.mark.parametrize("kind,code", [("document", "imported_document"), ("sync", "synced_memory"),
                                       ("export", "synced_memory"), ("code", "code_links")])
def test_attached_records_return_actionable_blockers(svc, kind, code):
    metadata = {"document": {"source_key": "abc"}} if kind == "document" else {}
    if kind == "sync":
        metadata = {"provenance": {"source": "sync", "synced_from_device": "test-device"}}
    mid = remember(svc, metadata=metadata)
    if kind == "export":
        svc.store.conn.execute("INSERT INTO memory_sync_exports VALUES(?,?,?,?,?)",
                               (mid, svc._lookup_workspace("source"), None, 1.0, 2.0))
    if kind == "code":
        rid = svc.store.get_or_create_repo(svc._lookup_workspace("source"), "api")
        svc.store.conn.execute("INSERT INTO code_memory_links(id,repo_id,symbol_id,memory_id) "
                               "VALUES(?,?,?,?)", ("link", rid, "sym_fixture", mid))
    svc.store.conn.commit()
    plan = preview(svc, mid)
    assert not plan["can_move"] and not plan["preview_token"]
    assert code in {item["code"] for item in plan["blockers"]}
    with pytest.raises(ValidationError, match="Preview and confirm"):
        move(svc, plan)


def test_graph_evidence_preserved_and_shared_source_entities_are_not_rehomed(svc):
    from engraphis.core.interfaces import Edge, Node
    mid = remember(svc)
    wid = svc._lookup_workspace("source")
    node_a = Node(new_id("entity"), "checkout", workspace_id=wid)
    node_b = Node(new_id("entity"), "validation", workspace_id=wid)
    svc.store.upsert_entity(node_a)
    svc.store.upsert_entity(node_b)
    edge = Edge(new_id("edge"), node_a.id, node_b.id, "requires", workspace_id=wid,
                provenance={"memory_ids": [mid]})
    svc.store.upsert_edge(edge)
    plan = preview(svc, mid)
    assert plan["can_move"] and plan["graph_edges"] == 1
    support_before = [dict(row) for row in svc.store.conn.execute(
        "SELECT * FROM edge_supports WHERE edge_id=?", (edge.id,))]
    move(svc, plan)
    moved = svc.store.conn.execute("SELECT * FROM edges WHERE id=?", (edge.id,)).fetchone()
    assert moved["workspace_id"] == svc._lookup_workspace("target")
    assert moved["src"] != node_a.id and moved["dst"] != node_b.id
    assert svc.store.conn.execute("SELECT workspace_id FROM entities WHERE id=?",
                                  (node_a.id,)).fetchone()[0] == wid
    assert support_before == [dict(row) for row in svc.store.conn.execute(
        "SELECT * FROM edge_supports WHERE edge_id=?", (edge.id,))]


def test_failed_audit_rolls_back_memories_repos_and_hlc(svc, monkeypatch):
    mid = remember(svc, repo="api")
    before = svc.store.get_memory(mid)
    plan = preview(svc, mid)

    def fail(*args, **kwargs):
        raise RuntimeError("audit failure")

    monkeypatch.setattr(svc.store, "audit", fail)
    with pytest.raises(RuntimeError, match="audit failure"):
        move(svc, plan)
    after = svc.store.get_memory(mid)
    assert after.workspace_id == before.workspace_id and after.modified_hlc == before.modified_hlc
    assert svc.store.conn.execute("SELECT 1 FROM repos WHERE workspace_id=?",
                                  (svc._lookup_workspace("target"),)).fetchone() is None


def test_related_private_session_is_not_disclosed_or_moved(svc):
    set_current_user({"id": "other", "email": "other@example.test", "role": "admin"})
    sid = svc.start_session(workspace="source", agent="other")["session_id"]
    hidden = remember(svc, "Private session title", session_id=sid, scope=Scope.SESSION)
    svc.end_session(sid, summary="private")
    public = remember(svc, metadata={"corrects": hidden})
    set_current_user({"id": "me", "email": "me@example.test", "role": "admin"})
    with pytest.raises(ValidationError, match="another user"):
        preview(svc, public)


def test_preview_and_apply_authorize_destination_each_time(svc):
    mid = remember(svc)
    plan = preview(svc, mid)
    svc.allowed_workspaces = {"source"}
    with pytest.raises(ValidationError):
        move(svc, plan)
    assert svc.store.get_memory(mid).workspace_id == svc._lookup_workspace("source")


def test_api_requires_explicit_confirmation_and_rejects_stale_preview(svc, monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from engraphis.routes import v2_api

    monkeypatch.setattr(v2_api, "service", lambda: svc)
    app = FastAPI()
    app.include_router(v2_api.router)
    mid = remember(svc)
    body = {"workspace": "source", "target_workspace": "target", "memory_ids": [mid]}
    with TestClient(app) as client:
        plan = client.post("/api/memories/move-preview", json=body).json()
        body["preview_token"] = plan["preview_token"]
        assert client.post("/api/memories/move", json=body).status_code == 400
        body["confirmed"] = "true"
        assert client.post("/api/memories/move", json=body).status_code == 422
        body["confirmed"] = True
        svc.store.conn.execute("UPDATE memories SET title='Changed after preview' WHERE id=?", (mid,))
        svc.store.conn.commit()
        stale = client.post("/api/memories/move", json=body)
        assert stale.status_code == 409
        assert stale.json()["detail"]["code"] == "memory_conflict"
        assert svc.store.get_memory(mid).workspace_id == svc._lookup_workspace("source")
        body["preview_token"] = client.post("/api/memories/move-preview", json=body).json()["preview_token"]
        assert client.post("/api/memories/move", json=body).json()["moved"] == [mid]
        assert svc.store.conn.execute("PRAGMA foreign_key_check").fetchall() == []


def graph_pair(svc, workspace, mid, *, reverse=False, relation="co_occurs"):
    from engraphis.core.interfaces import Edge, Node
    wid = svc._lookup_workspace(workspace)
    names = ["checkout", "validation"]
    if reverse:
        names.reverse()
    nodes = {name: Node(new_id("entity"), name, workspace_id=wid) for name in names}
    for node in nodes.values():
        svc.store.upsert_entity(node)
    edge = Edge(new_id("edge"), nodes["checkout"].id, nodes["validation"].id, relation,
                workspace_id=wid, provenance={"memory_ids": [mid]})
    svc.store.upsert_edge(edge)
    return nodes, edge


def test_undirected_graph_collision_uses_destination_order(svc):
    source = remember(svc)
    target = remember(svc, "Destination evidence.", workspace="target")
    graph_pair(svc, "source", source)
    graph_pair(svc, "target", target, reverse=True)
    plan = preview(svc, source)
    assert "target_graph_conflict" in {item["code"] for item in plan["blockers"]}
    assert svc.store.conn.execute("SELECT COUNT(*) FROM edges WHERE workspace_id=?",
                                  (svc._lookup_workspace("target"),)).fetchone()[0] == 1


def test_move_preserves_alias_canonical_history_including_unattached_root(svc):
    from engraphis.core.interfaces import Node
    mid = remember(svc)
    wid = svc._lookup_workspace("source")
    root = Node(new_id("entity"), "PostgreSQL database", workspace_id=wid)
    alias = Node(new_id("entity"), "PostgreSQL database server", workspace_id=wid)
    svc.store.upsert_entity(root)
    svc.store.upsert_entity(alias)
    svc.store.conn.execute("UPDATE entities SET canonical_id=?,canonical_method='token_overlap',"
                           "canonical_confidence=0.8 WHERE id=?", (root.id, alias.id))
    svc.store.conn.execute("INSERT INTO memory_entities(id,memory_id,entity_id,workspace_id) "
                           "VALUES(?,?,?,?)", ("incidence", mid, alias.id, wid))
    svc.store.conn.commit()
    plan = preview(svc, mid)
    assert plan["can_move"]
    # Adding an unrelated target repo's identity must never change an already
    # prepared canonical choice at apply time.
    rid = svc.store.get_or_create_repo(svc._lookup_workspace("target"), "unrelated")
    foreign_root = Node(new_id("entity"), root.name, workspace_id=svc._lookup_workspace("target"), repo_id=rid)
    svc.store.upsert_entity(foreign_root)
    move(svc, plan)
    rows = list(svc.store.conn.execute("SELECT * FROM entities WHERE workspace_id=? AND repo_id IS NULL",
                                      (svc._lookup_workspace("target"),)))
    moved_root = next(row for row in rows if row["name"] == root.name)
    moved_alias = next(row for row in rows if row["name"] == alias.name)
    assert moved_alias["canonical_id"] == moved_root["id"]
    assert moved_alias["canonical_method"] == "token_overlap"
    assert moved_alias["canonical_confidence"] == 0.8
    assert moved_root["canonical_id"] == moved_root["id"]


def test_different_sessions_can_retain_same_claim_key(svc):
    selected = []
    for workspace in ("source", "target"):
        sid = svc.start_session(workspace=workspace, agent="test")["session_id"]
        selected.append(remember(svc, workspace=workspace, session_id=sid, scope=Scope.SESSION,
                                 subject_key="plan", claim_kind="step"))
        svc.end_session(sid, summary="Session complete")
    plan = preview(svc, selected[0])
    assert plan["can_move"]
    move(svc, plan)
    assert svc.store.get_memory(selected[0]).session_id != svc.store.get_memory(selected[1]).session_id


def test_move_does_not_rewrite_original_receipt_chains(svc):
    mid = svc.remember("Keep the original audit chain.", workspace="source")["id"]
    receipts = [dict(row) for row in svc.store.conn.execute("SELECT * FROM operation_receipts")]
    assert receipts
    move(svc, preview(svc, mid))
    assert receipts == [dict(row) for row in svc.store.conn.execute("SELECT * FROM operation_receipts")]


def test_failed_move_preserves_a_caller_owned_transaction(svc, monkeypatch):
    mid = remember(svc, repo="api")
    plan = preview(svc, mid)
    svc.store.conn.execute("BEGIN IMMEDIATE")
    svc.store.conn.execute("UPDATE workspaces SET created_at=42 WHERE name='source'")

    def fail(*args, **kwargs):
        raise RuntimeError("audit failure")

    monkeypatch.setattr(svc.store, "audit", fail)
    # The caller's change is unrelated to the preview's ownership policy.
    plan = preview(svc, mid)
    with pytest.raises(RuntimeError, match="audit failure"):
        move(svc, plan)
    assert svc.store.conn.transaction_owned_by_current_thread()
    assert svc.store.conn.execute("SELECT created_at FROM workspaces WHERE name='source'").fetchone()[0] == 42
    assert svc.store.get_memory(mid).workspace_id == svc._lookup_workspace("source")
    svc.store.conn.rollback()


def test_correction_commands_follow_history_and_keep_retries_idempotent(svc):
    from engraphis.core.mutations import memory_version

    old = remember(svc, "Use the first deployment path.", repo="api")
    corrected = svc.correct(old, "Use the second deployment path.", workspace="source", repo="api")
    command = dict(svc.store.conn.execute("SELECT * FROM memory_commands WHERE result_id=?",
                                         (corrected["id"],)).fetchone())
    plan = preview(svc, old)
    assert plan["can_move"] and set(plan["memory_ids"]) == {old, corrected["id"]}
    move(svc, plan)
    replay = svc.correct(old, "Use the second deployment path.", workspace="target", repo="api")
    assert replay["id"] == corrected["id"]
    moved = svc.store.conn.execute("SELECT * FROM memory_commands WHERE sequence=?", (command["sequence"],)).fetchone()
    assert moved["workspace_id"] == svc._lookup_workspace("target")
    assert moved["request_hash"] == command["request_hash"]
    assert moved["result_version"] == memory_version(svc.store.get_memory(corrected["id"]))
    assert list(svc.store.conn.execute("PRAGMA foreign_key_check")) == []


def test_move_blocks_destination_operation_id_collision(svc):
    old = remember(svc, "Original deployment path.")
    corrected = svc.correct(old, "Corrected deployment path.", workspace="source")
    destination = remember(svc, "Destination operation.", workspace="target")
    svc.store.conn.execute("INSERT INTO memory_commands(workspace_id,operation_id,operation,request_hash,"
                           "result_id,result_version,created_at) SELECT ?,operation_id,operation,request_hash,"
                           "?,result_version,created_at FROM memory_commands WHERE result_id=?",
                           (svc._lookup_workspace("target"), destination, corrected["id"]))
    svc.store.conn.commit()
    assert "target_operation_conflict" in {item["code"] for item in preview(svc, old)["blockers"]}


@pytest.mark.parametrize("other_session", [False, True])
def test_incoming_event_references_block_moves_and_stale_previews(svc, other_session):
    mid = remember(svc)
    before = preview(svc, mid)
    sid = svc.start_session("source", goal="Other work")["session_id"] if other_session else None
    svc.record_event("decision", "This event cites the memory.", workspace="source", session_id=sid, refs=[mid])
    if sid:
        svc.end_session(sid, summary="Closed other work")
    assert "external_events" in {item["code"] for item in preview(svc, mid)["blockers"]}
    with pytest.raises(MemoryConflict, match="stale"):
        move(svc, before)
    assert svc.store.get_memory(mid).workspace_id == svc._lookup_workspace("source")


def test_preview_discloses_and_binds_workspace_access(svc):
    set_current_user({"id": "me", "email": "me@example.test", "role": "admin"})
    svc.set_workspace_visibility("source", "personal", confirmed=True)
    mid = remember(svc)
    plan = preview(svc, mid)
    assert plan["source_visibility"] == "personal" and plan["target_visibility"] == "shared"
    svc.set_workspace_visibility("target", "personal", confirmed=True)
    with pytest.raises(MemoryConflict, match="stale"):
        move(svc, plan)
