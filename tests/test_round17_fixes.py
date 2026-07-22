"""Regressions for sync/store integrity fixes:
- get_or_create_workspace bypassed the workspace allow-list on the retrieve path.
- sync clamped future world-time validity (valid_to/valid_from) to now+skew.
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
