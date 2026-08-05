from __future__ import annotations

import argparse
import sys
import subprocess
from pathlib import Path

import pytest

from scripts import install_shortcuts
from scripts.install_shortcuts import (
    _desktop_path,
    _remove_shortcuts,
    _shortcut_paths,
    _validated_icon_path,
)


def test_windows_desktop_path_uses_the_known_folder(monkeypatch, tmp_path):
    home = tmp_path / "Home"
    redirected = tmp_path / "OneDrive" / "Desktop"

    class Result:
        stdout = str(redirected) + "\n"

    monkeypatch.setattr(install_shortcuts.subprocess, "run", lambda *args, **kwargs: Result())

    assert _desktop_path("Windows", home) == redirected


def test_windows_uninstall_uses_the_same_known_desktop_folder(monkeypatch, tmp_path):
    home = tmp_path / "Home"
    redirected = tmp_path / "OneDrive" / "Desktop"
    captured = {}

    monkeypatch.setattr(sys, "argv", ["install-shortcuts", "--uninstall"])
    monkeypatch.setattr(install_shortcuts.platform, "system", lambda: "Windows")
    monkeypatch.setattr(install_shortcuts.Path, "home", lambda: home)
    monkeypatch.setattr(install_shortcuts, "_desktop_path", lambda system, received_home: redirected)
    monkeypatch.setattr(
        install_shortcuts,
        "_remove_shortcuts",
        lambda system, desktop, start_menu, *, home: captured.update(
            system=system, desktop=desktop, start_menu=start_menu, home=home
        ) or [],
    )

    install_shortcuts.main()

    assert captured["system"] == "Windows"
    assert captured["desktop"] == redirected
    assert captured["home"] == home


def test_windows_icon_is_passed_as_data_not_interpolated_into_powershell(
    monkeypatch, tmp_path
):
    icon = 'C:\\icons\\quoted"$value.ico'
    captured = {}

    def run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(install_shortcuts.subprocess, "run", run)

    install_shortcuts._windows(
        tmp_path / "Desktop",
        tmp_path / "Start Menu",
        argparse.Namespace(icon=icon),
    )

    powershell = captured["command"][-1]
    assert icon not in powershell
    assert powershell.count("$env:ENGRAPHIS_SHORTCUT_ICON") == 2
    assert captured["kwargs"]["env"]["ENGRAPHIS_SHORTCUT_ICON"] == icon
    assert captured["kwargs"]["check"] is True


@pytest.mark.parametrize(
    "failure",
    [
        FileNotFoundError("powershell executable is unavailable"),
        OSError("PowerShell could not be started"),
        subprocess.CalledProcessError(
            1,
            ["powershell"],
            stderr="child output containing a secret-like payload",
        ),
    ],
)
def test_windows_uses_a_redacted_bat_fallback_for_powershell_failures(
    monkeypatch, tmp_path, capsys, failure
):
    desktop = tmp_path / "Desktop"
    desktop.mkdir()

    def raise_failure(*args, **kwargs):
        raise failure

    monkeypatch.setattr(install_shortcuts.subprocess, "run", raise_failure)

    install_shortcuts._windows(
        desktop,
        tmp_path / "Start Menu",
        argparse.Namespace(icon="C:\\icons\\engraphis.ico"),
    )

    launcher = desktop / "Engraphis Dashboard.bat"
    assert launcher.read_text() == (
        "@echo off\nengraphis-dashboard\n"
        "echo.\necho Dashboard stopped. Press any key.\npause >nul\n"
    )
    stderr = capsys.readouterr().err
    assert "Falling back to a simple .bat launcher" in stderr
    assert "secret-like payload" not in stderr
    assert "PowerShell could not be started" not in stderr

@pytest.mark.parametrize(
    "value",
    ["", "/safe/icon.png\nExec=unexpected-command", "/safe/icon.png\x1b[31m"],
)
def test_icon_validation_rejects_empty_or_control_bearing_values(value):
    with pytest.raises(ValueError, match="control characters"):
        _validated_icon_path(value)


@pytest.mark.parametrize("system", ["Windows", "Darwin", "Linux"])
def test_remove_shortcuts_removes_only_known_artifacts_and_is_idempotent(tmp_path, system):
    desktop = tmp_path / "Desktop"
    start_menu = tmp_path / "Start Menu" / "Programs"
    home = tmp_path / "Home"
    desktop.mkdir(parents=True)

    expected = _shortcut_paths(system, desktop, start_menu, home=home)
    for path in expected:
        if path.suffix == ".app":
            (path / "Contents").mkdir(parents=True)
            (path / "Contents" / "Info.plist").write_text("owned artifact")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("owned artifact")

    untouched = home / "Applications" / "Unrelated.app"
    untouched.mkdir(parents=True)
    (untouched / "keep.txt").write_text("keep")
    nearby = desktop / "other-shortcut.desktop"
    nearby.write_text("keep")

    assert _remove_shortcuts(system, desktop, start_menu, home=home) == expected
    assert all(not path.exists() and not path.is_symlink() for path in expected)
    assert untouched.is_dir()
    assert nearby.read_text() == "keep"

    assert _remove_shortcuts(system, desktop, start_menu, home=home) == []


def test_uninstall_cli_needs_no_desktop_and_does_not_prompt(monkeypatch, tmp_path):
    home = tmp_path / "Home"
    captured = {}

    monkeypatch.setattr(sys, "argv", ["install-shortcuts", "--uninstall"])
    monkeypatch.setattr(install_shortcuts.platform, "system", lambda: "Linux")
    monkeypatch.setattr(install_shortcuts.Path, "home", lambda: home)
    monkeypatch.setattr(
        install_shortcuts,
        "_remove_shortcuts",
        lambda system, desktop, start_menu, *, home: captured.update(
            system=system, desktop=desktop, start_menu=start_menu, home=home
        ) or [],
    )

    install_shortcuts.main()

    assert captured["system"] == "Linux"
    assert captured["desktop"] == home / "Desktop"
    assert captured["home"] == home


def test_linux_shortcuts_keep_desktop_launcher_executable_and_menu_entry_data(monkeypatch, tmp_path):
    home = tmp_path / "Home"
    desktop = home / "Desktop"
    icon = tmp_path / "assets" / "engraphis icon.png"
    desktop.mkdir(parents=True)
    icon.parent.mkdir()
    icon.touch()
    monkeypatch.setattr(install_shortcuts.Path, "home", lambda: home)
    chmod_calls = {}
    original_chmod = install_shortcuts.os.chmod

    def traced_chmod(path, mode, **kwargs):
        chmod_calls[Path(path)] = mode
        return original_chmod(path, mode, **kwargs)

    monkeypatch.setattr(install_shortcuts.os, "chmod", traced_chmod)

    install_shortcuts._linux(desktop, argparse.Namespace(icon=str(icon)))

    desktop_entry = desktop / "engraphis-dashboard.desktop"
    menu_entry = home / ".local" / "share" / "applications" / "engraphis-dashboard.desktop"
    expected = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Engraphis Dashboard\n"
        "Comment=Local AI memory engine WebUI\n"
        "Exec=engraphis-dashboard\n"
        f"Icon={icon}\n"
        "Terminal=false\n"
        "Categories=Development;Utility;\n"
        "Keywords=AI;memory;agent;dashboard;\n"
        "StartupWMClass=engraphis-dashboard\n"
    )
    assert desktop_entry.read_text() == expected
    assert menu_entry.read_text() == expected
    # Assert requested modes rather than host filesystem semantics: Windows test
    # volumes do not preserve POSIX execute bits, whereas Linux does.
    assert chmod_calls[desktop_entry] == 0o755
    assert chmod_calls[menu_entry] == 0o644


def test_linux_shortcuts_reject_icon_newline_before_mutating_files(monkeypatch, tmp_path):
    home = tmp_path / "Home"
    desktop = home / "Desktop"
    desktop.mkdir(parents=True)
    monkeypatch.setattr(install_shortcuts.Path, "home", lambda: home)

    with pytest.raises(ValueError, match="control characters"):
        install_shortcuts._linux(
            desktop,
            argparse.Namespace(icon="/safe/icon.png\nExec=unexpected-command"),
        )

    assert not (desktop / "engraphis-dashboard.desktop").exists()
    assert not (home / ".local").exists()
