"""engraphis-init — onboarding command. Runs on the numpy-only gate (stdlib only)."""
from scripts.init import main


def test_init_writes_env_with_absolute_db_path(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["--db", "mem/engraphis.db"]) == 0
    env = (tmp_path / ".env").read_text()
    out = capsys.readouterr().out
    assert "ENGRAPHIS_DB_PATH=" in env
    assert str((tmp_path / "mem" / "engraphis.db").resolve()) in env
    assert "engraphis-mcp" in out and "mcpServers" in out   # agent snippets printed


def test_init_never_clobbers_existing_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("ENGRAPHIS_DB_PATH=/keep/me.db\n")
    assert main([]) == 0
    assert (tmp_path / ".env").read_text() == "ENGRAPHIS_DB_PATH=/keep/me.db\n"
    assert main(["--force"]) == 0                            # explicit opt-in overwrites
    assert "/keep/me.db" not in (tmp_path / ".env").read_text()


def test_init_token_flag_generates_bearer_token(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["--token"]) == 0
    assert "ENGRAPHIS_API_TOKEN=" in (tmp_path / ".env").read_text()


def test_doctor_runs_and_reports(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ENGRAPHIS_DB_PATH", str(tmp_path / "doc.db"))
    # settings is constructed at import; doctor re-reads env via a fresh Settings
    import engraphis.config as cfg
    monkeypatch.setattr(cfg, "settings", cfg.Settings())
    assert main(["--check"]) == 0
    out = capsys.readouterr().out
    assert "numpy (required core)" in out and "database writable" in out
