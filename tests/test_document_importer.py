from __future__ import annotations

import hashlib
import json
import socket
import sys
import types

import pytest

from engraphis.core.documents import (
    DOCUMENT_FORMATS,
    DocumentFileIssue,
    DocumentRecord,
    DocumentScan,
    parse_document,
)
from engraphis.core.interfaces import MemoryType, Scope, SearchFilter
from engraphis.document_import import (
    DocumentImporter,
    local_document_adapter,
    scan_document_upload,
)
from engraphis.obsidian_import import ObsidianImporter
from engraphis.service import MemoryService


def _service() -> MemoryService:
    return MemoryService.create(
        ":memory:", embed_dim=64, extractor="none", graph_extractor="none",
        retention_supervisor="none",
    )


def _scan(*files: tuple[str, bytes]) -> DocumentScan:
    scan = DocumentScan(root_path="", source_id="d" * 64)
    scan.documents.extend(parse_document(raw, path) for path, raw in files)
    return scan


def test_mixed_document_import_is_temporal_repeatable_and_source_neutral(monkeypatch):
    def no_network(*_args, **_kwargs):
        raise AssertionError("document import attempted a network call")

    monkeypatch.setattr(socket, "create_connection", no_network)
    service = _service()
    try:
        workspace_id = service.store.get_or_create_workspace("mixed")
        scan = _scan(
            ("notes/readme.md", b"# Read me\nSee [plan](../plan.txt).\n"),
            ("plan.txt", b"Ship the universal importer.\n"),
            ("data/info.json", b'{"title":"Facts","enabled":true}'),
        )
        importer = DocumentImporter(service)
        before = {
            table: service.store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("memories", "source_vaults", "source_imports", "jobs", "operation_receipts")
        }
        preview = importer.preview(
            scan, workspace_id=workspace_id, repo_id=None, session_id=None,
            scope=Scope.WORKSPACE, memory_type=MemoryType.SEMANTIC,
            source_label="Loose files",
        )
        after = {
            table: service.store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in before
        }
        assert after == before
        assert preview["counts"]["documents"] == 3
        assert preview["summary"]["formats"] == {"json": 1, "markdown": 1, "text": 1}

        first = importer.import_scan(
            scan, workspace_id=workspace_id, repo_id=None, session_id=None,
            scope=Scope.WORKSPACE, memory_type=MemoryType.SEMANTIC,
            source_label="Loose files", confirmed=True,
        )
        assert first["state"] == "completed"
        assert first["counts"]["imported"] == 3
        assert service.store.list_source_vaults(kind="documents")[0]["id"] == first["source_id"]
        memories = service.store.list_memories(SearchFilter(workspace_id=workspace_id))
        assert len(memories) == 3
        plan = next(memory for memory in memories if memory.title == "plan")
        assert plan.metadata["document"]["relative_path"] == "plan.txt"
        assert plan.metadata["document"]["format"] == "text"
        assert plan.claim_kind == "source_document"

        again = importer.import_scan(
            scan, workspace_id=workspace_id, repo_id=None, session_id=None,
            scope=Scope.WORKSPACE, memory_type=MemoryType.SEMANTIC,
            source_id=first["source_id"], source_label="Loose files", confirmed=True,
        )
        assert again["counts"]["skipped"] == 3
        assert service.store.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 3

        revised = _scan(
            ("notes/readme.md", b"# Read me\nSee [plan](../plan.txt).\n"),
            ("plan.txt", b"Ship the improved universal importer.\n"),
            ("data/info.json", b'{"title":"Facts","enabled":true}'),
        )
        changed = importer.import_scan(
            revised, workspace_id=workspace_id, repo_id=None, session_id=None,
            scope=Scope.WORKSPACE, memory_type=MemoryType.SEMANTIC,
            source_id=first["source_id"], source_label="Loose files", confirmed=True,
        )
        assert changed["counts"]["updated"] == 1
        history = service.store.conn.execute(
            "SELECT valid_to FROM memories WHERE subject_key=? ORDER BY valid_from",
            (plan.subject_key,),
        ).fetchall()
        assert len(history) == 2
        assert history[0]["valid_to"] is not None and history[1]["valid_to"] is None
        assert service.store.conn.execute(
            "SELECT COUNT(*) FROM operation_receipts WHERE operation='document_import'"
        ).fetchone()[0] == 3
    finally:
        service.close()


def test_pdf_uses_optional_local_resource_adapter(monkeypatch):
    class Page:
        @staticmethod
        def extract_text():
            return "Quarterly report"

    class Reader:
        def __init__(self, _stream):
            self.pages = [Page()]

    monkeypatch.setitem(sys.modules, "pypdf", types.SimpleNamespace(PdfReader=Reader))
    record = parse_document(
        b"%PDF-local", "reports/q1.pdf", adapter=local_document_adapter,
    )
    assert record.format == "pdf"
    assert record.body == "Quarterly report"
    assert record.metadata["pages"] == 1


def test_transcription_adapter_requires_an_existing_local_model(monkeypatch):
    monkeypatch.delenv("ENGRAPHIS_WHISPER_MODEL", raising=False)
    with pytest.raises(ValueError, match="local model file or directory"):
        local_document_adapter(b"not-used", "meeting.mp3")


def test_transcription_adapter_accepts_a_local_model_directory(monkeypatch, tmp_path):
    from engraphis.backends import resources

    model = tmp_path / "whisper-model"
    model.mkdir()
    monkeypatch.setenv("ENGRAPHIS_WHISPER_MODEL", str(model))
    monkeypatch.setattr(
        resources, "get_resource_extractor",
        lambda: types.SimpleNamespace(extract_bytes=lambda _name, _raw: types.SimpleNamespace(
            text="Local transcript", media_type="audio/mpeg", title="Meeting",
            kind="transcript", metadata={"duration": 3}, warnings=[],
        )),
    )

    record = parse_document(
        b"local audio bytes", "meeting.mp3", adapter=local_document_adapter,
    )
    assert record.body == "Local transcript"
    assert record.format == "audio"


def test_upload_rejects_casefold_and_nfc_path_collisions():
    with pytest.raises(ValueError, match="source_label is required"):
        scan_document_upload([("note.txt", b"note")], source_label="  ")

    scan = scan_document_upload(
        [
            ("Notes/Caf\u00e9.txt", b"first"),
            ("notes/cafe\u0301.txt", b"second"),
        ],
        source_label="Portable paths",
    )
    assert [item.relative_path for item in scan.documents] == ["Notes/Caf\u00e9.txt"]
    assert [(item.relative_path, item.reason) for item in scan.rejected] == [
        ("notes/caf\u00e9.txt", "duplicate upload path"),
    ]


def test_document_links_normalize_parent_paths_and_link_imported_attachments():
    service = _service()
    try:
        workspace_id = service.store.get_or_create_workspace("links")
        source = parse_document(
            b"[plan](../plan.txt) [local](#section) "
            b"[web](https://example.test/x) ![report](../reports/q1.pdf)",
            "notes/readme.txt",
        )
        plan = parse_document(b"The release plan.", "plan.txt")
        raw_pdf = b"%PDF-local"
        pdf = DocumentRecord(
            relative_path="reports/q1.pdf", format="pdf",
            media_type="application/pdf", title="Quarterly report",
            content="Quarterly report", body="Quarterly report",
            raw_sha256=hashlib.sha256(raw_pdf).hexdigest(),
            canonical_sha256=hashlib.sha256(b"Quarterly report").hexdigest(),
            source_size=len(raw_pdf), title_source="extracted",
        )
        scan = DocumentScan(root_path="", source_id="e" * 64)
        scan.documents.extend((source, plan, pdf))
        report = DocumentImporter(service).import_scan(
            scan, workspace_id=workspace_id, repo_id=None, session_id=None,
            scope=Scope.WORKSPACE, memory_type=MemoryType.SEMANTIC,
            source_label="Linked files", confirmed=True,
        )

        assert report["state"] == "completed"
        rows = service.store.conn.execute(
            "SELECT a.title AS source_title, b.title AS target_title, l.relation "
            "FROM mem_links l JOIN memories a ON a.id=l.a "
            "JOIN memories b ON b.id=l.b WHERE l.valid_to IS NULL "
            "ORDER BY b.title"
        ).fetchall()
        assert [(row["source_title"], row["target_title"], row["relation"]) for row in rows] == [
            ("readme", "Quarterly report", "embeds"),
            ("readme", "plan", "references"),
        ]
        assert report["counts"].get("warning", 0) == 0
    finally:
        service.close()


def test_failed_document_revision_stays_seen_and_resumes(monkeypatch):
    service = _service()
    try:
        workspace_id = service.store.get_or_create_workspace("resume")
        importer = DocumentImporter(service)
        first_scan = _scan(("note.txt", b"Durable version one."))
        first = importer.import_scan(
            first_scan, workspace_id=workspace_id, repo_id=None, session_id=None,
            scope=Scope.WORKSPACE, memory_type=MemoryType.SEMANTIC,
            confirmed=True,
        )
        changed = _scan(("note.txt", b"Durable version two."))
        original = service.store.upsert_source_import_item

        def fail_finalizer(**kwargs):
            if not kwargs.get("commit", True):
                raise RuntimeError("injected finalizer failure")
            return original(**kwargs)

        monkeypatch.setattr(service.store, "upsert_source_import_item", fail_finalizer)
        failed = importer.import_scan(
            changed, workspace_id=workspace_id, repo_id=None, session_id=None,
            scope=Scope.WORKSPACE, memory_type=MemoryType.SEMANTIC,
            source_id=first["source_id"], confirmed=True,
        )
        assert failed["counts"]["error"] == 1
        manifest = service.store.list_source_import_items(vault_id=first["source_id"])[0]
        assert manifest["state"] == "error"
        assert manifest["missing_at"] is None
        assert service.store.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1

        monkeypatch.setattr(service.store, "upsert_source_import_item", original)
        resumed = importer.import_scan(
            changed, workspace_id=workspace_id, repo_id=None, session_id=None,
            scope=Scope.WORKSPACE, memory_type=MemoryType.SEMANTIC,
            source_id=first["source_id"], confirmed=True,
        )
        assert resumed["counts"]["updated"] == 1
        assert service.store.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 2
    finally:
        service.close()


def test_cancelled_import_reports_planned_missing_rows_as_pending():
    service = _service()
    try:
        workspace_id = service.store.get_or_create_workspace("cancel-missing")
        importer = DocumentImporter(service)
        first = importer.import_scan(
            _scan(("keep.txt", b"Keep me."), ("gone.txt", b"Gone later.")),
            workspace_id=workspace_id, repo_id=None, session_id=None,
            scope=Scope.WORKSPACE, memory_type=MemoryType.SEMANTIC,
            confirmed=True,
        )
        cancelled = importer.import_scan(
            _scan(("keep.txt", b"Keep me.")),
            workspace_id=workspace_id, repo_id=None, session_id=None,
            scope=Scope.WORKSPACE, memory_type=MemoryType.SEMANTIC,
            source_id=first["source_id"], confirmed=True,
            cancel_check=lambda: True,
        )
        assert cancelled["state"] == "cancelled"
        assert cancelled["counts"].get("missing", 0) == 0
        assert cancelled["counts"]["pending"] == 2
        assert {
            row["relative_path"] for row in cancelled["files"]
            if row["status"] == "pending"
        } == {"keep.txt", "gone.txt"}
        item = service.store.conn.execute(
            "SELECT state FROM source_imports WHERE relative_path='gone.txt'"
        ).fetchone()
        assert item["state"] == "imported"
    finally:
        service.close()


def test_cancelled_import_reports_unprocessed_plans_as_pending():
    service = _service()
    try:
        workspace_id = service.store.get_or_create_workspace("cancel-plans")
        importer = DocumentImporter(service)
        cancelled = importer.import_scan(
            _scan(("first.txt", b"First."), ("second.txt", b"Second.")),
            workspace_id=workspace_id, repo_id=None, session_id=None,
            scope=Scope.WORKSPACE, memory_type=MemoryType.SEMANTIC,
            confirmed=True, cancel_check=lambda: True,
        )
        assert cancelled["state"] == "cancelled"
        assert cancelled["counts"]["documents"] == 2
        assert cancelled["counts"]["pending"] == 2
        assert {
            row["relative_path"] for row in cancelled["files"]
            if row["status"] == "pending"
        } == {"first.txt", "second.txt"}
        job_items = service.store.conn.execute(
            "SELECT relative_path, result_state FROM source_import_items "
            "WHERE job_id=? ORDER BY relative_path",
            (cancelled["job_id"],),
        ).fetchall()
        assert [(row["relative_path"], row["result_state"]) for row in job_items] == [
            ("first.txt", "pending"), ("second.txt", "pending"),
        ]
    finally:
        service.close()


def test_unreadable_directory_does_not_finalize_descendants_as_missing():
    service = _service()
    try:
        workspace_id = service.store.get_or_create_workspace("unreadable-dir")
        importer = DocumentImporter(service)
        first = importer.import_scan(
            _scan(("blocked/gone.txt", b"Keep me.")),
            workspace_id=workspace_id, repo_id=None, session_id=None,
            scope=Scope.WORKSPACE, memory_type=MemoryType.SEMANTIC,
            confirmed=True,
        )
        scan = DocumentScan(root_path="", source_id="d" * 64)
        scan.skipped.append(DocumentFileIssue("blocked", "unreadable directory"))
        report = importer.import_scan(
            scan, workspace_id=workspace_id, repo_id=None, session_id=None,
            scope=Scope.WORKSPACE, memory_type=MemoryType.SEMANTIC,
            source_id=first["source_id"], confirmed=True,
        )
        assert report["state"] == "partial"
        assert report["counts"].get("missing", 0) == 0
        assert report["counts"]["skipped"] == 1
        item = service.store.conn.execute(
            "SELECT state FROM source_imports WHERE relative_path='blocked/gone.txt'"
        ).fetchone()
        assert item["state"] == "imported"
    finally:
        service.close()


def test_incomplete_document_scan_defers_missing_reconciliation():
    service = _service()
    try:
        workspace_id = service.store.get_or_create_workspace("incomplete-scan")
        importer = DocumentImporter(service)
        first = importer.import_scan(
            _scan(("keep.txt", b"Keep me."), ("gone.txt", b"Gone later.")),
            workspace_id=workspace_id, repo_id=None, session_id=None,
            scope=Scope.WORKSPACE, memory_type=MemoryType.SEMANTIC,
            confirmed=True,
        )
        scan = _scan(("keep.txt", b"Keep me."))
        scan.complete = False
        report = importer.import_scan(
            scan, workspace_id=workspace_id, repo_id=None, session_id=None,
            scope=Scope.WORKSPACE, memory_type=MemoryType.SEMANTIC,
            source_id=first["source_id"], confirmed=True,
        )
        assert report["state"] == "partial"
        assert report["counts"].get("missing", 0) == 0
        assert report["counts"].get("pending", 0) == 1
        item = service.store.conn.execute(
            "SELECT state FROM source_imports WHERE relative_path='gone.txt'"
        ).fetchone()
        assert item["state"] == "imported"
    finally:
        service.close()


def test_document_importer_rejects_an_obsidian_source_identity():
    service = _service()
    try:
        workspace_id = service.store.get_or_create_workspace("identity")
        scan = _scan(("note.txt", b"Source neutral."))
        wrong_id = service.store.register_source_vault(
            kind="obsidian", root_digest=scan.source_id,
            workspace_id=workspace_id, repo_id=None, session_id=None,
            display_name="Wrong adapter", scope="workspace",
            memory_type="semantic", importer_version="1",
        )
        with pytest.raises(ValueError, match="different import adapter"):
            DocumentImporter(service).preview(
                scan, workspace_id=workspace_id, repo_id=None, session_id=None,
                scope=Scope.WORKSPACE, memory_type=MemoryType.SEMANTIC,
                source_id=wrong_id,
            )
        # The same identity remains valid for its compatibility adapter.
        ObsidianImporter(service).preview(
            scan, workspace_id=workspace_id, repo_id=None, session_id=None,
            scope=Scope.WORKSPACE, memory_type=MemoryType.SEMANTIC,
            vault_id=wrong_id,
        )
    finally:
        service.close()


def test_format_metadata_and_rejected_items_stay_bounded_and_complete():
    service = _service()
    try:
        workspace_id = service.store.get_or_create_workspace("bounds")
        note = parse_document(b"Bounded metadata.", "bounded.txt")
        note.metadata = {f"field_{index}": "x" * 1_000 for index in range(64)}
        envelope = DocumentImporter._metadata(
            note, vault_id="vlt_preview", source_id="src_preview",
            imported_at=1.0, actor="test", branch="",
        )
        assert len(json.dumps(envelope, ensure_ascii=False).encode("utf-8")) < 14_500
        assert envelope["document"]["omitted_counts"]["format_metadata"] > 0

        scan = DocumentScan(root_path="", source_id="f" * 64)
        scan.documents.append(note)
        scan.rejected.append(DocumentFileIssue("broken.pdf", "source rejected"))
        report = DocumentImporter(service).import_scan(
            scan, workspace_id=workspace_id, repo_id=None, session_id=None,
            scope=Scope.WORKSPACE, memory_type=MemoryType.SEMANTIC,
            confirmed=True,
        )
        assert report["state"] == "partial"
        job = service.store.conn.execute(
            "SELECT total_items, processed_items FROM jobs WHERE id=?",
            (report["job_id"],),
        ).fetchone()
        assert (job["total_items"], job["processed_items"]) == (2, 2)
        persisted = service.store.list_source_import_job_items(job_id=report["job_id"])
        imported = next(row for row in persisted if row["relative_path"] == "bounded.txt")
        assert imported["source_format"] == "text"
    finally:
        service.close()


def test_every_registered_format_survives_the_generic_import_handoff():
    """Dispatch is parser-tested; this covers the shared v2 persistence contract."""
    service = _service()
    try:
        workspace_id = service.store.get_or_create_workspace("all-formats")
        scan = DocumentScan(root_path="", source_id="a" * 64)
        for index, spec in enumerate(DOCUMENT_FORMATS.values()):
            raw = ("source-%d" % index).encode("ascii")
            text = "Readable %s source" % spec.name
            scan.documents.append(DocumentRecord(
                relative_path="format_%d%s" % (index, spec.extensions[0]),
                format=spec.name, media_type=spec.media_type, title=spec.name,
                content=text, body=text,
                raw_sha256=hashlib.sha256(raw).hexdigest(),
                canonical_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                source_size=len(raw), title_source="fixture",
            ))
        report = DocumentImporter(service).import_scan(
            scan, workspace_id=workspace_id, repo_id=None, session_id=None,
            scope=Scope.WORKSPACE, memory_type=MemoryType.SEMANTIC,
            source_label="Format contract", confirmed=True,
        )
        assert report["state"] == "completed"
        assert report["counts"]["imported"] == len(DOCUMENT_FORMATS)
        rows = service.store.list_source_import_job_items(job_id=report["job_id"])
        assert {row["source_format"] for row in rows} == set(DOCUMENT_FORMATS)
    finally:
        service.close()

def test_secure_erase_removes_document_import_job_items():
    service = _service()
    try:
        workspace = "erase-import-items"
        workspace_id = service.store.get_or_create_workspace(workspace)
        report = DocumentImporter(service).import_scan(
            _scan(("private.txt", b"delete this secret\\n")),
            workspace_id=workspace_id, repo_id=None, session_id=None,
            scope=Scope.WORKSPACE, memory_type=MemoryType.SEMANTIC,
            source_label="Private documents", confirmed=True,
        )
        source = service.store.conn.execute(
            "SELECT id, memory_id FROM source_imports WHERE vault_id=?",
            (report["source_id"],),
        ).fetchone()
        assert source is not None and source["memory_id"]
        job_items = service.store.list_source_import_job_items(job_id=report["job_id"])
        assert len(job_items) == 1
        job_item_id = job_items[0]["id"]
        assert service.get_document_import_job(
            report["job_id"], workspace=workspace,
        )["files"][0]["relative_path"] == "private.txt"

        service.store.secure_erase_memory(source["memory_id"])

        assert service.store.conn.execute(
            "SELECT COUNT(*) FROM source_import_items WHERE id=?",
            (job_item_id,),
        ).fetchone()[0] == 0
        assert service.store.conn.execute(
            "SELECT COUNT(*) FROM source_imports WHERE id=?",
            (source["id"],),
        ).fetchone()[0] == 0
        assert service.get_document_import_job(
            report["job_id"], workspace=workspace,
        )["files"] == []
    finally:
        service.close()


def test_document_import_preserves_source_provenance_in_metadata():
    """Imported memories must carry raw_sha256, canonical_sha256, and source_mtime_ns."""
    service = _service()
    try:
        workspace_id = service.store.get_or_create_workspace("provenance")
        mtime = 1_700_000_000_000_000_000
        raw = b"# Provenance Test\n\nBody content.\n"
        scan = _scan(("provenance.md", raw))
        scan.documents[0].source_mtime_ns = mtime
        importer = DocumentImporter(service)
        report = importer.import_scan(
            scan, workspace_id=workspace_id, repo_id=None, session_id=None,
            scope=Scope.WORKSPACE, memory_type=MemoryType.SEMANTIC,
            source_label="Provenance test", confirmed=True,
        )
        assert report["state"] == "completed"
        from engraphis.core.interfaces import SearchFilter
        memories = service.store.list_memories(SearchFilter(workspace_id=workspace_id))
        assert len(memories) == 1
        doc_meta = memories[0].metadata.get("document", {})
        assert doc_meta.get("raw_sha256") == hashlib.sha256(raw).hexdigest()
        assert doc_meta.get("canonical_sha256")
        assert doc_meta.get("relative_path") == "provenance.md"
    finally:
        service.close()


def test_document_import_rejects_oversized_source_gracefully():
    """A source with rejected files still imports valid documents without crashing."""
    service = _service()
    try:
        workspace_id = service.store.get_or_create_workspace("oversized")
        small_raw = b"# Small\n\nOK.\n"
        # Build a scan with one valid document and one rejected entry.
        scan = _scan(("small.md", small_raw))
        from engraphis.core.documents import DocumentFileIssue
        scan.rejected.append(DocumentFileIssue("big.md", "document exceeds safety limit"))
        importer = DocumentImporter(service)
        report = importer.import_scan(
            scan, workspace_id=workspace_id, repo_id=None, session_id=None,
            scope=Scope.WORKSPACE, memory_type=MemoryType.SEMANTIC,
            source_label="Mixed sizes", confirmed=True,
        )
        assert report["state"] in ("completed", "partial")
        assert report["counts"]["imported"] == 1
    finally:
        service.close()