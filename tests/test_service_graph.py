"""Tests for MemoryService.graph() — the Graph tab data source shared by the
v1-look dashboard and the Inspector UI (engraphis/graphdata.py).

Entity/edge extraction has its own coverage (test_graph_extractor.py); these
tests write directly to the entities/edges tables so they can pin exact
nodes/edges/stats without depending on the extractor's heuristics, and — the
main point of this file — lock in that graph() enforces the same
workspace-binding isolation boundary as every other read (service.py's
_clean_ws), which the dashboard-only implementation it replaced did not.
"""
import time

import pytest

from engraphis.backends.extractor import StructuredLLMExtractor
from engraphis.backends.graph_extractor import (
    RegexGraphExtractor,
    feed as graph_feed,
    get_graph_extractor,
)
from engraphis.core.graph_layers import infer_graph_layer
from engraphis.core.interfaces import Edge, MemoryRecord, MemoryType, Scope, SearchFilter
from engraphis.service import MemoryService, ValidationError, set_current_user


class _StructuredGraphLLM:
    def extract_json(self, prompt, schema):
        return {"facts": [{
            "content": "Engraphis stores memories in SQLite.",
            "title": "Storage backend",
            "entities": ["Engraphis", "SQLite"],
            "relations": [{"source": "engraphis", "relation": "stores_in", "target": "SQLite"}],
        }]}


def _seed_entities(svc, workspace, rows, edges):
    """``rows``: [(name, etype), ...]; ``edges``: [(src_name, dst_name, relation), ...]
    — authored by name for readability, but written to the DB the way the real
    extractor does (backends.graph_extractor.feed): edges.src/dst are entity **ids**
    (``ent0``, ``ent1``, ...), never the display name."""
    wid = svc.store.get_or_create_workspace(workspace)
    conn = svc.store.conn
    id_of = {}
    for i, (name, etype) in enumerate(rows):
        eid = f"ent{i}"
        id_of[name] = eid
        conn.execute(
            "INSERT INTO entities(id, workspace_id, repo_id, name, etype, created_at) "
            "VALUES (?,?,?,?,?,0)", (eid, wid, None, name, etype))
    for i, (src, dst, rel) in enumerate(edges):
        conn.execute(
            "INSERT INTO edges(id, workspace_id, repo_id, src, dst, relation, layer) "
            "VALUES (?,?,?,?,?,?,?)",
            (f"edge{i}", wid, None, id_of[src], id_of[dst], rel,
             infer_graph_layer(rel).value))
    conn.commit()
    return wid, id_of


def test_graph_returns_seeded_nodes_and_edges():
    svc = MemoryService.create(":memory:")
    _wid, id_of = _seed_entities(
        svc, "acme",
        [("Alice", "person_or_concept"), ("Acme Corp", "organization")],
        [("Alice", "Acme Corp", "works_at")])
    g = svc.graph(workspace="acme")
    assert {n["id"] for n in g["nodes"]} == {id_of["Alice"], id_of["Acme Corp"]}
    assert {n["label"] for n in g["nodes"]} == {"Alice", "Acme Corp"}
    assert g["edges"] == [{
        "from": id_of["Alice"], "to": id_of["Acme Corp"],
        "label": "works_at", "layer": "entity",
    }]
    assert g["stats"] == {"entities": 2, "edges": 1, "connected": 2, "isolated": 0}


def test_graph_on_nonexistent_workspace_is_empty_not_an_error():
    svc = MemoryService.create(":memory:")
    g = svc.graph(workspace="never-created")
    assert g["nodes"] == [] and g["edges"] == []
    assert g["stats"]["entities"] == 0


def test_graph_rejects_unpermitted_workspace():
    """The isolation boundary this method must not skip: a bound instance refuses
    to read another tenant's graph even if the caller knows/guesses its name —
    the exact gap the old dashboard-only implementation (a raw sqlite connection
    straight to the DB file, no MemoryService involved) left open."""
    seed = MemoryService.create(":memory:")
    _seed_entities(seed, "beta", [("Secret Corp", "organization")], [])
    attacker = MemoryService.create(":memory:")
    attacker.engine = seed.engine
    attacker.store = seed.store  # share the underlying store, differ only in binding
    attacker.allowed_workspaces = frozenset(["alpha"])
    import pytest
    with pytest.raises(ValidationError):
        attacker.graph(workspace="beta")


def test_graph_allows_its_own_bound_workspace():
    svc = MemoryService.create(":memory:", allowed_workspaces=["alpha"])
    _seed_entities(svc, "alpha", [("Widget", "person_or_concept")], [])
    g = svc.graph(workspace="alpha")
    assert {n["label"] for n in g["nodes"]} == {"Widget"}


def test_create_defaults_graph_extractor_on():
    """The config default is "regex", so a plain create() wires an extractor —
    every front end populates the graph without opting in (the wiring gap that
    left settings.graph_extractor orphaned)."""
    svc = MemoryService.create(":memory:")
    assert svc.engine.graph_extractor is not None


def test_remember_populates_graph_when_extractor_wired():
    """Ingest through the wired extractor writes entities, so the Graph tab has
    nodes for freshly remembered content (new users, day one)."""
    svc = MemoryService.create(":memory:", graph_extractor="regex")
    svc.remember("Alice Johnson works at Acme Corp.", workspace="acme",
                 scope="workspace")
    nodes = svc.graph(workspace="acme")["nodes"]
    labels = {n["label"] for n in nodes}
    assert "Alice Johnson" in labels and "Acme Corp" in labels
    # node identity is the entity id (ent_<ulid>), not the extracted name —
    # regression guard for the 2026-07-11 id/name mixup bug
    assert all(n["id"] != n["label"] for n in nodes)


def test_team_graph_reads_hide_legacy_session_supported_entities():
    svc = MemoryService.create(":memory:", graph_extractor="regex")
    try:
        set_current_user({"id": "usr_alice", "email": "alice@test", "role": "member"})
        svc.create_workspace("acme", visibility="shared", confirmed=True)
        session = svc.start_session("acme", repo="r", agent="codex", goal="private")
        private = svc.remember(
            "Alice Johnson works at Secret Corporation.", workspace="acme", repo="r",
            session_id=session["session_id"], scope="session",
        )
        # Simulate a database written before session graph extraction was disabled.
        memory = svc.store.get_memory(private["id"])
        graph_feed(
            svc.store, memory.content, workspace_id=memory.workspace_id,
            repo_id=memory.repo_id, extractor=RegexGraphExtractor(),
            provenance={"source": "legacy", "memory_id": memory.id},
        )

        set_current_user({"id": "usr_bob", "email": "bob@test", "role": "member"})
        assert "Secret Corporation" not in repr(svc.graph(workspace="acme", backfill=False))
        assert "Secret Corporation" not in repr(svc.graph_scene(workspace="acme"))
        assert svc.graph_suggest("Secret", workspace="acme")["groups"]["entities"] == []
        assert svc.graph_suggest("works", workspace="acme")["groups"]["relations"] == []

        # Ordinary local graph reads also have no session context; authentication being
        # disabled must not widen the hierarchy.
        set_current_user(None)
        assert "Secret Corporation" not in repr(svc.graph(workspace="acme", backfill=False))
        assert "Secret Corporation" not in repr(svc.graph_scene(workspace="acme"))
    finally:
        set_current_user(None)


def test_team_graph_reads_keep_forgotten_session_entities_private():
    svc = MemoryService.create(":memory:", graph_extractor="regex")
    try:
        set_current_user({"id": "usr_alice", "email": "alice@test", "role": "member"})
        svc.create_workspace("acme", visibility="shared", confirmed=True)
        session = svc.start_session("acme", repo="r", agent="codex", goal="private")
        private = svc.remember(
            "Alice Johnson works at Secret Corporation.", workspace="acme", repo="r",
            session_id=session["session_id"], scope="session",
        )
        memory = svc.store.get_memory(private["id"])
        graph_feed(
            svc.store, memory.content, workspace_id=memory.workspace_id,
            repo_id=memory.repo_id, extractor=RegexGraphExtractor(),
            provenance={"source": "legacy", "memory_id": memory.id},
        )

        svc.forget(private["id"], workspace="acme")

        set_current_user({"id": "usr_bob", "email": "bob@test", "role": "member"})
        assert "Alice Johnson" not in repr(svc.graph(workspace="acme", backfill=False))
        assert "Secret Corporation" not in repr(svc.graph(workspace="acme", backfill=False))
        assert "Secret Corporation" not in repr(svc.graph_scene(workspace="acme"))
        assert svc.graph_suggest("Secret", workspace="acme")["groups"]["entities"] == []
        assert svc.graph_suggest("works", workspace="acme")["groups"]["relations"] == []

        set_current_user(None)
        assert "Secret Corporation" not in repr(svc.graph(workspace="acme", backfill=False))
        assert "Secret Corporation" not in repr(svc.graph_scene(workspace="acme"))
        assert svc.graph_suggest("Secret", workspace="acme")["groups"]["entities"] == []
    finally:
        set_current_user(None)


def test_team_graph_entity_with_mixed_session_and_workspace_history_is_visible():
    svc = MemoryService.create(":memory:", graph_extractor="regex")
    try:
        set_current_user({"id": "usr_alice", "email": "alice@test", "role": "member"})
        svc.create_workspace("acme", visibility="shared", confirmed=True)
        session = svc.start_session("acme", repo="r", agent="codex", goal="private")
        private = svc.remember(
            "Alice Johnson works at Shared Corporation.", workspace="acme", repo="r",
            session_id=session["session_id"], scope="session",
        )
        public = svc.remember(
            "Alice Johnson works at Shared Corporation.", workspace="acme", repo="r",
            scope="repo",
        )
        for memory_id in (private["id"], public["id"]):
            memory = svc.store.get_memory(memory_id)
            graph_feed(
                svc.store, memory.content, workspace_id=memory.workspace_id,
                repo_id=memory.repo_id, extractor=RegexGraphExtractor(),
                provenance={"source": "legacy", "memory_id": memory.id},
            )

        set_current_user({"id": "usr_bob", "email": "bob@test", "role": "member"})
        graph = repr(svc.graph(workspace="acme", backfill=False))
        assert "Alice Johnson" in graph
        assert "Shared Corporation" in graph
        assert svc.graph_suggest("Shared", workspace="acme")["groups"]["entities"]
    finally:
        set_current_user(None)


def test_graph_index_excludes_session_scoped_memories():
    svc = MemoryService.create(":memory:", graph_extractor="none")
    try:
        set_current_user({"id": "usr_alice", "email": "alice@test", "role": "member"})
        svc.create_workspace("acme", visibility="shared", confirmed=True)
        session = svc.start_session("acme", repo="r", agent="codex", goal="private")
        svc.remember(
            "Private Falcon works at Hidden Corporation.", workspace="acme", repo="r",
            session_id=session["session_id"], scope="session",
        )
        svc.remember(
            "Public Robin works at Visible Corporation.", workspace="acme", repo="r",
            scope="repo",
        )

        set_current_user({"id": "usr_bob", "email": "bob@test", "role": "member"})
        job = svc.start_graph_index_job(workspace="acme", dry_run=False)
        deadline = time.time() + 5
        while job["state"] in {"queued", "running"} and time.time() < deadline:
            time.sleep(0.01)
            job = svc.graph_index_job(job["id"], workspace="acme")

        assert job["state"] == "completed"
        assert job["total_items"] == 1
        assert job["counts"]["memories_scanned"] == 1
        graph = repr(svc.graph(workspace="acme", backfill=False))
        assert "Hidden Corporation" not in graph
        assert "Visible Corporation" in graph
    finally:
        set_current_user(None)


def test_structured_extractor_metadata_populates_graph_without_regex_extractor():
    """llm_structured emits validated entity/relation hints; those should feed the
    graph directly even when the regex text graph extractor is disabled."""
    pytest.importorskip("pydantic")
    svc = MemoryService.create(":memory:", graph_extractor="none")
    svc.engine.extractor = StructuredLLMExtractor(_StructuredGraphLLM())
    svc.ingest("raw transcript blob", workspace="acme", scope="workspace")

    g = svc.graph(workspace="acme")
    id_by_label = {n["label"]: n["id"] for n in g["nodes"]}
    assert {"Engraphis", "SQLite"} <= set(id_by_label)
    assert {"from": id_by_label["Engraphis"], "to": id_by_label["SQLite"],
            "label": "stores_in", "layer": "semantic"} in g["edges"]


def test_graph_hides_edges_from_forgotten_memory():
    svc = MemoryService.create(":memory:", graph_extractor="regex")
    out = svc.remember("Alice Johnson works at Acme Corp.", workspace="acme",
                       scope="workspace")
    assert svc.graph(workspace="acme")["edges"]

    svc.forget(out["id"], workspace="acme")
    assert svc.graph(workspace="acme")["edges"] == []


def test_graph_lazy_backfills_structured_metadata_without_regex_extractor():
    svc = MemoryService.create(":memory:", graph_extractor="none")
    wid = svc.store.get_or_create_workspace("acme")
    svc.store.add_memory(MemoryRecord(
        id="", content="Engraphis stores memories in SQLite.",
        workspace_id=wid, scope=Scope.WORKSPACE, mtype=MemoryType.SEMANTIC,
        metadata={"entities": ["Engraphis", "SQLite"],
                  "relations": [{"source": "engraphis", "relation": "stores_in",
                                 "target": "SQLite"}]},
    ))
    g = svc.graph(workspace="acme")
    id_by_label = {n["label"]: n["id"] for n in g["nodes"]}
    assert {"Engraphis", "SQLite"} <= set(id_by_label)
    assert {"from": id_by_label["Engraphis"], "to": id_by_label["SQLite"],
            "label": "stores_in", "layer": "semantic"} in g["edges"]


def test_graph_lazy_backfills_preexisting_memories():
    """Memories written while extraction was OFF have no entities. When extraction
    is later enabled (an update), the first Graph-tab open backfills that
    workspace's graph from its existing memories — no manual migration."""
    svc = MemoryService.create(":memory:", graph_extractor="none")
    svc.remember("Alice Johnson works at Acme Corp.", workspace="acme",
                 scope="workspace")
    assert svc.graph(workspace="acme")["nodes"] == []      # extractor off -> no backfill

    svc.engine.graph_extractor = get_graph_extractor("regex")   # simulate the update
    labels = {n["label"] for n in svc.graph(workspace="acme")["nodes"]}
    assert "Alice Johnson" in labels and "Acme Corp" in labels


def test_graph_lazy_backfill_is_idempotent():
    """Re-opening the Graph tab must not duplicate entities."""
    svc = MemoryService.create(":memory:", graph_extractor="regex")
    svc.remember("Alice Johnson works at Acme Corp.", workspace="acme",
                 scope="workspace")
    first = svc.graph(workspace="acme")["stats"]["entities"]
    second = svc.graph(workspace="acme")["stats"]["entities"]
    assert first == second and first >= 2


def test_graph_hides_edges_before_their_validity_window():
    svc = MemoryService.create(":memory:")
    _seed_entities(
        svc, "acme",
        [("Alice", "person"), ("Acme Corp", "organization")],
        [("Alice", "Acme Corp", "works_at")])
    svc.store.conn.execute(
        "UPDATE edges SET valid_from=? WHERE id='edge0'", (10**12,))
    svc.store.conn.commit()

    assert svc.graph(workspace="acme")["edges"] == []


def test_forgetting_one_support_keeps_a_multi_source_edge_live():
    svc = MemoryService.create(":memory:")
    wid, ids = _seed_entities(
        svc, "acme",
        [("Alice", "person"), ("Acme Corp", "organization")], [])
    first = svc.store.add_memory(MemoryRecord(
        id="", content="Alice works at Acme Corp.", workspace_id=wid,
        scope=Scope.WORKSPACE))
    second = svc.store.add_memory(MemoryRecord(
        id="", content="Acme Corp employs Alice.", workspace_id=wid,
        scope=Scope.WORKSPACE))
    edge_id = svc.store.upsert_edge(Edge(
        id="", src=ids["Alice"], dst=ids["Acme Corp"], relation="works_at",
        workspace_id=wid))
    svc.store.add_edge_support(edge_id, {"memory_id": first})
    svc.store.add_edge_support(edge_id, {"memory_id": second})

    svc.store.invalidate_edges_for_memory(first)

    edges = svc.store.edges_in_scope(SearchFilter(workspace_id=wid))
    assert [edge.id for edge in edges] == [edge_id]
    assert edges[0].provenance["memory_ids"] == [second]

    svc.store.invalidate_edges_for_memory(second)
    assert svc.store.edges_in_scope(SearchFilter(workspace_id=wid)) == []


# ── caller-supplied graph metadata must not forge trusted provenance ───────────────
def test_client_supplied_entities_never_claim_structured_extractor_provenance():
    """metadata.entities/relations are how the *trusted* extractor
    (backends.extractor.StructuredLLMExtractor) hands MemoryEngine graph hints, fed
    with provenance.source="structured_extractor" (core/engine.py). remember() is
    reachable directly (MCP tool, HTTP route, dashboard) with caller-chosen metadata,
    so a caller setting the same keys must not inherit that trusted label for content
    the extractor never saw — the values are preserved (not silently dropped) but
    re-homed under an honestly-labeled key the engine's structured-graph check does
    not recognize, so no entity/edge is written under the trusted label at all."""
    svc = MemoryService.create(":memory:", graph_extractor="none")
    out = svc.remember(
        "Innocuous content.", workspace="acme", scope="workspace",
        metadata={
            "entities": ["Forged Entity"],
            "relations": [{"source": "Forged Entity", "relation": "controls",
                           "target": "Everything"}],
        },
    )
    rec = svc.store.get_memory(out["id"])
    assert "entities" not in rec.metadata and "relations" not in rec.metadata
    assert rec.metadata["client_supplied_graph"]["entities"] == ["Forged Entity"]
    assert rec.metadata["client_supplied_graph"]["source"] == "client_supplied"

    g = svc.graph(workspace="acme")
    assert g["nodes"] == [] and g["edges"] == []
    wid = svc.store.get_or_create_workspace("acme")
    edges = svc.store.edges_in_scope(SearchFilter(workspace_id=wid), limit=100)
    assert all(e.provenance.get("source") != "structured_extractor" for e in edges)


def test_client_supplied_structured_extraction_key_is_also_relabeled():
    """The third key _has_structured_graph_metadata checks — nested under
    metadata.structured_extraction rather than top-level — gets the same treatment."""
    svc = MemoryService.create(":memory:", graph_extractor="none")
    out = svc.ingest(
        "raw transcript blob", workspace="acme", scope="workspace",
        metadata={"structured_extraction": {"entities": ["Forged"], "relations": []}},
    )
    mid = out["facts"][0]["id"]
    rec = svc.store.get_memory(mid)
    assert "structured_extraction" not in rec.metadata
    assert (rec.metadata["client_supplied_graph"]["structured_extraction"]["entities"]
            == ["Forged"])
    assert svc.graph(workspace="acme")["nodes"] == []


def test_engine_itself_refuses_forged_graph_provenance_without_the_service():
    """Defense in depth: the mitigation above lives in service.py::_clean_metadata, so it
    only covers callers that route through the service. MemoryEngine is reachable
    directly — the sync apply path, consolidation, any future caller — and it is the
    engine that stamps provenance.source="structured_extractor". Going straight to the
    engine must not be a way around the label."""
    svc = MemoryService.create(":memory:", graph_extractor="none")
    wid = svc.store.get_or_create_workspace("acme")
    mid = svc.engine.remember(
        "Innocuous content.", workspace_id=wid, scope=Scope.WORKSPACE,
        metadata={"entities": ["Forged Entity"],
                  "relations": [{"source": "Forged Entity", "relation": "controls",
                                 "target": "Everything"}]},
    )

    rec = svc.store.get_memory(mid)
    assert "entities" not in rec.metadata and "relations" not in rec.metadata
    assert rec.metadata["client_supplied_graph"]["entities"] == ["Forged Entity"]
    assert rec.metadata["client_supplied_graph"]["source"] == "client_supplied"
    edges = svc.store.edges_in_scope(SearchFilter(workspace_id=wid), limit=100)
    assert all(e.provenance.get("source") != "structured_extractor" for e in edges)


class _EntitiesOnlyLLM:
    """Emits entities but no relations — so ``relations`` is the one graph-hint key the
    extractor does not vouch for on this write."""

    def extract_json(self, prompt, schema):
        return {"facts": [{"content": "Engraphis stores memories in SQLite.",
                           "entities": ["Engraphis", "SQLite"]}]}


def test_engine_ingest_argument_cannot_borrow_the_extractors_trusted_label():
    """The sharpest case: a real Extractor IS configured, so the trusted feed does run —
    but only for the keys the extractor itself produced. A hint key supplied through
    ingest()'s own ``metadata`` argument is demoted in that very same write, sharing the
    record with keys that keep the trusted label."""
    pytest.importorskip("pydantic")
    svc = MemoryService.create(":memory:", graph_extractor="none")
    svc.engine.extractor = StructuredLLMExtractor(_EntitiesOnlyLLM())
    wid = svc.store.get_or_create_workspace("acme")
    out = svc.engine.ingest(
        "raw transcript blob", workspace_id=wid, scope=Scope.WORKSPACE,
        metadata={"relations": [{"source": "Engraphis", "relation": "controls",
                                 "target": "Everything"}]},
    )

    rec = svc.store.get_memory(out["facts"][0]["id"])
    assert rec.metadata["entities"] == ["Engraphis", "SQLite"]   # extractor's: kept
    assert "relations" not in rec.metadata                       # caller's: demoted
    assert (rec.metadata["client_supplied_graph"]["relations"][0]["relation"]
            == "controls")
    assert rec.metadata["client_supplied_graph"]["source"] == "client_supplied"
    # the genuine hints still reached the graph…
    names = {r["name"] for r in svc.store.conn.execute(
        "SELECT name FROM entities WHERE workspace_id=?", (wid,))}
    assert {"Engraphis", "SQLite"} <= names
    # …and the forged relation produced no edge at all
    edges = svc.store.edges_in_scope(SearchFilter(workspace_id=wid), limit=100)
    assert all(e.relation != "controls" for e in edges)


def test_structured_extractor_metadata_still_populates_graph_when_genuine():
    """Regression guard for the fix above: the legitimate path — a configured
    Extractor's OWN ExtractedFact.metadata, never the caller's ingest() argument —
    must still feed the graph under the real "structured_extractor" label."""
    pytest.importorskip("pydantic")
    svc = MemoryService.create(":memory:", graph_extractor="none")
    svc.engine.extractor = StructuredLLMExtractor(_StructuredGraphLLM())
    svc.ingest("raw transcript blob", workspace="acme", scope="workspace")

    wid = svc.store.get_or_create_workspace("acme")
    edges = svc.store.edges_in_scope(SearchFilter(workspace_id=wid), limit=100)
    assert edges and all(e.provenance.get("source") == "structured_extractor"
                         for e in edges)


# ── graph(include_code=True) linked-memory lookups must be batched, not N+1 ────────
def test_graph_include_code_batches_linked_memory_lookups(monkeypatch):
    """Was one store.get_memory() call per code-linked memory (up to `limit`, <=5000)
    on every include_code=True request. Store.get_memories() batches it into one
    IN (...) query; this pins both correctness (same nodes surface) and the batching
    itself (get_memory() must not be called from this loop at all)."""
    svc = MemoryService.create(":memory:", graph_extractor="none")
    wid = svc.store.get_or_create_workspace("acme")
    rid = svc.store.get_or_create_repo(wid, "web")

    mem_ids = []
    for i in range(3):
        mid = svc.remember(f"Memory number {i}.", workspace="acme", repo="web",
                           scope="repo", resolve_conflicts=False)["id"]
        mem_ids.append(mid)
        symbol_id = svc.store.upsert_symbol(
            repo_id=rid, kind="function", name=f"fn{i}", fqname=f"fn{i}",
            file=f"f{i}.py", span="1:1-2:1", lang="python",
        )
        svc.store.link_memory_symbol(repo_id=rid, symbol_id=symbol_id, memory_id=mid)

    get_memory_calls = []
    real_get_memory = svc.store.get_memory

    def _tracked_get_memory(memory_id):
        get_memory_calls.append(memory_id)
        return real_get_memory(memory_id)

    monkeypatch.setattr(svc.store, "get_memory", _tracked_get_memory)

    g = svc.graph(workspace="acme", repo="web", include_code=True)

    assert get_memory_calls == []                          # batched, not per-row
    assert set(mem_ids) <= {n["id"] for n in g["nodes"]}
