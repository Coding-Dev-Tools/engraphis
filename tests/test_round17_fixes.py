"""Regressions for sync/store integrity fixes:
- get_or_create_workspace bypassed the workspace allow-list on the retrieve path.
- sync clamped future world-time validity (valid_to/valid_from) to now+skew.
- autosync _record reset the policy to the disabled default on a transient read failure.
"""
import time

import pytest


def test_get_or_create_workspace_enforces_allowlist(tmp_path):
    from engraphis.core import ids
    from engraphis.core.store import Store
    s = Store(str(tmp_path / "w.db"), allowed_workspaces={"allowed"})
    # A workspace outside the allow-list already exists in the DB (predates the allow-list,
    # or arrived via sync). The RETRIEVE path must refuse it, not hand it back.
    s.conn.execute("INSERT INTO workspaces(id, name, created_at, settings) VALUES (?,?,?,?)",
                   (ids.new_id("workspace"), "secret", 0.0, "{}"))
    s.conn.commit()
    with pytest.raises(ValueError):
        s.get_or_create_workspace("secret")
    assert s.get_or_create_workspace("allowed")        # allowed workspace still works
    s.close()


def test_sync_apply_preserves_future_world_validity():
    from engraphis.core.sync import dict_to_record
    future = time.time() + 5 * 365 * 86400             # ~5 years out (a fact valid until then)
    rec = dict_to_record({"id": "mem_x", "content": "c", "valid_to": future,
                          "valid_from": future - 86400})
    assert rec is not None
    assert rec.valid_to > time.time() + 365 * 86400    # NOT truncated to now+skew
    # System timestamps are still clamped near now (they feed the version key / anti-poison).
    poisoned = dict_to_record({"id": "mem_y", "content": "c", "ingested_at": future,
                               "last_access": future})
    assert poisoned.ingested_at <= time.time() + 10 * 86400


def test_autosync_record_preserves_policy_on_unreadable_file(monkeypatch, tmp_path):
    from engraphis import autosync
    target = tmp_path / "autosync.json"
    target.mkdir()                                     # exists but read_text raises OSError
    monkeypatch.setattr(autosync, "policy_path", lambda: target)
    wrote = []
    monkeypatch.setattr(autosync, "_write", lambda doc: wrote.append(doc))
    autosync._record({"workspaces": 1})
    assert wrote == []                                 # never clobber the policy on a read hiccup


def test_autosync_record_creates_on_fresh_install(monkeypatch, tmp_path):
    from engraphis import autosync
    monkeypatch.setattr(autosync, "policy_path", lambda: tmp_path / "fresh.json")
    autosync._record({"workspaces": 1}, now=123.0)
    assert autosync.load_policy()["last_run"] == 123.0
