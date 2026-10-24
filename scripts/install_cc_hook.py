# -*- coding: utf-8 -*-
"""Install Engraphis Command Code hooks into the user-scope settings file.

Idempotent: running twice updates the existing entry rather than duplicating it.
Backup is written to ``<settings>.bak-engraphis-<UTC-date>`` on first write only.

Usage:
    python scripts/install_cc_hook.py
    python scripts/install_cc_hook.py --file-guard
    python scripts/install_cc_hook.py --uninstall
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

SETTINGS_PATH = Path(os.environ.get("COMMANDCODE_SETTINGS_PATH")
                     or Path.home() / ".commandcode" / "settings.json")
HOOK_PATH = Path(__file__).resolve().parent.parent / "integrations" / "commandcode" / "session_start_hook.py"
HOOK_KEY = "cc-engraphis-session-start"
FILE_GUARD_PATH = (
    Path(__file__).resolve().parent.parent
    / "integrations" / "commandcode" / "file_guard_hook.py"
)
FILE_GUARD_KEY = "cc-engraphis-file-guard"
# Command Code tests matchers against display names (WRITE/EDIT/SHELL) while documenting
# lowercase literals; list both spellings so the guard fires whether or not matching is
# case-sensitive. The canonical tool ids are listed as well: a host that matches the
# matcher against the tool *id* it sends would otherwise never route ``write_file``,
# ``edit_file`` or ``shell_command`` to the guard at all. The capitalized names keep
# Claude-Code-style hosts covered.
FILE_GUARD_MATCHER = (
    "write|edit|shell|WRITE|EDIT|SHELL"
    "|Write|Edit|MultiEdit|NotebookEdit|ApplyPatch|Bash"
    "|write_file|edit_file|shell_command"
    "|create_file|delete_file|move_file|copy_file|update_file|write_to_file"
    "|str_replace_editor"
)
# Matchers written by earlier unreleased installers; upgraded in place on reinstall.
_LEGACY_FILE_GUARD_MATCHERS = frozenset({
    "Write|Edit|MultiEdit|NotebookEdit|Bash",
    "write|edit|shell|Write|Edit|MultiEdit|NotebookEdit|Bash",
    "write|edit|shell|WRITE|EDIT|SHELL|Write|Edit|MultiEdit|NotebookEdit|Bash",
})


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
        text = settings_path.read_text(encoding="utf-8") or "{}"
    except OSError as exc:
        print(f"error: cannot read {settings_path}: {exc}", file=sys.stderr)
        sys.exit(2)
    try:
        settings = json.loads(text)
    except json.JSONDecodeError as exc:
        print(f"error: {settings_path} is not valid JSON: {exc}", file=sys.stderr)
        sys.exit(2)
    if not isinstance(settings, dict):
        print(f"error: {settings_path} must contain a JSON object", file=sys.stderr)
        sys.exit(2)
    hooks = settings.get("hooks")
    if hooks is not None and not isinstance(hooks, dict):
        print(f"error: {settings_path} has a non-object 'hooks' entry", file=sys.stderr)
        sys.exit(2)
    for event, wrappers in (hooks or {}).items():
        if not isinstance(wrappers, list):
            print(
                f"error: {settings_path} has a non-list 'hooks.{event}' entry",
                file=sys.stderr,
            )
            sys.exit(2)
        for wrapper in wrappers:
            if not isinstance(wrapper, dict):
                print(
                    f"error: {settings_path} has a non-object 'hooks.{event}' entry",
                    file=sys.stderr,
                )
                sys.exit(2)
            entries = wrapper.get("hooks", [])
            if not isinstance(entries, list) or not all(
                isinstance(entry, dict) for entry in entries
            ):
                print(
                    f"error: {settings_path} has a malformed 'hooks.{event}.hooks' entry",
                    file=sys.stderr,
                )
                sys.exit(2)
    return settings


def _fsync_directory(directory: Path) -> None:
    """Best-effort persist of a rename in ``directory``.

    Windows has no directory descriptors, and some network filesystems reject a
    directory fsync. The replace has already succeeded by then, so a refusal here
    must not turn a completed install into a reported failure.
    """
    if os.name == "nt":
        return
    try:
        descriptor = os.open(str(directory), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _write_settings(settings_path: Path, settings: dict) -> None:
    """Write the settings file atomically, preserving its permissions and key order.

    A crash or a concurrent run must not leave the operator with a truncated
    settings file, so the new content is staged next to the target, flushed to
    disk, and moved into place with an atomic replace whose directory entry is
    then synced too. Keys keep the order the operator wrote them in.

    This standalone script deliberately stays stdlib-only, so it mirrors
    ``engraphis.private_state.atomic_private_text`` rather than importing it.
    That helper also defaults to a private ``0o600`` mode and rejects a concurrent
    edit as a conflict; this one keeps the settings file's existing mode.
    """
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(settings, indent=2) + "\n"
    try:
        mode = stat.S_IMODE(settings_path.stat().st_mode)
    except OSError:
        mode = None
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(settings_path.parent),
        prefix=f"{settings_path.name}.",
        suffix=".tmp",
        delete=False,
    )
    staged = handle.name
    try:
        with handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(staged, mode)
        os.replace(staged, str(settings_path))
    except BaseException:
        try:
            os.unlink(staged)
        except OSError:
            pass
        raise
    _fsync_directory(settings_path.parent)


def _hook_entry() -> dict:
    return {
        "type": "command",
        "command": f"\"{sys.executable}\" \"{HOOK_PATH}\"",
        "name": HOOK_KEY,
        "timeout": 5,
    }


def _entry_command() -> str:
    return _hook_entry()["command"]


def _file_guard_entry() -> dict:
    return {
        "type": "command",
        "command": f"\"{sys.executable}\" \"{FILE_GUARD_PATH}\"",
        "name": FILE_GUARD_KEY,
        "timeout": 5,
    }


def _is_our_entry(entry: dict) -> bool:
    """Match by stable ``name`` (HOOK_KEY) first, then fall back to command string.

    The command path can vary across installations, so a stable identifier is the
    primary key; the command match stays as a backstop for legacy entries written
    by older versions of this script.
    """
    if entry.get("name") in {HOOK_KEY, FILE_GUARD_KEY}:
        return True
    return entry.get("command", "") in {
        _entry_command(), _file_guard_entry()["command"],
        # Before the explicit interpreter path, nameless legacy entries used this form.
        f'python "{HOOK_PATH}"',
    }


def _is_file_guard_entry(entry: dict) -> bool:
    """Match the file-guard hook by its stable name, then by its command.

    The command string is a second identity for an entry whose ``name`` was
    edited or dropped by hand, so a reinstall upgrades that entry instead of
    appending a second copy of the guard.
    """
    if entry.get("name") == FILE_GUARD_KEY:
        return True
    return entry.get("command", "") == _file_guard_entry()["command"]


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
    it). Preserves every sibling inner entry the operator added manually and
    every wrapper-level key (e.g. ``matcher``) so uninstall does not silently
    drop the operator's filter config.
    """
    remaining = [
        entry for entry in wrapper.get("hooks", []) or []
        if not _is_our_entry(entry)
    ]
    if not remaining:
        return None
    new_wrapper = dict(wrapper)
    new_wrapper["hooks"] = remaining
    return new_wrapper


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
        # Reuse the existing wrapper dict so any wrapper-level keys the
        # operator added (e.g. ``matcher``) are preserved; only swap the
        # inner ``hooks`` list.
        wrapper["hooks"] = [*siblings, _hook_entry()]
    return refreshed


def install(*, enable_file_guard: bool = False) -> None:
    settings = _read_settings(SETTINGS_PATH)
    hooks = settings.setdefault("hooks", {}).setdefault("SessionStart", [])
    # Idempotency: refresh our entry in-place inside any wrapper that already
    # contains it, so a manually-added sibling inner hook is preserved and we
    # do not append a second wrapper. Only fall through to a fresh append when
    # no prior wrapper mentions us.
    if not _refresh_existing_wrappers(hooks):
        hooks.append({"hooks": [_hook_entry()]})
    if enable_file_guard:
        guard_hooks = settings.setdefault("hooks", {}).setdefault("PreToolUse", [])
        found = False
        for wrapper in guard_hooks:
            entries = wrapper.get("hooks", []) or []
            if any(_is_file_guard_entry(entry) for entry in entries):
                wrapper["hooks"] = [
                    entry for entry in entries if not _is_file_guard_entry(entry)
                ] + [_file_guard_entry()]
                # A matcher an earlier installer wrote would skip Command Code's real tool
                # names; upgrade it, but keep any matcher the operator customized.
                if wrapper.get("matcher") in _LEGACY_FILE_GUARD_MATCHERS:
                    wrapper["matcher"] = FILE_GUARD_MATCHER
                found = True
        if not found:
            guard_hooks.append({
                "matcher": FILE_GUARD_MATCHER,
                "hooks": [_file_guard_entry()],
            })
    _backup(SETTINGS_PATH)
    _write_settings(SETTINGS_PATH, settings)
    print(f"installed SessionStart hook into {SETTINGS_PATH}")
    if enable_file_guard:
        print(f"installed the file-protection PreToolUse hook into {SETTINGS_PATH}")


def uninstall() -> None:
    settings = _read_settings(SETTINGS_PATH)
    if "hooks" not in settings:
        print(f"no Engraphis hook entry in {SETTINGS_PATH}")
        return
    for event in ("SessionStart", "PreToolUse"):
        if event not in settings["hooks"]:
            continue
        cleaned = []
        for wrapper in settings["hooks"][event]:
            stripped = _strip_our_entries(wrapper)
            if stripped is not None:
                cleaned.append(stripped)
        settings["hooks"][event] = cleaned
        if not cleaned:
            del settings["hooks"][event]
    if not settings["hooks"]:
        del settings["hooks"]
    _backup(SETTINGS_PATH)
    _write_settings(SETTINGS_PATH, settings)
    print(f"removed the Engraphis hooks from {SETTINGS_PATH}")


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n", 1)[0])
    parser.add_argument("--uninstall", action="store_true",
                        help="Remove the Engraphis SessionStart hook from the user settings.")
    parser.add_argument(
        "--file-guard", action="store_true",
        help="Also install the fail-closed protected-file PreToolUse hook.",
    )
    args = parser.parse_args()
    if args.uninstall:
        # Removal must work even when the hook scripts are gone: the entries
        # in the settings file have to be removable on their own.
        try:
            uninstall()
        except OSError as exc:
            print(f"error: could not update {SETTINGS_PATH}: {exc}", file=sys.stderr)
            return 2
        return 0
    if not HOOK_PATH.exists():
        print(f"error: hook script not found at {HOOK_PATH}", file=sys.stderr)
        return 2
    if args.file_guard and not FILE_GUARD_PATH.exists():
        print(f"error: file guard hook not found at {FILE_GUARD_PATH}", file=sys.stderr)
        return 2
    try:
        install(enable_file_guard=args.file_guard)
    except OSError as exc:
        print(f"error: could not update {SETTINGS_PATH}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
