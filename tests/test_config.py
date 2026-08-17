"""Config wiring tests — env vars must reach Settings, and the offline defaults must hold.

Covers the ENGRAPHIS_RERANK_MODEL knob added so the cross-encoder reranker (the biggest
precision win on top of hybrid retrieval) can be turned on by config
instead of only in code. The default must stay empty so the offline/numpy-only CI path is
unchanged (empty -> None -> IdentityReranker, no torch).
"""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from engraphis import config
from engraphis.config import Settings


REPO_ROOT = Path(__file__).resolve().parents[1]
RETIRED_RELAY_URLS = (
    "https://engraphis-production.up.railway.app",
    "https://team.engraphis.com",
)


def test_deployment_mode_defaults_to_local(monkeypatch):
    for name in config._HOSTED_MODE_ENV_VARS:
        monkeypatch.delenv(name, raising=False)

    assert config.deployment_mode() == "local"
    assert config.is_local_mode() is True
    assert config.is_hosted_mode() is False


def test_deployment_mode_detects_hosted_configuration_and_explicit_overrides(monkeypatch):
    for name in config._HOSTED_MODE_ENV_VARS:
        monkeypatch.delenv(name, raising=False)

    monkeypatch.setenv("ENGRAPHIS_CLOUD_CONTROL_URL", "https://cloud.example.test")
    assert config.deployment_mode() == "hosted"

    monkeypatch.setenv("ENGRAPHIS_HOSTED_MODE", "false")
    assert config.deployment_mode() == "local"
    monkeypatch.setenv("ENGRAPHIS_HOSTED_MODE", "true")
    assert config.deployment_mode() == "hosted"


def test_rerank_model_defaults_to_empty(monkeypatch):
    monkeypatch.delenv("ENGRAPHIS_RERANK_MODEL", raising=False)
    monkeypatch.delenv("ENGRAPHIS_RERANK_REVISION", raising=False)
    assert Settings().rerank_model == ""
    assert Settings().rerank_revision == ""


def test_cors_default_origins_follow_configured_port():
    # The empty-CORS default derives loopback origins from the port, so running on a
    # non-default ENGRAPHIS_PORT doesn't lock the dashboard's own origin out.
    assert config._parse_origins("", 9000) == [
        "http://127.0.0.1:9000", "http://localhost:9000"]
    # Explicit origins pass through unchanged.
    assert config._parse_origins("https://app.example.com", 9000) == [
        "https://app.example.com"]


def test_cors_origins_use_engraphis_port_env(monkeypatch):
    monkeypatch.delenv("ENGRAPHIS_CORS_ORIGINS", raising=False)
    monkeypatch.setenv("ENGRAPHIS_PORT", "9100")
    assert Settings().cors_origins == [
        "http://127.0.0.1:9100", "http://localhost:9100"]


def test_sample_operational_config_matches_runtime_contract(monkeypatch):
    example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    monkeypatch.delenv("ENGRAPHIS_RATE_LIMIT", raising=False)
    assert Settings().rate_limit == 0
    assert "# ENGRAPHIS_RATE_LIMIT=0" in example

    monkeypatch.setenv("ENGRAPHIS_WORKSPACES", "acme,personal")
    assert Settings().allowed_workspaces == []
    assert "ENGRAPHIS_WORKSPACES" not in example

    assert "http://127.0.0.1:<ENGRAPHIS_PORT>" in example
    assert "http://localhost:<ENGRAPHIS_PORT>" in example
    assert "These settings do not change CORS" in example
    assert "# ENGRAPHIS_DASHBOARD_URL=https://engraphis.example.com" in example
    assert "# ENGRAPHIS_API_TOKEN=<a-long-random-secret>" in example
    assert "docker-compose.lan.yml" in example
    assert "# ENGRAPHIS_DASHBOARD_URL=http://192.168.10.151:8700" in example
    assert "# ENGRAPHIS_DASHBOARD_URL=http://engraphis.local" in example
    assert "# ENGRAPHIS_HTTP_PORT=8711" in example

    monkeypatch.delenv("ENGRAPHIS_LLM_AUTO_EXTRACT", raising=False)
    assert Settings().llm_auto_extract is False
    assert "ENGRAPHIS_LLM_AUTO_EXTRACT=0" in example
    assert "| `ENGRAPHIS_LLM_AUTO_EXTRACT` | `0` |" in readme

    for name in (
        "ENGRAPHIS_DECAY_HALFLIFE_DAYS",
        "ENGRAPHIS_LOOP_INTERVAL",
        "ENGRAPHIS_LOOP_TOP_K",
    ):
        monkeypatch.delenv(name, raising=False)
    configured = Settings()
    assert f"# ENGRAPHIS_DECAY_HALFLIFE_DAYS={configured.decay_halflife_days:g}" in example
    assert f"# ENGRAPHIS_LOOP_INTERVAL={configured.loop_interval}" in example
    assert f"# ENGRAPHIS_LOOP_TOP_K={configured.loop_top_k}" in example

    from engraphis.backends.extractor import (
        CHUNK_MAX,
        CHUNK_OVERLAP_TOKENS,
        CHUNK_TARGET_TOKENS,
    )

    assert f"# ENGRAPHIS_CHUNK_TOKENS={CHUNK_TARGET_TOKENS}" in example
    assert f"# ENGRAPHIS_CHUNK_MAX={CHUNK_MAX}" in example
    assert f"# ENGRAPHIS_CHUNK_OVERLAP={CHUNK_OVERLAP_TOKENS}" in example


def test_rerank_model_read_from_env(monkeypatch):
    monkeypatch.setenv("ENGRAPHIS_RERANK_MODEL", "cross-encoder/ms-marco-MiniLM-L-6-v2")
    monkeypatch.setenv("ENGRAPHIS_RERANK_REVISION", "a" * 40)
    assert Settings().rerank_model == "cross-encoder/ms-marco-MiniLM-L-6-v2"
    assert Settings().rerank_revision == "a" * 40


def test_empty_rerank_model_normalizes_to_none(monkeypatch):
    # This is the exact expression the service builders pass:
    #   MemoryService.create(..., rerank_model=settings.rerank_model or None)
    # Empty must become None so get_reranker returns the offline IdentityReranker.
    monkeypatch.delenv("ENGRAPHIS_RERANK_MODEL", raising=False)
    assert (Settings().rerank_model or None) is None


def test_service_builds_offline_with_default_rerank_model(monkeypatch):
    # End-to-end: with no rerank model configured, a MemoryService builds on numpy alone
    # (DeterministicEmbedder + IdentityReranker) and serves a round-trip — the CI path.
    monkeypatch.delenv("ENGRAPHIS_RERANK_MODEL", raising=False)
    from engraphis.service import MemoryService
    from engraphis.backends.vector_numpy import NumpyVectorIndex
    svc = MemoryService.create(":memory:", rerank_model=(Settings().rerank_model or None))
    assert isinstance(svc.engine.index, NumpyVectorIndex)
    pending = svc.remember("a durable fact", workspace="w", repo="r")
    assert pending["stored"] is True
    svc.engine.approve_for_prompt(
        pending["id"], reviewer="test_operator", reason="offline control",
    )
    assert svc.recall("a durable fact", workspace="w", repo="r")["count"] >= 1


def test_embed_dim_defaults_to_default_model_dimension(monkeypatch):
    monkeypatch.delenv("ENGRAPHIS_EMBED_DIM", raising=False)
    assert Settings().embed_dim == 384


def test_model_provenance_settings_read_environment_and_are_documented(monkeypatch):
    monkeypatch.setenv("ENGRAPHIS_EMBED_REVISION", "a" * 40)
    monkeypatch.setenv("ENGRAPHIS_REQUIRE_IMMUTABLE_MODELS", "true")

    configured = Settings()

    assert configured.embed_revision == "a" * 40
    assert configured.require_immutable_models is True
    assert "ENGRAPHIS_EMBED_REVISION" in (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "ENGRAPHIS_REQUIRE_IMMUTABLE_MODELS" in (REPO_ROOT / "README.md").read_text(
        encoding="utf-8"
    )
    assert "ENGRAPHIS_RERANK_REVISION" in (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "ENGRAPHIS_RERANK_REVISION" in (REPO_ROOT / "README.md").read_text(encoding="utf-8")


def test_server_vector_backend_defaults_to_safe_auto(monkeypatch):
    monkeypatch.delenv("ENGRAPHIS_VECTOR_BACKEND", raising=False)
    assert Settings().vector_backend == "auto"


def test_vector_backend_reads_env(monkeypatch):
    monkeypatch.setenv("ENGRAPHIS_VECTOR_BACKEND", "sqlite-vec")
    assert Settings().vector_backend == "sqlite-vec"



@pytest.mark.parametrize("raw", ("", "unsupported", " sqlite vec "))
def test_vector_backend_invalid_values_fail_closed_to_numpy(monkeypatch, raw):
    monkeypatch.setenv("ENGRAPHIS_VECTOR_BACKEND", raw)
    assert Settings().vector_backend == "numpy"


def test_blank_llm_provider_uses_documented_default(monkeypatch):
    monkeypatch.setenv("ENGRAPHIS_LLM_PROVIDER", " ")
    assert Settings().llm_provider == "openai"


def test_malformed_llm_headers_are_ignored(monkeypatch):
    monkeypatch.setenv("ENGRAPHIS_LLM_EXTRA_HEADERS", "[]")
    assert Settings().llm_extra_headers == {}
    monkeypatch.setenv("ENGRAPHIS_LLM_EXTRA_HEADERS", '{"X-Test": 1}')
    assert Settings().llm_extra_headers == {}


@pytest.mark.parametrize("raw", ("nan", "inf", "-inf", "not-a-number"))
def test_nonfinite_or_malformed_decay_halflife_uses_default(monkeypatch, raw):
    monkeypatch.setenv("ENGRAPHIS_DECAY_HALFLIFE_DAYS", raw)
    assert Settings().decay_halflife_days == 7.0


def test_malformed_boolean_values_use_safe_default(monkeypatch):
    monkeypatch.setenv("ENGRAPHIS_LLM_AUTO_EXTRACT", "perhaps")
    assert Settings().llm_auto_extract is False
    monkeypatch.setenv("ENGRAPHIS_UPDATE_CHECK", "perhaps")
    assert Settings().update_check is False

@pytest.mark.parametrize("url", RETIRED_RELAY_URLS)
def test_retired_relay_url_override_is_canonicalized(url):
    assert config.canonicalize_relay_url(url) == config.DEFAULT_RELAY_URL


def test_customer_relay_url_is_not_rewritten():
    url = "https://relay.customer.example/team/"
    assert config.canonicalize_relay_url(url) == url.rstrip("/")


def test_invalid_relay_url_error_does_not_echo_credentials(monkeypatch):
    secret_url = "ftp://relay-user:relay-token@example.test"
    monkeypatch.setenv("ENGRAPHIS_RELAY_URL", secret_url)

    with pytest.raises(ValueError) as caught:
        Settings()

    assert secret_url not in str(caught.value)
    assert "relay-token" not in str(caught.value)


def test_invalid_cors_origin_diagnostic_does_not_echo_credentials(monkeypatch, capsys):
    config._parse_origins("ftp://cors-user:cors-token@example.test")

    assert "cors-token" not in capsys.readouterr().err

def test_invalid_service_mode_exits_process(monkeypatch):
    """Invalid ENGRAPHIS_SERVICE_MODE must fail-closed (sys.exit), not silently fall back."""
    monkeypatch.setenv("ENGRAPHIS_SERVICE_MODE", "bogus")
    with pytest.raises(SystemExit):
        Settings()


def test_service_mode_defaults_to_customer_trust_domain(monkeypatch):
    monkeypatch.delenv("ENGRAPHIS_SERVICE_MODE", raising=False)
    configured = Settings()

    assert configured.service_mode == "customer"
    assert configured.customer_service is True


def test_private_service_modes_are_not_available_in_the_public_package(monkeypatch):
    for mode in ("relay", "vendor", "combined"):
        monkeypatch.setenv("ENGRAPHIS_SERVICE_MODE", mode)
        with pytest.raises(SystemExit):
            Settings()


def _isolated_config_probe(
    tmp_path: Path,
    environment: dict[str, str],
    code: str = (
        "import os; import engraphis.config; "
        "print(os.environ.get('ENGRAPHIS_CLOUD_CONTROL_URL', ''))"
    ),
):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    probe_environment = dict(environment)
    probe_environment["HOME"] = str(home)
    probe_environment["USERPROFILE"] = str(home)
    probe_environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    return subprocess.run(
        [
            sys.executable,
            "-c",
            code,
        ],
        cwd=tmp_path,
        env=probe_environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_arbitrary_working_directory_dotenv_is_not_loaded(tmp_path) -> None:
    (tmp_path / ".env").write_text(
        "ENGRAPHIS_CLOUD_CONTROL_URL=https://attacker.example.test\n",
        encoding="utf-8",
    )
    environment = dict(os.environ)
    environment.pop("ENGRAPHIS_ENV_FILE", None)
    environment.pop("ENGRAPHIS_CLOUD_CONTROL_URL", None)

    completed = _isolated_config_probe(tmp_path, environment)

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == ""


def test_explicit_owner_private_env_file_loads_without_overriding_process_env(
    tmp_path,
) -> None:
    pytest.importorskip(
        "dotenv", reason="explicit env-file config tests require python-dotenv"
    )
    trusted = tmp_path / "trusted.env"
    trusted.write_text(
        "ENGRAPHIS_CLOUD_CONTROL_URL=https://trusted.example.test\n",
        encoding="utf-8",
    )
    if os.name != "nt":
        os.chmod(trusted, 0o600)
    environment = dict(os.environ)
    environment["ENGRAPHIS_ENV_FILE"] = str(trusted)
    environment.pop("ENGRAPHIS_CLOUD_CONTROL_URL", None)

    loaded = _isolated_config_probe(tmp_path, environment)
    assert loaded.returncode == 0, loaded.stderr
    assert loaded.stdout.strip() == "https://trusted.example.test"

    environment["ENGRAPHIS_CLOUD_CONTROL_URL"] = "https://operator.example.test"
    overridden = _isolated_config_probe(tmp_path, environment)
    assert overridden.returncode == 0, overridden.stderr
    assert overridden.stdout.strip() == "https://operator.example.test"


def test_trusted_env_parser_supports_documented_values_without_interpolation() -> None:
    parsed = config._parse_trusted_env(
        "# generated and operator-managed values\n"
        "\n"
        "ENGRAPHIS_DB_PATH=C:\\Users\\O'Brien\\Memory Vault\\engraphis.db\n"
        "ENGRAPHIS_CSP=\"default-src 'self'; frame-ancestors 'none'\"\n"
        "ENGRAPHIS_HSTS='max-age=31536000; includeSubDomains' # TLS only\n"
        'ENGRAPHIS_LLM_EXTRA_HEADERS={"X-Literal":"${HOME}","X-Title":"engraphis"}\n'
        "ENGRAPHIS_DUPLICATE=first\n"
        "export ENGRAPHIS_DUPLICATE=second\n"
    )

    assert parsed == {
        "ENGRAPHIS_DB_PATH": r"C:\Users\O'Brien\Memory Vault\engraphis.db",
        "ENGRAPHIS_CSP": "default-src 'self'; frame-ancestors 'none'",
        "ENGRAPHIS_HSTS": "max-age=31536000; includeSubDomains",
        "ENGRAPHIS_LLM_EXTRA_HEADERS": (
            '{"X-Literal":"${HOME}","X-Title":"engraphis"}'
        ),
        "ENGRAPHIS_DUPLICATE": "second",
    }


@pytest.mark.parametrize(
    "raw",
    [
        "lowercase=value\n",
        "ENGRAPHIS_BROKEN\n",
        'ENGRAPHIS_CSP="unterminated\n',
        "ENGRAPHIS_CSP='unterminated\n",
        'ENGRAPHIS_CSP="valid" trailing\n',
        "ENGRAPHIS_TOKEN=do-not-print\x00suffix\n",
    ],
)
def test_trusted_env_parser_rejects_malformed_syntax_without_echoing_values(raw) -> None:
    with pytest.raises(ValueError, match=r"trusted config .* contains invalid syntax") as caught:
        config._parse_trusted_env(raw)

    assert "do-not-print" not in str(caught.value)


def test_explicit_env_file_path_must_be_absolute(tmp_path) -> None:
    environment = dict(os.environ)
    environment["ENGRAPHIS_ENV_FILE"] = "relative.env"
    environment.pop("ENGRAPHIS_CLOUD_CONTROL_URL", None)

    completed = _isolated_config_probe(tmp_path, environment)

    assert completed.returncode != 0
    assert completed.stdout.strip() == ""
    assert "must be an absolute path" in completed.stderr


def test_explicit_env_file_must_be_owner_private_on_posix(tmp_path) -> None:
    if os.name == "nt":
        pytest.skip("POSIX permission bits are not authoritative on Windows")
    public = tmp_path / "public.env"
    public.write_text(
        "ENGRAPHIS_CLOUD_CONTROL_URL=https://attacker.example.test\n",
        encoding="utf-8",
    )
    os.chmod(public, 0o644)
    environment = dict(os.environ)
    environment["ENGRAPHIS_ENV_FILE"] = str(public)
    environment.pop("ENGRAPHIS_CLOUD_CONTROL_URL", None)

    completed = _isolated_config_probe(tmp_path, environment)

    assert completed.returncode != 0
    assert completed.stdout.strip() == ""
    assert "owner-only permissions" in completed.stderr


def test_explicit_env_file_rejects_linked_leaves(tmp_path) -> None:
    victim = tmp_path / "victim.env"
    victim.write_text(
        "ENGRAPHIS_CLOUD_CONTROL_URL=https://attacker.example.test\n",
        encoding="utf-8",
    )
    if os.name != "nt":
        os.chmod(victim, 0o600)
    linked = tmp_path / "linked.env"
    try:
        linked.symlink_to(victim)
    except (NotImplementedError, OSError):
        try:
            os.link(victim, linked)
        except OSError:
            pytest.skip("this platform cannot create an adversarial config link")
    environment = dict(os.environ)
    environment["ENGRAPHIS_ENV_FILE"] = str(linked)
    environment.pop("ENGRAPHIS_CLOUD_CONTROL_URL", None)

    completed = _isolated_config_probe(tmp_path, environment)

    assert completed.returncode != 0
    assert completed.stdout.strip() == ""
    assert "unsafe private state file" in completed.stderr


def test_default_settings_persistence_uses_trusted_home_not_working_directory(
    tmp_path,
) -> None:
    working_env = tmp_path / ".env"
    working_env.write_text("KEEP=1\n", encoding="utf-8")
    environment = dict(os.environ)
    environment.pop("ENGRAPHIS_ENV_FILE", None)
    environment.pop("ENGRAPHIS_CLOUD_CONTROL_URL", None)

    completed = _isolated_config_probe(
        tmp_path,
        environment,
        (
            "from engraphis.config import persist_project_env; "
            "print(persist_project_env({'ENGRAPHIS_LOOP_INTERVAL': '7'}))"
        ),
    )

    trusted = tmp_path / "home" / ".engraphis" / "config.env"
    assert completed.returncode == 0, completed.stderr
    assert Path(completed.stdout.strip()) == trusted
    assert working_env.read_text(encoding="utf-8") == "KEEP=1\n"
    assert trusted.read_text(encoding="utf-8") == "ENGRAPHIS_LOOP_INTERVAL=7\n"
    if os.name != "nt":
        assert trusted.stat().st_mode & 0o077 == 0
