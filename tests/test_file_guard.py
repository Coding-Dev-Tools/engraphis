from __future__ import annotations

import json
import ntpath
import os
from pathlib import Path
import posixpath
import sys

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


@pytest.mark.parametrize(
    "edit_through_alias", [False, True], ids=["real-parent", "alias-parent"],
)
def test_policy_protects_itself_through_a_symlinked_parent(
    tmp_path: Path, edit_through_alias: bool,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    policy_directory = tmp_path / "operator"
    policy_directory.mkdir()
    policy = _policy(policy_directory, root)
    alias = tmp_path / "operator-alias"
    try:
        alias.symlink_to(policy_directory, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")
    aliased_policy = alias / policy.name
    rules = load_policy(aliased_policy)
    target = aliased_policy if edit_through_alias else policy
    decision = decide_tool_call(
        "Edit", {"file_path": str(target)}, cwd=root, rules=rules,
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
    # The simulation describes *platform* semantics, so the filesystem probe
    # (which asks the volume whether it ignores case) is neutralized too: a
    # case-insensitive volume must not change what this simulation asserts.
    monkeypatch.setattr(file_guard, "_filesystem_is_case_insensitive", lambda root: False)
    root = tmp_path / "repo"
    root.mkdir()
    rules = load_policy(_policy(tmp_path, root, protect=[pattern]))
    assert not (root / target).exists()
    decision = decide_tool_call("Edit", {"file_path": target}, cwd=root, rules=rules)
    denied = exact_match is not None and (is_windows or exact_match)
    assert decision.allowed is not denied
    assert decision.matched_pattern == (pattern if denied else None)
    assert os.path.normcase is native_normalizer


@pytest.mark.parametrize("tool_name,tool_input", [
    ("write_file", {"file_path": ".env", "content": "x"}),
    ("edit_file", {"file_path": ".github/workflows/ci.yml", "old_value": "a", "new_value": "b"}),
    ("shell_command", {"command": "echo x > .env"}),
])
def test_command_code_canonical_tool_ids_are_enforced(
    tmp_path: Path, tool_name: str, tool_input: dict,
) -> None:
    # Command Code's PreToolUse payload carries canonical ids, not display names.
    root = tmp_path / "repo"
    root.mkdir()
    rules = load_policy(_policy(tmp_path, root))
    assert not decide_tool_call(tool_name, tool_input, cwd=root, rules=rules).allowed


def test_symlink_followed_by_parent_segment_cannot_reach_protected_target(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    (root / ".github" / "workflows" / "deep").mkdir(parents=True)
    (root / "sub").mkdir()
    try:
        (root / "sub" / "link").symlink_to(
            root / ".github" / "workflows" / "deep", target_is_directory=True,
        )
    except OSError:
        pytest.skip("directory symlinks are unavailable")
    rules = load_policy(_policy(tmp_path, root))
    target = "sub/link/../release.yml"
    # Confirm the platform resolves the symlink before ``..`` for this alias.
    if os.path.realpath(root / target) != os.path.realpath(
        root / ".github" / "workflows" / "release.yml"
    ):
        pytest.skip("platform normalizes '..' before following symlinks")
    decision = decide_tool_call("Write", {"file_path": target}, cwd=root, rules=rules)
    assert not decision.allowed
    assert decision.matched_pattern == ".github/workflows/**"


_HOOK = Path(__file__).resolve().parent.parent / "integrations" / "commandcode" / "file_guard_hook.py"


def _run_hook(payload, policy: Path, *python_flags: str) -> int:
    import subprocess
    import sys

    env = {
        k: v for k, v in os.environ.items()
        if not k.startswith("ENGRAPHIS_") and k != "PYTHONPATH"
    }
    env["ENGRAPHIS_FILE_PROTECTION_POLICY"] = str(policy)
    if "-S" not in python_flags:
        # Exercise this checkout rather than whichever ``engraphis`` is installed.
        env["PYTHONPATH"] = str(_HOOK.parent.parent.parent)
    command = [sys.executable, *python_flags, str(_HOOK)]
    if isinstance(payload, bytes):
        return subprocess.run(
            command, input=payload, capture_output=True, env=env, timeout=60,
        ).returncode
    return subprocess.run(
        command, input=payload, text=True, capture_output=True, env=env, timeout=60,
    ).returncode


def test_command_code_hook_blocks_canonical_payload(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    policy = _policy(tmp_path, root)
    write = json.dumps({
        "hook_event_name": "PreToolUse", "tool_name": "write_file",
        "tool_display_name": "WRITE", "cwd": str(root),
        "tool_input": {"file_path": str(root / ".env"), "content": "x"},
    })
    read = json.dumps({
        "hook_event_name": "PreToolUse", "tool_name": "read_file",
        "tool_display_name": "READ", "cwd": str(root),
        "tool_input": {"file_path": str(root / ".env")},
    })
    assert _run_hook(write, policy) == 2
    assert _run_hook(read, policy) == 0


@pytest.mark.parametrize("case", ["deeply-nested-json", "engraphis-not-importable"])
def test_command_code_hook_fails_closed_on_unexpected_errors(
    tmp_path: Path, case: str,
) -> None:
    # Command Code runs the tool on any exit status other than 2.
    root = tmp_path / "repo"
    root.mkdir()
    policy = _policy(tmp_path, root)
    if case == "deeply-nested-json":
        assert _run_hook("[" * 100_000 + "]" * 100_000, policy) == 2
    else:
        payload = json.dumps({"tool_name": "write_file", "tool_input": {"file_path": ".env"}})
        # ``-S`` skips site-packages, so ``engraphis`` cannot be imported.
        assert _run_hook(payload, policy, "-S") == 2


def _load_hook_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("file_guard_hook_under_test", _HOOK)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    exec_module = getattr(spec.loader, "exec_module")
    exec_module(module)
    return module


@pytest.mark.parametrize("payload", [
    "",                                   # empty stdin
    "not json at all",                    # non-JSON stdin
    "[]",                                 # non-object payload
    '"text"',                             # non-object payload
    "42",                                 # non-object payload
    "null",                               # non-object payload
    b"\xff\xfe\x00broken",                # undecodable bytes
])
def test_command_code_hook_denies_malformed_payloads(tmp_path: Path, payload) -> None:
    # Command Code runs the tool on any exit status other than 2, so every
    # unusable payload has to be a denial.
    root = tmp_path / "repo"
    root.mkdir()
    policy = _policy(tmp_path, root)
    assert _run_hook(payload, policy) == 2


def test_command_code_hook_denies_an_oversized_payload(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    policy = _policy(tmp_path, root)
    payload = json.dumps({
        "tool_name": "read_file",
        "tool_input": {"file_path": "notes.md", "content": "x" * (1 << 20)},
    })
    assert len(payload.encode("utf-8")) > (1 << 20)
    assert _run_hook(payload, policy) == 2


def test_command_code_hook_denies_a_deeply_nested_object(tmp_path: Path) -> None:
    # A payload nested far past the accepted depth is refused before the JSON
    # parser can recurse into it.
    root = tmp_path / "repo"
    root.mkdir()
    policy = _policy(tmp_path, root)
    payload = '{"tool_name": "read_file", "tool_input": ' + "[" * 500 + "]" * 500 + "}"
    assert _run_hook(payload, policy) == 2


def test_command_code_hook_denies_when_the_policy_is_missing(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    missing = tmp_path / "absent-policy.json"
    payload = json.dumps({"tool_name": "read_file", "tool_input": {}})
    assert _run_hook(payload, missing) == 2


def test_command_code_hook_denies_when_the_evaluator_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An unexpected error inside the evaluator must be a denial, not an exit
    # status the host would treat as "allowed".
    import io

    import engraphis.file_guard as guard

    root = tmp_path / "repo"
    root.mkdir()
    policy = _policy(tmp_path, root)
    monkeypatch.setenv("ENGRAPHIS_FILE_PROTECTION_POLICY", str(policy))

    def boom():
        raise RuntimeError("policy backend unavailable")

    monkeypatch.setattr(guard, "load_policy", boom)
    module = _load_hook_module()
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(json.dumps({"tool_name": "read_file", "tool_input": {}})),
    )
    assert module.main() == 2


def test_command_code_hook_report_never_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A broken stderr must not change the exit status: the host only blocks the
    # tool call on exit code 2.
    class _Broken:
        def write(self, *_args, **_kwargs):
            raise OSError("stderr is closed")

        def flush(self):
            raise OSError("stderr is closed")

    module = _load_hook_module()
    monkeypatch.setattr(module.sys, "stderr", _Broken())
    module._report("Engraphis file guard denied the tool call: reason")


@pytest.mark.parametrize("value,unverifiable", [
    ("\\\\?\\C:\\repo\\.env", True),             # extended-length prefix
    ("\\\\.\\C:\\repo\\.env", True),             # device namespace
    ("//?/C:/repo/.env", True),                   # forward-slash spelling
    ("\\\\localhost\\C$\\repo\\.env", True),      # admin share alias
    ("\\\\?\\UNC\\localhost\\C$\\repo\\.env", True),
    ("C:\\repo\\.env::$DATA", True),             # NTFS default data stream
    (".env:hidden", True),                        # relative alternate data stream
    ("C:relative.txt", True),                     # drive-relative: resolves against
                                                  # the current directory of that drive
    ("C:", True),                                 # bare drive-relative path
    ("C:\\repo\\src\\app.py", False),
    ("src/app.py", False),
    ("  \\\\localhost\\C$\\repo\\.env", True),      # leading whitespace is stripped
    ("\t\\\\.\\C:\\repo\\.env", True),
])
def test_windows_alias_targets_are_unverifiable(value: str, unverifiable: bool) -> None:
    from engraphis.file_guard import _unverifiable_windows_target

    assert _unverifiable_windows_target(value) is unverifiable


def test_windows_alias_targets_are_denied(tmp_path: Path, monkeypatch) -> None:
    import engraphis.file_guard as guard

    root = tmp_path / "repo"
    root.mkdir()
    rules = load_policy(_policy(tmp_path, root))
    monkeypatch.setattr(guard, "_WINDOWS", True)
    for value in ("\\\\?\\C:\\repo\\.env", ".env::$DATA"):
        decision = decide_tool_call("write_file", {"file_path": value}, cwd=root, rules=rules)
        assert not decision.allowed and "Windows" in decision.reason
    allowed = decide_tool_call("write_file", {"file_path": "notes.md"}, cwd=root, rules=rules)
    assert allowed.allowed


# ── Bypass regressions ────────────────────────────────────────────────────────


@pytest.mark.parametrize("tool_name", [
    "apply_patch", "ApplyPatch", "apply-patch",
    "create_file", "delete_file", "move_file", "rename_file", "copy_file",
    "update_file", "write_to_file", "replace_file", "str_replace_editor",
    "write-file", "edit-file", "notebook_edit", "multi_edit",
])
def test_mutating_tool_ids_are_denied(tmp_path: Path, tool_name: str) -> None:
    # A mutating tool id the evaluator does not recognize is allowed, so the
    # list of recognized ids has to cover the names hosts actually send.
    root = tmp_path / "repo"
    root.mkdir()
    rules = load_policy(_policy(tmp_path, root))
    decision = decide_tool_call(
        tool_name, {"file_path": str(root / ".env")}, cwd=root, rules=rules,
    )
    assert not decision.allowed


@pytest.mark.parametrize("tool_name", ["write_filex", "rewrite", "read_file"])
def test_near_miss_tool_ids_are_not_treated_as_the_file_writer(
    tmp_path: Path, tool_name: str,
) -> None:
    # Separators are normalized away, but a near miss must not collapse into
    # the protected writer id (and must not deny the whole session either).
    root = tmp_path / "repo"
    root.mkdir()
    rules = load_policy(_policy(tmp_path, root))
    decision = decide_tool_call(
        tool_name, {"file_path": str(root / ".env")}, cwd=root, rules=rules,
    )
    assert decision.allowed


def test_shell_tool_ids_are_still_denied_while_protection_is_active(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    rules = load_policy(_policy(tmp_path, root))
    for tool_name in ("shell", "shell_command", "Bash", "run_terminal_cmd"):
        decision = decide_tool_call(tool_name, {"command": "echo x"}, cwd=root, rules=rules)
        assert not decision.allowed
        assert "shell tools are disabled" in decision.reason


def test_windows_trailing_dot_and_space_targets_are_denied(
    tmp_path: Path, monkeypatch,
) -> None:
    # Win32 strips trailing dots and spaces from a path component, so
    # ``.env `` and ``.env.`` name the protected ``.env`` file.
    import engraphis.file_guard as guard

    root = tmp_path / "repo"
    root.mkdir()
    rules = load_policy(_policy(tmp_path, root))
    monkeypatch.setattr(guard, "_WINDOWS", True)
    for suffix in (" ", ".", " ."):
        decision = decide_tool_call(
            "write_file", {"file_path": str(root / (".env" + suffix))}, cwd=root, rules=rules,
        )
        assert not decision.allowed, suffix
        assert decision.matched_pattern == ".env"


def test_windows_drive_relative_target_is_denied(tmp_path: Path, monkeypatch) -> None:
    # ``C:.env`` resolves against the current directory of drive C:, which the
    # evaluator cannot map back to a protected root.
    import engraphis.file_guard as guard

    root = tmp_path / "repo"
    root.mkdir()
    rules = load_policy(_policy(tmp_path, root))
    monkeypatch.setattr(guard, "_WINDOWS", True)
    decision = decide_tool_call("write_file", {"file_path": "C:.env"}, cwd=root, rules=rules)
    assert not decision.allowed
    assert "Windows" in decision.reason


def test_windows_unverifiable_cwd_is_denied(tmp_path: Path, monkeypatch) -> None:
    # The working directory decides whether a relative target is protected, so a
    # UNC or device cwd must be refused before it is resolved.
    import engraphis.file_guard as guard

    root = tmp_path / "repo"
    root.mkdir()
    rules = load_policy(_policy(tmp_path, root))
    monkeypatch.setattr(guard, "_WINDOWS", True)
    decision = decide_tool_call(
        "write_file", {"file_path": ".env"}, cwd="  \\\\synthetic-host\\share\\repo",
        rules=rules,
    )
    assert not decision.allowed
    assert "Windows" in decision.reason


def test_case_insensitive_volume_denies_a_case_variant(tmp_path: Path, monkeypatch) -> None:
    # macOS volumes are case-insensitive by default, so ``.ENV`` would
    # overwrite a protected ``.env`` there.
    root = tmp_path / "repo"
    root.mkdir()
    rules = load_policy(_policy(tmp_path, root))
    monkeypatch.setattr(file_guard, "_normcase", posixpath.normcase)
    monkeypatch.setattr(file_guard, "_POSIX", True)
    monkeypatch.setattr(file_guard, "_filesystem_is_case_insensitive", lambda root: True)
    decision = decide_tool_call(
        "write_file", {"file_path": str(root / ".ENV")}, cwd=root, rules=rules,
    )
    assert not decision.allowed
    assert decision.matched_pattern == ".env"


def test_case_sensitive_volume_keeps_case_sensitive_matching(
    tmp_path: Path, monkeypatch,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    rules = load_policy(_policy(tmp_path, root))
    monkeypatch.setattr(file_guard, "_normcase", posixpath.normcase)
    monkeypatch.setattr(file_guard, "_POSIX", True)
    monkeypatch.setattr(file_guard, "_filesystem_is_case_insensitive", lambda root: False)
    decision = decide_tool_call(
        "write_file", {"file_path": str(root / ".ENV")}, cwd=root, rules=rules,
    )
    assert decision.allowed


def test_filesystem_case_probe_agrees_with_the_volume(tmp_path: Path) -> None:
    # The probe asks the filesystem; the test asks the filesystem the same way.
    probe = tmp_path / "CaseProbe"
    probe.write_text("x", encoding="utf-8")
    expected = (tmp_path / "caseprobe").exists()
    assert file_guard._filesystem_is_case_insensitive(str(tmp_path)) is expected


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS case-insensitive volume")
def test_macos_case_insensitive_volume_denies_case_variant(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    rules = load_policy(_policy(tmp_path, root))
    decision = decide_tool_call(
        "write_file", {"file_path": str(root / ".ENV")}, cwd=root, rules=rules,
    )
    assert not decision.allowed


def test_unicode_normalization_matches_equivalent_spellings(tmp_path: Path) -> None:
    # APFS stores names in a normalized form, so the composed and decomposed
    # spellings of a pattern name the same file.
    root = tmp_path / "repo"
    root.mkdir()
    policy = _policy(tmp_path, root, protect=["caf\u00e9.md"])
    rules = load_policy(policy)
    decision = decide_tool_call(
        "write_file", {"file_path": str(root / "cafe\u0301.md")}, cwd=root, rules=rules,
    )
    assert not decision.allowed
    assert decision.matched_pattern == "caf\u00e9.md"


@pytest.mark.parametrize("pattern", ["/etc/passwd", "C:\\Windows\\System32", "\\\\server\\share"])
def test_absolute_policy_patterns_are_rejected_on_every_platform(
    tmp_path: Path, pattern: str,
) -> None:
    # A pattern that names an absolute path can never match a root-relative
    # target, so accepting it would silently disable the operator's rule.
    root = tmp_path / "repo"
    root.mkdir()
    with pytest.raises(FileProtectionError, match="absolute"):
        load_policy(_policy(tmp_path, root, protect=[pattern]))


def test_policy_patterns_may_not_escape_their_root(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    with pytest.raises(FileProtectionError, match="escape"):
        load_policy(_policy(tmp_path, root, protect=["legal/../../secret"]))


def test_policy_file_swapped_before_the_read_is_refused(tmp_path: Path, monkeypatch) -> None:
    # The policy file is validated and read through one descriptor; a file
    # replaced in between must not be honoured.
    root = tmp_path / "repo"
    root.mkdir()
    policy = _policy(tmp_path, root)
    real_fstat = os.fstat

    def fake_fstat(descriptor):
        info = real_fstat(descriptor)
        fields = list(info)
        fields[1] = fields[1] + 1  # a different inode identity
        return os.stat_result(tuple(fields))

    monkeypatch.setattr(file_guard.os, "fstat", fake_fstat)
    with pytest.raises(FileProtectionError, match="changed while"):
        load_policy(policy)


def test_host_hook_settings_file_is_self_protected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A guarded process that can rewrite the host hook configuration can
    # disable the guard, so the settings file is protected as well.
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"hooks": {}}), encoding="utf-8")
    monkeypatch.setenv("COMMANDCODE_SETTINGS_PATH", str(settings))
    root = tmp_path / "repo"
    root.mkdir()
    rules = load_policy(_policy(tmp_path, root))
    decision = decide_tool_call(
        "write_file", {"file_path": str(settings)}, cwd=root, rules=rules,
    )
    assert not decision.allowed
    assert decision.matched_pattern == settings.name


def test_protected_root_boundary_is_component_wise(tmp_path: Path) -> None:
    # A sibling whose name starts with the root's name is not below the root.
    root = tmp_path / "repo"
    root.mkdir()
    sibling = tmp_path / "repo-evil"
    sibling.mkdir()
    (sibling / ".env").write_text("secret", encoding="utf-8")
    rules = load_policy(_policy(tmp_path, root))
    decision = decide_tool_call(
        "write_file", {"file_path": str(sibling / ".env")}, cwd=root, rules=rules,
    )
    assert decision.allowed


def test_home_relative_policy_patterns_are_rejected(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    with pytest.raises(FileProtectionError, match="relative"):
        load_policy(_policy(tmp_path, root, protect=["~/.env"]))


@pytest.mark.skipif(os.name != "nt", reason="Windows directory junction")
def test_windows_junction_into_the_protected_tree_is_denied(tmp_path: Path) -> None:
    import subprocess

    root = tmp_path / "repo"
    root.mkdir()
    alias = root / "alias"
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(alias), str(root)],
        capture_output=True, text=True,
    )
    if created.returncode != 0:
        pytest.skip("directory junctions are unavailable")
    rules = load_policy(_policy(tmp_path, root))
    decision = decide_tool_call(
        "write_file", {"file_path": str(alias / ".env")}, cwd=root, rules=rules,
    )
    assert not decision.allowed


@pytest.mark.skipif(os.name != "nt", reason="Windows 8.3 short names")
def test_windows_short_name_alias_is_denied(tmp_path: Path) -> None:
    import subprocess

    root = tmp_path / "LongProtectedDirectoryName"
    root.mkdir()
    listing = subprocess.run(
        ["cmd", "/c", "dir", "/x", str(tmp_path)], capture_output=True, text=True,
    ).stdout
    short_name = None
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[-1] == root.name and "~" in parts[-2]:
            short_name = parts[-2]
            break
    if short_name is None:
        pytest.skip("8.3 short names are unavailable")
    rules = load_policy(_policy(tmp_path, root, protect=[".env"]))
    decision = decide_tool_call(
        "write_file", {"file_path": str(tmp_path / short_name / ".env")},
        cwd=root, rules=rules,
    )
    assert not decision.allowed
