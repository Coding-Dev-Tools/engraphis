"""Descriptor-bound reads of operator-approved specification files."""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from engraphis.core.source_paths import read_local_source
from engraphis.service import MemoryService, ValidationError


@pytest.fixture
def source_service(tmp_path, monkeypatch):
    monkeypatch.setenv("ENGRAPHIS_INDEX_ROOTS", str(tmp_path))
    service = MemoryService.create(":memory:")
    try:
        yield service
    finally:
        service.close()


def _source_pair(tmp_path):
    public = tmp_path / "public" / "policy.md"
    private = tmp_path / ".aws" / "policy.md"
    public.parent.mkdir()
    private.parent.mkdir()
    public.write_text("# Rules\nSynthetic public policy.", encoding="utf-8")
    private.write_text("# Rules\nSYNTHETIC_PRIVATE_POLICY_NOT_FOR_CAPTURE.", encoding="utf-8")
    # Check link availability before installing a race seam, notably on Windows.
    probe = tmp_path / "link-probe"
    try:
        probe.symlink_to(private)
        probe.unlink()
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    return public, private


@pytest.mark.parametrize("replace_parent", [False, True], ids=["file", "parent"])
@pytest.mark.parametrize("no_follow", [True, False], ids=["native", "portable-fallback"])
def test_source_open_race_rejects_sensitive_substitution(
    source_service, tmp_path, monkeypatch, replace_parent, no_follow,
):
    public, private = _source_pair(tmp_path)
    native_open, native_path_open, native_read = os.open, Path.open, os.read
    if not no_follow:
        monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    state = {"swapped": False, "private_reads": 0}
    private_info = os.stat(private)

    def swap():
        if not state["swapped"]:
            if replace_parent:
                public.parent.rename(tmp_path / "original-public")
                public.parent.symlink_to(private.parent, target_is_directory=True)
            else:
                public.unlink()
                public.symlink_to(private)
            state["swapped"] = True

    def swapping_open(path, *args, **kwargs):
        if os.fspath(path) in {str(public), public.name}:
            swap()
        return native_open(path, *args, **kwargs)

    def swapping_path_open(path, *args, **kwargs):
        if path == public:
            swap()
        return native_path_open(path, *args, **kwargs)

    def observed_read(fd, size):
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == (private_info.st_dev, private_info.st_ino):
            state["private_reads"] += 1
        return native_read(fd, size)

    monkeypatch.setattr(os, "open", swapping_open)
    monkeypatch.setattr(Path, "open", swapping_path_open)
    monkeypatch.setattr(os, "read", observed_read)
    with pytest.raises(ValidationError):
        source_service.spec_crawl(source_id=str(public), trace_claims=False)
    assert state["swapped"]
    assert state["private_reads"] == 0


@pytest.mark.parametrize("replace_parent", [False, True], ids=["file", "parent"])
def test_source_open_race_rejects_regular_file_substitution(
    source_service, tmp_path, monkeypatch, replace_parent,
):
    public = tmp_path / "public" / "policy.md"
    private = tmp_path / ".aws" / "policy.md"
    public.parent.mkdir()
    private.parent.mkdir()
    public.write_text("# Rules\nSynthetic public policy.", encoding="utf-8")
    private.write_text("# Rules\nSYNTHETIC_PRIVATE_POLICY_NOT_FOR_CAPTURE.", encoding="utf-8")
    native_open, native_path_open, native_read = os.open, Path.open, os.read
    private_info = os.stat(private)
    state = {"swapped": False, "private_reads": 0}

    def swap():
        if not state["swapped"]:
            if replace_parent:
                public.parent.rename(tmp_path / "original-public")
                private.parent.rename(public.parent)
            else:
                os.replace(private, public)
            state["swapped"] = True

    def swapping_open(path, *args, **kwargs):
        if os.fspath(path) in {str(public), public.name}:
            swap()
        return native_open(path, *args, **kwargs)

    def swapping_path_open(path, *args, **kwargs):
        if path == public:
            swap()
        return native_path_open(path, *args, **kwargs)

    def observed_read(fd, size):
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == (private_info.st_dev, private_info.st_ino):
            state["private_reads"] += 1
        return native_read(fd, size)

    monkeypatch.setattr(os, "open", swapping_open)
    monkeypatch.setattr(Path, "open", swapping_path_open)
    monkeypatch.setattr(os, "read", observed_read)
    with pytest.raises(ValidationError):
        source_service.spec_crawl(source_id=str(public), trace_claims=False)
    assert state["swapped"]
    assert state["private_reads"] == 0


@pytest.mark.parametrize("mutation", ["file-link", "parent-link", "content", "growth"])
def test_source_changes_during_read_discard_payload(
    source_service, tmp_path, monkeypatch, mutation,
):
    public, private = _source_pair(tmp_path)
    native_read = os.read
    changed = []

    def changing_read(fd, size):
        payload = native_read(fd, size)
        if payload and not changed:
            changed.append(True)
            if mutation == "file-link":
                public.unlink()
                public.symlink_to(private)
            elif mutation == "parent-link":
                public.parent.rename(tmp_path / "original-public")
                public.parent.symlink_to(private.parent, target_is_directory=True)
            else:
                initial = public.stat()
                public.write_bytes(b"x" * (128_001 if mutation == "growth" else initial.st_size))
                os.utime(public, ns=(initial.st_atime_ns, initial.st_mtime_ns + 1_000_000_000))
        return payload

    monkeypatch.setattr(os, "read", changing_read)
    with pytest.raises(ValidationError):
        source_service.spec_crawl(source_id=str(public), trace_claims=False)
    assert changed


@pytest.mark.parametrize("no_follow", [True, False], ids=["native", "portable-fallback"])
@pytest.mark.parametrize("selected_link", [False, True], ids=["regular", "benign-link"])
def test_source_read_preserves_benign_unicode_files(
    source_service, tmp_path, monkeypatch, no_follow, selected_link,
):
    root = tmp_path / ".worktree" / "specs"
    root.mkdir(parents=True)
    source = root / "rules.md"
    source.write_text("# Rules\nKeep café data local; run 15 tests.", encoding="utf-8")
    if selected_link:
        link = tmp_path / "rules-link.md"
        try:
            link.symlink_to(source)
        except OSError as exc:
            pytest.skip(f"symlinks unavailable: {exc}")
        source = link
    if not no_follow:
        monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    report = source_service.spec_crawl(source_id=str(source), trace_claims=False)
    assert report["word_count"] > 0
    assert "café" in str(report["sections"])


@pytest.mark.parametrize("size", [0, 127_999, 128_000, 128_001])
def test_local_source_enforces_exact_byte_bound(tmp_path, size):
    source = tmp_path / "rules.md"
    source.write_bytes(b"a" * size)
    if size > 128_000:
        with pytest.raises(ValueError, match="exceeds 128000 bytes"):
            read_local_source(source, max_bytes=128_000)
    else:
        assert read_local_source(source, max_bytes=128_000) == b"a" * size


@pytest.mark.parametrize("target", ["file", "parent"])
def test_local_source_rejects_reparse_attributes(tmp_path, monkeypatch, target):
    source = tmp_path / "public" / "rules.md"
    source.parent.mkdir()
    source.write_text("# Rules\nRun 15 tests.", encoding="utf-8")
    native_lstat = os.lstat
    changed = source if target == "file" else source.parent

    def reparse_lstat(path, *args, **kwargs):
        info = native_lstat(path, *args, **kwargs)
        if Path(path) == changed:
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
        return info

    monkeypatch.setattr(os, "lstat", reparse_lstat)
    with pytest.raises(ValueError, match="unsafe source"):
        read_local_source(source, max_bytes=128_000)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires POSIX FIFO support")
def test_source_fifo_substitution_never_blocks(source_service, tmp_path, monkeypatch):
    source = tmp_path / "rules.md"
    source.write_text("# Rules\nRun 15 tests.", encoding="utf-8")
    native_open = os.open
    changed = []

    def fifo_open(path, flags, *args, **kwargs):
        if os.fspath(path) in {str(source), source.name}:
            assert flags & os.O_NONBLOCK
            source.unlink()
            os.mkfifo(source)
            changed.append(True)
        return native_open(path, flags, *args, **kwargs)

    # Fail quickly if an ordinary stream open is reintroduced; it would wait for
    # a FIFO writer instead of reaching the descriptor's regular-file check.
    def blocking_open(*args, **kwargs):
        pytest.fail("source reads must use a nonblocking descriptor")

    monkeypatch.setattr(os, "open", fifo_open)
    monkeypatch.setattr(Path, "open", blocking_open)
    with pytest.raises(ValidationError):
        source_service.spec_crawl(source_id=str(source), trace_claims=False)
    assert changed


@pytest.mark.parametrize("fail", [False, True])
def test_local_source_closes_every_descriptor(tmp_path, monkeypatch, fail):
    source = tmp_path / "rules.md"
    source.write_bytes(b"plain policy")
    native_open = os.open
    descriptors = []

    def observed_open(*args, **kwargs):
        fd = native_open(*args, **kwargs)
        descriptors.append(fd)
        return fd

    monkeypatch.setattr(os, "open", observed_open)
    if fail:
        with pytest.raises(ValueError, match="exceeds"):
            read_local_source(source, max_bytes=1)
    else:
        assert read_local_source(source, max_bytes=128_000) == b"plain policy"
    assert descriptors
    for fd in descriptors:
        with pytest.raises(OSError):
            os.fstat(fd)
