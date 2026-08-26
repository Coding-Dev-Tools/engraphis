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
        "name": HOOK_KEY,
        "timeout": 5,
    }


def _entry_command() -> str:
    return _hook_entry()["command"]


def _is_our_entry(entry: dict) -> bool:
    """Match by stable ``name`` (HOOK_KEY) first, then fall back to command string.

    The command path can vary across installations, so a stable identifier is the
    primary key; the command match stays as a backstop for legacy entries written
    by older versions of this script.
    """
    if entry.get("name") == HOOK_KEY:
        return True
    return entry.get("command", "") == _entry_command()


def _session_start_has_our_entry(hooks: list) -> bool:
    """Each SessionStart entry is ``{"hooks": [{"command": ...}, ...]}``."""
    for wrapper in hooks:
        for entry in wrapper.get("hooks", []) or []:
            if _is_our_entry(entry):
                return True
    return False


def _strip_our_entries(wrapper: dict) -> dict | None:
    """Return a new wrapper with our inner entries removed.

    Returns ``None`` if the wrapper becomes empty after stripping (caller drops
    it). Preserves every sibling inner entry the operator added manually.
    """
    remaining = [
        entry for entry in wrapper.get("hooks", []) or []
        if not _is_our_entry(entry)
    ]
    if not remaining:
        return None
    return {"hooks": remaining}


def _refresh_existing_wrappers(hooks: list) -> bool:
    """Replace any of our inner entries in-place inside matching wrappers.

    Returns ``True`` if at least one wrapper already contained our entry (so the
    caller knows a fresh append is not required). Sibling inner entries are
    preserved; a wrapper that contained only our entry is replaced with the
    fresh entry instead of being kept as an empty wrapper.
    """
    refreshed = False
    for i, wrapper in enumerate(hooks):
        inner = wrapper.get("hooks", []) or []
        if not any(_is_our_entry(e) for e in inner):
            continue
        refreshed = True
        siblings = [e for e in inner if not _is_our_entry(e)]
        # Re-add the fresh entry alongside the siblings so the original
        # wrapper is preserved verbatim except for our entry being replaced.
        hooks[i] = {"hooks": [*siblings, _hook_entry()]}
    return refreshed


def install() -> None:
    settings = _read_settings(SETTINGS_PATH)
    hooks = settings.setdefault("hooks", {}).setdefault("SessionStart", [])
    # Idempotency: refresh our entry in-place inside any wrapper that already
    # contains it, so a manually-added sibling inner hook is preserved and we
    # do not append a second wrapper. Only fall through to a fresh append when
    # no prior wrapper mentions us.
    if not _refresh_existing_wrappers(hooks):
        hooks.append({"hooks": [_hook_entry()]})
    _backup(SETTINGS_PATH)
    _write_settings(SETTINGS_PATH, settings)
    print(f"installed SessionStart hook into {SETTINGS_PATH}")


def uninstall() -> None:
    settings = _read_settings(SETTINGS_PATH)
    if "hooks" not in settings or "SessionStart" not in settings["hooks"]:
        print(f"no SessionStart hook entry in {SETTINGS_PATH}")
        return
    cleaned: list = []
    for wrapper in settings["hooks"]["SessionStart"]:
        stripped = _strip_our_entries(wrapper)
        if stripped is not None:
            cleaned.append(stripped)
    settings["hooks"]["SessionStart"] = cleaned
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
