"""Client-side Cloud Sync encryption and fail-closed relay behavior."""
from __future__ import annotations

import json

import pytest

pytest.importorskip("cryptography")

from engraphis.backends.sync_relay import (
    EncryptedRelayTransport,
    RelayError,
    SYNC_E2EE_MAGIC,
)
from engraphis.core.engine import MemoryEngine
from engraphis.core.interfaces import Scope, SearchFilter
from engraphis.core.sync import SyncEngine


class _MemoryRelay:
    """A relay-shaped ciphertext store. It deliberately never decrypts a bundle."""

    def __init__(self, workspace_id: str = "acme") -> None:
        self.workspace_id = workspace_id
        self.bundles: dict[str, bytes] = {}

    def push(self, name: str, data: bytes) -> None:
        self.bundles[name] = data

    def pull(self):
        return list(self.bundles.items())

    def list_names(self):
        return sorted(self.bundles)


def _transport(relay: _MemoryRelay, key_byte: int) -> EncryptedRelayTransport:
    return EncryptedRelayTransport(relay, bytes([key_byte]) * 32)


def test_cloud_sync_bundle_is_ciphertext_with_a_stable_opaque_name():
    relay = _MemoryRelay()
    sender = _transport(relay, 1)
    receiver = _transport(relay, 1)
    plaintext = b"customer-only roadmap and device note"

    sender.push("bundle-dev_customer.json", plaintext)
    sender.push("bundle-dev_customer.json", plaintext)

    assert len(relay.bundles) == 1
    name, stored = next(iter(relay.bundles.items()))
    assert name.startswith("e2ee-") and name.endswith(".json")
    assert "dev_customer" not in name
    assert stored.startswith(SYNC_E2EE_MAGIC)
    assert plaintext not in stored
    assert list(receiver.pull()) == [(name, plaintext)]


def test_cloud_sync_rejects_tampered_or_plaintext_bundle():
    relay = _MemoryRelay()
    sender = _transport(relay, 2)
    receiver = _transport(relay, 2)
    sender.push("bundle-dev_a.json", b"private content")
    name, stored = next(iter(relay.bundles.items()))
    relay.bundles[name] = stored[:-1] + bytes([stored[-1] ^ 1])

    with pytest.raises(RelayError, match="unreadable bundle"):
        list(receiver.pull())

    relay.bundles[name] = b'{"legacy":"plaintext"}'
    with pytest.raises(RelayError, match="unreadable bundle"):
        list(receiver.pull())


def test_cloud_sync_rejects_a_bundle_from_another_key_or_workspace():
    relay = _MemoryRelay()
    sender = _transport(relay, 3)
    wrong_key = _transport(relay, 4)
    sender.push("bundle-dev_a.json", b"private content")

    with pytest.raises(RelayError, match="unreadable bundle"):
        list(wrong_key.pull())

    wrong_workspace = _transport(_MemoryRelay("other"), 3)
    name, stored = next(iter(relay.bundles.items()))
    wrong_workspace.relay.bundles[name] = stored
    with pytest.raises(RelayError, match="unreadable bundle"):
        list(wrong_workspace.pull())


@pytest.mark.parametrize("bad_kind", ["legacy", "tampered"], ids=["legacy", "tampered"])
def test_sync_engine_applies_later_encrypted_bundle_after_unreadable_relay_object(bad_kind):
    """Legacy/corrupt relay objects cannot starve later authenticated peers."""
    relay = _MemoryRelay()
    key = bytes(range(32))
    sender = MemoryEngine.create(":memory:")
    receiver = MemoryEngine.create(":memory:")
    sender_workspace = sender.store.get_or_create_workspace("acme")
    receiver_workspace = receiver.store.get_or_create_workspace("acme")
    sender.remember("peer fact survives a bad relay object", workspace_id=sender_workspace,
                    scope=Scope.WORKSPACE)
    sender_sync = SyncEngine(sender.store, embedder=sender.embedder, vector_index=sender.index)
    receiver_sync = SyncEngine(
        receiver.store, embedder=receiver.embedder, vector_index=receiver.index
    )

    # The bad object is deliberately inserted before the sender's encrypted bundle.
    if bad_kind == "legacy":
        relay.bundles["bundle-legacy.json"] = b'{"legacy":"plaintext"}'
    else:
        corrupt_writer = EncryptedRelayTransport(relay, key)
        corrupt_writer.push("bundle-corrupt.json", b"original authenticated ciphertext")
        corrupt_name, ciphertext = next(iter(relay.bundles.items()))
        relay.bundles[corrupt_name] = ciphertext[:-1] + bytes([ciphertext[-1] ^ 1])
    sender_sync.sync(EncryptedRelayTransport(relay, key), sender_workspace)

    report = receiver_sync.sync(
        EncryptedRelayTransport(relay, key), receiver_workspace, push=False
    )

    contents = {
        memory.content
        for memory in receiver.store.list_memories(SearchFilter(workspace_id=receiver_workspace))
    }
    assert contents == {"peer fact survives a bad relay object"}
    assert report["totals"]["added"] == 1
    assert report["peers_applied"] == 1
    assert report["complete"] is False
    assert {item["error"] for item in report["errors"]} == {
        "transport failure", "snapshot freshness unavailable",
    }


def test_sync_engine_converges_through_encrypted_relay_without_plaintext_storage():
    relay = _MemoryRelay()
    key = bytes(range(32))
    a = MemoryEngine.create(":memory:")
    b = MemoryEngine.create(":memory:")
    wa = a.store.get_or_create_workspace("acme")
    wb = b.store.get_or_create_workspace("acme")
    a.remember("customer private fact", workspace_id=wa, scope=Scope.WORKSPACE)
    b.remember("other private fact", workspace_id=wb, scope=Scope.WORKSPACE)
    sa = SyncEngine(a.store, embedder=a.embedder, vector_index=a.index)
    sb = SyncEngine(b.store, embedder=b.embedder, vector_index=b.index)

    sa.sync(EncryptedRelayTransport(relay, key), wa)
    sb.sync(EncryptedRelayTransport(relay, key), wb)
    sa.sync(EncryptedRelayTransport(relay, key), wa)

    contents_a = {memory.content for memory in a.store.list_memories(SearchFilter(workspace_id=wa))}
    contents_b = {memory.content for memory in b.store.list_memories(SearchFilter(workspace_id=wb))}
    assert contents_a == contents_b == {"customer private fact", "other private fact"}
    stored = b"".join(relay.bundles.values())
    assert b"customer private fact" not in stored
    assert b"other private fact" not in stored


def test_authenticated_snapshot_replay_is_rejected_after_newer_tombstone():
    relay = _MemoryRelay()
    key = bytes(range(32))
    source = MemoryEngine.create(":memory:")
    receiver = MemoryEngine.create(":memory:")
    source_workspace = source.store.get_or_create_workspace("acme")
    receiver_workspace = receiver.store.get_or_create_workspace("acme")
    memory_id = source.remember(
        "pre-erasure fact",
        workspace_id=source_workspace,
        scope=Scope.WORKSPACE,
    )
    source_sync = SyncEngine(source.store)
    receiver_sync = SyncEngine(receiver.store)

    encrypted = EncryptedRelayTransport(relay, key)
    source_sync.sync(encrypted, source_workspace)
    source_name = encrypted._opaque_name(
        "bundle-%s.json" % source_sync.device_id
    )
    generation_one = relay.bundles[source_name]
    receiver_sync.sync(
        EncryptedRelayTransport(relay, key), receiver_workspace
    )

    source.store.secure_erase_memory(memory_id)
    source_sync.sync(EncryptedRelayTransport(relay, key), source_workspace)
    receiver_sync.sync(
        EncryptedRelayTransport(relay, key), receiver_workspace
    )
    assert receiver.store.get_memory(memory_id) is None

    # A relay replaying a valid old ciphertext must not resurrect or roll back the
    # authenticated tombstone checkpoint.
    relay.bundles[source_name] = generation_one
    rollback = receiver_sync.sync(
        EncryptedRelayTransport(relay, key), receiver_workspace
    )

    assert rollback["complete"] is False
    assert rollback["errors"][0]["error"] == "bundle rejected"
    assert receiver.store.get_memory(memory_id) is None
    decrypted = [
        json.loads(data)
        for _, data in EncryptedRelayTransport(relay, key).pull()
    ]
    own = next(
        bundle for bundle in decrypted
        if bundle["device_id"] == receiver_sync.device_id
    )
    assert own["generation"] >= 3
    assert any(item["id"] == memory_id for item in own["tombstones"])


def test_restored_device_merges_own_newer_snapshot_before_replacing_it():
    relay = _MemoryRelay()
    key = bytes(range(32))
    source = MemoryEngine.create(":memory:")
    source_workspace = source.store.get_or_create_workspace("acme")
    memory_id = source.remember(
        "pre-backup fact",
        workspace_id=source_workspace,
        scope=Scope.WORKSPACE,
    )
    source_sync = SyncEngine(source.store)
    source_sync.sync(EncryptedRelayTransport(relay, key), source_workspace)

    # Stand in for a database backup taken after this device accepted its own first
    # snapshot, but before the later local erase and generation checkpoint.
    restored = MemoryEngine.create(":memory:")
    restored_workspace = restored.store.get_or_create_workspace("acme")
    restored_sync = SyncEngine(
        restored.store,
        device_id=source_sync.device_id,
    )
    generation_one = json.loads(next(iter(
        EncryptedRelayTransport(relay, key).pull()
    ))[1])
    restored_sync.apply_bundle(generation_one, into_workspace="acme")
    assert restored.store.get_memory(memory_id) is not None

    source.store.secure_erase_memory(memory_id)
    source_sync.sync(EncryptedRelayTransport(relay, key), source_workspace)
    report = restored_sync.sync(
        EncryptedRelayTransport(relay, key), restored_workspace
    )

    assert report["totals"]["tombstones_applied"] == 1
    assert restored.store.get_memory(memory_id) is None