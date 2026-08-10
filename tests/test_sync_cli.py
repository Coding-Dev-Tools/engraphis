"""CLI + factory wiring for cloud sync — proves the relay transport is actually
reachable from a user-facing entry point, not just implemented in a backend.

Regression guard: the managed relay (client `backends/sync_relay.py`, server
`inspector/sync_relay.py`) was fully built and tested end-to-end, yet `get_transport`
refused `"relay"` and `scripts/sync.py` only accepted `--remote <folder>`, so no
shipped entry point could drive it. These tests lock the wiring in place.
"""
from __future__ import annotations

import base64
import json
import socket

import pytest

from engraphis.backends.sync_folder import get_transport
from engraphis.backends.sync_relay import (
    EncryptedRelayTransport,
    RelayError,
    RelayTransport,
    _saved_sync_token,
    decode_sync_e2ee_key,
)
from engraphis.core.engine import MemoryEngine
from engraphis.core.interfaces import SyncTransport
from scripts.sync import main as sync_main


# ── factory: relay is now a first-class transport ───────────────────────────────────

def test_decode_sync_e2ee_key_accepts_padded_and_unpadded_forms():
    pytest.importorskip("cryptography")
    key = b"k" * 32
    unpadded = base64.urlsafe_b64encode(key).decode().rstrip("=")
    padded = unpadded + "="  # conventional 44-char base64 with one '=' pad
    assert decode_sync_e2ee_key(unpadded) == key
    assert decode_sync_e2ee_key(padded) == key


def test_decode_sync_e2ee_key_rejects_short_and_malformed_values():
    pytest.importorskip("cryptography")
    with pytest.raises(RelayError):
        decode_sync_e2ee_key("short")
    with pytest.raises(RelayError):
        decode_sync_e2ee_key("A" * 43 + "==")  # two pads is not a 32-byte key


def test_saved_sync_token_rejects_configured_token_bound_to_another_relay(monkeypatch):
    monkeypatch.setenv("ENGRAPHIS_SYNC_TOKEN", "engr_ut_" + "x" * 32)
    monkeypatch.setenv("ENGRAPHIS_SYNC_TOKEN_ORIGIN", "https://trusted.test")

    with pytest.raises(RelayError, match="belongs to another relay") as caught:
        _saved_sync_token("https://other.test")

    assert caught.value.status == 409


def test_saved_sync_token_rejects_configured_token_without_valid_origin(monkeypatch):
    monkeypatch.setenv("ENGRAPHIS_SYNC_TOKEN", "engr_ut_" + "x" * 32)
    monkeypatch.setenv("ENGRAPHIS_SYNC_TOKEN_ORIGIN", "")

    with pytest.raises(RelayError, match="no valid relay binding") as caught:
        _saved_sync_token("https://other.test")

    assert caught.value.status == 409


def test_get_transport_relay_builds_relay_transport(monkeypatch):
    pytest.importorskip("cryptography")
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))
        ],
    )
    t = get_transport("relay", base_url="https://sync.test/", workspace_id="acme",
                      access_token="engr_ut_" + "x" * 32,
                      e2ee_key=base64.urlsafe_b64encode(b"k" * 32).decode().rstrip("="))
    assert isinstance(t, EncryptedRelayTransport)
    assert isinstance(t, SyncTransport)          # satisfies the runtime-checkable protocol
    assert isinstance(t.relay, RelayTransport)
    assert t.relay.base == "https://sync.test"   # trailing slash stripped
    assert t.workspace_id == "acme"
    assert t.relay.key == "engr_ut_" + "x" * 32


def test_get_transport_relay_refuses_to_fall_back_to_plaintext(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))
        ],
    )
    monkeypatch.delenv("ENGRAPHIS_SYNC_E2EE_KEY", raising=False)
    with pytest.raises(RelayError, match="end-to-end encryption key"):
        get_transport("relay", base_url="https://sync.test/", workspace_id="acme",
                      access_token="engr_ut_" + "x" * 32)


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
    try:
        eng.store.get_or_create_workspace("acme")
        eng.store.conn.commit()
    finally:
        eng.store.close()
    return path


@pytest.fixture
def _capture_transport(monkeypatch):
    """Provide a cloud session and capture how the CLI builds its transport."""
    monkeypatch.setenv("ENGRAPHIS_CLOUD_ACCESS_TOKEN", "cloud-token-" + "x" * 32)
    monkeypatch.setenv("ENGRAPHIS_SYNC_TOKEN", "user-token-" + "x" * 32)
    monkeypatch.setenv("ENGRAPHIS_SYNC_TOKEN_ORIGIN", "https://sync.test")
    monkeypatch.setenv(
        "ENGRAPHIS_SYNC_E2EE_KEY",
        base64.urlsafe_b64encode(b"k" * 32).decode().rstrip("="),
    )
    monkeypatch.setenv("ENGRAPHIS_CLOUD_ORGANIZATION_ID", "org_test")
    from engraphis.config import settings
    monkeypatch.setattr(settings, "allowed_workspaces", [])
    captured = {}

    def fake_get_transport(kind="folder", **kw):
        captured["kind"] = kind
        captured["kw"] = kw
        captured["transport"] = _FakeTransport()
        return captured["transport"]

    monkeypatch.setattr("engraphis.backends.sync_folder.get_transport", fake_get_transport)
    return captured


def test_cli_selects_relay_and_namespaces_by_workspace_name(db_with_workspace, _capture_transport):
    rc = sync_main(["--db", db_with_workspace, "--workspace", "acme",
                    "--relay", "https://sync.test"])
    assert rc == 0
    assert _capture_transport["kind"] == "relay"
    kw = _capture_transport["kw"]
    assert kw["base_url"] == "https://sync.test"
    # Namespace MUST be the workspace name, not a per-device id, or two devices never meet.
    assert kw["workspace_id"] == "acme"
    assert kw["access_token"] == "user-token-" + "x" * 32


def test_cli_secret_values_are_not_accepted_as_argv_flags(capsys):
    with pytest.raises(SystemExit) as caught:
        sync_main(["--help"])
    assert caught.value.code == 0
    help_text = capsys.readouterr().out
    assert "--relay-token" not in help_text
    assert "--relay-e2ee-key" not in help_text


@pytest.mark.parametrize("flag", ["--relay-token", "--relay-e2ee-key"])
def test_cli_rejects_legacy_secret_flags_without_echoing_value(flag, capsys):
    secret = "must-not-reach-terminal"

    rc = sync_main([flag, secret])

    assert rc == 2
    output = capsys.readouterr()
    assert secret not in output.out
    assert secret not in output.err


def test_cli_reports_relay_error_while_opening_transport(
        db_with_workspace, monkeypatch, capsys):
    from engraphis.config import settings
    monkeypatch.setattr(settings, "allowed_workspaces", [])
    monkeypatch.setenv("ENGRAPHIS_SYNC_TOKEN", "test-token-" + "x" * 32)
    monkeypatch.setenv("ENGRAPHIS_SYNC_TOKEN_ORIGIN", "https://sync.test")

    def fail_open(*_args, **_kwargs):
        raise RelayError("credential exchange is temporarily unavailable", status=503)

    monkeypatch.setattr("engraphis.backends.sync_folder.get_transport", fail_open)

    rc = sync_main([
        "--db", db_with_workspace, "--workspace", "acme",
        "--relay", "https://sync.test",
    ])

    assert rc == 2
    assert "temporarily unavailable" in capsys.readouterr().err


def test_cli_reports_relay_error_during_sync(db_with_workspace, monkeypatch, capsys):
    from engraphis.config import settings
    monkeypatch.setattr(settings, "allowed_workspaces", [])
    monkeypatch.setenv("ENGRAPHIS_SYNC_TOKEN", "test-token-" + "x" * 32)
    monkeypatch.setenv("ENGRAPHIS_SYNC_TOKEN_ORIGIN", "https://sync.test")

    class _FailingTransport(_FakeTransport):
        def push(self, name, data):
            raise RelayError("upload was not replayed; retry sync", status=401)

    monkeypatch.setattr(
        "engraphis.backends.sync_folder.get_transport",
        lambda *_args, **_kwargs: _FailingTransport(),
    )

    rc = sync_main([
        "--db", db_with_workspace, "--workspace", "acme",
        "--relay", "https://sync.test",
    ])

    assert rc == 2
    error = capsys.readouterr().err
    assert "relay sync failed" in error
    assert "was not replayed" in error


def test_cli_viewer_token_pulls_without_pushing(db_with_workspace, _capture_transport):
    rc = sync_main([
        "--db", db_with_workspace,
        "--workspace", "acme",
        "--relay", "https://sync.test",
        "--read-only",
    ])
    assert rc == 0
    assert _capture_transport["kind"] == "relay"
    assert _capture_transport["transport"].pushed == []


def test_cli_honors_saved_device_read_only_policy(
        db_with_workspace, _capture_transport, monkeypatch):
    monkeypatch.setattr("engraphis.backends.sync_relay.sync_read_only", lambda: True)

    rc = sync_main([
        "--db", db_with_workspace,
        "--workspace", "acme",
        "--relay", "https://sync.test",
    ])

    assert rc == 0
    assert _capture_transport["transport"].pushed == []


def test_cli_selects_folder(db_with_workspace, _capture_transport, tmp_path):
    share = str(tmp_path / "share")
    rc = sync_main(["--db", db_with_workspace, "--workspace", "acme", "--remote", share])
    assert rc == 0
    assert _capture_transport["kind"] == "folder"
    assert _capture_transport["kw"]["root"] == share
    assert _capture_transport["kw"]["create"] is True


def test_cli_returns_nonzero_and_labels_incomplete_folder_round(
        db_with_workspace, monkeypatch, capsys):
    class IncompleteTransport(_FakeTransport):
        def pull(self):
            raise RuntimeError("folder pull incomplete")
            yield

    monkeypatch.setattr(
        "engraphis.backends.sync_folder.get_transport",
        lambda *_args, **_kwargs: IncompleteTransport(),
    )
    rc = sync_main([
        "--db", db_with_workspace,
        "--workspace", "acme",
        "--remote", "unused-share",
    ])

    captured = capsys.readouterr()
    assert rc == 1
    assert '"complete": false' in captured.out
    assert "incomplete:" in captured.err


def test_cli_folder_dry_run_does_not_create_missing_remote(db_with_workspace, tmp_path):
    share = tmp_path / "missing-share"

    rc = sync_main([
        "--db", db_with_workspace,
        "--workspace", "acme",
        "--remote", str(share),
        "--dry-run",
    ])

    assert rc == 0
    assert not share.exists()
    engine = MemoryEngine.create(db_with_workspace)
    assert engine.store.get_sync_state("device_id") is None
    assert engine.store.conn.execute(
        "SELECT 1 FROM sync_state WHERE key LIKE 'sync_snapshot:%'"
    ).fetchone() is None


def test_cli_bare_relay_falls_back_to_config(db_with_workspace, _capture_transport, monkeypatch):
    from engraphis.config import settings
    monkeypatch.setattr(settings, "relay_url", "https://env-default.test")
    monkeypatch.setenv("ENGRAPHIS_SYNC_TOKEN_ORIGIN", "https://env-default.test")
    rc = sync_main(["--db", db_with_workspace, "--workspace", "acme", "--relay"])
    assert rc == 0
    assert _capture_transport["kw"]["base_url"] == "https://env-default.test"


def test_cli_bare_relay_without_config_is_an_error(db_with_workspace, monkeypatch):
    from engraphis.config import settings
    monkeypatch.setattr(settings, "allowed_workspaces", [])
    monkeypatch.setattr(settings, "relay_url", "")
    rc = sync_main(["--db", db_with_workspace, "--workspace", "acme", "--relay"])
    assert rc == 2


def test_cli_never_acquires_managed_bearer_for_custom_origin(
        db_with_workspace, monkeypatch, capsys):
    from engraphis.config import settings
    monkeypatch.setattr(settings, "allowed_workspaces", [])
    monkeypatch.setenv("ENGRAPHIS_SYNC_TOKEN", "test-token-" + "x" * 32)
    monkeypatch.setenv("ENGRAPHIS_SYNC_TOKEN_ORIGIN", "https://trusted.test")

    def must_not_acquire(*_args, **_kwargs):
        raise AssertionError("managed bearer acquisition must not run for a custom origin")

    monkeypatch.setattr(
        "engraphis.cloud_session.access_for_workspace", must_not_acquire
    )
    rc = sync_main([
        "--db", db_with_workspace,
        "--workspace", "acme",
        "--relay", "https://hostile.test",
    ])

    assert rc == 2
    assert "not bound to this relay origin" in capsys.readouterr().err


def test_cli_never_acquires_managed_bearer_for_environment_relay_override(
        db_with_workspace, monkeypatch, capsys):
    from engraphis.config import settings
    monkeypatch.setattr(settings, "allowed_workspaces", [])
    monkeypatch.setattr(settings, "relay_url", "https://hostile-env.test")
    monkeypatch.delenv("ENGRAPHIS_SYNC_TOKEN", raising=False)
    monkeypatch.delenv("ENGRAPHIS_SYNC_TOKEN_ORIGIN", raising=False)
    monkeypatch.setattr(
        "engraphis.backends.sync_relay.has_sync_token", lambda: False
    )

    def must_not_acquire(*_args, **_kwargs):
        raise AssertionError("managed bearer acquisition must not run for an env override")

    monkeypatch.setattr(
        "engraphis.cloud_session.access_for_workspace", must_not_acquire
    )
    rc = sync_main([
        "--db", db_with_workspace,
        "--workspace", "acme",
        "--relay",
    ])

    assert rc == 2
    assert "custom relay needs" in capsys.readouterr().err


def test_cli_acquires_managed_bearer_for_canonical_origin_only(
        db_with_workspace, _capture_transport, monkeypatch):
    from engraphis.config import DEFAULT_RELAY_URL

    monkeypatch.delenv("ENGRAPHIS_SYNC_TOKEN", raising=False)
    monkeypatch.delenv("ENGRAPHIS_SYNC_TOKEN_ORIGIN", raising=False)
    monkeypatch.setattr(
        "engraphis.backends.sync_relay.has_sync_token", lambda: False
    )
    acquired = []

    def acquire(workspace, *, require_compute):
        acquired.append((workspace, require_compute))
        return "managed-scoped-token", "member", {}

    monkeypatch.setattr(
        "engraphis.cloud_session.access_for_workspace", acquire
    )

    rc = sync_main([
        "--db", db_with_workspace,
        "--workspace", "acme",
        "--relay", DEFAULT_RELAY_URL,
    ])

    assert rc == 0
    assert acquired == [("acme", False)]
    assert _capture_transport["kw"]["access_token"] == "managed-scoped-token"


def test_cli_invalid_relay_does_not_echo_custom_url_secrets(
        db_with_workspace, monkeypatch, capsys):
    from engraphis.config import settings
    monkeypatch.setattr(settings, "allowed_workspaces", [])
    monkeypatch.setenv("ENGRAPHIS_SYNC_TOKEN", "safe-user-token-value")
    monkeypatch.setenv("ENGRAPHIS_SYNC_TOKEN_ORIGIN", "https://relay.test")
    monkeypatch.setenv(
        "ENGRAPHIS_SYNC_E2EE_KEY",
        base64.urlsafe_b64encode(b"k" * 32).decode().rstrip("="),
    )
    endpoint_marker = "private-owner@example.com"
    token_marker = "query-token-secret"
    relay = "https://relay.test/%s?token=%s" % (endpoint_marker, token_marker)

    rc = sync_main([
        "--db", db_with_workspace,
        "--workspace", "acme",
        "--relay", relay,
    ])

    assert rc == 2
    error = capsys.readouterr().err
    assert "could not open relay" in error
    assert endpoint_marker not in error
    assert token_marker not in error


def test_cli_refuses_to_upload_personal_workspace_to_shared_relay(
        db_with_workspace, _capture_transport):
    engine = MemoryEngine.create(db_with_workspace)
    row = engine.store.conn.execute(
        "SELECT id FROM workspaces WHERE name='acme'"
    ).fetchone()
    engine.store.conn.execute(
        "UPDATE workspaces SET settings=? WHERE id=?",
        (json.dumps({"visibility": "personal", "owner": "owner@example.com"}), row["id"]),
    )
    engine.store.conn.commit()
    engine.store.close()

    rc = sync_main([
        "--db", db_with_workspace, "--workspace", "acme",
        "--relay", "https://sync.test",
    ])

    assert rc == 2
    assert _capture_transport == {}


def test_cli_refuses_invalid_workspace_visibility_for_shared_relay(
        db_with_workspace, _capture_transport):
    engine = MemoryEngine.create(db_with_workspace)
    row = engine.store.conn.execute(
        "SELECT id FROM workspaces WHERE name='acme'"
    ).fetchone()
    engine.store.conn.execute(
        "UPDATE workspaces SET settings=? WHERE id=?",
        (json.dumps({"visibility": "corrupt-value"}), row["id"]),
    )
    engine.store.conn.commit()
    engine.store.close()

    rc = sync_main([
        "--db", db_with_workspace, "--workspace", "acme",
        "--relay", "https://sync.test",
    ])

    assert rc == 2
    assert _capture_transport == {}


def test_cli_requires_exactly_one_transport(db_with_workspace, tmp_path):
    # neither --remote nor --relay
    assert sync_main(["--db", db_with_workspace, "--workspace", "acme"]) == 2
    # both at once
    assert sync_main(["--db", db_with_workspace, "--workspace", "acme",
                      "--remote", str(tmp_path / "s"), "--relay", "https://x.test"]) == 2



def test_cli_reports_missing_workspace_without_opening_transport(
        db_with_workspace, _capture_transport, capsys):
    rc = sync_main([
        "--db", db_with_workspace,
        "--workspace", "missing",
        "--remote", "unused-share",
    ])

    assert rc == 2
    assert "no workspace named 'missing'" in capsys.readouterr().err
    assert _capture_transport == {}


def test_cli_reports_missing_repo_without_opening_transport(
        db_with_workspace, _capture_transport, capsys):
    rc = sync_main([
        "--db", db_with_workspace,
        "--workspace", "acme",
        "--repo", "missing",
        "--remote", "unused-share",
    ])

    assert rc == 2
    assert "no repo named 'missing'" in capsys.readouterr().err
    assert _capture_transport == {}
