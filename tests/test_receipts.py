import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from engraphis.core.ids import new_id
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


def test_short_user_controlled_receipt_labels_are_never_stored_verbatim():
    store = Store(":memory:")
    wid = store.get_or_create_workspace("w")
    secret = "sk-live-short"
    receipt = store.record_receipt(
        "recall",
        workspace_id=wid,
        metadata={
            "intent": secret,
            "relation": secret,
            "token_usage": {"context_tokens": 1, "token_counter": secret},
        },
    )

    encoded = json.dumps(receipt)
    assert secret not in encoded
    assert receipt["metadata"]["intent"].startswith("sha256:")
    assert receipt["metadata"]["relation"].startswith("sha256:")
    assert receipt["metadata"]["token_usage"]["token_counter"].startswith("sha256:")


def test_store_list_receipts_never_reflects_poisoned_storage_fields():
    store = Store(":memory:")
    wid = store.get_or_create_workspace("w")
    receipt = store.record_receipt("recall", workspace_id=wid)
    markers = {
        "id": "STORE_LIST_POISON_ID",
        "prev": "STORE_LIST_POISON_PREV",
        "hash": "STORE_LIST_POISON_HASH",
        "payload": "STORE_LIST_POISON_PAYLOAD",
    }
    store.conn.execute(
        "UPDATE operation_receipts SET id=?, prev_hash=?, receipt_hash=?, payload=? "
        "WHERE id=?",
        (
            markers["id"], markers["prev"], markers["hash"],
            json.dumps({"secret": markers["payload"]}), receipt["id"],
        ),
    )
    store.conn.commit()

    exported = store.list_receipts(workspace_id=wid)
    encoded = json.dumps(exported)
    assert all(marker not in encoded for marker in markers.values())
    assert exported[0]["invalid_payload"] is True
    assert exported[0]["id"].startswith("redacted_sha256:")
    assert exported[0]["prev_hash"].startswith("redacted_sha256:")
    assert exported[0]["hash"].startswith("redacted_sha256:")
    assert store.verify_receipts(workspace_id=wid)["valid"] is False


def test_operation_status_and_nonfinite_metadata_cannot_leak_into_receipts():
    store = Store(":memory:")
    wid = store.get_or_create_workspace("w")
    secret = "short-secret"
    receipt = store.record_receipt(
        secret,
        workspace_id=wid,
        status=secret,
        target_count="not-a-number",
        metadata={"k": float("nan"), "result_count": float("inf")},
    )

    encoded = json.dumps(receipt)
    assert secret not in encoded
    assert receipt["operation"].startswith("sha256:")
    assert receipt["status"].startswith("sha256:")
    assert receipt["target_count"] == 0
    assert receipt["metadata"] == {}
    assert store.verify_receipts(workspace_id=wid)["valid"] is True


def test_fixed_terminal_statuses_remain_human_readable():
    store = Store(":memory:")
    wid = store.get_or_create_workspace("w")

    failed = store.record_receipt("graph_index", workspace_id=wid, status="failed")
    cancelled = store.record_receipt("graph_index", workspace_id=wid, status="cancelled")

    assert failed["status"] == "failed"
    assert cancelled["status"] == "cancelled"


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


def test_receipt_anchor_detects_tail_truncation():
    store = Store(":memory:")
    wid = store.get_or_create_workspace("team")
    first = store.record_receipt("remember", workspace_id=wid)
    second = store.record_receipt("recall", workspace_id=wid)
    assert store.verify_receipts(workspace_id=wid)["valid"] is True

    store.conn.execute("DELETE FROM operation_receipts WHERE id=?", (second["id"],))
    store.conn.commit()

    verification = store.verify_receipts(workspace_id=wid)
    assert verification["valid"] is False
    assert {error["error"] for error in verification["errors"]} >= {
        "anchor_count_mismatch", "anchor_head_mismatch",
    }
    assert verification["head"] == first["hash"]


def test_receipt_append_after_truncation_preserves_integrity_failure():
    store = Store(":memory:")
    wid = store.get_or_create_workspace("team")
    store.record_receipt("remember", workspace_id=wid)
    second = store.record_receipt("recall", workspace_id=wid)
    store.conn.execute("DELETE FROM operation_receipts WHERE id=?", (second["id"],))
    store.conn.commit()

    # Receipt integrity problems must stay visible, but must not turn a completed memory
    # operation into a misleading API failure merely because its receipt is appended next.
    store.record_receipt("link", workspace_id=wid)

    verification = store.verify_receipts(workspace_id=wid)
    assert verification["valid"] is False
    assert "anchor_integrity_error" in {
        error["error"] for error in verification["errors"]
    }


def test_append_after_missing_anchor_preserves_public_integrity_marker():
    service = MemoryService.create(":memory:")
    first = service.remember("First fact.", workspace="team", scope="workspace")
    wid = service._lookup_workspace("team")
    service.store.conn.execute(
        "DELETE FROM receipt_chain_heads WHERE workspace_id=?", (wid,)
    )
    service.store.conn.commit()

    appended = service.store.record_receipt("recall", workspace_id=wid)
    exported = service.export_workspace(workspace="team")

    assert appended["prev_hash"] == first["receipt"]["hash"]
    assert exported["receipt_chain"]["integrity_error"] == "pre_append_anchor_missing"
    assert exported["receipt_verification"]["valid"] is False


def test_receipt_append_after_payload_corruption_is_non_bricking_and_stays_invalid():
    store = Store(":memory:")
    wid = store.get_or_create_workspace("team")
    first = store.record_receipt("remember", workspace_id=wid)
    store.conn.execute(
        "UPDATE operation_receipts SET payload=payload || ' ' WHERE id=?",
        (first["id"],),
    )
    store.conn.commit()

    appended = store.record_receipt("recall", workspace_id=wid)

    assert appended["prev_hash"] == first["hash"]
    verification = store.verify_receipts(workspace_id=wid)
    assert verification["valid"] is False
    assert {
        "hash_mismatch", "anchor_integrity_error",
    } <= {error["error"] for error in verification["errors"]}


def test_receipt_fork_has_no_safe_append_head():
    store = Store(":memory:")
    wid = store.get_or_create_workspace("team")
    first = store.record_receipt("remember", workspace_id=wid)
    second = store.record_receipt("recall", workspace_id=wid)
    fork = dict(second)
    fork.pop("hash")
    fork["id"] = new_id("receipt")
    fork["prev_hash"] = first["hash"]
    fork["ts_ms"] += 1
    payload = json.dumps(
        fork, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    receipt_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    store.conn.execute(
        "INSERT INTO operation_receipts(id, ts, operation, workspace_id, repo_id, "
        "sequence, scope_digest, actor, target_count, status, payload, prev_hash, "
        "receipt_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            fork["id"], fork["ts_ms"] / 1000.0, fork["operation"], wid, "", 999,
            fork["scope_digest"], fork["actor_digest"], fork["target_count"],
            fork["status"], payload, fork["prev_hash"], receipt_hash,
        ),
    )
    store.conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="no unique structural head"):
        store.record_receipt("link", workspace_id=wid)

    verification = store.verify_receipts(workspace_id=wid)
    assert verification["valid"] is False
    assert "chain_fork" in {error["error"] for error in verification["errors"]}


def test_healthy_receipt_append_does_not_reconstruct_chain(monkeypatch):
    store = Store(":memory:")
    wid = store.get_or_create_workspace("team")
    first = store.record_receipt("remember", workspace_id=wid)

    def unexpected_reconstruction(_workspace_id):
        raise AssertionError("healthy append must use the anchored sequence head")

    monkeypatch.setattr(store, "_receipt_chain_state", unexpected_reconstruction)
    second = store.record_receipt("recall", workspace_id=wid)

    assert second["prev_hash"] == first["hash"]
    assert [
        row["sequence"] for row in store.conn.execute(
            "SELECT sequence FROM operation_receipts "
            "WHERE workspace_id=? ORDER BY sequence",
            (wid,),
        ).fetchall()
    ] == [1, 2]


def test_bounded_receipt_logs_do_not_reconstruct_chain(monkeypatch):
    service = MemoryService.create(":memory:")
    wid = service.store.get_or_create_workspace("team")
    first = service.store.record_receipt("remember", workspace_id=wid)
    second = service.store.record_receipt("recall", workspace_id=wid)

    def unexpected_reconstruction(_workspace_id):
        raise AssertionError("bounded receipt inspection must use the sequence index")

    monkeypatch.setattr(
        service.store, "_receipt_chain_state", unexpected_reconstruction
    )
    assert service.store.list_receipts(workspace_id=wid, limit=1) == [
        {**second}
    ]
    assert service.receipt_log(workspace="team", limit=1)["entries"] == [
        {**second}
    ]
    assert first["id"] != second["id"]


def test_receipt_sequence_is_immutable():
    store = Store(":memory:")
    wid = store.get_or_create_workspace("team")
    receipt = store.record_receipt("remember", workspace_id=wid)

    with pytest.raises(sqlite3.IntegrityError, match="sequence is immutable"):
        store.conn.execute(
            "UPDATE operation_receipts SET sequence=2 WHERE id=?",
            (receipt["id"],),
        )
    store.conn.rollback()

    assert store.verify_receipts(workspace_id=wid)["valid"] is True


def test_receipt_chain_survives_physical_row_reordering_and_vacuum(tmp_path):
    db = str(tmp_path / "vacuum-receipts.db")
    store = Store(db)
    wid = store.get_or_create_workspace("team")
    receipts = [
        store.record_receipt(operation, workspace_id=wid)
        for operation in ("remember", "recall", "link")
    ]
    for index, receipt in enumerate(receipts):
        store.conn.execute(
            "UPDATE operation_receipts SET rowid=? WHERE id=?",
            (10_000 - index, receipt["id"]),
        )
    store.conn.commit()
    store.conn.execute("VACUUM")
    physical = [
        row["id"] for row in store.conn.execute(
            "SELECT id FROM operation_receipts ORDER BY rowid"
        ).fetchall()
    ]
    assert physical == [row["id"] for row in reversed(receipts)]

    assert store.verify_receipts(workspace_id=wid)["valid"] is True
    assert [
        row["id"] for row in reversed(store.list_receipts(
            workspace_id=wid, limit=10
        ))
    ] == [row["id"] for row in receipts]
    appended = store.record_receipt("sync", workspace_id=wid)
    assert appended["prev_hash"] == receipts[-1]["hash"]
    assert store.verify_receipts(workspace_id=wid)["valid"] is True
    store.close()

    service = MemoryService.create(db)
    try:
        exported = service.export_receipts(workspace="team")
        assert exported["verification"]["valid"] is True
        assert [row["id"] for row in exported["entries"]] == [
            *[row["id"] for row in receipts],
            appended["id"],
        ]
    finally:
        service.store.close()


def test_reopening_anchored_receipts_does_not_reconstruct_chains(tmp_path, monkeypatch):
    db = str(tmp_path / "reopen-receipts.db")
    store = Store(db)
    wid = store.get_or_create_workspace("team")
    store.record_receipt("remember", workspace_id=wid)
    store.record_receipt("recall", workspace_id=wid)
    store.close()

    calls = []
    original = Store._receipt_chain_state

    def tracked(self, workspace_id):
        calls.append(workspace_id)
        return original(self, workspace_id)

    monkeypatch.setattr(Store, "_receipt_chain_state", tracked)
    reopened = Store(db)
    try:
        assert calls == []
        assert reopened.verify_receipts(workspace_id=wid)["valid"] is True
        assert calls == [wid]
    finally:
        reopened.close()


def test_receipt_anchor_migration_normalizes_legacy_null_scope(tmp_path):
    db = str(tmp_path / "receipts.db")
    store = Store(db)
    workspace = store.get_or_create_workspace("team")
    receipt = store.record_receipt("remember", workspace_id=workspace)
    store.conn.execute("DELETE FROM receipt_chain_heads")
    store.conn.execute(
        "UPDATE operation_receipts SET workspace_id=NULL, repo_id=NULL WHERE id=?",
        (receipt["id"],),
    )
    store.conn.execute("DELETE FROM schema_migrations")
    store.conn.execute(
        "INSERT INTO schema_migrations(version, applied_at) VALUES (4, 0)"
    )
    store.conn.commit()
    store.close()

    reopened = Store(db)
    try:
        row = reopened.conn.execute(
            "SELECT workspace_id, repo_id FROM operation_receipts WHERE id=?",
            (receipt["id"],),
        ).fetchone()
        assert tuple(row) == ("", "")
        assert reopened.verify_receipts(workspace_id="")["valid"] is True
    finally:
        reopened.close()


def test_current_schema_reopen_does_not_recreate_missing_receipt_anchor(tmp_path):
    db = str(tmp_path / "missing-anchor.db")
    store = Store(db)
    wid = store.get_or_create_workspace("team")
    store.record_receipt("remember", workspace_id=wid)
    store.conn.execute(
        "DELETE FROM receipt_chain_heads WHERE workspace_id=?", (wid,)
    )
    store.conn.commit()
    store.close()

    reopened = Store(db)
    try:
        verification = reopened.verify_receipts(workspace_id=wid)
        assert verification["valid"] is False
        assert verification["anchored"] is False
        assert "missing_anchor" in {
            error["error"] for error in verification["errors"]
        }
    finally:
        reopened.close()


def test_external_receipt_anchor_detects_rewritten_local_anchor():
    store = Store(":memory:")
    wid = store.get_or_create_workspace("team")
    first = store.record_receipt("remember", workspace_id=wid)
    store.record_receipt("recall", workspace_id=wid)
    saved = store.verify_receipts(workspace_id=wid)

    # Simulate an attacker truncating both the receipt tail and the anchor stored in the
    # same database. Local verification alone cannot prove history against a full rewrite;
    # an externally saved head/count can.
    store.conn.execute(
        "DELETE FROM operation_receipts WHERE receipt_hash!=?",
        (first["hash"],),
    )
    store.conn.execute(
        "UPDATE receipt_chain_heads SET receipt_count=1, head_hash=? WHERE workspace_id=?",
        (first["hash"], wid),
    )
    store.conn.commit()

    assert store.verify_receipts(workspace_id=wid)["valid"] is True
    verification = store.verify_receipts(
        workspace_id=wid,
        expected_head=saved["head"],
        expected_count=saved["count"],
    )
    assert verification["valid"] is False
    assert {error["error"] for error in verification["errors"]} >= {
        "expected_head_mismatch", "expected_count_mismatch",
    }


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


def test_context_savings_is_scoped_content_free_and_groups_token_counters():
    service = MemoryService.create(":memory:")
    stored = service.remember(
        "Receipt saving marker PURPLE-FOX-177.", workspace="acme", repo="api"
    )
    wid = service.store.get_or_create_workspace("acme")
    rid = service._lookup_repo(wid, "api")
    assert rid is not None
    service.store.record_receipt(
        "recall", workspace_id=wid, repo_id=rid, actor="PURPLE-FOX-177",
        metadata={"token_usage": {
            "budget_tokens": 80, "source_tokens": 100, "context_tokens": 25,
            "saved_tokens": 75, "savings_ratio": 0.75, "packed_count": 2,
            "omitted_count": 3, "token_counter": "engraphis.regex.v1",
        }},
    )
    service.store.record_receipt(
        "grounded_recall", workspace_id=wid, repo_id=rid,
        metadata={"token_usage": {
            "budget_tokens": 40, "source_tokens": 40, "context_tokens": 40,
            "saved_tokens": 0, "savings_ratio": 0.0, "packed_count": 1,
            "omitted_count": 0, "token_counter": "engraphis.regex.v1",
        }},
    )
    service.store.record_receipt(
        "recall", workspace_id=wid,
        metadata={"token_usage": {
            "budget_tokens": 20, "source_tokens": 20, "context_tokens": 5,
            "saved_tokens": 15, "savings_ratio": 0.75, "packed_count": 1,
            "omitted_count": 1, "token_counter": "estimate_tokens",
        }},
    )
    service.store.record_receipt(
        "recall", workspace_id=wid, repo_id=rid,
        metadata={"token_usage": {
            "budget_tokens": 10, "source_tokens": 10, "context_tokens": 8,
            "saved_tokens": 9, "token_counter": "engraphis.regex.v1",
        }},
    )

    repo_summary = service.context_savings(workspace="acme", repo="api")
    assert repo_summary["scope"] == {"workspace": "acme", "repo": "api"}
    assert repo_summary["receipt_chain_valid"] is True
    assert repo_summary["receipt_chain_error_count"] == 0
    assert repo_summary["receipt_count"] == 4
    assert repo_summary["savings_receipt_count"] == 2
    assert repo_summary["incomplete_usage_receipt_count"] == 1
    assert repo_summary["by_token_counter"] == [{
        "token_counter": "engraphis.regex.v1",
        "receipt_count": 2,
        "source_tokens": 140,
        "context_tokens": 65,
        "saved_tokens": 75,
        "budget_tokens": 120,
        "packed_count": 3,
        "omitted_count": 3,
        "savings_ratio": 75 / 140,
        "by_operation": [
            {"operation": "grounded_recall", "receipt_count": 1,
             "source_tokens": 40, "context_tokens": 40, "saved_tokens": 0,
             "budget_tokens": 40, "packed_count": 1, "omitted_count": 0,
             "savings_ratio": 0.0},
            {"operation": "recall", "receipt_count": 1,
             "source_tokens": 100, "context_tokens": 25, "saved_tokens": 75,
             "budget_tokens": 80, "packed_count": 2, "omitted_count": 3,
             "savings_ratio": 0.75},
        ],
    }]
    workspace_summary = service.context_savings(workspace="acme")
    assert [row["token_counter"] for row in workspace_summary["by_token_counter"]] == [
        "engraphis.regex.v1", "estimate_tokens"
    ]
    assert "PURPLE-FOX-177" not in json.dumps(workspace_summary)
    assert stored["receipt"]["operation"] == "remember"

    poisoned = service.store.record_receipt("recall", workspace_id=wid)
    service.store.conn.execute(
        "UPDATE operation_receipts SET payload=? WHERE id=?",
        ('{"query":"PURPLE-FOX-177"}', poisoned["id"]),
    )
    service.store.conn.commit()
    poisoned_summary = service.context_savings(workspace="acme")
    assert poisoned_summary["invalid_receipt_count"] == 1
    assert poisoned_summary["receipt_chain_valid"] is False
    assert poisoned_summary["receipt_chain_error_count"] > 0
    assert "PURPLE-FOX-177" not in json.dumps(poisoned_summary)


def test_context_savings_excludes_receipts_with_reassigned_repo_ids():
    store = Store(":memory:")
    workspace_id = store.get_or_create_workspace("acme")
    original_repo = store.get_or_create_repo(workspace_id, "api")
    reassigned_repo = store.get_or_create_repo(workspace_id, "other")
    receipt = store.record_receipt(
        "recall",
        workspace_id=workspace_id,
        repo_id=original_repo,
        metadata={"token_usage": {
            "source_tokens": 100,
            "context_tokens": 20,
            "saved_tokens": 80,
            "token_counter": "estimate_tokens",
        }},
    )

    # The signed payload remains valid, but the relational repo column is not its scope.
    store.conn.execute(
        "UPDATE operation_receipts SET repo_id=? WHERE id=?",
        (reassigned_repo, receipt["id"]),
    )
    store.conn.commit()

    assert store.verify_receipts(workspace_id=workspace_id)["valid"] is True
    summary = store.context_savings(workspace_id=workspace_id, repo_id=reassigned_repo)
    assert summary["receipt_count"] == 1
    assert summary["invalid_receipt_count"] == 1
    assert summary["usage_receipt_count"] == 0
    assert summary["savings_receipt_count"] == 0
    assert summary["by_token_counter"] == []


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


def test_store_and_service_share_strict_receipt_projection():
    service = MemoryService.create(":memory:")
    wid = service.store.get_or_create_workspace("acme")
    receipt = service.store.record_receipt(
        "recall", workspace_id=wid, metadata={"retrieval_profile": "code"}
    )
    listed = service.store.list_receipts(workspace_id=wid)
    logged = service.receipt_log(workspace="acme")["entries"]
    assert listed == logged
    assert listed[0]["metadata"]["retrieval_profile"] == "code"

    payload = dict(receipt)
    payload.pop("hash")
    payload["metadata"] = {"scope": "semantic"}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    poisoned_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    service.store.conn.execute(
        "UPDATE operation_receipts SET payload=?, receipt_hash=? WHERE id=?",
        (raw, poisoned_hash, receipt["id"]),
    )
    service.store.conn.execute(
        "UPDATE receipt_chain_heads SET head_hash=? WHERE workspace_id=?",
        (poisoned_hash, wid),
    )
    service.store.conn.commit()

    listed = service.store.list_receipts(workspace_id=wid)
    logged = service.receipt_log(workspace="acme")["entries"]
    assert listed == logged
    assert listed[0]["invalid_payload"] is True
