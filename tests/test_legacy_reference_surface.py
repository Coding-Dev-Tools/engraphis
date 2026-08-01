"""The v1 ASGI import target must never reopen the active v2 database."""
from __future__ import annotations

import threading

import pytest

pytest.importorskip("fastapi", reason="full-stack extra not installed")
httpx = pytest.importorskip("httpx", reason="httpx not installed")

from engraphis.config import settings  # noqa: E402


def _get(app, path="/"):
    import anyio

    async def request():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(path)

    return anyio.run(request)


def test_direct_app_target_is_a_retirement_gate_not_the_v1_engine(monkeypatch, tmp_path):
    current_v2_db = tmp_path / "engraphis-v2.db"
    monkeypatch.setattr(settings, "db_path", str(current_v2_db))

    from engraphis.app import app

    response = _get(app, "/memory/export")

    assert response.status_code == 410
    assert response.json()["error"] == "legacy v1 reference application is retired"
    assert "engraphis-dashboard" in response.json()["detail"]
    assert not current_v2_db.exists()


def test_explicit_reference_rejects_the_active_v2_database(monkeypatch, tmp_path):
    current_v2_db = tmp_path / "engraphis-v2.db"
    monkeypatch.setattr(settings, "db_path", str(current_v2_db))
    monkeypatch.setattr("engraphis.stores._local", threading.local())

    from engraphis.app import (
        LegacyReferenceConfigurationError,
        create_legacy_reference_app,
    )

    with pytest.raises(LegacyReferenceConfigurationError, match="must differ"):
        create_legacy_reference_app(legacy_db_path=current_v2_db)

    assert settings.db_path == str(current_v2_db)
    assert not current_v2_db.exists()


def test_explicit_reference_uses_its_separate_database(monkeypatch, tmp_path):
    current_v2_db = tmp_path / "engraphis-v2.db"
    reference_db = tmp_path / "engraphis-v1-reference.db"
    monkeypatch.setattr(settings, "db_path", str(current_v2_db))
    monkeypatch.setattr(settings, "embed_model", "")
    monkeypatch.setattr(settings, "loop_interval", 0)
    monkeypatch.setattr("engraphis.stores._local", threading.local())

    from engraphis.app import create_legacy_reference_app
    from fastapi.testclient import TestClient

    with TestClient(create_legacy_reference_app(legacy_db_path=reference_db)) as client:
        assert client.get("/api/health").status_code == 200

    assert settings.db_path == str(reference_db.resolve())
    assert reference_db.exists()
    assert not current_v2_db.exists()
