#!/usr/bin/env python3
"""Update Engraphis to the latest release — one command, any install method.

    engraphis-update                # update to latest
    engraphis-update --check        # only report if an update is available
    engraphis-update v0.1.2         # pin a specific version

Detects how you installed Engraphis and upgrades the same way:

    pip from PyPI       → `pip install --upgrade engraphis`
    pip from Git        → `pip install --upgrade git+<remote>`
    pip -e . from clone → `git pull && pip install -e .`
    pipx                → `pipx upgrade engraphis`
    Docker              → `docker pull ghcr.io/engraphis/engraphis:latest`

Runs offline checks first — no API key, no signup, no telemetry.
"""
from __future__ import annotations


import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_URL = "https://github.com/Coding-Dev-Tools/engraphis.git"
LATEST_TAG = "v0.1.0"


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def _detect_install() -> str:
    """Return the install method: 'pypi', 'git', 'editable', 'pipx', 'docker', 'unknown'."""
    # Docker detection: ENGRAPHIS_DOCKER is set in our Dockerfile.
    if os.environ.get("ENGRAPHIS_DOCKER") or Path("/.dockerenv").exists():
        return "docker"

    # pipx creates isolated venvs with a predictable parent.
    try:
        from engraphis import __file__ as engraphis_path
        engraphis_dir = Path(engraphis_path).resolve().parent
        if "pipx" in str(engraphis_dir):
            return "pipx"
    except ImportError:
        pass

    # Editable install: there's a .git directory at the project root and pip
    # installed it in develop mode. pip show engraphis will list an "Editable
    # project location" line.
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "show", "engraphis"],
            capture_output=True, text=True)
        if result.returncode == 0:
            info = result.stdout
            if "Editable project location:" in info:
                location = [line.split(":", 1)[1].strip() for line in info.split("\n") if line.startswith("Editable project location:")]
                if location and (Path(location[0]) / ".git").exists():
                    return "editable"
            if "git+" in info:
                return "git"
            return "pypi"
    except Exception:
        pass

    return "unknown"


def _git_update(check_only: bool = False) -> None:
    """Update an editable install (git pull + reinstall)."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "show", "engraphis"],
            capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError:
        print("Engraphis is not installed.", file=sys.stderr)
        sys.exit(1)

    location_line = next(
        (line for line in result.stdout.split("\n") if line.startswith("Editable project location:")),
        None)
    if not location_line:
        print("Could not determine the editable install location.", file=sys.stderr)
        sys.exit(1)

    project_dir = Path(location_line.split(":", 1)[1].strip())
    if not (project_dir / ".git").exists():
        print(f"Not a git repository: {project_dir}", file=sys.stderr)
        sys.exit(1)

    git = shutil.which("git")
    if not git:
        print("Git is not installed or not on PATH.", file=sys.stderr)
        sys.exit(1)

    # Fetch and compare.
    subprocess.run([git, "-C", str(project_dir), "fetch", "--tags", "origin"], check=False)
    local = subprocess.run([git, "-C", str(project_dir), "rev-parse", "HEAD"],
                           capture_output=True, text=True).stdout.strip()
    remote = subprocess.run([git, "-C", str(project_dir), "rev-parse", f"refs/tags/{LATEST_TAG}"],
                            capture_output=True, text=True)
    remote_sha = remote.stdout.strip() if remote.returncode == 0 else ""

    if not remote_sha or local == remote_sha:
        print(f"Engraphis is up to date ({LATEST_TAG}).")
        if check_only:
            return
        print("Nothing to update.")
        return

    print(f"Update available: {local[:8]} → {remote_sha[:8]} ({LATEST_TAG})")
    if check_only:
        return

    print("Pulling latest...")
    subprocess.run([git, "-C", str(project_dir), "checkout", f"tags/{LATEST_TAG}"], check=True)
    subprocess.run([sys.executable, "-m", "pip", "install", "-e", str(project_dir)], check=True)
    print(f"Updated to {LATEST_TAG}.")


def _pip_update(method: str, check_only: bool = False) -> None:
    """Update a pip install (PyPI or git)."""
    if check_only:
        subprocess.run([sys.executable, "-m", "pip", "install", "--dry-run", "--upgrade", "engraphis"],
                       check=False)
        return
    if method == "git":
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade",
             f"git+{REPO_URL}@v0.1.0#egg=engraphis"],
            check=True)
        return
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--upgrade", "engraphis[server]"],
        check=True)


def _pipx_update(check_only: bool = False) -> None:
    """Update a pipx install."""
    if check_only:
        print("pipx detected — run `pipx upgrade engraphis` to check for updates.")
        return
    subprocess.run(["pipx", "upgrade", "engraphis"], check=True)


def _docker_update(check_only: bool = False) -> None:
    """Update a Docker install."""
    image = "ghcr.io/engraphis/engraphis:latest"
    if check_only:
        subprocess.run(["docker", "pull", "--quiet", image], check=False)
        print(f"Checked {image} — run `docker compose pull && docker compose up -d` to apply.")
        return
    subprocess.run(["docker", "pull", image], check=True)
    print(f"Pulled {image}. Restart your container to apply.")


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Update Engraphis to the latest release.")
    ap.add_argument("version", nargs="?", default="", help="Pin a specific version (e.g. v0.2.0).")
    ap.add_argument("--check", action="store_true",
                    help="Only report if an update is available, don't apply it.")
    args = ap.parse_args()

    global LATEST_TAG
    if args.version:
        LATEST_TAG = args.version

    method = _detect_install()
    print(f"Install method: {method}")

    if method == "editable":
        _git_update(check_only=args.check)
    elif method == "pipx":
        _pipx_update(check_only=args.check)
    elif method == "docker":
        _docker_update(check_only=args.check)
    elif method in ("pypi", "git"):
        _pip_update(method, check_only=args.check)
    else:
        print("Could not determine how Engraphis was installed.", file=sys.stderr)
        print("Try: pip install --upgrade engraphis[server]", file=sys.stderr)
        print(" or: pipx upgrade engraphis", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
