"""Suite-wide isolation for local runtime configuration and private client state."""
from __future__ import annotations

import socket

import pytest

from engraphis.config import settings


def pytest_collection_modifyitems(items):
    """Run mutually incompatible SQLite native integrations in safe order.

    Importing SQLCipher before sqlite-vec can bind the latter extension to the
    wrong SQLite ABI and segfault the interpreter.  SQLCipher tests run last;
    ordinary engine/service tests use the safe NumPy default in either phase.
    """
    priorities = {"native_sqlitevec": 0, "native_sqlcipher": 2}
    items.sort(key=lambda item: min(
        (priorities.get(marker.name, 1) for marker in item.iter_markers()),
        default=1,
    ))


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
    """Keep developer deployment bindings, models, and cloud credentials out of tests."""

    state_dir = tmp_path / ".engraphis"
    database = tmp_path / "engraphis.db"
    monkeypatch.setenv("ENGRAPHIS_STATE_DIR", str(state_dir))
    monkeypatch.setenv("ENGRAPHIS_DB_PATH", str(database))
    monkeypatch.delenv("ENGRAPHIS_CLOUD_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("ENGRAPHIS_CLOUD_REFRESH_CREDENTIAL", raising=False)
    monkeypatch.delenv("ENGRAPHIS_SYNC_TOKEN", raising=False)
    monkeypatch.setenv("ENGRAPHIS_API_TOKEN", "")
    monkeypatch.setattr(settings, "api_token", "")
    monkeypatch.setattr(settings, "allowed_workspaces", [])
    monkeypatch.setattr(settings, "service_mode", "customer")
    monkeypatch.setattr(settings, "db_path", str(database))
    # The full developer environment can have sentence-transformers installed. Keep the
    # documented offline suite from downloading the production default model merely
    # because an operational CLI test opens a configured MemoryService.
    monkeypatch.setattr(settings, "embed_model", "")
    monkeypatch.setattr(settings, "rerank_model", "")
    # An owner-private ~/.engraphis/config.env may enable live LLM extraction, LLM
    # retention supervision, or a BYOK decision backend. The unit suite is offline-inert
    # by contract (AGENTS.md sec. 1), and a live provider connect inside remember() blocks
    # the gate on a DNS-stubbed address instead of failing fast. Force the deterministic
    # defaults; tests that exercise these paths opt back in explicitly via monkeypatch.
    monkeypatch.setattr(settings, "extractor", "none")
    monkeypatch.setattr(settings, "llm_auto_extract", False)
    monkeypatch.setattr(settings, "retention_supervisor", "none")
    monkeypatch.setattr(settings, "decision_backend", "none")
    for name in (
        "ENGRAPHIS_EXTRACTOR",
        "ENGRAPHIS_LLM_AUTO_EXTRACT",
        "ENGRAPHIS_RETENTION_SUPERVISOR",
        "ENGRAPHIS_DECISION_BACKEND",
        "ENGRAPHIS_LLM_PROVIDER",
        "ENGRAPHIS_LLM_MODEL",
        "ENGRAPHIS_LLM_API_KEY",
        "TYPESAFE_API_KEY",
        "JEV_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
