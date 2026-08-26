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
from pathlib import Path

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
