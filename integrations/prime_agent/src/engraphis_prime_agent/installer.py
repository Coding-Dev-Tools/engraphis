"""Idempotent registration of the integration with PrimeIntellect's prime-agent.

This module is the canonical, package-distributed implementation. The
``scripts/install_prime_agent.py`` wrapper at the repo root invokes this
module so the install/uninstall behaviour stays identical for both
``pip install`` users and source-tree developers.

The exact prime-agent config file path is the verification point: at
implementation time the implementer inspects
https://github.com/PrimeIntellect-ai/prime-agent and uses the documented
location. This module defaults to a JSON file at
``~/.config/prime-agent/config.json`` (or whatever ``PRIME_AGENT_CONFIG_PATH``
points at) and falls back to TOML when the file has a ``.toml`` extension.
The path and format can be confirmed and tightened once the prime-agent
repo is available.

Usage:
    python scripts/install_prime_agent.py
    python scripts/install_prime_agent.py --uninstall
"""
from __future__ import annotations

import argparse
import copy
import datetime
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

PACKAGE = "engraphis_prime_agent"
ENTRY = "PrimeAgentFleet"
TOOL_KEY = "engraphis"

# Default path; override with PRIME_AGENT_CONFIG_PATH.
_DEFAULT_PATH = Path.home() / ".config" / "prime-agent" / "config.json"


def _settings_path() -> Path:
    override = os.environ.get("PRIME_AGENT_CONFIG_PATH")
    if override:
        return Path(override)
    return _DEFAULT_PATH


def _utc_stamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d")


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    # Skip the backup when the file is brand new (zero bytes) or empty —
    # there's nothing meaningful to preserve, and the timestamp collision
    # on rapid successive runs is avoided.
    if path.stat().st_size == 0:
        return None
    # Use a collision-resistant suffix (UTC date + pid + unix-ms) so a
    # second run on the same UTC date captures the user's other tool
    # settings too. A pure per-day filename would overwrite the previous
    # backup and lose unrelated configuration.
    import os as _os
    import time as _time
    _pid = _os.getpid()
    _now_ms = int(_time.time() * 1000)
    base_name = (
        f"{path.name}.bak-engraphis-{_utc_stamp()}.{_pid}.{_now_ms}"
    )
    if path.with_name(base_name).exists():
        # Last-ditch uniqueness: append a counter until the name is free.
        counter = 0
        candidate_name = base_name
        while path.with_name(candidate_name).exists():
            counter += 1
            candidate_name = (
                f"{path.name}.bak-engraphis-{_utc_stamp()}.{_pid}."
                f"{_now_ms}.{counter}"
            )
        backup = path.with_name(candidate_name)
    else:
        backup = path.with_name(base_name)
    backup.write_bytes(path.read_bytes())
    shutil.copymode(path, backup)
    return backup


def _read(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return {}
    if path.suffix == ".json":
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            print(f"error: {path} is not valid JSON: {exc}", file=sys.stderr)
            sys.exit(2)
    if path.suffix == ".toml":
        try:
            import tomllib  # Python 3.11+
        except ImportError:
            print(
                f"error: reading {path} as TOML requires Python 3.11+ "
                "(tomllib is in the stdlib from 3.11 onward)",
                file=sys.stderr,
            )
            sys.exit(2)
        try:
            return tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            print(f"error: {path} is not valid TOML: {exc}", file=sys.stderr)
            sys.exit(2)
    print(
        f"error: unsupported config format for {path} "
        f"(expected .json or .toml, got {path.suffix!r})",
        file=sys.stderr,
    )
    sys.exit(2)


def _ensure_writable_parent(path: Path) -> None:
    """Refuse to write if the parent directory is not writable.

    Catches the common failure modes early: missing parent on a read-only
    filesystem, an unwritable existing directory, or a path whose parent is a
    file. The actual write still happens after this check, so a TOCTOU race is
    technically possible, but in practice the only way to fail here is the
    configuration the user is asking us to use.
    """
    parent = path.parent
    if parent.exists() and not parent.is_dir():
        print(
            f"error: parent of {path} exists but is not a directory: {parent}",
            file=sys.stderr,
        )
        sys.exit(2)
    if not parent.exists():
        # We will create it; check that we can. ``os.access`` on a non-existent
        # path checks the nearest existing ancestor, which is what we want.
        ancestor = parent
        while not ancestor.exists():
            ancestor = ancestor.parent
        if not os.access(str(ancestor), os.W_OK):
            print(
                f"error: cannot create {path}: no write access to {ancestor}",
                file=sys.stderr,
            )
            sys.exit(2)
        return
    if not os.access(str(parent), os.W_OK):
        print(
            f"error: parent directory of {path} is not writable: {parent}",
            file=sys.stderr,
        )
        sys.exit(2)


def _write(path: Path, data: dict[str, Any]) -> None:
    _ensure_writable_parent(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".json":
        path.write_text(
            json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return
    if path.suffix == ".toml":
        try:
            import tomli_w
        except ImportError:
            print(
                f"error: writing {path} as TOML requires the 'tomli_w' package; "
                "install it with: pip install 'tomli_w>=1.0' "
                "(it is not bundled with the engraphis core package)",
                file=sys.stderr,
            )
            sys.exit(2)
        # tomli_w.dumps returns str, not bytes — use write_text, not write_bytes.
        path.write_text(tomli_w.dumps(data), encoding="utf-8")
        return
    print(
        f"error: unsupported config format for {path} "
        f"(expected .json or .toml, got {path.suffix!r})",
        file=sys.stderr,
    )
    sys.exit(2)


def _entry() -> dict[str, str]:
    # Use the underscore-separated import name as the distribution name; the
    # PyPI distribution is engraphis-prime-agent (hyphenated) but the
    # Python import path is engraphis_prime_agent (underscored).
    return {
        "package": "engraphis-prime-agent",
        "import": PACKAGE,
        "entry": ENTRY,
    }


def _dry_run(path: Path, before: dict[str, Any], after: dict[str, Any]) -> None:
    print("--- before")
    print(json.dumps(before, indent=2, sort_keys=True))
    print("--- after")
    print(json.dumps(after, indent=2, sort_keys=True))
    print(f"(dry-run) no changes written to {path}")


def install(
    path: Path | None = None,
    *,
    merge: bool = False,
    dry_run: bool = False,
) -> None:
    path = path or _settings_path()
    cfg = _read(path)
    # Deep-copy so the dry-run snapshot does not observe the mutations
    # below: ``cfg.setdefault("tools", {})`` would otherwise return a
    # reference to the same nested dict that we then overwrite with the
    # new entry, mutating ``before`` as well.
    before = copy.deepcopy(cfg)
    tools = cfg.setdefault("tools", {})
    entry = _entry()
    if merge and isinstance(tools.get(TOOL_KEY), dict):
        # Preserve operator-supplied keys under the tools.engraphis table.
        merged = dict(tools[TOOL_KEY])
        merged.update(entry)
        tools[TOOL_KEY] = merged
    else:
        tools[TOOL_KEY] = entry
    if dry_run:
        _dry_run(path, before, cfg)
        return
    _backup(path)
    _write(path, cfg)
    print(f"installed engraphis-prime-agent into {path}")


def uninstall(
    path: Path | None = None,
    *,
    dry_run: bool = False,
) -> None:
    path = path or _settings_path()
    cfg = _read(path)
    # Deep-copy so the dry-run snapshot does not observe the deletions
    # below: ``tools.pop(TOOL_KEY)`` mutates the same nested mapping that
    # ``before`` still points at, so the printed "before" would show the
    # already-removed key.
    before = copy.deepcopy(cfg)
    tools = cfg.get("tools", {})
    if TOOL_KEY not in tools:
        if dry_run:
            _dry_run(path, before, before)
        else:
            print(f"no engraphis entry in {path}")
        return
    del tools[TOOL_KEY]
    if not tools:
        cfg.pop("tools", None)
    if dry_run:
        _dry_run(path, before, cfg)
        return
    _backup(path)
    _write(path, cfg)
    print(f"removed engraphis entry from {path}")


def _resolve_config_path(explicit: str | None) -> Path | None:
    """CLI flag → env var → None (use default). Empty string is treated as unset."""
    if explicit:
        return Path(explicit)
    env = os.environ.get("PRIME_AGENT_CONFIG_PATH")
    if env:
        return Path(env)
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n", 1)[0])
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument(
        "--config-path",
        default=None,
        help="Override the prime-agent config file path (defaults to "
        "$PRIME_AGENT_CONFIG_PATH or ~/.config/prime-agent/config.json).",
    )
    parser.add_argument(
        "--merge",
        action="store_true",
        help="Merge with any existing [tools.engraphis] entry instead of replacing it.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the before/after diff and exit without writing or backing up.",
    )
    args = parser.parse_args(argv)
    path = _resolve_config_path(args.config_path)
    if args.uninstall:
        uninstall(path, dry_run=args.dry_run)
    else:
        install(path, merge=args.merge, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
