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


def _client(monkeypatch, tmp_path, *, api_token=""):
    db_path = str(tmp_path / "bounded.db")
    monkeypatch.setattr(settings, "db_path", db_path)
    monkeypatch.setattr(settings, "embed_model", "")
    monkeypatch.setattr(settings, "embed_dim", 384)
    monkeypatch.setattr(settings, "allowed_workspaces", [])
    monkeypatch.setattr(settings, "api_token", api_token)
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
    Starlette's raw 'Too many files' failure.  Multipart parsing (route class)
    runs before the owner gate, so this holds in open mode too."""
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
    then comes from the route's owner gate (401 — no browser session), never
    from Starlette's 1,000-part default ceiling."""
    with _client(monkeypatch, tmp_path, api_token="dashboard-owner-token") as client:
        response = client.post(
            "/api/workspaces/import-documents/preview",
            data={"workspace": "demo", "source_label": "Notes"},
            files=_wizard_upload(MAX_IMPORT_FILES),
        )
        assert response.status_code == 401
        assert response.json()["error"] == "unauthorized"


def test_bounded_route_maps_too_many_fields(monkeypatch, tmp_path):
    """Starlette raises 'Too many fields' at parse time; our handler must map it to
    a clean client error rather than an unhandled failure."""
    with _client(monkeypatch, tmp_path) as client:
        response = client.post(
            "/api/workspaces/import-documents/preview",
            data={f"field{i}": str(i) for i in range(64)},
            files=_wizard_upload(1),
        )
        assert response.status_code == 400
        assert response.json()["detail"]["error"] == "invalid upload form"


def test_bounded_upload_router_installs_route_class():
    """The router mechanism must install _BoundedUploadRoute on every path in
    _BOUNDED_UPLOAD_PATHS (and only those). Asserted on router.routes rather than
    app.routes: newer FastAPI wraps included routers in one composite object
    instead of flattening per-path routes."""
    from engraphis.dashboard_app import (
        _BOUNDED_UPLOAD_PATHS,
        _BoundedUploadRouter,
    )

    def _endpoint():  # noqa: ANN202 - test stub
        return {}

    router = _BoundedUploadRouter()
    for index, path in enumerate(sorted(_BOUNDED_UPLOAD_PATHS)):
        router.add_api_route(path, _endpoint, methods=["POST"])
    router.add_api_route("/unrelated", _endpoint, methods=["POST"])

    classes = {
        route.path: type(route).__name__
        for route in router.routes
        if hasattr(route, "path")
    }
    for path in _BOUNDED_UPLOAD_PATHS:
        assert classes[path] == "_BoundedUploadRoute", path
    assert classes["/unrelated"] == "APIRoute"
    # Every bounded path is one of the multipart upload surfaces.
    assert all(path.endswith(("/preview", "/run")) for path in _BOUNDED_UPLOAD_PATHS)
