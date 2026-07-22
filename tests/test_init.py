"""engraphis-init — onboarding command. Runs on the numpy-only gate (stdlib only)."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from scripts.init import main


def test_init_writes_env_with_absolute_db_path(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["--db", "mem/engraphis.db"]) == 0
    env = (tmp_path / ".env").read_text()
    out = capsys.readouterr().out
    assert "ENGRAPHIS_DB_PATH=" in env
    assert str((tmp_path / "mem" / "engraphis.db").resolve()) in env
    assert "engraphis-mcp" in out and "mcpServers" in out   # agent snippets printed
    out.encode("ascii")  # redirected Windows consoles may not be UTF-8


def test_init_never_clobbers_existing_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("ENGRAPHIS_DB_PATH=/keep/me.db\n")
    assert main([]) == 0
    assert (tmp_path / ".env").read_text() == "ENGRAPHIS_DB_PATH=/keep/me.db\n"
    assert main(["--force"]) == 0                            # explicit opt-in overwrites
    assert "/keep/me.db" not in (tmp_path / ".env").read_text()


def test_existing_env_snippets_use_the_kept_database(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    kept = tmp_path / "kept.db"
    (tmp_path / ".env").write_text(f"ENGRAPHIS_DB_PATH={kept}\n")
    assert main([]) == 0
    assert str(kept) in capsys.readouterr().out


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits do not apply on Windows")
def test_generated_env_is_private(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["--token"]) == 0
    assert (tmp_path / ".env").stat().st_mode & 0o077 == 0


def test_init_token_flag_generates_bearer_token(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["--token"]) == 0
    assert "ENGRAPHIS_API_TOKEN=" in (tmp_path / ".env").read_text()


def test_installed_config_loads_the_env_written_in_current_directory(
        tmp_path, monkeypatch):
    """The wheel must consume the exact project-local file ``engraphis-init`` writes."""
    pytest.importorskip("dotenv")
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "preserved.db"
    main(["--db", str(target)])

    env = os.environ.copy()
    env.pop("ENGRAPHIS_DB_PATH", None)
    root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = root + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    result = subprocess.run(
        [sys.executable, "-c",
         "from engraphis.config import settings; print(settings.db_path)"],
        cwd=tmp_path, env=env, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(target.resolve())

    explicit = tmp_path / "explicit.db"
    env["ENGRAPHIS_DB_PATH"] = str(explicit)
    result = subprocess.run(
        [sys.executable, "-c",
         "from engraphis.config import settings; print(settings.db_path)"],
        cwd=tmp_path, env=env, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(explicit)


def test_doctor_runs_and_reports(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ENGRAPHIS_DB_PATH", str(tmp_path / "doc.db"))
    # settings is constructed at import; doctor re-reads env via a fresh Settings
    import engraphis.config as cfg
    monkeypatch.setattr(cfg, "settings", cfg.Settings())
    assert main(["--check"]) == 0
    out = capsys.readouterr().out
    assert "numpy (required core)" in out and "database writable" in out


def _fresh_settings(monkeypatch, tmp_path):
    import engraphis.config as cfg
    monkeypatch.setenv("ENGRAPHIS_DB_PATH", str(tmp_path / "doc.db"))
    monkeypatch.setattr(cfg, "settings", cfg.Settings())


def test_doctor_reports_local_core_reassuringly(tmp_path, monkeypatch, capsys):
    _fresh_settings(monkeypatch, tmp_path)
    monkeypatch.setenv("ENGRAPHIS_STATE_DIR", str(tmp_path / "state"))
    assert main(["--check"]) == 0
    output = capsys.readouterr().out
    assert "local core - single-user features available without a hosted subscription" in output
    assert "Engraphis Cloud - not connected" in output
    output.encode("ascii")


def test_doctor_reports_connected_cloud_install(tmp_path, monkeypatch, capsys):
    _fresh_settings(monkeypatch, tmp_path)
    monkeypatch.setenv("ENGRAPHIS_CLOUD_ACCESS_TOKEN", "cloud-token-" + "x" * 32)
    monkeypatch.setenv("ENGRAPHIS_CLOUD_ORGANIZATION_ID", "org_test")
    assert main(["--check"]) == 0
    out = capsys.readouterr().out
    assert "Engraphis Cloud - installation connected" in out
