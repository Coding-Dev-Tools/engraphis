from __future__ import annotations

import json
import subprocess
from pathlib import Path

from scripts import audit, entry


def test_run_audit_passes_offline_and_leaves_env_db_untouched(tmp_path: Path, monkeypatch) -> None:
    sentinel_db = tmp_path / "should_not_exist.db"
    monkeypatch.setenv("ENGRAPHIS_DB_PATH", str(sentinel_db))

    report = audit.run_audit()

    assert report["schema"] == "engraphis-audit/v1"
    assert report["offline"] is True
    assert report["passed"] is True
    assert report["score"] == "3/3"
    assert len(report["checks"]) == 3
    assert all(check["passed"] for check in report["checks"])
    assert not sentinel_db.exists()


def test_audit_cli_json_and_markdown_outputs_have_no_emdash(capsys) -> None:
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


def test_audit_repo_flag_reads_git_commit_subject_read_only(tmp_path: Path) -> None:
    repo = tmp_path / "sample-repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "Coding-Dev-Tools"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "274649000+Coding-Dev-Tools@users.noreply.github.com"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    (repo / "README.md").write_text("# Sample\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "feat: migrate auth tokens from JWT to PASETO"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    report = audit.run_audit(repo_path=repo)
    assert report["passed"] is True
    assert report["repo_commit_hint"] == "feat: migrate auth tokens from JWT to PASETO"
    assert "migrate auth tokens from JWT to PASETO" in report["diagnostic_prompt"]
