"""Unified local dashboard tests for the public open-core boundary."""
import ast
import io
import re
import threading
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="full-stack extra not installed")
pytest.importorskip("httpx", reason="httpx not installed")

from fastapi.testclient import TestClient  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from engraphis import __version__, cloud_features  # noqa: E402
from engraphis.config import settings  # noqa: E402
from engraphis.cloud_features import CloudFeatureError  # noqa: E402
from engraphis.core.interfaces import MemoryType, Scope  # noqa: E402
from engraphis.routes import v2_api  # noqa: E402
from engraphis.core.schema import SCHEMA_VERSION  # noqa: E402
from engraphis.service import MemoryService, ValidationError  # noqa: E402


def _client(monkeypatch, tmp_path):
    db_path = str(tmp_path / "dashboard.db")
    monkeypatch.setattr(settings, "db_path", db_path)
    monkeypatch.setattr(settings, "embed_model", "")
    monkeypatch.setattr(settings, "embed_dim", 384)
    monkeypatch.setattr(settings, "allowed_workspaces", [])
    monkeypatch.setattr(settings, "api_token", "")
    seeded = MemoryService.create(db_path)
    demo_id = seeded.store.get_or_create_workspace("demo")
    beta_id = seeded.store.get_or_create_workspace("beta")
    seeded.engine.remember(
        "Postgres 16 is the main database.",
        workspace_id=demo_id,
        scope=Scope.WORKSPACE,
        title="Database",
    )
    seeded.engine.remember(
        "A second workspace must stay isolated.",
        workspace_id=beta_id,
        scope=Scope.WORKSPACE,
        title="Isolation",
    )
    seeded.store.close()
    from engraphis.dashboard_app import create_app
    return TestClient(create_app(), client=("127.0.0.1", 50000))


def test_dashboard_serves_and_bootstraps_local_core(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "<title>Engraphis Ledger</title>" in page.text
        assert "Live memories" in page.text
        assert "All versions, including history" in page.text
        assert "Live rows" not in page.text
        assert 'class="sidebar"' in page.text
        for area in ("Today", "Ask", "Library", "Graph &amp; Relationships", "Provenance", "Manage"):
            assert f">{area}<" in page.text
        assert 'value="matrix">Matrix' in page.text
        assert 'class="dashboard-switcher" aria-label="Dashboard interface"' in page.text
        assert 'id="sidebar-theme-select" aria-label="Dashboard theme"' in page.text
        assert 'value="classic">Classic<' in page.text
        assert 'href="/classic">Classic<' in page.text
        assert 'Ledger (primary)' not in page.text
        assert 'Classic (alternate)' not in page.text
        assert '/v2-assets/vendor/d3.min.js' not in page.text
        assert '/v2-assets/vendor/force-graph.min.js' not in page.text
        assert '/v2-assets/engraphis-graph.js' not in page.text
        classic = client.get("/classic")
        assert classic.status_code == 200
        assert '/classic-assets/dashboard.css' in classic.text
        assert 'class="dashboard-switcher" aria-label="Dashboard interface"' in classic.text
        assert 'href="/"' in classic.text
        assert 'href="/classic" aria-current="page">Classic (alternate)<' in classic.text
        assert 'value="classic" selected>Classic dashboard (alternate)<' in classic.text
        assert 'id="graph-show-iso" data-onchange="h49" checked' in classic.text
        assert client.get("/v2-assets/ledger.css").status_code == 200
        ledger_js = client.get("/v2-assets/ledger.js")
        assert ledger_js.status_code == 200
        assert "'/v2-assets/vendor/d3.min.js?v=20260727-final'" in ledger_js.text
        assert "'/v2-assets/vendor/force-graph.min.js?v=20260727-final'" in ledger_js.text
        assert re.search(
            r"'/v2-assets/engraphis-graph\.js\?v=[A-Za-z0-9._-]+'", ledger_js.text
        )
        assert re.search(r"/v2-assets/ledger\.css\?v=[A-Za-z0-9._-]+", page.text)
        assert re.search(r"/v2-assets/ledger\.js\?v=[A-Za-z0-9._-]+", page.text)
        classic_js = client.get("/classic-assets/dashboard.js")
        assert classic_js.status_code == 200
        assert "/static/vendor/force-graph.min.js?v=20260809-csp" in classic_js.text
        assert re.search(
            r"/v2-assets/engraphis-graph\.js\?v=[A-Za-z0-9._-]+", classic_js.text
        )
        assert "presentation=all" in classic_js.text
        assert "limit=1000&node_limit=1000&edge_limit=2000" in classic_js.text
        assert "renderMode:fullGraph?'all':'overview'" in classic_js.text
        bootstrap = client.get("/api/bootstrap")
        assert bootstrap.status_code == 200
        assert bootstrap.json()["version"] == __version__
        assert bootstrap.json()["stats"]["memories"] >= 1
        savings = client.get("/api/context-savings", params={"workspace": "demo"})
        assert savings.status_code == 200
        assert savings.json()["format"] == "engraphis-context-savings/1"
        global_savings = client.get("/api/context-savings")
        assert global_savings.status_code == 200
        assert global_savings.json()["scope"] == {"workspace": "all"}
        assert global_savings.json()["workspace_count"] == 2
        filtered = client.get(
            "/api/context-savings",
            params={"workspace": "demo", "from_ts": 0, "to_ts": 9_999_999_999,
                    "release_version": "1.5"},
        )
        assert filtered.status_code == 200
        assert filtered.json()["period"] == {"from_ts": 0, "to_ts": 9_999_999_999}
        assert "Estimated context saved" in page.text
        assert 'class="persistent-savings-summary manage-savings-summary" id="context-savings-persistent"' in page.text
        assert 'class="content-section savings-overview-section" id="context-savings-summary"' not in page.text


def test_legacy_workspace_binding_does_not_hide_dashboard_workspaces(monkeypatch, tmp_path):
    """A legacy allow-list setting no longer changes dashboard bootstrap behavior."""
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "bound-empty.db"))
    monkeypatch.setattr(settings, "embed_model", "")
    monkeypatch.setattr(settings, "embed_dim", 384)
    monkeypatch.setattr(settings, "vector_backend", "numpy")
    monkeypatch.setattr(settings, "allowed_workspaces", ["workspace-that-is-gone"])
    monkeypatch.setattr(settings, "api_token", "")
    from engraphis.dashboard_app import create_app

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        response = client.get("/api/bootstrap")

    assert response.status_code == 200
    payload = response.json()
    assert payload["workspaces"] == []
    assert payload["stats"]["workspace"] is None
    assert payload["stats"]["memories"] == 0
    assert payload["stats"]["total_rows"] == 0
    assert payload["stats"]["workspaces"] == 0
    assert payload["stats"]["sessions"] == 0
    assert payload["stats"]["schema_version"] == SCHEMA_VERSION


def test_dashboard_create_workspace_succeeds_when_unbound(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        response = client.post(
            "/api/workspaces/create",
            json={
                "workspace": "fresh-workspace",
                "description": "Created from the dashboard",
                "visibility": "personal",
                "confirmed": False,
            },
        )

    assert response.status_code == 200, response.text
    assert response.json()["workspace"] == "fresh-workspace"
    assert response.json()["created"] is True


def test_dashboard_ignores_legacy_workspace_binding_setting(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "legacy-binding.db"))
    monkeypatch.setattr(settings, "embed_model", "")
    monkeypatch.setattr(settings, "embed_dim", 384)
    monkeypatch.setattr(settings, "allowed_workspaces", ["default"])
    monkeypatch.setattr(settings, "api_token", "")
    from engraphis.dashboard_app import create_app

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        assert client.app.state.service.allowed_workspaces is None
        response = client.post(
            "/api/workspaces/create",
            json={"workspace": "created-after-legacy-binding", "description": ""},
        )

    assert response.status_code == 200, response.text
    assert response.json()["workspace"] == "created-after-legacy-binding"


def test_dashboard_create_workspace_reports_binding_without_leaking_names(
    monkeypatch, tmp_path
):
    with _client(monkeypatch, tmp_path) as client:
        bound = client.app.state.service
        bound.allowed_workspaces = frozenset({"demo"})
        bound.store.allowed_workspaces = bound.allowed_workspaces
        response = client.post(
            "/api/workspaces/create",
            json={"workspace": "new-tenant", "description": ""},
        )

    assert response.status_code == 403
    assert response.json() == {"detail": {
        "error": "workspace is not permitted by this instance's configuration",
        "code": "workspace_not_permitted",
    }}
    assert "new-tenant" not in response.text


def test_dashboard_memory_reads_use_the_active_store_for_memory_databases(monkeypatch):
    monkeypatch.setattr(settings, "db_path", ":memory:")
    monkeypatch.setattr(settings, "embed_model", "")
    monkeypatch.setattr(settings, "embed_dim", 384)
    monkeypatch.setattr(settings, "allowed_workspaces", [])
    monkeypatch.setattr(settings, "api_token", "")
    from engraphis.dashboard_app import create_app

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as client:
        service = client.app.state.service
        workspace_id = service.store.get_or_create_workspace("memory-db")
        service.engine.remember(
            "The dashboard must show this in-memory record.",
            workspace_id=workspace_id,
            scope=Scope.WORKSPACE,
            title="Visible in memory",
        )

        listed = client.get("/api/memories", params={"workspace": "memory-db"})
        assert listed.status_code == 200
        assert listed.json()["count"] == 1
        assert listed.json()["memories"][0]["title"] == "Visible in memory"

        fallback = v2_api._keyword_search("memory-db", "dashboard in-memory")
        assert len(fallback) == 1


def test_dashboard_exposes_accessible_document_import_preview_and_job_contract(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        page = client.get("/")
        assert page.status_code == 200
        for fragment in (
            'id="obsidian-import-button"',
            'id="obsidian-import-dialog"',
            'aria-labelledby="obsidian-import-title"',
            'id="obsidian-import-files"',
            'id="obsidian-import-folder"',
            'webkitdirectory',
            'id="obsidian-source-mode"',
            'id="obsidian-workspace"',
            'id="obsidian-repo"',
            'id="obsidian-session"',
            'id="obsidian-scope"',
            'id="obsidian-memory-type"',
            'id="obsidian-conflict"',
            'id="obsidian-confirmed"',
            'id="obsidian-report-filter"',
            'id="obsidian-cancel"',
        ):
            assert fragment in page.text
        # Folder selection must reveal every local file.  In document mode the
        # browser uploads only supported document bytes; Obsidian mode retains a
        # content-free attachment manifest for link/report accuracy.
        assert 'accept=".md"' not in page.text
        script = client.get("/v2-assets/ledger.js").text
        for endpoint in (
            '/workspaces/import-documents/sources?',
            '/workspaces/import-documents/formats',
            '/workspaces/import-documents/preview',
            '/workspaces/import-documents/run',
            '/workspaces/import-documents/jobs/',
        ):
            assert endpoint in script
        assert "attachment_manifest" in script
        assert "webkitRelativePath" in script
        assert "documentExtensions" in script
        assert "loadDocumentFormats" in script
        assert "applySelectedDocumentSource" in script
        assert "prefillNewSourceLabelFromFolder" in script
        assert "requireNewSourceLabel" in script
        assert "Enter a Source label before creating a new source." in script
        assert "sourceMode === 'obsidian'" in script
        assert "Confirm the selected scope before importing." in script
        assert "review_token" in script
        assert "reviewGeneration" in script
        assert "invalidateDocumentImportPreview" in script
        assert "selection.reviewToken = '';" in script
        assert "if (obsidianImport.running) return;" in script
        assert "pollObsidianImport(jobId, workspace)" in script
        assert "dataset.workspace" in script
        for material_control in (
            "obsidian-import-files", "obsidian-import-folder", "obsidian-workspace",
            "obsidian-repo", "obsidian-session", "obsidian-scope",
            "obsidian-memory-type", "obsidian-vault-label", "obsidian-conflict",
        ):
            assert material_control in script


def test_document_dashboard_endpoints_use_generic_service_and_reject_unknown_binary_uploads(
        monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        monkeypatch.setattr(settings, "api_token", "dashboard-owner-token")
        session = client.post("/api/auth/session", json={"token": "dashboard-owner-token"})
        headers = {
            "X-Engraphis-Browser-Session": "1",
            "X-Engraphis-Review-CSRF": session.json()["review_csrf_token"],
        }
        seen = {}

        # The dashboard's generic bearer/API authority remains insufficient for
        # document bytes, format disclosure, source metadata, or job control.
        denied = client.get(
            "/api/workspaces/import-documents/formats",
            headers={"Authorization": "Bearer dashboard-owner-token"},
        )
        # The client still holds its session cookie, but the bearer cannot supply
        # the required browser-session marker or the per-session CSRF nonce.
        assert denied.status_code == 403

        def preview_document_upload(**kwargs):
            seen["preview"] = kwargs
            return {"counts": {"documents": 1}, "files": [{"path": "notes/readme.txt", "status": "import"}]}

        def import_document_upload(**kwargs):
            seen["run"] = kwargs
            return {"job_id": "job_document", "state": "queued"}

        def preview_obsidian_upload(**kwargs):
            seen["obsidian_preview"] = kwargs
            return {"counts": {"markdown": 1}, "files": []}

        def import_obsidian_upload(**kwargs):
            seen["obsidian_run"] = kwargs
            return {"job_id": "job_obsidian_via_wizard", "state": "queued"}

        monkeypatch.setattr(client.app.state.service, "list_source_vaults", lambda workspace: [{"id": "vlt_1", "label": workspace}], raising=False)
        monkeypatch.setattr(client.app.state.service, "preview_document_upload", preview_document_upload, raising=False)
        monkeypatch.setattr(client.app.state.service, "import_document_upload", import_document_upload, raising=False)
        monkeypatch.setattr(client.app.state.service, "preview_obsidian_upload", preview_obsidian_upload, raising=False)
        monkeypatch.setattr(client.app.state.service, "import_obsidian_upload", import_obsidian_upload, raising=False)
        monkeypatch.setattr(client.app.state.service, "get_document_import_job", lambda job_id, workspace: {"id": job_id, "workspace": workspace, "state": "completed"}, raising=False)
        monkeypatch.setattr(client.app.state.service, "cancel_document_import_job", lambda job_id, workspace: {"id": job_id, "workspace": workspace, "cancel_requested": True}, raising=False)
        payload = {
            "workspace": "demo", "scope": "workspace", "memory_type": "semantic",
            "source_id": "vlt_1", "source_label": "Notes", "on_conflict": "error",
            "source_mode": "documents", "confirmed": "true", "attachment_manifest": "[]",
        }
        upload = [("files", ("notes/readme.txt", b"hello", "text/plain"))]
        missing_label = {**payload, "source_id": "", "source_label": "   "}
        preview_missing_label = client.post(
            "/api/workspaces/import-documents/preview", data=missing_label,
            files=upload, headers=headers,
        )
        assert preview_missing_label.status_code == 400
        assert preview_missing_label.json()["detail"]["error"] == "source label is required for a new source"
        run_missing_label = client.post(
            "/api/workspaces/import-documents/run", data=missing_label,
            files=upload, headers=headers,
        )
        assert run_missing_label.status_code == 400
        sources = client.get("/api/workspaces/import-documents/sources?workspace=demo", headers=headers)
        assert sources.json()["sources"] == [{"id": "vlt_1", "label": "demo"}]
        formats = client.get("/api/workspaces/import-documents/formats", headers=headers)
        assert formats.status_code == 200
        assert ".png" in formats.json()["extensions"]
        preview = client.post("/api/workspaces/import-documents/preview", data=payload, files=upload, headers=headers)
        assert preview.status_code == 200
        review_token = preview.json()["review_token"]
        assert preview.json()["review_expires_in"] == 300
        assert seen["preview"]["source_id"] == "vlt_1"
        assert seen["preview"]["files"] == [("notes/readme.txt", b"hello")]
        saved_source_without_label = client.post(
            "/api/workspaces/import-documents/preview",
            data={**payload, "source_label": ""}, files=upload, headers=headers,
        )
        assert saved_source_without_label.status_code == 200
        image = client.post(
            "/api/workspaces/import-documents/preview", data=payload,
            files=[("files", ("notes/pic.png", b"png", "image/png"))], headers=headers,
        )
        assert image.status_code == 200
        binary = client.post(
            "/api/workspaces/import-documents/preview", data=payload,
            files=[("files", ("notes/archive.bin", b"binary", "application/octet-stream"))],
            headers=headers,
        )
        assert binary.status_code == 400
        assert binary.json()["detail"]["error"] == "unsupported document format"
        attachments = client.post(
            "/api/workspaces/import-documents/preview",
            data={**payload, "attachment_manifest": '[{"path":"notes/pic.png","size":3}]'},
            files=upload, headers=headers,
        )
        assert attachments.status_code == 400
        assert client.post(
            "/api/workspaces/import-documents/run",
            data={**payload, "confirmed": "false", "review_token": review_token},
            files=upload, headers=headers,
        ).status_code == 403
        run_payload = {**payload, "review_token": review_token}
        run = client.post(
            "/api/workspaces/import-documents/run", data=run_payload,
            files=upload, headers=headers,
        )
        assert run.status_code == 200
        assert seen["run"]["confirmed"] is True
        replay = client.post(
            "/api/workspaces/import-documents/run", data=run_payload,
            files=upload, headers=headers,
        )
        assert replay.status_code == 403
        assert replay.json()["detail"]["error"] == "a fresh matching import preview is required"
        assert client.get("/api/workspaces/import-documents/jobs/job_document?workspace=demo", headers=headers).status_code == 200
        assert client.post(
            "/api/workspaces/import-documents/jobs/job_document/cancel",
            data={"workspace": "demo"}, headers=headers,
        ).json()["cancel_requested"] is True

        # The source-neutral wizard keeps an explicit Obsidian mode. It must
        # call the compatibility adapter so an ``obsidian`` vlt_ identity and
        # its rich Markdown lineage remain resumable instead of being treated
        # as a generic ``documents`` source.
        obsidian_payload = {
            **payload, "source_id": "vlt_obsidian", "source_label": "Vault",
            "source_mode": "obsidian",
        }
        markdown = [("files", ("notes/readme.md", b"# Note\n", "text/markdown"))]
        obsidian_preview = client.post(
            "/api/workspaces/import-documents/preview", data=obsidian_payload,
            files=markdown, headers=headers,
        )
        assert obsidian_preview.status_code == 200
        assert seen["obsidian_preview"]["vault_id"] == "vlt_obsidian"
        obsidian_run = client.post(
            "/api/workspaces/import-documents/run",
            data={**obsidian_payload, "review_token": obsidian_preview.json()["review_token"]},
            files=markdown, headers=headers,
        )
        assert obsidian_run.status_code == 200
        assert seen["obsidian_run"]["vault_id"] == "vlt_obsidian"


def test_document_import_review_binds_owner_target_policy_manifest_and_exact_bytes(
        monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        monkeypatch.setattr(settings, "api_token", "dashboard-owner-token")
        session = client.post("/api/auth/session", json={"token": "dashboard-owner-token"})
        headers = {
            "X-Engraphis-Browser-Session": "1",
            "X-Engraphis-Review-CSRF": session.json()["review_csrf_token"],
        }
        imported = []

        monkeypatch.setattr(
            client.app.state.service,
            "preview_document_upload",
            lambda **kwargs: {"counts": {"documents": len(kwargs["files"])}},
            raising=False,
        )
        monkeypatch.setattr(
            client.app.state.service,
            "preview_obsidian_upload",
            lambda **kwargs: {"counts": {"markdown": len(kwargs["files"])}},
            raising=False,
        )

        def import_document_upload(**kwargs):
            imported.append(kwargs)
            return {"job_id": "job_bound", "state": "queued"}

        monkeypatch.setattr(
            client.app.state.service,
            "import_document_upload",
            import_document_upload,
            raising=False,
        )
        base = {
            "workspace": "demo", "repo": "repo-a", "session_id": "session-a",
            "scope": "workspace", "memory_type": "semantic",
            "source_id": "vlt_1", "source_label": "Notes",
            "on_conflict": "error", "source_mode": "documents",
            "confirmed": "true", "attachment_manifest": "[]",
        }
        upload = [("files", ("notes/Welcome.md", b"# Welcome", "text/markdown"))]

        def preview_token(data=None, files=None):
            response = client.post(
                "/api/workspaces/import-documents/preview",
                data=data or base, files=files or upload, headers=headers,
            )
            assert response.status_code == 200
            return response.json()["review_token"]

        token = preview_token()
        missing = client.post(
            "/api/workspaces/import-documents/run", data=base,
            files=upload, headers=headers,
        )
        assert missing.status_code == 403
        assert missing.json()["detail"]["error"] == (
            "a fresh matching import preview is required"
        )

        mutations = {
            "workspace": "beta",
            "repo": "repo-b",
            "session_id": "session-b",
            "scope": "repo",
            "memory_type": "episodic",
            "source_id": "vlt_2",
            "source_label": "Other notes",
            "on_conflict": "update",
            "source_mode": "obsidian",
        }
        for field, changed_value in mutations.items():
            token = preview_token()
            changed = {**base, field: changed_value, "review_token": token}
            rejected = client.post(
                "/api/workspaces/import-documents/run", data=changed,
                files=upload, headers=headers,
            )
            assert rejected.status_code == 403, field
            assert rejected.json()["detail"]["error"] == (
                "a fresh matching import preview is required"
            )

        for changed_upload in (
            [("files", ("notes/Renamed.md", b"# Welcome", "text/markdown"))],
            [("files", ("notes/Welcome.md", b"# Changed", "text/markdown"))],
        ):
            token = preview_token()
            rejected = client.post(
                "/api/workspaces/import-documents/run",
                data={**base, "review_token": token},
                files=changed_upload, headers=headers,
            )
            assert rejected.status_code == 403

        obsidian = {
            **base,
            "source_mode": "obsidian",
            "attachment_manifest": '[{"path":"assets/pic.png","size":12}]',
        }
        token = preview_token(data=obsidian)
        changed_manifest = {
            **obsidian,
            "attachment_manifest": '[{"path":"assets/pic.png","size":13}]',
            "review_token": token,
        }
        assert client.post(
            "/api/workspaces/import-documents/run", data=changed_manifest,
            files=upload, headers=headers,
        ).status_code == 403

        token = preview_token()
        with client.app.state.document_import_review_lock:
            client.app.state.document_import_reviews[token]["expires_at"] = 0
        expired = client.post(
            "/api/workspaces/import-documents/run",
            data={**base, "review_token": token}, files=upload, headers=headers,
        )
        assert expired.status_code == 403

        token = preview_token()
        replacement_session = client.post(
            "/api/auth/session", json={"token": "dashboard-owner-token"},
        )
        replacement_headers = {
            "X-Engraphis-Browser-Session": "1",
            "X-Engraphis-Review-CSRF": replacement_session.json()["review_csrf_token"],
        }
        rebound = client.post(
            "/api/workspaces/import-documents/run",
            data={**base, "review_token": token}, files=upload,
            headers=replacement_headers,
        )
        assert rebound.status_code == 403
        assert imported == []


def test_obsidian_dashboard_endpoints_require_browser_owner_csrf_and_preserve_relative_paths(
        monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        monkeypatch.setattr(settings, "api_token", "dashboard-owner-token")
        payload = {
            "workspace": "demo", "scope": "workspace", "memory_type": "semantic",
            "vault_id": "", "vault_label": "Notes", "on_conflict": "error",
            "confirmed": "true", "attachment_manifest": '[{"path":"assets/pic.png","size":12}]',
        }
        upload = [("files", ("notes/Welcome.md", b"# Welcome", "text/markdown"))]
        # The generic bearer passes the outer API gate, but vault imports themselves are
        # intentionally owner-browser-only.
        denied = client.post(
            "/api/workspaces/import-obsidian/preview", data=payload, files=upload,
            headers={"Authorization": "Bearer dashboard-owner-token"},
        )
        assert denied.status_code == 401

        session = client.post("/api/auth/session", json={"token": "dashboard-owner-token"})
        assert session.status_code == 200
        headers = {
            "X-Engraphis-Browser-Session": "1",
            "X-Engraphis-Review-CSRF": session.json()["review_csrf_token"],
        }
        seen = {}

        def preview_obsidian_upload(**kwargs):
            seen["preview"] = kwargs
            return {"counts": {"markdown_files": 1}, "files": [{"path": "notes/Welcome.md", "status": "import"}]}

        def import_obsidian_upload(**kwargs):
            seen["run"] = kwargs
            return {"job_id": "job_obsidian", "state": "queued"}

        monkeypatch.setattr(client.app.state.service, "list_obsidian_vaults", lambda workspace: [{"id": "v_1", "label": workspace}], raising=False)
        monkeypatch.setattr(client.app.state.service, "preview_obsidian_upload", preview_obsidian_upload, raising=False)
        monkeypatch.setattr(client.app.state.service, "import_obsidian_upload", import_obsidian_upload, raising=False)
        monkeypatch.setattr(client.app.state.service, "get_obsidian_import_job", lambda job_id, workspace: {"id": job_id, "workspace": workspace, "state": "completed"}, raising=False)
        monkeypatch.setattr(client.app.state.service, "cancel_obsidian_import_job", lambda job_id, workspace: {"id": job_id, "workspace": workspace, "cancel_requested": True}, raising=False)

        missing_label = client.post(
            "/api/workspaces/import-obsidian/preview",
            data={**payload, "vault_label": "\t"}, files=upload, headers=headers,
        )
        assert missing_label.status_code == 400
        assert missing_label.json()["detail"]["error"] == "source label is required for a new source"

        vaults = client.get("/api/workspaces/import-obsidian/vaults?workspace=demo", headers=headers)
        assert vaults.status_code == 200
        assert vaults.json()["vaults"] == [{"id": "v_1", "label": "demo"}]
        preview = client.post("/api/workspaces/import-obsidian/preview", data=payload, files=upload, headers=headers)
        assert preview.status_code == 200
        review_token = preview.json()["review_token"]
        assert seen["preview"]["files"] == [("notes/Welcome.md", b"# Welcome")]
        assert seen["preview"]["attachment_manifest"] == [{"path": "assets/pic.png", "size": 12}]

        duplicate = client.post(
            "/api/workspaces/import-obsidian/preview", data=payload,
            files=[
                ("files", ("notes/Dupe.md", b"one", "text/markdown")),
                ("files", ("NOTES/DUPE.md", b"two", "text/markdown")),
            ], headers=headers,
        )
        assert duplicate.status_code == 400
        assert duplicate.json()["detail"]["error"] == "duplicate upload path"
        for unsafe in ("C:/vault/Note.md", "//server/share/Note.md"):
            rejected = client.post(
                "/api/workspaces/import-obsidian/preview", data=payload,
                files=[("files", (unsafe, b"unsafe", "text/markdown"))],
                headers=headers,
            )
            assert rejected.status_code == 400
        overlap_payload = {
            **payload,
            "attachment_manifest": '[{"path":"NOTES/welcome.MD","size":12}]',
        }
        overlap = client.post(
            "/api/workspaces/import-obsidian/preview", data=overlap_payload,
            files=upload, headers=headers,
        )
        assert overlap.status_code == 400
        assert overlap.json()["detail"]["error"] == "upload and attachment paths overlap"

        markdown_alias = client.post(
            "/api/workspaces/import-obsidian/preview", data=payload,
            files=[("files", ("notes/Legacy.markdown", b"legacy", "text/markdown"))],
            headers=headers,
        )
        assert markdown_alias.status_code == 400
        assert markdown_alias.json()["detail"]["error"] == (
            "Obsidian mode accepts Markdown note bytes only"
        )

        payload["confirmed"] = "false"
        assert client.post(
            "/api/workspaces/import-obsidian/run",
            data={**payload, "review_token": review_token}, files=upload, headers=headers,
        ).status_code == 403
        payload["confirmed"] = "true"
        run_payload = {**payload, "review_token": review_token}
        run = client.post(
            "/api/workspaces/import-obsidian/run", data=run_payload,
            files=upload, headers=headers,
        )
        assert run.status_code == 200
        assert seen["run"]["confirmed"] is True
        assert client.post(
            "/api/workspaces/import-obsidian/run", data=run_payload,
            files=upload, headers=headers,
        ).status_code == 403
        status = client.get("/api/workspaces/import-obsidian/jobs/job_obsidian?workspace=demo", headers=headers)
        assert status.status_code == 200
        assert status.json()["state"] == "completed"
        cancelled = client.post(
            "/api/workspaces/import-obsidian/jobs/job_obsidian/cancel",
            data={"workspace": "demo"}, headers=headers,
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["cancel_requested"] is True


def test_dashboard_assets_revalidate_instead_of_pinning_old_visuals(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        for path in (
            "/v2-assets/engraphis-graph.js?v=20260809-pin-only-physics",
            "/v2-assets/ledger.js?v=20260728-connected-memories",
            "/v2-assets/ledger.css?v=20260728-connected-memories",
            "/classic-assets/dashboard.js?v=20260809-pin-only-physics",
        ):
            response = client.get(path)
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-cache, must-revalidate"


def test_classic_dashboard_script_mirrors_the_static_compatibility_asset():
    root = Path(__file__).parents[1] / "engraphis"
    assert (root / "classic_assets" / "dashboard.js").read_bytes() == (
        root / "static" / "dashboard.js"
    ).read_bytes()


def test_dashboard_and_mcp_recall_share_the_v2_service(monkeypatch, tmp_path):
    pytest.importorskip("mcp", reason="MCP extra not installed")
    import json

    from engraphis import mcp_server

    with _client(monkeypatch, tmp_path) as client:
        assert mcp_server.service() is client.app.state.service
        response = client.get(
            "/api/recall",
            params={"q": "which database do we use", "workspace": "demo", "k": 3},
        )
        assert response.status_code == 200
        dashboard = response.json()
        mcp = json.loads(mcp_server.engraphis_recall(
            query="which database do we use", workspace="demo", k=3,
        ))
        assert [memory["id"] for memory in dashboard["memories"]] == [
            memory["id"] for memory in mcp["memories"]
        ]
        assert [memory["retention"] for memory in dashboard["memories"]] == [
            memory["retention"] for memory in mcp["memories"]
        ]
        assert [memory["relative_score"] for memory in dashboard["memories"]] == [
            memory["relative_score"] for memory in mcp["memories"]
        ]
        assert [memory["absolute_support"] for memory in dashboard["memories"]] == [
            memory["absolute_support"] for memory in mcp["memories"]
        ]
        assert dashboard["score_semantics"] == mcp["score_semantics"]


def test_dashboard_keyword_fallback_reports_truthful_lexical_scores(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        def mismatched_embedder(*_args, **_kwargs):
            raise ValueError("shapes (1,256) and (384,1) not aligned")

        monkeypatch.setattr(client.app.state.service, "recall", mismatched_embedder)
        response = client.get(
            "/api/recall",
            params={
                "q": "which database do we use",
                "workspace": "demo",
                "k": 3,
                "response_mode": "compact",
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["mode"] == "keyword"
        assert "lexical Jaccard" in payload["score_semantics"]["relative_score"]
        assert "Semantic support is unavailable" in (
            payload["score_semantics"]["absolute_support"]
        )
        memory = payload["memories"][0]
        assert memory["score"] == memory["relative_score"] == 1.0
        assert 0.0 < memory["absolute_support"] < 1.0
        assert memory["arm"] == "lexical"
        assert "content" not in memory


def test_dashboard_keyword_fallback_applies_requested_memory_type_limits(
    monkeypatch, tmp_path
):
    with _client(monkeypatch, tmp_path) as client:
        workspace_id = client.app.state.service.store.get_or_create_workspace("demo")
        client.app.state.service.engine.remember(
            "Database upgrade procedure requires a verified backup.",
            workspace_id=workspace_id,
            scope=Scope.WORKSPACE,
            mtype=MemoryType.PROCEDURAL,
            title="Database procedure",
        )

        def mismatched_embedder(*_args, **_kwargs):
            raise ValueError("shapes (1,256) and (384,1) not aligned")

        monkeypatch.setattr(client.app.state.service, "recall", mismatched_embedder)
        response = client.get(
            "/api/recall",
            params={
                "q": "database",
                "workspace": "demo",
                "k": 3,
                "mtype_limits": '{"semantic":0,"procedural":1}',
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["mtype_limits"] == {"semantic": 0, "procedural": 1}
        assert [memory["memory_type"] for memory in payload["memories"]] == [
            "procedural"
        ]


@pytest.mark.parametrize("invalid_limit", [True, "2"])
def test_dashboard_post_recall_surfaces_reject_coerced_memory_type_limits(
    monkeypatch, tmp_path, invalid_limit
):
    with _client(monkeypatch, tmp_path) as client:
        response = client.post(
            "/api/intent/recall",
            json={"query": "database", "mtype_limits": {"semantic": invalid_limit}},
        )

        assert response.status_code == 422


def test_dashboard_serves_the_graph_engine_from_its_v2_asset_surface(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        asset = client.get("/v2-assets/engraphis-graph.js")
        assert asset.status_code == 200
        assert "window.EngraphisGraph =" in asset.text
        compat = client.get("/v2-assets/engraphis-graph-compat.js")
        assert compat.status_code == 200
        assert "window.EngraphisGraphCompat =" in compat.text
        assert client.get("/v2-assets/vendor/d3.min.js").status_code == 200
        assert client.get("/v2-assets/vendor/force-graph.min.js").status_code == 200


def test_graph_load_is_bounded_single_flight_and_retryable(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        page = client.get("/")
        script = client.get("/v2-assets/ledger.js")
        assert 'id="graph-retry"' in page.text
        assert 'id="graph-full"' not in page.text
        assert 'id="graph-show-all"' not in page.text
        assert 'data-graph-preset-choice="every"' in page.text
        assert 'id="graph-show-unlinked"' in page.text
        assert 'id="graph-show-unlinked" class="graph-action" type="button" aria-pressed="true"' in page.text
        assert 'id="graph-unlinked"' not in page.text
        assert 'id="graph-tune-unlinked"' not in page.text
        assert 'id="graph-style" type="hidden" value="cyber"' in page.text
        assert "const GRAPH_INITIAL_NODE_LIMIT = 1000;" in script.text
        assert "const GRAPH_INITIAL_EDGE_LIMIT = 2000;" in script.text
        assert "const GRAPH_ALL_NODE_LIMIT = 20_000;" in script.text
        assert "const GRAPH_LOAD_TIMEOUT_MS = 12_000;" in script.text
        assert "AbortController" in script.text
        assert "state.graphLoadPromise" in script.text
        assert "graphLoadRepo: ''" in script.text
        assert "function graphLoadKey(" in script.text
        assert "function isCurrentGraphLoad(request)" in script.text
        assert "request.repo === (byId('graph-repo-filter').value || '').trim()" in script.text
        assert "Filter by exact repository name…" in script.text
        assert "candidate && !validatedGraphRepository(candidate)" in script.text
        assert "function retryGraphLoad()" in script.text
        assert "function releaseGraphAssetsAttempt(attempt)" in script.text
        assert "function ensureGraphAllAsset()" in script.text
        assert "function releaseGraphAllAssetsAttempt(attempt)" in script.text
        assert "const assets = ensureGraphAssets(fullGraph);" in script.text
        assert "if (!force && state.graphLoadPromise && state.graphLoadKey === key)" in script.text
        assert "previousController.abort()" in script.text
        assert "Promise.race([" in script.text
        assert "timeoutPromise" in script.text
        assert "/graph/scene?" in script.text
        assert "&level=${level}" in script.text
        assert "&include_memory_nodes=false" in script.text
        assert "&presentation=all" in script.text
        assert "renderMode: galaxyQuality ? 'full' : fullGraph ? 'all' : 'overview'" in script.text
        assert "&include_history=true" in script.text
        assert "&connected_only=true" in script.text
        assert "const repo = (byId('graph-repo-filter').value || '').trim();" in script.text
        assert "repo ? `&repo=${encodeURIComponent(repo)}`" in script.text
        assert "item.degree != null ? item.degree : item.weighted_degree" in script.text
        assert "style: 'cyber'" in script.text
        assert "renderMode: galaxyQuality ? 'full' : fullGraph ? 'all' : 'overview'" in script.text
        assert "loadGraph({ force: true })" in script.text
        assert "if ((!fullGraph || galaxyQuality) && window.EngraphisSpacetime" in script.text
        assert "setAttribute('aria-busy', 'true')" in script.text
        assert "setAttribute('aria-busy', 'false')" in script.text


def test_graph_motion_saved_views_and_tuning_controls_are_wired(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        page = client.get("/")
        script = client.get("/v2-assets/ledger.js")
        for control in (
            'id="graph-flow-speed"', 'data-graph-saved-view="operations"',
            'data-graph-saved-view="schema"', 'data-graph-saved-view="people"',
            'data-graph-saved-view="code"', 'id="graph-save-view"',
            'id="graph-repel"', 'id="graph-depth"', 'id="graph-reset-tuning"',
            'id="graph-gravity" type="range" min="0" max="400"',
            'data-graph-layer="code"',
        ):
            assert control in page.text
        for behavior in (
            "function applyGraphView(id)", "function resetGraphTuning()",
            "function saveCurrentGraphView()", "function graphTuningSettings()",
            "&include_code=true", "graph.setLayers(graphLayerState())",
            "setSettings({ flowSpeed: speed })",
        ):
            assert behavior in script.text


def test_code_overlay_scopes_only_to_known_repositories(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        script = client.get("/v2-assets/ledger.js")
        assert script.status_code == 200
        assert "function graphRepositoryNames()" in script.text
        assert "function validatedGraphRepository(value)" in script.text
        assert "workspace && Array.isArray(workspace.repos)" in script.text
        assert "repositories: Array.isArray(scene.repos)" in script.text
        assert "const validatedRepo = targetIncludeCode || fullGraph" in script.text
        assert "const scopedRepo = validatedRepo" in script.text
        assert "targetIncludeCode && targetRepo" not in script.text


def test_all_nodes_mode_preserves_scope_preferences_and_bounds_heavy_work(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        script = client.get("/v2-assets/ledger.js")
        assert script.status_code == 200
        assert "graphScopeBeforeAll: null" not in script.text
        assert "showUnlinked: state.graphShowUnlinked" in script.text
        assert "includeCode: state.graphIncludeCode" in script.text
        assert "minDegree: number(byId('graph-min-degree').value)" in script.text
        assert "if (loadAll && !graphIsGalaxy()) return ensureGraphAllAsset();" in script.text
        assert "const graphFactory = galaxyQuality ? window.EngraphisGraph" in script.text
        assert "scopeControl.disabled = full" not in script.text
        assert "graph.setCollapse(byId('graph-collapse').checked ? 'auto' : false)" in script.text
        assert "const includeCode = targetIncludeCode ? '&include_code=true' : '';" in script.text
        assert "function scheduleGraphRepositoryReload()" in script.text
        assert "}, 250);" in script.text
        assert "const indentation = state.graphMode === 'full' ? undefined : 2;" in script.text
        assert "error.code === 'GRAPH_CAPACITY'" in script.text
        assert "state.graphLoadRequest !== request.id" in script.text


def test_graph_palette_notice_auto_dismisses_after_three_seconds(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        script = client.get("/v2-assets/ledger.js")
        assert script.status_code == 200
        assert "const NOTICE_DURATION_MS = 3000;" in script.text
        assert "clearTimeout(noticeTimer);" in script.text
        assert "noticeTimer = setTimeout(" in script.text
        assert "banner.hidden = true;" in script.text


def test_graph_palette_recolors_every_colour_mode(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        engine = client.get("/v2-assets/engraphis-graph.js")
        ledger = client.get("/v2-assets/ledger.js")
        assert engine.status_code == 200
        assert "function selectedPalette()" in engine.text
        assert "function commPal() {" in engine.text
        assert "return selectedPalette() ||" in engine.text
        assert "const colors = selectedPalette() || GRAPH_HEAT;" in engine.text
        # Palettes still recolor every identity mode, but material families stay stable:
        # semantic color belongs to the slim identity ring rather than rotating the whole
        # Cyber film into arbitrary green/yellow alloys.
        assert "function iridescentTint(c)" not in engine.text
        assert "fixedPalette" in engine.text
        assert "function identityRing(" in engine.text
        assert "identity: rgbString(identity)" in engine.text
        assert "function graphThemeColors()" in ledger.text
        assert "graph.setThemeColors(graphThemeColors());" in ledger.text
        assert "state.graphEngine.setThemeColors(graphThemeColors());" in ledger.text
        assert "renderMode: opts.renderMode === 'full' ? 'full' : 'overview'" in engine.text
        assert "function pinFullGraphLayout(data)" in engine.text


def test_graph_facts_and_search_use_the_atomic_node_reveal(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        page = client.get("/")
        ledger = client.get("/v2-assets/ledger.js")
        engine = client.get("/v2-assets/engraphis-graph.js")
        assert 'id="graph-connections-dialog"' in page.text
        assert "function revealGraphNode(id, label = 'Selected entity')" in ledger.text
        assert "revealGraphNode(item.id, item.name)" in ledger.text
        assert "function openGraphConnections(item)" in ledger.text
        assert "function showGraphConnectionMemories(item, includeHistory = false)" in ledger.text
        assert "onNodeClick: item => openGraphConnections(item)" in ledger.text
        assert "api.reveal = id =>" in engine.text
        assert "function centerRenderedNode(id)" in engine.text
        assert "suppressNodeClickAfterDrag" in engine.text
        assert "render(true, true);" not in engine.text[engine.text.index("api.focus = id =>"):engine.text.index("api.clearFocus")]


def test_library_editor_stacks_directly_below_the_selected_memory_panel(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert '<div class="library-detail-stack">' in page.text
        assert page.text.index('id="memory-detail"') < page.text.index('id="memory-editor"')
        stylesheet = client.get("/v2-assets/ledger.css")
        assert ".library-detail-stack { display: grid; gap: 12px; align-content: start; }" in stylesheet.text


def test_workspace_switcher_uses_the_active_ledger_theme_for_native_dropdowns(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        stylesheet = client.get("/v2-assets/ledger.css")
        assert stylesheet.status_code == 200
        css = stylesheet.text
        assert ".workspace-switcher select {" in css
        assert "background: var(--c-inset);" in css
        assert "color-scheme: dark;" in css
        assert 'body[data-theme="paper"] .workspace-switcher select { color-scheme: light; }' in css
        assert ".workspace-switcher select option { background: var(--c-inset); color: var(--c-fg); }" in css
        assert ".workspace-switcher select option:checked { background: var(--c-acc); color: var(--c-bg); }" in css


def test_sidebar_keeps_manage_and_compare_plans_in_separate_flex_rows(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        stylesheet = client.get("/v2-assets/ledger.css")
        assert stylesheet.status_code == 200
        css = stylesheet.text
        sidebar = css[css.index(".sidebar {"):css.index(".brand-row {")]
        assert "display: flex;" in sidebar
        assert "flex-direction: column;" in sidebar
        assert "grid-template-rows" not in sidebar
        assert ".primary-nav { flex: 1 0 auto; }" in css
        assert ".manage-nav { flex: 0 0 auto; }" in css
        assert ".sidebar-promo {\n  flex: 0 0 auto;" in css


def test_dashboard_grounded_answer_route_cites_or_abstains(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        grounded = client.post(
            "/api/answer",
            json={
                "query": "Which database is the main database?",
                "workspace": "demo",
                "k": 8,
                "max_citations": 5,
                "candidate_depth": "adaptive",
            },
        )
        assert grounded.status_code == 200
        body = grounded.json()
        assert body["query"] == "Which database is the main database?"
        assert body["grounded"] is True
        assert body["abstained"] is False
        assert body["citations"]
        assert body["sources"] == body["citations"]
        assert "[1]" in body["answer"]
        assert body["candidate_depth"] == "adaptive"
        # ``candidate_k_used`` is the final page depth after prompt-safe
        # overfetch/widening, rather than the adaptive policy's starting depth.
        assert body["candidate_k_used"] >= body["candidate_k_requested"]

        abstained = client.post(
            "/api/answer",
            json={
                "query": "How should I bake a sourdough loaf?",
                "workspace": "demo",
            },
        )
        assert abstained.status_code == 200
        assert abstained.json()["grounded"] is False
        assert abstained.json()["abstained"] is True


def test_dashboard_grounded_answer_route_bounds_and_redacts(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        assert client.post("/api/answer", json={"query": "", "workspace": "demo"}).status_code == 422
        assert client.post(
            "/api/answer",
            json={"query": "database", "workspace": "demo", "k": 51},
        ).status_code == 422


def test_team_account_routes_are_not_in_public_runtime(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        assert client.post("/api/auth/setup", json={}).status_code == 404
        assert client.get("/api/auth/users").status_code == 404
        state = client.get("/api/auth/state").json()
        assert state["enabled"] is False
        assert state["deployment_mode"] == "local"
        assert state["hosted_team"] is False
        assert state["cloud_url"] == ""
        assert state["local_invitations"] is True


def test_local_agent_write_has_no_client_side_team_paywall(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        response = client.post(
            "/api/remember",
            json={"workspace": "demo", "content": "Queues use at-least-once delivery."},
        )
        assert response.status_code == 200


def test_http_memory_api_exposes_world_timed_agent_writes_immediately(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        old = client.post(
            "/api/remember",
            json={
                "workspace": "demo",
                    "content": "The API rate limit is 100 requests per minute.",
                    "valid_from": 1_000.0,
                    "subject_key": "api.rate_limit",
                    "claim_kind": "configured_value",
            },
        ).json()
        new = client.post(
            "/api/intent/remember",
            json={
                "workspace": "demo",
                    "text": "The API rate limit is 500 requests per minute.",
                    "valid_from": 2_000.0,
                    "subject_key": "api.rate_limit",
                    "claim_kind": "configured_value",
            },
        ).json()

        before = client.get(
            "/api/recall",
            params={
                "workspace": "demo",
                "q": "What is the API rate limit?",
                "as_of": 1_500.0,
            },
        )
        after = client.post(
            "/api/answer",
            json={
                "workspace": "demo",
                "query": "What is the API rate limit?",
                "as_of": 2_500.0,
                "min_support": 0.0,
            },
        )

        assert before.status_code == 200
        assert [memory["id"] for memory in before.json()["memories"]] == [old["id"]]
        assert after.status_code == 200
        assert after.json()["sources"]
        service = client.app.state.service
        assert service.store.get_memory(old["id"]).valid_from == 1_000.0
        assert service.store.get_memory(new["id"]).valid_from == 2_000.0
        assert service.store.get_memory(old["id"]).provenance["review_state"] == "approved"
        assert service.store.get_memory(new["id"]).provenance["review_state"] == "approved"


def test_keyword_recall_fallback_keeps_bitemporal_visibility(monkeypatch, tmp_path):
    """A semantic-backend failure must not leak current facts into historical views."""
    with _client(monkeypatch, tmp_path) as client:
        svc = v2_api.service()
        workspace_id = svc.store.get_or_create_workspace("demo")
        old = {"id": svc.engine.remember(
            "The fallback retention setting was ten days.", workspace_id=workspace_id,
            scope=Scope.WORKSPACE, valid_from=1_000.0, resolve_conflicts=False,
        )}
        new = {"id": svc.engine.remember(
            "The fallback retention setting was thirty days.", workspace_id=workspace_id,
            scope=Scope.WORKSPACE, valid_from=2_000.0, resolve_conflicts=False,
        )}
        # The writes happened during this test, but the fixture models facts learned
        # before the requested historical system-time anchors.
        svc.store.conn.execute(
            "UPDATE memories SET ingested_at=100 WHERE id=?", (old["id"],)
        )
        svc.store.conn.execute(
            "UPDATE memories SET ingested_at=200 WHERE id=?", (new["id"],)
        )
        svc.store.conn.execute(
            "UPDATE memories SET valid_to=2000, valid_to_recorded_at=200, "
            "subject_key='retention.days', claim_kind='configured_value' "
            "WHERE id=?",
            (old["id"],),
        )
        svc.store.conn.commit()
        old_before = v2_api._keyword_search(
            "demo", "fallback retention", valid_at=1_500.0, known_at=3_000.0
        )
        old_known = v2_api._keyword_search(
            "demo", "fallback retention", valid_at=1_500.0, known_at=50.0
        )
        current = v2_api._keyword_search(
            "demo", "fallback retention", valid_at=2_500.0, known_at=3_000.0
        )
        closure_unknown = v2_api._keyword_search(
            "demo", "fallback retention", valid_at=2_500.0, known_at=150.0
        )

        assert [memory["id"] for memory in old_before] == [old["id"]]
        assert old_known == []
        assert [memory["id"] for memory in current] == [new["id"]]
        assert [memory["id"] for memory in closure_unknown] == [old["id"]]
        assert closure_unknown[0]["valid_to_recorded_at"] == 200.0
        assert closure_unknown[0]["subject_key"] == "retention.days"
        assert closure_unknown[0]["claim_kind"] == "configured_value"

        def incompatible_embedder(*_args, **_kwargs):
            raise ValueError("shapes (256,) and (384,) not aligned")

        monkeypatch.setattr(svc, "recall", incompatible_embedder)
        fallback = client.get(
            "/api/recall",
            params={
                "workspace": "demo", "q": "fallback retention",
                "valid_at": 2_500.0, "known_at": 150.0,
            },
        )
        assert fallback.status_code == 200
        assert fallback.json()["mode"] == "keyword"
        assert [item["id"] for item in fallback.json()["memories"]] == [old["id"]]

        compact_fallback = client.get(
            "/api/recall",
            params={
                "workspace": "demo", "q": "fallback retention", "response_mode": "compact",
                "token_budget": 0,
            },
        )
        payload = compact_fallback.json()
        assert compact_fallback.status_code == 200
        assert payload["mode"] == "keyword"
        assert payload["response_mode"] == "compact"
        assert payload["usage"]["budget_tokens"] == 0
        assert payload["usage"]["context_tokens"] == 0
        assert payload["memories"] and "content" not in payload["memories"][0]


def test_keyword_recall_fallback_excludes_untrusted_memories(monkeypatch, tmp_path):
    """A degraded HTTP recall must enforce the same prompt eligibility boundary."""
    with _client(monkeypatch, tmp_path) as client:
        svc = v2_api.service()
        workspace_id = svc.store.get_or_create_workspace("demo")
        trusted = {"id": svc.engine.remember(
            "Fallback visibility trusted candidate.",
            workspace_id=workspace_id, scope=Scope.WORKSPACE,
        )}
        untrusted = svc.remember(
            "Fallback visibility untrusted candidate.",
            workspace="demo",
            source="sync",
            trusted=False,
        )

        def incompatible_embedder(*_args, **_kwargs):
            raise ValueError("shapes (256,) and (384,) not aligned")

        monkeypatch.setattr(svc, "recall", incompatible_embedder)
        response = client.get(
            "/api/recall",
            params={"workspace": "demo", "q": "fallback visibility candidate", "k": 1},
        )

    payload = response.json()
    assert response.status_code == 200
    assert payload["mode"] == "keyword"
    assert [memory["id"] for memory in payload["memories"]] == [trusted["id"]]
    assert untrusted["id"] not in {memory["id"] for memory in payload["memories"]}
    assert "untrusted candidate" not in repr(payload)


def test_http_memory_api_rejects_backdated_agent_claim_supersession(
    monkeypatch, tmp_path
):
    with _client(monkeypatch, tmp_path) as client:
        original = client.post(
            "/api/remember",
            json={
                "workspace": "demo",
                "content": "The deployment window is Friday afternoon.",
                "valid_from": 2_000.0,
            },
        ).json()
        service = v2_api.service()
        count_before = len(service.store.list_memories(include_invalid=True))
        rejected = client.post(
            "/api/remember",
            json={
                "workspace": "demo",
                "content": "The deployment window is Thursday afternoon.",
                "valid_from": 1_000.0,
            },
        )

        assert rejected.status_code == 400
        assert service.store.get_memory(original["id"]).valid_to is None
        assert len(service.store.list_memories(include_invalid=True)) == count_before


def test_manual_consolidation_stays_local_but_dreaming_is_cloud_only(
    monkeypatch, tmp_path
):
    with _client(monkeypatch, tmp_path) as client:
        assert "Close superseded source validity" not in client.get("/").text
        assert "supersede_sources" not in client.get("/v2-assets/ledger.js").text
        manual = client.post(
            "/api/consolidate",
            json={"workspace": "demo", "dry_run": True, "infer": False},
        )
        assert manual.status_code == 200
        dream = client.post(
            "/api/consolidate",
            json={"workspace": "demo", "dry_run": True, "infer": True},
        )
        assert dream.status_code == 501
        assert dream.json()["detail"]["cloud_only"] is True


def test_analytics_route_delegates_to_managed_compute(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "engraphis.cloud_features.run_managed_job",
        lambda service, workspace, kind: {
            "result": {
                "kind": kind,
                "generation": 4,
                "totals": {"live": 1},
            }
        },
    )
    with _client(monkeypatch, tmp_path) as client:
        response = client.get("/api/analytics?workspace=demo")
        assert response.status_code == 200
        assert response.json()["kind"] == "analytics"
        assert response.json()["generation"] == 4


def test_unconnected_automation_returns_a_structured_auth_error(monkeypatch, tmp_path):
    for name in (
        "ENGRAPHIS_CLOUD_ACCESS_TOKEN",
        "ENGRAPHIS_CLOUD_ORGANIZATION_ID",
        "ENGRAPHIS_CLOUD_COMPUTE_URL",
        "ENGRAPHIS_CLOUD_REFRESH_CREDENTIAL",
        "ENGRAPHIS_CLOUD_CONTROL_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ENGRAPHIS_STATE_DIR", str(tmp_path / "unconnected-state"))

    with _client(monkeypatch, tmp_path) as client:
        response = client.get("/api/automation?workspace=demo")

    assert response.status_code == 401
    # The copy is ``_public_session_error(401)``: fixed, status-keyed, and actionable. The
    # generic placeholder told an unconnected customer nothing they could act on.
    assert response.json()["detail"] == {
        "error": "Connect this installation to Engraphis Cloud to use hosted features.",
        "managed_cloud": True,
        "transient": False,
        "code": "cloud_unconfigured",
    }


def test_hosted_automation_accepts_the_cloud_policy_field(monkeypatch, tmp_path):
    saved = {}

    class _Cloud:
        def upload_snapshot(self, workspace_id, snapshot):
            return {"generation": snapshot["generation"]}

        def get_policy(self, workspace_id):
            return {"enabled": False, "cadence_minutes": 1440, "dream_enabled": False}

        def save_policy(self, workspace_id, policy):
            saved.update(policy)
            return {"version": 2}

    monkeypatch.setattr(
        "engraphis.cloud_features.build_managed_snapshot",
        lambda service, workspace: ("ws_cloud", {"generation": 1}),
    )
    monkeypatch.setattr(
        "engraphis.cloud_features.CloudFeatureClient.from_environment",
        lambda workspace_id=None: _Cloud(),
    )
    with _client(monkeypatch, tmp_path) as client:
        response = client.post(
            "/api/automation",
            json={"enabled": True, "dream_enabled": True, "cadence_hours": 12},
        )
        assert response.status_code == 200
        assert response.json()["dream_enabled"] is True
        assert saved["dream_enabled"] is True


def test_first_hosted_automation_view_waits_for_explicit_bootstrap(
    monkeypatch, tmp_path
):
    """Observation-only GET must not upload memory content or write a hosted policy."""

    uploaded = []
    saved = []

    class _Cloud:
        organization_id = "org_test"

        def __init__(self):
            self.policy = {"enabled": False, "cadence_minutes": 1440, "version": 0}

        def get_policy(self, workspace_id):
            return dict(self.policy)

        def upload_snapshot(self, workspace_id, snapshot):
            uploaded.append((workspace_id, snapshot))
            return {"generation": snapshot["generation"]}

        def save_policy(self, workspace_id, policy):
            saved.append((workspace_id, policy))
            self.policy = {**policy, "version": 1}
            return {"version": 1}

        def list_jobs(self, workspace_id, *, limit=10):
            return {"jobs": []}

    cloud = _Cloud()
    monkeypatch.setattr(
        "engraphis.cloud_features.build_managed_snapshot",
        lambda service, workspace: (
            service._lookup_workspace(workspace),
            {"generation": 7},
        ),
    )
    monkeypatch.setattr(
        "engraphis.cloud_features.CloudFeatureClient.from_environment",
        lambda workspace_id=None: cloud,
    )
    with _client(monkeypatch, tmp_path) as client:
        observed = client.get("/api/automation")
        assert observed.status_code == 200
        assert observed.json()["bootstrap_required"] is True
        assert observed.json()["enabled"] is False
        assert uploaded == []
        assert saved == []

        initialized = client.post("/api/automation/bootstrap")

    assert initialized.status_code == 200
    assert initialized.json()["bootstrap_required"] is False
    assert initialized.json()["enabled"] is True
    assert len(uploaded) == 1
    assert len(saved) == 1
    assert uploaded[0] == (saved[0][0], {"generation": 7})
    assert saved[0][1] == {
        "enabled": True,
        "cadence_minutes": 1440,
        "dream_enabled": True,
        "dream_min_new": 25,
        "dream_idle_minutes": 15,
        "infer": False,
    }


def test_explicit_automation_bootstrap_retry_does_not_upload_twice(
    monkeypatch, tmp_path
):
    """A failed policy write resumes after the already successful private upload."""

    from engraphis.cloud_features import CloudFeatureError

    uploaded = []
    saved = []
    builds = []

    class _Cloud:
        organization_id = "org_test"

        def __init__(self):
            self.policy = {"enabled": False, "cadence_minutes": 1440, "version": 0}

        def get_policy(self, workspace_id):
            return dict(self.policy)

        def upload_snapshot(self, workspace_id, snapshot):
            uploaded.append((workspace_id, snapshot))
            return {"generation": snapshot["generation"]}

        def save_policy(self, workspace_id, policy):
            saved.append((workspace_id, policy))
            if len(saved) == 1:
                raise CloudFeatureError(
                    "Engraphis Cloud is temporarily unavailable.",
                    status=503,
                    transient=True,
                )
            self.policy = {**policy, "version": 1}
            return {"version": 1}

        def list_jobs(self, workspace_id, *, limit=10):
            return {"jobs": []}

    def _snapshot(service, workspace):
        builds.append(workspace)
        return service._lookup_workspace(workspace), {"generation": 7}

    cloud = _Cloud()
    monkeypatch.setattr("engraphis.cloud_features.build_managed_snapshot", _snapshot)
    monkeypatch.setattr(
        "engraphis.cloud_features.CloudFeatureClient.from_environment",
        lambda workspace_id=None: cloud,
    )
    with _client(monkeypatch, tmp_path) as client:
        first = client.post("/api/automation/bootstrap")
        second = client.post("/api/automation/bootstrap")

    assert first.status_code == 503
    assert second.status_code == 200
    assert second.json()["enabled"] is True
    assert len(builds) == 1
    assert len(uploaded) == 1
    assert uploaded[0] == (saved[0][0], {"generation": 7})
    assert len(saved) == 2


def test_concurrent_explicit_automation_bootstraps_upload_one_snapshot(
    monkeypatch, tmp_path
):
    """Parallel opt-in actions serialize the sensitive first-bootstrap upload."""

    uploaded = []
    saved = []
    started = threading.Event()
    release_upload = threading.Event()

    class _Cloud:
        organization_id = "org_concurrent"

        def __init__(self):
            self.policy = {"enabled": False, "cadence_minutes": 1440, "version": 0}

        def get_policy(self, workspace_id):
            return dict(self.policy)

        def upload_snapshot(self, workspace_id, snapshot):
            uploaded.append((workspace_id, snapshot))
            started.set()
            assert release_upload.wait(timeout=5)
            return {"generation": snapshot["generation"]}

        def save_policy(self, workspace_id, policy):
            saved.append((workspace_id, policy))
            self.policy = {**policy, "version": 1}
            return {"version": 1}

        def list_jobs(self, workspace_id, *, limit=10):
            return {"jobs": []}

    cloud = _Cloud()
    monkeypatch.setattr(
        "engraphis.cloud_features.build_managed_snapshot",
        lambda service, workspace: (
            service._lookup_workspace(workspace),
            {"generation": 7},
        ),
    )
    monkeypatch.setattr(
        "engraphis.cloud_features.CloudFeatureClient.from_environment",
        lambda workspace_id=None: cloud,
    )
    with _client(monkeypatch, tmp_path):
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(v2_api.automation_bootstrap)
            assert started.wait(timeout=5)
            second = pool.submit(v2_api.automation_bootstrap)
            release_upload.set()
            assert first.result(timeout=5)["enabled"] is True
            follower = second.result(timeout=5)
            assert follower["enabled"] is True
            assert follower["version"] == 1

    assert len(uploaded) == 1
    assert uploaded[0] == (saved[0][0], {"generation": 7})
    assert len(saved) == 1


def test_reading_or_disabling_automation_never_uploads_memory_content(
    monkeypatch, tmp_path
):
    saved = {}

    class _Cloud:
        def get_policy(self, workspace_id):
            return {"enabled": True, "cadence_minutes": 60, "dream_enabled": True}

        def list_jobs(self, workspace_id, *, limit=10):
            return {"jobs": []}

        def save_policy(self, workspace_id, policy):
            saved.update(policy)
            return {"version": 3}

    def _unexpected_upload(*args, **kwargs):
        raise AssertionError("policy inspection must not build or upload a snapshot")

    monkeypatch.setattr(
        "engraphis.cloud_features.build_managed_snapshot",
        _unexpected_upload,
    )
    monkeypatch.setattr(
        "engraphis.cloud_features.CloudFeatureClient.from_environment",
        lambda workspace_id=None: _Cloud(),
    )
    with _client(monkeypatch, tmp_path) as client:
        assert client.get("/api/automation").status_code == 200
        response = client.post("/api/automation", json={"enabled": False})
        assert response.status_code == 200
        assert saved["enabled"] is False


def test_automation_and_maintenance_use_the_selected_workspace(monkeypatch, tmp_path):
    policy_workspaces = []
    snapshot_workspaces = []
    maintenance_workspaces = []

    class _Cloud:
        def get_policy(self, workspace_id):
            policy_workspaces.append(workspace_id)
            return {"enabled": False, "cadence_minutes": 60, "dream_enabled": True}

        def list_jobs(self, workspace_id, *, limit=10):
            policy_workspaces.append(workspace_id)
            return {"jobs": []}

        def upload_snapshot(self, workspace_id, snapshot):
            snapshot_workspaces.append(workspace_id)
            return {"generation": snapshot["generation"]}

        def save_policy(self, workspace_id, policy):
            policy_workspaces.append(workspace_id)
            return {"version": 1}

    def snapshot(service, workspace):
        snapshot_workspaces.append(workspace)
        return service._lookup_workspace(workspace), {"generation": 1}

    def managed_job(service, workspace, kind):
        maintenance_workspaces.append((workspace, kind))
        return {"result": {"kind": kind}}

    monkeypatch.setattr("engraphis.cloud_features.build_managed_snapshot", snapshot)
    monkeypatch.setattr("engraphis.cloud_features.run_managed_job", managed_job)
    monkeypatch.setattr(
        "engraphis.cloud_features.CloudFeatureClient.from_environment",
        lambda workspace_id=None: _Cloud(),
    )
    with _client(monkeypatch, tmp_path) as client:
        beta_id = client.app.state.service._lookup_workspace("beta")
        demo_id = client.app.state.service._lookup_workspace("demo")
        assert client.get("/api/automation?workspace=beta").status_code == 200
        assert client.post(
            "/api/automation?workspace=beta", json={"enabled": True}
        ).status_code == 200
        assert client.post(
            "/api/maintenance/run?workspace=beta", json={"dry_run": True}
        ).status_code == 200

    assert beta_id in policy_workspaces
    assert demo_id not in policy_workspaces
    assert "beta" in snapshot_workspaces
    assert maintenance_workspaces == [("beta", "consolidate")]


def test_automation_workspace_query_unknown_is_not_replaced_by_legacy_default(
    monkeypatch, tmp_path
):
    with _client(monkeypatch, tmp_path) as client:
        for method, path, payload in (
            (client.get, "/api/automation?workspace=missing", None),
            (client.post, "/api/automation?workspace=missing", {"enabled": False}),
            (client.post, "/api/maintenance/run?workspace=missing", {"dry_run": True}),
        ):
            response = method(path, json=payload) if payload is not None else method(path)
            assert response.status_code == 404


def test_dashboard_automation_uses_active_workspace_and_discloses_upload_boundary():
    source = Path(__file__).parents[1] / "engraphis" / "static" / "dashboard.js"
    source = source.read_text(encoding="utf-8")
    assert "/automation?workspace=" in source
    assert "/maintenance/run?workspace=" in source
    assert "Preview snapshot" not in source
    assert "uploads the selected workspace’s normal and sensitive memory content" in source
    # The upload boundary is still disclosed, but consent now travels with the cloud
    # account: the dashboard must not name the operator override anywhere.
    assert "ENGRAPHIS_MANAGED_COMPUTE_CONSENT" not in source
    assert "Hosted work is automatic with Pro." in source


def test_portfolio_and_report_analytics_are_hosted_only(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        assert client.get("/api/analytics/portfolio").status_code == 501
        assert client.get("/api/analytics/export?workspace=demo").status_code == 501


def test_raw_owner_export_is_free_and_signed_export_is_honestly_unimplemented(
    monkeypatch, tmp_path
):
    """The signed variant must not claim to exist somewhere else.

    It previously answered ``cloud_only: True`` — but Engraphis Cloud has no export route,
    no supported hosted export capability, so that pointed a customer at a
    product that does not exist. The 501 now says the capability is unimplemented and names
    the working unsigned export instead.
    """

    with _client(monkeypatch, tmp_path) as client:
        raw = client.get("/api/export?workspace=demo")
        assert raw.status_code == 200
        assert raw.json()["counts"]["memories"] >= 1
        signed = client.get("/api/export?workspace=demo&signed=true")
        assert signed.status_code == 501
        detail = signed.json()["detail"]
        assert detail["implemented"] is False
        assert detail["alternative"] == "/export"
        assert "cloud_only" not in detail
        assert "Engraphis Cloud" not in detail["error"]


def test_health_and_readiness_remain_public(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/ready").status_code == 200


def test_dashboard_exception_responses_do_not_echo_untrusted_exception_text():
    secret = "https://provider.example/?api_key=do-not-return-this"

    def fail_with(exc):
        raise exc

    with pytest.raises(HTTPException) as internal:
        v2_api._run(fail_with, RuntimeError(secret))
    assert internal.value.status_code == 500
    assert internal.value.detail == {"error": "internal server error"}
    assert secret not in repr(internal.value.detail)

    with pytest.raises(HTTPException) as validation:
        v2_api._run(fail_with, ValidationError(secret))
    assert validation.value.status_code == 400
    assert validation.value.detail == {"error": "invalid request"}
    assert secret not in repr(validation.value.detail)

    with pytest.raises(HTTPException) as downstream:
        v2_api._run(fail_with, HTTPException(status_code=418, detail={"error": secret}))
    assert downstream.value.status_code == 418
    assert downstream.value.detail == {"error": "request rejected"}
    assert secret not in repr(downstream.value.detail)

    with pytest.raises(HTTPException) as invalid_status:
        v2_api._run(fail_with, HTTPException(status_code=999, detail={"error": secret}))
    assert invalid_status.value.status_code == 500
    assert invalid_status.value.detail == {"error": "internal server error"}
    assert secret not in repr(invalid_status.value.detail)

    with pytest.raises(HTTPException) as mismatch:
        v2_api._run(fail_with, ValueError(f"{secret}: shapes 256 and 384 are not aligned"))
    assert mismatch.value.status_code == 409
    assert mismatch.value.detail["embedder"] is True
    assert secret not in repr(mismatch.value.detail)

    with pytest.raises(HTTPException) as ordinary_value_error:
        v2_api._run(fail_with, ValueError(secret))
    assert ordinary_value_error.value.status_code == 400
    assert ordinary_value_error.value.detail == {"error": "invalid request"}
    assert secret not in repr(ordinary_value_error.value.detail)


def test_dashboard_engine_value_error_is_a_sanitized_client_error(monkeypatch, tmp_path):
    secret = "malformed document details must stay private"
    with _client(monkeypatch, tmp_path) as client:
        def reject_document(*_args, **_kwargs):
            raise ValueError(secret)

        monkeypatch.setattr(client.app.state.service, "remember", reject_document)
        response = client.post(
            "/api/remember",
            json={"content": "client document", "workspace": "demo"},
        )

    assert response.status_code == 400
    assert response.json() == {"detail": {"error": "invalid request"}}
    assert secret not in response.text


def test_managed_cloud_errors_forward_only_bounded_public_copy():
    """``_managed_call`` forwards the message; the bound is the boundary's own check.

    ``CloudFeatureError`` is the already-redacted form -- every raise site builds it from
    fixed, status-keyed copy -- so its text is what the customer should read. The bound
    here is not the redaction, it is the guard for a message that is *not* that fixed copy:
    anything oversized, empty, or carrying control characters is dropped for the generic
    placeholder rather than rendered into a JSON error body.
    """

    def fail_with(exc):
        raise exc

    for message in ("x" * 301, "", "connection\x00reset", "trace\x1b[31m"):
        with pytest.raises(HTTPException) as caught:
            v2_api._managed_call(fail_with, CloudFeatureError(message, status=502))
        assert caught.value.status_code == 502
        assert caught.value.detail == {
            "error": v2_api._MANAGED_ERROR_FALLBACK, "managed_cloud": True,
            "transient": False,
        }

    with pytest.raises(HTTPException) as consent:
        v2_api._managed_call(
            fail_with,
            CloudFeatureError(
                "Managed compute is turned off for this installation.",
                status=409, code="consent_required",
            ),
        )
    assert consent.value.status_code == 409
    assert consent.value.detail == {
        "error": "Managed compute is turned off for this installation.",
        "managed_cloud": True,
        "transient": False,
        "code": "consent_required",
    }

    with pytest.raises(HTTPException) as unconfigured:
        v2_api._managed_call(
            fail_with,
            CloudFeatureError(
                "Connect this installation to Engraphis Cloud to use hosted features.",
                status=401, code="cloud_unconfigured",
            ),
        )
    assert unconfigured.value.status_code == 401
    assert unconfigured.value.detail == {
        "error": "Connect this installation to Engraphis Cloud to use hosted features.",
        "managed_cloud": True,
        "transient": False,
        "code": "cloud_unconfigured",
    }


@pytest.mark.parametrize("status", (401, 402, 403))
def test_managed_authorization_denial_settles_local_entitlement(monkeypatch, status):
    """A live hosted denial must immediately retire stale paid presentation state."""

    calls = []
    monkeypatch.setattr(v2_api, "_record_authoritative_denial", lambda: calls.append(status))

    def fail_with(exc):
        raise exc

    with pytest.raises(HTTPException) as caught:
        v2_api._managed_call(
            fail_with, CloudFeatureError("Engraphis Cloud authorization was rejected.",
                                         status=status),
        )

    assert caught.value.status_code == status
    assert calls == [status]


@pytest.mark.parametrize("status", (409, 429, 503))
def test_managed_non_authorization_failures_do_not_settle_entitlement(monkeypatch, status):
    """Conflicts and outages do not prove that a subscription or membership changed."""

    calls = []
    monkeypatch.setattr(v2_api, "_record_authoritative_denial", lambda: calls.append(status))

    def fail_with(exc):
        raise exc

    with pytest.raises(HTTPException):
        v2_api._managed_call(
            fail_with, CloudFeatureError("Engraphis Cloud temporarily failed.", status=status),
        )

    assert calls == []


def _managed_http_failure(monkeypatch, status: int) -> HTTPException:
    """Drive one real hosted request against a control plane that answers ``status``."""

    class _Opener:
        def open(self, request, timeout=None):
            raise urllib.error.HTTPError(
                "https://compute.example.test/private", status, "failure", {},
                io.BytesIO(b'{"detail": "provider-internals https://backend.invalid"}'),
            )

    monkeypatch.setattr(
        cloud_features, "build_pinned_https_opener", lambda *handlers: _Opener()
    )
    client = cloud_features.CloudFeatureClient(
        "https://compute.example.test", "org_1", "token"
    )
    with pytest.raises(HTTPException) as caught:
        v2_api._managed_call(client._request, "GET", "/private")
    return caught.value


def test_a_managed_outage_is_distinguishable_from_a_workspace_conflict(monkeypatch):
    """The defect: every hosted failure rendered as one fixed, unactionable string.

    ``cloud_features._public_http_error`` already produces redacted, status-keyed copy that
    tells a retryable outage apart from a conflict the customer has to fix -- and
    ``_managed_call`` threw all of it away, so the dashboard's error branch could only ever
    show "managed cloud operation failed" for a 429, a 5xx and a 409 alike.
    """

    busy = _managed_http_failure(monkeypatch, 429)
    down = _managed_http_failure(monkeypatch, 503)
    conflict = _managed_http_failure(monkeypatch, 409)

    assert busy.status_code == 429
    assert busy.detail["transient"] is True
    assert "temporarily busy" in busy.detail["error"], busy.detail["error"]

    assert down.status_code == 503
    assert down.detail["transient"] is True
    assert "temporarily unavailable" in down.detail["error"], down.detail["error"]

    assert conflict.status_code == 409
    assert conflict.detail["transient"] is False
    assert "workspace state" in conflict.detail["error"], conflict.detail["error"]

    messages = {busy.detail["error"], down.detail["error"], conflict.detail["error"]}
    assert len(messages) == 3, "the dashboard still cannot tell these three apart"
    assert v2_api._MANAGED_ERROR_FALLBACK not in messages
    # Forwarding the public copy must not forward the provider's body with it.
    assert all("provider-internals" not in text for text in messages)
    assert all("backend.invalid" not in text for text in messages)


def test_every_managed_cloud_error_message_is_fixed_local_copy():
    """The invariant that makes forwarding safe, pinned against future raise sites.

    ``_managed_call`` may forward a ``CloudFeatureError`` message only because every one of
    them is built from a literal in this repository -- never from a provider body, a
    ``CloudSessionError``, or a local path. A raise site that interpolated a runtime value
    would silently turn this boundary into a reflection point, so the shape is asserted
    rather than trusted.

    Three forms are accepted: a string literal; a name bound from ``_public_http_error`` /
    ``_public_session_error`` (both of which switch on a bare integer status and return
    fixed copy); and the one audited ``%`` template, below.
    """

    source = Path(cloud_features.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    public_copy = {"_public_http_error", "_public_session_error"}
    from_public_copy = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        called = node.value.func
        if not isinstance(called, ast.Name) or called.id not in public_copy:
            continue
        for target in node.targets:
            elements = target.elts if isinstance(target, ast.Tuple) else [target]
            from_public_copy.update(
                item.id for item in elements if isinstance(item, ast.Name)
            )
    assert from_public_copy, "the fixed-copy helpers are no longer bound to a name"

    interpolated = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.id if isinstance(node.func, ast.Name) else None
        if name != "CloudFeatureError" or not node.args:
            continue
        message = node.args[0]
        if isinstance(message, ast.Constant) and isinstance(message.value, str):
            continue
        if isinstance(message, ast.Name) and message.id in from_public_copy:
            continue
        # ``"literal %s" % (...)`` is allowed only where the substituted values are
        # themselves constrained to local literals; ``run_job`` is the single such site
        # and its ``status`` is guarded by an ``in {"failed", "canceled"}`` membership
        # test one line above. Anything else -- an f-string, a bare name, a concatenated
        # response field -- is a reflection risk and fails here.
        if (isinstance(message, ast.BinOp) and isinstance(message.op, ast.Mod)
                and isinstance(message.left, ast.Constant)
                and message.left.value == "Managed %s did not complete (%s)."):
            continue
        interpolated.append((node.lineno, ast.dump(message)[:120]))

    assert interpolated == [], (
        "a CloudFeatureError message is no longer fixed local copy; _managed_call "
        "forwards it to the customer: %r" % (interpolated,)
    )
