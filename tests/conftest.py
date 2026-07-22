"""Suite-wide isolation for local runtime configuration and private client state."""
from __future__ import annotations

import pytest

from engraphis.config import settings


@pytest.fixture(autouse=True)
def _deployment_settings_isolation(monkeypatch, tmp_path):
    """Keep developer deployment bindings and cloud credentials out of tests."""

    state_dir = tmp_path / ".engraphis"
    database = tmp_path / "engraphis.db"
    monkeypatch.setenv("ENGRAPHIS_STATE_DIR", str(state_dir))
    monkeypatch.setenv("ENGRAPHIS_DB_PATH", str(database))
    monkeypatch.delenv("ENGRAPHIS_CLOUD_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("ENGRAPHIS_CLOUD_REFRESH_CREDENTIAL", raising=False)
    monkeypatch.delenv("ENGRAPHIS_SYNC_TOKEN", raising=False)
    monkeypatch.setattr(settings, "allowed_workspaces", [])
    monkeypatch.setattr(settings, "service_mode", "customer")
    monkeypatch.setattr(settings, "db_path", str(database))
