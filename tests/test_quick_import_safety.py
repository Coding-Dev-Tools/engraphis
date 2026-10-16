"""Quick imports must exclude credential paths before reading or deriving content."""
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from engraphis.backends import resources
from engraphis.service import MemoryService, _iter_import_files


BLOCKED_PATHS = [
    ".env", "project/.env.production", "project/CREDENTIALS.json", "id_rsa",
    "keys/client.pem", "project\\id_ed25519", "project/.aws/config",
    "project/node_modules/library/readme.md", "config/recovery-codes.txt",
    ".netrc", "_netrc", ".npmrc", ".pypirc", ".git-credentials", ".pgpass", "pgpass.conf",
]

CREDENTIAL_RC_CASES = {
    ".netrc": "machine example.invalid login test_user password synthetic_password",
    "_netrc": "machine example.invalid login test_user password synthetic_password",
    ".npmrc": "//registry.example.invalid/:_authToken=synthetic_registry_credential",
    ".pypirc": "[pypi]\nusername = test_user\npassword = synthetic_password\n",
    ".git-credentials": "https://test_user:synthetic_password@example.invalid",
    ".pgpass": "example.invalid:5432:test_db:test_user:synthetic_password",
    "pgpass.conf": "example.invalid:5432:test_db:test_user:synthetic_password",
}

SAFE_DOTFILES = {
    ".gitignore": "build/\n*.pyc\n",
    ".editorconfig": "root = true\n[*]\nindent_style = space\n",
}


@pytest.fixture
def service():
    svc = MemoryService.create(":memory:", graph_extractor="none")
    try:
        yield svc
    finally:
        svc.store.close()


@pytest.mark.parametrize("name", CREDENTIAL_RC_CASES)
@pytest.mark.parametrize("uppercase", [False, True])
def test_shared_document_policy_blocks_standard_credential_rc_names(name, uppercase):
    from engraphis.core.documents import _sensitive_filename

    assert _sensitive_filename(name.upper() if uppercase else name)


@pytest.mark.parametrize("name", SAFE_DOTFILES)
def test_shared_document_policy_preserves_safe_dotfiles(name):
    from engraphis.core.documents import _sensitive_filename

    assert not _sensitive_filename(name)


@pytest.mark.parametrize("name,content", CREDENTIAL_RC_CASES.items())
def test_real_credential_rc_uploads_stop_before_extraction_and_derivation(
    service, monkeypatch, name, content,
):
    def forbidden(*args, **kwargs):
        pytest.fail("credential rc content reached extraction or derivation")

    monkeypatch.setattr(
        resources, "get_resource_extractor", lambda: SimpleNamespace(extract_bytes=forbidden),
    )
    monkeypatch.setattr(service, "_derive_import_facts", forbidden)
    report = service.import_files(
        workspace="safe-import", derive_facts=True,
        files=[{"name": f"project/{name}", "content": content}],
    )

    assert report["skipped"] == report["scanned"] == 1
    assert report["imported"] == report["errors"] == report["derived_facts"] == 0
    assert service.store.list_memories() == []
    assert "synthetic_password" not in repr(report)
    assert "synthetic_registry_credential" not in repr(report)


@pytest.mark.parametrize("name,content", CREDENTIAL_RC_CASES.items())
def test_real_credential_rc_folders_preserve_safe_dotfiles_and_derivation(
    service, monkeypatch, tmp_path, name, content,
):
    (tmp_path / name).write_text(content, encoding="utf-8")
    for safe_name, safe_content in SAFE_DOTFILES.items():
        (tmp_path / safe_name).write_text(safe_content, encoding="utf-8")
    monkeypatch.setenv("ENGRAPHIS_IMPORT_ROOTS", str(tmp_path))
    original_extractor = resources.get_resource_extractor()
    extracted = []
    derived = []

    def extract_path(path):
        assert Path(path).name in SAFE_DOTFILES
        extracted.append(Path(path).name)
        return original_extractor.extract_path(path)

    def derive(text, **kwargs):
        assert kwargs["resource_name"] in SAFE_DOTFILES
        assert text.strip() == SAFE_DOTFILES[kwargs["resource_name"]].strip()
        derived.append(kwargs["resource_name"])
        return 0, ""

    monkeypatch.setattr(
        resources, "get_resource_extractor", lambda: SimpleNamespace(extract_path=extract_path),
    )
    monkeypatch.setattr(service, "_derive_import_facts", derive)
    files, matched, unreadable = _iter_import_files(tmp_path, "*", len(SAFE_DOTFILES))
    assert {path.name for path in files} == set(SAFE_DOTFILES)
    assert matched == len(SAFE_DOTFILES)
    assert unreadable == 0

    report = service.import_folder(
        workspace="safe-import", path=str(tmp_path), file_pattern="*", derive_facts=True,
    )

    assert set(extracted) == set(derived) == set(SAFE_DOTFILES)
    assert report["imported"] == report["matched_total"] == report["scanned"] == 2
    assert report["errors"] == report["derived_facts"] == 0
    assert {record.content.strip() for record in service.store.list_memories()} == {
        safe_content.strip() for safe_content in SAFE_DOTFILES.values()
    }


def test_safe_dotfile_uploads_remain_importable_and_can_derive(service, monkeypatch):
    derived = []

    def derive(text, **kwargs):
        derived.append((kwargs["resource_name"], text))
        return 0, ""

    monkeypatch.setattr(service, "_derive_import_facts", derive)
    report = service.import_files(
        workspace="safe-import", derive_facts=True,
        files=[{"name": name, "content": content} for name, content in SAFE_DOTFILES.items()],
    )

    assert report["imported"] == report["scanned"] == len(SAFE_DOTFILES)
    assert report["skipped"] == report["errors"] == 0
    assert {name for name, _ in derived} == set(SAFE_DOTFILES)


@pytest.mark.parametrize("name", BLOCKED_PATHS)
def test_upload_excludes_credentials_before_extraction_and_derivation(service, monkeypatch, name):
    def forbidden(*args, **kwargs):
        pytest.fail("an excluded upload reached extraction or fact derivation")

    monkeypatch.setattr(
        resources, "get_resource_extractor",
        lambda: SimpleNamespace(extract_bytes=forbidden),
    )
    monkeypatch.setattr(service, "_derive_import_facts", forbidden)
    report = service.import_files(
        workspace="safe-import", derive_facts=True,
        files=[{"name": name, "content": "MAIL_URL=smtp://demo:demo@example.invalid:587"}],
    )

    assert report["scanned"] == 1
    assert report["skipped"] == 1
    assert report["imported"] == report["errors"] == report["derived_facts"] == 0
    assert service.store.list_memories() == []
    assert "smtp://" not in repr(report)


def test_upload_keeps_safe_documents_in_a_mixed_selection(service):
    report = service.import_files(workspace="safe-import", files=[
        {"name": ".env", "content": "MAIL_URL=smtp://demo:demo@example.invalid:587"},
        {"name": "project/readme.txt", "content": "Release notes for the local memory engine."},
    ])

    assert report["imported"] == report["skipped"] == 1
    assert report["errors"] == 0
    assert [record.content for record in service.store.list_memories()] == [
        "Release notes for the local memory engine.",
    ]


def test_folder_excludes_credentials_before_extraction_and_file_cap(service, monkeypatch, tmp_path):
    for name in [".env", "credentials.json", "id_rsa", "config/client.key", ".aws/config"]:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("MAIL_URL=smtp://demo:demo@example.invalid:587", encoding="utf-8")
    safe = tmp_path / "readme.txt"
    safe.write_text("Release notes for the local memory engine.", encoding="utf-8")
    monkeypatch.setenv("ENGRAPHIS_IMPORT_ROOTS", str(tmp_path))
    extractor = resources.get_resource_extractor()
    extracted = []

    def extract_path(path):
        assert Path(path) == safe
        extracted.append(path)
        return extractor.extract_path(path)

    monkeypatch.setattr(
        resources, "get_resource_extractor", lambda: SimpleNamespace(extract_path=extract_path),
    )
    files, matched, unreadable = _iter_import_files(tmp_path, "*", 1)
    assert files == [safe]
    assert matched == 1
    assert unreadable == 0

    report = service.import_folder(workspace="safe-import", path=str(tmp_path), file_pattern="*")

    assert extracted == [str(safe)]
    assert report["imported"] == report["matched_total"] == report["scanned"] == 1
    assert report["errors"] == 0
    assert [record.content for record in service.store.list_memories()] == [
        "Release notes for the local memory engine.",
    ]


def test_folder_checks_both_symlink_name_and_target(tmp_path):
    private = tmp_path / ".env"
    private.write_text("MAIL_URL=smtp://demo:demo@example.invalid:587", encoding="utf-8")
    safe = tmp_path / "readme.txt"
    safe.write_text("Release notes.", encoding="utf-8")
    try:
        (tmp_path / "notes.txt").symlink_to(private)
        (tmp_path / "credentials.json").symlink_to(safe)
    except (OSError, NotImplementedError):
        pytest.skip("file symlinks are unavailable")

    files, matched, unreadable = _iter_import_files(tmp_path, "*", 10)

    assert files == [safe]
    assert matched == 1
    assert unreadable == 0


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for upload behavior")
def test_browser_never_uploads_excluded_files_from_a_mixed_selection(tmp_path):
    script = tmp_path / "quick-import.cjs"
    script.write_text(r"""
const assert = require('node:assert/strict');
const elements = new Map();
function element() {
  return {
    checked: false, classList: { toggle() {}, remove() {}, add() {} },
    addEventListener() {}, setAttribute() {}, replaceChildren() {}, append() {},
  };
}
global.window = {};
global.document = {
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, element());
    return elements.get(id);
  },
  createElement: element,
};
require(process.argv[2]);
const calls = [];
const imports = window.EngraphisImports.create({
  api: async (url, options) => {
    calls.push({ url, names: options.body.getAll('files').map(file => file.name) });
    return { imported: 1 };
  },
  workspace: () => 'safe-import', notify() {}, onImported() {},
  confirmDerive: async () => { throw new Error('derivation was not requested'); },
});
const blocked = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const file = name => ({ name, file: new File(['local source material'], name.split('/').pop()) });
(async () => {
  await imports.upload(blocked.map(file));
  assert.deepEqual(calls, []);
  await imports.upload([...blocked.map(file), file('project/readme.txt'), file('.gitignore'), file('.editorconfig')]);
  assert.deepEqual(calls, [{
    url: '/workspaces/import-files', names: ['project/readme.txt', '.gitignore', '.editorconfig'],
  }]);
})().catch(error => { console.error(error); process.exitCode = 1; });
""", encoding="utf-8")
    source = Path(__file__).resolve().parents[1] / "engraphis/dashboard_assets/import-sources.js"
    completed = subprocess.run(
        ["node", str(script), str(source)], input=json.dumps(BLOCKED_PATHS + [name.upper() for name in CREDENTIAL_RC_CASES]),
        text=True, capture_output=True, timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
