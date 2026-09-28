"""engraphis-init — onboarding command. Runs on the numpy-only gate (stdlib only)."""
import os
import json
import sqlite3
from pathlib import Path
import subprocess
import sys

import pytest

import scripts.init as init_script
from scripts.init import main


def _config_env(tmp_path: Path) -> Path:
    return tmp_path / "home" / ".engraphis" / "config.env"


def _write_private(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)


@pytest.fixture(autouse=True)
def _select_trusted_config(tmp_path, monkeypatch):
    path = _config_env(tmp_path)
    monkeypatch.setattr(init_script, "_trusted_env_file", lambda: path)
    # Fresh Settings instances must keep the offline test configuration.
    monkeypatch.setenv("ENGRAPHIS_EMBED_MODEL", "")
    return path


def test_init_writes_trusted_env_with_absolute_db_path(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["--db", "mem/engraphis.db"]) == 0
    env = _config_env(tmp_path).read_text()
    out = capsys.readouterr().out
    assert "ENGRAPHIS_DB_PATH=" in env
    assert "ENGRAPHIS_API_TOKEN=" in env
    assert str((tmp_path / "mem" / "engraphis.db").resolve()) in env
    assert not (tmp_path / ".env").exists()
    assert "engraphis-mcp" in out and "mcpServers" in out
    generated_token = next(line.split("=", 1)[1] for line in env.splitlines() if line.startswith("ENGRAPHIS_API_TOKEN="))
    assert len(generated_token) >= 24 and generated_token not in out
    out.encode("ascii")


def test_init_never_clobbers_existing_trusted_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    env_file = _config_env(tmp_path)
    _write_private(env_file, "ENGRAPHIS_DB_PATH=/keep/me.db\n")
    assert main([]) == 0
    assert env_file.read_text() == "ENGRAPHIS_DB_PATH=/keep/me.db\n"
    assert main(["--force"]) == 0
    assert "/keep/me.db" not in env_file.read_text()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits do not apply on Windows")
def test_init_rejects_an_insecure_existing_trusted_env(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    env_file = _config_env(tmp_path)
    _write_private(env_file, "ENGRAPHIS_DB_PATH=/keep/me.db\n")
    env_file.chmod(0o644)

    assert main([]) == 1

    assert env_file.read_text() == "ENGRAPHIS_DB_PATH=/keep/me.db\n"
    assert "owner-only permissions are required" in capsys.readouterr().out


def test_init_reports_trusted_config_selection_failure(monkeypatch, capsys):
    def fail():
        raise ValueError("ENGRAPHIS_ENV_FILE must be an absolute path")

    monkeypatch.setattr(init_script, "_trusted_env_file", fail)

    assert main([]) == 1
    assert "ENGRAPHIS_ENV_FILE must be an absolute path" in capsys.readouterr().out


def test_existing_env_snippets_use_the_kept_database(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    kept = tmp_path / "kept.db"
    _write_private(_config_env(tmp_path), f"ENGRAPHIS_DB_PATH={kept}\n")
    assert main([]) == 0
    assert str(kept) in capsys.readouterr().out


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits do not apply on Windows")
def test_generated_trusted_env_and_parent_are_private(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["--token"]) == 0
    env_file = _config_env(tmp_path)
    assert env_file.stat().st_mode & 0o077 == 0
    assert env_file.parent.stat().st_mode & 0o077 == 0


def test_init_token_flag_generates_bearer_token(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["--token"]) == 0
    assert "ENGRAPHIS_API_TOKEN=" in _config_env(tmp_path).read_text()


def test_init_encrypted_generates_private_key_file_and_mcp_configuration(
        tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "scripts.init._try_import",
        lambda name: object() if name == "sqlcipher3" else None,
    )

    assert main(["--encrypted", "--db", "vault/mem.db"]) == 0

    db_path = (tmp_path / "vault" / "mem.db").resolve()
    key_path = db_path.with_name(".mem.db.key")
    env = _config_env(tmp_path).read_text()
    output = capsys.readouterr().out
    assert f"ENGRAPHIS_DB_KEY_FILE={key_path}" in env
    key = key_path.read_text().strip()
    assert len(key) == 64 and all(character in "0123456789abcdef" for character in key)
    assert str(key_path) in output
    assert key not in env and key not in output


def test_init_uses_encryption_by_default_when_sqlcipher_is_available(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "scripts.init._try_import",
        lambda name: object() if name == "sqlcipher3" else None,
    )

    assert main([]) == 0

    env = _config_env(tmp_path).read_text()
    assert "ENGRAPHIS_DB_KEY_FILE=" in env


def test_init_refuses_to_attach_new_key_to_existing_database(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "scripts.init._try_import",
        lambda name: object() if name == "sqlcipher3" else None,
    )
    existing = tmp_path / "existing.db"
    existing.write_bytes(b"SQLite format 3\x00")

    assert main(["--encrypted", "--db", str(existing)]) == 1

    assert not _config_env(tmp_path).exists()
    assert not existing.with_name(".existing.db.key").exists()
    assert "refusing to enable encryption" in capsys.readouterr().out


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits do not apply on Windows")
def test_generated_encryption_key_is_private(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "scripts.init._try_import",
        lambda name: object() if name == "sqlcipher3" else None,
    )

    assert main(["--encrypted"]) == 0

    key_path = tmp_path / ".engraphis.db.key"
    assert key_path.stat().st_mode & 0o077 == 0


def test_installed_config_loads_the_trusted_env_from_an_unrelated_cwd(
        tmp_path, monkeypatch):
    """The wheel must consume the exact trusted file ``engraphis-init`` writes."""
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "preserved.db"
    main(["--db", str(target)])

    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    (unrelated / ".env").write_text("ENGRAPHIS_DB_PATH=/wrong/cwd.db\n")
    env = os.environ.copy()
    env.pop("ENGRAPHIS_DB_PATH", None)
    env["ENGRAPHIS_ENV_FILE"] = str(_config_env(tmp_path))
    root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = root + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    result = subprocess.run(
        [sys.executable, "-c",
         "from engraphis.config import settings; print(settings.db_path)"],
        cwd=unrelated, env=env, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(target.resolve())

    explicit = tmp_path / "explicit.db"
    env["ENGRAPHIS_DB_PATH"] = str(explicit)
    result = subprocess.run(
        [sys.executable, "-c",
         "from engraphis.config import settings; print(settings.db_path)"],
        cwd=unrelated, env=env, capture_output=True, text=True, check=False,
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
    assert "Engraphis Cloud - configuration present; connection not verified" in out


def test_doctor_reports_functional_embedder(tmp_path, monkeypatch, capsys):
    _fresh_settings(monkeypatch, tmp_path)
    assert main(["--check"]) == 0
    out = capsys.readouterr().out
    assert "embedder functional" in out


def test_doctor_explains_tokenless_review_setup(tmp_path, monkeypatch, capsys):
    _fresh_settings(monkeypatch, tmp_path)
    import engraphis.config as cfg
    monkeypatch.setattr(cfg.settings, "api_token", "")
    assert main(["--check", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    review = next(check for check in report["checks"] if check["code"] == "browser_approval")
    assert review["status"] == "optional"
    assert "private config" in review["detail"] and "engraphis-dashboard" in review["detail"]


def test_prefetch_command_reports_ready_or_offline(tmp_path, monkeypatch, capsys):
    _fresh_settings(monkeypatch, tmp_path)
    # Test prefetch with offline deterministic model
    monkeypatch.setenv("ENGRAPHIS_EMBED_MODEL", "")
    import engraphis.config as cfg
    monkeypatch.setattr(cfg, "settings", cfg.Settings())
    assert main(["--prefetch"]) == 0
    out = capsys.readouterr().out
    assert "deterministic offline embedder is active" in out


def test_doctor_json_probes_writes_without_leaving_schema_or_rows(tmp_path, monkeypatch, capsys):
    _fresh_settings(monkeypatch, tmp_path)
    path = tmp_path / "doc.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE preserved (value TEXT)")
        conn.execute("INSERT INTO preserved VALUES ('existing record')")
    assert main(["--check", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["schema_version"] == 1 and report["ok"]
    assert any(check["code"] == "database_writable" for check in report["checks"])
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [("preserved",)]
        assert conn.execute("SELECT value FROM preserved").fetchall() == [("existing record",)]


def test_doctor_rejects_readable_but_read_only_database(tmp_path, monkeypatch, capsys):
    _fresh_settings(monkeypatch, tmp_path)
    path = tmp_path / "doc.db"
    connect = sqlite3.connect
    with connect(path) as conn:
        conn.execute("CREATE TABLE preserved (value TEXT)")
    monkeypatch.setattr(init_script, "connector_from_env", lambda: None)
    monkeypatch.setattr(init_script.sqlite3, "connect", lambda *_args, **_kwargs: connect(path.as_uri() + "?mode=ro", uri=True))
    assert main(["--check", "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert not report["ok"]
    checks = {check["code"]: check for check in report["checks"]}
    assert checks["database_readable"]["status"] == "ok"
    assert checks["database_unwritable"]["status"] == "fail"
    assert "database_writable" not in checks


def test_doctor_rejects_a_newer_database_schema(tmp_path, monkeypatch, capsys):
    _fresh_settings(monkeypatch, tmp_path)
    from engraphis.core.schema import SCHEMA_VERSION
    with sqlite3.connect(tmp_path / "doc.db") as conn:
        conn.execute("CREATE TABLE schema_migrations (version INTEGER)")
        conn.execute("INSERT INTO schema_migrations VALUES (?)", (SCHEMA_VERSION + 1,))
    assert main(["--check", "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert any(check["code"] == "schema_newer" for check in report["checks"])


def test_doctor_reports_lock_contention_without_leaving_probe_state(tmp_path, monkeypatch, capsys):
    _fresh_settings(monkeypatch, tmp_path)
    path = tmp_path / "doc.db"
    blocker = sqlite3.connect(path)
    try:
        blocker.execute("CREATE TABLE preserved (value TEXT)")
        blocker.execute("BEGIN EXCLUSIVE")
        assert main(["--check", "--json"]) == 1
        report = json.loads(capsys.readouterr().out)
        assert any(check["code"] == "database_locked" for check in report["checks"])
    finally:
        blocker.rollback()
        blocker.close()
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [("preserved",)]


def test_doctor_does_not_mask_a_broken_configured_embedder_or_echo_its_error(tmp_path, monkeypatch, capsys):
    _fresh_settings(monkeypatch, tmp_path)
    import engraphis.config as cfg
    import engraphis.backends.embedder_st as backend
    monkeypatch.setattr(cfg.settings, "embed_model", "missing-configured-model")

    def fail(*_args, **kwargs):
        assert kwargs["require_exact"] is True
        raise RuntimeError("private-provider-secret")

    monkeypatch.setattr(backend, "get_embedder", fail)
    assert main(["--check", "--json"]) == 1
    output = capsys.readouterr().out
    assert "private-provider-secret" not in output
    report = json.loads(output)
    assert any(check["code"] == "embedder" and check["status"] == "fail" for check in report["checks"])


def test_init_records_only_explicit_installation_intent(tmp_path, monkeypatch, capsys):
    from scripts import installation_profile
    monkeypatch.chdir(tmp_path)
    path = installation_profile.profile_path(_config_env(tmp_path))
    assert main(["--no-encryption"]) == 0
    assert not path.exists()
    assert main(["--extras", "server,mcp", "--no-encryption"]) == 0
    assert json.loads(path.read_text())["extras"] == ["mcp", "server"]
    output = capsys.readouterr().out
    assert "codex mcp add engraphis" in output
    assert "save one project decision" in output


def test_init_rejects_invalid_extras_before_writing_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        main(["--extras", "server;owned"])
    assert exc.value.code == 2
    assert not _config_env(tmp_path).exists()


def test_init_configures_jev_key_on_fresh_setup(tmp_path, monkeypatch, capsys):
    from engraphis.config import _parse_trusted_env

    monkeypatch.chdir(tmp_path)
    assert main(["--jev-key", "test-typesafe-key-123", "--no-encryption"]) == 0
    values = _parse_trusted_env(_config_env(tmp_path).read_text())
    assert values["TYPESAFE_API_KEY"] == "test-typesafe-key-123"
    assert values["JEV_API_KEY"] == "test-typesafe-key-123"
    assert values["ENGRAPHIS_DECISION_BACKEND"] == "byok"
    out = capsys.readouterr().out
    assert "jev api key -> configured in trusted config" in out
    assert "test-typesafe-key-123" not in out


def test_init_updates_jev_key_on_existing_setup(tmp_path, monkeypatch, capsys):
    from engraphis.config import _parse_trusted_env

    monkeypatch.chdir(tmp_path)
    env_file = _config_env(tmp_path)
    _write_private(env_file, "ENGRAPHIS_DB_PATH=/keep/database.db\n")
    assert main(["--jev-key", "updated-key-456"]) == 0
    values = _parse_trusted_env(env_file.read_text())
    assert values["ENGRAPHIS_DB_PATH"] == "/keep/database.db"
    assert values["TYPESAFE_API_KEY"] == "updated-key-456"
    assert values["JEV_API_KEY"] == "updated-key-456"
    assert values["ENGRAPHIS_DECISION_BACKEND"] == "byok"
    out = capsys.readouterr().out
    assert "jev api key -> updated in trusted config" in out
    assert "updated-key-456" not in out


def test_init_reads_jev_key_from_stdin(tmp_path, monkeypatch, capsys):
    import io
    from engraphis.config import _parse_trusted_env

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "stdin", io.StringIO("stdin-key-789\n"))
    assert main(["--jev-key", "-", "--no-encryption"]) == 0
    values = _parse_trusted_env(_config_env(tmp_path).read_text())
    assert values["TYPESAFE_API_KEY"] == "stdin-key-789"
    assert values["JEV_API_KEY"] == "stdin-key-789"
    out = capsys.readouterr().out
    assert "jev api key -> configured in trusted config" in out
    assert "stdin-key-789" not in out


@pytest.mark.parametrize("existing", [False, True], ids=["fresh", "existing"])
@pytest.mark.parametrize("key", [
    '"synthetic-leading-double', "'synthetic-leading-single",
    'synthetic"double\'single\\backslash#hash=$dollar', "synthetic-trailing-backslash\\",
])
def test_init_jev_key_roundtrips_trusted_parser(tmp_path, monkeypatch, capsys, existing, key):
    from engraphis.config import _parse_trusted_env

    monkeypatch.chdir(tmp_path)
    env_file = _config_env(tmp_path)
    if existing:
        _write_private(env_file, "ENGRAPHIS_DB_PATH=/keep/database.db\n")
    assert main(["--jev-key", key, "--no-encryption"]) == 0
    values = _parse_trusted_env(env_file.read_text())
    assert values["TYPESAFE_API_KEY"] == key
    assert values["JEV_API_KEY"] == key
    assert values["ENGRAPHIS_DECISION_BACKEND"] == "byok"
    if existing:
        assert values["ENGRAPHIS_DB_PATH"] == "/keep/database.db"
    captured = capsys.readouterr()
    assert key not in captured.out + captured.err


@pytest.mark.parametrize("key", ["", "key with space", "synthetic\nsecret", "synthetic\x00secret",
                                 "synthetic\tsecret", "synthetic\u00e9secret"])
def test_init_rejects_malformed_jev_key(tmp_path, monkeypatch, capsys, key):
    monkeypatch.chdir(tmp_path)
    assert main(["--jev-key", key, "--no-encryption"]) == 1
    assert not _config_env(tmp_path).exists()
    captured = capsys.readouterr()
    assert "key must be printable ASCII without whitespace" in captured.out
    if key:
        assert key not in captured.out + captured.err


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("source", ["argument", "stdin"])
def test_init_rejects_oversized_jev_key_without_writing(tmp_path, monkeypatch, capsys, existing, source):
    import io

    monkeypatch.chdir(tmp_path)
    env_file = _config_env(tmp_path)
    original = b"ENGRAPHIS_DB_PATH=/keep/database.db\n"
    if existing:
        _write_private(env_file, original.decode())
        original = env_file.read_bytes()
    key = "synthetic-" + "x" * (600 * 1024)
    stream = io.StringIO(key + "\n")
    monkeypatch.setattr(sys, "stdin", stream)
    assert main(["--jev-key", "-" if source == "stdin" else key, "--no-encryption"]) == 1
    if source == "stdin":
        assert stream.tell() == init_script._MAX_JEV_KEY_CHARS + 3
    assert env_file.read_bytes() == original if existing else not env_file.exists()
    captured = capsys.readouterr()
    assert "at most 4096 characters" in captured.out
    assert "synthetic-" not in captured.out + captured.err


@pytest.mark.parametrize("source", ["argument", "stdin"])
def test_init_maximum_jev_key_roundtrips_with_escaping(tmp_path, monkeypatch, capsys, source):
    import io
    from engraphis.config import _parse_trusted_env

    monkeypatch.chdir(tmp_path)
    key = '\\"' * (init_script._MAX_JEV_KEY_CHARS // 2)
    assert len(key) == init_script._MAX_JEV_KEY_CHARS
    monkeypatch.setattr(sys, "stdin", io.StringIO(key + "\r\n"))
    assert main(["--jev-key", "-" if source == "stdin" else key, "--no-encryption"]) == 0
    content = _config_env(tmp_path).read_text()
    values = _parse_trusted_env(content)
    assert values["TYPESAFE_API_KEY"] == values["JEV_API_KEY"] == key
    assert len(content.encode()) <= 1024 * 1024
    assert key not in capsys.readouterr().out


def test_init_jev_update_preserves_a_near_limit_config(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    env_file = _config_env(tmp_path)
    prefix = "ENGRAPHIS_DB_PATH=/keep/database.db\n#"
    original = prefix + "x" * (1024 * 1024 - len(prefix) - 1) + "\n"
    _write_private(env_file, original)
    env_file.write_bytes(original.encode())
    before = env_file.read_bytes()
    assert len(before) == 1024 * 1024
    mode = env_file.stat().st_mode
    assert main(["--jev-key", "synthetic-small-key", "--no-encryption"]) == 1
    assert env_file.read_bytes() == before
    assert env_file.stat().st_mode == mode
    captured = capsys.readouterr()
    assert "1 MiB size limit" in captured.out
    assert "synthetic-small-key" not in captured.out + captured.err


@pytest.mark.parametrize("existing", [False, True])
def test_init_rejects_oversized_rendered_config_before_publication(tmp_path, monkeypatch, capsys, existing):
    monkeypatch.chdir(tmp_path)
    env_file = _config_env(tmp_path)
    original = b"ENGRAPHIS_DB_PATH=/keep/database.db\n"
    if existing:
        _write_private(env_file, original.decode())
        original = env_file.read_bytes()
    monkeypatch.setattr(init_script, "_env_content", lambda *args, **kwargs: "\u00e9" * (512 * 1024 + 1))
    assert main(["--force", "--no-encryption"]) == 1
    assert env_file.read_bytes() == original if existing else not env_file.exists()
    assert "1 MiB size limit" in capsys.readouterr().out


def test_doctor_reports_jev_decision_status(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("JEV_API_KEY", raising=False)

    # Optional / not configured
    assert main(["--check", "--json"]) == 0
    report_unconf = json.loads(capsys.readouterr().out)
    jev_check = next(c for c in report_unconf["checks"] if c["code"] == "jev_decision")
    assert jev_check["status"] == "optional"

    # Configured
    monkeypatch.setenv("TYPESAFE_API_KEY", "apikey_test_123")
    monkeypatch.setenv("ENGRAPHIS_DECISION_BACKEND", "byok")
    assert main(["--check", "--json"]) == 0
    report_conf = json.loads(capsys.readouterr().out)
    jev_check_conf = next(c for c in report_conf["checks"] if c["code"] == "jev_decision")
    assert jev_check_conf["status"] == "ok"
    assert "configured (TypeSafe AI BYOK); not verified" in jev_check_conf["detail"]
