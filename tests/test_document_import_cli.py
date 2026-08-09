"""Universal local-document CLI and service contracts."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from engraphis.service import MemoryService, ValidationError
from engraphis.core.interfaces import Scope
from engraphis.document_import import scan_document_upload
from scripts import importer


def _scan(count: int = 2):
    return SimpleNamespace(
        source_id="a" * 64,
        documents=[object() for _ in range(count)],
        rejected=[],
        skipped=[],
    )


def test_documents_dry_run_dispatches_generic_planner_with_zero_writes(
    monkeypatch, tmp_path, capsys,
):
    root = tmp_path / "Unsorted"
    root.mkdir()
    database = tmp_path / "missing.db"
    scan = _scan()
    captured = {}

    monkeypatch.setattr(importer, "scan_document_tree", lambda path, **kwargs: scan)

    def preview(args, selected_scan, workspace):
        captured.update({"args": args, "scan": selected_scan, "workspace": workspace})
        return {
            "state": "preview",
            "adapter": "documents",
            "counts": {"documents": 2},
            "summary": {"formats": {"markdown": 1, "pdf": 1}},
            "files": [],
        }

    monkeypatch.setattr(importer, "_preview_documents", preview)
    result = importer.main([
        "documents", str(root), "--db", str(database), "--workspace", "acme",
        "--dry-run", "--json",
    ])

    assert result == 0
    assert captured == {"args": captured["args"], "scan": scan, "workspace": "acme"}
    payload = json.loads(capsys.readouterr().out)
    assert payload["adapter"] == "documents"
    assert payload["summary"]["formats"] == {"markdown": 1, "pdf": 1}
    assert not database.exists()


def test_documents_real_dry_run_creates_no_database_or_sidecars(tmp_path, capsys):
    root = tmp_path / "Unsorted"
    root.mkdir()
    (root / "brief.txt").write_text("Local-only brief.", encoding="utf-8")
    database = tmp_path / "absent.db"

    result = importer.main([
        "documents", str(root), "--db", str(database), "--workspace", "acme",
        "--dry-run", "--json",
    ])

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == "preview"
    assert payload["adapter"] == "documents"
    assert payload["counts"]["documents"] == 1
    assert not database.exists()
    assert not Path(str(database) + "-wal").exists()
    assert not Path(str(database) + "-shm").exists()


def test_documents_dry_run_new_workspace_cannot_reuse_foreign_source(
    tmp_path, capsys,
):
    root = tmp_path / "Documents"
    root.mkdir()
    (root / "brief.txt").write_text("Approved local brief.", encoding="utf-8")
    database = tmp_path / "memory.db"
    service = MemoryService.create(
        str(database), embed_dim=64, extractor="none", graph_extractor="none",
        retention_supervisor="none",
    )
    try:
        imported = service.import_document_tree(
            str(root), workspace="alpha", source_label=root.name,
            confirmed=True,
        )
        foreign_source_id = imported["source_id"]
    finally:
        service.close()

    result = importer.main([
        "documents", str(root), "--db", str(database), "--workspace", "beta",
        "--dry-run", "--json",
    ])

    assert result == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["source_id"] is None
    assert preview["vault_id"] is None
    assert preview["counts"]["imported"] == 1
    assert preview["counts"].get("skipped", 0) == 0
    assert foreign_source_id not in str(preview)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM workspaces WHERE name='beta'"
        ).fetchone()[0] == 0


def test_documents_actual_run_uses_generic_service_and_source_names(
    monkeypatch, tmp_path, capsys,
):
    root = tmp_path / "Loose files"
    root.mkdir()
    scan = _scan(count=1)
    calls = {}

    class Service:
        def import_document_tree(self, path, **kwargs):
            calls.update({"path": path, **kwargs})
            kwargs["progress"]({
                "status": "imported", "relative_path": "notes/brief.docx",
            })
            return {
                "state": "completed", "adapter": "documents",
                "counts": {"documents": 1, "imported": 1},
                "summary": {"formats": {"docx": 1}}, "files": [],
            }

        def close(self):
            calls["closed"] = True

    monkeypatch.setattr(importer, "scan_document_tree", lambda path, **kwargs: scan)
    monkeypatch.setattr(importer, "_preview_documents", lambda *args: {
        "state": "preview", "counts": {"documents": 1},
        "summary": {"formats": {"docx": 1}}, "files": [],
    })
    monkeypatch.setattr(importer, "_local_service", lambda path: Service())

    result = importer.main([
        "documents", str(root), "--workspace", "acme", "--repo", "knowledge",
        "--source-id", "vlt_registered", "--source-label", "Company archive",
        "--yes", "--json",
    ])

    assert result == 0
    assert calls["path"] == str(root)
    assert calls["workspace"] == "acme"
    assert calls["repo"] == "knowledge"
    assert calls["source_id"] == "vlt_registered"
    assert calls["source_label"] == "Company archive"
    assert calls["confirmed"] is True
    assert calls["closed"] is True
    assert json.loads(capsys.readouterr().out)["report"]["adapter"] == "documents"


def test_documents_noninteractive_write_requires_confirmation(monkeypatch, tmp_path, capsys):
    root = tmp_path / "docs"
    root.mkdir()
    monkeypatch.setattr(importer, "scan_document_tree", lambda path, **kwargs: _scan())
    monkeypatch.setattr(importer, "_preview_documents", lambda *args: {
        "counts": {"documents": 2}, "summary": {}, "files": [],
    })
    monkeypatch.setattr(importer.sys.stdin, "isatty", lambda: False)

    assert importer.main(["documents", str(root), "--workspace", "acme"]) == 2
    assert "require --yes" in capsys.readouterr().err


def test_generic_upload_validation_accepts_mixed_files_and_rejects_traversal(tmp_path, monkeypatch):
    service = MemoryService.create(
        str(tmp_path / "memory.db"), embed_dim=32, extractor="none",
        graph_extractor="none", retention_supervisor="none",
    )
    try:
        monkeypatch.setattr("engraphis.service.MAX_IMPORT_RESOURCE_BYTES", 10)
        monkeypatch.setattr("engraphis.service.MAX_IMPORT_TOTAL_BYTES", 15)
        uploads, attachments = service._document_upload_inputs(
            [("notes/readme.md", b"# Hello"), ("reports/q1.pdf", b"%PDF")],
            [{"path": "images/chart.png", "size": 10}],
        )
        assert [path for path, _raw in uploads] == [
            "notes/readme.md", "reports/q1.pdf",
        ]
        assert attachments == [{"path": "images/chart.png", "size": 10}]
        with pytest.raises(ValidationError, match="invalid path"):
            service._document_upload_inputs([("../secret.txt", b"x")], [])
        with pytest.raises(ValidationError, match="duplicate path"):
            service._document_upload_inputs(
                [("Notes/Readme.md", b"a"), ("notes/readme.md", b"b")], [],
            )
        with pytest.raises(ValidationError, match="paths overlap"):
            service._document_upload_inputs(
                [("notes/readme.md", b"a")],
                [{"path": "notes/readme.md", "size": 1}],
            )
        with pytest.raises(ValidationError, match="invalid size"):
            service._document_upload_inputs(
                [("notes/readme.md", b"a")],
                [{"path": "assets/big.bin", "size": 11}],
            )
        with pytest.raises(ValidationError, match="too large"):
            service._document_upload_inputs(
                [("notes/big.md", b"x" * 11)], [],
            )
        with pytest.raises(ValidationError, match="total size"):
            service._document_upload_inputs(
                [("notes/one.md", b"x" * 8), ("notes/two.md", b"y" * 8)], [],
            )
    finally:
        service.close()


def test_document_browser_label_identity_is_nfc() -> None:
    files = [("note.txt", b"local note")]
    assert scan_document_upload(files, source_label="Caf\u00e9").source_id == (
        scan_document_upload(files, source_label="Cafe\u0301").source_id
    ) == scan_document_upload(files, source_label="CAF\u00c9").source_id


def test_import_help_presents_documents_as_primary_and_obsidian_as_compatibility():
    help_text = " ".join(importer._parser().format_help().split())
    assert "documents" in help_text
    assert "mixed local folder" in help_text
    assert "obsidian" in help_text
    assert "compatibility command" in help_text


def test_session_id_defaults_to_session_scope_and_repo_must_match_help():
    args = importer._parser().parse_args([
        "documents", "C:/files", "--session", "ses_example",
    ])
    assert importer._scope(args) == Scope.SESSION
    help_text = importer._parser()._subparsers._group_actions[0].choices[
        "documents"
    ].format_help()
    assert "must match --session" in " ".join(help_text.split())


def test_manifest_matching_infers_repo_from_session_when_repo_is_omitted():
    snapshot = {
        "vaults": [{
            "id": "vlt_existing",
            "kind": "documents",
            "root_digest": "d" * 64,
            "workspace_name": "acme",
            "repo_name": "product",
            "session_id": "ses_product",
            "workspace_id": "ws_acme",
            "repo_id": "repo_product",
        }],
    }

    effective_repo = importer._effective_manifest_repo(
        snapshot, repo=None, session_id="ses_product",
    )
    selected, workspace_id, repo_id = importer._snapshot_target(
        snapshot, root_digest="d" * 64, workspace="acme",
        repo=effective_repo, session_id="ses_product", vault_id=None,
        source_kind="documents",
    )

    assert effective_repo == "product"
    assert selected["id"] == "vlt_existing"
    assert (workspace_id, repo_id) == ("ws_acme", "repo_product")


def test_manifest_matching_normalizes_repo_scope_session_target():
    snapshot = {
        "vaults": [
            {
                "id": "vlt_session",
                "kind": "documents",
                "root_digest": "s" * 64,
                "workspace_name": "acme",
                "repo_name": "product",
                "session_id": "ses_product",
            },
            {
                "id": "vlt_repo",
                "kind": "documents",
                "root_digest": "d" * 64,
                "workspace_name": "acme",
                "repo_name": "product",
                "session_id": None,
                "workspace_id": "ws_acme",
                "repo_id": "repo_product",
            },
        ],
    }

    effective_repo, effective_session = importer._effective_manifest_target(
        snapshot, repo=None, session_id="ses_product", scope=Scope.REPO,
    )
    selected, workspace_id, repo_id = importer._snapshot_target(
        snapshot, root_digest="d" * 64, workspace="acme",
        repo=effective_repo, session_id=effective_session, vault_id=None,
        source_kind="documents",
    )

    assert (effective_repo, effective_session) == ("product", None)
    assert selected["id"] == "vlt_repo"
    assert (workspace_id, repo_id) == ("ws_acme", "repo_product")


@pytest.mark.parametrize(
    "extra", [["--repo", "api"], ["--session", "ses_example"]],
)
def test_cli_workspace_scope_rejects_narrower_target_ids(extra):
    args = importer._parser().parse_args([
        "documents", "C:/files", "--scope", "workspace", *extra,
    ])
    with pytest.raises(ValueError, match="requires --repo and --session to be omitted"):
        importer._scope(args)


def test_service_workspace_scope_rejects_session_before_writes():
    service = MemoryService.create(
        ":memory:", embed_dim=64, extractor="none", graph_extractor="none",
        retention_supervisor="none",
    )
    try:
        with pytest.raises(
            ValidationError, match="requires repo and session_id to be omitted",
        ):
            service.preview_document_upload(
                files=[("note.txt", b"Local note")], attachment_manifest=[],
                workspace="must-not-exist", source_label="Loose files",
                session_id="ses_example", scope="workspace",
            )
        assert service._lookup_workspace("must-not-exist") is None
    finally:
        service.close()


def test_universal_service_imports_mixed_tree_idempotently(tmp_path):
    root = tmp_path / "Archive"
    (root / "notes").mkdir(parents=True)
    (root / "notes" / "plan.md").write_text(
        "# Plan\n\nShip the local importer.\n", encoding="utf-8",
    )
    (root / "meeting.txt").write_text(
        "Meeting notes\n\nThe launch is Tuesday.\n", encoding="utf-8",
    )
    (root / "status.json").write_text(
        '{"state": "ready", "owner": "local"}', encoding="utf-8",
    )
    database = tmp_path / "memory.db"
    service = MemoryService.create(
        str(database), embed_dim=64, extractor="none", graph_extractor="none",
        retention_supervisor="none",
    )
    try:
        preview = service.preview_document_tree(str(root), workspace="acme")
        assert preview["adapter"] == "documents"
        assert preview["counts"]["documents"] == 3
        assert preview["summary"]["formats"] == {
            "json": 1, "markdown": 1, "text": 1,
        }

        first = service.import_document_tree(
            str(root), workspace="acme", confirmed=True,
        )
        assert first["state"] == "completed", {
            key: first[key] for key in ("state", "counts", "files")
        }
        assert first["counts"]["imported"] == 3
        second = service.import_document_tree(
            str(root), workspace="acme", source_id=first["source_id"],
            confirmed=True,
        )
        assert second["counts"]["skipped"] == 3
    finally:
        service.close()

    with sqlite3.connect(database) as connection:
        rows = connection.execute("SELECT metadata FROM memories").fetchall()
    metadata = [json.loads(row[0]) for row in rows]
    assert len(metadata) == 3
    assert {item["document"]["format"] for item in metadata} == {
        "json", "markdown", "text",
    }
    assert all("obsidian" not in item for item in metadata)


def test_extensionless_document_links_remain_ambiguous(tmp_path):
    root = tmp_path / "Ambiguous"
    root.mkdir()
    (root / "Source.md").write_text(
        "# Source\n\nSee [[foo]].\n", encoding="utf-8",
    )
    (root / "foo.md").write_text("# Markdown\n", encoding="utf-8")
    (root / "foo.txt").write_text("Plain text\n", encoding="utf-8")
    service = MemoryService.create(
        str(tmp_path / "memory.db"), embed_dim=64, extractor="none",
        graph_extractor="none", retention_supervisor="none",
    )
    try:
        report = service.import_document_tree(
            str(root), workspace="acme", confirmed=True,
        )
        assert report["counts"]["warning"] == 1
        assert any(
            row["reason"] == "ambiguous_wikilink"
            for row in report["files"]
        )
        assert service.store.conn.execute(
            "SELECT COUNT(*) FROM mem_links WHERE valid_to IS NULL"
        ).fetchone()[0] == 0
    finally:
        service.close()


def test_rejected_existing_document_is_not_reported_as_missing(tmp_path):
    root = tmp_path / "Documents"
    root.mkdir()
    path = root / "note.txt"
    path.write_text("Safe local note.", encoding="utf-8")
    service = MemoryService.create(
        str(tmp_path / "memory.db"), embed_dim=64, extractor="none",
        graph_extractor="none", retention_supervisor="none",
    )
    try:
        first = service.import_document_tree(
            str(root), workspace="acme", confirmed=True,
        )
        path.write_text("api_key: very-secret-value", encoding="utf-8")
        report = service.import_document_tree(
            str(root), workspace="acme", source_id=first["source_id"],
            confirmed=True,
        )
        assert report["counts"]["rejected"] == 1
        assert report["counts"].get("missing", 0) == 0
        item = service.store.conn.execute(
            "SELECT state FROM source_imports WHERE relative_path='note.txt'"
        ).fetchone()
        assert item["state"] == "imported"
    finally:
        service.close()


def test_universal_upload_job_reports_adapter_and_registered_source(tmp_path):
    service = MemoryService.create(
        str(tmp_path / "memory.db"), embed_dim=64, extractor="none",
        graph_extractor="none", retention_supervisor="none",
    )
    try:
        with pytest.raises(ValidationError, match="source_label is required"):
            service.import_document_upload(
                files=[("brief.txt", b"Local release brief.")],
                attachment_manifest=[], workspace="must-not-exist", confirmed=True,
            )
        assert service._lookup_workspace("must-not-exist") is None

        started = service.import_document_upload(
            files=[
                ("brief.txt", b"Local release brief."),
                ("notes/context.md", b"# Context\n\nOffline and repeatable."),
            ],
            attachment_manifest=[], workspace="acme",
            source_label="Caf\u00e9 documents", confirmed=True,
        )
        assert started["adapter"] == "documents"
        worker = service._obsidian_job_threads[started["job_id"]]
        worker.join(30)
        assert not worker.is_alive()

        job = service.get_document_import_job(
            started["job_id"], workspace="acme",
        )
        assert job["kind"] == "document_import"
        assert job["state"] == "completed"
        assert job["counts"]["documents"] == 2
        assert job["processed_items"] == job["total_items"] == 2
        assert {row["format"] for row in job["files"]} == {"markdown", "text"}
        assert all("content" not in row for row in job["files"])
        sources = service.list_document_sources("acme")
        assert len(sources) == 1
        assert sources[0]["id"] == started["source_id"]
        assert sources[0]["label"] == "Caf\u00e9 documents"
        assert sources[0]["kind"] == "documents"
        assert sources[0]["adapter"] == "documents"
        with pytest.raises(ValidationError, match="select its source_id"):
            service.preview_document_upload(
                files=[("other.txt", b"A separate collection")],
                attachment_manifest=[], workspace="acme",
                source_label="Cafe\u0301 DOCUMENTS",
            )
        with pytest.raises(ValidationError, match="select its source_id"):
            service.import_document_upload(
                files=[("other.txt", b"A separate collection")],
                attachment_manifest=[], workspace="acme",
                source_label="Cafe\u0301 documents", confirmed=True,
            )
        resumed_preview = service.preview_document_upload(
            files=[("brief.txt", b"Local release brief.")],
            attachment_manifest=[], workspace="acme",
            source_id=started["source_id"],
        )
        assert resumed_preview["source_id"] == started["source_id"]
        assert resumed_preview["source_label"] == "Caf\u00e9 documents"
    finally:
        service.close()
