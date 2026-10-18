"""Mapped caches obey the existing strict source-checkout bytecode policy.

Fixtures compile only unchanged benign source and never import or execute it.
Cache headers and payloads are irrelevant: the policy rejects their presence.
"""
import importlib.util
import ntpath
import py_compile
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import check_release_readiness as checker


def _commit(repository):
    subprocess.run(["git", "add", "--force", "."], cwd=repository, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
         "commit", "--quiet", "-m", "benign source fixture"], cwd=repository, check=True,
    )


@pytest.fixture
def candidate(tmp_path):
    repository = tmp_path / "source [candidate]"
    repository.mkdir()
    (repository / ".gitignore").write_text("__pycache__/\n*.pyc\n*.pyo\n", encoding="utf-8")
    (repository / "engine.py").write_text("value = 'tracked'\n", encoding="utf-8")
    subprocess.run(["git", "init", "--quiet", str(repository)], check=True)
    _commit(repository)
    return repository


def _mapped(source, optimization=""):
    return Path(importlib.util.cache_from_source(str(source), optimization=optimization))


def _write_cache(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"Inert presence-only fixture; never imported or decoded.\n")


def _validate(candidate, tmp_path):
    components = {name: {"commit": "a" * 40} for name in checker.COMPONENTS}
    components["engine"]["commit"] = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=candidate, text=True,
    ).strip()
    return checker.validate(checker.new_ledger(components), tmp_path, engine_root=candidate)


@pytest.mark.parametrize("state", ["no-prefix", "missing", "empty"])
def test_clean_candidate_with_absent_or_empty_mapped_cache(candidate, tmp_path, monkeypatch, state):
    prefix = None if state == "no-prefix" else str(tmp_path / "cache")
    monkeypatch.setattr(sys, "pycache_prefix", prefix)
    if state == "empty":
        _mapped(candidate / "engine.py").parent.mkdir(parents=True)
    result = _validate(candidate, tmp_path)
    assert result["valid"]
    assert result["engine_checkout_verified"]
    assert not checker._ignored_executable_paths(candidate)


@pytest.mark.parametrize("mode", list(py_compile.PycInvalidationMode))
@pytest.mark.parametrize("optimization", ["", "1", "2"])
def test_all_benign_compiled_cache_headers_fail_closed(
    candidate, tmp_path, monkeypatch, mode, optimization,
):
    monkeypatch.setattr(sys, "pycache_prefix", str(tmp_path / "cache"))
    source = candidate / "engine.py"
    original = source.read_bytes()
    cache = _mapped(source, optimization)
    py_compile.compile(
        str(source), cfile=str(cache), doraise=True,
        invalidation_mode=mode, optimize=int(optimization or "0"),
    )
    assert source.read_bytes() == original == b"value = 'tracked'\n"
    result = _validate(candidate, tmp_path)
    assert not result["valid"]
    assert not result["engine_checkout_verified"]
    assert any("__pycache__" in error and cache.name in error for error in result["errors"])


@pytest.mark.parametrize("name", [
    "engine.cpython-399.pyc", "engine.pypy39.pyc", "engine.cpython-399.opt-1.pyc",
    "engine.cpython-399.opt-2.pyc", "engine.cpython-399.opt-42.pyc",
    "engine.cpython-399.pyo", "engine.pyc", "engine.pyo",
])
def test_other_tags_and_optimization_names_are_rejected(candidate, tmp_path, monkeypatch, name):
    monkeypatch.setattr(sys, "pycache_prefix", str(tmp_path / "cache"))
    cache = _mapped(candidate / "engine.py").with_name(name)
    _write_cache(cache)
    assert any(cache.name in entry for entry in checker._ignored_executable_paths(candidate))


@pytest.mark.parametrize("relative", ["nested/[module].py", "nested/deeper/module.name[2].pyw"])
def test_nested_literal_names_and_pyw_mapping(candidate, tmp_path, monkeypatch, relative):
    monkeypatch.setattr(sys, "pycache_prefix", str(tmp_path / "cache [literal]"))
    source = candidate / relative
    source.parent.mkdir(parents=True)
    source.write_text("value = 'tracked'\n", encoding="utf-8")
    _commit(candidate)
    cache = _mapped(source)
    _write_cache(cache)
    assert any(str(cache) in entry for entry in checker._ignored_executable_paths(candidate))


def test_relative_prefix_uses_runtime_mapping_from_process_cwd(candidate, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "pycache_prefix", "relative [cache]")
    cache = _mapped(candidate / "engine.py")
    assert not cache.is_absolute()
    _write_cache(cache)
    assert any(str(cache.absolute()) in entry for entry in checker._ignored_executable_paths(candidate))


def test_unrelated_cache_names_directories_and_payloads_are_not_read(candidate, tmp_path, monkeypatch):
    prefix = tmp_path / "cache"
    monkeypatch.setattr(sys, "pycache_prefix", str(prefix))
    directory = _mapped(candidate / "engine.py").parent
    directory.mkdir(parents=True)
    for name in ["other.cpython-312.pyc", "engine.sibling.cpython-312.pyc",
                 "engine.cpython-312.pyc.txt", "engine.notes.txt"]:
        _write_cache(directory / name)
    _write_cache(directory / "unrelated-profile" / "engine.cpython-312.pyc")
    _write_cache(prefix / "unrelated-repository" / "engine.cpython-312.pyc")
    scans = []
    original_scandir = checker.os.scandir
    original_open = Path.open

    def bounded_scandir(path):
        assert Path(path) == directory
        scans.append(str(path))
        return original_scandir(path)

    def no_cache_payload_open(path, *args, **kwargs):
        assert not path.is_relative_to(prefix)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(checker.os, "scandir", bounded_scandir)
    monkeypatch.setattr(Path, "open", no_cache_payload_open)
    assert not checker._ignored_executable_paths(candidate)
    assert scans == [str(directory)]


@pytest.mark.parametrize("relative,blocked", [
    ("venv/module.py", False), ("node_modules/module.pyw", False),
    ("build/package/module.py", False), ("build/__init__.py", True),
])
def test_existing_runtime_directory_exclusions_are_preserved(
    candidate, tmp_path, monkeypatch, relative, blocked,
):
    monkeypatch.setattr(sys, "pycache_prefix", str(tmp_path / "cache"))
    source = candidate / relative
    source.parent.mkdir(parents=True)
    source.write_text("value = 'tracked'\n", encoding="utf-8")
    _commit(candidate)
    cache = _mapped(source)
    _write_cache(cache)
    suspicious = checker._ignored_executable_paths(candidate)
    assert bool(suspicious) is blocked
    if blocked:
        assert any(str(cache) in entry for entry in suspicious)


@pytest.mark.parametrize("kind", ["link", "broken-link", "unreadable", "directory"])
def test_matching_cache_entries_fail_without_reading_them(candidate, tmp_path, monkeypatch, kind):
    monkeypatch.setattr(sys, "pycache_prefix", str(tmp_path / "cache"))
    cache = _mapped(candidate / "engine.py")
    cache.parent.mkdir(parents=True)
    target = tmp_path / "unrelated-data"
    target.write_text("Never read this target.\n", encoding="utf-8")
    if kind in {"link", "broken-link"}:
        try:
            cache.symlink_to(target if kind == "link" else tmp_path / "missing-target")
        except OSError as exc:
            pytest.skip("symlink creation unavailable: " + str(exc))
    elif kind == "directory":
        cache.mkdir()
    else:
        cache.write_bytes(b"Unreadable inert fixture.\n")
        cache.chmod(0)

    def no_open(*args, **kwargs):
        pytest.fail("the cache guard must inspect names without opening payloads")

    monkeypatch.setattr(Path, "open", no_open)
    assert any(cache.name in entry for entry in checker._ignored_executable_paths(candidate))


@pytest.mark.parametrize("kind", ["prefix-ancestor", "prefix", "parent", "mapped-directory"])
def test_linked_mapping_directories_are_not_traversed(candidate, tmp_path, monkeypatch, kind):
    prefix = tmp_path / "linked-parent" / "cache" if kind == "prefix-ancestor" else tmp_path / "cache"
    monkeypatch.setattr(sys, "pycache_prefix", str(prefix))
    directory = _mapped(candidate / "engine.py").parent
    linked = {"prefix-ancestor": prefix.parent, "prefix": prefix,
              "parent": directory.parent, "mapped-directory": directory}[kind]
    linked.parent.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "unrelated-profile"
    target.mkdir()
    if kind == "prefix-ancestor":
        (target / "cache").mkdir()
    try:
        linked.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip("symlink creation unavailable: " + str(exc))

    def no_scandir(*args, **kwargs):
        pytest.fail("a linked cache mapping must not be traversed")

    monkeypatch.setattr(checker.os, "scandir", no_scandir)
    suspicious = checker._ignored_executable_paths(candidate)
    assert any("__pycache__" in entry and "linked" in entry for entry in suspicious)


@pytest.mark.parametrize("operation", ["metadata", "directory-listing"])
def test_unreadable_mapped_directory_fails_closed(candidate, tmp_path, monkeypatch, operation):
    monkeypatch.setattr(sys, "pycache_prefix", str(tmp_path / "cache"))
    directory = _mapped(candidate / "engine.py").parent
    directory.mkdir(parents=True)
    if operation == "metadata":
        original = Path.lstat

        def denied(path):
            if path == directory:
                raise PermissionError("fixture access denied")
            return original(path)

        monkeypatch.setattr(Path, "lstat", denied)
    else:
        def denied(path):
            assert Path(path) == directory
            raise PermissionError("fixture access denied")

        monkeypatch.setattr(checker.os, "scandir", denied)
    assert any("__pycache__" in entry and "cannot be inspected" in entry
               for entry in checker._ignored_executable_paths(candidate))


@pytest.mark.parametrize("name", [
    "ENGINE.cpython-312.pyc", "engine.cpython-312.PYC", "ENGINE.PYC",
    "ENGINE.cpython-312.opt-1.pyc", "engine.cpython-312.OPT-2.PYC", "ENGINE.PYO",
])
@pytest.mark.parametrize("platform_model", ["windows", "posix"])
def test_cache_names_follow_platform_case_rules(
    candidate, tmp_path, monkeypatch, name, platform_model,
):
    # Filename-only models; no Windows execution or cache payload access.
    monkeypatch.setattr(sys, "pycache_prefix", str(tmp_path / "cache"))
    cache = _mapped(candidate / "engine.py").with_name(name)
    _write_cache(cache)
    normalizer = ntpath.normcase if platform_model == "windows" else lambda value: value
    monkeypatch.setattr(checker.os.path, "normcase", normalizer)
    assert bool(checker._ignored_executable_paths(candidate)) is (platform_model == "windows")


@pytest.mark.parametrize("where", ["prefix-ancestor", "prefix", "mapped-ancestor", "mapped-directory"])
@pytest.mark.parametrize("junction_helper", ["absent", "returns-false"])
def test_directory_reparse_metadata_fails_before_traversal(
    candidate, tmp_path, monkeypatch, where, junction_helper,
):
    prefix = tmp_path / "prefix-parent" / "cache"
    monkeypatch.setattr(sys, "pycache_prefix", str(prefix))
    directory = _mapped(candidate / "engine.py").parent
    directory.mkdir(parents=True)
    affected = {"prefix-ancestor": prefix.parent, "prefix": prefix,
                "mapped-ancestor": directory.parent, "mapped-directory": directory}[where]
    original = Path.lstat

    def reparse_metadata(path):
        if path == affected:
            # Model Windows lstat: directory mode, reparse attribute, no symlink bit.
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o700,
                                   st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
        return original(path)

    def no_scandir(*args, **kwargs):
        pytest.fail("a directory reparse point must fail before any traversal")

    monkeypatch.setattr(Path, "lstat", reparse_metadata)
    if junction_helper == "absent":
        monkeypatch.delattr(Path, "is_junction", raising=False)
    else:
        monkeypatch.setattr(Path, "is_junction", lambda path: False, raising=False)
    monkeypatch.setattr(checker.os, "scandir", no_scandir)
    suspicious = checker._ignored_executable_paths(candidate)
    assert any("__pycache__" in entry and "linked" in entry for entry in suspicious)
