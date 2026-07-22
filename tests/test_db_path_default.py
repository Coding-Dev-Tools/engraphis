"""Default DB location and the preservation-first upgrade path for installed builds.

A DB defaulting into site-packages is silently deleted by ``pip install -U``
(first-run data loss) — the 0.9.8 fix routes installed builds to a platform data dir
and migrates any database an earlier install left behind.
Pure-function test of both branches; no monkeypatching of module state.
"""
from pathlib import Path, PureWindowsPath

from concurrent.futures import ThreadPoolExecutor
import os
import sqlite3

import pytest

from engraphis.config import (
    Settings,
    _configured_db_path,
    _default_db_path,
    _prepare_installed_db_default,
)
from engraphis.private_state import UnsafeStateFile


def test_source_checkout_keeps_repo_root():
    root = Path("/home/dev/src/engraphis")
    assert _default_db_path(root) == str(root / "engraphis.db")


def test_site_packages_install_moves_to_user_data_dir():
    root = Path("/usr/lib/python3.11/site-packages")
    out = _default_db_path(root)
    assert "site-packages" not in out and out.endswith("engraphis.db")
    assert "engraphis" in Path(out).parent.parts[-1]


def test_dist_packages_install_also_detected():
    out = _default_db_path(Path("/usr/lib/python3/dist-packages"))
    assert "dist-packages" not in out and out.endswith("engraphis.db")


def test_exact_platform_defaults():
    home = Path("/home/alice")
    root = Path("/opt/python/site-packages")
    assert _default_db_path(
        root, os_name="posix", platform="linux", environ={}, home=home
    ) == "/home/alice/.local/share/engraphis/engraphis.db"
    assert _default_db_path(
        root, os_name="posix", platform="linux",
        environ={"XDG_DATA_HOME": "/data/alice"}, home=home,
    ) == "/data/alice/engraphis/engraphis.db"
    assert _default_db_path(
        root, os_name="posix", platform="darwin", environ={}, home=home
    ) == "/home/alice/Library/Application Support/engraphis/engraphis.db"
    assert _default_db_path(
        root, os_name="nt", platform="win32",
        environ={"LOCALAPPDATA": "C:/Users/Alice/AppData/Local"}, home=home,
    ) == str(PureWindowsPath(
        "C:/Users/Alice/AppData/Local", "engraphis", "engraphis.db"
    ))


def test_env_override_wins(monkeypatch):
    monkeypatch.setenv("ENGRAPHIS_DB_PATH", "/custom/spot.db")
    assert Settings().db_path == "/custom/spot.db"


def _sqlite(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE sentinel(value TEXT)")
    conn.execute("INSERT INTO sentinel VALUES(?)", (value,))
    conn.commit()
    conn.close()


def _value(path):
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute("SELECT value FROM sentinel").fetchone()[0]
    finally:
        conn.close()


def test_pre_1_0_memory_and_auth_databases_are_preserved_and_migrated(tmp_path):
    root = tmp_path / "venv" / "Lib" / "site-packages"
    legacy = root / "engraphis.db"
    legacy_users = Path(str(legacy) + ".users.db")
    target = tmp_path / "local" / "engraphis" / "engraphis.db"
    _sqlite(legacy, "memory-data")
    _sqlite(legacy_users, "auth-data")

    assert _prepare_installed_db_default(root, target) == target
    assert _value(target) == "memory-data"
    assert _value(Path(str(target) + ".users.db")) == "auth-data"
    assert _value(legacy) == "memory-data"
    assert _value(legacy_users) == "auth-data"


def test_concurrent_first_start_serializes_migration(tmp_path):
    root = tmp_path / "site-packages"
    legacy = root / "engraphis.db"
    legacy_users = Path(str(legacy) + ".users.db")
    target = tmp_path / "data" / "engraphis.db"
    _sqlite(legacy, "memory-data")
    _sqlite(legacy_users, "auth-data")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda _index: _prepare_installed_db_default(root, target), range(2)
        ))

    assert results == [target, target]
    assert _value(target) == "memory-data"
    assert _value(Path(str(target) + ".users.db")) == "auth-data"


def test_upgrade_includes_committed_wal_content(tmp_path):
    root = tmp_path / "site-packages"
    legacy = root / "engraphis.db"
    target = tmp_path / "data" / "engraphis.db"
    root.mkdir(parents=True)
    conn = sqlite3.connect(str(legacy))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE sentinel(value TEXT)")
    conn.execute("INSERT INTO sentinel VALUES('from-wal')")
    conn.commit()
    try:
        _prepare_installed_db_default(root, target)
        assert _value(target) == "from-wal"
    finally:
        conn.close()


def test_existing_current_database_wins_without_overwrite(tmp_path, capsys):
    root = tmp_path / "site-packages"
    legacy = root / "engraphis.db"
    target = tmp_path / "data" / "engraphis.db"
    _sqlite(legacy, "legacy")
    _sqlite(target, "current")
    assert _prepare_installed_db_default(root, target) == target
    assert _value(target) == "current" and _value(legacy) == "legacy"
    assert "without merging or overwriting" in capsys.readouterr().err


def test_corrupt_legacy_database_fails_closed(tmp_path):
    root = tmp_path / "site-packages"
    root.mkdir()
    legacy = root / "engraphis.db"
    legacy.write_bytes(b"not a database")
    target = tmp_path / "data" / "engraphis.db"
    with pytest.raises(RuntimeError, match="no new database was opened"):
        _prepare_installed_db_default(root, target)
    assert not target.exists()
    assert list(target.parent.glob("*.migrating-*")) == []


def test_second_database_publish_failure_rolls_back_the_first(tmp_path, monkeypatch):
    root = tmp_path / "site-packages"
    legacy = root / "engraphis.db"
    legacy_users = Path(str(legacy) + ".users.db")
    target = tmp_path / "data" / "engraphis.db"
    target_users = Path(str(target) + ".users.db")
    _sqlite(legacy, "memory-data")
    _sqlite(legacy_users, "auth-data")
    import engraphis.config as config
    real_publish = config._publish_no_replace
    destinations = []

    def fail_second(src, dst):
        destinations.append(Path(dst))
        if len(destinations) == 2:
            raise OSError("simulated memory DB publish failure")
        return real_publish(src, dst)

    monkeypatch.setattr(config, "_publish_no_replace", fail_second)
    with pytest.raises(RuntimeError, match="no new database was opened"):
        _prepare_installed_db_default(root, target)
    assert not target.exists() and not target_users.exists()
    assert destinations == [target_users, target]
    assert _value(legacy) == "memory-data" and _value(legacy_users) == "auth-data"
    assert list(target.parent.glob("*.migrating-*")) == []


def test_hard_crash_after_auth_publish_is_verified_and_resumed(tmp_path, monkeypatch):
    root = tmp_path / "site-packages"
    legacy = root / "engraphis.db"
    legacy_users = Path(str(legacy) + ".users.db")
    target = tmp_path / "data" / "engraphis.db"
    target_users = Path(str(target) + ".users.db")
    _sqlite(legacy, "memory-data")
    _sqlite(legacy_users, "auth-data")
    import engraphis.config as config
    real_publish = config._publish_no_replace
    calls = 0

    def hard_crash_second(src, dst):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise SystemExit("simulated host death")
        return real_publish(src, dst)

    monkeypatch.setattr(config, "_publish_no_replace", hard_crash_second)
    with pytest.raises(SystemExit, match="host death"):
        _prepare_installed_db_default(root, target)
    assert not target.exists()
    assert _value(target_users) == "auth-data"

    monkeypatch.setattr(config, "_publish_no_replace", real_publish)
    assert _prepare_installed_db_default(root, target) == target
    assert _value(target) == "memory-data"
    assert _value(target_users) == "auth-data"
    assert not list(target.parent.glob(".engraphis.db.migrating-*"))


def test_hard_crash_after_staging_is_cleaned_before_retry(tmp_path, monkeypatch):
    root = tmp_path / "site-packages"
    legacy = root / "engraphis.db"
    legacy_users = Path(str(legacy) + ".users.db")
    target = tmp_path / "data" / "engraphis.db"
    _sqlite(legacy, "memory-data")
    _sqlite(legacy_users, "auth-data")
    import engraphis.config as config
    real_backup = config._backup_sqlite
    staged = []

    def hard_crash_after_backup(src, dst):
        real_backup(src, dst)
        staged.append(dst)
        raise SystemExit("simulated host death after staging")

    monkeypatch.setattr(config, "_backup_sqlite", hard_crash_after_backup)
    with pytest.raises(SystemExit, match="host death after staging"):
        _prepare_installed_db_default(root, target)
    assert len(staged) == 1 and staged[0].is_file()

    monkeypatch.setattr(config, "_backup_sqlite", real_backup)
    assert _prepare_installed_db_default(root, target) == target
    assert _value(target) == "memory-data"
    assert _value(Path(str(target) + ".users.db")) == "auth-data"
    assert not list(target.parent.glob(".*.migrating-*"))


def test_migration_stage_is_exclusively_created_owner_only(tmp_path, monkeypatch):
    source = tmp_path / "source.db"
    stage = tmp_path / (".target.db.migrating-" + "a" * 32)
    _sqlite(source, "private-data")
    import engraphis.config as config
    real_open = config.os.open
    created = []

    def capture_open(path, flags, mode=0o777):
        if Path(path) == stage:
            created.append((flags, mode))
        return real_open(path, flags, mode)

    monkeypatch.setattr(config.os, "open", capture_open)
    config._backup_sqlite(source, stage)

    assert created[0][0] & os.O_EXCL
    assert created[0][1] == 0o600
    assert _value(stage) == "private-data"


def test_destination_created_during_publish_is_never_overwritten(tmp_path, monkeypatch):
    root = tmp_path / "site-packages"
    legacy = root / "engraphis.db"
    legacy_users = Path(str(legacy) + ".users.db")
    target = tmp_path / "data" / "engraphis.db"
    target_users = Path(str(target) + ".users.db")
    _sqlite(legacy, "memory-data")
    _sqlite(legacy_users, "auth-data")
    import engraphis.config as config
    real_link = config.os.link

    def create_collision_then_link(src, dst):
        if Path(dst) == target_users and not target_users.exists():
            _sqlite(target_users, "concurrent-owner-data")
        return real_link(src, dst)

    monkeypatch.setattr(config.os, "link", create_collision_then_link)
    with pytest.raises(RuntimeError, match="no new database was opened"):
        _prepare_installed_db_default(root, target)

    assert not target.exists()
    assert _value(target_users) == "concurrent-owner-data"
    assert _value(legacy) == "memory-data"
    assert _value(legacy_users) == "auth-data"


def test_link_swap_before_sqlite_stage_open_never_writes_victim(tmp_path, monkeypatch):
    source = tmp_path / "source.db"
    stage = tmp_path / (".target.db.migrating-" + "b" * 32)
    victim = tmp_path / "victim.db"
    _sqlite(source, "source-data")
    _sqlite(victim, "victim-data")
    import engraphis.config as config
    real_connect = config.sqlite3.connect
    swapped = False

    def swap_then_connect(path, *args, **kwargs):
        nonlocal swapped
        if Path(path) == stage and not swapped:
            swapped = True
            stage.unlink()
            os.link(str(victim), str(stage))
        return real_connect(path, *args, **kwargs)

    monkeypatch.setattr(config.sqlite3, "connect", swap_then_connect)
    with pytest.raises(UnsafeStateFile, match="hard-linked|stage changed while opening"):
        config._backup_sqlite(source, stage)

    assert _value(victim) == "victim-data"


def test_auth_and_primary_publications_are_directory_durable_in_order(
        tmp_path, monkeypatch):
    root = tmp_path / "site-packages"
    legacy = root / "engraphis.db"
    legacy_users = Path(str(legacy) + ".users.db")
    target = tmp_path / "data" / "engraphis.db"
    target_users = Path(str(target) + ".users.db")
    _sqlite(legacy, "memory-data")
    _sqlite(legacy_users, "auth-data")
    import engraphis.config as config
    flushed = []
    original = config._fsync_parent

    def record(path):
        original(path)
        flushed.append(Path(path))

    monkeypatch.setattr(config, "_fsync_parent", record)
    _prepare_installed_db_default(root, target)

    assert flushed[:2] == [target_users, target]
    assert _value(target_users) == "auth-data"
    assert _value(target) == "memory-data"


def test_interrupted_migration_refuses_mismatched_auth_companion(tmp_path):
    root = tmp_path / "site-packages"
    legacy = root / "engraphis.db"
    legacy_users = Path(str(legacy) + ".users.db")
    target = tmp_path / "data" / "engraphis.db"
    target_users = Path(str(target) + ".users.db")
    _sqlite(legacy, "memory-data")
    _sqlite(legacy_users, "legacy-auth")
    _sqlite(target_users, "unrelated-auth")

    with pytest.raises(RuntimeError, match="does not match"):
        _prepare_installed_db_default(root, target)
    assert not target.exists()
    assert _value(target_users) == "unrelated-auth"
    assert _value(legacy_users) == "legacy-auth"


def test_primary_destination_without_expected_auth_companion_fails_closed(tmp_path):
    root = tmp_path / "site-packages"
    legacy = root / "engraphis.db"
    legacy_users = Path(str(legacy) + ".users.db")
    target = tmp_path / "data" / "engraphis.db"
    _sqlite(legacy, "legacy-memory")
    _sqlite(legacy_users, "legacy-auth")
    _sqlite(target, "current-memory")

    with pytest.raises(RuntimeError, match="without its expected auth companion"):
        _prepare_installed_db_default(root, target)
    assert _value(target) == "current-memory"
    assert _value(legacy_users) == "legacy-auth"


def test_migration_rejects_aliased_lock_and_destination_companion(tmp_path):
    root = tmp_path / "site-packages"
    legacy = root / "engraphis.db"
    legacy_users = Path(str(legacy) + ".users.db")
    target = tmp_path / "data" / "engraphis.db"
    target.parent.mkdir(parents=True)
    _sqlite(legacy, "legacy-memory")
    _sqlite(legacy_users, "legacy-auth")

    lock_victim = tmp_path / "lock-victim"
    lock_victim.write_bytes(b"")
    lock_path = target.with_name(".%s.migration.lock" % target.name)
    os.link(str(lock_victim), str(lock_path))
    with pytest.raises(OSError, match="hard-linked"):
        _prepare_installed_db_default(root, target)
    assert lock_victim.read_bytes() == b""

    lock_path.unlink()
    unrelated = tmp_path / "unrelated-users.db"
    _sqlite(unrelated, "unrelated-auth")
    target_users = Path(str(target) + ".users.db")
    os.link(str(unrelated), str(target_users))
    with pytest.raises(RuntimeError, match="unsafe destination"):
        _prepare_installed_db_default(root, target)
    assert _value(unrelated) == "unrelated-auth"
    assert not target.exists()


def test_configured_path_bypasses_legacy_migration(tmp_path, monkeypatch):
    root = tmp_path / "site-packages"
    root.mkdir()
    (root / "engraphis.db").write_bytes(b"not a database")
    monkeypatch.setenv("ENGRAPHIS_DB_PATH", str(tmp_path / "explicit.db"))
    assert _configured_db_path(root) == str(tmp_path / "explicit.db")
