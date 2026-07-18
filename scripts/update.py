#!/usr/bin/env python3
"""Update Engraphis to the latest release — one command, any install method.

    engraphis-update                # update to latest
    engraphis-update --check        # only report if an update is available
    engraphis-update v0.1.2         # pin a specific version

Detects how you installed Engraphis and upgrades the same way:

    pip from PyPI       → `pip install --upgrade engraphis`
    pip from Git        → `pip install --upgrade git+<remote>`
    pip -e . from clone → latest release tag + `pip install -e .`
    pipx                → `pipx upgrade engraphis`
    Docker              → rebuild from the updated host checkout
"""
from __future__ import annotations


import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_URL = "https://github.com/Coding-Dev-Tools/engraphis.git"
LATEST_TAG = ""
# Stable SemVer only. Bounded components prevent an untrusted remote ref containing
# millions of digits from turning int() conversion into a local denial of service.
_SEMVER = re.compile(
    r"^v?((?:0|[1-9]\d{0,8}))\.((?:0|[1-9]\d{0,8}))\.((?:0|[1-9]\d{0,8}))$"
)


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def _select_latest_tag(tags) -> str:
    """Return the highest stable ``vMAJOR.MINOR.PATCH`` tag, ignoring other refs."""
    parsed = []
    for raw in tags:
        tag = str(raw).strip()
        match = _SEMVER.fullmatch(tag)
        if match:
            version = tuple(int(part) for part in match.groups())
            parsed.append((version, "v" + ".".join(str(part) for part in version)))
    return max(parsed)[1] if parsed else ""


def _remote_latest_tag(git: str, repo_url: str = REPO_URL) -> str:
    result = subprocess.run(
        [git, "ls-remote", "--tags", "--refs", repo_url, "v*"],
        capture_output=True, text=True,
    )
    if result.returncode:
        return ""
    return _select_latest_tag(
        line.rsplit("refs/tags/", 1)[-1]
        for line in result.stdout.splitlines() if "refs/tags/" in line
    )


def _installed_git_url() -> str:
    """Return the PEP 610 Git origin for a non-editable VCS install."""
    try:
        raw = importlib.metadata.distribution("engraphis").read_text("direct_url.json")
        direct = json.loads(raw) if raw else {}
    except (importlib.metadata.PackageNotFoundError, OSError, ValueError, TypeError):
        return ""
    vcs = direct.get("vcs_info")
    url = direct.get("url")
    if not isinstance(vcs, dict) or vcs.get("vcs") != "git" or not isinstance(url, str):
        return ""
    return url.strip()


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
            # PEP 610 records VCS provenance in direct_url.json. ``pip show`` does not
            # expose it, so looking for ``git+`` in that output misclassified every
            # non-editable Git install as PyPI.
            if _installed_git_url():
                return "git"
            return "pypi"
    except Exception:
        pass

    return "unknown"


def _git_update(check_only: bool = False) -> None:
    """Update an editable install to a validated stable tag and reinstall it."""
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

    # Fetch and compare. Fail closed on a network/ref error: selecting the highest LOCAL
    # tag would let a stray or malicious tag masquerade as the latest upstream release.
    fetched = subprocess.run(
        [git, "-C", str(project_dir), "fetch", "--tags", "origin"],
        capture_output=True, text=True,
    )
    if fetched.returncode:
        print("Could not fetch release tags from origin; no update was applied.",
              file=sys.stderr)
        sys.exit(1)
    local = subprocess.run([git, "-C", str(project_dir), "rev-parse", "HEAD"],
                            capture_output=True, text=True).stdout.strip()
    branch_result = subprocess.run(
        [git, "-C", str(project_dir), "symbolic-ref", "--quiet", "--short", "HEAD"],
        capture_output=True, text=True,
    )
    original_ref = branch_result.stdout.strip() if branch_result.returncode == 0 else local
    tag = LATEST_TAG
    if not tag:
        tags = subprocess.run(
            [git, "-C", str(project_dir), "ls-remote", "--tags", "--refs", "origin", "v*"],
            capture_output=True, text=True,
        )
        if tags.returncode:
            print("Could not list release tags from origin; no update was applied.",
                  file=sys.stderr)
            sys.exit(1)
        tag = _select_latest_tag(
            line.rsplit("refs/tags/", 1)[-1]
            for line in tags.stdout.splitlines() if "refs/tags/" in line
        )
    if not tag:
        print("Could not determine the latest stable release tag.", file=sys.stderr)
        sys.exit(1)
    # ``rev-list`` peels annotated tags; comparing HEAD to the tag object itself would
    # report a false update forever.
    remote = subprocess.run(
        [git, "-C", str(project_dir), "rev-list", "-n", "1", tag],
        capture_output=True, text=True,
    )
    remote_sha = remote.stdout.strip() if remote.returncode == 0 else ""

    if not remote_sha:
        print(f"Could not resolve release tag {tag} after fetching origin.", file=sys.stderr)
        sys.exit(1)
    if local == remote_sha:
        print(f"Engraphis is up to date ({tag}).")
        if check_only:
            return
        print("Nothing to update.")
        return

    print(f"Update available: {local[:8]} -> {remote_sha[:8]} ({tag})")
    if check_only:
        return

    dirty = subprocess.run(
        [git, "-C", str(project_dir), "status", "--porcelain"],
        capture_output=True, text=True,
    )
    if dirty.stdout.strip():
        print("Refusing to update a working tree with uncommitted changes.", file=sys.stderr)
        sys.exit(1)
    print(f"Checking out release {tag}...")
    subprocess.run([git, "-C", str(project_dir), "checkout", f"tags/{tag}"], check=True)
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-e", str(project_dir)],
            check=True,
        )
    except subprocess.CalledProcessError:
        # A failed reinstall must not strand a previously working editable checkout at
        # a half-applied detached release. Restore its original branch (or exact commit
        # when it started detached) and best-effort reinstall before propagating failure.
        subprocess.run([git, "-C", str(project_dir), "checkout", original_ref], check=False)
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-e", str(project_dir)],
            check=False,
        )
        raise
    print(f"Updated to {tag}.")


def _pip_update(method: str, check_only: bool = False) -> None:
    """Update a pip install (PyPI or git)."""
    if method == "git":
        git = shutil.which("git")
        remote = _installed_git_url()
        if not remote:
            print("Could not read the recorded Git install URL; refusing to switch sources.",
                  file=sys.stderr)
            sys.exit(1)
        tag = LATEST_TAG or (_remote_latest_tag(git, remote) if git else "")
        if not tag:
            print("Could not determine the latest stable release tag.", file=sys.stderr)
            sys.exit(1)
        if check_only:
            print(f"Latest stable Git release: {tag}")
            return
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade",
             f"git+{remote}@{tag}#egg=engraphis"],
            check=True)
        return
    version = LATEST_TAG[1:] if LATEST_TAG else ""
    target = "engraphis[server]" + ("==" + version if version else "")
    if check_only:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--dry-run", "--upgrade", target],
            check=False,
        )
        return
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--upgrade", target],
        check=True)


def _pipx_update(check_only: bool = False) -> None:
    """Update a pipx install."""
    if check_only:
        if LATEST_TAG:
            target = "engraphis[server]==" + LATEST_TAG[1:]
            subprocess.run(
                ["pipx", "runpip", "engraphis", "install", "--dry-run", "--upgrade", target],
                check=False,
            )
        else:
            print("pipx detected - run `pipx upgrade engraphis` to check for updates.")
        return
    if LATEST_TAG:
        subprocess.run(
            ["pipx", "install", "--force", "engraphis[server]==" + LATEST_TAG[1:]],
            check=True,
        )
        return
    subprocess.run(["pipx", "upgrade", "engraphis"], check=True)


def _docker_update(check_only: bool = False) -> None:
    """Explain the supported update path for the source-built Compose image."""
    message = (
        "This project does not publish a managed container image. Update the host "
        "checkout, then run `docker compose build --pull && docker compose up -d`."
    )
    print(message)
    if not check_only:
        raise SystemExit(1)


def main(argv=None) -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Update Engraphis to the latest release.")
    ap.add_argument("version", nargs="?", default="",
                    help="Pin a specific stable version (e.g. v1.0.0).")
    ap.add_argument("--check", action="store_true",
                    help="Only report if an update is available, don't apply it.")
    args = ap.parse_args(argv)

    global LATEST_TAG
    LATEST_TAG = ""
    if args.version:
        LATEST_TAG = _select_latest_tag([args.version])
        if not LATEST_TAG:
            ap.error("version must be a stable MAJOR.MINOR.PATCH tag (for example v1.0.0)")

    method = _detect_install()
    print(f"Install method: {method}")

    try:
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
    except subprocess.CalledProcessError:
        ap.exit(
            1,
            "Error: update failed; the previous installation was restored when possible.\n",
        )


if __name__ == "__main__":
    main()
