"""Synthetic directories containing filename-only credential substrings stay usable."""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from engraphis.core.documents import parse_document, scan_document_tree
from engraphis.core.obsidian import parse_obsidian_note, scan_obsidian_vault
from engraphis.core.source_paths import sensitive_path
from engraphis.document_import import scan_document_upload
from engraphis.obsidian_import import scan_obsidian_upload
from engraphis.service import MemoryService, _iter_import_files, _resolve_import_root


BENIGN = ("tokenizer-project", "secretary-notes", "credentialing")
CONTENT = b"# Release notes\nReadable synthetic project documentation."


@pytest.fixture
def service():
    instance = MemoryService.create(":memory:", extractor="none", graph_extractor="none")
    try:
        yield instance
    finally:
        instance.close()


@pytest.mark.parametrize("name", BENIGN)
@pytest.mark.parametrize("selected", [True, False])
@pytest.mark.parametrize("kind", ["quick", "documents", "obsidian"])
def test_benign_substring_selected_roots_and_ancestors_are_importable(
    service, monkeypatch, tmp_path_factory, name, selected, kind,
):
    base = tmp_path_factory.mktemp("source-policy") / name
    root = base if selected else base / "docs"
    root.mkdir(parents=True)
    (root / "readme.md").write_bytes(CONTENT)
    monkeypatch.setenv("ENGRAPHIS_IMPORT_ROOTS", str(base.parent))
    if kind == "quick":
        report = service.import_folder(workspace="sources", path=str(root))
        assert report["imported"] == 1 and report["skipped"] == report["errors"] == 0
    elif kind == "documents":
        assert [row.relative_path for row in scan_document_tree(root).documents] == ["readme.md"]
    else:
        assert [row.relative_path for row in scan_obsidian_vault(root).notes] == ["readme.md"]


@pytest.mark.parametrize("kind", ["quick", "documents", "obsidian"])
def test_walk_classifies_ordinary_directories_before_broad_file_rules(
    monkeypatch, tmp_path_factory, kind,
):
    root = tmp_path_factory.mktemp("source-policy")
    for name in BENIGN:
        (root / name).mkdir()
        (root / name / "readme.md").write_bytes(CONTENT)
    excluded = ("credentials", "secrets", "tokens", "recovery_codes", "id_ed25519", "client.key")
    for name in excluded:
        (root / name).mkdir()
        (root / name / "readme.md").write_bytes(CONTENT)
    # These are actual files, so the intentionally broad basename policy remains.
    for name in BENIGN:
        (root / f"{name}.md").write_bytes(CONTENT)
    original = Path.lstat
    def metadata(path, *args, **kwargs):
        if path.name in excluded:
            pytest.fail("known sensitive directory reached metadata")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "lstat", metadata)
    expected = [f"{name}/readme.md" for name in sorted(BENIGN)]
    if kind == "quick":
        files, matched, unreadable = _iter_import_files(root, "*.md", 2)
        assert [path.relative_to(root).as_posix() for path in files] == expected[:2]
        assert matched == 3 and unreadable == 0
    elif kind == "documents":
        assert [row.relative_path for row in scan_document_tree(root).documents] == expected
    else:
        assert [row.relative_path for row in scan_obsidian_vault(root).notes] == expected


@pytest.mark.parametrize("name", BENIGN)
@pytest.mark.parametrize("kind", ["quick", "documents", "obsidian", "document-parser", "note-parser"])
def test_uploaded_and_direct_file_paths_allow_benign_directory_substrings(service, name, kind):
    path = f"{name}/readme.md"
    assert not sensitive_path(path)
    if kind == "quick":
        assert service.import_files(workspace="sources", files=[{"name": path, "data": CONTENT}])["imported"] == 1
    elif kind == "documents":
        assert len(scan_document_upload([(path, CONTENT)], source_label="Fixtures").documents) == 1
    elif kind == "obsidian":
        assert len(scan_obsidian_upload([(path, CONTENT)], vault_label="Fixtures").notes) == 1
    elif kind == "document-parser":
        assert parse_document(CONTENT, path).relative_path == path
    else:
        assert parse_obsidian_note(CONTENT, path).relative_path == path


@pytest.mark.parametrize("name", BENIGN)
def test_broad_actual_file_basenames_still_rejected_before_read(service, monkeypatch, name):
    path = f"docs/{name}.md"
    assert sensitive_path(path)
    def forbidden(*args, **kwargs):
        pytest.fail("excluded file reached extraction")
    monkeypatch.setattr("engraphis.backends.resources.get_resource_extractor",
                        lambda: SimpleNamespace(extract_bytes=forbidden))
    assert service.import_files(workspace="sources", files=[{"name": path, "data": CONTENT}])["skipped"] == 1
    assert not scan_document_upload([(path, CONTENT)], source_label="Fixtures").documents
    assert not scan_obsidian_upload([(path, CONTENT)], vault_label="Fixtures").notes
    with pytest.raises(ValueError, match="sensitive"):
        parse_document(CONTENT, path)
    with pytest.raises(ValueError, match="sensitive"):
        parse_obsidian_note(CONTENT, path)


@pytest.mark.parametrize("name", ["credential", "token", "recoverycodes", "recovery-code",
                                  "Recovery Codes", ".credentials", ".tokens"])
def test_exact_credential_directory_spellings_still_stop_before_metadata(monkeypatch, name):
    def forbidden(*args, **kwargs):
        pytest.fail("known sensitive directory reached filesystem inspection")
    monkeypatch.setattr(os.path, "realpath", forbidden)
    monkeypatch.setattr(Path, "lstat", forbidden)
    monkeypatch.setattr(Path, "resolve", forbidden)
    for scanner in (_resolve_import_root, scan_document_tree, scan_obsidian_vault):
        with pytest.raises(ValueError, match="sensitive"):
            scanner("/synthetic/home/" + name)


def test_safe_symlink_targets_below_ordinary_substring_directories_remain_contained(tmp_path_factory):
    root = tmp_path_factory.mktemp("source-policy")
    target = root / "tokenizer-project" / "readme.md"
    target.parent.mkdir()
    target.write_bytes(CONTENT)
    alias = root / "secretary-notes" / "alias.md"
    alias.parent.mkdir()
    try:
        alias.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    files, matched, unreadable = _iter_import_files(root, "*.md", 2)
    assert files == [target, target] and matched == 2 and unreadable == 0
    outside = root.parent / "outside.md"
    outside.write_bytes(CONTENT)
    (alias.parent / "outside.md").symlink_to(outside)
    files, matched, unreadable = _iter_import_files(root, "*.md", 2)
    assert files == [target, target] and matched == 2 and unreadable == 1
