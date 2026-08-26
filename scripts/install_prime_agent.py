# -*- coding: utf-8 -*-
"""Idempotently register the engraphis-prime-agent integration with prime-agent.

The exact prime-agent config file path is the verification point: at
implementation time the implementer inspects
https://github.com/PrimeIntellect-ai/prime-agent and uses the documented
location. This script defaults to a JSON file at
``~/.config/prime-agent/config.json`` (or whatever ``PRIME_AGENT_CONFIG_PATH``
points at) and falls back to a thin TOML block if the file has a ``.toml``
extension. The path and format can be confirmed and tightened once the
prime-agent repo is available.

Format support:
    * ``.json`` — stdlib ``json`` only; works on every supported Python.
    * ``.toml`` — requires Python 3.11+ for ``tomllib`` (reading) and the
      third-party ``tomli_w`` package for writing. ``tomli_w`` is NOT
      installed by ``pip install engraphis`` because the core package does
      not need it; install it manually with
      ``pip install 'tomli_w>=1.0'`` before using this script against a
      ``.toml`` config.

Usage:
    python scripts/install_prime_agent.py
    python scripts/install_prime_agent.py --uninstall
    python scripts/install_prime_agent.py --config-path /path/to/config.json
    python scripts/install_prime_agent.py --merge        # preserve custom fields
    python scripts/install_prime_agent.py --dry-run      # show the change, write nothing

Resolution order for the config path:
    1. ``--config-path`` CLI flag (highest priority).
    2. ``PRIME_AGENT_CONFIG_PATH`` environment variable.
    3. ``~/.config/prime-agent/config.json`` (default).
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from pathlib import Path

# Distribution name on PyPI (hyphenated, as published by the integration's
# pyproject.toml: ``name = "engraphis-prime-agent"``). prime-agent uses this to
# resolve and install the package, so it must NOT be underscored.
DISTRIBUTION = "engraphis-prime-agent"
# Importable Python module name (underscored — the import name differs from the
# distribution name in this project, as it does for most hyphenated PyPI names).
IMPORT_NAME = "engraphis_prime_agent"
ENTRY = "PrimeAgentFleet"
TOOL_KEY = "engraphis"

# Default path; override with --config-path or PRIME_AGENT_CONFIG_PATH.
_DEFAULT_PATH = Path.home() / ".config" / "prime-agent" / "config.json"


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def _settings_path(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the config file path.

    Precedence: explicit ``--config-path`` argument > ``PRIME_AGENT_CONFIG_PATH``
    env var > the built-in default. Empty strings are treated as unset.
    """
    if explicit is not None and str(explicit) != "":
        return Path(explicit)
    override = os.environ.get("PRIME_AGENT_CONFIG_PATH")
    if override:
        return Path(override)
    return _DEFAULT_PATH


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------


def _utc_stamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d")


def _backup(path: Path) -> Path | None:
    """Copy ``path`` to a dated sibling before any in-place write.

    Returns the backup path, or ``None`` if no backup was needed (the file did
    not yet exist, or is empty — there is nothing to back up). The filename
    format is fixed: ``<name>.bak-engraphis-<UTC-date>`` and is part of the
    public contract documented in the project README.
    """
    if not path.exists():
        return None
    if path.stat().st_size == 0:
        return None
    backup = path.with_name(f"{path.name}.bak-engraphis-{_utc_stamp()}")
    if backup.exists():
        return backup
    backup.write_bytes(path.read_bytes())
    return backup


# ---------------------------------------------------------------------------
# Read / write
# ---------------------------------------------------------------------------


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return {}
    if path.suffix == ".json":
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            print(
                f"error: {path} is not valid JSON: {exc}",
                file=sys.stderr,
            )
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


def _write(path: Path, data: dict) -> None:
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
        path.write_bytes(tomli_w.dumps(data))
        return
    print(
        f"error: unsupported config format for {path} "
        f"(expected .json or .toml, got {path.suffix!r})",
        file=sys.stderr,
    )
    sys.exit(2)


# ---------------------------------------------------------------------------
# Entry construction
# ---------------------------------------------------------------------------


def _entry() -> dict:
    """Build the ``[tools.engraphis]`` snippet written to the config.

    The ``package`` key is the PyPI distribution name (hyphenated); the
    ``import`` key is the Python module name (underscored). They are NOT the
    same string for this integration: prime-agent installs ``package`` and
    then runs ``from <import> import <entry>``.
    """
    return {"package": DISTRIBUTION, "import": IMPORT_NAME, "entry": ENTRY}


# ---------------------------------------------------------------------------
# Install / uninstall
# ---------------------------------------------------------------------------


def _print_diff(before: dict, after: dict) -> None:
    """Render a minimal before/after diff for ``--dry-run``."""
    print("--- before")
    print(json.dumps(before, indent=2, sort_keys=True))
    print("--- after")
    print(json.dumps(after, indent=2, sort_keys=True))


def install(
    *,
    config_path: str | os.PathLike[str] | None = None,
    merge: bool = False,
    dry_run: bool = False,
) -> int:
    """Register the engraphis integration into the prime-agent config.

    Parameters
    ----------
    config_path:
        Explicit path to the config file. Overrides the
        ``PRIME_AGENT_CONFIG_PATH`` env var and the built-in default.
    merge:
        If True, preserve any extra keys already present in the existing
        ``[tools.engraphis]`` entry (only the ``package``/``import``/``entry``
        keys we own are updated). If False (the default), the entire entry is
        replaced — this is the safe idempotent behavior, but it WILL clobber
        any user-added keys in that sub-table.
    dry_run:
        If True, print the diff between the current and proposed config and do
        not write or back up anything.

    Returns the process exit code (0 on success).
    """
    path = _settings_path(config_path)
    cfg = _read(path)
    before = json.loads(json.dumps(cfg))  # deep copy for the diff

    tools = cfg.setdefault("tools", {})
    new_entry = _entry()
    if merge and isinstance(tools.get(TOOL_KEY), dict):
        merged = dict(tools[TOOL_KEY])
        merged.update(new_entry)
        tools[TOOL_KEY] = merged
    else:
        tools[TOOL_KEY] = new_entry

    if dry_run:
        _print_diff(before, cfg)
        print(f"(dry-run) no changes written to {path}")
        return 0

    backup = _backup(path)
    _write(path, cfg)
    if backup is not None:
        print(f"installed engraphis-prime-agent into {path} (backup: {backup})")
    else:
        print(f"installed engraphis-prime-agent into {path}")
    return 0


def uninstall(
    *,
    config_path: str | os.PathLike[str] | None = None,
    dry_run: bool = False,
) -> int:
    """Remove the engraphis integration from the prime-agent config."""
    path = _settings_path(config_path)
    cfg = _read(path)
    before = json.loads(json.dumps(cfg))

    tools = cfg.get("tools", {})
    if TOOL_KEY not in tools:
        if dry_run:
            _print_diff(before, cfg)
            print(f"(dry-run) no engraphis entry in {path}")
            return 0
        print(f"no engraphis entry in {path}")
        return 0

    del tools[TOOL_KEY]
    if not tools:
        cfg.pop("tools", None)

    if dry_run:
        _print_diff(before, cfg)
        print(f"(dry-run) no changes written to {path}")
        return 0

    backup = _backup(path)
    _write(path, cfg)
    if backup is not None:
        print(f"removed engraphis entry from {path} (backup: {backup})")
    else:
        print(f"removed engraphis entry from {path}")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="install_prime_agent.py",
        description=__doc__.split("\n\n", 1)[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="Remove the engraphis entry instead of installing it.",
    )
    parser.add_argument(
        "--config-path",
        default=None,
        metavar="PATH",
        help=(
            "Path to the prime-agent config file. Overrides the "
            "PRIME_AGENT_CONFIG_PATH environment variable and the built-in "
            "default of ~/.config/prime-agent/config.json."
        ),
    )
    parser.add_argument(
        "--merge",
        action="store_true",
        help=(
            "Preserve any extra keys already present in the existing "
            "[tools.engraphis] entry; only the package/import/entry keys we "
            "own are updated. Without this flag the entire entry is replaced."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the diff between the current and proposed config and exit "
        "without writing or creating a backup.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.uninstall:
        return uninstall(config_path=args.config_path, dry_run=args.dry_run)
    return install(
        config_path=args.config_path,
        merge=args.merge,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    sys.exit(main())
