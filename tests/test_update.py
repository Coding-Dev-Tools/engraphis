"""The updater must never embed a stale release or select prerelease-like refs."""
import json
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
