"""Only synthetic sources: exclude credential ancestors before traversal or reads."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from urllib.parse import quote

from engraphis.core import source_paths
from engraphis.core.documents import parse_document, scan_document_tree
from engraphis.core.obsidian import parse_obsidian_note, scan_obsidian_vault
from engraphis.core.secrets import SecretDetectedError, redact_secrets, secret_kind
from engraphis.core.sync import SyncEngine, dict_to_record
from engraphis.document_import import scan_document_upload
from engraphis.obsidian_import import scan_obsidian_upload
from engraphis.service import MemoryService, ValidationError, _iter_import_files, _resolve_import_root, _skip_import_path


SYNTHETIC_URI = "smtp://fixture_user:obviously_fake_mail_password@mail.example.invalid:587"
EXCLUDED = [
    "credentials/config.json", "project/SeCrEtS/mail.json", r"project\ToKeNs\note.md",
    "/synthetic/credentials/config.json", r"C:\synthetic\Credentials\config.json",
    r"\\synthetic-host\share\Secrets\config.json",
    "project/client.key/config.txt", "project/.NETRC/settings.md",
    "project/recovery_codes/notes.md", "id_ed25519/mail.json",
    "credentials/../readme.txt", "project/.env.production/config.json",
]


@pytest.fixture
def service():
    svc = MemoryService.create(":memory:", graph_extractor="none")
    try:
        yield svc
    finally:
        svc.close()


@pytest.mark.parametrize("name", EXCLUDED)
def test_quick_import_excludes_each_named_component(name):
    assert _skip_import_path(name)


@pytest.mark.parametrize("name", ["docs/mail.json", "config/readme.md", "examples/connection.txt",
                                  ".gitignore", ".editorconfig", "project/build-notes.md"])
def test_quick_import_preserves_benign_names(name):
    assert not _skip_import_path(name)


@pytest.mark.parametrize("name", EXCLUDED)
def test_quick_upload_blocks_before_extraction_and_derivation(service, monkeypatch, name):
    def forbidden(*args, **kwargs):
        pytest.fail("excluded source reached an extractor")
    monkeypatch.setattr("engraphis.backends.resources.get_resource_extractor",
                        lambda: SimpleNamespace(extract_bytes=forbidden))
    monkeypatch.setattr(service, "_derive_import_facts", forbidden)
    report = service.import_files(workspace="sources", derive_facts=True,
                                  files=[{"name": name, "content": SYNTHETIC_URI}])
    assert report["scanned"] == report["skipped"] == 1
    assert report["imported"] == report["errors"] == report["derived_facts"] == 0
    assert service.store.count_memories() == 0
    assert "obviously_fake_mail_password" not in repr(report)


@pytest.mark.parametrize("path", ["/synthetic/credentials", "/synthetic/Secrets/notes",
                                  r"C:\synthetic\ToKeNs\project"])
@pytest.mark.parametrize("scanner", [_resolve_import_root, scan_document_tree, scan_obsidian_vault])
def test_selected_root_is_rejected_before_filesystem_access(monkeypatch, path, scanner):
    def forbidden(*args, **kwargs):
        pytest.fail("excluded selected root touched the filesystem")
    with monkeypatch.context() as guard:
        guard.setattr(os.path, "realpath", forbidden)
        guard.setattr(os, "lstat", forbidden)
        guard.setattr(Path, "resolve", forbidden)
        with pytest.raises(ValueError, match="sensitive"):
            scanner(path)


@pytest.mark.parametrize("scanner", ["quick", "documents", "obsidian"])
def test_walk_prunes_named_directories_before_metadata_or_descent(tmp_path_factory, monkeypatch, scanner):
    root = tmp_path_factory.mktemp("source-fixtures")
    safe = root / "notes.md"
    safe.write_text("# Release notes\nLocal readable notes.")
    for selected in ("credentials", "SeCrEtS", "tokens", "client.key", "id_ed25519"):
        target = root / selected / "config.md"
        target.parent.mkdir()
        target.write_text(SYNTHETIC_URI)
    excluded = {"credentials", "secrets", "tokens", "client.key", "id_ed25519"}
    for method in ("stat", "lstat", "resolve", "iterdir", "open", "read_bytes"):
        original = getattr(Path, method)
        def guarded(path, *args, _original=original, **kwargs):
            if any(part.casefold() in excluded for part in path.parts):
                pytest.fail("excluded descendant reached metadata, traversal or read")
            return _original(path, *args, **kwargs)
        monkeypatch.setattr(Path, method, guarded)
    if scanner == "quick":
        files, matched, unreadable = _iter_import_files(root, "*", 1)
        assert files == [safe] and matched == 1 and unreadable == 0
    elif scanner == "documents":
        report = scan_document_tree(root)
        assert [record.relative_path for record in report.documents] == ["notes.md"]
        assert report.complete
    else:
        report = scan_obsidian_vault(root)
        assert [record.relative_path for record in report.notes] == ["notes.md"]
        assert report.complete


def test_quick_iterator_rejects_a_sensitive_selected_root_before_listing(monkeypatch):
    monkeypatch.setattr(Path, "iterdir", lambda *args: pytest.fail("excluded root was listed"))
    assert _iter_import_files(Path("/synthetic/credentials"), "*", 10) == ([], 0, 0)


@pytest.mark.parametrize("parser,path", [(parse_document, "credentials/config.json"),
                                        (parse_obsidian_note, "secrets/notes.md")])
def test_direct_parser_rejects_sensitive_ancestors(parser, path):
    with pytest.raises(ValueError, match="sensitive"):
        parser(b"Safe-looking readable document.", path)


@pytest.mark.parametrize("scanner", [scan_document_upload, scan_obsidian_upload])
def test_browser_source_scanners_reject_ancestors_before_parsing(monkeypatch, scanner):
    module = "engraphis.document_import" if scanner is scan_document_upload else "engraphis.obsidian_import"
    parser = "parse_document" if scanner is scan_document_upload else "parse_obsidian_note"
    monkeypatch.setattr(module + "." + parser, lambda *args, **kwargs: pytest.fail("excluded bytes were parsed"))
    kwargs = {"source_label": "Fixtures"} if scanner is scan_document_upload else {"vault_label": "Fixtures"}
    report = scanner([("credentials/config.md", SYNTHETIC_URI.encode())], **kwargs)
    records = report.documents if scanner is scan_document_upload else report.notes
    assert not records and len(report.rejected) == 1


def test_benign_hidden_allowed_root_and_symlink_target_policy(tmp_path_factory, monkeypatch):
    root = tmp_path_factory.mktemp("root-fixture") / ".worktrees" / "checkout"
    root.mkdir(parents=True)
    safe = root / "readme.md"
    safe.write_text("Readable project notes.")
    private = root / "credentials"
    private.mkdir()
    (private / "config.md").write_text(SYNTHETIC_URI)
    monkeypatch.setenv("ENGRAPHIS_IMPORT_ROOTS", str(root))
    assert _resolve_import_root(str(root)) == root.resolve()
    try:
        (root / "alias.md").symlink_to(private / "config.md")
    except (OSError, NotImplementedError):
        pass
    files, matched, unreadable = _iter_import_files(root, "*", 5)
    assert files == [safe] and matched == 1 and unreadable == 0
    assert [row.relative_path for row in scan_document_tree(root).documents] == ["readme.md"]
    assert [row.relative_path for row in scan_obsidian_vault(root).notes] == ["readme.md"]


def test_quick_walk_keeps_alphabetical_cap_total_and_unreadable_count(tmp_path_factory, monkeypatch):
    root = tmp_path_factory.mktemp("enumeration-fixtures")
    (root / "a").mkdir()
    for name in ("b.md", "a/z.md", "a.md"):
        (root / name).write_text("Readable notes.")
    (root / "blocked").mkdir()
    original = Path.iterdir
    def listing(path):
        if path.name == "blocked":
            raise OSError("synthetic unreadable directory")
        return original(path)
    monkeypatch.setattr(Path, "iterdir", listing)
    files, matched, unreadable = _iter_import_files(root, "*.md", 1)
    # Path ordering compares components, preserving the original rglob prefix.
    assert files == [root / "a/z.md"] and matched == 3 and unreadable == 1


def test_quick_walk_counts_cyclic_file_symlink_as_unreadable(tmp_path_factory):
    root = tmp_path_factory.mktemp("source-fixtures")
    safe = root / "notes.md"
    safe.write_text("Readable project notes.")
    try:
        (root / "loop.md").symlink_to("loop.md")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    files, matched, unreadable = _iter_import_files(root, "*.md", 10)
    assert files == [safe] and matched == 1 and unreadable == 1


@pytest.mark.parametrize("scheme", ["smtp", "smtps", "imap", "pop3s", "https", "sftp", "custom+mail"])
def test_authenticated_uris_are_rejected_and_redactable_for_all_schemes(scheme):
    content = SYNTHETIC_URI.replace("smtp://", scheme + "://")
    assert secret_kind(content) == "credential-bearing connection URI"
    redacted = redact_secrets(content)
    assert "obviously_fake_mail_password" not in redacted
    assert secret_kind(redacted) is None
    assert redact_secrets(redacted) == redacted


@pytest.mark.parametrize("content", ["smtp://mail.example.invalid:587", "mailto:user@example.invalid",
                                     "smtp://fixture_user@mail.example.invalid", "smtp://user:@mail.example.invalid",
                                     "https://example.invalid/docs/user:password@example.invalid",
                                     "smtps://user:<redacted>@mail.example.invalid",
                                     "smtp://user:withheld@mail.example.invalid"])
def test_benign_and_redacted_urls_remain_safe(content):
    assert secret_kind(content) is None


@pytest.mark.parametrize("userinfo", ["user:p", "user:demo", "fixture%40user:p%2Fq", "user:%70"])
def test_short_and_percent_encoded_uri_passwords_are_credentials(userinfo):
    value = f"smtp://{userinfo}@mail.example.invalid"
    assert secret_kind(value) == "credential-bearing connection URI"
    safe = redact_secrets(value)
    assert userinfo not in safe and secret_kind(safe) is None
    assert redact_secrets(safe) == safe


@pytest.mark.parametrize("scheme", ["smtp", "https"])
@pytest.mark.parametrize("userinfo", [":p", "fixture:p'ass", "f'ix:p"])
def test_empty_username_and_apostrophe_authentication_cannot_be_captured(service, scheme, userinfo):
    value = f"{scheme}://{userinfo}@mail.example.invalid"
    assert secret_kind(value) == "credential-bearing connection URI"
    safe = redact_secrets(value)
    assert userinfo not in safe and secret_kind(safe) is None
    assert redact_secrets(safe) == safe
    with pytest.raises(ValidationError, match="credential-bearing connection URI"):
        service.remember(value, workspace="sources")
    assert service.store.count_memories() == 0


@pytest.mark.parametrize("symbol", list("!$&'()*+,;=:@"))
@pytest.mark.parametrize("encoded", [False, True])
def test_userinfo_subdelimiters_and_percent_encodings_remain_credential_shaped(symbol, encoded):
    password = "f" + (quote(symbol, safe="") if encoded else symbol) + "p"
    value = f"smtp://fixture:{password}@mail.example.invalid"
    assert secret_kind(value) == "credential-bearing connection URI"
    safe = redact_secrets(value)
    assert password not in safe and secret_kind(safe) is None
    assert redact_secrets(safe) == safe


def test_long_scheme_and_many_scheme_near_misses_stay_bounded():
    import time
    value = "custom-" * 1_000 + "://fixture:p@mail.example.invalid"
    assert secret_kind(value) == "credential-bearing connection URI"
    assert secret_kind(redact_secrets(value)) is None
    started = time.monotonic()
    assert secret_kind("nonscheme." * 20_000) is None
    assert time.monotonic() - started < 3


def test_uri_scanning_stays_bounded_for_many_incomplete_authorities():
    import time
    content = "custom://fixture:missing_authentication " * 20_000
    started = time.monotonic()
    assert secret_kind(content) is None
    assert redact_secrets(content) == content
    assert time.monotonic() - started < 3


def test_authenticated_uri_capture_ingest_and_sync_fail_before_writes_or_extractors(service, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("credential input reached a provider or persistence")
    monkeypatch.setattr(service.engine, "extractor", SimpleNamespace(extract=forbidden))
    monkeypatch.setattr(service.engine.embedder, "embed", forbidden)
    for capture in (lambda: service.remember(SYNTHETIC_URI, workspace="sources"),
                    lambda: service.ingest(SYNTHETIC_URI, workspace="sources"),
                    lambda: service.record_event("observation", SYNTHETIC_URI, workspace="sources")):
        with pytest.raises(ValidationError, match="credential-bearing connection URI") as error:
            capture()
        assert "obviously_fake_mail_password" not in str(error.value)
    wid = service.store.get_or_create_workspace("sources")
    with pytest.raises(SecretDetectedError, match="credential-bearing connection URI"):
        service.engine.ingest(SYNTHETIC_URI, workspace_id=wid)
    row = {"id": "mem_synthetic_peer", "content": SYNTHETIC_URI, "scope": "workspace", "mtype": "semantic"}
    assert dict_to_record(row) is None
    report = SyncEngine(service.store).apply_bundle({
        "format": "engraphis-sync", "version": 2, "device_id": "synthetic-peer",
        "workspace_name": "sources", "repos": {}, "memories": [row], "mem_links": [],
    }, into_workspace="sources")
    assert report["rejected"] == 1 and service.store.count_memories() == 0
    assert "obviously_fake_mail_password" not in json.dumps(report)


def test_quick_http_upload_does_not_read_sensitive_file_bytes(service, monkeypatch):
    pytest.importorskip("fastapi", reason="HTTP upload guard requires the optional server extra")
    from engraphis.routes import v2_api
    async def forbidden(*args, **kwargs):
        pytest.fail("excluded upload bytes were read")
    async def inline(function, *args, **kwargs):
        return function(*args, **kwargs)
    monkeypatch.setattr(v2_api, "service", lambda: service)
    monkeypatch.setattr(v2_api, "run_in_threadpool", inline)
    report = asyncio.run(v2_api._import_uploaded_files(
        workspace="sources", memory_type="semantic", derive_facts=False,
        files=[SimpleNamespace(filename="credentials/config.json", size=0, read=forbidden)],
    ))
    assert report["skipped"] == report["scanned"] == 1 and report["imported"] == 0


@pytest.mark.parametrize("root", [
    r"\\synthetic-host\share\notes",
    "//synthetic-host/share/notes",
    r"\\?\C:\synthetic\notes",
    r"\\.\C:\synthetic\notes",
    r"  \\synthetic-host\share\notes",
])
def test_import_root_rejects_remote_or_device_paths_before_resolution(root, monkeypatch):
    """A UNC/device root must be refused before realpath opens an SMB session.

    ``os.path.realpath`` on ``\\\\host\\share`` contacts the remote host, so the
    ordering matters: the raw text has to be refused first, not after resolution.
    """
    calls = []
    real_realpath = os.path.realpath


    def guarded(path, *args, **kwargs):
        calls.append(str(path))
        return real_realpath(path, *args, **kwargs)

    monkeypatch.setattr(os.path, "realpath", guarded)
    with pytest.raises(ValidationError, match="local path"):
        _resolve_import_root(root)
    monkeypatch.undo()
    assert [call for call in calls if "synthetic-host" in call or root.startswith("\\\\?")] == []


def test_import_root_rejects_remote_allowed_root_entries(monkeypatch):
    """``ENGRAPHIS_IMPORT_ROOTS`` cannot widen the policy to a remote share either."""
    monkeypatch.setenv("ENGRAPHIS_IMPORT_ROOTS", r"\\synthetic-host\share")
    with pytest.raises(ValidationError, match="ENGRAPHIS_IMPORT_ROOTS"):
        _resolve_import_root(str(Path.home()))


def test_windows_creation_time_skew_is_not_a_content_change(monkeypatch):
    """Windows ``st_ctime`` is creation time and cannot detect a content change.

    Python 3.12 can report the same file's creation time about a millisecond
    apart depending on whether the metadata came from the directory entry or
    the open handle, which made ``read_local_source`` reject freshly written
    files at random. Size and mtime stay authoritative there; POSIX keeps the
    inode change time.
    """
    def stamp(*, size=10, mtime=1_000, ctime=2_000):
        return SimpleNamespace(st_size=size, st_mtime_ns=mtime, st_ctime_ns=ctime)

    monkeypatch.setattr(source_paths, "_CTIME_IS_CHANGE_TIME", False)
    assert not source_paths._stamp_changed(stamp(ctime=2_000), stamp(ctime=2_001))
    assert source_paths._stamp_changed(stamp(mtime=1_000), stamp(mtime=1_001))
    assert source_paths._stamp_changed(stamp(size=10), stamp(size=11))

    monkeypatch.setattr(source_paths, "_CTIME_IS_CHANGE_TIME", True)
    assert source_paths._stamp_changed(stamp(ctime=2_000), stamp(ctime=2_001))

