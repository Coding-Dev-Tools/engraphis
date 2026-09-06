"""A failed resource must not leave unreported fragments in canonical storage."""
import json
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest

from engraphis.core.interfaces import ExtractedFact
from engraphis.core.store import SavepointError
from engraphis.service import MemoryService


@pytest.fixture(params=["files", "folder"])
def import_resources(request, tmp_path, monkeypatch):
    monkeypatch.setenv("ENGRAPHIS_IMPORT_ROOTS", str(tmp_path))

    def run(service, files, **kwargs):
        if request.param == "files":
            return service.import_files(workspace="atomic", files=files, **kwargs)
        folder = tmp_path / "resources"
        folder.mkdir(exist_ok=True)
        for item in files:
            (folder / item["name"]).write_text(item["content"], encoding="utf-8")
        return service.import_folder(workspace="atomic", path=str(folder), **kwargs)

    return run


FILES = [
    {"name": "1-good.md", "content": "Herons gather beside the river."},
    {"name": "2-bad.md", "content": "Egrets nest in the southern marsh."},
    {"name": "3-good.md", "content": "Cranes migrate across the northern plain."},
]


def assert_consistent(service, expected_files):
    conn = service.store.conn
    rows = conn.execute("SELECT id, metadata FROM memories").fetchall()
    assert {json.loads(row["metadata"]).get("import_file") for row in rows} == set(expected_files)
    ids = {row["id"] for row in rows}
    assert {row[0] for row in conn.execute("SELECT id FROM mem_vectors")} == ids
    assert {row[0] for row in conn.execute("SELECT id FROM mem_fts")} == ids
    assert conn.execute("SELECT COUNT(*) FROM operation_receipts").fetchone()[0] == len(ids)
    assert not conn.execute(
        "SELECT r.memory_id FROM vector_index_repairs r "
        "LEFT JOIN memories m ON m.id=r.memory_id WHERE m.id IS NULL"
    ).fetchall()


@pytest.mark.parametrize("stage", ["fts", "vector", "receipt"])
def test_expected_write_failure_rolls_back_only_failed_resource(
    import_resources, tmp_path, monkeypatch, stage,
):
    with closing(MemoryService.create(str(tmp_path / "import.db"), extractor="none")) as service:
        method_name = {"fts": "_fts_upsert", "vector": "put_vector", "receipt": "record_receipt"}[stage]
        original = getattr(service.store, method_name)
        calls = 0

        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            result = original(*args, **kwargs)
            if calls == 2:
                raise sqlite3.OperationalError("injected failure after partial write")
            return result

        monkeypatch.setattr(service.store, method_name, fail_second)
        report = import_resources(service, FILES)
        assert (report["imported"], report["errors"]) == (2, 1)
        assert report["details"] == [
            {"file": "2-bad.md", "error": "resource could not be imported"},
        ]
        assert_consistent(service, ["1-good.md", "3-good.md"])
        assert not service.store.conn.in_transaction


def test_second_chunk_embedding_failure_discards_whole_file(
    import_resources, tmp_path, monkeypatch,
):
    with closing(MemoryService.create(str(tmp_path / "chunks.db"), extractor="chunk")) as service:
        embed = service.engine.embedder.embed
        calls = 0

        def fail_second(texts):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ValueError("injected second chunk failure")
            return embed(texts)

        monkeypatch.setattr(service.engine.embedder, "embed", fail_second)
        report = import_resources(service, [{"name": "sections.md", "content": (
            "# Herons\nHerons gather beside the river.\n\n"
            "# Egrets\nEgrets nest in the southern marsh.\n\n"
            "# Cranes\nCranes migrate across the northern plain.\n"
        )}])
        assert calls == 2
        assert (report["imported"], report["errors"]) == (0, 1)
        assert_consistent(service, [])


@pytest.mark.parametrize("failure", ["late_write", "audit"])
@pytest.mark.parametrize("caller_owned", [False, True])
def test_fatal_batch_failure_preserves_transaction_owner(
    import_resources, tmp_path, monkeypatch, failure, caller_owned,
):
    with closing(MemoryService.create(str(tmp_path / "fatal.db"), extractor="none")) as service:
        conn = service.store.conn
        if caller_owned:
            wid = service.create_workspace("atomic")["id"]
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE workspaces SET settings=? WHERE id=?",
                         ('{"caller":"preserved"}', wid))
        original = getattr(service.store, "_fts_upsert" if failure == "late_write" else "audit")
        calls = 0

        def fail_late(*args, **kwargs):
            nonlocal calls
            calls += 1
            result = original(*args, **kwargs)
            if (failure == "late_write" and calls == 2) or (
                failure == "audit" and args[1] in {"import_files", "import_folder"}
            ):
                raise RuntimeError("injected fatal batch failure")
            return result

        monkeypatch.setattr(service.store, "_fts_upsert" if failure == "late_write" else "audit", fail_late)
        with pytest.raises(RuntimeError, match="injected fatal batch failure"):
            import_resources(service, FILES)
        assert_consistent(service, [])
        assert conn.transaction_owned_by_current_thread() is caller_owned
        if caller_owned:
            assert conn.execute("SELECT settings FROM workspaces WHERE id=?", (wid,)).fetchone()[0] == (
                '{"caller":"preserved"}'
            )
            conn.rollback()
        else:
            assert conn.execute("SELECT id FROM workspaces WHERE name='atomic'").fetchone() is None
        # A failed batch must release every owned reservation/savepoint.
        service.create_workspace("after-failure")


def test_optional_derivation_failure_keeps_base_without_partial_facts(
    import_resources, tmp_path, monkeypatch,
):
    with closing(MemoryService.create(str(tmp_path / "derive.db"), extractor="none")) as service:
        service.engine.extractor = SimpleNamespace(extract=lambda text: [
            ExtractedFact(content="Derived first fact about herons."),
            ExtractedFact(content="Derived second fact about egrets."),
        ])
        original = service.store._fts_upsert
        calls = 0

        def fail_second_fact(*args, **kwargs):
            nonlocal calls
            calls += 1
            result = original(*args, **kwargs)
            if calls == 3:
                raise ValueError("injected derived fact failure")
            return result

        monkeypatch.setattr(service.store, "_fts_upsert", fail_second_fact)
        report = import_resources(service, FILES[:1], derive_facts=True)
        assert (report["imported"], report["errors"], report["derived_facts"]) == (1, 0, 0)
        assert report["warnings"] == [{"file": "1-good.md", "warnings": ["fact derivation failed"]}]
        assert_consistent(service, ["1-good.md"])


@pytest.mark.parametrize("backend", ["sqlite-adapter", "sqlite-vec"])
def test_index_failure_rolls_back_resource_and_native_rows(
    import_resources, tmp_path, monkeypatch, backend,
):
    if backend == "sqlite-vec":
        pytest.importorskip("sqlite_vec")
    with closing(MemoryService.create(
        str(tmp_path / "native.db"), extractor="none",
        vector_backend="sqlite-vec" if backend == "sqlite-vec" else "numpy",
    )) as service:
        if backend == "sqlite-adapter":
            # A real SQLite participant exercises rollback without the optional
            # extension; it is not native-backend performance evidence.
            conn = service.store.conn
            conn.execute("CREATE TABLE import_test_index (id TEXT PRIMARY KEY)")
            conn.commit()

            def upsert(ids, vectors, meta=None, *, commit=True):
                conn.executemany("INSERT INTO import_test_index VALUES (?)", [(mid,) for mid in ids])
                if commit:
                    conn.commit()

            service.engine.index = SimpleNamespace(
                store=service.store, shares_store_transaction=True,
                upsert=upsert,
            )
        original = service.engine.index.upsert
        calls = 0

        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            original(*args, **kwargs)
            if calls == 2:
                raise sqlite3.OperationalError("injected native index failure")

        monkeypatch.setattr(service.engine.index, "upsert", fail_second)
        report = import_resources(service, FILES)
        assert (report["imported"], report["errors"]) == (2, 1)
        assert_consistent(service, ["1-good.md", "3-good.md"])
        table = "mem_vec_ann" if backend == "sqlite-vec" else "import_test_index"
        indexed = {row[0] for row in service.store.conn.execute(f"SELECT id FROM {table}")}
        assert indexed == {row[0] for row in service.store.conn.execute("SELECT id FROM memories")}


def test_recoverable_file_error_does_not_commit_callers_batch(
    import_resources, tmp_path, monkeypatch,
):
    path = str(tmp_path / "caller.db")
    with closing(MemoryService.create(path, extractor="none")) as service:
        wid = service.create_workspace("atomic")["id"]
        conn = service.store.conn
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE workspaces SET settings=? WHERE id=?", ('{"caller":true}', wid))
        original = service.store._fts_upsert
        calls = 0

        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            result = original(*args, **kwargs)
            if calls == 2:
                raise sqlite3.OperationalError("injected recoverable failure")
            return result

        monkeypatch.setattr(service.store, "_fts_upsert", fail_second)
        report = import_resources(service, FILES)
        assert (report["imported"], report["errors"]) == (2, 1)
        assert_consistent(service, ["1-good.md", "3-good.md"])
        assert conn.transaction_owned_by_current_thread()
        with closing(sqlite3.connect(path)) as observer:
            assert observer.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
            assert observer.execute("SELECT settings FROM workspaces WHERE id=?", (wid,)).fetchone()[0] != (
                '{"caller":true}'
            )
        conn.rollback()
        assert_consistent(service, [])


def test_final_commit_failure_rolls_back_owned_batch(import_resources, tmp_path, monkeypatch):
    with closing(MemoryService.create(str(tmp_path / "commit.db"), extractor="none")) as service:
        conn = service.store.conn
        commit = type(conn).commit

        def fail_commit(current):
            if current is conn and not getattr(conn._pin, "defer_commits", 0):
                raise sqlite3.OperationalError("injected commit failure")
            return commit(current)

        monkeypatch.setattr(type(conn), "commit", fail_commit)
        with pytest.raises(sqlite3.OperationalError, match="injected commit failure"):
            import_resources(service, FILES)
        assert_consistent(service, [])
        assert not conn.in_transaction
        assert conn.execute("SELECT id FROM workspaces WHERE name='atomic'").fetchone() is None


def fail_settlement(monkeypatch, conn, action, *, occurrence):
    execute = type(conn).execute
    seen = 0
    selected = ""
    failed = False

    def run(current, statement, *args, **kwargs):
        nonlocal seen, selected, failed
        if current is conn:
            if statement.startswith("SAVEPOINT engraphis_optional_"):
                seen += 1
                if seen == occurrence:
                    selected = statement.split()[-1]
            if selected and not failed and statement == f"{action} SAVEPOINT {selected}":
                failed = True
                raise sqlite3.OperationalError("injected savepoint settlement failure")
        return execute(current, statement, *args, **kwargs)

    monkeypatch.setattr(type(conn), "execute", run)


@pytest.mark.parametrize("action", ["RELEASE", "ROLLBACK TO"])
@pytest.mark.parametrize("derive", [False, True])
@pytest.mark.parametrize("caller_owned", [False, True])
def test_savepoint_settlement_failure_aborts_batch(
    import_resources, tmp_path, monkeypatch, action, derive, caller_owned,
):
    with closing(MemoryService.create(str(tmp_path / "settlement.db"), extractor="none")) as service:
        conn = service.store.conn
        if caller_owned:
            wid = service.create_workspace("atomic")["id"]
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE workspaces SET settings=? WHERE id=?", ('{"caller":true}', wid))
        if derive:
            service.engine.extractor = SimpleNamespace(extract=lambda text: [
                ExtractedFact(content="Derived first fact about herons."),
                ExtractedFact(content="Derived second fact about egrets."),
            ])
        if action == "ROLLBACK TO":
            original = service.store._fts_upsert
            calls = 0

            def fail_write(*args, **kwargs):
                nonlocal calls
                calls += 1
                result = original(*args, **kwargs)
                if calls == (3 if derive else 2):
                    raise ValueError("injected operation failure")
                return result

            monkeypatch.setattr(service.store, "_fts_upsert", fail_write)
        fail_settlement(monkeypatch, conn, action, occurrence=2)
        with pytest.raises(SavepointError, match="write savepoint"):
            import_resources(service, FILES[:1] if derive else FILES, derive_facts=derive)
        assert_consistent(service, [])
        assert conn.transaction_owned_by_current_thread() is caller_owned
        if caller_owned:
            assert conn.execute("SELECT settings FROM workspaces WHERE id=?", (wid,)).fetchone()[0] == (
                '{"caller":true}'
            )
            conn.rollback()


@pytest.mark.parametrize("action", ["RELEASE", "ROLLBACK TO"])
def test_conflict_repair_cannot_swallow_settlement_failure(tmp_path, monkeypatch, action):
    with closing(MemoryService.create(str(tmp_path / "conflict.db"), extractor="none")) as service:
        engine = service.engine
        wid = service.create_workspace("atomic")["id"]
        original_id = engine.remember(
            "The API uses JWT tokens for authentication.", workspace_id=wid,
        )
        before = service.store.get_memory(original_id)
        if action == "ROLLBACK TO":
            advance = service.store.advance_memory_modified_hlc

            def fail_advance(*args, **kwargs):
                advance(*args, **kwargs)
                raise RuntimeError("injected conflict repair failure")

            monkeypatch.setattr(service.store, "advance_memory_modified_hlc", fail_advance)
        fail_settlement(monkeypatch, service.store.conn, action, occurrence=1)
        with pytest.raises(SavepointError, match="write savepoint"):
            engine.remember("The API does not use JWT tokens for authentication.", workspace_id=wid)
        assert service.store.get_memory(original_id) == before
        assert {row[0] for row in service.store.conn.execute("SELECT id FROM memories")} == {original_id}
        assert not service.store.conn.execute("SELECT * FROM mem_links").fetchall()
        assert not service.store.conn.in_transaction
