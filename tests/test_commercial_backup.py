"""Focused safety coverage for encrypted commercial backup and restore."""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest


def _database(path: Path, statements: list[tuple[str, tuple]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        for sql, parameters in statements:
            conn.execute(sql, parameters)
        conn.commit()
    finally:
        conn.close()


def _vendor_environment(monkeypatch, tmp_path):
    from engraphis.config import settings

    relay = tmp_path / "live" / "relay.db"
    webhooks = tmp_path / "live" / "polar-webhooks.db"
    monkeypatch.setattr(settings, "service_mode", "vendor")
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "live" / "memory.db"))
    monkeypatch.setenv("ENGRAPHIS_RELAY_DB", str(relay))
    monkeypatch.setenv("ENGRAPHIS_WEBHOOK_STATE", str(webhooks))
    monkeypatch.setenv("ENGRAPHIS_BACKUP_KEY", "42" * 32)
    return relay, webhooks


def test_vendor_backup_fails_closed_when_managed_state_is_missing(monkeypatch, tmp_path):
    pytest.importorskip("cryptography")
    from scripts import commercial_backup

    relay, webhooks = _vendor_environment(monkeypatch, tmp_path)
    _database(relay, [("CREATE TABLE issued_licenses(id TEXT PRIMARY KEY)", ())])
    marker = tmp_path / "status" / "backup.json"
    output = tmp_path / "artifacts"

    with pytest.raises(SystemExit, match=r"webhooks\.db"):
        commercial_backup.backup(output, marker, 30, allow_same_device=True)
    assert not marker.exists()
    assert list(output.glob("*.egbak")) == []

    _database(webhooks, [("CREATE TABLE processed(webhook_id TEXT PRIMARY KEY)", ())])
    relay.unlink()
    with pytest.raises(SystemExit, match=r"relay\.db"):
        commercial_backup.backup(output, marker, 30, allow_same_device=True)
    assert not marker.exists()
    assert list(output.glob("*.egbak")) == []


def test_vendor_backup_rejects_uninitialized_polar_state(monkeypatch, tmp_path):
    from scripts import commercial_backup

    relay, webhooks = _vendor_environment(monkeypatch, tmp_path)
    _database(relay, [
        ("CREATE TABLE issued_licenses("
         "key_id TEXT PRIMARY KEY, subscription_id TEXT, order_id TEXT)", ()),
    ])
    _database(webhooks, [
        ("CREATE TABLE processed(webhook_id TEXT PRIMARY KEY, state TEXT)", ()),
    ])

    marker = tmp_path / "status" / "backup.json"
    output = tmp_path / "artifacts"
    with pytest.raises(SystemExit, match=r"subscription_seats"):
        commercial_backup.backup(output, marker, 30, allow_same_device=True)
    assert not marker.exists()
    assert list(output.glob("*.egbak")) == []


def test_vendor_backup_restores_registry_order_and_seat_state(monkeypatch, tmp_path):
    pytest.importorskip("cryptography")
    from scripts import commercial_backup

    relay, webhooks = _vendor_environment(monkeypatch, tmp_path)
    # Even if stale/customer databases are colocated or accidentally configured on the
    # vendor service, the control-plane artifact must never absorb their contents.
    memory = tmp_path / "live" / "memory.db"
    users = Path(str(memory) + ".users.db")
    _database(memory, [("CREATE TABLE private_memory(value TEXT)", ())])
    _database(users, [("CREATE TABLE password_hashes(value TEXT)", ())])
    _database(relay, [
        ("CREATE TABLE issued_licenses("
         "key_id TEXT PRIMARY KEY, subscription_id TEXT, order_id TEXT)", ()),
        ("INSERT INTO issued_licenses VALUES (?, ?, ?)",
         ("license-1", "subscription-1", "order-1")),
    ])
    _database(webhooks, [
        ("CREATE TABLE processed(webhook_id TEXT PRIMARY KEY, state TEXT)", ()),
        ("INSERT INTO processed VALUES (?, ?)", ("delivery-1", "fulfilled")),
        ("CREATE TABLE subscription_seats("
         "subscription_id TEXT PRIMARY KEY, seats INTEGER, event_ts REAL)", ()),
        ("INSERT INTO subscription_seats VALUES (?, ?, ?)", ("sub-1", 7, 1234.0)),
    ])
    fallback = webhooks.parent / "undelivered_license_keys.tsv"
    fallback.write_text(
        "1784485000\tbuyer@example.com\tPro\tENGR1.payload.signature\n",
        encoding="utf-8")
    # The backup source is a strict single-file allowlist. In particular, colocating a
    # vendor signer beside the fallback must never put the signing seed in an archive.
    (webhooks.parent / "vendor_signing.key").write_text("ab" * 32, encoding="ascii")

    marker = tmp_path / "status" / "backup.json"
    artifact = commercial_backup.backup(
        tmp_path / "artifacts", marker, 30, allow_same_device=True)
    inventory = commercial_backup._verify_artifact(artifact)
    assert {item["name"] for item in inventory["databases"]} == {
        "relay.db", "webhooks.db"}
    assert {item["name"] for item in inventory["state_files"]} == {
        "undelivered_license_keys.tsv"}

    restored = tmp_path / "empty-staging-restore"
    commercial_backup._verify_artifact(artifact, restored)
    relay_conn = sqlite3.connect(str(restored / "relay.db"))
    webhook_conn = sqlite3.connect(str(restored / "webhooks.db"))
    try:
        assert relay_conn.execute(
            "SELECT key_id FROM issued_licenses WHERE order_id='order-1'"
        ).fetchone() == ("license-1",)
        assert webhook_conn.execute(
            "SELECT state FROM processed WHERE webhook_id='delivery-1'"
        ).fetchone() == ("fulfilled",)
        assert webhook_conn.execute(
            "SELECT seats, event_ts FROM subscription_seats WHERE subscription_id='sub-1'"
        ).fetchone() == (7, 1234.0)
        assert (restored / ".engraphis" / "undelivered_license_keys.tsv").read_text(
            encoding="utf-8") == fallback.read_text(encoding="utf-8")
        assert not (restored / ".engraphis" / "vendor_signing.key").exists()
        plan = json.loads((restored / "RESTORE_PLAN.json").read_text(encoding="utf-8"))
        fallback_plan = next(
            item for item in plan["files"]
            if item["staged"] == ".engraphis/undelivered_license_keys.tsv")
        assert fallback_plan["destination"] == str(fallback)
        assert fallback_plan["mode"] == "0600"
    finally:
        webhook_conn.close()
        relay_conn.close()


def test_webhook_state_default_matches_billing_database_location(monkeypatch, tmp_path):
    from scripts import commercial_backup

    configured = tmp_path / "managed" / "memory.db"
    monkeypatch.delenv("ENGRAPHIS_WEBHOOK_STATE", raising=False)
    monkeypatch.setenv("ENGRAPHIS_DB_PATH", str(configured))
    assert commercial_backup._webhook_state_path() == (
        configured.parent / ".engraphis_webhooks.db").resolve()


def test_vendor_default_aligns_webhook_fallback_and_backup_paths(monkeypatch, tmp_path):
    from engraphis.config import settings
    from engraphis.inspector import webhooks
    from scripts import commercial_backup

    relay = tmp_path / "managed" / "relay.db"
    monkeypatch.setattr(settings, "service_mode", "vendor")
    monkeypatch.delenv("ENGRAPHIS_WEBHOOK_STATE", raising=False)
    monkeypatch.delenv("ENGRAPHIS_DB_PATH", raising=False)
    monkeypatch.setenv("ENGRAPHIS_RELAY_DB", str(relay))

    expected = relay.parent / "polar-webhooks.db"
    assert commercial_backup._webhook_state_path() == expected.resolve()
    assert webhooks._fallback_dir() == expected.resolve().parent


def test_private_state_snapshot_rejects_hardlinks(tmp_path):
    from scripts import commercial_backup

    original = tmp_path / "unreviewed-secret"
    original.write_text("do-not-archive", encoding="utf-8")
    allowlisted_name = tmp_path / "license.key"
    try:
        os.link(str(original), str(allowlisted_name))
    except (NotImplementedError, OSError):
        pytest.skip("this filesystem cannot create test hardlinks")

    target = tmp_path / "snapshot" / "license.key"
    with pytest.raises(SystemExit, match="private state file changed"):
        commercial_backup._copy_private_state(allowlisted_name, target)
    assert not target.exists()
