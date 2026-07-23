"""The remote code-index endpoint is confined to one operator-owned root."""
import os

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from engraphis.routes import v2_api


class _IndexService:
    def __init__(self):
        self.calls = []

    def index_repo(self, **kwargs):
        self.calls.append(kwargs)
        return {"files_indexed": 0}


@pytest.fixture()
def indexed_client(monkeypatch):
    service = _IndexService()
    v2_api.set_service(service)
    app = FastAPI()
    app.include_router(v2_api.router)
    yield TestClient(app), service
    v2_api._service = None


def _request(client, root_path):
    return client.post("/api/code/index", json={
        "workspace": "acme", "repo": "sample", "root_path": root_path,
    })


def _canonical_with_sep(path):
    return os.path.normcase(os.path.realpath(path)).rstrip(os.sep) + os.sep


def test_code_index_uses_configured_http_root_for_relative_paths(
    indexed_client, monkeypatch, tmp_path,
):
    client, service = indexed_client
    root = tmp_path / "operator-root"
    target = root / "project"
    target.mkdir(parents=True)
    monkeypatch.setenv("ENGRAPHIS_HTTP_INDEX_ROOT", str(root))

    response = _request(client, "project")

    assert response.status_code == 200
    assert service.calls[0]["root_path"] == _canonical_with_sep(target)


def test_code_index_allows_absolute_path_only_inside_http_root(indexed_client, monkeypatch, tmp_path):
    client, service = indexed_client
    root = tmp_path / "operator-root"
    target = root / "project"
    target.mkdir(parents=True)
    monkeypatch.setenv("ENGRAPHIS_HTTP_INDEX_ROOT", str(root))

    response = _request(client, str(target))

    assert response.status_code == 200
    assert service.calls[0]["root_path"] == _canonical_with_sep(target)


def test_code_index_rejects_outside_and_prefix_sibling_paths(indexed_client, monkeypatch, tmp_path):
    client, service = indexed_client
    root = tmp_path / "operator-root"
    sibling = tmp_path / "operator-root-copy"
    root.mkdir()
    sibling.mkdir()
    monkeypatch.setenv("ENGRAPHIS_HTTP_INDEX_ROOT", str(root))

    for path in ("../operator-root-copy", str(sibling)):
        response = _request(client, path)
        assert response.status_code == 400
        assert response.json() == {"detail": {"error": "invalid request"}}
    assert service.calls == []


def test_code_index_rejects_symlink_escape(indexed_client, monkeypatch, tmp_path):
    client, service = indexed_client
    root = tmp_path / "operator-root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    link = root / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks are unavailable in this environment")
    monkeypatch.setenv("ENGRAPHIS_HTTP_INDEX_ROOT", str(root))

    assert _request(client, "escape").status_code == 400
    assert service.calls == []


def test_code_index_uses_first_engine_root_when_http_root_is_unset(indexed_client, monkeypatch, tmp_path):
    client, service = indexed_client
    first = tmp_path / "first"
    second = tmp_path / "second"
    target = first / "project"
    target.mkdir(parents=True)
    second.mkdir()
    monkeypatch.delenv("ENGRAPHIS_HTTP_INDEX_ROOT", raising=False)
    monkeypatch.setenv("ENGRAPHIS_INDEX_ROOTS", os.pathsep.join((str(first), str(second))))

    response = _request(client, "project")

    assert response.status_code == 200
    assert service.calls[0]["root_path"] == _canonical_with_sep(target)


@pytest.mark.parametrize(
    ("http_root", "engine_roots"),
    [
        ("relative-root", None),
        (None, "relative-root"),
    ],
)
def test_code_index_rejects_relative_operator_root_configuration(
    indexed_client, monkeypatch, http_root, engine_roots,
):
    client, service = indexed_client
    if http_root is None:
        monkeypatch.delenv("ENGRAPHIS_HTTP_INDEX_ROOT", raising=False)
    else:
        monkeypatch.setenv("ENGRAPHIS_HTTP_INDEX_ROOT", http_root)
    if engine_roots is None:
        monkeypatch.delenv("ENGRAPHIS_INDEX_ROOTS", raising=False)
    else:
        monkeypatch.setenv("ENGRAPHIS_INDEX_ROOTS", engine_roots)

    response = _request(client, "project")

    assert response.status_code == 500
    assert response.json() == {"detail": {"error": "internal server error"}}
    assert service.calls == []


def test_code_index_uses_current_directory_without_operator_root_configuration(indexed_client, monkeypatch):
    client, service = indexed_client
    monkeypatch.delenv("ENGRAPHIS_HTTP_INDEX_ROOT", raising=False)
    monkeypatch.delenv("ENGRAPHIS_INDEX_ROOTS", raising=False)

    response = _request(client, ".")

    assert response.status_code == 200
    assert service.calls[0]["root_path"] == _canonical_with_sep(os.getcwd())
