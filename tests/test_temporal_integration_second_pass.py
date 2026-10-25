from dataclasses import replace
from unittest.mock import patch
import sqlite3

import pytest

from engraphis.core.interfaces import MemoryRecord, Scope, SearchFilter, MAX_CLOSURE_OBSERVATIONS, format_modified_hlc
from engraphis.core.store import Store, memory_matches_filter
from engraphis.core.sync import SyncEngine, dict_to_record, record_to_dict


def bundle(rows):
    return {"format":"engraphis-sync","version":1,"workspace_name":"review","device_id":"review", "repos":{},"memories":rows,"mem_links":[],"tombstones":[]}


def row(mid="mem_bad", **extra):
    return {"id":mid,"content":"Example memory", "scope":"workspace", "valid_from":100.0,"ingested_at":100.0, **extra}


@pytest.mark.parametrize("endpoint", [50.0, 100.0])
def test_parser_rejects_inverted_or_empty_nonquarantine_history(endpoint):
    assert dict_to_record(row(closure_observations=[[endpoint,200.0]])) is None


@pytest.mark.parametrize("dry_run", [False, True])
def test_bad_history_rejected_without_aborting_good_same_batch(dry_run):
    store=Store(":memory:")
    try:
        good=row("mem_good")
        bad=row("mem_bad",closure_observations=[[50.0,200.0]])
        report=SyncEngine(store).apply_bundle(bundle([good,bad]),dry_run=dry_run)
        assert report["rejected"]==1
        assert report["added"]==1
        assert (store.get_memory("mem_good") is not None) is (not dry_run)
        assert store.get_memory("mem_bad") is None
    finally:
        store.close()


@pytest.mark.parametrize("dry_run", [False, True])
def test_merge_rejects_interval_inversion_before_writing(dry_run):
    store=Store(":memory:")
    try:
        workspace=store.get_or_create_workspace("review")
        original=MemoryRecord(
            "mem_merge","Original claim",scope=Scope.WORKSPACE,workspace_id=workspace,
            valid_from=100,valid_to=200,valid_to_recorded_at=250,ingested_at=100,last_access=100,
            provenance={"source":"sync","trusted":False,"review_state":"pending"},
            modified_hlc=format_modified_hlc(100000,0,"dev_"+"0"*26),
        )
        store.add_memory(original)
        # Each peer interval is individually valid, but the whole-record winner
        # starts after the other peer's earliest closure. The join must be rejected.
        incoming=replace(original,content="Revised claim",valid_from=300,valid_to=400,
            valid_to_recorded_at=450,ingested_at=300,last_access=300,
            closure_observations=(),modified_hlc=format_modified_hlc(300000,0,"dev_"+"1"*26))
        before=store.get_memory(original.id)
        audit_count=store.conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
        report=SyncEngine(store).apply_bundle(bundle([record_to_dict(incoming)]),dry_run=dry_run)
        assert report["rejected"]==1
        assert report["added"]==report["updated"]==0
        assert store.get_memory(original.id)==before
        assert store.conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0]==audit_count
    finally:
        store.close()


def test_hydration_uses_same_snapshot_for_scalar_and_observations(tmp_path,monkeypatch):
    path=str(tmp_path/"consistent.db")
    reader=Store(path)
    writer=Store(path)
    try:
        workspace=reader.get_or_create_workspace("review")
        mid=reader.add_memory(MemoryRecord("mem_snapshot","Example memory",workspace_id=workspace,
            scope=Scope.WORKSPACE,valid_from=100,ingested_at=100,valid_to=300,valid_to_recorded_at=400))
        execute=type(reader.conn).execute
        interleaved=False
        def intercept(connection,sql,*args,**kwargs):
            nonlocal interleaved
            result=execute(connection,sql,*args,**kwargs)
            if connection is reader.conn and not interleaved and sql.startswith("SELECT * FROM memories WHERE id="):
                interleaved=True
                with patch("engraphis.core.store.now_ts",return_value=500):
                    writer.close_validity(mid,at=200)
            return result
        monkeypatch.setattr(type(reader.conn),"execute",intercept)
        snapshot=reader.get_memory(mid)
        assert interleaved
        assert snapshot.valid_to==300
        assert snapshot.closure_observations==((300.0,400.0),)
        assert reader.get_memory(mid).closure_observations==((200.0,500.0),(300.0,400.0))
    finally:
        writer.close()
        reader.close()


def test_store_rejects_union_overflow_without_audit_or_scalar_changes():
    store=Store(":memory:")
    try:
        workspace=store.get_or_create_workspace("review")
        original=MemoryRecord("mem_full","Example memory",workspace_id=workspace,scope=Scope.WORKSPACE,
            valid_from=10,ingested_at=10,closure_observations=tuple((1000.0+i,500.0) for i in range(MAX_CLOSURE_OBSERVATIONS)))
        store.add_memory(original)
        before=store.get_memory(original.id)
        audit_count=store.conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
        with patch("engraphis.core.store.now_ts",return_value=600):
            with pytest.raises(sqlite3.IntegrityError,match="limit"):
                store.close_validity(original.id,at=200)
        assert store.get_memory(original.id)==before
        assert store.conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0]==audit_count
    finally:
        store.close()


def test_unprojected_scalar_and_sql_record_filter_agree():
    store=Store(":memory:")
    try:
        workspace=store.get_or_create_workspace("review")
        mid=store.add_memory(MemoryRecord("mem_scalar","Example memory",workspace_id=workspace,
            scope=Scope.WORKSPACE,valid_from=100,ingested_at=100,valid_to=300,valid_to_recorded_at=400))
        store.conn.execute("INSERT INTO memory_closures VALUES (?,?,?)",(mid,200,500))
        store.conn.commit()
        record=store.get_memory(mid)
        for world,known in ((150,350),(250,450),(350,450),(250,550)):
            flt=SearchFilter(workspace_id=workspace,valid_at=world,known_at=known)
            assert bool(store.visible_memory_ids([mid],flt)) is memory_matches_filter(record,flt)
    finally:
        store.close()


def test_failed_v19_seed_migration_rolls_back_and_retries(tmp_path,monkeypatch):
    path=tmp_path/"migration.db"
    seed=Store(str(path))
    workspace=seed.get_or_create_workspace("review")
    mid=seed.add_memory(MemoryRecord("mem_migrate","Example memory",workspace_id=workspace,
        scope=Scope.WORKSPACE,valid_from=100,ingested_at=100,valid_to=300,valid_to_recorded_at=400))
    seed.close()
    conn=sqlite3.connect(path)
    for trigger in ("trg_memory_closure_insert","trg_memory_closure_update"):
        conn.execute("DROP TRIGGER "+trigger)
    conn.execute("DROP TABLE memory_closures")
    conn.execute("DELETE FROM schema_migrations WHERE version>=19")
    conn.commit()
    conn.close()
    from engraphis.core.schema import MEMORY_CLOSURE_SCHEMA_SQL
    execute=Store._execute_script_transactional
    def fail_after_seed(self,script):
        execute(self,script)
        if script==MEMORY_CLOSURE_SCHEMA_SQL:
            raise RuntimeError("synthetic migration interruption")
    with monkeypatch.context() as patcher:
        patcher.setattr(Store,"_execute_script_transactional",fail_after_seed)
        with pytest.raises(RuntimeError,match="migration interruption"):
            Store(str(path))
    conn=sqlite3.connect(path)
    assert conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]==18
    assert not conn.execute("SELECT name FROM sqlite_master WHERE name='memory_closures'").fetchone()
    assert conn.execute("SELECT valid_to,valid_to_recorded_at FROM memories WHERE id=?",(mid,)).fetchone()==(300,400)
    conn.close()
    reopened=Store(str(path))
    try:
        assert reopened.schema_version==19
        assert reopened.get_memory(mid).closure_observations==((300.0,400.0),)
    finally:
        reopened.close()


def test_stale_upsert_cannot_reopen_or_drop_existing_observations():
    store=Store(":memory:")
    try:
        workspace=store.get_or_create_workspace("review")
        original=MemoryRecord("mem_stale","Example memory",workspace_id=workspace,
            scope=Scope.WORKSPACE,valid_from=100,ingested_at=100)
        store.add_memory(original)
        stale=replace(original)
        with patch("engraphis.core.store.now_ts",return_value=400):
            store.close_validity(original.id,at=300)
        with patch("engraphis.core.store.now_ts",return_value=500):
            store.close_validity(original.id,at=200)
        store.add_memory(stale)
        after=store.get_memory(original.id)
        assert after.closure_observations==((200.0,500.0),(300.0,400.0))
        assert (after.valid_to,after.valid_to_recorded_at)==(200.0,500.0)
        assert after.effective_valid_to(450)==300
    finally:
        store.close()
