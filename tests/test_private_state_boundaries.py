"""Adversarial regressions for local credential and policy file boundaries."""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from engraphis import cloud_license, licensing
from engraphis.backends import sync_relay
from engraphis.private_state import (
    UnsafeStateFile,
    atomic_private_text,
    private_file_stat,
    publish_private_text_if_absent,
)


def _adversarial_link(target, link):
    try:
        link.symlink_to(target)
        return "symlink"
    except (NotImplementedError, OSError):
        # Unprivileged Windows CI commonly cannot create symlinks. A hardlink exercises
        # the same no-alias policy and is available on ordinary NTFS test volumes.
        os.link(str(target), str(link))
        return "hardlink"


def test_machine_id_rejects_link_malformed_and_oversize_state(monkeypatch, tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("a" * 32, encoding="utf-8")
    linked = tmp_path / "machine_id"
    _adversarial_link(victim, linked)
    monkeypatch.setattr(cloud_license, "_MACHINE_ID_FILE", linked)
    cloud_license._machine_id_cache.clear()

    with pytest.raises(RuntimeError, match="unsafe"):
        cloud_license.machine_id()
    assert victim.read_text(encoding="utf-8") == "a" * 32

    linked.unlink()
    linked.write_text("not-a-generated-machine-id", encoding="utf-8")
    with pytest.raises(RuntimeError, match="malformed"):
        cloud_license.machine_id()
    linked.write_bytes(b"a" * 129)
    with pytest.raises(RuntimeError, match="unsafe"):
        cloud_license.machine_id()


def test_lease_state_never_follows_links_or_fixed_temp_traps(monkeypatch, tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("do-not-overwrite", encoding="utf-8")
    lease = tmp_path / "lease.sig"
    _adversarial_link(victim, lease)
    monkeypatch.setattr(cloud_license, "_LEASE_FILE", lease)

    assert cloud_license._read_lease() == ""
    cloud_license._write_lease("ENGRLS1." + "a" * 40 + "." + "b" * 40)
    assert victim.read_text(encoding="utf-8") == "do-not-overwrite"
    assert lease.exists()

    lease.unlink()
    fixed_temp = tmp_path / "lease.sig.tmp"
    _adversarial_link(victim, fixed_temp)
    token = "ENGRLS1." + "c" * 40 + "." + "d" * 40
    cloud_license._write_lease(token)
    assert cloud_license._read_lease() == token
    assert victim.read_text(encoding="utf-8") == "do-not-overwrite"
    assert fixed_temp.exists()

    lease.write_bytes(b"x" * (cloud_license._MAX_LEASE_BYTES + 1))
    assert cloud_license._read_lease() == ""


def test_sync_token_link_and_malformed_state_fail_without_license_fallback(
        monkeypatch, tmp_path):
    monkeypatch.setenv("ENGRAPHIS_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("ENGRAPHIS_SYNC_TOKEN", raising=False)
    victim = tmp_path / "victim.txt"
    victim.write_text("engr_ut_" + "s" * 40, encoding="utf-8")
    token_path = tmp_path / "sync.token"
    _adversarial_link(victim, token_path)
    monkeypatch.setattr(
        licensing, "_read_key_material",
        lambda: (_ for _ in ()).throw(AssertionError("must not widen to license key")))

    assert sync_relay.has_sync_token() is False
    with pytest.raises(sync_relay.RelayError, match="unsafe|unreadable"):
        sync_relay._current_key("https://relay.example")
    with pytest.raises(UnsafeStateFile):
        sync_relay.save_sync_token("engr_ut_" + "n" * 40)
    assert victim.read_text(encoding="utf-8") == "engr_ut_" + "s" * 40

    token_path.unlink()
    token_path.write_text("short\nsecond-line\n", encoding="utf-8")
    assert sync_relay.has_sync_token() is False
    with pytest.raises(sync_relay.RelayError, match="malformed"):
        sync_relay._current_key("https://relay.example")
    token_path.write_bytes(b"x" * (sync_relay.MAX_SYNC_TOKEN_BYTES + 3))
    assert sync_relay.has_sync_token() is False
    with pytest.raises(sync_relay.RelayError, match="unsafe|unreadable"):
        sync_relay._current_key("https://relay.example")


def test_sync_policy_link_and_malformed_state_are_read_only(monkeypatch, tmp_path):
    monkeypatch.setenv("ENGRAPHIS_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("ENGRAPHIS_SYNC_READ_ONLY", raising=False)
    victim = tmp_path / "victim.txt"
    victim.write_text("0\n", encoding="utf-8")
    policy = tmp_path / "sync.read_only"
    _adversarial_link(victim, policy)

    assert sync_relay.sync_read_only() is True
    with pytest.raises(UnsafeStateFile):
        sync_relay.save_sync_read_only(False)
    assert victim.read_text(encoding="utf-8") == "0\n"

    policy.unlink()
    policy.write_text("maybe\n", encoding="utf-8")
    assert sync_relay.sync_read_only() is True
    policy.write_bytes(b"0" * (sync_relay.MAX_SYNC_POLICY_BYTES + 1))
    assert sync_relay.sync_read_only() is True


@pytest.mark.parametrize("token", [
    " leading-credential-value-123456",
    "trailing-credential-value-123456 ",
    "credential-value-with-a space-123456",
    "credential-value-with-unicode-12345\N{SNOWMAN}",
    "credential-value-with-newline-123\n456",
])
def test_sync_token_validation_is_strict_ascii(monkeypatch, tmp_path, token):
    monkeypatch.setenv("ENGRAPHIS_STATE_DIR", str(tmp_path))
    with pytest.raises(ValueError, match="ASCII bearer token"):
        sync_relay.save_sync_token(token)


def test_windows_reparse_attribute_is_rejected_even_without_symlink_mode(
        monkeypatch, tmp_path):
    path = tmp_path / "state"
    path.write_text("value", encoding="utf-8")
    original = os.lstat(path)
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

    class ReparseStat:
        st_file_attributes = flag

        def __getattr__(self, name):
            return getattr(original, name)

    monkeypatch.setattr("engraphis.private_state.os.lstat", lambda _path: ReparseStat())
    with pytest.raises(UnsafeStateFile, match="reparse"):
        private_file_stat(path)


def test_atomic_write_rejects_same_inode_edit_after_read(tmp_path):
    path = tmp_path / ".env"
    path.write_text("ORIGINAL=1\n", encoding="utf-8")
    expected = private_file_stat(path)
    path.write_text("CONCURRENT=longer\n", encoding="utf-8")

    with pytest.raises(UnsafeStateFile, match="changed after it was read"):
        atomic_private_text(path, "OUR_UPDATE=1\n", expected_stat=expected)

    assert path.read_text(encoding="utf-8") == "CONCURRENT=longer\n"


@pytest.mark.parametrize("publisher", [
    lambda path: atomic_private_text(path, "secret\n"),
    lambda path: publish_private_text_if_absent(path, "secret\n"),
])
def test_private_publish_never_chmods_destination_path(monkeypatch, tmp_path, publisher):
    target = tmp_path / "state"
    calls = []
    real_chmod = os.chmod

    def capture(path, mode, *args, **kwargs):
        calls.append(os.fspath(path))
        return real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr("engraphis.private_state.os.chmod", capture)
    publisher(target)

    assert target.read_text(encoding="utf-8") == "secret\n"
    assert os.fspath(target) not in calls


@pytest.mark.parametrize("publisher", [
    lambda path: atomic_private_text(path, "secret\n"),
    lambda path: publish_private_text_if_absent(path, "secret\n"),
])
def test_private_publication_flushes_parent_before_success(monkeypatch, tmp_path, publisher):
    flushed = []
    monkeypatch.setattr(
        "engraphis.private_state._fsync_parent", lambda path: flushed.append(Path(path)))
    target = tmp_path / "state"

    publisher(target)

    assert flushed == [target]
