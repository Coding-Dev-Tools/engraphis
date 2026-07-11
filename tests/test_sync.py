"""Cloud-sync tests — convergence, idempotency, and the untrusted-bundle boundary.

Fully offline: two ``:memory:`` engines stand in for two devices, a temp directory
stands in for the shared folder. No network, no model download (deterministic
hashing embedder + NumPy index, per AGENTS.md §7).
"""
from __future__ import annotations

import pytest

from engraphis.backends.sync_folder import FolderTransport, get_transport
from engraphis.core.engine import MemoryEngine
from engraphis.core.interfaces import MemoryRecord, MemoryType, Scope, SearchFilter
from engraphis.core.store import Store
from engraphis.core.sync import (
    MAX_CONTENT_CHARS,
    SYNC_FORMAT,
    SyncEngine,
    SyncError,
    _signature,
    dict_to_record,
    merge_record,
    record_to_dict,
)


# ── pure merge lattice (the convergence guarantees) ───────────────────────────

def test_merge_is_commutative_and_lww_by_version_key():
    a = MemoryRecord(id="mem_1", content="hello", last_access=100.0, ingested_at=10.0,
                     stability=2.0, access_count=3)
    b = MemoryRecord(id="mem_1", content="hello v2", last_access=200.0, ingested_at=10.0,
                     stability=1.0, access_count=5)
    m1, m2 = merge_record(a, b), merge_record(b, a)
    assert _signature(m1) == _signature(m2)          # order-independent
    assert m1.content == "hello v2"                  # higher last_access wins the label
    assert m1.stability == 2.0                       # lattice: max
    assert m1.access_count == 5                       # lattice: max


def test_merge_is_idempotent():
    a = MemoryRecord(id="mem_1", content="x", last_access=100.0, ingested_at=10.0)
    b = MemoryRecord(id="mem_1", content="x edited", last_access=150.0, ingested_at=10.0)
    m = merge_record(a, b)
    assert _signature(merge_record(m, b)) == _signature(m)
    assert _signature(merge_record(m, a)) == _signature(m)


def test_merge_commutes_even_on_identical_clock():
    # Same last_access AND ingested_at but different content: the content-hash tiebreak
    # must still make the winner order-independent (no divergence).
    a = MemoryRecord(id="mem_1", content="alpha", last_access=5.0, ingested_at=5.0)
    b = MemoryRecord(id="mem_1", content="bravo", last_access=5.0, ingested_at=5.0)
    assert _signature(merge_record(a, b)) == _signature(merge_record(b, a))


def test_invalidation_is_earliest_wins_and_sticky():
    a = MemoryRecord(id="mem_1", content="x", valid_to=500.0)
    b = MemoryRecord(id="mem_1", content="x", valid_to=300.0)
    assert merge_record(a, b).valid_to == 300.0          # earliest close wins
    live = MemoryRecord(id="mem_1", content="x", valid_to=None)
    assert merge_record(a, live).valid_to == 500.0       # a close is never resurrected


def test_reinforcement_and_pin_are_monotone():
    a = MemoryRecord(id="mem_1", content="x", stability=1.0, access_count=2,
                     last_access=10.0, pinned=False)
    b = MemoryRecord(id="mem_1", content="x", stability=9.0, access_count=1,
                     last_access=20.0, pinned=True)
    m = merge_record(a, b)
    assert m.stability == 9.0 and m.access_count == 2      # max of each
    assert m.last_access == 20.0 and m.pinned is True      # max / OR


def test_serialization_roundtrip_preserves_signature():
    rec = MemoryRecord(id="mem_1", content="hi", title="T", keywords=["b", "a"],
                       metadata={"k": 1}, pinned=True, stability=3.5,
                       mtype=MemoryType.EPISODIC, scope=Scope.WORKSPACE, access_count=4)
    r2 = dict_to_record(record_to_dict(rec))
    assert r2 is not None
    assert r2.mtype == MemoryType.EPISODIC and r2.scope == Scope.WORKSPACE
    assert r2.pinned is True and r2.keywords == ["b", "a"]
    assert _signature(r2) == _signature(rec)


# ── untrusted-bundle boundary (memory-poisoning threat, SECURITY.md) ──────────

def test_apply_rejects_bad_header():
    se = SyncEngine(Store(":memory:"))
    with pytest.raises(SyncError):
        se.apply_bundle({"format": "not-engraphis"})
    with pytest.raises(SyncError):
        se.apply_bundle({"format": SYNC_FORMAT, "version": 999})
    with pytest.raises(SyncError):
        se.apply_bundle("i am not a dict")


def test_apply_clamps_and_drops_bad_rows():
    store = Store(":memory:")
    se = SyncEngine(store)
    bundle = {
        "format": SYNC_FORMAT, "version": 1, "workspace_name": "w", "repos": {},
        "memories": [
            {"id": "mem_ok", "content": "x" * (MAX_CONTENT_CHARS + 5_000)},  # clamped
            {"id": "", "content": "y"},        # rejected: no id
            {"content": "z"},                   # rejected: no id
            "not-a-dict",                       # rejected: not an object
        ],
        "mem_links": [],
    }
    rep = se.apply_bundle(bundle)
    assert rep["added"] == 1 and rep["rejected"] == 3
    got = store.get_memory("mem_ok")
    assert got is not None and len(got.content) == MAX_CONTENT_CHARS  # truncated, not trusted


def test_apply_is_idempotent_on_replay():
    store = Store(":memory:")
    se = SyncEngine(store)
    bundle = {
        "format": SYNC_FORMAT, "version": 1, "workspace_name": "w", "repos": {},
        "memories": [{"id": "mem_a", "content": "one"}, {"id": "mem_b", "content": "two"}],
        "mem_links": [{"a": "mem_a", "b": "mem_b", "relation": "related"}],
    }
    first = se.apply_bundle(bundle)
    assert first["added"] == 2 and first["links_added"] == 1
    second = se.apply_bundle(bundle)
    assert second["added"] == 0 and second["updated"] == 0
    assert second["unchanged"] == 2 and second["links_added"] == 0


def test_dry_run_writes_nothing():
    store = Store(":memory:")
    se = SyncEngine(store)
    bundle = {"format": SYNC_FORMAT, "version": 1, "workspace_name": "w", "repos": {},
              "memories": [{"id": "mem_a", "content": "one"}], "mem_links": []}
    rep = se.apply_bundle(bundle, dry_run=True)
    assert rep["added"] == 1 and rep["dry_run"] is True
    assert store.get_memory("mem_a") is None                  # nothing persisted
    assert store.conn.execute("SELECT COUNT(*) c FROM workspaces").fetchone()["c"] == 0


# ── two-device integration over the folder transport ──────────────────────────

def _live(engine: MemoryEngine, wid: str) -> list:
    return engine.store.list_memories(SearchFilter(workspace_id=wid))


def _contents(engine: MemoryEngine, wid: str) -> set:
    return {m.content for m in _live(engine, wid)}


def test_folder_transport_is_a_valid_synctransport(tmp_path):
    t = get_transport("folder", root=str(tmp_path / "share"))
    assert isinstance(t, FolderTransport)
    t.push("bundle-x.json", b"{}")
    t.push("README.txt", b"ignore me")           # non-json ignored
    names = t.list_names()
    assert names == ["bundle-x.json"]
    assert t.pull() == [("bundle-x.json", b"{}")]


def test_two_devices_converge(tmp_path):
    a = MemoryEngine.create(":memory:")
    b = MemoryEngine.create(":memory:")
    wa = a.store.get_or_create_workspace("acme")
    wb = b.store.get_or_create_workspace("acme")
    a.remember("Postgres is the primary datastore", workspace_id=wa, scope=Scope.WORKSPACE)
    b.remember("The API rate limit is 100 req/s", workspace_id=wb, scope=Scope.WORKSPACE)

    root = str(tmp_path / "share")
    sa = SyncEngine(a.store, embedder=a.embedder, vector_index=a.index)
    sb = SyncEngine(b.store, embedder=b.embedder, vector_index=b.index)

    sa.sync(get_transport("folder", root=root), wa)   # A publishes
    sb.sync(get_transport("folder", root=root), wb)   # B publishes + pulls A
    sa.sync(get_transport("folder", root=root), wa)   # A pulls B

    both = {"Postgres is the primary datastore", "The API rate limit is 100 req/s"}
    assert _contents(a, wa) == both
    assert _contents(b, wb) == both
    # memory ids are global: the same ULIDs exist on both devices
    assert {m.id for m in _live(a, wa)} == {m.id for m in _live(b, wb)}


def test_resync_is_a_noop(tmp_path):
    a = MemoryEngine.create(":memory:")
    b = MemoryEngine.create(":memory:")
    wa = a.store.get_or_create_workspace("acme")
    wb = b.store.get_or_create_workspace("acme")
    a.remember("shared fact", workspace_id=wa, scope=Scope.WORKSPACE)

    root = str(tmp_path / "share")
    sa = SyncEngine(a.store, embedder=a.embedder, vector_index=a.index)
    sb = SyncEngine(b.store, embedder=b.embedder, vector_index=b.index)
    sa.sync(get_transport("folder", root=root), wa)
    sb.sync(get_transport("folder", root=root), wb)          # B gets the fact
    rep = sb.sync(get_transport("folder", root=root), wb)    # second pull: nothing new
    assert rep["totals"]["added"] == 0 and rep["totals"]["updated"] == 0
    assert rep["totals"]["unchanged"] >= 1


def test_invalidation_propagates_across_devices(tmp_path):
    a = MemoryEngine.create(":memory:")
    b = MemoryEngine.create(":memory:")
    wa = a.store.get_or_create_workspace("acme")
    wb = b.store.get_or_create_workspace("acme")
    mid = a.remember("temporary fact", workspace_id=wa, scope=Scope.WORKSPACE)

    root = str(tmp_path / "share")
    sa = SyncEngine(a.store, embedder=a.embedder, vector_index=a.index)
    sb = SyncEngine(b.store, embedder=b.embedder, vector_index=b.index)
    sa.sync(get_transport("folder", root=root), wa)
    sb.sync(get_transport("folder", root=root), wb)
    assert mid in {m.id for m in _live(b, wb)}               # B has it, live

    a.forget(mid)                                            # bi-temporal close on A
    sa.sync(get_transport("folder", root=root), wa)          # A republishes
    sb.sync(get_transport("folder", root=root), wb)          # B pulls the invalidation

    assert mid not in {m.id for m in _live(b, wb)}           # gone from B's live set
    closed = b.store.get_memory(mid)
    assert closed is not None and closed.valid_to is not None  # preserved, not deleted


# ── security regressions (from the sync ingest-path audit) ────────────────────

def test_cross_workspace_id_is_confined():
    """A bundle cannot overwrite a memory that lives in a workspace it isn't syncing —
    even if the attacker knows the (non-secret-by-design) memory id."""
    store = Store(":memory:")
    priv = store.get_or_create_workspace("private")
    store.add_memory(MemoryRecord(id="mem_secret", content="salary is 100k",
                                  workspace_id=priv, scope=Scope.WORKSPACE))
    se = SyncEngine(store)
    poison = {"format": SYNC_FORMAT, "version": 1, "workspace_name": "shared",
              "device_id": "dev_attacker", "repos": {},
              "memories": [{"id": "mem_secret", "content": "HACKED"}], "mem_links": []}
    rep = se.apply_bundle(poison)                       # applying into 'shared'
    assert rep["rejected"] == 1 and rep["added"] == 0 and rep["updated"] == 0
    assert store.get_memory("mem_secret").content == "salary is 100k"  # untouched


def test_hostile_infinity_bundle_does_not_crash_sync(tmp_path):
    """A JSON ``Infinity`` bundle is rejected without aborting the whole sync run."""
    a = MemoryEngine.create(":memory:")
    wa = a.store.get_or_create_workspace("acme")
    a.remember("good fact", workspace_id=wa, scope=Scope.WORKSPACE)
    root = tmp_path / "share"
    root.mkdir()
    (root / "bundle-dev_evil.json").write_text(
        '{"format":"engraphis-sync","version":1,"device_id":"dev_evil",'
        '"workspace_name":"acme","repos":{},'
        '"memories":[{"id":"mem_x","content":"y","last_access":Infinity}],"mem_links":[]}')
    sa = SyncEngine(a.store, embedder=a.embedder, vector_index=a.index)
    report = sa.sync(get_transport("folder", root=str(root)), wa)   # must NOT raise
    assert any("dev_evil" in x.get("bundle", "") for x in report["applied"] if "error" in x)
    assert {m.content for m in _live(a, wa)} == {"good fact"}       # store intact


def test_nonfinite_numeric_fields_are_clamped():
    store = Store(":memory:")
    se = SyncEngine(store)
    bundle = {"format": SYNC_FORMAT, "version": 1, "workspace_name": "w", "repos": {},
              "memories": [{"id": "mem_p", "content": "p", "stability": float("inf"),
                            "importance": float("nan"), "last_access": float("inf")}],
              "mem_links": []}
    assert se.apply_bundle(bundle)["added"] == 1                    # no crash
    got = store.get_memory("mem_p")
    import math as _m
    assert _m.isfinite(got.stability) and got.stability <= 1e6
    assert _m.isfinite(got.importance) and 0.0 <= got.importance <= 1.0
    assert got.last_access is None or _m.isfinite(got.last_access)


def test_control_and_ansi_chars_are_stripped():
    store = Store(":memory:")
    se = SyncEngine(store)
    bundle = {"format": SYNC_FORMAT, "version": 1, "workspace_name": "w", "repos": {},
              "memories": [{"id": "mem_c", "content": "safe\x1b[31mred\x00 end",
                            "title": "t\x07itle"}], "mem_links": []}
    se.apply_bundle(bundle)
    got = store.get_memory("mem_c")
    assert "\x1b" not in got.content and "\x00" not in got.content and "\x07" not in got.title
    assert "red" in got.content and "end" in got.content           # visible text preserved


def test_secret_memories_are_not_exported():
    store = Store(":memory:")
    w = store.get_or_create_workspace("w")
    store.add_memory(MemoryRecord(id="mem_pub", content="public",
                                  workspace_id=w, scope=Scope.WORKSPACE))
    store.add_memory(MemoryRecord(id="mem_sec", content="secret",
                                  workspace_id=w, scope=Scope.WORKSPACE, sensitivity="secret"))
    bundle = SyncEngine(store).export_bundle(w)
    ids = {m["id"] for m in bundle["memories"]}
    assert "mem_pub" in ids and "mem_sec" not in ids


def test_allowed_workspaces_enforcement():
    store = Store(":memory:")
    se = SyncEngine(store, allowed_workspaces=frozenset(["allowed_ws"]))
    bundle = {"format": SYNC_FORMAT, "version": 1, "workspace_name": "disallowed_ws", "repos": {},
              "memories": [{"id": "mem_a", "content": "hello"}], "mem_links": []}
    with pytest.raises(SyncError, match="not authorized for sync"):
        se.apply_bundle(bundle)

    bundle_allowed = {"format": SYNC_FORMAT, "version": 1, "workspace_name": "allowed_ws", "repos": {},
                      "memories": [{"id": "mem_a", "content": "hello"}], "mem_links": []}
    rep = se.apply_bundle(bundle_allowed)
    assert rep["added"] == 1


def test_sync_auditing_for_adds_updates_and_links():
    store = Store(":memory:")
    se = SyncEngine(store)

    # 1. Test audit logging for added memories
    bundle = {
        "format": SYNC_FORMAT, "version": 1, "workspace_name": "w", "repos": {},
        "memories": [{"id": "mem_a", "content": "hello", "last_access": 100.0}], "mem_links": []
    }
    se.apply_bundle(bundle)
    audits = store.conn.execute("SELECT action, target, detail FROM audit").fetchall()
    assert len(audits) == 1
    assert audits[0]["action"] == "sync_add"
    assert audits[0]["target"] == "mem_a"

    # 2. Test audit logging for updated memories
    bundle_update = {
        "format": SYNC_FORMAT, "version": 1, "workspace_name": "w", "repos": {},
        "memories": [{"id": "mem_a", "content": "hello updated", "last_access": 200.0}], "mem_links": []
    }
    se.apply_bundle(bundle_update)
    audits = store.conn.execute("SELECT action, target FROM audit ORDER BY ts ASC").fetchall()
    assert len(audits) == 2
    assert audits[1]["action"] == "sync_overwrite"
    assert audits[1]["target"] == "mem_a"

    # 3. Test audit logging for memory links
    bundle_link = {
        "format": SYNC_FORMAT, "version": 1, "workspace_name": "w", "repos": {},
        "memories": [{"id": "mem_b", "content": "another fact"}],
        "mem_links": [{"a": "mem_a", "b": "mem_b", "relation": "related"}]
    }
    se.apply_bundle(bundle_link)
    audits = store.conn.execute("SELECT action, target FROM audit ORDER BY ts ASC").fetchall()
    assert len(audits) == 4  # +1 for mem_b add, +1 for link
    assert audits[2]["action"] == "sync_add"
    assert audits[2]["target"] == "mem_b"
    assert audits[3]["action"] == "sync_link"
    assert audits[3]["target"] == "mem_a"


def test_deeply_nested_json_does_not_crash_sync_decoding(tmp_path):
    a = MemoryEngine.create(":memory:")
    wa = a.store.get_or_create_workspace("acme")
    root = tmp_path / "share"
    root.mkdir()

    # Construct a deeply nested JSON string
    nested = '{"format":"engraphis-sync","version":1,"device_id":"dev_nested","workspace_name":"acme","repos":{},"memories":' + ('[' * 1000) + ']' * 1000 + '}'
    (root / "bundle-dev_nested.json").write_text(nested)

    sa = SyncEngine(a.store, embedder=a.embedder, vector_index=a.index)
    # The sync run should catch the RecursionError/ValueError and log it as "unreadable" without crashing the entire run
    report = sa.sync(get_transport("folder", root=str(root)), wa)
    assert report["totals"]["added"] == 0
    assert any(x.get("bundle") == "bundle-dev_nested.json" and x.get("error") == "unreadable" for x in report["applied"])


