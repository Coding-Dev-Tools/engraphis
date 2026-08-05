"""Dependency-free provenance policy for optional Hugging Face loaders."""
from __future__ import annotations

import os
import re
from pathlib import Path, PureWindowsPath
from typing import Optional


_IMMUTABLE_REVISION = re.compile(r"[0-9a-f]{40}\Z")
_TRUTHY = {"1", "true", "yes", "on", "enable", "enabled"}


def immutable_models_required(value: Optional[bool] = None) -> bool:
    """Resolve the strict provenance policy without importing application config."""
    if value is not None:
        return bool(value)
    return os.environ.get("ENGRAPHIS_REQUIRE_IMMUTABLE_MODELS", "").strip().lower() in _TRUTHY


def is_local_model_source(model: object) -> bool:
    """Whether ``model`` denotes a local-only selector or filesystem location.

    Existing directories are local even when written without ``./``.  Syntactic
    absolute/relative paths remain local before they exist so strict mode reports
    the eventual filesystem loader error instead of misclassifying them as Hub ids.
    """
    source = str(model or "").strip()
    if not source:
        return False
    if source.startswith("local:"):
        return True
    expanded = os.path.expanduser(source)
    if expanded.startswith(("./", "../", ".\\", "..\\")):
        return True
    windows_path = PureWindowsPath(expanded)
    if (
        os.path.isabs(expanded)
        or windows_path.is_absolute()
        # C:models\\foo is drive-relative on Windows, not a Hub namespace; it
        # remains a local source even before the directory exists.
        or bool(windows_path.drive)
    ):
        return True
    try:
        return Path(expanded).is_dir()
    except OSError:
        return False


def validate_model_source(
    model: object,
    revision: Optional[object],
    *,
    require_immutable_models: Optional[bool] = None,
    loader: str = "Hugging Face model",
) -> None:
    """Reject mutable remote revisions before an optional loader can resolve them.

    The default deliberately preserves historical tag/branch behavior.  Strict
    mode affects only remote Hub identifiers; ``local:`` selectors and filesystem
    paths/directories do not need a Hub commit because their provenance is owned by
    the local deployment.
    """
    if not immutable_models_required(require_immutable_models):
        return
    source = str(model or "").strip()
    if not source or is_local_model_source(source):
        return
    pinned = str(revision or "").strip()
    if _IMMUTABLE_REVISION.fullmatch(pinned) is None:
        raise ValueError(
            "%s requires a lowercase 40-character commit revision when "
            "ENGRAPHIS_REQUIRE_IMMUTABLE_MODELS=1" % loader
        )
