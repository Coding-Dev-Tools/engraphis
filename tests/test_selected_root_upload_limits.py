"""Synthetic credential-store roots and unread excluded-upload resource budgets."""
from __future__ import annotations

import asyncio
import inspect
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from engraphis.core.documents import scan_document_tree
from engraphis.core.obsidian import scan_obsidian_vault
from engraphis.core.source_paths import sensitive_path
from engraphis.service import MemoryService, ValidationError, _iter_import_files, _resolve_import_root


STORES = (".aws", ".ssh", ".gnupg", ".azure", ".kube", ".docker", ".engraphis")


@pytest.fixture
def service():
    instance = MemoryService.create(":memory:", extractor="none", graph_extractor="none")
    try:
        yield instance
    finally:
        instance.close()


@pytest.mark.parametrize("store", STORES)
@pytest.mark.parametrize("scanner", [_resolve_import_root, scan_document_tree, scan_obsidian_vault])
def test_known_store_selected_roots_stop_before_filesystem_access(monkeypatch, store, scanner):
    def forbidden(*args, **kwargs):
        pytest.fail("excluded selected root reached filesystem inspection")

    with monkeypatch.context() as guard:
        guard.setattr(os.path, "realpath", forbidden)
        guard.setattr(Path, "lstat", forbidden)
        guard.setattr(Path, "resolve", forbidden)
        guard.setattr(Path, "iterdir", forbidden)
        with pytest.raises(ValueError, match="sensitive"):
            scanner("/synthetic/home/" + store.upper())


@pytest.mark.parametrize("store", STORES)
def test_known_store_components_are_lexical_across_platforms(store):
    assert sensitive_path(store)
    assert sensitive_path(rf"C:\synthetic\home\{store.upper()}\cache\opaque.json")
    assert sensitive_path("/synthetic/home/" + store + "/../notes.md")


@pytest.mark.parametrize("store", [".aws", ".ssh"])
@pytest.mark.parametrize("kind", ["quick", "documents", "obsidian"])
def test_actual_selected_stores_cannot_import_innocuous_files(
    service, monkeypatch, tmp_path_factory, store, kind,
):
    root = tmp_path_factory.mktemp("selected-source") / store
    root.mkdir()
    (root / "opaque.md").write_text("# Synthetic notes\nPublic fixture content.")
    monkeypatch.setenv("ENGRAPHIS_IMPORT_ROOTS", str(root.parent))
    with pytest.raises(ValueError, match="sensitive"):
        if kind == "quick":
            service.import_folder(workspace="sources", path=str(root), file_pattern="*")
        elif kind == "documents":
            scan_document_tree(root)
        else:
            scan_obsidian_vault(root)
    assert service.store.count_memories() == 0


@pytest.mark.parametrize("name", [".worktrees", ".notes", ".config"])
def test_benign_hidden_selected_roots_remain_usable(monkeypatch, tmp_path_factory, name):
    root = tmp_path_factory.mktemp("selected-source") / name
    root.mkdir()
    note = root / "notes.md"
    note.write_text("# Release notes\nPublic synthetic notes.")
    monkeypatch.setenv("ENGRAPHIS_IMPORT_ROOTS", str(root.parent))
    assert _resolve_import_root(str(root)) == root.resolve()
    assert _iter_import_files(root, "*.md", 1) == ([note], 1, 0)
    assert [item.relative_path for item in scan_document_tree(root).documents] == ["notes.md"]
    assert [item.relative_path for item in scan_obsidian_vault(root).notes] == ["notes.md"]


def test_alias_to_known_store_is_rejected_before_content_read(monkeypatch, tmp_path_factory):
    root = tmp_path_factory.mktemp("selected-source")
    target = root / ".ssh" / "opaque.md"
    target.parent.mkdir()
    target.write_text("Public synthetic notes.")
    alias = root / "notes.md"
    try:
        alias.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    assert _iter_import_files(root, "*.md", 1) == ([], 0, 0)
    monkeypatch.setenv("ENGRAPHIS_IMPORT_ROOTS", str(root))
    selected = root / "selected"
    selected.symlink_to(target.parent, target_is_directory=True)
    with pytest.raises(ValidationError, match="sensitive"):
        _resolve_import_root(str(selected))


@pytest.fixture
def guarded_helpers(service, monkeypatch):
    pytest.importorskip("fastapi")
    from engraphis import dashboard_app
    from engraphis.routes import v2_api

    async def inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(v2_api, "service", lambda: service)
    monkeypatch.setattr(v2_api, "run_in_threadpool", inline)
    monkeypatch.setattr(v2_api, "set_service", lambda instance: None)
    monkeypatch.setattr(MemoryService, "create", lambda *args, **kwargs: service)
    monkeypatch.setattr("engraphis.service.MAX_IMPORT_RESOURCE_BYTES", 8)
    monkeypatch.setattr("engraphis.service.MAX_IMPORT_TOTAL_BYTES", 12)
    monkeypatch.setattr(dashboard_app, "MAX_IMPORT_RESOURCE_BYTES", 8)
    monkeypatch.setattr(dashboard_app, "MAX_IMPORT_TOTAL_BYTES", 12)
    production_app = dashboard_app.create_app()
    def routes(router):
        for route in router.routes:
            yield route
            nested = getattr(route, "original_router", None)
            if nested is not None:
                yield from routes(nested)

    route = next(route for route in routes(production_app)
                 if getattr(route, "path", "") == "/api/workspaces/import-documents/preview")
    document_uploads = inspect.getclosurevars(route.endpoint).nonlocals["_document_uploads"]

    async def quick(files):
        return await v2_api._import_uploaded_files(
            workspace="sources", memory_type="semantic", derive_facts=False, files=files,
        )

    async def documents(files):
        return await document_uploads(files, source_mode="documents")

    async def obsidian(files):
        return await document_uploads(files, source_mode="obsidian")

    return {"quick": quick, "documents": documents, "obsidian": obsidian}, production_app, route


def upload(name, size, data=b"", *, unread=False):
    async def read(limit):
        if unread:
            pytest.fail("excluded upload body was read")
        return data[:limit]

    return SimpleNamespace(filename=name, size=size, read=read)


@pytest.mark.parametrize("kind", ["quick", "documents", "obsidian"])
def test_excluded_upload_rejects_declared_per_file_excess_without_read(guarded_helpers, kind):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as error:
        asyncio.run(guarded_helpers[0][kind]([upload("credentials/config.json", 9, unread=True)]))
    assert error.value.status_code == 413


@pytest.mark.parametrize("kind", ["quick", "documents", "obsidian"])
@pytest.mark.parametrize("safe_first", [False, True])
def test_mixed_excluded_uploads_still_count_toward_aggregate(guarded_helpers, kind, safe_first):
    from fastapi import HTTPException
    files = [upload("credentials/config.json", 7, unread=True), upload("notes.md", 6, b"notes.")]
    if safe_first:
        files.reverse()
    with pytest.raises(HTTPException) as error:
        asyncio.run(guarded_helpers[0][kind](files))
    assert error.value.status_code == 413


@pytest.mark.parametrize("kind", ["quick", "documents", "obsidian"])
def test_all_excluded_uploads_still_count_toward_aggregate(guarded_helpers, kind):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as error:
        asyncio.run(guarded_helpers[0][kind]([
            upload("credentials/first.json", 7, unread=True),
            upload("credentials/second.json", 6, unread=True),
        ]))
    assert error.value.status_code == 413


@pytest.mark.parametrize("kind", ["quick", "documents", "obsidian"])
def test_exact_upload_limits_accept_skips_without_double_counting(guarded_helpers, kind):
    result = asyncio.run(guarded_helpers[0][kind]([
        upload("credentials/config.json", 8, unread=True), upload("notes.md", 4, b"note"),
    ]))
    if kind == "quick":
        assert result["scanned"] == 2 and result["skipped"] == result["imported"] == 1
    else:
        assert result == [("credentials/config.json", b""), ("notes.md", b"note")]


@pytest.mark.parametrize("kind", ["quick", "documents", "obsidian"])
def test_ordinary_upload_without_declared_size_remains_bounded(guarded_helpers, kind):
    result = asyncio.run(guarded_helpers[0][kind]([upload("notes.md", None, b"note")]))
    if kind == "quick":
        assert result["imported"] == 1 and result["skipped"] == 0
    else:
        assert result == [("notes.md", b"note")]


@pytest.mark.parametrize("kind", ["quick", "documents", "obsidian"])
@pytest.mark.parametrize("size", [None, 1])
def test_ordinary_upload_read_cannot_exceed_limits_despite_metadata(guarded_helpers, kind, size):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as error:
        asyncio.run(guarded_helpers[0][kind]([upload("notes.md", size, b"123456789")]))
    assert error.value.status_code == 413


@pytest.mark.parametrize("kind", ["quick", "documents", "obsidian"])
@pytest.mark.parametrize("size", [None, -1, True, "9"])
def test_excluded_upload_unknown_or_invalid_size_fails_closed(guarded_helpers, kind, size):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as error:
        asyncio.run(guarded_helpers[0][kind]([upload("credentials/config.json", size, unread=True)]))
    assert error.value.status_code == 422


@pytest.mark.parametrize("kind", ["quick", "documents"])
@pytest.mark.parametrize("budget", ["per-file", "batch"])
def test_actual_multipart_transport_rejects_spooled_excluded_excess(
    guarded_helpers, monkeypatch, kind, budget,
):
    httpx = pytest.importorskip("httpx")
    from fastapi import FastAPI
    from starlette.datastructures import UploadFile
    from engraphis import dashboard_app
    from engraphis.routes import v2_api

    async def forbidden(*args, **kwargs):
        pytest.fail("multipart excluded upload was read")

    monkeypatch.setattr(UploadFile, "read", forbidden)
    app = FastAPI()
    if kind == "quick":
        app.include_router(v2_api.router)
        path = "/api/workspaces/import-files"
        headers = {}
        fields = {"workspace": "sources"}
    else:
        production_app, route = guarded_helpers[1:]
        production_app.state.review_csrf_tokens[dashboard_app._OPEN_MODE_CSRF_KEY] = "synthetic-nonce"
        app.router.routes.append(route)
        path = route.path
        headers = {"X-Engraphis-Review-CSRF": "synthetic-nonce"}
        fields = {"workspace": "sources", "source_label": "Synthetic sources"}

    async def request():
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
            files = [
                ("files", ("credentials/config.json", b"123456789", "application/json")),
            ] if budget == "per-file" else [
                ("files", ("credentials/first.json", b"1234567", "application/json")),
                ("files", ("credentials/second.json", b"123456", "application/json")),
            ]
            return await client.post(path, data=fields, headers=headers, files=files)

    response = asyncio.run(request())
    assert response.status_code == 413, response.text
