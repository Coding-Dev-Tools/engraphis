from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from scripts import audit, entry


def _isolated_git_env() -> dict[str, str]:
    env = dict(os.environ)
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    return env


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=", *args],
        cwd=repo,
        env=_isolated_git_env(),
        check=True,
        capture_output=True,
    )


def test_run_audit_is_hermetically_isolated_from_host_env_and_encryption(
    tmp_path: Path, monkeypatch
) -> None:
    sentinel_db = tmp_path / "should_not_exist.db"
    monkeypatch.setenv("ENGRAPHIS_DB_PATH", str(sentinel_db))
    monkeypatch.setenv("ENGRAPHIS_DB_KEY", "test-key-must-be-ignored")
    monkeypatch.setenv("ENGRAPHIS_ENV_FILE", str(tmp_path / "nonexistent-config.env"))
    monkeypatch.setenv("ENGRAPHIS_SQLITE_DURABILITY", "invalid_mode")

    report = audit.run_audit()

    assert report["schema"] == "engraphis-audit/v1"
    assert report["offline"] is True
    assert report["passed"] is True
    assert report["score"] == "3/3"
    assert len(report["checks"]) == 3
    assert all(check["passed"] for check in report["checks"])
    assert not sentinel_db.exists()


def test_audit_cli_json_markdown_help_and_mutual_exclusion(capsys) -> None:
    rc_json = entry.main(["audit", "--json"])
    assert rc_json == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["passed"] is True
    assert payload["score"] == "3/3"

    rc_md = entry.main(["audit", "--markdown"])
    assert rc_md == 0
    md = capsys.readouterr().out
    assert "\u2014" not in md
    assert "Bi-temporal supersession" in md
    assert "engraphis-audit" in md

    rc_text = audit.main([])
    assert rc_text == 0
    text = capsys.readouterr().out
    assert "\u2014" not in text
    assert "[PASS]" in text
    assert "engraphis-init" in text
    assert "engraphis-init --prefetch" in text

    with pytest.raises(SystemExit) as help_exit:
        audit.main(["--help"])
    assert help_exit.value.code == 0
    assert "usage:" in capsys.readouterr().out

    with pytest.raises(SystemExit) as conflict_exit:
        audit.main(["--json", "--markdown"])
    assert conflict_exit.value.code == 2


def test_audit_repo_flag_sanitizes_utf8_ansi_emdash_and_skips_bump_commits(tmp_path: Path) -> None:
    repo = tmp_path / "sample-repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Coding-Dev-Tools")
    _git(repo, "config", "user.email", "274649000+Coding-Dev-Tools@users.noreply.github.com")

    (repo / "README.md").write_text("# Sample\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "feat: migrate auth \x1b[31mtokens\x1b[0m \u2014 JWT to PASETO \U0001f60f")

    (repo / "README.md").write_text("# Sample 2\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "bump ruff from 0.15 to 0.16")

    report = audit.run_audit(repo_path=repo)
    assert report["passed"] is True
    hint = report["repo_commit_hint"]
    assert hint is not None
    assert "migrate auth tokens - JWT to PASETO" in hint
    assert "\x1b" not in hint
    assert "\u2014" not in hint

    md = audit.render_markdown(report)
    assert "\u2014" not in md
    terminal = audit.render_terminal(report)
    terminal.encode("ascii")


def test_audit_repo_flag_fallbacks_for_missing_or_non_git_paths(tmp_path: Path) -> None:
    non_git = tmp_path / "plain-dir"
    non_git.mkdir()
    missing = tmp_path / "does-not-exist"

    for path in (non_git, missing):
        prompt, subject = audit.build_diagnostic_prompt(path)
        assert subject is None
        assert prompt == audit.DEFAULT_DIAGNOSTIC_PROMPT


def test_audit_check1_does_not_false_pass_on_migration_rationale_only(monkeypatch) -> None:
    orig_create = audit._create_isolated_service

    def patched_service():
        svc = orig_create()
        orig_why = svc.why

        def fake_why(*args, **kwargs):
            res = orig_why(*args, **kwargs)
            # Keep only the migration rationale memory (not the active auth.token_format decision).
            res["answer"] = [
                item
                for item in res.get("answer", [])
                if "November auth migration" in str(item.get("content") or "")
            ]
            return res

        svc.why = fake_why  # type: ignore[method-assign]
        return svc

    monkeypatch.setattr(audit, "_create_isolated_service", patched_service)
    report = audit.run_audit()
    assert report["checks"][0]["passed"] is False
    assert audit.main(["--json"]) == 1


def test_audit_prompt_json_quotes_embedded_quotes_and_redacts_secrets(tmp_path: Path) -> None:
    repo = tmp_path / "quoted-repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Coding-Dev-Tools")
    _git(repo, "config", "user.email", "274649000+Coding-Dev-Tools@users.noreply.github.com")
    (repo / "a.txt").write_text("hi\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    fake_tok = "ghp_" + "A" * 36
    _git(repo, "commit", "-m", f'fix: escape "quoted" subject and rotate {fake_tok}')

    prompt, subject = audit.build_diagnostic_prompt(repo)
    assert subject is not None
    assert fake_tok not in subject
    assert fake_tok not in prompt
    assert "<redacted>" in subject
    assert json.dumps(subject) in prompt


def test_run_audit_succeeds_under_frozen_coarse_clock(monkeypatch) -> None:
    """Coarse/identical system clock ticks must not break AUDIT_FACTS supersession."""
    import time as time_mod
    from engraphis.core import store as core_store

    frozen = 1_750_000_000.0
    monkeypatch.setattr(time_mod, "time", lambda: frozen)
    monkeypatch.setattr(core_store, "now_ts", lambda: frozen)

    report = audit.run_audit()
    assert report["passed"] is True
    assert report["score"] == "3/3"
    assert all(check["passed"] for check in report["checks"])


def test_audit_repo_isolates_inherited_git_env_and_invalid_paths(
    tmp_path: Path, monkeypatch
) -> None:
    """Inherited GIT_DIR must not hijack --repo, and invalid paths fall back cleanly."""
    private_repo = tmp_path / "private-repo"
    private_repo.mkdir()
    _git(private_repo, "init")
    _git(private_repo, "config", "user.name", "Coding-Dev-Tools")
    _git(private_repo, "config", "user.email", "274649000+Coding-Dev-Tools@users.noreply.github.com")
    (private_repo / "secret.txt").write_text("private\n", encoding="utf-8")
    _git(private_repo, "add", "secret.txt")
    _git(private_repo, "commit", "-m", "chore: internal private acquisition roadmap")

    plain_dir = tmp_path / "not-a-repo"
    plain_dir.mkdir()
    monkeypatch.setenv("GIT_DIR", str(private_repo / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(private_repo))

    prompt, subject = audit.build_diagnostic_prompt(plain_dir)
    assert subject is None
    assert "acquisition roadmap" not in prompt
    assert prompt == audit.DEFAULT_DIAGNOSTIC_PROMPT

    bad_prompt, bad_subject = audit.build_diagnostic_prompt(Path("bad\x00path"))
    assert bad_subject is None
    assert bad_prompt == audit.DEFAULT_DIAGNOSTIC_PROMPT
