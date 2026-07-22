"""The legacy REST surface exposes no local commercial authority."""
import tempfile
import threading
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="full-stack extra not installed")
pytest.importorskip("httpx", reason="httpx not installed")

from fastapi.testclient import TestClient  # noqa: E402

from engraphis.config import settings  # noqa: E402

_DB_PATH = str(Path(tempfile.mkdtemp()) / "legacy-boundary.db")


def _client(monkeypatch):
    monkeypatch.setattr(settings, "db_path", _DB_PATH)
    monkeypatch.setattr("engraphis.stores._local", threading.local())
    monkeypatch.setattr(settings, "loop_interval", 0)
    monkeypatch.setattr(settings, "embed_model", "")
    from engraphis.app import create_app
    return TestClient(create_app())


def test_v1_reports_hosted_plan_boundary(monkeypatch):
    with _client(monkeypatch) as client:
        license_state = client.get("/memory/license").json()["data"]
        assert license_state["plan"] == "local"
        assert license_state["cloud_managed"] is True
        assert license_state["trial_seconds"] == 259_200
        assert license_state["grace_seconds"] == 86_400


def test_v1_analytics_is_cloud_only_and_raw_export_remains_available(monkeypatch):
    with _client(monkeypatch) as client:
        analytics = client.get("/memory/analytics")
        assert analytics.status_code == 501
        assert client.get("/memory/export").status_code == 200


def test_v1_local_license_activation_is_retired(monkeypatch):
    with _client(monkeypatch) as client:
        response = client.post(
            "/memory/license/activate", json={"key": "ENGR1.legacy.value"}
        )
        assert response.status_code == 501
        assert response.json()["detail"]["cloud_only"] is True
