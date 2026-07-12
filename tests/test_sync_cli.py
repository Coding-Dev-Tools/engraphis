"""CLI + factory wiring for cloud sync — proves the relay transport is actually
reachable from a user-facing entry point, not just implemented in a backend.

Regression guard: the managed relay (client `backends/sync_relay.py`, server
`inspector/sync_relay.py`) was fully built and tested end-to-end, yet `get_transport`
refused `"relay"` and `scripts/sync.py` only accepted `--remote <folder>`, so no
shipped entry point could drive it. These tests lock the wiring in place.
"""
from __future__ import annotations

import pytest

from engraphis.backends.sync_folder import get_transport
from engraphis.backends.sync_relay import RelayTransport
from engraphis.core.engine import MemoryEngine
from engraphis.core.interfaces import SyncTransport
from scripts.sync import main as sync_main


# ── factory: relay is now a first-class transport ───────────────────────────────────

def test_get_transport_relay_builds_relay_transport():
    t = get_transport("relay", base_url="https://sync.test/", workspace_id="acme",
                      license_key="ENGR1.x.y")
    assert isinstance(t, RelayTransport)
    assert isinstance(t, SyncTransport)          # satisfies the runtime-checkable protocol
    assert t.base == "https://sync.test"         # trailing slash stripped
    assert t.workspace_id == "acme"
    assert t.key == "ENGR1.x.y"


def test_get_transport_relay_requires_base_url_and_workspace():
    with pytest.raises(ValueError, match="base_url"):
        get_transport("relay", workspace_id="acme")
    with pytest.raises(ValueError, match="workspace_id"):
        get_transport("relay", base_url="https://sync.test")


def test_get_transport_unknown_kind_lists_both():
    with pytest.raises(ValueError, match="folder, relay"):
        get_transport("smoke-signals")


# ── CLI transport selection ─────────────────────────────────────────────────────────

class _FakeTransport:
    """Records pushes; pulls nothing (a one-device sync round-trip)."""

    def __init__(self):
        self.pushed = []

    def push(self, name, data):
        self.pushed.append((name, data))

    def pull(self):
        return []

    def list_names(self):
        return [n for n, _ in self.pushed]


@pytest.fixture
def db_with_workspace(tmp_path):
    """A persisted v2 DB file containing a workspace named 'acme'."""
    path = str(tmp_path / "sync.db")
    eng = MemoryEngine.create(path)
    eng.store.get_or_create_workspace("acme")
    eng.store.conn.commit()
    return path


@pytest.fixture
def _capture_transport(monkeypatch):
    """Bypass the Pro license gate and capture how the CLI builds its transport."""
    monkeypatch.setattr("engraphis.licensing.require_feature", lambda *a, **k: None)
    captured = {}

    def fake_get_transport(kind="folder", **kw):
        captured["kind"] = kind
        captured["kw"] = kw
        return _FakeTransport()

    monkeypatch.setattr("engraphis.backends.sync_folder.get_transport", fake_get_transport)
    return captured


def test_cli_selects_relay_and_namespaces_by_workspace_name(db_with_workspace, _capture_transport):
    rc = sync_main(["--db", db_with_workspace, "--workspace", "acme",
                    "--relay", "https://sync.test", "--relay-key", "ENGR1.a.b"])
    assert rc == 0
    assert _capture_transport["kind"] == "relay"
    kw = _capture_transport["kw"]
    assert kw["base_url"] == "https://sync.test"
    # Namespace MUST be the workspace name, not a per-device id, or two devices never meet.
    assert kw["workspace_id"] == "acme"
    assert kw["license_key"] == "ENGR1.a.b"


def test_cli_selects_folder(db_with_workspace, _capture_transport, tmp_path):
    share = str(tmp_path / "share")
    rc = sync_main(["--db", db_with_workspace, "--workspace", "acme", "--remote", share])
    assert rc == 0
    assert _capture_transport["kind"] == "folder"
    assert _capture_transport["kw"]["root"] == share


def test_cli_bare_relay_falls_back_to_config(db_with_workspace, _capture_transport, monkeypatch):
    from engraphis.config import settings
    monkeypatch.setattr(settings, "relay_url", "https://env-default.test")
    rc = sync_main(["--db", db_with_workspace, "--workspace", "acme", "--relay"])
    assert rc == 0
    assert _capture_transport["kw"]["base_url"] == "https://env-default.test"


def test_cli_bare_relay_without_config_is_an_error(db_with_workspace, monkeypatch):
    monkeypatch.setattr("engraphis.licensing.require_feature", lambda *a, **k: None)
    from engraphis.config import settings
    monkeypatch.setattr(settings, "relay_url", "")
    rc = sync_main(["--db", db_with_workspace, "--workspace", "acme", "--relay"])
    assert rc == 2


def test_cli_requires_exactly_one_transport(db_with_workspace, tmp_path):
    # neither --remote nor --relay
    assert sync_main(["--db", db_with_workspace, "--workspace", "acme"]) == 2
    # both at once
    assert sync_main(["--db", db_with_workspace, "--workspace", "acme",
                      "--remote", str(tmp_path / "s"), "--relay", "https://x.test"]) == 2
