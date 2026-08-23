"""HTTP-layer regression coverage for the bounded multipart parser on the wizard
upload routes (the fix for 'Import failed: Internal Server Error' above 1,000 files).

Starlette's default multipart ceiling is 1,000 files and FastAPI resolves UploadFile
parameters before any route code runs, so the dashboard's document/Obsidian wizard
routes parse forms through _BoundedUploadRoute with the advertised MAX_IMPORT_FILES
ceiling instead.
"""
import io

import pytest

pytest.importorskip("fastapi", reason="full-stack extra not installed")
pytest.importorskip("httpx", reason="httpx not installed")

from engraphis.config import settings  # noqa: E402
from engraphis.service import MAX_IMPORT_FILES  # noqa: E402


def _client(monkeypatch, tmp_path):
    db_path = str(tmp_path / "bounded.db")
    monkeypatch.setattr(settings, "db_path", db_path)
    monkeypatch.setattr(settings, "embed_model", "")
    monkeypatch.setattr(settings, "embed_dim", 384)
    monkeypatch.setattr(settings, "allowed_workspaces", [])
    monkeypatch.setattr(settings, "api_token", "")
    from engraphis.dashboard_app import create_app
    from fastapi.testclient import TestClient
    return TestClient(create_app(), client=("127.0.0.1", 50000))


def _wizard_upload(files: int):
    return [
        ("files", (f"note-{i}.md", io.BytesIO(b"# note\n"), "text/markdown"))
        for i in range(files)
    ]


def test_bounded_route_rejects_over_ceiling_with_413(monkeypatch, tmp_path):
    """MAX_IMPORT_FILES + 1 parts must reach our handler as a clean 413 — not
    Starlette's raw 'Too many files' failure."""
    with _client(monkeypatch, tmp_path) as client:
        response = client.post(
            "/api/workspaces/import-documents/preview",
            data={"workspace": "demo", "source_label": "Notes"},
            files=_wizard_upload(MAX_IMPORT_FILES + 1),
        )
        assert response.status_code == 413
        assert response.json()["detail"]["error"] == (
            f"too many files (max {MAX_IMPORT_FILES})"
        )


def test_bounded_route_accepts_full_ceiling(monkeypatch, tmp_path):
    """Exactly MAX_IMPORT_FILES parts must pass multipart parsing; the response
    then comes from the route's owner gate (409 — no API token configured),
    never from Starlette's 1,000-part default ceiling."""
    with _client(monkeypatch, tmp_path) as client:
        response = client.post(
            "/api/workspaces/import-documents/preview",
            data={"workspace": "demo", "source_label": "Notes"},
            files=_wizard_upload(MAX_IMPORT_FILES),
        )
        assert response.status_code == 409
        assert response.json()["detail"]["error"] == (
            "document import requires ENGRAPHIS_API_TOKEN"
        )


def test_bounded_route_maps_too_many_fields(monkeypatch, tmp_path):
    """Starlette raises 'Too many fields' at parse time; our handler must map it to
    a clean client error rather than an unhandled failure."""
    from engraphis.dashboard_app import _BoundedUploadRoute

    with _client(monkeypatch, tmp_path) as client:
        response = client.post(
            "/api/workspaces/import-documents/preview",
            data={"field": str(i) for i in range(64)},
            files=_wizard_upload(1),
        )
        assert response.status_code in {400, 422}
        body = response.json()
        detail = body.get("detail")
        if isinstance(detail, dict):
            assert "invalid upload form" in str(detail.get("error", ""))
        # The class must still carry the explicit field ceiling either way.
        assert _BoundedUploadRoute._MAX_FORM_FIELDS == 14


def test_wizard_routes_use_bounded_route_class():
    """The four wizard routes must be registered through the bounded route class,
    so the parser ceiling cannot silently regress to Starlette's default."""
    from engraphis.dashboard_app import create_app

    app = create_app()
    bounded_paths = {
        "/api/workspaces/import-documents/preview",
        "/api/workspaces/import-documents/run",
        "/api/workspaces/import-obsidian/preview",
        "/api/workspaces/import-obsidian/run",
    }
    seen = {}
    for route in app.routes:
        path = getattr(route, "path", None)
        if path in bounded_paths:
            seen[path] = type(route).__name__
    assert set(seen) == bounded_paths
    assert {name for name in seen.values()} == {"_BoundedUploadRoute"}
