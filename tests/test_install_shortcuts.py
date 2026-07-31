from __future__ import annotations

import sys

import pytest

from scripts import install_shortcuts
from scripts.install_shortcuts import _remove_shortcuts, _shortcut_paths


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
