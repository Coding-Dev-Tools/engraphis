#!/usr/bin/env python3
"""Install desktop and Start Menu shortcuts for the Engraphis dashboard WebUI.

    engraphis-dashboard --install-shortcuts     # creates shortcuts interactively
    engraphis-dashboard --install-shortcuts --silent  # no prompts

Shortcuts created on each platform:

* Windows   — Desktop .lnk + Start Menu .lnk (requires PowerShell)
* macOS     — Desktop .app bundle                    (no Start Menu analogue)
* Linux     — Desktop .desktop file                  (XDG-compliant)

Each shortcut runs ``engraphis-dashboard`` which starts the server and opens
the browser. Requires ``engraphis`` to be pip-installed with the ``[server]``
extra.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

_OWNER_ID = "engraphis-dashboard-shortcut-v1"
_LINUX_OWNER_LINE = f"X-Engraphis-Managed={_OWNER_ID}"
_BAT_OWNER_LINE = f"REM Engraphis-Managed: {_OWNER_ID}"


def _icon_path(base: str) -> str:
    return str(Path(base) / "engraphis" / "static" / "engraphis.ico")


def _validated_icon_path(value: object) -> str:
    """Return a printable icon path safe for terminal and launcher-file boundaries."""
    if (
        not isinstance(value, str)
        or not value
        or any(not character.isprintable() for character in value)
    ):
        raise ValueError(
            "icon path must be a non-empty printable string without control characters"
        )
    return value


def _desktop_path(system: str, home: Path) -> Path:
    """Locate the Desktop folder using the same Windows known-folder API as installation."""
    if system != "Windows":
        return home / "Desktop"
    try:
        result = subprocess.run(
            [
                "powershell", "-NoProfile", "-Command",
                "[Environment]::GetFolderPath([Environment+SpecialFolder]::Desktop)",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return home / "Desktop"
    desktop = result.stdout.strip()
    return Path(desktop) if desktop else home / "Desktop"


def _shortcut_paths(system: str, desktop: Path, start_menu: Path, *, home: Path) -> list[Path]:
    """Return only the exact launcher artifacts this installer owns."""
    if system == "Windows":
        return [
            desktop / "Engraphis Dashboard.lnk",
            desktop / "Engraphis Dashboard.bat",
            start_menu / "Engraphis" / "Engraphis Dashboard.lnk",
        ]
    if system == "Darwin":
        return [
            desktop / "Engraphis Dashboard.app",
            home / "Applications" / "Engraphis Dashboard.app",
        ]
    return [
        desktop / "engraphis-dashboard.desktop",
        home / ".local" / "share" / "applications" / "engraphis-dashboard.desktop",
    ]


def _path_present(path: Path) -> bool:
    return path.is_symlink() or path.exists()


def _windows_marker(path: Path) -> Path:
    digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:12]
    return path.with_name(f".{path.name}.{digest}.engraphis-owner")


def _write_windows_marker(path: Path) -> None:
    marker = _windows_marker(path)
    if _path_present(marker):
        raise FileExistsError(f"refusing to overwrite launcher ownership marker: {marker}")
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError(f"cannot mark a missing launcher: {path}")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    marker.write_text(
        f"{_OWNER_ID}\nsha256:{digest}\n",
        encoding="utf-8",
    )


def _is_owned_shortcut(system: str, path: Path, *, home: Path) -> bool:
    """Verify identity from file content/target, never from a familiar pathname alone."""
    if system == "Windows":
        if path.suffix.casefold() == ".lnk":
            marker = _windows_marker(path)
            if (
                path.is_symlink()
                or not path.is_file()
                or not marker.is_file()
                or marker.is_symlink()
            ):
                return False
            try:
                lines = marker.read_text(encoding="utf-8").splitlines()
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                return lines == [_OWNER_ID, f"sha256:{digest}"]
            except (OSError, UnicodeError):
                return False
        if path.suffix.casefold() != ".bat" or path.is_symlink() or not path.is_file():
            return False
        try:
            return path.read_text(encoding="utf-8").splitlines()[:1] == [_BAT_OWNER_LINE]
        except (OSError, UnicodeError):
            return False
    if system == "Darwin":
        expected_app = home / "Applications" / "Engraphis Dashboard.app"
        if path.is_symlink():
            try:
                return path.resolve(strict=False) == expected_app.resolve(strict=False)
            except OSError:
                return False
        marker = path / "Contents" / "Resources" / ".engraphis-owner"
        launcher = path / "Contents" / "MacOS" / "engraphis-dashboard"
        plist = path / "Contents" / "Info.plist"
        try:
            return (
                path.is_dir()
                and marker.is_file()
                and not marker.is_symlink()
                and marker.read_text(encoding="utf-8") == _OWNER_ID + "\n"
                and launcher.is_file()
                and plist.is_file()
            )
        except (OSError, UnicodeError):
            return False
    if path.is_symlink() or not path.is_file():
        return False
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return False
    return (
        _LINUX_OWNER_LINE in lines
        and "Type=Application" in lines
        and "Exec=engraphis-dashboard" in lines
    )


def _remove_owned_path(system: str, path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    if system == "Windows" and path.suffix.casefold() == ".lnk":
        marker = _windows_marker(path)
        if marker.is_file() and not marker.is_symlink():
            marker.unlink()


def _prepare_install_paths(system: str, paths: list[Path], *, home: Path) -> None:
    """Validate every collision before removing any artifact from a prior install."""
    for path in paths:
        present = _path_present(path)
        owned = present and _is_owned_shortcut(system, path, home=home)
        marker = (
            _windows_marker(path)
            if system == "Windows" and path.suffix.casefold() == ".lnk"
            else None
        )
        marker_present = marker is not None and _path_present(marker)
        if (present or marker_present) and not owned:
            collision = marker if marker_present and not present else path
            raise FileExistsError(
                f"refusing to overwrite an unrecognized launcher collision: {collision}"
            )
    for path in paths:
        if _path_present(path):
            _remove_owned_path(system, path)


def _remove_shortcuts(system: str, desktop: Path, start_menu: Path, *, home: Path) -> list[Path]:
    """Remove only launchers whose durable identity still matches this installer."""
    removed: list[Path] = []
    for path in _shortcut_paths(system, desktop, start_menu, home=home):
        if not _is_owned_shortcut(system, path, home=home):
            continue
        try:
            _remove_owned_path(system, path)
        except FileNotFoundError:
            continue
        removed.append(path)
    return removed



def _windows(desktop: Path, start_menu: Path, args: argparse.Namespace) -> None:
    icon = _validated_icon_path(args.icon)
    desktop_link = desktop / "Engraphis Dashboard.lnk"
    menu_link = start_menu / "Engraphis" / "Engraphis Dashboard.lnk"
    ps_cmd = r"""
#Requires -Version 5.1
$WshShell = New-Object -ComObject WScript.Shell
$desktop = $env:ENGRAPHIS_SHORTCUT_DESKTOP
$smDir = $env:ENGRAPHIS_SHORTCUT_START_MENU
if (!(Test-Path $smDir)) { New-Item -ItemType Directory -Path $smDir | Out-Null }

$lnk = $WshShell.CreateShortcut((Join-Path $desktop "Engraphis Dashboard.lnk"))
$lnk.TargetPath = "engraphis-dashboard.exe"
$lnk.Arguments = ""
$lnk.WorkingDirectory = (Get-Location).Path
$lnk.IconLocation = $env:ENGRAPHIS_SHORTCUT_ICON
$lnk.Description = "Engraphis Dashboard WebUI - local AI memory engine"
$lnk.Save()

$lnk2 = $WshShell.CreateShortcut((Join-Path $smDir "Engraphis Dashboard.lnk"))
$lnk2.TargetPath = "engraphis-dashboard.exe"
$lnk2.Arguments = ""
$lnk2.WorkingDirectory = (Get-Location).Path
$lnk2.IconLocation = $env:ENGRAPHIS_SHORTCUT_ICON
$lnk2.Description = "Engraphis Dashboard WebUI"
$lnk2.Save()
"""
    child_env = os.environ.copy()
    child_env["ENGRAPHIS_SHORTCUT_ICON"] = icon
    child_env["ENGRAPHIS_SHORTCUT_DESKTOP"] = str(desktop)
    child_env["ENGRAPHIS_SHORTCUT_START_MENU"] = str(menu_link.parent)
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_cmd],
            check=True, capture_output=True, text=True, env=child_env,
        )
        for link in (desktop_link, menu_link):
            if link.is_symlink() or not link.is_file():
                raise RuntimeError("PowerShell did not create the expected shortcut")
        for link in (desktop_link, menu_link):
            _write_windows_marker(link)
        print("  Desktop shortcut created.")
        print("  Start Menu shortcut created.")
    except (OSError, subprocess.CalledProcessError, RuntimeError):
        # Child stderr and exceptions can contain private paths or environment content.
        print("  PowerShell shortcut creation failed.", file=sys.stderr)
        print("  Falling back to a simple .bat launcher on Desktop.", file=sys.stderr)
        bat = desktop / "Engraphis Dashboard.bat"
        bat.write_text(
            _BAT_OWNER_LINE + "\n@echo off\nengraphis-dashboard\n"
            "echo.\necho Dashboard stopped. Press any key.\npause >nul\n",
            encoding="utf-8",
        )
        print(f"  Desktop launcher created: {bat}")


def _macos(
    desktop: Path,
    args: argparse.Namespace,
    *,
    home: Path | None = None,
) -> None:
    icon = _validated_icon_path(args.icon)
    home = Path.home() if home is None else home
    app_dir = home / "Applications" / "Engraphis Dashboard.app"
    contents = app_dir / "Contents"
    macos_dir = contents / "MacOS"
    resources = contents / "Resources"
    macos_dir.mkdir(parents=True, exist_ok=False)
    resources.mkdir(parents=True, exist_ok=False)

    launcher = macos_dir / "engraphis-dashboard"
    working_directory = shlex.quote(str(Path.cwd()))
    launcher.write_text(
        "#!/bin/bash\n"
        f"cd -- {working_directory}\n"
        "exec engraphis-dashboard\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)

    ico_src = Path(icon)
    if ico_src.exists():
        shutil.copy2(ico_src, resources / "engraphis.icns")

    (contents / "Info.plist").write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>Engraphis Dashboard</string>
    <key>CFBundleDisplayName</key>
    <string>Engraphis Dashboard</string>
    <key>CFBundleIdentifier</key>
    <string>dev.engraphis.dashboard</string>
    <key>CFBundleVersion</key>
    <string>1.0</string>
    <key>CFBundleExecutable</key>
    <string>engraphis-dashboard</string>
    <key>CFBundleIconFile</key>
    <string>engraphis.icns</string>
    <key>LSBackgroundOnly</key>
    <string>0</string>
    <key>LSMinimumSystemVersion</key>
    <string>10.14</string>
</dict>
</plist>""",
        encoding="utf-8",
    )
    (resources / ".engraphis-owner").write_text(
        _OWNER_ID + "\n",
        encoding="utf-8",
    )

    desktop_link = desktop / "Engraphis Dashboard.app"
    desktop_link.symlink_to(app_dir)
    print(f"  Application created: {app_dir}")
    print("  Desktop alias created.")


def _linux(
    desktop: Path,
    args: argparse.Namespace,
    *,
    home: Path | None = None,
) -> None:
    # Desktop-entry values are line-oriented. Reject controls before mutating files.
    icon = _validated_icon_path(args.icon)
    home = Path.home() if home is None else home
    desktop_file_path = desktop / "engraphis-dashboard.desktop"
    app_dir = home / ".local" / "share" / "applications"
    app_dir.mkdir(parents=True, exist_ok=True)

    desktop_file = f"""[Desktop Entry]
Type=Application
Name=Engraphis Dashboard
Comment=Local AI memory engine WebUI
Exec=engraphis-dashboard
Icon={icon}
Terminal=false
Categories=Development;Utility;
Keywords=AI;memory;agent;dashboard;
StartupWMClass=engraphis-dashboard
{_LINUX_OWNER_LINE}
"""
    desktop_file_path.write_text(desktop_file, encoding="utf-8")
    os.chmod(desktop_file_path, 0o755)

    app_entry = app_dir / "engraphis-dashboard.desktop"
    shutil.copy2(desktop_file_path, app_entry)
    os.chmod(app_entry, 0o644)

    print(f"  Desktop shortcut created: {desktop_file_path}")
    print(f"  Application menu entry created: {app_entry}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Install desktop/Start Menu shortcuts for Engraphis Dashboard.")
    ap.add_argument("--silent", action="store_true",
                    help="Skip confirmation prompts.")
    ap.add_argument("--icon",
                    default=_icon_path(os.path.dirname(os.path.dirname(__file__))),
                    help="Path to the icon file.")
    ap.add_argument("--uninstall", action="store_true",
                    help="Remove previously installed shortcuts.")
    args = ap.parse_args()

    system = platform.system()
    home = Path.home()
    desktop = _desktop_path(system, home)
    start_menu = (Path(os.environ.get("APPDATA", ""))
                  / "Microsoft" / "Windows" / "Start Menu" / "Programs")

    if args.uninstall:
        print("Removing shortcuts...")
        removed = _remove_shortcuts(system, desktop, start_menu, home=home)
        if removed:
            for path in removed:
                print(f"  Removed: {path}")
        else:
            print("  No Engraphis shortcuts were found.")
        return

    # Validate before echoing the value to a terminal or mutating any launcher files.
    args.icon = _validated_icon_path(args.icon)

    if not desktop.exists():
        desktop = home / "Desktop"
    if not desktop.exists():
        print("Could not locate the Desktop folder.", file=sys.stderr)
        sys.exit(1)

    if not args.silent:
        print("Engraphis Dashboard — Shortcut Installer")
        print(f"  Platform: {system}")
        print("  Command:  engraphis-dashboard (opens http://127.0.0.1:8700)")
        print(f"  Icon:     {args.icon}")
        print()
        ok = input("Create shortcuts? [Y/n] ").strip().lower()
        if ok not in ("", "y", "yes"):
            sys.exit(0)

    print("Creating shortcuts...")
    paths = _shortcut_paths(system, desktop, start_menu, home=home)
    _prepare_install_paths(system, paths, home=home)

    if system == "Windows":
        _windows(desktop, start_menu, args)
    elif system == "Darwin":
        _macos(desktop, args, home=home)
    else:
        _linux(desktop, args, home=home)

    print()
    print("Done. Double-click the shortcut to open the Engraphis dashboard.")
    print("  http://127.0.0.1:8700")
    print()


if __name__ == "__main__":
    main()
