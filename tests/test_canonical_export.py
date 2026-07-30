"""Deterministic workspace-export evidence."""
from __future__ import annotations

import hashlib
import json

from engraphis.service import MemoryService, set_current_user


def _digest_payload(export: dict) -> str:
    payload = dict(export)
    expected = payload.pop("sha256")
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    assert hashlib.sha256(encoded).hexdigest() == expected
    return expected


def test_unchanged_workspace_has_identical_canonical_export_and_digest():
    service = MemoryService.create(":memory:")
    service.remember(
        "Deployments require signed release tags.",
        workspace="acme",
        repo="api",
    )

    first = service.export_workspace(workspace="acme", canonical=True)
    second = service.export_workspace(workspace="acme", canonical=True)

    assert first == second
    assert first["canonical"] is True
    assert "exported_at" not in first
    assert _digest_payload(first) == _digest_payload(second)


def test_default_workspace_export_is_v2_and_keeps_timestamped_compatibility_fields():
    service = MemoryService.create(":memory:")
    service.remember("A durable fact.", workspace="acme")

    exported = service.export_workspace(workspace="acme")

    assert exported["format"] == "engraphis-export/2"
    assert "exported_at" in exported
    assert "sha256" not in exported
    assert exported["counts"]["memories"] == 1
    assert exported["completeness"]["durable_workspace_state"] is True
    assert exported["completeness"]["receipts"] is True
    assert exported["receipt_verification"]["valid"] is True


def test_canonical_digest_covers_graph_and_code_state():
    service = MemoryService.create(":memory:")
    memory = service.remember(
        "The API delegates parsing to load_config.",
        workspace="acme",
        repo="api",
    )
    wid = service.store.get_or_create_workspace("acme")
    rid = service.store.get_or_create_repo(wid, "api")
    baseline = service.export_workspace(workspace="acme", canonical=True)["sha256"]

    service.store.conn.executemany(
        "INSERT INTO entities(id, workspace_id, repo_id, name, etype, created_at) "
        "VALUES (?,?,?,?,?,?)",
        [
            ("ent_export_api", wid, rid, "API", "module", 1.0),
            ("ent_export_config", wid, rid, "Config", "module", 1.0),
        ],
    )
    service.store.conn.execute(
        "INSERT INTO edges(id, workspace_id, repo_id, src, dst, relation, layer, "
        "valid_from, ingested_at, provenance) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            "edg_export_graph", wid, rid, "ent_export_api", "ent_export_config",
            "depends_on", "causal", 1.0, 1.0,
            json.dumps({"memory_id": memory["id"]}),
        ),
    )
    service.store.conn.execute(
        "INSERT INTO edge_supports(edge_id, memory_id, source_kind, confidence, "
        "valid_from, ingested_at, provenance) VALUES (?,?,?,?,?,?,?)",
        (
            "edg_export_graph", memory["id"], "manual", 1.0, 1.0, 1.0,
            json.dumps({"memory_id": memory["id"]}),
        ),
    )
    service.store.conn.commit()
    after_graph = service.export_workspace(
        workspace="acme", canonical=True
    )["sha256"]
    assert after_graph != baseline

    service.store.conn.execute(
        "INSERT INTO symbols(id, repo_id, kind, name, fqname, file, lang, "
        "valid_from, ingested_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "sym_export_load_config", rid, "function", "load_config",
            "config.load_config", "config.py", "python", 1.0, 1.0,
        ),
    )
    service.store.conn.execute(
        "INSERT INTO code_files(repo_id, file, lang, content_hash, size_bytes, "
        "mtime_ns, backend, indexed_at) VALUES (?,?,?,?,?,?,?,?)",
        (rid, "config.py", "python", "sha256:test", 42, 1, "test", 1.0),
    )
    service.store.conn.execute(
        "INSERT INTO code_memory_links(id, repo_id, symbol_id, memory_id, relation, "
        "confidence, created_at, valid_from, ingested_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "edg_export_code_memory", rid, "sym_export_load_config", memory["id"],
            "mentions", 1.0, 1.0, 1.0, 1.0,
        ),
    )
    service.store.conn.commit()
    after_code = service.export_workspace(
        workspace="acme", canonical=True
    )["sha256"]

    assert after_code != after_graph
    exported = service.export_workspace(workspace="acme", canonical=True)
    assert exported["counts"]["entities"] == 2
    assert exported["counts"]["edges"] == 1
    assert exported["counts"]["edge_supports"] == 1
    assert exported["counts"]["symbols"] == 1
    assert exported["counts"]["code_files"] == 1
    assert exported["counts"]["code_memory_links"] == 1


def test_member_export_filters_private_session_derivatives_but_admin_export_is_complete():
    service = MemoryService.create(":memory:")
    try:
        set_current_user({
            "id": "usr_alice",
            "email": "alice@example.test",
            "role": "member",
        })
        service.create_workspace("shared", visibility="shared", confirmed=True)
        shared = service.remember(
            "Shared release guidance.", workspace="shared", repo="api",
            scope="repo",
        )
        alice_session = service.start_session(
            "shared", repo="api", agent="codex", goal="internal material"
        )
        private = service.remember(
            "ALICE_EXPORT_PRIVATE_MARKER",
            workspace="shared",
            repo="api",
            session_id=alice_session["session_id"],
            scope="session",
        )
        service.record_event(
            "private_note",
            "ALICE_EXPORT_EVENT_MARKER",
            workspace="shared",
            repo="api",
            session_id=alice_session["session_id"],
            refs=[private["id"]],
        )
        wid = service.store.get_or_create_workspace("shared")
        rid = service.store.get_or_create_repo(wid, "api")
        service.store.conn.execute(
            "UPDATE memories SET session_id=?, metadata=?, provenance=? WHERE id=?",
            (
                alice_session["session_id"],
                json.dumps({"related": private["id"]}),
                json.dumps({"memory_id": private["id"]}),
                shared["id"],
            ),
        )
        service.store.conn.executemany(
            "INSERT INTO entities(id, workspace_id, repo_id, name, etype, created_at) "
            "VALUES (?,?,?,?,?,?)",
            [
                ("ent_alice_secret", wid, rid, "ALICE_EXPORT_ENTITY_MARKER", "secret", 1.0),
                ("ent_alice_target", wid, rid, "Private target", "secret", 1.0),
            ],
        )
        service.store.conn.execute(
            "INSERT INTO edges(id, workspace_id, repo_id, src, dst, relation, layer, "
            "valid_from, ingested_at, provenance) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "edg_alice_private", wid, rid, "ent_alice_secret", "ent_alice_target",
                "related", "semantic", 1.0, 1.0,
                json.dumps({"memory_id": private["id"]}),
            ),
        )
        service.store.conn.execute(
            "INSERT INTO edge_supports(edge_id, memory_id, source_kind, confidence, "
            "valid_from, ingested_at, provenance) VALUES (?,?,?,?,?,?,?)",
            (
                "edg_alice_private", private["id"], "manual", 1.0, 1.0, 1.0,
                json.dumps({"memory_id": private["id"]}),
            ),
        )
        service.store.conn.execute(
            "INSERT INTO memory_entities(id, memory_id, entity_id, workspace_id, repo_id, "
            "source_kind, confidence, valid_from, ingested_at, provenance) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "edg_alice_incidence", private["id"], "ent_alice_secret", wid, rid,
                "manual", 1.0, 1.0, 1.0,
                json.dumps({"memory_id": private["id"]}),
            ),
        )
        service.store.conn.execute(
            "INSERT INTO mem_links(a, b, relation, layer, reason, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (
                private["id"], shared["id"], "related", "semantic",
                "ALICE_EXPORT_LINK_MARKER", 1.0,
            ),
        )
        service.store.conn.execute(
            "INSERT INTO symbols(id, repo_id, kind, name, fqname, file, lang, "
            "valid_from, ingested_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "sym_shared_export", rid, "function", "shared_export",
                "api.shared_export", "api.py", "python", 1.0, 1.0,
            ),
        )
        service.store.conn.execute(
            "INSERT INTO code_memory_links(id, repo_id, symbol_id, memory_id, relation, "
            "confidence, created_at, valid_from, ingested_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "edg_alice_code_link", rid, "sym_shared_export", private["id"],
                "mentions", 1.0, 1.0, 1.0, 1.0,
            ),
        )
        service.store.conn.commit()

        set_current_user({
            "id": "usr_bob",
            "email": "bob@example.test",
            "role": "member",
        })
        bob_export = service.export_workspace(workspace="shared", canonical=True)
        serialized = json.dumps(bob_export, sort_keys=True)
        assert bob_export["visibility"] == "principal"
        assert private["id"] not in serialized
        assert alice_session["session_id"] not in serialized
        assert "alice@example.test" not in serialized
        assert "ALICE_EXPORT_PRIVATE_MARKER" not in serialized
        assert "ALICE_EXPORT_EVENT_MARKER" not in serialized
        assert "ALICE_EXPORT_ENTITY_MARKER" not in serialized
        assert "ALICE_EXPORT_LINK_MARKER" not in serialized
        assert all(
            row["memory_id"] != private["id"]
            for row in bob_export["edge_supports"]
        )
        assert all(
            row["memory_id"] != private["id"]
            for row in bob_export["memory_entities"]
        )
        assert all(
            private["id"] not in {row["a"], row["b"]}
            for row in bob_export["memory_links"]
        )
        assert all(
            row["memory_id"] != private["id"]
            for row in bob_export["code_memory_links"]
        )
        exported_shared = next(
            row for row in bob_export["memories"] if row["id"] == shared["id"]
        )
        assert exported_shared["session_id"] is None
        assert json.loads(exported_shared["metadata"]) == {}
        assert json.loads(exported_shared["provenance"]) == {}

        set_current_user({
            "id": "usr_admin",
            "email": "admin@example.test",
            "role": "admin",
        })
        admin_export = service.export_workspace(workspace="shared", canonical=True)
        assert admin_export["visibility"] == "workspace"
        assert "ALICE_EXPORT_PRIVATE_MARKER" in json.dumps(admin_export)
        assert any(
            row["memory_id"] == private["id"]
            for row in admin_export["edge_supports"]
        )
        assert any(
            row["memory_id"] == private["id"]
            for row in admin_export["memory_entities"]
        )
        assert any(
            private["id"] in {row["a"], row["b"]}
            for row in admin_export["memory_links"]
        )
        assert any(
            row["memory_id"] == private["id"]
            for row in admin_export["code_memory_links"]
        )
    finally:
        set_current_user(None)


def test_canonical_export_includes_more_than_ten_thousand_receipts():
    service = MemoryService.create(":memory:")
    wid = service.store.get_or_create_workspace("acme")
    previous = ""
    rows = []
    for index in range(10_001):
        receipt_id = f"rcpt_{index:026d}"
        payload = json.dumps(
            {
                "version": 1,
                "id": receipt_id,
                "ts_ms": index,
                "operation": "recall",
                "scope_digest": "0" * 24,
                "actor_digest": "1" * 16,
                "target_count": 0,
                "status": "ok",
                "metadata": {},
                "prev_hash": previous,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        receipt_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        rows.append(
            (
                receipt_id, index / 1000.0, "recall", wid, "", index + 1,
                "scope", "actor", 0, "ok", payload, previous, receipt_hash,
            )
        )
        previous = receipt_hash
    service.store.conn.executemany(
        "INSERT INTO operation_receipts(id, ts, operation, workspace_id, repo_id, "
        "sequence, scope_digest, actor, target_count, status, payload, prev_hash, "
        "receipt_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    service.store.conn.execute(
        "INSERT INTO receipt_chain_heads(workspace_id, receipt_count, head_hash, "
        "integrity_error, updated_at) VALUES (?,?,?,?,?)",
        (wid, len(rows), previous, "", 10.001),
    )
    service.store.conn.commit()

    exported = service.export_workspace(workspace="acme", canonical=True)
    receipt_export = service.export_receipts(workspace="acme")
    assert exported["counts"]["receipts"] == 10_001
    assert len(exported["receipts"]) == 10_001
    assert len(receipt_export["entries"]) == 10_001
    assert exported["receipt_verification"]["valid"] is True
    assert receipt_export["verification"]["valid"] is True

    original_digest = exported["sha256"]
    service.store.conn.execute(
        "UPDATE operation_receipts SET receipt_hash=? WHERE id=?",
        ("0" * 64, "rcpt_" + ("0" * 26)),
    )
    service.store.conn.commit()
    tampered = service.export_workspace(workspace="acme", canonical=True)
    assert tampered["sha256"] != original_digest
    assert tampered["receipt_verification"]["valid"] is False


def test_receipt_export_redacts_malformed_payload_and_anchor_text():
    service = MemoryService.create(":memory:")
    stored = service.remember("A safe fact.", workspace="acme")
    wid = service.store.get_or_create_workspace("acme")
    markers = {
        "id": "POISONED_RECEIPT_ID_PRIVATE_MARKER",
        "hash": "POISONED_RECEIPT_HASH_PRIVATE_MARKER",
        "payload": "POISONED_RECEIPT_PAYLOAD_PRIVATE_MARKER",
        "anchor": "POISONED_RECEIPT_ANCHOR_PRIVATE_MARKER",
        "integrity": "POISONED_RECEIPT_INTEGRITY_PRIVATE_MARKER",
        "prev": "POISONED_RECEIPT_PREV_PRIVATE_MARKER",
        "count": "POISONED_RECEIPT_COUNT_PRIVATE_MARKER",
    }
    raw = json.dumps({"secret": markers["payload"]})
    service.store.conn.execute(
        "UPDATE operation_receipts SET id=?, payload=?, receipt_hash=? WHERE id=?",
        (markers["id"], raw, markers["hash"], stored["receipt"]["id"]),
    )
    service.store.conn.execute(
        "UPDATE receipt_chain_heads SET receipt_count=?, head_hash=?, integrity_error=? "
        "WHERE workspace_id=?",
        (markers["count"], markers["anchor"], markers["integrity"], wid),
    )
    service.store.conn.commit()

    exported = service.export_workspace(workspace="acme", canonical=True)
    receipt_export = service.export_receipts(workspace="acme")
    receipt_log = service.receipt_log(workspace="acme")
    direct_verification = service.verify_receipts(workspace="acme")

    other = service.remember("Another safe fact.", workspace="other")
    service.store.conn.execute(
        "UPDATE operation_receipts SET prev_hash=? WHERE id=?",
        (markers["prev"], other["receipt"]["id"]),
    )
    service.store.conn.commit()
    other_export = service.export_receipts(workspace="other")

    encoded = json.dumps({
        "workspace": exported,
        "receipts": receipt_export,
        "receipt_log": receipt_log,
        "verification": direct_verification,
        "other": other_export,
    })
    assert all(marker not in encoded for marker in markers.values())
    assert exported["receipts"][0]["invalid_payload"] is True
    assert "raw_payload" not in exported["receipts"][0]
    assert exported["receipts"][0]["id"].startswith("redacted_sha256:")
    assert exported["receipts"][0]["hash"].startswith("redacted_sha256:")
    assert receipt_export["verification"]["head"].startswith("redacted_sha256:")
    assert all(
        not error["id"] or error["id"].startswith("redacted_sha256:")
        for error in receipt_export["verification"]["errors"]
    )
    assert other_export["entries"][0]["prev_hash"].startswith("redacted_sha256:")
    assert exported["receipt_chain"]["integrity_error"].startswith("redacted_sha256:")
    assert exported["receipt_chain"]["head_hash"].startswith("redacted_sha256:")
    assert exported["receipt_chain"]["receipt_count"] is None
    assert direct_verification["valid"] is False


def test_receipt_export_accepts_declared_terminal_statuses():
    service = MemoryService.create(":memory:")
    wid = service.store.get_or_create_workspace("acme")
    service.store.record_receipt("sync", workspace_id=wid, status="failed")
    service.store.record_receipt("sync", workspace_id=wid, status="cancelled")

    exported = service.export_receipts(workspace="acme")
    assert [row["status"] for row in exported["entries"]] == [
        "failed", "cancelled",
    ]
    assert all("invalid_payload" not in row for row in exported["entries"])


def test_workspace_export_owns_only_the_transaction_it_starts():
    service = MemoryService.create(":memory:")
    service.remember("A safe fact.", workspace="acme")

    service.export_workspace(workspace="acme", canonical=True)
    assert service.store.conn.in_transaction is False

    service.store.conn.execute("BEGIN")
    try:
        service.export_workspace(workspace="acme", canonical=True)
        service.export_receipts(workspace="acme")
        assert service.store.conn.in_transaction is True
    finally:
        service.store.conn.rollback()
