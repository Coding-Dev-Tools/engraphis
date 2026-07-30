"""Client-side Cloud Sync encryption and fail-closed relay behavior."""
from __future__ import annotations

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
    assert report["errors"] == [
        {"bundle": "?", "error": "transport failure", "error_type": "RelayError"}
    ]


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
