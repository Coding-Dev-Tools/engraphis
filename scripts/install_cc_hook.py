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


def _strip_our_entry(wrapper: dict) -> tuple[dict | None, bool]:
    """Remove our inner entry from a wrapper; return (new_wrapper, removed).

    ``new_wrapper`` is ``None`` if the wrapper should be dropped entirely
    (no remaining inner entries). Otherwise the wrapper keeps its sibling
    inner entries verbatim so a manually-added sibling hook is preserved.
    """
    target = _entry_command()
    inner = wrapper.get("hooks", []) or []
    kept = [
        entry for entry in inner
        if entry.get("command", "") != target
    ]
    if len(kept) == len(inner):
        # Our entry wasn't here; leave the wrapper untouched.
        return wrapper, False
    if not kept:
        return None, True
    new_wrapper = dict(wrapper)
    new_wrapper["hooks"] = kept
    return new_wrapper, True


def _session_start_has_our_entry(hooks: list) -> bool:
    """Each SessionStart entry is ``{"hooks": [{"command": ...}, ...]}``."""
    target = _entry_command()
    for wrapper in hooks:
        for entry in wrapper.get("hooks", []) or []:
            if entry.get("command", "") == target:
                return True
    return False


def _strip_our_entries(hooks: list) -> list:
    """Return a new SessionStart list with our inner entry removed per wrapper.

    Wrappers that contained our entry alongside a sibling inner entry keep
    the sibling intact; wrappers that contained only our entry are dropped.
    Wrappers that did not contain our entry are returned verbatim.
    """
    new_hooks: list = []
    for wrapper in hooks:
        stripped, removed = _strip_our_entry(wrapper)
        if not removed:
            new_hooks.append(wrapper)
        elif stripped is not None:
            new_hooks.append(stripped)
    return new_hooks


def install() -> None:
    settings = _read_settings(SETTINGS_PATH)
    hooks = settings.setdefault("hooks", {}).setdefault("SessionStart", [])
    # Remove any prior copy of our entry (idempotency) per wrapper, then
    # add our entry. If a stripped wrapper still has sibling inner entries
    # (the operator had a manually-added hook in the same wrapper), append
    # our entry to that same wrapper so we don't end up with two
    # single-entry wrappers that are effectively one logical SessionStart
    # entry.
    if _session_start_has_our_entry(hooks):
        new_hooks: list = []
        reattach_target: dict | None = None
        for wrapper in hooks:
            stripped, removed = _strip_our_entry(wrapper)
            if not removed:
                new_hooks.append(wrapper)
            elif stripped is not None:
                # Wrapper had siblings; remember it as the target to
                # reattach our entry to.
                reattach_target = stripped
                new_hooks.append(stripped)
        hooks[:] = new_hooks
        if reattach_target is not None:
            reattach_target["hooks"] = list(reattach_target.get("hooks", [])) + [_hook_entry()]
        else:
            hooks.append({"hooks": [_hook_entry()]})
    else:
        hooks.append({"hooks": [_hook_entry()]})
    _backup(SETTINGS_PATH)
    _write_settings(SETTINGS_PATH, settings)
    print(f"installed SessionStart hook into {SETTINGS_PATH}")


def uninstall() -> None:
    settings = _read_settings(SETTINGS_PATH)
    if "hooks" not in settings or "SessionStart" not in settings["hooks"]:
        print(f"no SessionStart hook entry in {SETTINGS_PATH}")
        return
    settings["hooks"]["SessionStart"] = _strip_our_entries(settings["hooks"]["SessionStart"])
    if not settings["hooks"]["SessionStart"]:
        del settings["hooks"]["SessionStart"]
    if not settings["hooks"]:
        del settings["hooks"]
    _backup(SETTINGS_PATH)
    _write_settings(SETTINGS_PATH, settings)
    print(f"removed SessionStart hook from {SETTINGS_PATH}")


def main() -> int:
    description = __doc__.split("\n\n", 1)[0] if __doc__ else None
    parser = argparse.ArgumentParser(description=description)
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
