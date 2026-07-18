"""Cloud-sync tests — convergence, idempotency, and the untrusted-bundle boundary.

Fully offline: two ``:memory:`` engines stand in for two devices, a temp directory
stands in for the shared folder. No network, no model download (deterministic
hashing embedder + NumPy index, per AGENTS.md §7).
"""
from __future__ import annotations

import json
import os
import time

import pytest

from engraphis.backends import sync_folder
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
    _version_key,
    dict_to_record,
    inherit_store_defaults,
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
    bundle = {
        "format": SYNC_FORMAT, "version": 1, "workspace_name": "w", "repos": {},
        "memories": [{"id": "mem_a", "content": "one"},
                     {"id": "mem_b", "content": "two"}],
        "mem_links": [{"a": "mem_a", "b": "mem_b", "relation": "related"}],
    }
    rep = se.apply_bundle(bundle, dry_run=True)
    assert rep["added"] == 2 and rep["links_added"] == 1 and rep["dry_run"] is True
    assert store.get_memory("mem_a") is None
    assert store.conn.execute("SELECT COUNT(*) c FROM workspaces").fetchone()["c"] == 0
    assert store.conn.execute("SELECT COUNT(*) c FROM audit").fetchone()["c"] == 0


def test_apply_rejects_memory_with_undeclared_remote_repo():
    store = Store(":memory:")
    bundle = {
        "format": SYNC_FORMAT, "version": 1, "workspace_name": "w", "repos": {},
        "memories": [{"id": "mem_a", "content": "one", "repo_id": "remote_repo"}],
        "mem_links": [],
    }

    report = SyncEngine(store).apply_bundle(bundle)

    assert report["rejected"] == 1 and report["added"] == 0
    assert store.get_memory("mem_a") is None


def test_dry_run_resolves_remote_repo_by_name_without_mutating():
    store = Store(":memory:")
    wid = store.get_or_create_workspace("w")
    local_repo = store.get_or_create_repo(wid, "api")
    bundle = {
        "format": SYNC_FORMAT, "version": 1, "workspace_name": "w",
        "repos": {"remote_repo": "api"},
        "memories": [{"id": "mem_a", "content": "one", "repo_id": "remote_repo"}],
        "mem_links": [],
    }

    report = SyncEngine(store).apply_bundle(
        bundle, only_repo_id=local_repo, dry_run=True)

    assert report["added"] == 1 and report["rejected"] == 0
    assert store.get_memory("mem_a") is None


def test_bundle_links_must_reference_accepted_bundle_memories():
    store = Store(":memory:")
    wid = store.get_or_create_workspace("w")
    store.add_memory(MemoryRecord(id="mem_a", content="one", workspace_id=wid))
    store.add_memory(MemoryRecord(id="mem_b", content="two", workspace_id=wid))
    bundle = {
        "format": SYNC_FORMAT, "version": 1, "workspace_name": "w", "repos": {},
        "memories": [],
        "mem_links": [{"a": "mem_a", "b": "mem_b", "relation": "injected"}],
    }

    report = SyncEngine(store).apply_bundle(bundle)

    assert report["links_added"] == 0
    assert not store.has_link("mem_a", "mem_b", relation="injected")


def test_repo_scoped_export_includes_only_that_repo_metadata():
    store = Store(":memory:")
    se = SyncEngine(store)
    wid = store.get_or_create_workspace("w")
    keep = store.get_or_create_repo(wid, "keep")
    drop = store.get_or_create_repo(wid, "drop")
    store.add_memory(MemoryRecord(id="mem_keep", content="x", workspace_id=wid,
                                  repo_id=keep, scope=Scope.REPO, mtype=MemoryType.SEMANTIC))
    bundle = se.export_bundle(wid, repo_id=keep)
    assert bundle["repos"] == {keep: "keep"}
    assert drop not in bundle["repos"]


# ── two-device integration over the folder transport ──────────────────────────

def _live(engine: MemoryEngine, wid: str) -> list:
    return engine.store.list_memories(SearchFilter(workspace_id=wid))


def _contents(engine: MemoryEngine, wid: str) -> set:
    return {m.content for m in _live(engine, wid)}


def test_folder_transport_is_a_valid_synctransport(tmp_path):
    t = get_transport("folder", root=str(tmp_path / "share"))
    assert isinstance(t, FolderTransport)
    t.push("bundle-x.json", b"{}")
    (tmp_path / "share" / "README.txt").write_bytes(b"ignore me")  # non-json ignored
    names = t.list_names()
    assert names == ["bundle-x.json"]
    assert t.pull() == [("bundle-x.json", b"{}")]
    with pytest.raises(ValueError, match="name is invalid"):
        t.push("../escape.json", b"{}")


def test_folder_transport_rejects_a_bundle_it_would_skip_on_pull(
        tmp_path, monkeypatch):
    monkeypatch.setattr(sync_folder, "MAX_BUNDLE_BYTES", 3)
    transport = FolderTransport(str(tmp_path / "share"))
    with pytest.raises(ValueError, match="transport limit"):
        transport.push("bundle-x.json", b"1234")
    assert transport.list_names() == []


def test_folder_transport_bounds_count_total_and_ignores_symlinks(tmp_path, monkeypatch):
    root = tmp_path / "share"
    root.mkdir()
    for name in ("bundle-a.json", "bundle-b.json", "bundle-c.json"):
        (root / name).write_bytes(b"12")
    monkeypatch.setattr(sync_folder, "MAX_BUNDLES", 2)
    monkeypatch.setattr(sync_folder, "MAX_TOTAL_PULL_BYTES", 3)
    transport = FolderTransport(str(root))
    assert transport.list_names() == ["bundle-a.json", "bundle-b.json"]
    assert transport.pull() == [("bundle-a.json", b"12")]

    outside = tmp_path / "outside.json"
    outside.write_bytes(b'{"secret":true}')
    link = root / "bundle-0-link.json"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        return
    assert "bundle-0-link.json" not in transport.list_names()


def test_folder_transport_rejects_file_swapped_after_enumeration(tmp_path, monkeypatch):
    root = tmp_path / "share"
    root.mkdir()
    target = root / "bundle-a.json"
    target.write_bytes(b'{"safe":true}')
    replacement = root / "replacement.tmp"
    replacement.write_bytes(b'{"outside":true}')
    original_open = os.open
    swapped = False

    def swap_then_open(path, flags):
        nonlocal swapped
        if not swapped and os.path.abspath(path) == os.path.abspath(target):
            os.replace(replacement, target)
            swapped = True
        return original_open(path, flags)

    monkeypatch.setattr(sync_folder.os, "open", swap_then_open)
    assert FolderTransport(str(root)).pull() == []
    assert swapped is True


def test_folder_transport_push_never_writes_through_planted_symlinks(tmp_path):
    """The shared folder is hostile on the WRITE side too: a peer who pre-plants
    symlinks at the temp or destination paths must not be able to redirect our
    own push into an arbitrary local file (PR #19 review follow-up)."""
    root = tmp_path / "share"
    root.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"precious")
    try:
        # The legacy predictable temp path and the destination itself.
        os.symlink(victim, root / "bundle-a.json.tmp")
        os.symlink(victim, root / "bundle-a.json")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable (e.g. unprivileged Windows)")

    transport = FolderTransport(str(root))
    transport.push("bundle-a.json", b'{"mine":true}')

    # The victim file is untouched and the destination is now a real file.
    assert victim.read_bytes() == b"precious"
    dest = root / "bundle-a.json"
    assert not dest.is_symlink()
    assert dest.read_bytes() == b'{"mine":true}'
    # No temp litter beyond the attacker's own planted link.
    leftovers = [p.name for p in root.iterdir()
                 if p.name.endswith(".tmp") and not p.is_symlink()]
    assert leftovers == []


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


def test_disallowed_workspace_cannot_be_exported_or_pushed(monkeypatch):
    store = Store(":memory:")
    disallowed = store.get_or_create_workspace("disallowed")
    store.add_memory(MemoryRecord(id="mem_private", content="private",
                                  workspace_id=disallowed, scope=Scope.WORKSPACE))
    syncer = SyncEngine(store, allowed_workspaces=frozenset({"allowed"}))
    local_calls = []
    network_calls = []

    def track_listing(*args, **kwargs):
        local_calls.append("list")
        return []

    def track_serialization(record):
        local_calls.append("serialize")
        return record_to_dict(record)

    class TrackingTransport:
        def push(self, name, data):
            network_calls.append(("push", name, data))

        def pull(self):
            network_calls.append(("pull",))
            return []

    monkeypatch.setattr(store, "list_memories", track_listing)
    monkeypatch.setattr("engraphis.core.sync.record_to_dict", track_serialization)

    with pytest.raises(SyncError, match="not authorized for sync"):
        syncer.export_bundle(disallowed)
    with pytest.raises(SyncError, match="not authorized for sync"):
        syncer.sync(TrackingTransport(), disallowed)

    assert local_calls == []
    assert network_calls == []


@pytest.mark.parametrize("dry_run", [False, True])
def test_repo_restricted_apply_rejects_forged_repo_for_existing_memory(dry_run):
    store = Store(":memory:")
    workspace = store.get_or_create_workspace("w")
    allowed_repo = store.get_or_create_repo(workspace, "allowed")
    other_repo = store.get_or_create_repo(workspace, "other")
    store.add_memory(MemoryRecord(id="mem_other", content="original",
                                  workspace_id=workspace, repo_id=other_repo,
                                  scope=Scope.REPO, last_access=1.0))
    bundle = {
        "format": SYNC_FORMAT, "version": 1, "workspace_name": "w",
        "repos": {"remote_allowed": "allowed"},
        "memories": [{"id": "mem_other", "content": "forged",
                      "repo_id": "remote_allowed", "last_access": 100.0}],
        "mem_links": [],
    }

    report = SyncEngine(store).apply_bundle(
        bundle, only_repo_id=allowed_repo, dry_run=dry_run)

    existing = store.get_memory("mem_other")
    assert report["rejected"] == 1
    assert report["updated"] == 0
    assert existing.content == "original"
    assert existing.repo_id == other_repo


@pytest.mark.parametrize("outside_endpoint", ["a", "b"])
def test_repo_restricted_links_reject_either_endpoint_outside_repo(outside_endpoint):
    store = Store(":memory:")
    workspace = store.get_or_create_workspace("w")
    allowed_repo = store.get_or_create_repo(workspace, "allowed")
    other_repo = store.get_or_create_repo(workspace, "other")
    repo_by_id = {
        "mem_a": other_repo if outside_endpoint == "a" else allowed_repo,
        "mem_b": other_repo if outside_endpoint == "b" else allowed_repo,
    }
    for memory_id, repo_id in repo_by_id.items():
        store.add_memory(MemoryRecord(id=memory_id, content=memory_id,
                                      workspace_id=workspace, repo_id=repo_id,
                                      scope=Scope.REPO))
    bundle = {
        "format": SYNC_FORMAT, "version": 1, "workspace_name": "w",
        "repos": {"remote_allowed": "allowed"},
        "memories": [
            {"id": "mem_a", "content": "mem_a", "repo_id": "remote_allowed"},
            {"id": "mem_b", "content": "mem_b", "repo_id": "remote_allowed"},
        ],
        "mem_links": [{"a": "mem_a", "b": "mem_b", "relation": "forged"}],
    }

    report = SyncEngine(store).apply_bundle(bundle, only_repo_id=allowed_repo)

    assert report["rejected"] == 1
    assert report["links_added"] == 0
    assert not store.has_link("mem_a", "mem_b", relation="forged")


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


def test_remote_bundle_cannot_overwrite_or_downgrade_local_secret():
    store = Store(":memory:")
    workspace = store.get_or_create_workspace("w")
    store.add_memory(MemoryRecord(
        id="mem_secret", content="local-only credential rotation note",
        workspace_id=workspace, scope=Scope.WORKSPACE,
        sensitivity="secret", last_access=1.0,
    ))
    bundle = {
        "format": SYNC_FORMAT,
        "version": 1,
        "workspace_name": "w",
        "device_id": "dev_hostile",
        "repos": {},
        "memories": [{
            "id": "mem_secret",
            "content": "remote overwrite",
            "sensitivity": "normal",
            "last_access": time.time() + 86_400,
        }],
        "mem_links": [],
    }

    report = SyncEngine(store).apply_bundle(bundle)

    assert report["rejected"] == 1 and report["updated"] == 0
    memory = store.get_memory("mem_secret")
    assert memory.content == "local-only credential rotation note"
    assert memory.sensitivity == "secret"
    assert SyncEngine(store).export_bundle(workspace)["memories"] == []


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
        # Links are accepted only between memories present in the bundle and accepted by
        # this apply pass. Include mem_a as an unchanged bundle memory so the link is
        # legitimate, while mem_b is newly added.
        "memories": [{"id": "mem_a", "content": "hello updated", "last_access": 200.0},
                     {"id": "mem_b", "content": "another fact"}],
        "mem_links": [{
            "a": "mem_a", "b": "mem_b", "relation": "related",
            "layer": "causal", "reason": "same deployment path",
        }]
    }
    se.apply_bundle(bundle_link)
    audits = store.conn.execute("SELECT action, target FROM audit ORDER BY ts ASC").fetchall()
    assert len(audits) == 4  # +1 for mem_b add, +1 for link
    assert audits[2]["action"] == "sync_add"
    assert audits[2]["target"] == "mem_b"
    assert audits[3]["action"] == "sync_link"
    assert audits[3]["target"] == "mem_a"
    link = store.get_links("mem_a")[0]
    assert link["layer"] == "causal"
    assert link["reason"] == "same deployment path"


def test_link_metadata_merge_converges_independent_of_bundle_order():
    memories = [
        {"id": "mem_a", "content": "one"},
        {"id": "mem_b", "content": "two"},
    ]
    semantic = {
        "format": SYNC_FORMAT, "version": 1, "workspace_name": "w", "repos": {},
        "memories": memories,
        "mem_links": [{
            "a": "mem_a", "b": "mem_b", "relation": "related",
            "layer": "semantic", "reason": "zeta",
        }],
    }
    causal = {
        "format": SYNC_FORMAT, "version": 1, "workspace_name": "w", "repos": {},
        "memories": memories,
        "mem_links": [{
            "a": "mem_a", "b": "mem_b", "relation": "related",
            "layer": "causal", "reason": "alpha",
        }],
    }

    left = Store(":memory:")
    right = Store(":memory:")
    left_sync = SyncEngine(left)
    right_sync = SyncEngine(right)
    left_sync.apply_bundle(semantic)
    left_sync.apply_bundle(causal)
    right_sync.apply_bundle(causal)
    right_sync.apply_bundle(semantic)

    left_link = left.get_links("mem_a")[0]
    right_link = right.get_links("mem_a")[0]
    assert (left_link["layer"], left_link["reason"]) == ("causal", "zeta")
    assert (right_link["layer"], right_link["reason"]) == ("causal", "zeta")


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


# ── regression: merge_record must be idempotent (ingested_at is LWW, not a lattice) ──

def test_merge_takes_the_winners_ingested_at():
    """``ingested_at`` is in _LWW_FIELDS and is the version key's second component.

    Merging it as a min-lattice made _version_key(merged) < _version_key(winner), so a
    replayed bundle re-ran LWW from a lowered key and fell through to the content-hash
    tiebreak — silently reverting the later edit.
    """
    a = MemoryRecord(id="mem_1", content="old", last_access=100.0, ingested_at=50.0)
    b = MemoryRecord(id="mem_1", content="new", last_access=200.0, ingested_at=10.0)

    merged = merge_record(a, b)

    assert merged.content == "new"                    # higher last_access wins
    assert merged.ingested_at == b.ingested_at        # ...and brings its own ingested_at
    assert _version_key(merged) == _version_key(b)    # merged IS the winner, key and all


@pytest.mark.parametrize(
    ("la_a", "ing_a", "la_b", "ing_b"),
    [
        (100.0, 50.0, 200.0, 10.0),    # incoming wins on last_access, lower ingested_at
        (200.0, 10.0, 100.0, 50.0),    # local wins on last_access, lower ingested_at
        (100.0, 10.0, 100.0, 50.0),    # tie on last_access, decided by ingested_at
        (100.0, 50.0, 100.0, 50.0),    # full tie, decided by the content hash
        (None, None, 100.0, 10.0),     # null clocks on one side
    ],
)
def test_merge_is_idempotent_for_unequal_ingested_at(la_a, ing_a, la_b, ing_b):
    a = MemoryRecord(id="mem_1", content="alpha", last_access=la_a, ingested_at=ing_a)
    b = MemoryRecord(id="mem_1", content="beta", last_access=la_b, ingested_at=ing_b)

    once = merge_record(a, b)
    # merge(merge(a, b), b) == merge(a, b), in both argument orders (the docstring's claim)
    assert _signature(merge_record(once, b)) == _signature(once)
    assert _signature(merge_record(b, once)) == _signature(once)
    assert _signature(merge_record(once, a)) == _signature(once)
    # ...and the merge result carries the winner's version key exactly
    winner = a if _version_key(a) >= _version_key(b) else b
    assert _version_key(once) == _version_key(winner)


@pytest.mark.parametrize("remote_content", ["first", "zzz", "aaa", "payload", "0"])
def test_replaying_a_bundle_reports_all_unchanged(remote_content):
    """apply_bundle's contract: 'applying the same bundle twice reports the second as
    all-unchanged'. Existing coverage only used equal ingested_at values, which hid the
    min-lattice bug entirely.

    Setup: the LOCAL row wins last-writer-wins (tie on last_access, higher ingested_at),
    while the remote carries a LOWER ingested_at. Under the min-lattice the merged row
    kept the remote's lower ingested_at, so the merged version key dropped below the
    remote's and the next replay was decided by the content-hash tiebreak — reverting the
    local content whenever the remote's hash happened to sort higher. Sweeping several
    remote payloads exercises both sides of that comparison.
    """
    store = Store(":memory:")
    wid = store.get_or_create_workspace("w")
    syncer = SyncEngine(store)
    store.add_memory(MemoryRecord(id="mem_a", content="local", workspace_id=wid,
                                  last_access=100.0, ingested_at=90.0, valid_from=1.0))
    # valid_from is set explicitly here, exactly as export_bundle/record_to_dict emit it.
    # A bundle that OMITS it converges too, but only because apply_bundle inherits
    # store-defaulted fields from the existing row — see the dedicated test below.
    bundle = {
        "format": SYNC_FORMAT, "version": 1, "workspace_name": "w", "repos": {},
        "memories": [
            {"id": "mem_a", "content": remote_content, "valid_from": 1.0,
             "last_access": 100.0, "ingested_at": 5.0},
            {"id": "mem_b", "content": "second", "valid_from": 1.0,
             "last_access": 50.0, "ingested_at": 10.0},
        ],
        "mem_links": [{"a": "mem_a", "b": "mem_b", "relation": "related"}],
    }

    first = syncer.apply_bundle(bundle)
    winner = store.get_memory("mem_a").content
    second = syncer.apply_bundle(bundle)
    third = syncer.apply_bundle(bundle)

    # The merged row keeps the LWW winner's ingested_at, not min(local, remote).
    assert store.get_memory("mem_a").ingested_at == 90.0
    assert winner == "local"                                # local won LWW
    assert first["unchanged"] == 1 and first["added"] == 1   # mem_a no-op, mem_b new
    assert second["added"] == second["updated"] == 0
    assert second["unchanged"] == 2
    assert third["updated"] == 0
    assert store.get_memory("mem_a").content == winner       # never reverts on replay


# ── regression: a bundle that OMITS a store-defaulted field must still converge ──

def _valid_from_less_bundle(content):
    """One row that omits ``valid_from`` but DOES supply ``last_access``/``ingested_at``,
    so a replay ties on both ordered components of the version key and lands squarely on
    the content-hash tiebreak — the only place the omission can decide anything."""
    return {
        "format": SYNC_FORMAT, "version": 1, "workspace_name": "w", "repos": {},
        "memories": [{"id": "mem_a", "content": content,
                      "last_access": 100.0, "ingested_at": 90.0}],
        "mem_links": [],
    }


# Contents for which the *incoming* (valid_from-less) label hashes ABOVE the stored one at
# valid_from=1000.0 — i.e. the ones that made the un-inherited tiebreak actually flip. Held
# fixed rather than left to the wall clock so this pins the bug on every run, not ~half.
_FLIPPING_CONTENTS = ["first", "zzz", "0", "alpha", "m", "beta", "gamma"]


@pytest.mark.parametrize("content", _FLIPPING_CONTENTS)
def test_bundle_omitting_valid_from_never_rewrites_the_stored_default(content):
    """A hand-crafted bundle row without ``valid_from`` must not rewrite itself forever.

    ``dict_to_record`` leaves the field ``None`` and ``Store.add_memory`` then stamps it
    with ``now()``. On replay the stored and incoming labels differed *only* in
    ``valid_from``; with ``last_access`` and ``ingested_at`` tied, the version key fell
    through to the content-hash tiebreak, so the row was reported ``updated`` and rewritten
    with a FRESH ``valid_from`` — which changed the hash, so it flipped again next round.
    Unbounded write amplification plus a ``sync_overwrite`` audit row per sync round,
    reachable from an untrusted bundle (SECURITY.md — memory poisoning).

    The local ``valid_from`` is seeded explicitly so the tiebreak is a pure function of the
    test data; a rewrite would stamp a real ``now()``, nowhere near 1000.0.
    """
    store = Store(":memory:")
    wid = store.get_or_create_workspace("w")
    syncer = SyncEngine(store)
    store.add_memory(MemoryRecord(id="mem_a", content=content, workspace_id=wid,
                                  last_access=100.0, ingested_at=90.0, valid_from=1000.0))
    bundle = _valid_from_less_bundle(content)

    for _ in range(6):
        report = syncer.apply_bundle(bundle)
        assert report["updated"] == 0 and report["added"] == 0   # never rewrites itself
        assert report["unchanged"] == 1
        row = store.get_memory("mem_a")
        assert row.valid_from == 1000.0                          # ...and never moves
        assert row.content == content
        assert row.ingested_at == 90.0 and row.last_access == 100.0

    # the write amplification was visible as one sync_overwrite audit row per round
    spam = store.conn.execute(
        "SELECT COUNT(*) c FROM audit WHERE action='sync_overwrite'").fetchone()["c"]
    assert spam == 0


@pytest.mark.parametrize("content", ["first", "zzz", "aaa", "payload", "0", "alpha", "m"])
def test_bundle_omitting_valid_from_converges_when_it_created_the_row(content):
    """End-to-end shape of the same vector: the bundle CREATES the row (so the store, not
    the test, supplies the defaulted ``valid_from``), then is replayed. Everything after
    the first round must be all-unchanged and the stored default must never move."""
    store = Store(":memory:")
    store.get_or_create_workspace("w")
    syncer = SyncEngine(store)
    bundle = _valid_from_less_bundle(content)

    first = syncer.apply_bundle(bundle)
    assert first["added"] == 1
    pinned = store.get_memory("mem_a").valid_from
    assert pinned is not None                       # the store defaulted it on write

    for _ in range(5):
        report = syncer.apply_bundle(bundle)
        assert report["added"] == 0
        assert report["updated"] == 0
        assert report["unchanged"] == 1
        assert store.get_memory("mem_a").valid_from == pinned
    spam = store.conn.execute(
        "SELECT COUNT(*) c FROM audit WHERE action='sync_overwrite'").fetchone()["c"]
    assert spam == 0


def test_incoming_valid_from_still_wins_when_genuinely_supplied():
    """The inheritance must only fill fields the bundle OMITTED — a real, newer
    ``valid_from`` still has to win last-writer-wins (and then stay converged)."""
    store = Store(":memory:")
    wid = store.get_or_create_workspace("w")
    syncer = SyncEngine(store)
    store.add_memory(MemoryRecord(id="mem_a", content="local", workspace_id=wid,
                                  last_access=100.0, ingested_at=90.0, valid_from=1.0))
    bundle = {
        "format": SYNC_FORMAT, "version": 1, "workspace_name": "w", "repos": {},
        "memories": [{"id": "mem_a", "content": "remote", "valid_from": 5000.0,
                      "last_access": 200.0, "ingested_at": 90.0}],   # newer last_access
        "mem_links": [],
    }

    first = syncer.apply_bundle(bundle)

    assert first["updated"] == 1
    row = store.get_memory("mem_a")
    assert row.valid_from == 5000.0 and row.content == "remote"
    # ...and the applied state is then stable
    for _ in range(3):
        assert syncer.apply_bundle(bundle)["unchanged"] == 1
        assert store.get_memory("mem_a").valid_from == 5000.0


def test_inherit_store_defaults_fills_only_omitted_fields():
    """Unit-level contract: exactly the fields Store.add_memory defaults from the server
    clock (valid_from / ingested_at / last_access) are inherited when omitted. valid_to and
    expired_at are NOT — there ``None`` is a real, persistable 'still valid' value that the
    earliest-non-null lattice already handles."""
    existing = MemoryRecord(id="mem_1", content="a", valid_from=1.0, ingested_at=2.0,
                            last_access=3.0, valid_to=4.0, expired_at=5.0)
    incoming = MemoryRecord(id="mem_1", content="b")

    inherit_store_defaults(existing, incoming)

    assert (incoming.valid_from, incoming.ingested_at, incoming.last_access) == (1.0, 2.0, 3.0)
    assert incoming.valid_to is None and incoming.expired_at is None
    assert incoming.content == "b"                   # descriptive fields untouched

    supplied = MemoryRecord(id="mem_1", content="b", valid_from=99.0,
                            ingested_at=98.0, last_access=97.0)
    inherit_store_defaults(existing, supplied)
    assert (supplied.valid_from, supplied.ingested_at, supplied.last_access) == (99.0, 98.0, 97.0)


# ── regression: apply_bundle must not be N+1 with a commit per row ────────────

def _bundle(n, *, links=()):
    return {
        "format": SYNC_FORMAT, "version": 1, "workspace_name": "w", "repos": {},
        "memories": [{"id": "mem_%d" % i, "content": "c%d" % i, "last_access": 1.0,
                      "valid_from": 1.0} for i in range(n)],
        "mem_links": list(links),
    }


def test_apply_bundle_commits_per_batch_not_per_row(monkeypatch):
    from engraphis.core import store as store_mod
    from engraphis.core import sync as sync_mod

    store = Store(":memory:")
    store.get_or_create_workspace("w")          # pre-create so its commit isn't counted
    syncer = SyncEngine(store)
    monkeypatch.setattr(sync_mod, "APPLY_BATCH", 2)
    commits = []
    real_commit = store_mod._SerializedConnection.commit

    def spy(self):
        commits.append(1)
        return real_commit(self)

    monkeypatch.setattr(store_mod._SerializedConnection, "commit", spy)

    report = syncer.apply_bundle(_bundle(5))

    assert report["added"] == 5
    # 3 (ceil(5/2) memory batches) + 1 (final links commit). The old per-row path paid a
    # commit per add_memory AND one per audit row — 10 for the same bundle.
    assert len(commits) == 4


def test_apply_bundle_uses_one_batched_lookup_instead_of_get_memory_per_row(monkeypatch):
    store = Store(":memory:")
    syncer = SyncEngine(store)
    calls = []
    monkeypatch.setattr(store, "get_memory",
                        lambda mid: calls.append(mid))          # must never be reached

    report = syncer.apply_bundle(_bundle(20))

    assert report["added"] == 20
    assert calls == []


def test_apply_bundle_sees_a_duplicate_id_within_one_batch():
    """The batched existence lookup must write through, or the second copy of an id in
    the same bundle would merge against a stale pre-write row."""
    store = Store(":memory:")
    bundle = {
        "format": SYNC_FORMAT, "version": 1, "workspace_name": "w", "repos": {},
        "memories": [
            {"id": "mem_dup", "content": "first", "last_access": 10.0},
            {"id": "mem_dup", "content": "second", "last_access": 20.0},
        ],
        "mem_links": [],
    }

    report = SyncEngine(store).apply_bundle(bundle)

    assert report["added"] == 1                       # the 2nd is an update, not an add
    assert report["updated"] == 1
    assert store.get_memory("mem_dup").content == "second"


def test_apply_bundle_failure_keeps_committed_batches_and_frees_the_connection(monkeypatch):
    """Preserve the old partial-apply semantics: a failure part-way through must not
    silently roll back the rows that already applied, and must never leave the shared
    connection pinned in an open transaction (that would stall every other thread)."""
    from engraphis.core import sync as sync_mod

    store = Store(":memory:")
    syncer = SyncEngine(store)
    monkeypatch.setattr(sync_mod, "APPLY_BATCH", 2)
    real_write = syncer._write

    def exploding_write(rec, *, commit=True):
        if rec.id == "mem_4":
            raise RuntimeError("disk on fire")
        return real_write(rec, commit=commit)

    monkeypatch.setattr(syncer, "_write", exploding_write)

    with pytest.raises(RuntimeError, match="disk on fire"):
        syncer.apply_bundle(_bundle(6))

    assert store.get_memory("mem_0") is not None      # committed batches survive
    assert store.get_memory("mem_1") is not None
    assert store.get_memory("mem_5") is None          # never reached
    assert store.conn.in_transaction is False         # no dangling pinned transaction
    store.create_workspace("still-usable")            # the connection is not deadlocked


# ── regression: one bad bundle must not kill the rest of the sync round ───────

class _FlakyTransport:
    """Mimics RelayTransport.pull(): a generator that raises part-way through the round
    (a relay 404 on a bundle deleted mid-round, an oversized blob, ...)."""

    def __init__(self, *bundles, fail_after=1):
        self.bundles = bundles
        self.fail_after = fail_after
        self.pushed = []

    def push(self, name, data):
        self.pushed.append(name)

    def pull(self):
        for i, (name, data) in enumerate(self.bundles):
            if i == self.fail_after:
                raise RuntimeError("relay request failed (404): %s" % name)
            yield name, data


def _peer_bundle(device, mem_id):
    return json.dumps({
        "format": SYNC_FORMAT, "version": 1, "device_id": device,
        "workspace_name": "w", "repos": {},
        "memories": [{"id": mem_id, "content": "from %s" % device, "last_access": 5.0}],
        "mem_links": [],
    }).encode("utf-8")


def test_sync_round_survives_a_transport_failure_mid_round():
    store = Store(":memory:")
    wid = store.get_or_create_workspace("w")
    transport = _FlakyTransport(
        ("bundle-peer1.json", _peer_bundle("dev_peer1", "mem_p1")),
        ("bundle-peer2.json", _peer_bundle("dev_peer2", "mem_p2")),
        fail_after=1,
    )

    result = SyncEngine(store).sync(transport, wid)   # must NOT raise

    assert store.get_memory("mem_p1") is not None     # the good bundle still applied
    assert result["totals"]["added"] == 1
    # The round is explicitly NOT a success: bundles were dropped.
    assert result["complete"] is False
    assert len(result["errors"]) == 1
    assert "transport" in result["errors"][0]["error"]
    assert result["peers_applied"] == 1


def test_sync_round_reports_incomplete_when_a_bundle_is_refused():
    """Fail-closed is preserved: a bundle apply_bundle refuses is still refused, and the
    round must not report success just because the other bundles landed."""
    store = Store(":memory:")
    wid = store.get_or_create_workspace("w")
    bad = json.dumps({"format": SYNC_FORMAT, "version": 99, "device_id": "dev_bad",
                      "workspace_name": "w", "repos": {},
                      "memories": [], "mem_links": []}).encode("utf-8")

    class _Transport:
        def push(self, name, data):
            pass

        def pull(self):
            return [("bundle-peer1.json", _peer_bundle("dev_peer1", "mem_p1")),
                    ("bundle-bad.json", bad)]

    result = SyncEngine(store).sync(_Transport(), wid)

    assert store.get_memory("mem_p1") is not None
    assert result["complete"] is False
    assert result["peers_applied"] == 1
    assert any("unsupported bundle version" in e["error"] for e in result["errors"])


def test_sync_round_is_complete_when_every_bundle_applies():
    store = Store(":memory:")
    wid = store.get_or_create_workspace("w")

    class _Transport:
        def push(self, name, data):
            pass

        def pull(self):
            return [("bundle-peer1.json", _peer_bundle("dev_peer1", "mem_p1")),
                    ("bundle-peer2.json", _peer_bundle("dev_peer2", "mem_p2"))]

    result = SyncEngine(store).sync(_Transport(), wid)

    assert result["complete"] is True
    assert result["errors"] == []
    assert result["peers_applied"] == 2
    assert result["totals"]["added"] == 2
