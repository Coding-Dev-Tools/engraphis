# -*- coding: utf-8 -*-
"""Install the Engraphis Command Code SessionStart hook into the user-scope settings file.

Idempotent: running twice updates the existing entry rather than duplicating it.
Backup is written to ``<settings>.bak-engraphis-<UTC-date>`` on first write only.

Usage:
    python scripts/install_cc_hook.py
    python scripts/install_cc_hook.py --uninstall
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from pathlib import Path

SETTINGS_PATH = Path(os.environ.get("COMMANDCODE_SETTINGS_PATH")
                     or Path.home() / ".commandcode" / "settings.json")
HOOK_PATH = Path(__file__).resolve().parent.parent / "integrations" / "commandcode" / "session_start_hook.py"
HOOK_KEY = "cc-engraphis-session-start"


def _utc_stamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d")


def _backup(settings_path: Path) -> Path | None:
    if not settings_path.exists():
        return None
    backup = settings_path.with_name(
        f"{settings_path.name}.bak-engraphis-{_utc_stamp()}")
    if backup.exists():
        return backup
    backup.write_bytes(settings_path.read_bytes())
    return backup


def _read_settings(settings_path: Path) -> dict:
    if not settings_path.exists():
        return {}
    try:
        return json.loads(settings_path.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError as exc:
        print(f"error: {settings_path} is not valid JSON: {exc}", file=sys.stderr)
        sys.exit(2)


def _write_settings(settings_path: Path, settings: dict) -> None:
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps(settings, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _hook_entry() -> dict:
    return {
        "type": "command",
        "command": f"python \"{HOOK_PATH}\"",
        "timeout": 5,
    }


def _entry_command() -> str:
    return _hook_entry()["command"]


def _session_start_has_our_entry(hooks: list) -> bool:
    """Each SessionStart entry is ``{"hooks": [{"command": ...}, ...]}``."""
    target = _entry_command()
    for wrapper in hooks:
        for entry in wrapper.get("hooks", []) or []:
            if entry.get("command", "") == target:
                return True
    return False


def install() -> None:
    settings = _read_settings(SETTINGS_PATH)
    hooks = settings.setdefault("hooks", {}).setdefault("SessionStart", [])
    # Remove any prior copy of our entry (idempotency), then append a fresh one.
    # Each entry is a wrapper of one or more inner hook objects; inspect the
    # inner "command" so the filter matches the shape uninstall() uses.
    if _session_start_has_our_entry(hooks):
        hooks[:] = [
            wrapper for wrapper in hooks
            if not any(
                entry.get("command", "") == _entry_command()
                for entry in wrapper.get("hooks", []) or []
            )
        ]
    hooks.append({"hooks": [_hook_entry()]})
    _backup(SETTINGS_PATH)
    _write_settings(SETTINGS_PATH, settings)
    print(f"installed SessionStart hook into {SETTINGS_PATH}")


def uninstall() -> None:
    settings = _read_settings(SETTINGS_PATH)
    if "hooks" not in settings or "SessionStart" not in settings["hooks"]:
        print(f"no SessionStart hook entry in {SETTINGS_PATH}")
        return
    settings["hooks"]["SessionStart"] = [
        h for h in settings["hooks"]["SessionStart"]
        if not any(
            e.get("command", "") == _entry_command()
            for e in h.get("hooks", [])
        )
    ]
    if not settings["hooks"]["SessionStart"]:
        del settings["hooks"]["SessionStart"]
    if not settings["hooks"]:
        del settings["hooks"]
    _backup(SETTINGS_PATH)
    _write_settings(SETTINGS_PATH, settings)
    print(f"removed SessionStart hook from {SETTINGS_PATH}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--uninstall", action="store_true",
                        help="Remove the Engraphis SessionStart hook from the user settings.")
    args = parser.parse_args()
    if not HOOK_PATH.exists():
        print(f"error: hook script not found at {HOOK_PATH}", file=sys.stderr)
        return 2
    if args.uninstall:
        uninstall()
    else:
        install()
    return 0


if __name__ == "__main__":
    sys.exit(main())
