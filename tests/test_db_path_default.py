"""Default DB location and the preservation-first upgrade path for installed builds.

A DB defaulting into site-packages is silently deleted by ``pip install -U``
(first-run data loss) — the 0.9.8 fix routes installed builds to a platform data dir
and migrates any database an earlier install left behind.
Pure-function test of both branches; no monkeypatching of module state.
"""
from pathlib import Path, PureWindowsPath

from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from engraphis.config import (
    Settings,
    _configured_db_path,
    _default_db_path,
    _prepare_installed_db_default,
)


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
    real_replace = __import__("engraphis.config", fromlist=["os"]).os.replace
    destinations = []

    def fail_second(src, dst):
        destinations.append(Path(dst))
        if len(destinations) == 2:
            raise OSError("simulated memory DB publish failure")
        return real_replace(src, dst)

    monkeypatch.setattr("engraphis.config.os.replace", fail_second)
    with pytest.raises(RuntimeError, match="no new database was opened"):
        _prepare_installed_db_default(root, target)
    assert not target.exists() and not target_users.exists()
    assert destinations == [target_users, target]
    assert _value(legacy) == "memory-data" and _value(legacy_users) == "auth-data"
    assert list(target.parent.glob("*.migrating-*")) == []


def test_configured_path_bypasses_legacy_migration(tmp_path, monkeypatch):
    root = tmp_path / "site-packages"
    root.mkdir()
    (root / "engraphis.db").write_bytes(b"not a database")
    monkeypatch.setenv("ENGRAPHIS_DB_PATH", str(tmp_path / "explicit.db"))
    assert _configured_db_path(root) == str(tmp_path / "explicit.db")
