"""Tests for scripts/install_cc_hook.py idempotency and nested-shape handling.

The SessionStart settings file stores each entry as
``{"hooks": [{"command": "..."}, ...]}``. A naive top-level
``h.get("command", ...)`` filter misses our entry on the inner dict and
double-installs, causing every session start to perform duplicate MCP
recalls.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import stat
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "install_cc_hook.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("install_cc_hook_under_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # ``spec.loader`` is typed as the abstract ``Loader`` base; the concrete
    # file/source loaders we get here all implement ``exec_module``.
    loader = spec.loader
    exec_module = getattr(loader, "exec_module")
    exec_module(module)
    return module


@pytest.fixture
def fake_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Isolated settings file and module-level env override."""
    settings_path = tmp_path / "settings.json"
    monkeypatch.setenv("COMMANDCODE_SETTINGS_PATH", str(settings_path))
    module = _load_module()
    return module, settings_path


def test_install_appends_one_entry_on_first_run(fake_settings) -> None:
    module, settings_path = fake_settings
    module.install()
    payload = json.loads(settings_path.read_text(encoding="utf-8"))
    entries = payload["hooks"]["SessionStart"]
    assert len(entries) == 1
    assert entries[0]["hooks"][0]["command"] == module._hook_entry()["command"]


def test_install_is_idempotent_on_repeat_runs(fake_settings) -> None:
    """Running install() twice must not duplicate the SessionStart entry."""
    module, settings_path = fake_settings
    module.install()
    module.install()
    payload = json.loads(settings_path.read_text(encoding="utf-8"))
    entries = payload["hooks"]["SessionStart"]
    assert len(entries) == 1
    assert entries[0]["hooks"][0]["command"] == module._hook_entry()["command"]


def test_file_guard_install_is_explicit_idempotent_and_fail_closed(fake_settings) -> None:
    module, settings_path = fake_settings
    module.install(enable_file_guard=True)
    module.install(enable_file_guard=True)
    payload = json.loads(settings_path.read_text(encoding="utf-8"))
    entries = payload["hooks"]["PreToolUse"]
    assert len(entries) == 1
    assert entries[0]["matcher"] == module.FILE_GUARD_MATCHER
    # Command Code's documented matcher literals must be routed to the guard.
    assert {"write", "edit", "shell"} <= set(entries[0]["matcher"].split("|"))
    assert entries[0]["hooks"] == [module._file_guard_entry()]


@pytest.mark.parametrize("matcher,expected", [
    ("Write|Edit|MultiEdit|NotebookEdit|Bash", None),  # earlier installer default: upgrade
    ("write|edit|shell|Write|Edit|MultiEdit|NotebookEdit|Bash", None),  # 1.7.10 default: upgrade
    (
        "write|edit|shell|WRITE|EDIT|SHELL|Write|Edit|MultiEdit|NotebookEdit|Bash",
        None,
    ),  # the matcher shipped by the 1.7.10 candidate: upgrade
    ("Write|Bash|my_tool", "Write|Bash|my_tool"),       # operator customization: keep
])
def test_file_guard_reinstall_upgrades_only_legacy_default_matcher(
    fake_settings, matcher: str, expected,
) -> None:
    module, settings_path = fake_settings
    settings_path.write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": matcher, "hooks": [module._file_guard_entry()]},
    ]}}), encoding="utf-8")
    module.install(enable_file_guard=True)
    entries = json.loads(settings_path.read_text(encoding="utf-8"))["hooks"]["PreToolUse"]
    assert len(entries) == 1
    assert entries[0]["matcher"] == (expected or module.FILE_GUARD_MATCHER)
    for name in ("WRITE", "EDIT", "SHELL"):
        assert expected or name in entries[0]["matcher"].split("|")


def test_file_guard_matcher_covers_the_canonical_tool_ids(fake_settings) -> None:
    # A host that matches the matcher against the tool *id* it sends would
    # never route the canonical ids to the guard without them.
    module, _ = fake_settings
    alternatives = set(module.FILE_GUARD_MATCHER.split("|"))
    assert {"write_file", "edit_file", "shell_command"} <= alternatives
    assert {"WRITE", "EDIT", "SHELL"} <= alternatives
    assert {"Write", "Edit", "MultiEdit", "NotebookEdit", "Bash"} <= alternatives


def test_file_guard_install_upgrades_a_nameless_entry_in_place(fake_settings) -> None:
    # An entry whose ``name`` was edited by hand must be upgraded in place
    # instead of leaving a second copy of the guard behind.
    module, settings_path = fake_settings
    nameless = {
        "type": "command",
        "command": module._file_guard_entry()["command"],
        "timeout": 5,
    }
    settings_path.write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": module.FILE_GUARD_MATCHER, "hooks": [nameless]},
    ]}}), encoding="utf-8")
    module.install(enable_file_guard=True)
    entries = json.loads(settings_path.read_text(encoding="utf-8"))["hooks"]["PreToolUse"]
    assert len(entries) == 1
    assert entries[0]["hooks"] == [module._file_guard_entry()]


def test_install_leaves_no_temporary_files_behind(fake_settings) -> None:
    module, settings_path = fake_settings
    module.install(enable_file_guard=True)
    leftovers = [p.name for p in settings_path.parent.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_install_preserves_the_settings_file_mode(fake_settings) -> None:
    module, settings_path = fake_settings
    settings_path.write_text("{}\n", encoding="utf-8")
    settings_path.chmod(0o600)
    module.install(enable_file_guard=True)
    assert stat.S_IMODE(settings_path.stat().st_mode) == 0o600


@pytest.mark.parametrize("payload", [
    "not json",
    json.dumps([1, 2, 3]),
    json.dumps({"hooks": []}),
    json.dumps({"hooks": {"SessionStart": {}}}),
    json.dumps({"hooks": {"SessionStart": ["not-a-wrapper"]}}),
    json.dumps({"hooks": {"SessionStart": [{"hooks": "not-a-list"}]}}),
])
def test_malformed_settings_fail_loudly(fake_settings, payload: str) -> None:
    # The installer must not report success on a settings file it cannot use.
    module, settings_path = fake_settings
    settings_path.write_text(payload, encoding="utf-8")
    with pytest.raises(SystemExit) as excinfo:
        module.install(enable_file_guard=True)
    assert excinfo.value.code == 2


def test_uninstall_removes_the_file_guard_entries(fake_settings) -> None:
    module, settings_path = fake_settings
    module.install(enable_file_guard=True)
    module.uninstall()
    payload = json.loads(settings_path.read_text(encoding="utf-8"))
    assert "PreToolUse" not in payload.get("hooks", {})


def test_main_reports_a_failed_write(fake_settings, monkeypatch, capsys) -> None:
    module, _ = fake_settings

    def boom(_settings_path, _settings):
        raise OSError("disk full")

    monkeypatch.setattr(module, "_write_settings", boom)
    monkeypatch.setattr(sys, "argv", ["install_cc_hook.py", "--file-guard"])
    assert module.main() == 2
    assert "error" in capsys.readouterr().err


def test_main_uninstall_works_when_the_hook_script_is_gone(
    fake_settings, monkeypatch, tmp_path: Path,
) -> None:
    # Removal has to work on its own: the entries in the settings file must be
    # removable even when the repository is no longer present.
    module, settings_path = fake_settings
    module.install(enable_file_guard=True)
    monkeypatch.setattr(module, "HOOK_PATH", tmp_path / "absent-hook.py")
    monkeypatch.setattr(sys, "argv", ["install_cc_hook.py", "--uninstall"])
    assert module.main() == 0
    payload = json.loads(settings_path.read_text(encoding="utf-8"))
    assert "PreToolUse" not in payload.get("hooks", {})


def test_default_install_does_not_enable_file_guard(fake_settings) -> None:
    module, settings_path = fake_settings
    module.install()
    payload = json.loads(settings_path.read_text(encoding="utf-8"))
    assert "PreToolUse" not in payload["hooks"]


def test_install_does_not_disturb_other_session_start_entries(fake_settings) -> None:
    module, settings_path = fake_settings
    settings_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "some-other-tool --flag",
                                    "timeout": 5,
                                }
                            ]
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    module.install()
    payload = json.loads(settings_path.read_text(encoding="utf-8"))
    entries = payload["hooks"]["SessionStart"]
    assert len(entries) == 2
    commands = [entry["hooks"][0]["command"] for entry in entries]
    assert "some-other-tool --flag" in commands
    assert module._hook_entry()["command"] in commands


def test_uninstall_removes_only_our_entry(fake_settings) -> None:
    module, settings_path = fake_settings
    settings_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "some-other-tool --flag",
                                    "timeout": 5,
                                }
                            ]
                        },
                        {"hooks": [module._hook_entry()]},
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    module.uninstall()
    payload = json.loads(settings_path.read_text(encoding="utf-8"))
    entries = payload["hooks"]["SessionStart"]
    assert len(entries) == 1
    assert entries[0]["hooks"][0]["command"] == "some-other-tool --flag"


def test_install_preserves_sibling_hook_in_same_wrapper(fake_settings) -> None:
    """A SessionStart wrapper that contains both our entry and a manually
    added sibling inner hook must keep the sibling after a reinstall. The
    old behaviour dropped the whole wrapper, silently deleting the
    operator's unrelated hook.
    """
    module, settings_path = fake_settings
    sibling = {
        "type": "command",
        "command": "some-other-tool --flag",
        "timeout": 5,
    }
    settings_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {"hooks": [sibling, module._hook_entry()]},
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    module.install()
    payload = json.loads(settings_path.read_text(encoding="utf-8"))
    entries = payload["hooks"]["SessionStart"]
    # The original wrapper survives, now with [sibling, fresh_engraphis].
    assert len(entries) == 1
    inner = entries[0]["hooks"]
    assert len(inner) == 2
    assert inner[0]["command"] == "some-other-tool --flag"
    assert inner[1]["command"] == module._hook_entry()["command"]


def test_uninstall_preserves_sibling_hook_in_same_wrapper(fake_settings) -> None:
    """uninstall() must not drop a sibling inner hook when stripping ours."""
    module, settings_path = fake_settings
    sibling = {
        "type": "command",
        "command": "some-other-tool --flag",
        "timeout": 5,
    }
    settings_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {"hooks": [sibling, module._hook_entry()]},
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    module.uninstall()
    payload = json.loads(settings_path.read_text(encoding="utf-8"))
    entries = payload["hooks"]["SessionStart"]
    assert len(entries) == 1
    assert entries[0]["hooks"] == [sibling]


def test_install_preserves_wrapper_level_metadata(fake_settings) -> None:
    """A SessionStart wrapper that carries wrapper-level config (such as a
    ``matcher`` filter) must keep those keys when our entry is refreshed
    in-place. Reinstalling must not silently drop the operator's filter.
    """
    module, settings_path = fake_settings
    settings_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "matcher": "session_id == 'abc'",
                            "hooks": [module._hook_entry()],
                        },
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    module.install()
    payload = json.loads(settings_path.read_text(encoding="utf-8"))
    entries = payload["hooks"]["SessionStart"]
    assert len(entries) == 1
    assert entries[0]["matcher"] == "session_id == 'abc'"
    assert entries[0]["hooks"] == [module._hook_entry()]


def test_uninstall_preserves_wrapper_level_metadata_with_sibling(fake_settings) -> None:
    """uninstall() with a sibling present must keep wrapper-level keys."""
    module, settings_path = fake_settings
    sibling = {
        "type": "command",
        "command": "some-other-tool --flag",
        "timeout": 5,
    }
    settings_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "matcher": "session_id == 'abc'",
                            "hooks": [sibling, module._hook_entry()],
                        },
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    module.uninstall()
    payload = json.loads(settings_path.read_text(encoding="utf-8"))
    entries = payload["hooks"]["SessionStart"]
    assert len(entries) == 1
    assert entries[0]["matcher"] == "session_id == 'abc'"
    assert entries[0]["hooks"] == [sibling]


@pytest.mark.parametrize("operation", ["install", "uninstall"])
def test_nameless_previous_python_command_is_migrated_or_removed(
    fake_settings, operation: str,
) -> None:
    module, settings_path = fake_settings
    # Freeze the command emitted by the pre-1.7.10 installer. It intentionally
    # lacks the newer stable name so only legacy command recognition can match.
    legacy = {"type": "command", "command": f'python "{module.HOOK_PATH}"', "timeout": 5}
    sibling = {"type": "command", "command": "some-other-tool --flag"}
    settings_path.write_text(json.dumps({"hooks": {"SessionStart": [{
        "matcher": "operator-filter", "hooks": [legacy, sibling],
    }]}}), encoding="utf-8")

    for _ in range(2):
        getattr(module, operation)()

    wrappers = json.loads(settings_path.read_text(encoding="utf-8"))["hooks"]["SessionStart"]
    assert len(wrappers) == 1
    assert wrappers[0]["matcher"] == "operator-filter"
    expected = [sibling, module._hook_entry()] if operation == "install" else [sibling]
    assert wrappers[0]["hooks"] == expected
