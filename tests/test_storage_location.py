"""Storage location feature: data-dir defaults, snapshot route, and guided DB move."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="storage routes need the server extra")

from fastapi import FastAPI  # noqa: E402 - after importorskip guard (house pattern)
from fastapi.testclient import TestClient  # noqa: E402

from engraphis.routes import v2_api  # noqa: E402


def _make_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE memories (id TEXT PRIMARY KEY, content TEXT)")
        conn.execute("INSERT INTO memories VALUES ('m1', 'hello')")
        conn.commit()
    finally:
        conn.close()


class _StubStore:
    def __init__(self) -> None:
        self.closed = False

    def close(self):
        self.closed = True


class _StubService:
    """Route-level double: exposes the close() contract the move relies on."""

    def __init__(self) -> None:
        self.store = _StubStore()

    def close(self) -> None:
        self.store.closed = True


def _client(monkeypatch, tmp_path, db_path):
    monkeypatch.setattr(v2_api.settings, "db_path", str(db_path))
    monkeypatch.setattr(v2_api.settings, "data_dir", str(tmp_path / "dataroot"))
    env_file = tmp_path / "config.env"
    monkeypatch.setattr(v2_api, "trusted_env_path", lambda: env_file)
    app = FastAPI()
    app.include_router(v2_api.router)
    return TestClient(app), env_file


def test_storage_snapshot_reports_location_and_writability(monkeypatch, tmp_path):
    db_path = tmp_path / "source" / "engraphis.db"
    db_path.parent.mkdir()
    _make_db(db_path)
    client, _ = _client(monkeypatch, tmp_path, db_path)

    response = client.get("/api/storage")
    assert response.status_code == 200
    body = response.json()
    assert body["db_path"] == str(db_path)
    assert body["db_exists"] is True
    assert body["db_bytes"] > 0
    assert body["writable"] is True
    assert body["data_dir"] == str(tmp_path / "dataroot")


def test_move_relocates_database_and_persists_choice(monkeypatch, tmp_path):
    source_dir = tmp_path / "on-c"
    dest_dir = tmp_path / "on-d"
    source_dir.mkdir()
    db_path = source_dir / "engraphis.db"
    _make_db(db_path)

    stub = _StubService()
    client, env_file = _client(monkeypatch, tmp_path, db_path)
    v2_api.set_service(stub)
    rebuilt = {}
    real_build = v2_api._build_service

    def fake_build(db_path_arg):
        rebuilt["path"] = db_path_arg
        return real_build(":memory:") if False else _StubService()

    monkeypatch.setattr(v2_api, "_build_service", fake_build)
    try:
        response = client.post(
            "/api/storage/db-path", json={"destination_dir": str(dest_dir)}
        )
    finally:
        v2_api._service = None

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["moved_to"] == str(dest_dir / "engraphis.db")
    assert body["persisted"] is True

    # Database physically relocated with its content intact.
    assert not db_path.exists()
    moved = dest_dir / "engraphis.db"
    check = sqlite3.connect(str(moved))
    try:
        rows = check.execute("SELECT count(*) FROM memories").fetchone()
    finally:
        check.close()
    assert rows[0] == 1

    # Choice persisted to the TRUSTED env file as an absolute assignment.
    text = env_file.read_text(encoding="utf-8")
    assert "ENGRAPHIS_DB_PATH=%s" % moved in text

    # Live binding was switched to the new location without a restart.
    assert rebuilt["path"] == str(moved)
    assert stub.store.closed is True


def test_move_refuses_system_drive_destination(monkeypatch, tmp_path):
    db_path = tmp_path / "engraphis.db"
    _make_db(db_path)
    client, _ = _client(monkeypatch, tmp_path, db_path)

    for payload in ("C:/Users/nobody/x", "C:\\Users\\nobody", "c:/temp"):
        response = client.post(
            "/api/storage/db-path", json={"destination_dir": payload}
        )
        assert response.status_code == 400, payload
        assert "system drive" in response.json()["detail"]


def test_move_windows_absolute_paths_resolve_without_home_anchoring(tmp_path):
    """A Windows-style non-system destination (D:) stays drive-absolute on any host."""
    from engraphis.routes.v2_api import _resolve_move_destination

    resolved = _resolve_move_destination("D:/EngraphisData")
    assert str(resolved).lower().startswith("d:")
