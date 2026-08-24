"""Real-service coverage for the owner-only Obsidian import facade."""
from __future__ import annotations

import hashlib
import time

import pytest

from engraphis.service import MemoryService, ValidationError
from engraphis.obsidian_import import ObsidianImporter, scan_obsidian_upload


_TERMINAL_STATES = {"completed", "partial", "failed", "cancelled"}
_SECRET = "sk-proj-0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _service() -> MemoryService:
    return MemoryService.create(
        ":memory:", embed_dim=64, extractor="none", graph_extractor="none",
        retention_supervisor="none",
    )


def _await_job(service: MemoryService, started: dict) -> dict:
    job_id = started["job_id"]
    worker = service._obsidian_job_threads.get(job_id)
    if worker is not None:
        worker.join(10)
    deadline = time.monotonic() + 10
    while True:
        result = service.get_obsidian_import_job(job_id, workspace=started["workspace"])
        if result["state"] in _TERMINAL_STATES:
            return result
        if time.monotonic() >= deadline:
            raise AssertionError(f"Obsidian job {job_id} did not finish: {result}")
        time.sleep(0.01)


def _import(service: MemoryService, files: list[tuple[str, bytes]], **kwargs) -> tuple[dict, dict]:
    started = service.import_obsidian_upload(
        files=files, attachment_manifest=[], workspace="alpha",
        vault_label="Team notes", confirmed=True, **kwargs,
    )
    return started, _await_job(service, started)


def test_preview_pages_the_full_manifest_like_execution():
    service = _service()
    try:
        started, imported = _import(
            service,
            [
                ("A.md", b"# A\n"),
                ("B.md", b"# B\n"),
                ("C.md", b"# C\n"),
            ],
        )
        vault_id = started["vault_id"]
        assert imported["state"] == "completed"

        # Push the vault manifest past the default 10k list-page boundary with
        # filler identity rows that sort before the scan set, so an unpaged
        # preview loses exactly the rows a real oversized vault would lose.
        for i in range(10_001):
            service.store.upsert_source_import_item(
                vault_id=vault_id,
                source_key=hashlib.sha256(f"seed-{i}".encode()).hexdigest(),
                relative_path=f"0000-seed-{i:05d}.md",
            )

        preview = service.preview_obsidian_upload(
            files=[("A.md", b"# A\n"), ("D.md", b"# D\n")],
            attachment_manifest=[],
            workspace="alpha",
            vault_label="Team notes",
            vault_id=vault_id,
        )

        # An unpaged preview reads only the first list page, so manifest rows
        # beyond the boundary vanish from the report entirely. The paged reader
        # must surface every manifest row: A plans as unchanged and B/C — not
        # part of this preview's scan — are reported as missing, not dropped.
        statuses = {
            row["relative_path"]: row["status"] for row in preview["files"]
        }
        assert statuses["A.md"] != "missing"
        assert "B.md" in statuses
        assert "C.md" in statuses
    finally:
        service.close()


def test_preview_defers_missing_rows_when_manifest_is_truncated(monkeypatch):
    service = _service()
    try:
        started, imported = _import(service, [("A.md", b"# A\n")])
        assert imported["state"] == "completed"
        vault_id = started["vault_id"]
        service.store.upsert_source_import_item(
            vault_id=vault_id,
            source_key=hashlib.sha256(b"gone").hexdigest(),
            relative_path="gone.md",
        )
        items = service.store.list_source_import_items(vault_id=vault_id)

        def _truncated(_self, *, vault_id, states=None):
            del vault_id, states
            return items, False

        monkeypatch.setattr(ObsidianImporter, "_all_source_items", _truncated)
        preview = service.preview_obsidian_upload(
            files=[("A.md", b"# A\n")], attachment_manifest=[],
            workspace="alpha", vault_label="Team notes", vault_id=vault_id,
        )

        statuses = {
            row["relative_path"]: row["status"] for row in preview["files"]
        }
        assert preview["manifest_complete"] is False
        assert preview["counts"].get("missing", 0) == 0
        assert preview["counts"]["pending"] == 1
        assert statuses["gone.md"] == "pending"
    finally:
        service.close()


def test_late_manifest_truncation_marks_import_partial(monkeypatch):
    service = _service()
    try:
        original = ObsidianImporter._all_source_items
        calls = 0

        def _late_truncation(self, *, vault_id, states=None):
            nonlocal calls
            calls += 1
            items, complete = original(self, vault_id=vault_id, states=states)
            return items, complete if calls == 1 else False

        monkeypatch.setattr(ObsidianImporter, "_all_source_items", _late_truncation)
        _, report = _import(
            service,
            [("A.md", b"# A\nSee [[B]].\n"), ("B.md", b"# B\n")],
        )

        assert calls >= 2
        assert report["state"] == "partial"
    finally:
        service.close()


def test_preview_is_write_free_and_service_enforces_confirmation_and_upload_guards(monkeypatch):
    service = _service()
    try:
        with pytest.raises(ValueError, match="vault_label is required"):
            scan_obsidian_upload([("One.md", b"# One\n")], vault_label=" ")
        collision = scan_obsidian_upload(
            [
                ("Notes/Caf\u00e9.md", b"# First\n"),
                ("notes/cafe\u0301.md", b"# Second\n"),
            ],
            vault_label="Portable vault",
        )
        assert [note.relative_path for note in collision.notes] == ["Notes/Caf\u00e9.md"]
        assert [(item.relative_path, item.reason) for item in collision.rejected] == [
            ("notes/caf\u00e9.md", "duplicate upload path"),
        ]

        preview = service.preview_obsidian_upload(
            files=[
                ("notes/Welcome.md", b"# Welcome\nPRIVATE_BODY_MARKER\n"),
                ("../escape.md", b"# Escape\n"),
            ],
            attachment_manifest=[{"path": "assets/photo.png", "size": 12}],
            workspace="alpha", vault_label="Team notes",
        )

        assert preview["state"] == "preview"
        assert preview["counts"]["imported"] == 1
        assert preview["counts"]["rejected"] == 1
        assert "PRIVATE_BODY_MARKER" not in str(preview)
        for table in (
            "workspaces", "source_vaults", "source_imports", "jobs",
            "memories", "operation_receipts", "audit",
        ):
            count = service.store.conn.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()
            assert count is not None and count[0] == 0

        with pytest.raises(ValidationError, match="vault_label is required"):
            service.import_obsidian_upload(
                files=[("One.md", b"# One\n")], attachment_manifest=[],
                workspace="must-not-exist", confirmed=True,
            )
        assert service._lookup_workspace("must-not-exist") is None

        with pytest.raises(ValidationError, match="confirmation"):
            service.import_obsidian_upload(
                files=[("One.md", b"# One\n")], attachment_manifest=[],
                workspace="alpha", vault_label="Team notes", confirmed="true",  # type: ignore[arg-type]
            )
        with pytest.raises(ValidationError, match="invalid path"):
            service.preview_obsidian_upload(
                files=[("One.md", b"# One\n")],
                attachment_manifest=[{"path": "../outside.png", "size": 1}],
                workspace="alpha", vault_label="Team notes",
            )
        with pytest.raises(ValidationError, match="OpenAI API key"):
            service.preview_obsidian_upload(
                files=[("One.md", b"# One\n")], attachment_manifest=[],
                workspace="alpha", vault_label=_SECRET,
            )
        monkeypatch.setattr("engraphis.core.obsidian.MAX_NOTE_BYTES", 10)
        monkeypatch.setattr("engraphis.core.obsidian.MAX_VAULT_BYTES", 15)
        with pytest.raises(ValidationError, match="duplicate path"):
            service._obsidian_upload_inputs(
                [("Notes/One.md", b"a"), ("notes/one.md", b"b")], [],
            )
        with pytest.raises(ValidationError, match="paths overlap"):
            service._obsidian_upload_inputs(
                [("notes/one.md", b"a")],
                [{"path": "notes/one.md", "size": 1}],
            )
        with pytest.raises(ValidationError, match="invalid size"):
            service._obsidian_upload_inputs(
                [("notes/one.md", b"a")],
                [{"path": "assets/large.png", "size": 100_000_001}],
            )
        with pytest.raises(ValidationError, match="too large"):
            service._obsidian_upload_inputs([("notes/big.md", b"x" * 11)], [])
        with pytest.raises(ValidationError, match="total size"):
            service._obsidian_upload_inputs(
                [("notes/one.md", b"x" * 8), ("notes/two.md", b"y" * 8)], [],
            )
    finally:
        service.close()


def test_obsidian_browser_label_identity_is_nfc_and_requires_registered_vault():
    service = _service()
    try:
        first = service.import_obsidian_upload(
            files=[("One.md", b"# One\nLocal note.\n")], attachment_manifest=[],
            workspace="alpha", vault_label="Caf\u00e9", confirmed=True,
        )
        assert _await_job(service, first)["state"] == "completed"
        assert scan_obsidian_upload(
            [("One.md", b"# One\n")], vault_label="Caf\u00e9",
        ).vault_id == scan_obsidian_upload(
            [("One.md", b"# One\n")], vault_label="Cafe\u0301",
        ).vault_id
        with pytest.raises(ValidationError, match="select its source_id"):
            service.preview_obsidian_upload(
                files=[("One.md", b"# One\nChanged selection.\n")],
                attachment_manifest=[], workspace="alpha", vault_label="Cafe\u0301",
            )
        resumed = service.preview_obsidian_upload(
            files=[("One.md", b"# One\nLocal note.\n")],
            attachment_manifest=[], workspace="alpha", vault_id=first["vault_id"],
        )
        assert resumed["vault_id"] == first["vault_id"]
        assert resumed["vault_label"] == "Caf\u00e9"
    finally:
        service.close()


def test_new_workspace_previews_cannot_reuse_another_workspaces_source(tmp_path):
    service = _service()
    try:
        started, job = _import(
            service, [("One.md", b"# One\nApproved upload.\n")],
        )
        assert job["state"] == "completed"
        upload_preview = service.preview_obsidian_upload(
            files=[("One.md", b"# One\nApproved upload.\n")],
            attachment_manifest=[], workspace="upload-target-does-not-exist",
            vault_label="Team notes",
        )
        assert upload_preview["vault_id"] is None
        assert upload_preview["counts"]["imported"] == 1
        assert upload_preview["counts"].get("skipped", 0) == 0
        assert upload_preview["vault_id"] != started["vault_id"]
        assert service._lookup_workspace("upload-target-does-not-exist") is None

        vault = tmp_path / "DiskVault"
        vault.mkdir()
        (vault / "One.md").write_text(
            "# One\nApproved disk note.\n", encoding="utf-8",
        )
        imported = service.import_obsidian_vault(
            str(vault), workspace="alpha", vault_label="Disk notes",
            confirmed=True,
        )
        assert imported["state"] == "completed"
        disk_preview = service.preview_obsidian_vault(
            str(vault), workspace="disk-target-does-not-exist",
            vault_label="Disk notes",
        )
        assert disk_preview["vault_id"] is None
        assert disk_preview["counts"]["imported"] == 1
        assert disk_preview["counts"].get("skipped", 0) == 0
        assert disk_preview["vault_id"] != imported["vault_id"]
        assert service._lookup_workspace("disk-target-does-not-exist") is None
    finally:
        service.close()


def test_import_job_vault_listing_receipt_and_workspace_isolation_are_content_free():
    service = _service()
    try:
        started, job = _import(
            service,
            [("Folder/One.md", b"# One\nUPLOAD_BODY_MARKER\nSee [[Two]].\n"),
             ("Two.md", b"# Two\nSecond note.\n")],
        )
        assert job["state"] == "completed"
        assert job["counts"]["imported"] == 2
        assert job["processed_items"] == 2
        assert {row["relative_path"] for row in job["files"]} == {
            "Folder/One.md", "Two.md",
        }
        assert "UPLOAD_BODY_MARKER" not in str(job)

        vaults = service.list_obsidian_vaults("alpha")
        assert len(vaults) == 1
        assert set(vaults[0]) == {
            "id", "label", "workspace", "repo", "session_id", "scope",
            "memory_type", "importer_version",
        }
        assert "root_digest" not in str(vaults)
        resumed_preview = service.preview_obsidian_upload(
            files=[("Folder/One.md", b"# One\nUPLOAD_BODY_MARKER\nSee [[Two]].\n")],
            attachment_manifest=[], workspace="alpha", vault_id=started["vault_id"],
        )
        assert resumed_preview["vault_id"] == started["vault_id"]
        assert resumed_preview["vault_label"] == "Team notes"
        workspace_id = service._lookup_workspace("alpha")
        assert workspace_id is not None
        receipt = service.store.list_receipts(workspace_id=workspace_id, limit=10)[0]
        assert receipt["operation"] == "obsidian_import"
        assert receipt["target_count"] == 2
        assert "One.md" not in str(receipt)
        assert "UPLOAD_BODY_MARKER" not in str(receipt)

        service.create_workspace("beta")
        assert service.list_obsidian_vaults("beta") == []
        with pytest.raises(KeyError):
            service.get_obsidian_import_job(started["job_id"], workspace="beta")
        with pytest.raises(ValidationError, match="that target"):
            service.preview_obsidian_upload(
                files=[("One.md", b"# One\n")], attachment_manifest=[],
                workspace="beta", vault_id=started["vault_id"],
                vault_label="Team notes",
            )
        with pytest.raises(ValidationError, match="not found"):
            service.import_obsidian_upload(
                files=[("One.md", b"# One\n")], attachment_manifest=[],
                workspace="must-not-be-created", vault_id=started["vault_id"],
                vault_label="Team notes", confirmed=True,
            )
        assert service._lookup_workspace("must-not-be-created") is None
        with pytest.raises(ValueError, match="different import defaults"):
            service.preview_obsidian_upload(
                files=[("One.md", b"# One\n")], attachment_manifest=[],
                workspace="alpha", vault_id=started["vault_id"],
                vault_label="Team notes", memory_type="procedural",
            )
    finally:
        service.close()


def test_upload_reimport_is_idempotent_and_tracks_rename_and_missing():
    service = _service()
    try:
        initial_files = [
            ("One.md", b"# One\nStable note.\n"),
            ("Folder/Two.md", b"# Two\nWill be removed.\n"),
        ]
        started, first = _import(service, initial_files)
        assert first["counts"]["imported"] == 2
        memory_count_row = service.store.conn.execute(
            "SELECT COUNT(*) FROM memories"
        ).fetchone()
        assert memory_count_row is not None
        memory_count = memory_count_row[0]

        _again, second = _import(
            service, initial_files, vault_id=started["vault_id"],
        )
        assert second["counts"]["skipped"] == 2
        current_count = service.store.conn.execute(
            "SELECT COUNT(*) FROM memories"
        ).fetchone()
        assert current_count is not None and current_count[0] == memory_count

        _moved, third = _import(
            service, [("Archive/One.md", initial_files[0][1])],
            vault_id=started["vault_id"],
        )
        assert third["counts"]["renamed"] == 1
        assert third["counts"]["missing"] == 1
        manifest = service.store.list_source_import_items(vault_id=started["vault_id"])
        assert {(row["relative_path"], row["state"]) for row in manifest} == {
            ("Archive/One.md", "renamed"), ("Folder/Two.md", "missing"),
        }
    finally:
        service.close()


def test_upload_conflict_error_and_replace_policies_preserve_temporal_lineage():
    service = _service()
    try:
        started, _first = _import(service, [("One.md", b"# One\nImported.\n")])
        source = service.store.list_source_import_items(vault_id=started["vault_id"])[0]
        imported = service.store.get_memory(source["memory_id"])
        assert imported is not None
        assert imported.workspace_id is not None
        correction = service.engine.remember_with_resolution(
            "# One\nOwner correction.\n", workspace_id=imported.workspace_id,
            repo_id=imported.repo_id, session_id=imported.session_id,
            scope=imported.scope, mtype=imported.mtype, title=imported.title,
            subject_key=imported.subject_key, claim_kind=imported.claim_kind,
            valid_from=(imported.valid_from or 0) + 1,
        )

        _conflict, reported = _import(
            service, [("One.md", b"# One\nSource changed.\n")],
            vault_id=started["vault_id"], on_conflict="error",
        )
        assert reported["state"] == "partial"
        assert reported["counts"]["conflict"] == 1
        corrected = service.store.get_memory(correction["id"])
        assert corrected is not None and corrected.valid_to is None

        _replace, replaced = _import(
            service, [("One.md", b"# One\nSource changed.\n")],
            vault_id=started["vault_id"], on_conflict="replace",
        )
        assert replaced["state"] == "completed"
        assert replaced["counts"]["updated"] == 1
        superseded = service.store.get_memory(correction["id"])
        assert superseded is not None and superseded.valid_to is not None
    finally:
        service.close()
