"""Suite-wide isolation for local runtime configuration and private client state."""
from __future__ import annotations

import socket

import pytest

from engraphis.config import settings


@pytest.fixture(autouse=True)
def _offline_dns_isolation(monkeypatch):
    """Keep the documented offline gate genuinely offline.

    AGENTS.md requires ``python -m pytest tests/ -q`` to pass with no network. Relay and
    cloud URL validation resolve their destination to reject private/reserved targets, so
    without this stub the suite silently depends on working DNS and fails on an air-gapped
    machine. Tests that need a specific resolution result still override it themselves.
    """

    def _resolve(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port or 0))]

    monkeypatch.setattr(socket, "getaddrinfo", _resolve)


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
