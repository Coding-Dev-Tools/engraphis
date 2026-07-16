import json
from concurrent.futures import ThreadPoolExecutor

from engraphis.core.store import Store
from engraphis.service import MemoryService


def test_receipts_are_content_free_and_tamper_evident():
    store = Store(":memory:")
    wid = store.get_or_create_workspace("private-workspace")
    receipt = store.record_receipt(
        "remember",
        workspace_id=wid,
        actor="alice@example.com",
        target_count=1,
        metadata={
            "mtype": "semantic",
            "content": "do not expose me",
            "query": "also private",
            "memory_id": "mem_secret",
        },
    )
    encoded = json.dumps(receipt)
    assert "private-workspace" not in encoded
    assert "alice@example.com" not in encoded
    assert "do not expose me" not in encoded
    assert "also private" not in encoded
    assert "mem_secret" not in encoded
    assert receipt["metadata"] == {"mtype": "semantic"}
    assert store.verify_receipts(workspace_id=wid)["valid"] is True

    store.conn.execute(
        "UPDATE operation_receipts SET payload=? WHERE id=?",
        ('{"tampered":true}', receipt["id"]),
    )
    store.conn.commit()
    verification = store.verify_receipts(workspace_id=wid)
    assert verification["valid"] is False
    assert {error["error"] for error in verification["errors"]} >= {
        "hash_mismatch", "payload_mismatch",
    }


def test_concurrent_receipts_form_one_valid_chain():
    store = Store(":memory:")
    wid = store.get_or_create_workspace("team")

    def write(index):
        return store.record_receipt(
            "recall", workspace_id=wid, actor=f"agent-{index}",
            metadata={"intent": "recall", "result_count": index},
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        receipts = list(pool.map(write, range(40)))

    assert len({receipt["hash"] for receipt in receipts}) == 40
    verification = store.verify_receipts(workspace_id=wid)
    assert verification["valid"] is True
    assert verification["count"] == 40
    assert verification["errors"] == []
    assert verification["head"] in {receipt["hash"] for receipt in receipts}


def test_receipts_are_serialized_across_store_connections(tmp_path):
    db = str(tmp_path / "team.db")
    stores = [Store(db) for _ in range(4)]
    wid = stores[0].get_or_create_workspace("team")

    def write(index):
        return stores[index % len(stores)].record_receipt(
            "recall", workspace_id=wid, actor=f"agent-{index}",
            metadata={"intent": "recall", "result_count": index},
        )

    try:
        with ThreadPoolExecutor(max_workers=len(stores)) as pool:
            receipts = list(pool.map(write, range(40)))

        assert len({receipt["hash"] for receipt in receipts}) == 40
        verification = stores[0].verify_receipts(workspace_id=wid)
        assert verification["valid"] is True
        assert verification["count"] == 40
        assert verification["errors"] == []
    finally:
        for store in stores:
            store.close()


def test_service_records_and_exports_operation_receipts():
    service = MemoryService.create(":memory:")
    stored = service.remember(
        "The release process uses signed tags.", workspace="acme", scope="workspace"
    )
    recalled = service.recall("release process", workspace="acme")
    assert stored["receipt"]["operation"] == "remember"
    assert recalled["receipt"]["operation"] == "recall"

    exported = service.export_receipts(workspace="acme")
    assert exported["format"] == "engraphis-receipts/1"
    assert exported["verification"]["valid"] is True
    assert {entry["operation"] for entry in exported["entries"]} == {"remember", "recall"}
