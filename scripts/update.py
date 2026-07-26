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
import signal
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


# Every step below runs with an explicit, differentiated budget. An unbounded call against
# a stalled package index or an unreachable git remote is an indefinite hang, and a
# *captured* one is a silent hang with nothing on screen to explain it. Sizes follow the
# work each command actually does: a refs query is one round trip, a fetch may transfer a
# whole object delta, and an install downloads and may build wheels. A budget is only real
# if nothing can outlive it — see ``_run`` and ``_run_captured`` for how that is enforced.
_GIT_LOCAL_TIMEOUT_S = 30        # plumbing on an existing clone (see scripts/graph_cli.py)
_GIT_CHECKOUT_TIMEOUT_S = 120    # local, but runs checkout filters and hooks
_GIT_LS_REMOTE_TIMEOUT_S = 60    # one network round trip for refs; no object transfer
_GIT_FETCH_TIMEOUT_S = 600       # may transfer every object a long-stale clone is missing
_PIP_METADATA_TIMEOUT_S = 60     # `pip show` is local, but a cold pip import is not fast
_PIP_RESOLVE_TIMEOUT_S = 300     # `--dry-run` still queries and resolves against the index
_PIP_INSTALL_TIMEOUT_S = 1800    # download plus build; an sdist with C extensions is slow
_PIPX_TIMEOUT_S = 1800           # a pip install plus venv creation
_TREE_KILL_TIMEOUT_S = 10        # bounding the kill itself; `taskkill` is local and fast
_DRAIN_AFTER_KILL_S = 5          # reading a pipe whose writers were just destroyed

# ``os.killpg`` must target *our* tree, never the shell that launched the updater, so the
# POSIX child gets its own session. Windows has no equivalent at spawn time — the tree is
# walked by ``taskkill /T`` instead — and the keyword's Windows meaning changed in 3.13,
# so it is not passed there at all.
_OWN_PROCESS_GROUP = {} if os.name == "nt" else {"start_new_session": True}


class UpdateTimeout(RuntimeError):
    """A step exceeded its bounded budget.

    Carries ready-to-print, actionable copy so a stalled remote never degrades into a
    silent hang, and so the editable-install rollback below can treat a timeout exactly
    like a failed reinstall instead of stranding a half-applied checkout.
    """


def _timed_out(what: str, timeout: int) -> UpdateTimeout:
    return UpdateTimeout(
        "%s timed out after %ds. Check your network connection, proxy settings, and "
        "package index, then run `engraphis-update` again." % (what, timeout)
    )


def _git_env() -> dict:
    """Environment for every git call: never stop to ask a human for credentials.

    An expired token, a revoked SSH key or a corporate proxy that wants authentication
    otherwise drops the updater into git's terminal prompt — or, on Windows, the Git
    Credential Manager dialog — and it blocks forever behind a question nobody is there
    to answer. That is a hang with no network fault to diagnose, so the budgets above look
    like they simply do not work. Fail the call instead; the caller already prints what to
    do about it.
    """
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    return env


def _kill_process_tree(process: subprocess.Popen) -> None:
    """Kill *process* and every descendant it spawned. Best effort; already-dead is fine.

    Killing only the direct child is what makes a "bounded" capture unbounded: git forks
    ``git-remote-https`` (and credential helpers), those grandchildren inherit the pipe's
    write handle, and a read of that pipe cannot complete until the last handle closes.
    """
    if os.name == "nt":
        taskkill = shutil.which("taskkill")
        if taskkill:
            try:
                subprocess.run(
                    [taskkill, "/F", "/T", "/PID", str(process.pid)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, timeout=_TREE_KILL_TIMEOUT_S,
                )
            except (OSError, subprocess.SubprocessError):
                pass
    else:
        try:
            os.killpg(os.getpgid(process.pid), getattr(signal, "SIGKILL", signal.SIGTERM))
        except (OSError, AttributeError):
            pass
    try:
        process.kill()
    except OSError:
        pass


def _run(cmd: list[str], what: str, timeout: int, check: bool = False,
         capture: bool = False, env: dict | None = None) -> subprocess.CompletedProcess:
    """Run *cmd* under an explicit budget; a stall raises instead of hanging forever.

    ``capture`` is opt-in and deliberately defaults to *off*. ``subprocess.run`` honours
    ``timeout`` only when it has no pipes left to drain: once the budget expires CPython
    kills the direct child and then calls ``communicate()`` with **no** timeout, so the
    reader threads keep waiting until every inherited write handle on those pipes closes —
    grandchildren included. Use ``capture=True`` only for local commands that fork nothing
    (``pip show``, git plumbing against an existing clone). Anything that reaches the
    network must go through :func:`_run_captured`, which kills the whole tree first.

    ``stdin`` is closed for every step: a subprocess that stops to read from a terminal is
    the same indefinite hang as a stalled socket, and none of these commands has anything
    to read.
    """
    try:
        return subprocess.run(cmd, capture_output=capture, text=True, check=check,
                              timeout=timeout, stdin=subprocess.DEVNULL, env=env)
    except subprocess.TimeoutExpired:
        raise _timed_out(what, timeout) from None


def _run_captured(cmd: list[str], what: str, timeout: int,
                  env: dict | None = None) -> subprocess.CompletedProcess:
    """Run *cmd* for its stdout under a budget that is actually enforced.

    For the steps that must be *parsed* rather than merely displayed, so simply not
    capturing is not an option. Only stdout is piped — stderr stays on the terminal, which
    both surfaces git's own explanation of a failure and leaves one fewer inherited write
    handle for a grandchild to hold open. On expiry the entire process tree is destroyed
    *before* the pipe is drained, and even that drain is bounded, so the call returns on
    schedule instead of waiting on ``git-remote-https``.
    """
    process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True, env=env,
        **_OWN_PROCESS_GROUP,
    )
    try:
        stdout, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_tree(process)
        try:
            process.communicate(timeout=_DRAIN_AFTER_KILL_S)
        except subprocess.TimeoutExpired:
            pass
        raise _timed_out(what, timeout) from None
    return subprocess.CompletedProcess(cmd, process.returncode, stdout or "", None)


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
    result = _run_captured(
        [git, "ls-remote", "--tags", "--refs", repo_url, "v*"],
        "Listing release tags from the Git remote", _GIT_LS_REMOTE_TIMEOUT_S,
        env=_git_env(),
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
        result = _run(
            [sys.executable, "-m", "pip", "show", "engraphis"],
            "Reading the installed Engraphis metadata", _PIP_METADATA_TIMEOUT_S,
            capture=True)
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
    except UpdateTimeout:
        # A stalled `pip show` must report why, not masquerade as "unknown install
        # method" and send the user off to guess at a reinstall command.
        raise
    except Exception:
        pass

    return "unknown"


def _git_update(check_only: bool = False) -> None:
    """Update an editable install to a validated stable tag and reinstall it."""
    try:
        result = _run(
            [sys.executable, "-m", "pip", "show", "engraphis"],
            "Reading the installed Engraphis metadata", _PIP_METADATA_TIMEOUT_S,
            check=True, capture=True)
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
    # Nothing here parses the fetch's output, and capturing it would forfeit the budget
    # below (see ``_run``), so let git report its own progress straight to the terminal.
    print("Fetching release tags from origin...")
    fetched = _run(
        [git, "-C", str(project_dir), "fetch", "--tags", "origin"],
        "Fetching release tags from origin", _GIT_FETCH_TIMEOUT_S,
        env=_git_env(),
    )
    if fetched.returncode:
        print("Could not fetch release tags from origin; no update was applied.",
              file=sys.stderr)
        sys.exit(1)
    local = _run([git, "-C", str(project_dir), "rev-parse", "HEAD"],
                 "Reading the current revision", _GIT_LOCAL_TIMEOUT_S,
                 capture=True, env=_git_env()).stdout.strip()
    branch_result = _run(
        [git, "-C", str(project_dir), "symbolic-ref", "--quiet", "--short", "HEAD"],
        "Reading the current branch", _GIT_LOCAL_TIMEOUT_S,
        capture=True, env=_git_env(),
    )
    original_ref = branch_result.stdout.strip() if branch_result.returncode == 0 else local
    tag = LATEST_TAG
    if not tag:
        tags = _run_captured(
            [git, "-C", str(project_dir), "ls-remote", "--tags", "--refs", "origin", "v*"],
            "Listing release tags from origin", _GIT_LS_REMOTE_TIMEOUT_S,
            env=_git_env(),
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
    remote = _run(
        [git, "-C", str(project_dir), "rev-list", "-n", "1", tag],
        "Resolving the release tag", _GIT_LOCAL_TIMEOUT_S,
        capture=True, env=_git_env(),
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

    dirty = _run(
        [git, "-C", str(project_dir), "status", "--porcelain"],
        "Checking the working tree", _GIT_LOCAL_TIMEOUT_S,
        capture=True, env=_git_env(),
    )
    if dirty.stdout.strip():
        print("Refusing to update a working tree with uncommitted changes.", file=sys.stderr)
        sys.exit(1)
    print(f"Checking out release {tag}...")
    _run([git, "-C", str(project_dir), "checkout", f"tags/{tag}"],
         "Checking out the release tag", _GIT_CHECKOUT_TIMEOUT_S,
         check=True, capture=False, env=_git_env())
    print(f"Reinstalling from {project_dir}...")
    try:
        _run(
            [sys.executable, "-m", "pip", "install", "-e", str(project_dir)],
            "Reinstalling the editable checkout", _PIP_INSTALL_TIMEOUT_S,
            check=True, capture=False,
        )
    except (subprocess.CalledProcessError, UpdateTimeout):
        # A failed *or stalled* reinstall must not strand a previously working editable
        # checkout at a half-applied detached release. The tree is already on the new tag
        # by this point, so a bare hang here is what wedges the install; catching the
        # timeout is what lets this rollback run at all. Restore the original branch (or
        # exact commit when it started detached) and best-effort reinstall, then
        # propagate the original failure.
        print("Restoring the previous checkout...", file=sys.stderr)
        try:
            _run([git, "-C", str(project_dir), "checkout", original_ref],
                 "Restoring the previous checkout", _GIT_CHECKOUT_TIMEOUT_S,
                 capture=False, env=_git_env())
            _run(
                [sys.executable, "-m", "pip", "install", "-e", str(project_dir)],
                "Reinstalling the previous checkout", _PIP_INSTALL_TIMEOUT_S,
                capture=False,
            )
        except UpdateTimeout:
            # Rollback itself stalled: name the two commands that finish it by hand
            # rather than exiting on a tree the user does not know has moved.
            print(
                "Rollback did not finish. Run `git -C %s checkout %s` and "
                "`%s -m pip install -e %s` to restore the previous installation."
                % (project_dir, original_ref, sys.executable, project_dir),
                file=sys.stderr,
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
        _run(
            [sys.executable, "-m", "pip", "install", "--upgrade",
             f"git+{remote}@{tag}#egg=engraphis"],
            "Installing the update from Git", _PIP_INSTALL_TIMEOUT_S,
            check=True, capture=False)
        return
    version = LATEST_TAG[1:] if LATEST_TAG else ""
    target = "engraphis[server]" + ("==" + version if version else "")
    if check_only:
        _run(
            [sys.executable, "-m", "pip", "install", "--dry-run", "--upgrade", target],
            "Checking the package index for a newer release", _PIP_RESOLVE_TIMEOUT_S,
            capture=False,
        )
        return
    _run(
        [sys.executable, "-m", "pip", "install", "--upgrade", target],
        "Installing the update from the package index", _PIP_INSTALL_TIMEOUT_S,
        check=True, capture=False)


def _pipx_update(check_only: bool = False) -> None:
    """Update a pipx install."""
    if check_only:
        if LATEST_TAG:
            target = "engraphis[server]==" + LATEST_TAG[1:]
            _run(
                ["pipx", "runpip", "engraphis", "install", "--dry-run", "--upgrade", target],
                "Checking the package index for a newer release", _PIP_RESOLVE_TIMEOUT_S,
                capture=False,
            )
        else:
            print("pipx detected - run `pipx upgrade engraphis` to check for updates.")
        return
    if LATEST_TAG:
        _run(
            ["pipx", "install", "--force", "engraphis[server]==" + LATEST_TAG[1:]],
            "Installing the update with pipx", _PIPX_TIMEOUT_S,
            check=True, capture=False,
        )
        return
    _run(["pipx", "upgrade", "engraphis"], "Upgrading with pipx", _PIPX_TIMEOUT_S,
         check=True, capture=False)


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

    try:
        # Inside the guard, not before it. ``_detect_install`` re-raises ``UpdateTimeout``
        # on purpose so a stalled `pip show` says which step hung and what to do about it;
        # raising it outside this ``try`` threw that crafted message away and printed a
        # traceback instead — the exact failure mode the exception exists to prevent.
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
    except subprocess.CalledProcessError:
        ap.exit(
            1,
            "Error: update failed; the previous installation was restored when possible.\n",
        )
    except UpdateTimeout as exc:
        # Say which step stalled and what to do about it. Silence here is the bug: every
        # network step used to be unbounded, so a stalled index simply never returned.
        ap.exit(1, "Error: %s\n" % exc)


if __name__ == "__main__":
    main()
