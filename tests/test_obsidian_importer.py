from __future__ import annotations

import json
from pathlib import Path

import pytest

from engraphis.core.interfaces import Scope, SearchFilter
from engraphis.core.obsidian import ObsidianNote, ObsidianVaultScan, scan_obsidian_vault
from engraphis.obsidian_import import ObsidianImportCancelled, ObsidianImporter
from engraphis.service import MemoryService
from scripts import importer as importer_cli


def _vault(root: Path) -> Path:
    vault = root / "Knowledge"
    (vault / "projects").mkdir(parents=True)
    (vault / "Home.md").write_text(
        "---\ntitle: Home base\naliases: [Start]\ntags: [index, private]\n"
        "created: 2026-01-02\n---\n# Home base\nSee [[projects/Plan|the plan]].\n",
        encoding="utf-8",
    )
    (vault / "projects" / "Plan.md").write_text(
        "# Plan\n\nShip locally. Link back to [[Home]].\n",
        encoding="utf-8",
    )
    (vault / ".obsidian").mkdir()
    (vault / ".obsidian" / "app.json").write_text("{}", encoding="utf-8")
    return vault


def _service(path: Path) -> MemoryService:
    return MemoryService.create(
        str(path), embed_dim=64, extractor="none", graph_extractor="none",
        retention_supervisor="none",
    )


def _live(service: MemoryService, workspace: str = "acme"):
    wid = service._lookup_workspace(workspace)
    return service.store.list_memories(SearchFilter(workspace_id=wid))


def test_import_reimport_revision_rename_and_missing(tmp_path: Path):
    vault = _vault(tmp_path)
    service = _service(tmp_path / "memory.db")
    try:
        preview = service.preview_obsidian_vault(str(vault), workspace="acme")
        assert preview["counts"]["markdown"] == 2
        assert preview["counts"]["imported"] == 2
        assert preview["summary"]["wikilinks"] == 2

        first = service.import_obsidian_vault(
            str(vault), workspace="acme", confirmed=True,
        )
        assert first["state"] == "completed"
        assert first["counts"]["imported"] == 2
        memories = _live(service)
        assert len(memories) == 2
        home = next(memory for memory in memories if memory.title == "Home base")
        assert home.content.startswith("# Home base")
        assert home.metadata["obsidian"]["relative_path"] == "Home.md"
        assert home.metadata["obsidian"]["aliases"] == ["Start"]
        assert service.store.conn.execute(
            "SELECT COUNT(*) FROM mem_links WHERE relation='references' AND valid_to IS NULL"
        ).fetchone()[0] == 1

        again = service.import_obsidian_vault(
            str(vault), workspace="acme", confirmed=True,
        )
        assert again["counts"]["skipped"] == 3  # two unchanged notes + .obsidian exclusion
        assert len(_live(service)) == 2
        assert service.store.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 2

        home_path = vault / "Home.md"
        home_path.write_text(
            "---\ntitle: Home base\naliases: [Start]\ntags: [index]\n---\n"
            "# Home base\nThe durable revision. See [[projects/Plan]].\n",
            encoding="utf-8",
        )
        revised = service.import_obsidian_vault(
            str(vault), workspace="acme", confirmed=True,
        )
        assert revised["counts"]["updated"] == 1
        history = service.store.conn.execute(
            "SELECT valid_to, metadata FROM memories WHERE subject_key=? ORDER BY valid_from",
            (home.subject_key,),
        ).fetchall()
        assert len(history) == 2
        assert history[0]["valid_to"] is not None
        assert history[1]["valid_to"] is None
        link_history = service.store.conn.execute(
            "SELECT valid_to FROM mem_links WHERE relation='references' ORDER BY valid_from"
        ).fetchall()
        assert len(link_history) == 2
        assert link_history[0]["valid_to"] is not None
        assert link_history[1]["valid_to"] is None

        moved = vault / "Welcome.md"
        home_path.rename(moved)
        renamed = service.import_obsidian_vault(
            str(vault), workspace="acme", confirmed=True,
        )
        assert renamed["counts"]["renamed"] == 1
        item = service.store.conn.execute(
            "SELECT relative_path FROM source_imports WHERE subject_key=?",
            (home.subject_key,),
        ).fetchone()
        assert item["relative_path"] == "Welcome.md"

        moved.unlink()
        missing = service.import_obsidian_vault(
            str(vault), workspace="acme", confirmed=True,
        )
        assert missing["counts"]["missing"] == 1
        manifest = service.store.conn.execute(
            "SELECT state FROM source_imports WHERE subject_key=?",
            (home.subject_key,),
        ).fetchone()
        assert manifest["state"] == "missing"
        assert service.store.conn.execute(
            "SELECT COUNT(*) FROM memories WHERE subject_key=?", (home.subject_key,),
        ).fetchone()[0] == 3
    finally:
        service.close()


def test_metadata_only_revision_and_conflict_policies(tmp_path: Path):
    vault = _vault(tmp_path)
    service = _service(tmp_path / "memory.db")
    try:
        service.import_obsidian_vault(str(vault), workspace="acme", confirmed=True)
        source = service.store.conn.execute(
            "SELECT * FROM source_imports WHERE relative_path='Home.md'"
        ).fetchone()
        old = service.store.get_memory(source["memory_id"])
        assert old is not None

        # A metadata-only frontmatter change is still a temporal source revision.
        path = vault / "Home.md"
        path.write_text(path.read_text(encoding="utf-8").replace("index, private", "index"),
                        encoding="utf-8")
        report = service.import_obsidian_vault(str(vault), workspace="acme", confirmed=True)
        assert report["counts"]["updated"] == 1
        current = service.store.conn.execute(
            "SELECT * FROM source_imports WHERE relative_path='Home.md' AND state='imported'"
        ).fetchone()
        assert current["memory_id"] != old.id

        # A local correction closes the importer's current record. The default importer
        # reports a conflict and does not silently supersede that divergent lineage.
        imported = service.store.get_memory(current["memory_id"])
        correction = service.engine.remember_with_resolution(
            "# Home base\nOwner correction.\n",
            workspace_id=imported.workspace_id, mtype=imported.mtype,
            scope=imported.scope, title=imported.title,
            subject_key=imported.subject_key, claim_kind=imported.claim_kind,
            valid_from=(imported.valid_from or 0) + 1,
        )
        assert correction["op"] == "invalidate"
        path.write_text("# Home base\nSource changed after correction.\n", encoding="utf-8")
        conflict = service.import_obsidian_vault(str(vault), workspace="acme", confirmed=True)
        assert conflict["state"] == "partial"
        assert conflict["counts"]["conflict"] == 1
        assert service.store.get_memory(correction["id"]).valid_to is None

        replaced = service.import_obsidian_vault(
            str(vault), workspace="acme", confirmed=True, on_conflict="replace",
        )
        assert replaced["counts"]["updated"] == 1
        assert service.store.get_memory(correction["id"]).valid_to is not None
    finally:
        service.close()


def test_cancelled_import_resumes_without_duplicates(tmp_path: Path):
    vault = _vault(tmp_path)
    db = tmp_path / "memory.db"
    service = _service(db)
    checks = 0

    def cancel_after_one() -> bool:
        nonlocal checks
        checks += 1
        return checks > 1

    try:
        cancelled = service.import_obsidian_vault(
            str(vault), workspace="acme", confirmed=True,
            cancel_check=cancel_after_one,
        )
        assert cancelled["state"] == "cancelled"
        assert service.store.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
        resumed = service.import_obsidian_vault(str(vault), workspace="acme", confirmed=True)
        assert resumed["state"] == "completed"
        assert resumed["counts"]["skipped"] == 2  # one unchanged note + .obsidian exclusion
        assert resumed["counts"]["imported"] == 1
        assert service.store.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 2
    finally:
        service.close()


def test_link_reconciliation_cancels_and_rolls_back_only_the_open_batch(tmp_path: Path):
    vault = _vault(tmp_path)
    service = _service(tmp_path / "memory.db")
    try:
        report = service.import_obsidian_vault(str(vault), workspace="acme", confirmed=True)
        service.store.conn.execute("DELETE FROM mem_links")
        service.store.conn.commit()
        checks = 0

        def cancel_after_first_source() -> bool:
            nonlocal checks
            checks += 1
            return checks >= 2

        with pytest.raises(ObsidianImportCancelled):
            ObsidianImporter(service)._reconcile_links(
                scan_obsidian_vault(str(vault)), vault_id=report["vault_id"],
                job_id=report["job_id"], cancel_check=cancel_after_first_source,
            )
        assert service.store.conn.execute("SELECT COUNT(*) FROM mem_links").fetchone()[0] == 0
    finally:
        service.close()


def test_rename_planner_indexes_hashes_once_for_large_sources():
    class CountingItems(list):
        iterations = 0

        def __iter__(self):
            self.iterations += 1
            return super().__iter__()

    notes = [
        ObsidianNote(
            relative_path=f"current/{index}.md", title=str(index), content="body", body="body",
            raw_sha256=f"{index:064x}", canonical_sha256=f"{index:064x}",
        )
        for index in range(128)
    ]
    items = CountingItems([
        {
            "id": f"src_{index}", "source_key": f"{index + 1000:064x}",
            "relative_path": f"archived/{index}.md", "content_sha256": f"{index:064x}",
            "state": "imported", "last_seen_at": index,
        }
        for index in range(128)
    ])
    plans, missing = ObsidianImporter()._plan(
        ObsidianVaultScan(vault_path="", vault_id="a" * 64, notes=notes),
        "a" * 64, items, inspect_memories=False,
    )
    assert not missing
    assert {plan.action for plan in plans} == {"renamed"}
    # The prior implementation iterated the full historical item collection once
    # per unmatched note. The indexed planner has only setup/final missing passes.
    assert items.iterations <= 3


def test_conflict_new_branch_and_atomic_note_failure(tmp_path: Path, monkeypatch):
    vault = tmp_path / "Vault"
    vault.mkdir()
    note = vault / "Note.md"
    note.write_text("# Note\nInitial.\n", encoding="utf-8")
    service = _service(tmp_path / "memory.db")
    try:
        service.import_obsidian_vault(str(vault), workspace="acme", confirmed=True)
        item = service.store.conn.execute("SELECT * FROM source_imports").fetchone()
        current = service.store.get_memory(item["memory_id"])
        service.engine.remember_with_resolution(
            "# Note\nOwner version.\n", workspace_id=current.workspace_id,
            scope=current.scope, mtype=current.mtype, title=current.title,
            subject_key=current.subject_key, claim_kind=current.claim_kind,
            valid_from=(current.valid_from or 0) + 1,
        )
        note.write_text("# Note\nIndependent source branch.\n", encoding="utf-8")
        branched = service.import_obsidian_vault(
            str(vault), workspace="acme", confirmed=True, on_conflict="new",
        )
        assert branched["state"] == "completed"
        assert branched["counts"]["imported"] == 1
        assert service.store.conn.execute(
            "SELECT COUNT(*) FROM source_imports WHERE relative_path='Note.md'"
        ).fetchone()[0] == 2

        # A manifest failure raised by the transactional finalizer rolls back the
        # canonical memory/FTS/vector mirrors for that note as one unit.
        second = vault / "Atomic.md"
        second.write_text("# Atomic\nMust be all or nothing.\n", encoding="utf-8")
        original = service.store.upsert_source_import_item

        def fail_atomic(**kwargs):
            if kwargs.get("relative_path") == "Atomic.md" and not kwargs.get("commit", True):
                raise RuntimeError("injected finalizer failure")
            return original(**kwargs)

        monkeypatch.setattr(service.store, "upsert_source_import_item", fail_atomic)
        failed = service.import_obsidian_vault(str(vault), workspace="acme", confirmed=True)
        assert failed["state"] == "partial", failed
        assert failed["counts"]["error"] == 1
        assert service.store.conn.execute(
            "SELECT COUNT(*) FROM memories WHERE title='Atomic'"
        ).fetchone()[0] == 0
        assert service.store.conn.execute(
            "SELECT COUNT(*) FROM mem_fts WHERE title='Atomic'"
        ).fetchone()[0] == 0
    finally:
        service.close()


def test_cli_dry_run_is_strictly_write_free(tmp_path: Path, capsys):
    vault = _vault(tmp_path)
    db = tmp_path / "does-not-exist.db"
    result = importer_cli.main([
        "obsidian", str(vault), "--db", str(db), "--workspace", "acme",
        "--dry-run", "--json",
    ])
    assert result == 0
    assert not db.exists()
    assert not Path(str(db) + "-wal").exists()
    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == "preview"
    assert payload["counts"]["markdown"] == 2


def test_repo_and_session_scope_mapping(tmp_path: Path):
    vault = _vault(tmp_path)
    service = _service(tmp_path / "memory.db")
    try:
        repo_report = service.import_obsidian_vault(
            str(vault), workspace="acme", repo="product", scope="repo", confirmed=True,
        )
        assert repo_report["target"]["scope"] == Scope.REPO.value
        assert all(memory.scope == Scope.REPO for memory in _live(service))

        session_vault = tmp_path / "Session"
        session_vault.mkdir()
        (session_vault / "Only.md").write_text("# Only\nSession memory.\n", encoding="utf-8")
        wid = service._lookup_workspace("acme")
        rid = service._lookup_repo(wid, "product")
        sid = service.store.start_session(wid, rid)
        session_report = service.import_obsidian_vault(
            str(session_vault), workspace="acme", repo="product",
            session_id=sid, scope="session", confirmed=True,
        )
        assert session_report["target"]["session_id"] == sid
        assert service.store.conn.execute(
            "SELECT session_id FROM jobs WHERE id=?", (session_report["job_id"],)
        ).fetchone()["session_id"] == sid
        record = service.store.get_memory(next(
            row["memory_id"] for row in service.store.list_source_import_items(
                vault_id=session_report["vault_id"]
            )
        ))
        assert record.scope == Scope.SESSION
        assert record.session_id == sid
    finally:
        service.close()


def test_browser_upload_runs_as_resumable_job_without_upload_copy(tmp_path: Path):
    service = _service(tmp_path / "memory.db")
    try:
        started = service.import_obsidian_upload(
            files=[("Folder/Browser.md", b"# Browser\nSee [[Second]].\n"),
                   ("Second.md", b"# Second\nLocal only.\n")],
            attachment_manifest=[{"path": "assets/photo.png", "size": 123}],
            workspace="acme", vault_label="Browser vault", confirmed=True,
        )
        job_id = started["job_id"]
        worker = service._obsidian_job_threads.get(job_id)
        if worker is not None:
            worker.join(10)
        status = service.get_obsidian_import_job(job_id, workspace="acme")
        assert status["state"] == "completed"
        assert status["counts"]["imported"] == 2
        assert not any(tmp_path.glob("**/*Browser.md"))
        assert service.store.conn.execute(
            "SELECT COUNT(*) FROM mem_links WHERE relation='references' AND valid_to IS NULL"
        ).fetchone()[0] == 1
    finally:
        service.close()
