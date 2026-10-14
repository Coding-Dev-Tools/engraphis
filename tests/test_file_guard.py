from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from engraphis.file_guard import (
    FileProtectionError,
    decide_tool_call,
    load_policy,
)


def _policy(tmp_path: Path, root: Path, **overrides) -> Path:
    payload = {
        "version": 1,
        "roots": [{
            "path": str(root),
            "protect": [".env", ".github/workflows/**", "legal/*.md"],
            **overrides,
        }],
    }
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)
    return path


def test_direct_writes_are_denied_only_for_matching_paths(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    rules = load_policy(_policy(tmp_path, root))

    denied = decide_tool_call(
        "Write", {"file_path": str(root / ".env")}, cwd=root, rules=rules,
    )
    nested = decide_tool_call(
        "Edit", {"file_path": ".github/workflows/release.yml"}, cwd=root, rules=rules,
    )
    allowed = decide_tool_call(
        "Edit", {"file_path": "src/app.py"}, cwd=root, rules=rules,
    )

    assert not denied.allowed and denied.matched_pattern == ".env"
    assert not nested.allowed and nested.matched_pattern == ".github/workflows/**"
    assert allowed.allowed


def test_shell_is_fail_closed_even_when_cwd_is_outside_root(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    rules = load_policy(_policy(tmp_path, root))
    decision = decide_tool_call(
        "Bash", {"command": f"rm {root / '.env'}"}, cwd=tmp_path, rules=rules,
    )
    assert not decision.allowed
    assert "shell tools are disabled" in decision.reason


def test_operator_can_allow_shell_when_os_sandbox_supplies_enforcement(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    rules = load_policy(_policy(tmp_path, root, allow_shell=True))
    assert decide_tool_call("Bash", {"command": "pytest"}, cwd=root, rules=rules).allowed


def test_missing_target_on_writer_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    rules = load_policy(_policy(tmp_path, root))
    assert not decide_tool_call("Write", {}, cwd=root, rules=rules).allowed


def test_policy_file_protects_itself(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    policy = _policy(tmp_path, root)
    rules = load_policy(policy)
    decision = decide_tool_call(
        "Edit", {"file_path": str(policy)}, cwd=root, rules=rules,
    )
    assert not decision.allowed
    assert decision.matched_pattern == policy.name


def test_policy_rejects_relative_roots_and_unsafe_permissions(tmp_path: Path) -> None:
    malformed = tmp_path / "malformed.json"
    malformed.write_text(
        json.dumps({"version": 1, "roots": [{"path": "repo", "protect": [".env"]}]}),
        encoding="utf-8",
    )
    with pytest.raises(FileProtectionError, match="absolute"):
        load_policy(malformed)

    if os.name == "posix":
        malformed.chmod(0o666)
        with pytest.raises(FileProtectionError, match="group- or world-writable"):
            load_policy(malformed)


def test_policy_symlink_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    policy = _policy(tmp_path, root)
    alias = tmp_path / "alias.json"
    try:
        alias.symlink_to(policy)
    except OSError:
        pytest.skip("symlinks are unavailable")
    with pytest.raises(FileProtectionError, match="regular file"):
        load_policy(alias)


def test_protected_symlink_name_cannot_escape_matching(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    target = tmp_path / "outside"
    target.write_text("secret", encoding="utf-8")
    link = root / ".env"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks are unavailable")
    rules = load_policy(_policy(tmp_path, root))
    assert not decide_tool_call(
        "Write", {"file_path": str(link)}, cwd=root, rules=rules,
    ).allowed
