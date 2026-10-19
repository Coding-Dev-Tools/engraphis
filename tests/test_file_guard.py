from __future__ import annotations

import json
import ntpath
import os
from pathlib import Path
import posixpath

import pytest

from engraphis import file_guard
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


@pytest.mark.skipif(os.name != "nt", reason="native Windows filename case semantics")
@pytest.mark.parametrize("target,patterns,matched", [
    (".ENV", None, ".env"),
    (".GITHUB/WORKFLOWS", None, ".github/workflows/**"),
    (".GITHUB/WORKFLOWS/release.yml", None, ".github/workflows/**"),
    (".GiThUb/WoRkFlOwS/deep/release.yml", None, ".github/workflows/**"),
    ("LEGAL/NEW.MD", None, "legal/*.md"),
    (".env", [".EnV"], ".EnV"),
    (".github/workflows", [".GitHub/WorkFlows/**"], ".GitHub/WorkFlows/**"),
    (".GITHUB/WORKFLOWS/deep/RELEASE.YML", [".GitHub/WorkFlows/**"], ".GitHub/WorkFlows/**"),
    ("legal/new.md", ["LeGaL/*.Md"], "LeGaL/*.Md"),
    (".env.example", None, None),
    (".github/actions/release.yml", None, None),
    (".github/workflows-other/release.yml", None, None),
    ("legal/new.txt", None, None),
    ("LegalExtra/new.md", None, None),
    ("src/app.py", None, None),
])
def test_windows_missing_targets_follow_policy_case(
    tmp_path: Path, target, patterns, matched,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    overrides = {} if patterns is None else {"protect": patterns}
    rules = load_policy(_policy(tmp_path, root, **overrides))
    # Missing components retain caller casing through realpath on Windows.
    assert not (root / target).exists()
    decision = decide_tool_call("Write", {"file_path": target}, cwd=root, rules=rules)
    assert decision.allowed is (matched is None)
    assert decision.matched_pattern == matched
    assert not (root / target).exists()


@pytest.mark.parametrize("normalizer,is_windows", [
    pytest.param(ntpath.normcase, True, id="windows"),
    pytest.param(posixpath.normcase, False, id="posix"),
])
@pytest.mark.parametrize("pattern,target,exact_match", [
    (".env", ".ENV", False),
    (".EnV", ".env", False),
    (".GitHub/WorkFlows/**", ".github/workflows", False),
    (".github/workflows/**", ".github/workflows", True),
    (".GitHub/WorkFlows/**", ".GITHUB/WORKFLOWS/new.yml", False),
    ("legal/*.md", "LEGAL/new.MD", False),
    ("legal/*.md", "legal/new.md", True),
    (".github/workflows/**", ".github/workflows-other/new.yml", None),
    (".env", "src/app.py", None),
])
def test_platform_case_rules_preserve_posix_matching(
    tmp_path: Path, monkeypatch, normalizer, is_windows, pattern, target, exact_match,
) -> None:
    # Replace only this module's binding; OS/pathlib resolution stays native.
    native_normalizer = os.path.normcase
    monkeypatch.setattr(file_guard, "_normcase", normalizer)
    root = tmp_path / "repo"
    root.mkdir()
    rules = load_policy(_policy(tmp_path, root, protect=[pattern]))
    assert not (root / target).exists()
    decision = decide_tool_call("Edit", {"file_path": target}, cwd=root, rules=rules)
    denied = exact_match is not None and (is_windows or exact_match)
    assert decision.allowed is not denied
    assert decision.matched_pattern == (pattern if denied else None)
    assert os.path.normcase is native_normalizer
