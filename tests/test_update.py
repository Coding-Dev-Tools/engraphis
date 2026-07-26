"""The updater must never embed a stale release or select prerelease-like refs."""
import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import update


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
    monkeypatch.setattr(update.subprocess, "run", lambda command, **_kwargs: calls.append(command))
    monkeypatch.setattr(update, "LATEST_TAG", "v1.2.3")
    update._pip_update("pypi")
    assert calls == [[
        update.sys.executable, "-m", "pip", "install", "--upgrade",
        "engraphis[server]==1.2.3",
    ]]


def test_detects_noneditable_git_install_from_pep610_metadata(monkeypatch):
    monkeypatch.delenv("ENGRAPHIS_DOCKER", raising=False)
    monkeypatch.setattr(update.Path, "exists", lambda self: False)
    monkeypatch.setattr(
        update.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout="Name: engraphis\nLocation: /site-packages\n"
        ),
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
    monkeypatch.setattr(
        update.subprocess,
        "run",
        lambda command, **_kwargs: calls.append(command)
        or SimpleNamespace(returncode=0, stdout=""),
    )

    update._pip_update("git")

    assert calls[-1][-1] == f"git+{fork}@v1.2.3#egg=engraphis"
    assert update.REPO_URL not in calls[-1][-1]


def test_non_git_pep610_install_is_not_misclassified(monkeypatch):
    monkeypatch.delenv("ENGRAPHIS_DOCKER", raising=False)
    monkeypatch.setattr(update.Path, "exists", lambda self: False)
    monkeypatch.setattr(
        update.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout="Name: engraphis\nLocation: /site-packages\n"
        ),
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

    def fake_run(command, **kwargs):
        nonlocal install_attempts
        calls.append(command)
        if command[:4] == [update.sys.executable, "-m", "pip", "show"]:
            return SimpleNamespace(
                returncode=0,
                stdout=f"Editable project location: {project}\n",
            )
        if "rev-parse" in command:
            return SimpleNamespace(returncode=0, stdout="old-sha\n")
        if "symbolic-ref" in command:
            return SimpleNamespace(returncode=0, stdout="main\n")
        if "rev-list" in command:
            return SimpleNamespace(returncode=0, stdout="new-sha\n")
        if "status" in command:
            return SimpleNamespace(returncode=0, stdout="")
        if command[:4] == [update.sys.executable, "-m", "pip", "install"]:
            install_attempts += 1
            if install_attempts == 1:
                raise update.subprocess.CalledProcessError(1, command)
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(update.subprocess, "run", fake_run)
    with pytest.raises(update.subprocess.CalledProcessError):
        update._git_update()

    assert ["git", "-C", str(project), "checkout", "main"] in calls
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

    def fake_run(command, **kwargs):
        calls.append((list(command), kwargs))
        if command[:4] == [sys.executable, "-m", "pip", "show"]:
            return SimpleNamespace(
                returncode=0, stdout=f"Editable project location: {project}\n")
        if "rev-parse" in command:
            return SimpleNamespace(returncode=0, stdout="old-sha\n")
        if "symbolic-ref" in command:
            return SimpleNamespace(returncode=0, stdout="main\n")
        if "rev-list" in command:
            return SimpleNamespace(returncode=0, stdout="new-sha\n")
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(update.subprocess, "run", fake_run)

    update._git_update(check_only=True)

    git_calls = [(cmd, kwargs) for cmd, kwargs in calls if cmd[0] == "git"]
    assert git_calls, "expected the editable path to shell out to git"
    for command, kwargs in git_calls:
        assert kwargs["stdin"] is subprocess.DEVNULL, command
        assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0", command
        assert kwargs["env"]["GCM_INTERACTIVE"] == "never", command
    # The fetch's own budget is only honoured while there is no pipe left to drain.
    fetches = [kwargs for cmd, kwargs in git_calls if "fetch" in cmd]
    assert fetches and fetches[0]["capture_output"] is False
    assert fetches[0]["timeout"] == update._GIT_FETCH_TIMEOUT_S


def test_the_remote_tag_query_parses_stdout_through_the_bounded_reader(monkeypatch):
    """It must keep its stdout — so it may not simply stop capturing; it changes *how*."""

    monkeypatch.setattr(
        update.subprocess, "run",
        lambda *a, **k: pytest.fail("a network query must not use the unenforceable path"),
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
