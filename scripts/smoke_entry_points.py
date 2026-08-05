"""Exercise every installed Engraphis console script without starting a service.

Release artifact smoke tests need to verify the actual generated console wrappers, not
just import the functions named in ``pyproject.toml``.  Every supported wrapper accepts
``--help`` before it opens a database, binds a port, reads a credential, or contacts the
network, so this module invokes that one deterministic code path for each entry point.

It deliberately does *not* attempt a normal invocation: several commands are servers,
and others intentionally mutate local state or contact the update/control plane.  When a
new console script cannot offer a side-effect-free help path, add a narrowly justified
exception here and a corresponding test rather than silently omitting it from release
coverage.

Run after installing a wheel or sdist into a fresh environment::

    python -m scripts.smoke_entry_points
"""
from __future__ import annotations

import argparse
import importlib.metadata
import math
import os
import site
import subprocess
import sys
import sysconfig
from pathlib import Path
from typing import Callable, Iterable, Optional


# Keep this mapping explicit.  It makes a packaging-surface change reviewable and lets the
# helper reject a stale wheel that accidentally drops (or unexpectedly adds) a public CLI.
# ``tests/test_artifact_smoke.py`` checks it against pyproject.toml's [project.scripts].
EXPECTED_ENTRY_POINTS = {
    "engraphis": "scripts.entry:main",
    "engraphis-connect": "scripts.connect:main",
    "engraphis-server": "scripts.start_server:main",
    "engraphis-cli": "scripts.cli:main",
    "engraphis-mcp": "engraphis.mcp_cli:main",
    "engraphis-mcp-classic": "engraphis.mcp_classic_cli:main",
    "engraphis-mcp-http": "engraphis.mcp_http_cli:main",
    "engraphis-inspector": "scripts.inspector:main",
    "engraphis-dashboard": "scripts.start_dashboard:main",
    "engraphis-consolidate": "scripts.consolidate:main",
    "engraphis-graph": "scripts.graph_cli:main",
    "engraphis-graph-server": "scripts.graph_server:main",
    "engraphis-init": "scripts.init:main",
    "engraphis-update": "scripts.update:main",
}

DEFAULT_TIMEOUT_SECONDS = 20.0
_OUTPUT_LIMIT = 4_000


def installed_entry_points(distribution: str = "engraphis") -> dict[str, str]:
    """Return Engraphis console-script metadata from the installed distribution."""
    try:
        points = importlib.metadata.distribution(distribution).entry_points
    except importlib.metadata.PackageNotFoundError as exc:
        raise RuntimeError("Engraphis is not installed in this environment") from exc
    try:
        console_scripts = points.select(group="console_scripts")
    except AttributeError:  # pragma: no cover - Python 3.9 compatibility adapter
        console_scripts = [point for point in points if point.group == "console_scripts"]
    return {
        point.name: point.value
        for point in console_scripts
        if point.name in EXPECTED_ENTRY_POINTS or point.name.startswith("engraphis-")
    }


def console_script_path(name: str, *, scripts_dir: Optional[Path] = None) -> Path:
    """Return this interpreter environment's generated wrapper, never a PATH lookalike."""
    suffix = ".exe" if os.name == "nt" else ""
    if scripts_dir is not None:
        return scripts_dir / (name + suffix)
    base_scripts = sysconfig.get_path("scripts")
    if not base_scripts:  # pragma: no cover - every supported CPython exposes it
        raise RuntimeError("the active Python installation has no scripts directory")
    directories = [Path(base_scripts)]
    # A `pip install --user` console wrapper lives under USER_BASE, while sysconfig's
    # default scheme still points at the base interpreter's Scripts/bin directory.
    # Artifact venvs take the first path; this fallback keeps the helper accurate for
    # the supported user-install path without ever resolving a lookalike through PATH.
    if site.ENABLE_USER_SITE and site.USER_BASE:
        user_scripts = Path(site.USER_BASE) / ("Scripts" if os.name == "nt" else "bin")
        if user_scripts not in directories:
            directories.append(user_scripts)
    for directory in directories:
        candidate = directory / (name + suffix)
        if candidate.is_file():
            return candidate
    return directories[0] / (name + suffix)


def _format_output(result: subprocess.CompletedProcess) -> str:
    chunks = []
    for label, value in (("stdout", result.stdout), ("stderr", result.stderr)):
        if value:
            text = str(value).strip()
            if len(text) > _OUTPUT_LIMIT:
                text = text[:_OUTPUT_LIMIT] + "\n[truncated]"
            chunks.append("%s:\n%s" % (label, text))
    return "\n".join(chunks)


def smoke_entry_points(
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    distribution: str = "engraphis",
    scripts_dir: Optional[Path] = None,
    entries: Optional[dict[str, str]] = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> list[str]:
    """Run ``--help`` for every installed public wrapper and return its names.

    ``entries`` and ``runner`` make the behavior unit-testable without relying on a
    developer machine's global console-script directory.  The default still reads the
    wheel/sdist metadata and invokes the generated wrappers in the active virtualenv.
    """
    try:
        timeout = float(timeout)
    except (TypeError, ValueError) as exc:
        raise ValueError("timeout must be a positive finite number") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a positive finite number")
    actual = dict(installed_entry_points(distribution) if entries is None else entries)
    missing = sorted(set(EXPECTED_ENTRY_POINTS) - set(actual))
    unexpected = sorted(set(actual) - set(EXPECTED_ENTRY_POINTS))
    incorrect = sorted(
        name for name, target in EXPECTED_ENTRY_POINTS.items()
        if actual.get(name) != target
    )
    if missing or unexpected or incorrect:
        details = []
        if missing:
            details.append("missing=" + ", ".join(missing))
        if unexpected:
            details.append("unexpected=" + ", ".join(unexpected))
        if incorrect:
            details.append("target mismatch=" + ", ".join(incorrect))
        raise RuntimeError("installed console-script metadata differs: " + "; ".join(details))

    passed = []
    for name in sorted(EXPECTED_ENTRY_POINTS):
        executable = console_script_path(name, scripts_dir=scripts_dir)
        if not executable.is_file():
            raise RuntimeError("generated console script is missing: %s" % executable)
        try:
            result = runner(
                [str(executable), "--help"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("%s --help timed out after %.1fs" % (name, timeout)) from exc
        output = _format_output(result)
        if result.returncode != 0:
            raise RuntimeError(
                "%s --help exited %s%s" % (
                    name, result.returncode, ("\n" + output) if output else "",
                )
            )
        if "usage:" not in output.casefold():
            raise RuntimeError(
                "%s --help produced no usage text%s" % (
                    name, ("\n" + output) if output else "",
                )
            )
        print("[ok] %s --help" % name)
        passed.append(name)
    return passed


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Smoke every installed Engraphis console script with --help."
    )
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS,
        help="per-command timeout in seconds (default: %(default)s)",
    )
    parser.add_argument(
        "--distribution", default="engraphis",
        help="installed distribution metadata to inspect (default: %(default)s)",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        passed = smoke_entry_points(timeout=args.timeout, distribution=args.distribution)
    except (RuntimeError, ValueError) as exc:
        print("artifact console smoke failed: %s" % exc, file=sys.stderr)
        return 1
    print("Smoke passed: %d console entry points." % len(passed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
