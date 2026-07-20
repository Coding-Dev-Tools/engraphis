"""Encrypted, integrity-checked backups for customer and vendor SQLite state.

The command intentionally writes only to an explicitly supplied off-volume directory.
Production should mount remote backup storage there (or upload the encrypted artifact
immediately) and publish the marker path as ENGRAPHIS_BACKUP_STATUS_FILE.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import stat
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Optional


MAGIC = b"ENGRAPHIS-BACKUP-V1\n"
PREFIX = "engraphis-backup-"
STATUS_SCHEMA = "engraphis-backup-status/v1"
DATABASE_NAMES = {"memory.db", "users.db", "relay.db", "webhooks.db"}
CUSTOMER_STATE_NAMES = frozenset({
    ".clock_anchor",
    "lease.sig",
    "license.key",
    "machine_id",
    "sync.read_only",
    "sync.token",
    "trial.json",
    "trial_used.json",
})
VENDOR_OPERATOR_STATE_NAMES = frozenset({"undelivered_license_keys.tsv"})
PRIVATE_STATE_NAMES = CUSTOMER_STATE_NAMES | VENDOR_OPERATOR_STATE_NAMES
STATE_ARCHIVE_DIR = ".engraphis"
RESTORE_PLAN_NAME = "RESTORE_PLAN.json"
MAX_STATE_FILE_BYTES = 1024 * 1024
VENDOR_REQUIRED_DATABASES = {"relay.db", "webhooks.db"}
VENDOR_REQUIRED_SCHEMA = {
    "relay.db": {
        "issued_licenses": {"key_id", "subscription_id", "order_id"},
    },
    "webhooks.db": {
        "processed": {"webhook_id", "state"},
        "subscription_seats": {"subscription_id", "seats", "event_ts"},
    },
}
CHUNK_SIZE = 1024 * 1024


def _fsync_directory(path: Path) -> None:
    """Best-effort directory sync after an atomic replace (unsupported on Windows)."""
    try:
        descriptor = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _key() -> bytes:
    raw = os.environ.get("ENGRAPHIS_BACKUP_KEY", "").strip()
    if len(raw) != 64:
        raise SystemExit("ENGRAPHIS_BACKUP_KEY must be exactly 64 hexadecimal characters")
    try:
        return bytes.fromhex(raw)
    except ValueError as exc:
        raise SystemExit("ENGRAPHIS_BACKUP_KEY must be hexadecimal") from exc


def _aesgcm(key: bytes):
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:
        raise SystemExit("encrypted backups require cryptography (install engraphis[all])") from exc
    return AESGCM(key)


def _stream_cipher():
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as exc:
        raise SystemExit("encrypted backups require cryptography (install engraphis[all])") from exc
    return Cipher, algorithms, modes


def _database_path(raw: str) -> Optional[Path]:
    """Resolve a file-backed SQLite path, preserving ``:memory:`` as absent."""
    raw = raw.strip()
    if not raw or raw == ":memory:":
        return None
    return Path(raw).expanduser().resolve()


def _webhook_state_path() -> Optional[Path]:
    """Return the exact durable Polar state path used by ``engraphis.billing``.

    Billing accepts an explicit ``ENGRAPHIS_WEBHOOK_STATE`` path, or derives its
    default next to an explicitly configured ``ENGRAPHIS_DB_PATH``. Keep this
    resolution deliberately aligned so a backup cannot silently snapshot a
    different, empty database while order claims and seat baselines remain live.
    """
    override = _database_path(os.environ.get("ENGRAPHIS_WEBHOOK_STATE", ""))
    if override is not None:
        return override
    configured_db = _database_path(os.environ.get("ENGRAPHIS_DB_PATH", ""))
    if configured_db is not None:
        return configured_db.parent / ".engraphis_webhooks.db"
    from engraphis.commercial import service_mode
    if service_mode() == "vendor":
        relay = _database_path(os.environ.get("ENGRAPHIS_RELAY_DB", ""))
        if relay is not None:
            return relay.parent / "polar-webhooks.db"
        state_raw = os.environ.get("ENGRAPHIS_STATE_DIR", "").strip()
        state_dir = (Path(state_raw).expanduser().resolve() if state_raw
                     else (Path.home() / ".engraphis").resolve())
        return state_dir / "polar-webhooks.db"
    return None


def _reviewed_state_sources(
        candidates: list[tuple[str, Path]], expected_parent: Path) -> list[tuple[str, Path]]:
    """Validate one explicit private-state allowlist without walking a directory."""
    sources = []
    for name, candidate in candidates:
        if candidate.is_symlink():
            raise SystemExit("private state file must not be a symbolic link: %s" % name)
        if not candidate.exists():
            continue
        try:
            resolved = candidate.resolve(strict=True)
            info = resolved.stat()
        except OSError as exc:
            raise SystemExit("private state file could not be inspected: %s" % name) from exc
        if resolved.parent != expected_parent or not resolved.is_file() \
                or info.st_size <= 0 or info.st_size > MAX_STATE_FILE_BYTES:
            raise SystemExit("private state file is invalid or too large: %s" % name)
        sources.append((name, resolved))
    return sources


def _private_state_sources() -> list[tuple[str, Path]]:
    """Return only reviewed customer or vendor recovery files.

    Never walk the state directory: it can also contain relay databases, operator files,
    or future secrets. Vendor mode includes only the manual-fulfillment fallback produced
    when a paid key was minted but the durable email outbox could not accept it. A signing
    seed can never enter this archive even if it is accidentally placed beside that file.
    """
    from engraphis.config import settings

    if settings.service_mode == "vendor":
        webhook_state = _webhook_state_path()
        if webhook_state is None:
            return []
        parent = webhook_state.parent.resolve()
        return _reviewed_state_sources([
            (name, parent / name) for name in sorted(VENDOR_OPERATOR_STATE_NAMES)
        ], parent)
    state_raw = os.environ.get("ENGRAPHIS_STATE_DIR", "").strip()
    state_dir = Path(state_raw).expanduser() if state_raw else Path.home() / ".engraphis"
    state_dir = state_dir.resolve()
    return _reviewed_state_sources([
        (name, state_dir / name) for name in sorted(CUSTOMER_STATE_NAMES)
    ], state_dir)


def _restore_plan(databases: list[dict], state_files: list[dict]) -> dict:
    """Describe an explicit, stopped-service copy plan for this staging restore.

    Restore never writes a live path. The operator configures the intended target
    environment, restores into an empty staging directory, reviews this file, stops the
    service, and then copies each verified file to its listed destination.
    """
    from engraphis.config import settings

    state_raw = os.environ.get("ENGRAPHIS_STATE_DIR", "").strip()
    state_dir = (Path(state_raw).expanduser() if state_raw else Path.home() / ".engraphis"
                 ).resolve()
    database = _database_path(settings.db_path)
    relay = _database_path(os.environ.get("ENGRAPHIS_RELAY_DB", "")) or (
        state_dir / "relay.db")
    webhooks = _webhook_state_path()
    destinations = {
        "memory.db": (str(database) if database else "<set file-backed ENGRAPHIS_DB_PATH>"),
        "users.db": (str(Path(str(database) + ".users.db")) if database
                     else "<set file-backed ENGRAPHIS_DB_PATH>.users.db"),
        "relay.db": str(relay),
        "webhooks.db": (str(webhooks) if webhooks
                        else "<set ENGRAPHIS_WEBHOOK_STATE to a durable file>"),
    }
    files = [
        {
            "staged": item["name"],
            "destination": destinations[item["name"]],
            "mode": "0600",
        }
        for item in databases
    ]
    operator_dir = webhooks.parent if webhooks else state_dir
    files.extend({
        "staged": "%s/%s" % (STATE_ARCHIVE_DIR, item["name"]),
        "destination": str(
            (operator_dir if item["name"] in VENDOR_OPERATOR_STATE_NAMES else state_dir)
            / item["name"]),
        "mode": "0600",
    } for item in state_files)
    return {
        "schema": "engraphis-restore-plan/v1",
        "service_must_be_stopped": True,
        "automatic_overwrite": False,
        "files": files,
        "instructions": [
            "Verify these destinations match the target deployment configuration.",
            "Stop every service that can write the listed databases or state files.",
            "Copy each staged file to its destination with owner-only permissions.",
            "Start the service, run a new encrypted backup, then verify serving and "
            "authenticated operations readiness.",
        ],
    }


def _validate_vendor_schema(sources: list[tuple[str, Path]]) -> None:
    paths = dict(sources)
    for database_name, tables in VENDOR_REQUIRED_SCHEMA.items():
        path = paths[database_name]
        try:
            conn = sqlite3.connect(str(path), timeout=30)
            try:
                for table, required_columns in tables.items():
                    columns = {
                        str(row[1]) for row in conn.execute(
                            "PRAGMA table_info(%s)" % table).fetchall()}
                    missing = sorted(required_columns - columns)
                    if missing:
                        raise SystemExit(
                            "vendor backup requires initialized %s table %s with "
                            "columns: %s" % (
                                database_name, table,
                                ", ".join(sorted(required_columns))))
            finally:
                conn.close()
        except sqlite3.Error as exc:
            raise SystemExit(
                "vendor backup could not inspect managed state database: %s"
                % database_name) from exc


def _sources() -> list[tuple[str, Path]]:
    from engraphis.config import settings
    db = _database_path(settings.db_path)
    state = Path(os.environ.get("ENGRAPHIS_STATE_DIR", "").strip()
                 or (Path.home() / ".engraphis")).expanduser().resolve()
    relay = Path(os.environ.get("ENGRAPHIS_RELAY_DB", "").strip()
                 or (state / "relay.db")).expanduser().resolve()
    if settings.service_mode == "vendor":
        # The control-plane artifact must never absorb a colocated customer memory or
        # dashboard-auth database. Its reviewed recovery surface is exactly the license
        # registry/outbox and Polar delivery ledger.
        candidates: list[tuple[str, Optional[Path]]] = [
            ("relay.db", relay),
            ("webhooks.db", _webhook_state_path()),
        ]
    else:
        candidates = [
            ("memory.db", db),
            ("users.db", Path(str(db) + ".users.db") if db is not None else None),
            ("relay.db", relay),
            ("webhooks.db", _webhook_state_path()),
        ]
    sources = [(name, path) for name, path in candidates
               if path is not None and path.is_file()]
    if settings.service_mode == "vendor":
        available = {name for name, _path in sources}
        missing = sorted(VENDOR_REQUIRED_DATABASES - available)
        if missing:
            raise SystemExit(
                "vendor backup requires durable managed state databases: %s; "
                "configure ENGRAPHIS_RELAY_DB and ENGRAPHIS_WEBHOOK_STATE and "
                "initialize both stores before backup" % ", ".join(missing))
        _validate_vendor_schema(sources)
    return sources


def _integrity(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()
    if result != "ok":
        raise RuntimeError("SQLite integrity check failed for %s" % path.name)


def _snapshot(source: Path, target: Path) -> None:
    src = sqlite3.connect(str(source), timeout=30)
    dst = sqlite3.connect(str(target))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    _integrity(target)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _copy_private_state(source: Path, target: Path) -> None:
    """Snapshot one bounded regular file without following a last-component symlink."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(str(source), flags)
    try:
        info = os.fstat(descriptor)
        linked = os.lstat(source)
        if not stat.S_ISREG(info.st_mode) or info.st_size <= 0 \
                or info.st_size > MAX_STATE_FILE_BYTES or info.st_nlink != 1 \
                or not stat.S_ISREG(linked.st_mode) \
                or not os.path.samestat(info, linked):
            raise SystemExit("private state file changed or is too large: %s" % source.name)
        target.parent.mkdir(parents=True, exist_ok=True)
        copied = 0
        with os.fdopen(descriptor, "rb", closefd=False) as input_handle, target.open("xb") as output:
            while True:
                chunk = input_handle.read(min(CHUNK_SIZE, MAX_STATE_FILE_BYTES + 1 - copied))
                if not chunk:
                    break
                copied += len(chunk)
                if copied > MAX_STATE_FILE_BYTES:
                    raise SystemExit("private state file is too large: %s" % source.name)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if copied != info.st_size:
            raise SystemExit("private state file changed during backup: %s" % source.name)
        try:
            target.chmod(0o600)
        except OSError:
            pass
    finally:
        os.close(descriptor)


def _archive_to_path(sources: list[tuple[str, Path]], archive_path: Path,
                     state_sources: Optional[list[tuple[str, Path]]] = None) -> None:
    if not sources:
        raise SystemExit("no Engraphis SQLite databases were found to back up")
    state_sources = state_sources or []
    with tempfile.TemporaryDirectory(prefix="engraphis-backup-") as temp:
        root = Path(temp)
        inventory = {"created_at": time.time(), "databases": [], "state_files": []}
        for name, source in sources:
            target = root / name
            _snapshot(source, target)
            inventory["databases"].append({
                "name": name, "bytes": target.stat().st_size,
                "sha256": _sha256_file(target)})
        for name, source in state_sources:
            target = root / STATE_ARCHIVE_DIR / name
            _copy_private_state(source, target)
            inventory["state_files"].append({
                "name": name, "bytes": target.stat().st_size,
                "sha256": _sha256_file(target), "mode": "0600"})
        with zipfile.ZipFile(
                archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("inventory.json", json.dumps(inventory, sort_keys=True))
            for item in inventory["databases"]:
                archive.write(root / item["name"], item["name"])
            for item in inventory["state_files"]:
                archive.write(
                    root / STATE_ARCHIVE_DIR / item["name"],
                    "%s/%s" % (STATE_ARCHIVE_DIR, item["name"]),
                )


def _archive(sources: list[tuple[str, Path]]) -> bytes:
    """Compatibility helper for callers that explicitly need an in-memory archive."""
    with tempfile.TemporaryDirectory(prefix="engraphis-archive-") as temp:
        target = Path(temp) / "backup.zip"
        _archive_to_path(sources, target)
        return target.read_bytes()


def _atomic_write(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    """Durably replace a file without exposing a partial artifact or marker."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    temporary = path.with_name(".%s.%s.tmp" % (path.name, secrets.token_hex(8)))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(str(temporary), flags, mode)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
        try:
            os.chmod(path, mode)
        except OSError:
            pass
    finally:
        if temporary.exists():
            temporary.unlink()


def _encrypt_path(plain_path: Path, target: Path) -> None:
    """Write the AES-GCM V1 format incrementally so database size does not drive RAM."""
    Cipher, algorithms, modes = _stream_cipher()
    nonce = secrets.token_bytes(12)
    encryptor = Cipher(algorithms.AES(_key()), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(MAGIC)
    temporary = target.with_name(".%s.%s.tmp" % (target.name, secrets.token_hex(8)))
    try:
        with plain_path.open("rb") as source, temporary.open("xb") as output:
            output.write(MAGIC)
            output.write(nonce)
            while True:
                chunk = source.read(CHUNK_SIZE)
                if not chunk:
                    break
                output.write(encryptor.update(chunk))
            output.write(encryptor.finalize())
            output.write(encryptor.tag)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
        _fsync_directory(target.parent)
        try:
            target.parent.chmod(0o700)
        except OSError:
            pass
        try:
            target.chmod(0o600)
        except OSError:
            pass
    finally:
        if temporary.exists():
            temporary.unlink()


def _decrypt_to_path(path: Path, target: Path) -> None:
    Cipher, algorithms, modes = _stream_cipher()
    size = path.stat().st_size
    minimum = len(MAGIC) + 12 + 16
    if size < minimum:
        raise RuntimeError("not an Engraphis encrypted backup")
    with path.open("rb") as source:
        if source.read(len(MAGIC)) != MAGIC:
            raise RuntimeError("not an Engraphis encrypted backup")
        nonce = source.read(12)
        ciphertext_size = size - len(MAGIC) - 12 - 16
        source.seek(-16, os.SEEK_END)
        tag = source.read(16)
        source.seek(len(MAGIC) + 12)
        decryptor = Cipher(algorithms.AES(_key()), modes.GCM(nonce, tag)).decryptor()
        decryptor.authenticate_additional_data(MAGIC)
        remaining = ciphertext_size
        with target.open("xb") as output:
            while remaining:
                chunk = source.read(min(CHUNK_SIZE, remaining))
                if not chunk:
                    raise RuntimeError("encrypted backup is truncated")
                remaining -= len(chunk)
                output.write(decryptor.update(chunk))
            output.write(decryptor.finalize())
            output.flush()
            os.fsync(output.fileno())


def _decrypt(path: Path) -> bytes:
    payload = path.read_bytes()
    if not payload.startswith(MAGIC) or len(payload) < len(MAGIC) + 12 + 16:
        raise RuntimeError("not an Engraphis encrypted backup")
    offset = len(MAGIC)
    nonce, ciphertext = payload[offset:offset + 12], payload[offset + 12:]
    return _aesgcm(_key()).decrypt(nonce, ciphertext, MAGIC)


def _verify_archive(archive_path: Path, restore_dir: Optional[Path] = None) -> dict:
    with tempfile.TemporaryDirectory(prefix="engraphis-restore-") as temp:
        root = Path(temp)
        with zipfile.ZipFile(archive_path, "r") as archive:
            listed_names = archive.namelist()
            names = set(listed_names)
            if "inventory.json" not in names or len(names) != len(listed_names):
                raise RuntimeError("backup archive contains an invalid path")
            try:
                inventory = json.loads(archive.read("inventory.json").decode("utf-8"))
            except (KeyError, UnicodeDecodeError, ValueError) as exc:
                raise RuntimeError("backup inventory is invalid") from exc
            databases = inventory.get("databases") if isinstance(inventory, dict) else None
            if not isinstance(databases, list) or not databases:
                raise RuntimeError("backup inventory contains no databases")
            state_files = inventory.get("state_files", [])
            if not isinstance(state_files, list):
                raise RuntimeError("backup inventory state-file list is invalid")
            inventory_names = [item.get("name") for item in databases
                               if isinstance(item, dict)]
            state_names = [item.get("name") for item in state_files
                           if isinstance(item, dict)]
            if len(inventory_names) != len(databases) \
                    or not all(isinstance(name, str) for name in inventory_names) \
                    or len(set(inventory_names)) != len(inventory_names) \
                    or not set(inventory_names).issubset(DATABASE_NAMES):
                raise RuntimeError("backup inventory database list is invalid")
            if len(state_names) != len(state_files) \
                    or not all(isinstance(name, str) for name in state_names) \
                    or len(set(state_names)) != len(state_names) \
                    or not set(state_names).issubset(PRIVATE_STATE_NAMES):
                raise RuntimeError("backup inventory state-file list is invalid")
            expected_members = {"inventory.json", *inventory_names}
            expected_members.update(
                "%s/%s" % (STATE_ARCHIVE_DIR, name) for name in state_names)
            if names != expected_members:
                raise RuntimeError("backup archive contains an invalid path")
            archive.extractall(root)
        for item in databases:
            path = root / item["name"]
            try:
                expected_size = int(item["bytes"])
                expected_hash = str(item["sha256"])
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError("backup inventory database entry is invalid") from exc
            if expected_size <= 0 or path.stat().st_size != expected_size \
                    or len(expected_hash) != 64 \
                    or any(c not in "0123456789abcdef" for c in expected_hash) \
                    or _sha256_file(path) != expected_hash:
                raise RuntimeError("backup checksum mismatch for %s" % item["name"])
            _integrity(path)
        for item in state_files:
            path = root / STATE_ARCHIVE_DIR / item["name"]
            try:
                expected_size = int(item["bytes"])
                expected_hash = str(item["sha256"])
                expected_mode = str(item["mode"])
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError("backup inventory state-file entry is invalid") from exc
            if expected_size <= 0 or expected_size > MAX_STATE_FILE_BYTES \
                    or path.stat().st_size != expected_size or expected_mode != "0600" \
                    or len(expected_hash) != 64 \
                    or any(c not in "0123456789abcdef" for c in expected_hash) \
                    or _sha256_file(path) != expected_hash:
                raise RuntimeError("backup checksum mismatch for state file %s" % item["name"])
        if restore_dir is not None:
            restore_dir = restore_dir.expanduser().resolve()
            if restore_dir.exists():
                raise FileExistsError("restore output already exists: %s" % restore_dir)
            restore_dir.parent.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(
                prefix=".%s.restore-" % restore_dir.name, dir=str(restore_dir.parent)))
            try:
                for item in databases:
                    target = staging / item["name"]
                    shutil.copy2(root / item["name"], target)
                    try:
                        target.chmod(0o600)
                    except OSError:
                        pass
                if state_files:
                    state_target = staging / STATE_ARCHIVE_DIR
                    state_target.mkdir(mode=0o700)
                    for item in state_files:
                        target = state_target / item["name"]
                        shutil.copy2(root / STATE_ARCHIVE_DIR / item["name"], target)
                        try:
                            target.chmod(0o600)
                        except OSError:
                            pass
                _atomic_write(
                    staging / RESTORE_PLAN_NAME,
                    json.dumps(
                        _restore_plan(databases, state_files), indent=2, sort_keys=True,
                    ).encode("utf-8") + b"\n",
                )
                staging.rename(restore_dir)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
        return inventory


def _verify_plain(plain: bytes, restore_dir: Optional[Path] = None) -> dict:
    with tempfile.TemporaryDirectory(prefix="engraphis-plain-") as temp:
        archive_path = Path(temp) / "backup.zip"
        archive_path.write_bytes(plain)
        return _verify_archive(archive_path, restore_dir)


def _verify_artifact(path: Path, restore_dir: Optional[Path] = None) -> dict:
    with tempfile.TemporaryDirectory(prefix="engraphis-decrypt-") as temp:
        archive_path = Path(temp) / "backup.zip"
        _decrypt_to_path(path, archive_path)
        return _verify_archive(archive_path, restore_dir)


def backup(output_dir: Path, marker: Path, retention_days: int,
           allow_same_device: bool) -> Path:
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    sources = _sources()
    state_sources = _private_state_sources()
    if not allow_same_device:
        output_device = output_dir.stat().st_dev
        if any(path.stat().st_dev == output_device
               for _name, path in [*sources, *state_sources]):
            raise SystemExit(
                "backup destination is on the same device as live data; mount off-volume "
                "storage or pass --allow-same-device only for a drill")
    stamp = "%s-%s" % (
        time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()), secrets.token_hex(4))
    target = (output_dir / (PREFIX + stamp + ".egbak")).resolve()
    with tempfile.TemporaryDirectory(prefix="engraphis-pack-") as temp:
        archive_path = Path(temp) / "backup.zip"
        _archive_to_path(sources, archive_path, state_sources)
        _encrypt_path(archive_path, target)
    _verify_artifact(target)
    marker = marker.expanduser().resolve()
    completed_at = time.time()
    _atomic_write(marker, json.dumps({
        "schema": STATUS_SCHEMA,
        "artifact": str(target),
        "bytes": target.stat().st_size,
        "created_at": completed_at,
        "sha256": _sha256_file(target),
    }, sort_keys=True).encode("utf-8"))
    os.utime(marker, (completed_at, completed_at))
    cutoff = time.time() - max(1, retention_days) * 86400
    for candidate in output_dir.glob(PREFIX + "*.egbak"):
        if candidate.is_symlink():
            continue
        resolved = candidate.resolve()
        if resolved.parent == output_dir and resolved != target \
                and resolved.stat().st_mtime < cutoff:
            candidate.unlink()
    return target


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("backup")
    create.add_argument("--output-dir", type=Path, required=True)
    create.add_argument("--marker", type=Path, required=True)
    create.add_argument("--retention-days", type=int, default=30)
    create.add_argument("--allow-same-device", action="store_true")
    verify = sub.add_parser("verify")
    verify.add_argument("artifact", type=Path)
    restore = sub.add_parser(
        "restore",
        help="verify into empty staging and emit RESTORE_PLAN.json; never writes live state",
    )
    restore.add_argument("artifact", type=Path)
    restore.add_argument(
        "--output-dir", type=Path, required=True,
        help="new staging directory (review RESTORE_PLAN.json before stopped-service copy)",
    )
    args = parser.parse_args()
    if args.command == "backup":
        result = backup(args.output_dir, args.marker, args.retention_days,
                        args.allow_same_device)
        print(result)
    elif args.command == "verify":
        print(json.dumps(_verify_artifact(args.artifact.resolve()), sort_keys=True))
    else:
        output = args.output_dir.expanduser().resolve()
        print(json.dumps(_verify_artifact(args.artifact.resolve(), output),
                         sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
