"""The updater must never embed a stale release or select prerelease-like refs."""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import update


class _FakeProcess:
    """A ``Popen`` stand-in: the updater reads ``pid``/``returncode`` and drains once.

    Every step now spawns through ``subprocess.Popen`` — that is the only way to hold on to
    a handle for the *process tree* long enough to destroy it at the budget — so a fake
    that intercepted ``subprocess.run`` would no longer see the call at all, and the real
    ``pip``/``git`` would run instead.
    """

    def __init__(self, returncode=0, stdout=""):
        self.pid = 4242
        self.returncode = returncode
        self._stdout = stdout
        self.drains = []

    def communicate(self, timeout=None):
        self.drains.append(timeout)
        return self._stdout, None


def _spawner(handler, calls=None):
    """Build a ``Popen`` fake from a ``cmd -> _FakeProcess`` *handler*."""

    def fake_popen(command, **kwargs):
        command = list(command)
        process = handler(command)
        if calls is not None:
            calls.append((command, kwargs, process))
        return process

    return fake_popen


def test_select_latest_stable_semver_tag():
    assert update._select_latest_tag([
        "v0.9.7", "v1.0.0", "v0.10.0", "v1.0.0rc1", "release/v9.0.0",
        "v01.0.0", "v1.0.0+local",
    ]) == "v1.0.0"


def test_updater_has_no_hard_coded_historical_git_target():
    source = Path(update.__file__).read_text(encoding="utf-8")
    assert "@v0.1.0" not in source
    assert "rev-list" in source


@pytest.mark.parametrize("value", [
    "main", "v1.0", "v1.0.0rc1", "v01.0.0", "--upload-pack=owned", "../v1.0.0",
])
def test_requested_version_must_be_a_stable_semver(value):
    with pytest.raises(SystemExit) as exc:
        update.main([value])
    assert exc.value.code == 2


def test_pypi_version_pin_is_applied_to_the_install_target(monkeypatch):
    calls = []
    monkeypatch.setattr(update.subprocess, "Popen",
                        _spawner(lambda _cmd: _FakeProcess(), calls))
    monkeypatch.setattr(update, "LATEST_TAG", "v1.2.3")
    update._pip_update("pypi")
    assert [command for command, _kwargs, _proc in calls] == [[
        update.sys.executable, "-m", "pip", "install", "--upgrade",
        "engraphis[server]==1.2.3",
    ]]


def test_detects_noneditable_git_install_from_pep610_metadata(monkeypatch):
    monkeypatch.delenv("ENGRAPHIS_DOCKER", raising=False)
    monkeypatch.setattr(update.Path, "exists", lambda self: False)
    monkeypatch.setattr(
        update.subprocess, "Popen",
        _spawner(lambda _cmd: _FakeProcess(
            stdout="Name: engraphis\nLocation: /site-packages\n")),
    )
    distribution = SimpleNamespace(read_text=lambda name: json.dumps({
        "url": "https://github.com/Coding-Dev-Tools/engraphis.git",
        "vcs_info": {"vcs": "git", "commit_id": "abc"},
    }))
    monkeypatch.setattr(update.importlib.metadata, "distribution", lambda _name: distribution)
    assert update._detect_install() == "git"


def test_noneditable_git_update_preserves_recorded_fork(monkeypatch):
    calls = []
    fork = "https://github.com/example/private-engraphis.git"
    monkeypatch.setattr(update, "_installed_git_url", lambda: fork)
    monkeypatch.setattr(update, "LATEST_TAG", "v1.2.3")
    monkeypatch.setattr(update.shutil, "which", lambda _name: "git")
    monkeypatch.setattr(update.subprocess, "Popen",
                        _spawner(lambda _cmd: _FakeProcess(), calls))

    update._pip_update("git")

    last = calls[-1][0]
    assert last[-1] == f"git+{fork}@v1.2.3#egg=engraphis"
    assert update.REPO_URL not in last[-1]


def test_non_git_pep610_install_is_not_misclassified(monkeypatch):
    monkeypatch.delenv("ENGRAPHIS_DOCKER", raising=False)
    monkeypatch.setattr(update.Path, "exists", lambda self: False)
    monkeypatch.setattr(
        update.subprocess, "Popen",
        _spawner(lambda _cmd: _FakeProcess(
            stdout="Name: engraphis\nLocation: /site-packages\n")),
    )
    distribution = SimpleNamespace(read_text=lambda name: json.dumps({
        "url": "https://example.com/archive",
        "vcs_info": {"vcs": "mercurial", "commit_id": "abc"},
    }))
    monkeypatch.setattr(update.importlib.metadata, "distribution", lambda _name: distribution)
    assert update._detect_install() == "pypi"


def test_failed_editable_reinstall_restores_original_branch(monkeypatch, tmp_path):
    project = tmp_path / "clone"
    (project / ".git").mkdir(parents=True)
    monkeypatch.setattr(update.shutil, "which", lambda _name: "git")
    monkeypatch.setattr(update, "LATEST_TAG", "v1.2.3")
    calls = []
    install_attempts = 0

    def handler(command):
        nonlocal install_attempts
        if command[:4] == [update.sys.executable, "-m", "pip", "show"]:
            return _FakeProcess(stdout=f"Editable project location: {project}\n")
        if "rev-parse" in command:
            return _FakeProcess(stdout="old-sha\n")
        if "symbolic-ref" in command:
            return _FakeProcess(stdout="main\n")
        if "rev-list" in command:
            return _FakeProcess(stdout="new-sha\n")
        if "status" in command:
            return _FakeProcess(stdout="")
        if command[:4] == [update.sys.executable, "-m", "pip", "install"]:
            install_attempts += 1
            if install_attempts == 1:
                return _FakeProcess(returncode=1)
        return _FakeProcess()

    monkeypatch.setattr(update.subprocess, "Popen", _spawner(handler, calls))
    with pytest.raises(update.subprocess.CalledProcessError):
        update._git_update()

    assert ["git", "-C", str(project), "checkout", "main"] in [c for c, _k, _p in calls]
    assert install_attempts == 2


def test_main_reports_update_failure_without_traceback(monkeypatch, capsys):
    monkeypatch.setattr(update, "_detect_install", lambda: "pypi")
    monkeypatch.setattr(
        update,
        "_pip_update",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            update.subprocess.CalledProcessError(1, ["pip", "secret-url"])
        ),
    )
    with pytest.raises(SystemExit) as exc:
        update.main([])
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "update failed" in captured.err.lower()
    assert "traceback" not in captured.err.lower()
    assert "secret-url" not in captured.err


def test_docker_update_does_not_claim_a_nonexistent_registry_image(capsys):
    update._docker_update(check_only=True)
    output = capsys.readouterr().out
    assert "does not publish a managed container image" in output
    assert "ghcr.io" not in output


def test_a_stalled_install_probe_is_reported_instead_of_crashing(monkeypatch, capsys):
    """``_detect_install`` re-raises ``UpdateTimeout`` so the user gets actionable copy.

    The call used to sit *outside* ``main()``'s ``try``, so the crafted message was thrown
    away and a stalled `pip show` printed a traceback — the one outcome the exception was
    written to prevent.
    """

    def _stall():
        raise update.UpdateTimeout(
            "Reading the installed Engraphis metadata timed out after 60s. Check your "
            "network connection, proxy settings, and package index, then run "
            "`engraphis-update` again."
        )

    monkeypatch.setattr(update, "_detect_install", _stall)

    with pytest.raises(SystemExit) as exc:
        update.main([])

    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "timed out" in captured.err
    assert "engraphis-update" in captured.err
    assert "Traceback" not in captured.err


# ── a budget is only real if nothing can outlive it ───────────────────────────
def test_every_git_step_closes_stdin_and_refuses_a_credential_prompt(monkeypatch, tmp_path):
    """An expired token must fail the step, not open a prompt nobody is there to answer.

    ``git`` otherwise reads a username from the terminal (or raises the Git Credential
    Manager dialog on Windows) and blocks past every budget above it, with no network
    fault to diagnose.
    """

    project = tmp_path / "clone"
    (project / ".git").mkdir(parents=True)
    monkeypatch.setattr(update.shutil, "which", lambda _name: "git")
    monkeypatch.setattr(update, "LATEST_TAG", "v1.2.3")
    calls = []

    def handler(command):
        if command[:4] == [sys.executable, "-m", "pip", "show"]:
            return _FakeProcess(stdout=f"Editable project location: {project}\n")
        if "rev-parse" in command:
            return _FakeProcess(stdout="old-sha\n")
        if "symbolic-ref" in command:
            return _FakeProcess(stdout="main\n")
        if "rev-list" in command:
            return _FakeProcess(stdout="new-sha\n")
        return _FakeProcess()

    monkeypatch.setattr(update.subprocess, "Popen", _spawner(handler, calls))

    update._git_update(check_only=True)

    git_calls = [entry for entry in calls if entry[0][0] == "git"]
    assert git_calls, "expected the editable path to shell out to git"
    for command, kwargs, _process in git_calls:
        assert kwargs["stdin"] is subprocess.DEVNULL, command
        assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0", command
        assert kwargs["env"]["GCM_INTERACTIVE"] == "never", command
        # Every step — not only the parsed ones — is spawned somewhere a whole tree can be
        # killed from, or a descendant outlives the budget that was supposed to bound it.
        for keyword, value in update._OWN_PROCESS_GROUP.items():
            assert kwargs.get(keyword) == value, command
    # The fetch is displayed, not parsed: no pipe, and its budget rides on the drain.
    fetches = [(kwargs, process) for cmd, kwargs, process in git_calls if "fetch" in cmd]
    assert fetches and fetches[0][0].get("stdout") is None
    assert fetches[0][1].drains == [update._GIT_FETCH_TIMEOUT_S]


def test_the_remote_tag_query_parses_stdout_through_the_bounded_reader(monkeypatch):
    """It must keep its stdout — so it may not simply stop capturing; it changes *how*."""

    monkeypatch.setattr(
        update.subprocess, "Popen",
        lambda *a, **k: pytest.fail("the refs query must go through the bounded reader"),
    )
    monkeypatch.setattr(
        update.subprocess, "run",
        lambda *a, **k: pytest.fail("nothing in the updater may spawn through subprocess.run"),
    )
    seen = {}

    def fake_captured(cmd, what, timeout, env=None):
        seen.update(cmd=list(cmd), timeout=timeout, env=env)
        return SimpleNamespace(
            returncode=0,
            stdout="a\trefs/tags/v0.9.0\nb\trefs/tags/v1.0.0\nc\trefs/heads/main\n",
        )

    monkeypatch.setattr(update, "_run_captured", fake_captured)

    assert update._remote_latest_tag("git", "https://example.test/e.git") == "v1.0.0"
    assert seen["timeout"] == update._GIT_LS_REMOTE_TIMEOUT_S
    assert seen["env"]["GIT_TERMINAL_PROMPT"] == "0"


def test_the_bounded_reader_pipes_only_stdout_and_never_prompts(monkeypatch):
    """Fewer pipes is fewer handles a grandchild can hold open; stderr goes to the user."""

    captured = {}

    class _Process:
        pid = 4321
        returncode = 0

        def communicate(self, timeout=None):
            captured["drain_timeout"] = timeout
            return "abc\trefs/tags/v2.0.0\n", None

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["kwargs"] = kwargs
        return _Process()

    monkeypatch.setattr(update.subprocess, "Popen", fake_popen)

    result = update._run_captured(["git", "ls-remote"], "Listing", 60, env=update._git_env())

    assert result.returncode == 0
    assert "v2.0.0" in result.stdout
    assert captured["drain_timeout"] == 60
    assert captured["kwargs"]["stdout"] is subprocess.PIPE
    assert captured["kwargs"]["stdin"] is subprocess.DEVNULL
    assert "stderr" not in captured["kwargs"], "stderr must stay on the terminal"
    assert captured["kwargs"]["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert captured["kwargs"]["env"]["GCM_INTERACTIVE"] == "never"


def test_windows_job_handle_stays_open_through_communicate(monkeypatch):
    """Closing KILL_ON_JOB_CLOSE before the drain finishes kills a healthy child."""

    events = []

    class _Process:
        pid = 4321
        returncode = 0

        def communicate(self, timeout=None):
            events.append(("communicate", timeout))
            return "done\n", None

    job = object()
    monkeypatch.setattr(update.subprocess, "Popen", lambda *args, **kwargs: _Process())
    monkeypatch.setattr(
        update, "_start_windows_job",
        lambda process: events.append(("start", process.pid)) or job,
    )
    monkeypatch.setattr(
        update, "_close_windows_job",
        lambda handle: events.append(("close", handle)),
    )

    result = update._run_captured(["git", "fetch"], "Fetching", 7)

    assert result.stdout == "done\n"
    assert events == [("start", 4321), ("communicate", 7), ("close", job)]


# A child that outlives its parent and inherits the same stdout pipe — exactly the shape of
# ``git`` forking ``git-remote-https``. ``subprocess.run(capture_output=True, timeout=N)``
# waits for this grandchild to exit no matter what ``N`` says.
_HOLDS_THE_PIPE = (
    "import subprocess, sys, time; "
    "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']); "
    "time.sleep(120)"
)


def test_the_budget_holds_even_when_a_grandchild_still_owns_the_pipe():
    """The measured defect: a 5s budget returned after 21s, or never. Now it returns."""

    started = time.monotonic()
    with pytest.raises(update.UpdateTimeout) as exc:
        update._run_captured([sys.executable, "-c", _HOLDS_THE_PIPE], "Stalled query", 2)
    elapsed = time.monotonic() - started

    assert "timed out after 2s" in str(exc.value)
    # Generous, because the point is the difference between "bounded" and "120 seconds".
    assert elapsed < 30, "the budget was not enforced: waited %.1fs" % elapsed


def test_a_stalled_uncaptured_step_takes_its_descendants_with_it(tmp_path):
    """The budget has to bound the *tree*, not just the process we happen to hold.

    ``pip install``, ``pipx`` and ``git fetch`` are displayed rather than parsed, so
    ``subprocess.run(timeout=...)`` did return on time for them — and left the descendants
    running. pip's vendored resolver or ``git-remote-https`` then keeps writing to the
    environment and the repository *while the caller has already started a rollback or a
    retry*, which is exactly the guarantee the budget is supposed to buy. Real processes,
    because the whole class of bug is that mocks hid the platform's actual kill semantics.
    """

    sentinel = tmp_path / "descendant-outlived-the-budget"
    grandchild = tmp_path / "grandchild.py"
    grandchild.write_text(
        "import sys, time\n"
        "time.sleep(3)\n"
        "open(sys.argv[1], 'w').write('still here')\n",
        encoding="utf-8",
    )
    child = tmp_path / "child.py"
    child.write_text(
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )

    started = time.monotonic()
    with pytest.raises(update.UpdateTimeout) as exc:
        update._run([sys.executable, str(child), str(grandchild), str(sentinel)],
                    "Installing the update", 2)
    assert "timed out after 2s" in str(exc.value)
    assert time.monotonic() - started < 30, "the call itself was not bounded"

    # Well past the moment the grandchild would have written, had it survived the kill.
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline and not sentinel.exists():
        time.sleep(0.2)
    assert not sentinel.exists(), (
        "a descendant outlived the budget and was still touching the environment while "
        "the updater had already moved on to rollback"
    )


# ── the destructive step belongs inside the rollback boundary ─────────────────
@pytest.fixture
def real_clone(tmp_path, monkeypatch):
    """A real clone, with a real ``origin`` and a real release tag ahead of its HEAD.

    Real git, because the failure modes under test are git's own: what a checkout does
    when ``index.lock`` exists, and what it leaves behind when it is killed holding one.
    """

    git = shutil.which("git")
    if not git:  # pragma: no cover - git is present everywhere this suite runs
        pytest.skip("git is not installed")

    # Neutralise the developer's own git configuration: a global hooksPath, commit
    # signing or a different init.defaultBranch would otherwise decide this test.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Engraphis Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.test")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Engraphis Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.test")

    def run(*args, cwd):
        subprocess.run([git, *args], cwd=str(cwd), check=True, env=dict(os.environ),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    upstream = tmp_path / "upstream"
    upstream.mkdir()
    run("-c", "init.defaultBranch=main", "init", "-q", ".", cwd=upstream)
    (upstream / "f.txt").write_text("one\n", encoding="utf-8")
    run("add", "f.txt", cwd=upstream)
    run("commit", "-qm", "one", cwd=upstream)

    clone = tmp_path / "clone"
    run("clone", "-q", str(upstream), str(clone), cwd=tmp_path)

    (upstream / "f.txt").write_text("two\n", encoding="utf-8")
    run("commit", "-qam", "two", cwd=upstream)
    run("tag", "v9.9.9", cwd=upstream)

    return SimpleNamespace(git=git, clone=clone)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object regression")
def test_windows_job_does_not_suspend_a_real_local_fetch(real_clone):
    """A local fetch spawns ``git-upload-pack`` and must finish inside its budget.

    ``CREATE_SUSPENDED`` is not safe through ``subprocess.Popen`` because CPython closes
    the primary-thread handle before returning. The old Job Object attempt therefore
    stranded this exact command with its only thread suspended.
    """

    started = time.monotonic()
    update._run(
        [real_clone.git, "-C", str(real_clone.clone), "fetch", "--tags", "origin"],
        "Fetching release tags", 15, env=update._git_env(),
    )
    elapsed = time.monotonic() - started

    tags = subprocess.run(
        [real_clone.git, "-C", str(real_clone.clone), "tag", "--list", "v9.9.9"],
        capture_output=True, text=True, check=True, timeout=5, env=update._git_env(),
    )
    assert tags.stdout.strip() == "v9.9.9"
    assert elapsed < 15, "local git fetch was stranded for %.1fs" % elapsed


def _branch_of(git, project):
    return subprocess.run([git, "-C", str(project), "rev-parse", "--abbrev-ref", "HEAD"],
                          capture_output=True, text=True).stdout.strip()


def _pip_router(project, pip_calls):
    """Let every git command run for real; answer pip without installing anything."""

    real_run = update._run

    def routed(cmd, what, timeout, **kwargs):
        cmd = list(cmd)
        if cmd[:3] == [sys.executable, "-m", "pip"]:
            pip_calls.append(cmd)
            stdout = ("Editable project location: %s\n" % project
                      if cmd[3] == "show" else "")
            return SimpleNamespace(returncode=0, stdout=stdout)
        return real_run(cmd, what, timeout, **kwargs)

    return routed


def test_a_killed_checkout_clears_its_own_lock_and_is_rolled_back(
        real_clone, monkeypatch, capsys):
    """``git checkout tags/<tag>`` is the destructive step, so it needs the same guard.

    It used to run *above* the ``try:`` that performs the rollback: a checkout that blew
    its 120s budget raised straight past the restore, leaving an editable install partially
    switched while the CLI reported nothing but a timeout — and the git we killed left
    ``.git/index.lock`` behind, which then blocks every subsequent git command in the clone.
    """

    project = real_clone.clone
    lock = project / ".git" / "index.lock"
    monkeypatch.setattr(update, "LATEST_TAG", "v9.9.9")
    monkeypatch.setattr(update.shutil, "which", lambda _name: real_clone.git)

    pip_calls = []
    routed = _pip_router(project, pip_calls)

    def stalled_checkout(cmd, what, timeout, **kwargs):
        cmd = list(cmd)
        if cmd[-1] == "tags/v9.9.9":
            lock.write_text("", encoding="utf-8")  # what a killed checkout leaves behind
            raise update.UpdateTimeout("Checking out the release tag timed out after 120s.")
        return routed(cmd, what, timeout, **kwargs)

    monkeypatch.setattr(update, "_run", stalled_checkout)

    with pytest.raises(update.UpdateTimeout):
        update._git_update()

    err = capsys.readouterr().err
    assert "Restoring the previous checkout" in err, "the checkout skipped the rollback"
    assert not lock.exists(), "our own killed checkout's lock still wedges the clone"
    assert "Removed the index lock" in err and str(lock) in err
    # The restore really ran, against a real repository.
    assert _branch_of(real_clone.git, project) == "main"
    assert pip_calls[-1][:5] == [sys.executable, "-m", "pip", "install", "-e"]


def test_a_lock_this_updater_did_not_create_is_named_not_deleted(
        real_clone, monkeypatch, capsys):
    """A pre-existing lock may belong to a *live* git; deleting it is worse than the wedge.

    Here the real ``git checkout`` really fails on the real lock, and so does the restore.
    Both of those must be visible: the exact path the user has to deal with, and the fact
    that the rollback did not succeed — which the unchecked restore used to swallow while
    ``main()`` still claimed "the previous installation was restored when possible".
    """

    project = real_clone.clone
    lock = project / ".git" / "index.lock"
    lock.write_text("", encoding="utf-8")  # left by something else, before we start

    monkeypatch.setattr(update, "LATEST_TAG", "v9.9.9")
    monkeypatch.setattr(update.shutil, "which", lambda _name: real_clone.git)
    pip_calls = []
    monkeypatch.setattr(update, "_run", _pip_router(project, pip_calls))

    with pytest.raises(subprocess.CalledProcessError):
        update._git_update()

    err = capsys.readouterr().err
    assert "Restoring the previous checkout" in err, "the checkout skipped the rollback"
    assert lock.exists(), "a lock this updater did not create must not be deleted"
    assert "did not create" in err and str(lock) in err
    assert "Rollback FAILED" in err, "a failed restore must not be reported as a success"
    assert _branch_of(real_clone.git, project) == "main"
