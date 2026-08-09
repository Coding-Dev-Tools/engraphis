"""Sync tombstones: secure-erase and unpin must propagate across devices."""
from __future__ import annotations

import json
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


class _CaptureTransport:
    def __init__(self):
        self.payloads = []

    def pull(self):
        return []

    def push(self, _name, data):
        self.payloads.append(data)

    def list_names(self):
        return []


def _push_bundle(syncer, workspace_id, *, repo_id=None):
    transport = _CaptureTransport()
    report = syncer.sync(transport, workspace_id, repo_id=repo_id)
    assert report["complete"] is True
    assert len(transport.payloads) == 1
    return json.loads(transport.payloads[0])


def test_secure_erase_propagates_tombstone_so_peer_does_not_resurrect():
    """Device A erases a memory; B's next bundle must not re-add it."""
    a, b, aw, bw = _two_devices()
    syncer_a, syncer_b = SyncEngine(a), SyncEngine(b)

    # A writes, B receives it.
    mid = a.add_memory(MemoryRecord(id="", content="secret plan", workspace_id=aw,
                                    scope=Scope.WORKSPACE))
    bundle = _push_bundle(syncer_a, aw)
    report = syncer_b.apply_bundle(bundle, into_workspace="w")
    assert report["added"] == 1

    # A erases the memory (secure erase) — local hard delete.
    a.secure_erase_memory(mid)
    assert a.get_memory(mid) is None

    # The next bundle from A carries the tombstone.
    bundle2 = _push_bundle(syncer_a, aw)
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


@pytest.mark.parametrize("protection", ["approved", "secret", "session"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_untrusted_tombstone_cannot_erase_protected_local_memory(
        protection, dry_run):
    store = Store(":memory:")
    workspace = store.get_or_create_workspace("w")
    record = MemoryRecord(
        id="mem_protected",
        content="local protected payload",
        workspace_id=workspace,
        scope=Scope.SESSION if protection == "session" else Scope.WORKSPACE,
        sensitivity="secret" if protection == "secret" else "normal",
        provenance=(
            {"source": "human", "trusted": True, "review_state": "approved"}
            if protection == "approved"
            else {"source": "sync", "trusted": False, "review_state": "pending"}
        ),
    )
    store.add_memory(record)
    assert store.conn.execute(
        "SELECT 1 FROM mem_fts WHERE id=?", (record.id,)
    ).fetchone() is not None
    bundle = {
        "format": "engraphis-sync",
        "version": 1,
        "device_id": "hostile-peer",
        "workspace_name": "w",
        "repos": {},
        "memories": [],
        "mem_links": [],
        "tombstones": [{
            "id": record.id,
            "deleted_at": 10.0,
            "device": "hostile-peer",
            "export_class": "remote_erasure",
        }],
    }

    report = SyncEngine(store).apply_bundle(
        bundle, into_workspace="w", dry_run=dry_run
    )

    assert report["rejected"] == 1
    assert report["tombstones_applied"] == 0
    assert store.get_memory(record.id) is not None
    assert store.list_memory_tombstones(workspace) == []
    assert store.conn.execute(
        "SELECT 1 FROM mem_fts WHERE id=?", (record.id,)
    ).fetchone() is not None
    audit = store.conn.execute(
        "SELECT detail FROM audit WHERE action='sync_trust_conflict'"
    ).fetchone()
    if dry_run:
        assert audit is None
    else:
        assert audit is not None
        assert audit["detail"] == "peer erasure ignored because local record is protected"
        assert "local protected payload" not in audit["detail"]


def test_workspace_export_includes_only_remote_erasure_tombstones(monkeypatch):
    store = Store(":memory:")
    workspace = store.get_or_create_workspace("w")
    monkeypatch.setattr(
        store,
        "list_memory_tombstones",
        lambda *_args, **_kwargs: [
            {"id": "mem_private", "deleted_at": 1.0,
             "export_class": "never_export"},
            {"id": "mem_shared", "deleted_at": 2.0,
             "export_class": "remote_erasure"},
        ],
    )

    exported = SyncEngine(store).export_bundle(workspace)

    assert exported["tombstones"] == [{
        "id": "mem_shared",
        "deleted_at": 2.0,
        "export_class": "remote_erasure",
    }]


@pytest.mark.parametrize(
    "export_class",
    [pytest.param(None, id="missing"), "never_export", "unknown"],
)
def test_imported_tombstone_requires_remote_erasure_classification(export_class):
    store = Store(":memory:")
    workspace = store.get_or_create_workspace("w")
    store.add_memory(MemoryRecord(
        id="mem_local",
        content="ordinary local payload",
        workspace_id=workspace,
        scope=Scope.WORKSPACE,
        provenance={"source": "sync", "trusted": False},
    ))
    tombstone = {
        "id": "mem_local",
        "deleted_at": 1.0,
        "device": "peer",
    }
    if export_class is not None:
        tombstone["export_class"] = export_class

    report = SyncEngine(store).apply_bundle({
        "format": "engraphis-sync",
        "version": 1,
        "device_id": "peer",
        "workspace_name": "w",
        "repos": {},
        "memories": [],
        "mem_links": [],
        "tombstones": [tombstone],
    }, into_workspace="w")

    assert report["rejected"] == 1
    assert store.get_memory("mem_local") is not None
    assert store.list_memory_tombstones(workspace) == []


def test_peer_cannot_upgrade_local_never_export_tombstone():
    store = Store(":memory:")
    workspace = store.get_or_create_workspace("w")
    store.add_memory_tombstone(
        "mem_private",
        deleted_at=1.0,
        workspace_id=workspace,
        export_class="never_export",
    )
    store.conn.commit()

    report = SyncEngine(store).apply_bundle({
        "format": "engraphis-sync",
        "version": 1,
        "device_id": "peer",
        "workspace_name": "w",
        "repos": {},
        "memories": [],
        "mem_links": [],
        "tombstones": [{
            "id": "mem_private",
            "deleted_at": 2.0,
            "device": "peer",
            "export_class": "remote_erasure",
        }],
    }, into_workspace="w")

    assert report["rejected"] == 1
    assert store.list_memory_tombstones(workspace)[0]["export_class"] == "never_export"
    assert SyncEngine(store).export_bundle(workspace)["tombstones"] == []


def test_secure_erase_classifies_private_and_shared_tombstones():
    store = Store(":memory:")
    workspace = store.get_or_create_workspace("w")
    records = (
        MemoryRecord(
            id="mem_shared",
            content="shared",
            workspace_id=workspace,
            scope=Scope.WORKSPACE,
        ),
        MemoryRecord(
            id="mem_secret",
            content="secret",
            workspace_id=workspace,
            scope=Scope.WORKSPACE,
            sensitivity="secret",
        ),
        MemoryRecord(
            id="mem_previously_shared",
            content="shared before local reclassification",
            workspace_id=workspace,
            scope=Scope.WORKSPACE,
        ),
        MemoryRecord(
            id="mem_session",
            content="session-local",
            workspace_id=workspace,
            scope=Scope.SESSION,
        ),
    )
    for record in records:
        store.add_memory(record)

    class CaptureTransport:
        def __init__(self):
            self.pushed = []

        def pull(self):
            return []

        def push(self, name, data):
            self.pushed.append((name, data))

        def list_names(self):
            return []

    transport = CaptureTransport()
    report = SyncEngine(store).sync(transport, workspace)
    assert report["complete"] is True
    assert {item.id for item in records if item.scope == Scope.WORKSPACE
            and item.sensitivity != "secret"} == {
        item["id"] for item in SyncEngine(store).export_bundle(workspace)["memories"]
    }
    assert store.get_memory_sync_export("mem_shared") is not None
    assert store.get_memory_sync_export("mem_previously_shared") is not None
    assert store.get_memory_sync_export("mem_secret") is None
    assert store.get_memory_sync_export("mem_session") is None

    store.advance_memory_modified_hlc("mem_previously_shared", commit=False)
    store.conn.execute(
        "UPDATE memories SET sensitivity='secret' "
        "WHERE id='mem_previously_shared'"
    )
    store.conn.commit()
    for record in records:
        store.secure_erase_memory(record.id)

    classifications = {
        item["id"]: item["export_class"]
        for item in store.list_memory_tombstones(workspace)
    }
    assert classifications == {
        "mem_secret": "never_export",
        "mem_previously_shared": "remote_erasure",
        "mem_session": "never_export",
        "mem_shared": "remote_erasure",
    }
    assert {
        item["id"]
        for item in SyncEngine(store).export_bundle(workspace)["tombstones"]
    } == {"mem_previously_shared", "mem_shared"}


def test_secure_erase_rolls_back_delete_when_tombstone_write_fails(monkeypatch):
    store = Store(":memory:")
    workspace = store.get_or_create_workspace("w")
    memory_id = store.add_memory(MemoryRecord(
        id="", content="secret plan", workspace_id=workspace,
        scope=Scope.WORKSPACE,
    ))
    device_id = store.get_sync_state("device_id")
    assert device_id

    def fail_tombstone(*args, **kwargs):
        raise RuntimeError("tombstone unavailable")

    monkeypatch.setattr(store, "add_memory_tombstone", fail_tombstone)
    with pytest.raises(RuntimeError, match="tombstone unavailable"):
        store.secure_erase_memory(memory_id)

    # HLC initialization already minted the durable device marker. The memory and
    # its erase audit must remain intact when the terminal marker fails.
    assert store.get_sync_state("device_id") == device_id
    assert store.get_memory(memory_id) is not None
    assert store.list_memory_tombstones() == []
    assert store.conn.in_transaction is False


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
    _push_bundle(SyncEngine(a), aw)
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
    syncer_b.apply_bundle(_push_bundle(syncer_a, aw), into_workspace="w")
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
    a.add_memory_tombstone(
        "shared-id", deleted_at=1.0, workspace_id=aw,
        export_class="remote_erasure",
    )
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
    a.add_memory_tombstone(
        mid, deleted_at=1.0, workspace_id=aw,
        export_class="remote_erasure",
    )
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
            {"id": "erased", "deleted_at": 20.0, "device": "late",
             "export_class": "remote_erasure"},
            {"id": "erased", "deleted_at": 10.0, "device": "early",
             "export_class": "remote_erasure"},
            {"id": "", "deleted_at": 5.0, "device": "malformed",
             "export_class": "remote_erasure"},
            {"id": "bad-time", "deleted_at": "not-a-number",
             "device": "malformed", "export_class": "remote_erasure"},
        ],
        "mem_links": [],
    }

    first = syncer.apply_bundle(bundle, into_workspace="w")
    assert first["tombstones_applied"] == 1
    assert first["rejected"] == 1
    assert store.get_memory("erased") is None
    tombstones = store.list_memory_tombstones(workspace)
    assert len(tombstones) == 1
    assert tombstones[0]["id"] == "erased"
    assert tombstones[0]["deleted_at"] == 10.0
    assert tombstones[0]["device"].startswith("legacy_")
    assert tombstones[0]["workspace_id"] == workspace
    assert tombstones[0]["repo_id"] is None

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
        export_class="remote_erasure",
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
        provenance={"source": "sync", "trusted": False},
    ))

    SyncEngine(store).apply_bundle({
        "format": "engraphis-sync", "version": 1, "workspace_name": "w",
        "repos": {}, "memories": [],
        "tombstones": [{
            "id": "legacy-global", "deleted_at": 1.0,
            "export_class": "remote_erasure",
        }],
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
        provenance={"source": "sync", "trusted": False},
    ))

    report = SyncEngine(store).apply_bundle({
        "format": "engraphis-sync", "version": 2, "workspace_name": "w",
        "repos": {"remote-a": "repo-a", "remote-b": "repo-b"},
        "memories": [],
        "tombstones": [
            {"id": "scoped-sibling", "deleted_at": 1.0,
             "repo_id": "remote-a", "export_class": "remote_erasure"},
            {"id": "scoped-sibling", "deleted_at": 2.0,
             "repo_id": "remote-b", "export_class": "remote_erasure"},
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
