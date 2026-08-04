"""Sync tombstones: secure-erase and unpin must propagate across devices."""
from __future__ import annotations

import pytest

from engraphis.core.interfaces import MemoryRecord, Scope
from engraphis.core.store import Store
from engraphis.core.sync import SyncEngine, merge_record


def _two_devices():
    """Two independent stores standing in for two synced devices."""
    a, b = Store(":memory:"), Store(":memory:")
    aw = a.get_or_create_workspace("w")
    bw = b.get_or_create_workspace("w")
    return a, b, aw, bw


def test_secure_erase_propagates_tombstone_so_peer_does_not_resurrect():
    """Device A erases a memory; B's next bundle must not re-add it."""
    a, b, aw, bw = _two_devices()
    syncer_a, syncer_b = SyncEngine(a), SyncEngine(b)

    # A writes, B receives it.
    mid = a.add_memory(MemoryRecord(id="", content="secret plan", workspace_id=aw,
                                    scope=Scope.WORKSPACE))
    bundle = syncer_a.export_bundle(aw)
    report = syncer_b.apply_bundle(bundle, into_workspace="w")
    assert report["added"] == 1

    # A erases the memory (secure erase) — local hard delete.
    a.secure_erase_memory(mid)
    assert a.get_memory(mid) is None

    # The next bundle from A carries the tombstone.
    bundle2 = syncer_a.export_bundle(aw)
    assert any(t["id"] == mid for t in bundle2["tombstones"])
    erased = next(t for t in bundle2["tombstones"] if t["id"] == mid)
    assert erased["workspace_id"] == aw

    # B applies the bundle: the tombstone is terminal — the memory must NOT be added,
    # even though B still holds a live row (B's row predates the tombstone).
    report2 = syncer_b.apply_bundle(bundle2, into_workspace="w")
    assert report2["tombstones_applied"] == 1
    assert b.get_memory(mid) is None

    # And B's own tombstone is now recorded, so a later bundle re-exported from B
    # cannot resurrect it either.
    assert any(t["id"] == mid for t in syncer_b.export_bundle(bw)["tombstones"])


def test_unpin_propagates_and_beats_a_peers_stale_pin(monkeypatch):
    """A local unpin must beat a peer's stale pinned=True via the marker lattice."""
    a, b, aw, bw = _two_devices()

    pinned = MemoryRecord(id="mem_pin", content="pinned note", workspace_id=aw,
                          scope=Scope.WORKSPACE, pinned=True, pinned_at=1.0)
    a.add_memory(pinned)

    # Peer B receives it pinned.
    syncer_a, syncer_b = SyncEngine(a), SyncEngine(b)
    syncer_b.apply_bundle(syncer_a.export_bundle(aw), into_workspace="w")
    assert b.get_memory("mem_pin").pinned is True

    # Use a controlled clock: ordering is the contract, not wall-clock resolution.
    import engraphis.core.store as store_module
    current = [10.0]
    monkeypatch.setattr(store_module, "now_ts", lambda: current[0])
    a.set_pinned("mem_pin", False)
    row = a.conn.execute("SELECT unpinned_at FROM memories WHERE id='mem_pin'").fetchone()
    assert row["unpinned_at"] == 10.0

    # B's stale bundle still says pinned=True with the OLD pinned_at; A's unpin bundle
    # carries the newer unpinned_at. Applying A's bundle must leave B unpinned.
    syncer_b.apply_bundle(syncer_a.export_bundle(aw), into_workspace="w")
    assert b.get_memory("mem_pin").pinned is False


def test_pin_lattice_keeps_legacy_pinned_true_without_marker():
    """A pinned=True row with no pinned_at marker (legacy) is pinned since epoch and
    only an explicit later unpin can clear it."""
    a = MemoryRecord(id="mem_1", content="x", pinned=True, pinned_at=None, unpinned_at=None)
    b = MemoryRecord(id="mem_1", content="x", pinned=False, pinned_at=None, unpinned_at=None)
    m = merge_record(a, b)
    assert m.pinned is True

    # An explicit unpin marker beats the epoch pin.
    c = MemoryRecord(id="mem_1", content="x", pinned=False,
                     pinned_at=0.0, unpinned_at=50.0)
    m2 = merge_record(m, c)

    assert m2.pinned is False


def test_new_id_after_secure_erase_is_not_blocked_by_old_tombstone():
    """A fresh id remains syncable after an unrelated id is securely erased."""
    a, b, aw, bw = _two_devices()
    syncer_a, syncer_b = SyncEngine(a), SyncEngine(b)

    mid = a.add_memory(MemoryRecord(id="", content="old fact", workspace_id=aw,
                                    scope=Scope.WORKSPACE))
    a.secure_erase_memory(mid)
    # A new, different memory written after the erase is unaffected.
    mid2 = a.add_memory(MemoryRecord(id="", content="new fact", workspace_id=aw,
                                     scope=Scope.WORKSPACE))
    bundle = syncer_a.export_bundle(aw)
    report = syncer_b.apply_bundle(bundle, into_workspace="w")
    assert report["added"] == 1
    assert b.get_memory(mid2) is not None
    assert b.get_memory(mid) is None


def test_repo_export_keeps_repo_tombstones_in_the_selected_repo():
    a, _b, aw, _bw = _two_devices()
    repo_a = a.get_or_create_repo(aw, "a")
    repo_b = a.get_or_create_repo(aw, "b")
    mid_a = a.add_memory(MemoryRecord(
        id="", content="repo a", workspace_id=aw, repo_id=repo_a, scope=Scope.REPO,
    ))
    mid_b = a.add_memory(MemoryRecord(
        id="", content="repo b", workspace_id=aw, repo_id=repo_b, scope=Scope.REPO,
    ))
    a.secure_erase_memory(mid_a)
    a.secure_erase_memory(mid_b)

    bundle = SyncEngine(a).export_bundle(aw, repo_id=repo_a)
    tombstone_ids = {item["id"] for item in bundle["tombstones"]}
    assert mid_a in tombstone_ids
    assert mid_b not in tombstone_ids
    assert all(item["repo_id"] in (None, repo_a) for item in bundle["tombstones"])



def test_repin_after_unpin_beats_the_unpin_marker(monkeypatch):
    """A later pin must converge after an earlier unpin on another device."""
    a, b, aw, _bw = _two_devices()
    pinned = MemoryRecord(id="mem_repin", content="note", workspace_id=aw,
                          scope=Scope.WORKSPACE, pinned=True, pinned_at=1.0)
    a.add_memory(pinned)
    syncer_a, syncer_b = SyncEngine(a), SyncEngine(b)
    syncer_b.apply_bundle(syncer_a.export_bundle(aw), into_workspace="w")

    import engraphis.core.store as store_module
    current = [10.0]
    monkeypatch.setattr(store_module, "now_ts", lambda: current[0])
    a.set_pinned("mem_repin", False)
    current[0] = 20.0
    a.set_pinned("mem_repin", True)
    row = a.conn.execute(
        "SELECT pinned_at, unpinned_at FROM memories WHERE id='mem_repin'"
    ).fetchone()
    assert row["pinned_at"] == 20.0
    assert row["unpinned_at"] == 10.0

    syncer_b.apply_bundle(syncer_a.export_bundle(aw), into_workspace="w")
    assert b.get_memory("mem_repin").pinned is True


def test_same_id_written_after_secure_erase_stays_tombstoned():
    """Secure erase is terminal even if a stale peer reuses the deleted id."""
    a, b, aw, _bw = _two_devices()
    syncer_a, syncer_b = SyncEngine(a), SyncEngine(b)
    mid = a.add_memory(MemoryRecord(id="", content="old", workspace_id=aw,
                                    scope=Scope.WORKSPACE))
    syncer_b.apply_bundle(syncer_a.export_bundle(aw), into_workspace="w")
    a.secure_erase_memory(mid)
    a.add_memory(MemoryRecord(id=mid, content="reused", workspace_id=aw,
                              scope=Scope.WORKSPACE))

    report = syncer_b.apply_bundle(syncer_a.export_bundle(aw), into_workspace="w")
    assert report["rejected"] >= 1
    assert b.get_memory(mid) is None


def test_scoped_tombstone_cannot_delete_same_id_in_another_workspace():
    """A workspace-scoped sync erase never reaches a row in another workspace."""
    a, b, aw, _bw = _two_devices()
    foreign_ws = b.get_or_create_workspace("other")
    foreign = MemoryRecord(id="shared-id", content="foreign", workspace_id=foreign_ws,
                           scope=Scope.WORKSPACE)
    b.add_memory(foreign)
    a.add_memory_tombstone("shared-id", deleted_at=1.0, workspace_id=aw)
    a.conn.commit()

    report = SyncEngine(b).apply_bundle(
        SyncEngine(a).export_bundle(aw), into_workspace="w"
    )
    assert report["rejected"] >= 1
    assert b.get_memory("shared-id").content == "foreign"


def test_dry_run_applies_bundle_tombstone_to_rejection_simulation():
    """Dry-run reports the same terminal tombstone rejection without mutating."""
    a, b, aw, bw = _two_devices()
    mid = "dry-run-id"
    a.add_memory_tombstone(mid, deleted_at=1.0, workspace_id=aw)
    a.add_memory(MemoryRecord(id=mid, content="reused", workspace_id=aw,
                              scope=Scope.WORKSPACE))
    bundle = SyncEngine(a).export_bundle(aw)

    report = SyncEngine(b).apply_bundle(bundle, into_workspace="w", dry_run=True)
    assert report["rejected"] >= 1
    assert b.get_memory(mid) is None
    assert b.list_memory_tombstones(bw) == []


def test_tombstone_order_and_duplicate_events_are_safe():
    """A tombstone wins regardless of row ordering and duplicate delivery."""
    store = Store(":memory:")
    workspace = store.get_or_create_workspace("w")
    syncer = SyncEngine(store)
    bundle = {
        "format": "engraphis-sync",
        "version": 2,
        "device_id": "peer",
        "workspace_name": "w",
        "repos": {},
        # Deliberately put the row before its tombstone: apply must stage deletion
        # before rows so a stale payload cannot re-create the erased id.
        "memories": [{"id": "erased", "content": "stale payload",
                      "workspace_id": "remote", "scope": "workspace"}],
        "tombstones": [
            {"id": "erased", "deleted_at": 20.0, "device": "late"},
            {"id": "erased", "deleted_at": 10.0, "device": "early"},
            {"id": "", "deleted_at": 5.0, "device": "malformed"},
            {"id": "bad-time", "deleted_at": "not-a-number", "device": "malformed"},
        ],
        "mem_links": [],
    }

    first = syncer.apply_bundle(bundle, into_workspace="w")
    assert first["tombstones_applied"] == 1
    assert first["rejected"] == 1
    assert store.get_memory("erased") is None
    tombstones = store.list_memory_tombstones(workspace)
    assert tombstones == [{
        "id": "erased", "deleted_at": 10.0, "device": "early",
        "workspace_id": workspace, "repo_id": None,
    }]

    # Replaying the same events cannot create a row or move the earliest marker.
    second = syncer.apply_bundle(bundle, into_workspace="w")
    assert second["tombstones_applied"] == 0
    assert second["rejected"] == 1
    assert store.get_memory("erased") is None
    assert store.list_memory_tombstones(workspace) == tombstones



def test_store_tombstone_scope_conflict_and_repo_filter_are_explicit():
    store = Store(":memory:")
    workspace = store.get_or_create_workspace("w")
    repo_a = store.get_or_create_repo(workspace, "repo-a")
    repo_b = store.get_or_create_repo(workspace, "repo-b")
    store.add_memory_tombstone(
        "scoped", deleted_at=10.0, workspace_id=workspace, repo_id=repo_a,
    )
    with pytest.raises(ValueError, match="repository scope"):
        store.add_memory_tombstone(
            "scoped", deleted_at=1.0, workspace_id=workspace, repo_id=repo_b,
        )
    store.add_memory_tombstone(
        "scoped", deleted_at=20.0, workspace_id=workspace,
    )
    marker = store.list_memory_tombstones(workspace, repo_id=repo_a)
    assert marker[0]["repo_id"] is None
    with pytest.raises(ValueError, match="requires workspace"):
        store.list_memory_tombstones(repo_id=repo_a)


def test_repo_tombstone_cannot_delete_same_id_in_a_sibling_repo():
    """A repo-A erase must not remove a same-id row owned by repo B."""
    a, b, aw, bw = _two_devices()
    source_repo = a.get_or_create_repo(aw, "repo-a")
    b.get_or_create_repo(bw, "repo-a")
    destination_repo = b.get_or_create_repo(bw, "repo-b")
    shared_id = "same-id-different-repo"
    a.add_memory_tombstone(
        shared_id, deleted_at=1.0, workspace_id=aw, repo_id=source_repo,
    )
    b.add_memory(MemoryRecord(
        id=shared_id, content="repo B fact", workspace_id=bw,
        repo_id=destination_repo, scope=Scope.REPO,
    ))

    bundle = SyncEngine(a).export_bundle(aw, repo_id=source_repo)
    report = SyncEngine(b).apply_bundle(bundle, into_workspace="w")

    assert report["tombstones_applied"] == 0
    assert report["rejected"] >= 1
    assert b.get_memory(shared_id).content == "repo B fact"
    assert b.list_memory_tombstones(bw) == []


def test_legacy_repo_less_tombstone_stays_global_against_sibling_reuse():
    """A legacy marker must not be narrowed to the repo of the erased local row."""
    store = Store(":memory:")
    workspace = store.get_or_create_workspace("w")
    repo_b = store.get_or_create_repo(workspace, "repo-b")
    store.add_memory(MemoryRecord(
        id="legacy-global", content="repo B fact", workspace_id=workspace,
        repo_id=repo_b, scope=Scope.REPO,
    ))

    SyncEngine(store).apply_bundle({
        "format": "engraphis-sync", "version": 1, "workspace_name": "w",
        "repos": {}, "memories": [],
        "tombstones": [{"id": "legacy-global", "deleted_at": 1.0}],
        "mem_links": [],
    }, into_workspace="w")

    assert store.get_memory("legacy-global") is None
    marker = store.list_memory_tombstones(workspace)
    assert marker and marker[0]["repo_id"] is None

    repo_a = store.get_or_create_repo(workspace, "repo-a")
    report = SyncEngine(store).apply_bundle({
        "format": "engraphis-sync", "version": 2, "workspace_name": "w",
        "repos": {"remote-a": "repo-a"},
        "memories": [{
            "id": "legacy-global", "content": "reused in repo A",
            "scope": "repo", "repo_id": "remote-a",
        }],
        "tombstones": [], "mem_links": [],
    }, into_workspace="w")

    assert report["rejected"] == 1
    assert store.get_memory("legacy-global") is None
    assert store.get_or_create_repo(workspace, "repo-a") == repo_a


def test_same_id_tombstones_keep_sibling_repository_scopes_independent():
    """An earlier sibling marker must not hide a later marker for the local repo."""
    store = Store(":memory:")
    workspace = store.get_or_create_workspace("w")
    repo_b = store.get_or_create_repo(workspace, "repo-b")
    store.add_memory(MemoryRecord(
        id="scoped-sibling", content="repo B fact", workspace_id=workspace,
        repo_id=repo_b, scope=Scope.REPO,
    ))

    report = SyncEngine(store).apply_bundle({
        "format": "engraphis-sync", "version": 2, "workspace_name": "w",
        "repos": {"remote-a": "repo-a", "remote-b": "repo-b"},
        "memories": [],
        "tombstones": [
            {"id": "scoped-sibling", "deleted_at": 1.0,
             "repo_id": "remote-a"},
            {"id": "scoped-sibling", "deleted_at": 2.0,
             "repo_id": "remote-b"},
        ],
        "mem_links": [],
    }, into_workspace="w")

    assert report["tombstones_applied"] == 1
    assert store.get_memory("scoped-sibling") is None
    markers = store.list_memory_tombstones(workspace)
    assert len(markers) == 1
    assert markers[0]["id"] == "scoped-sibling"
    assert markers[0]["deleted_at"] == 2.0
    assert markers[0]["device"]
    assert markers[0]["workspace_id"] == workspace
    assert markers[0]["repo_id"] == repo_b
