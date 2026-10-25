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


def test_bound_store_enforces_allowlist_for_scoped_reads_and_writes(tmp_path):
    from engraphis.core.interfaces import MemoryRecord
    from engraphis.core.store import Store

    path = tmp_path / "bound.db"
    seed = Store(str(path))
    allowed_id = seed.get_or_create_workspace("allowed")
    secret_id = seed.get_or_create_workspace("secret")
    secret_memory = seed.add_memory(MemoryRecord(
        id="", content="private", workspace_id=secret_id,
    ))
    seed.close()

    bound = Store(str(path), allowed_workspaces={"allowed"})
    with pytest.raises(ValueError):
        bound.create_repo(secret_id, "repo")
    with pytest.raises(ValueError):
        bound.start_session(secret_id)
    with pytest.raises(ValueError):
        bound.add_memory(MemoryRecord(id="", content="blocked", workspace_id=secret_id))
    assert bound.get_memory(secret_memory) is None
    assert bound.list_memories() == []
    assert bound.get_or_create_workspace("allowed") == allowed_id
    bound.close()


def test_sync_apply_preserves_future_world_validity():
    from engraphis.core.sync import dict_to_record
    future = time.time() + 5 * 365 * 86400             # ~5 years out (a fact valid until then)
    rec = dict_to_record({"id": "mem_x", "content": "c", "valid_to": future,
                          "valid_from": future - 86400})
    assert rec is not None
    assert rec.valid_to > time.time() + 365 * 86400    # NOT truncated to now+skew
    # Supplied system timestamps beyond the skew window are rejected row-locally;
    # clamping them relative to each receiver would make replicas diverge.
    poisoned = dict_to_record({"id": "mem_y", "content": "c", "ingested_at": future,
                               "last_access": future})
    assert poisoned is None


def test_bound_store_protects_foreign_session_and_governance_ids(tmp_path):
    from engraphis.core.interfaces import MemoryRecord
    from engraphis.core.store import Store

    path = tmp_path / "governance.db"
    with Store(str(path)) as seed:
        seed.get_or_create_workspace("allowed")
        secret = seed.get_or_create_workspace("secret")
        memory = seed.add_memory(MemoryRecord(id="", content="private", workspace_id=secret))
        session = seed.start_session(secret, goal="private session")
        before_memory = dict(seed.conn.execute(
            "SELECT * FROM memories WHERE id=?", (memory,),
        ).fetchone())
        before_session = seed.get_session(session)
        before_audit = seed.conn.execute("SELECT count(*) FROM audit").fetchone()[0]

    with Store(str(path), allowed_workspaces={"allowed"}) as bound:
        assert bound.get_session(session) is None
        for mutation in (
            lambda: bound.end_session(session),
            lambda: bound.close_validity(memory),
            lambda: bound.set_pinned(memory, True),
            lambda: bound.reinforce(memory),
        ):
            with pytest.raises(ValueError, match="not permitted"):
                mutation()
        assert not bound.conn.in_transaction

    with Store(str(path)) as verify:
        assert dict(verify.conn.execute(
            "SELECT * FROM memories WHERE id=?", (memory,),
        ).fetchone()) == before_memory
        assert verify.get_session(session) == before_session
        assert verify.conn.execute("SELECT count(*) FROM audit").fetchone()[0] == before_audit
