"""Explicit installation intent, scoped to a trusted config and Python environment.

Package metadata cannot tell which extras the owner selected. Only an explicit setup
choice creates this profile; old or unreadable profiles retain the updater's fallback.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

from engraphis.private_state import atomic_private_text, ensure_owner_private_dir, read_private_text


def normalize_extras(value: str) -> list[str]:
    """Validate package-extra names without interpreting them as command arguments."""
    if value.strip().casefold() in {"", "none", "base"}:
        return []
    names = [name.strip().lower() for name in value.split(",") if name.strip()]
    if not names or any(not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", name) for name in names):
        raise ValueError("extras must be comma-separated package-extra names or 'none'")
    return sorted(set(names))


def _environment() -> str:
    return os.path.normcase(str(Path(sys.prefix).resolve()))


def profile_path(config_path: Optional[Path] = None) -> Path:
    if config_path is None:
        from engraphis.config import trusted_env_path
        config_path = trusted_env_path()
    key = hashlib.sha256(_environment().encode("utf-8")).hexdigest()[:24]
    return config_path.parent / "installations" / (key + ".json")


def write_profile(extras: list[str], *, config_path: Optional[Path] = None) -> None:
    path = profile_path(config_path)
    names = normalize_extras(",".join(extras))
    ensure_owner_private_dir(path.parent)
    atomic_private_text(path, json.dumps({"schema_version": 1, "environment": _environment(),
                                         "extras": names}, sort_keys=True) + "\n")


def read_profile() -> Optional[list[str]]:
    """Return explicit intent, including [] for base, or None when it is unknown."""
    try:
        raw = read_private_text(profile_path(), max_bytes=8192, owner_only=True)
        if not raw:
            return None
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("schema_version") != 1 or data.get("environment") != _environment():
            return None
        extras = data.get("extras")
        if not isinstance(extras, list) or not all(isinstance(name, str) for name in extras):
            return None
        normalized = normalize_extras(",".join(extras))
        return normalized if normalized == extras else None
    except (OSError, ValueError, RuntimeError):
        return None
