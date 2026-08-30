"""Tests for review-feedback fixes on PR 174.

Covers:
1. Agent repo precedence: explicit > config.default_repo > self.name.
2. register() wrappers lazily start the session.
3. install_prime_agent / scripts wrapper dispatches via the package module.
4. Installer TOML path uses write_text (not write_bytes).
5. CLI install command does not require scripts/ outside the wheel.
"""
from __future__ import annotations

import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from engraphis_prime_agent.config import EngraphisRuntimeConfig
from engraphis_prime_agent.mcp_client import EngraphisMcpClient


# ---- Fix 1: agent repo precedence --------------------------------------


def test_agent_repo_uses_explicit_kwarg() -> None:
    from engraphis_prime_agent.agent import EngraphisPrimeAgent

    config = EngraphisRuntimeConfig(
        command="ignored", default_repo="api", environment={}
    )
    client = EngraphisMcpClient(config)
    agent = EngraphisPrimeAgent(
        "researcher", client, config, workspace="acme", repo="custom"
    )
    assert agent.repo == "custom"


def test_agent_repo_uses_default_repo_when_no_explicit() -> None:
    from engraphis_prime_agent.agent import EngraphisPrimeAgent

    config = EngraphisRuntimeConfig(
        command="ignored", default_repo="api", environment={}
    )
    client = EngraphisMcpClient(config)
    agent = EngraphisPrimeAgent("researcher", client, config, workspace="acme")
    assert agent.repo == "api"


def test_agent_repo_falls_back_to_name_when_no_default() -> None:
    from engraphis_prime_agent.agent import EngraphisPrimeAgent

    config = EngraphisRuntimeConfig(command="ignored", environment={})
    client = EngraphisMcpClient(config)
    agent = EngraphisPrimeAgent("researcher", client, config, workspace="acme")
    assert agent.repo == "researcher"


# ---- Fix 2: register() wrappers lazily start the session ------------------


@pytest.mark.asyncio
async def test_register_wrappers_lazy_start_session(fake_mcp_server) -> None:
    """When a fresh agent is registered and the framework invokes a tool
    directly, the session must be started before the tool is called — the
    wrapper around each registered callable must drive the lazy-start path.
    """
    from engraphis_prime_agent.agent import EngraphisPrimeAgent, PrimeAgentFleet

    config = EngraphisRuntimeConfig(command="ignored", environment={})
    client = EngraphisMcpClient(config)
    await client.connect()
    try:
        fleet = PrimeAgentFleet(workspace="test", config=config)
        agent = EngraphisPrimeAgent("researcher", client, config)

        registered: dict[str, object] = {}

        class _Target:
            def register_tool(self, name: str, fn, schema: dict) -> None:
                registered[name] = fn

        agent.register(_Target())
        assert "engraphis_recall_context" in registered
        wrapper = registered["engraphis_recall_context"]
        # Before the framework calls the wrapper, no session exists.
        assert agent.session_id is None
        await wrapper({"query": "hello"})
        # After the framework calls the wrapper, the session is started.
        assert agent.session_id is not None
        await fleet.aclose()
    finally:
        await client.close()


# ---- Fix 3 + 4: installer module + TOML write_text ----------------------


def test_installer_module_importable() -> None:
    """The installer must ship inside the package so the wheel works."""
    from engraphis_prime_agent import installer

    assert hasattr(installer, "install")
    assert hasattr(installer, "uninstall")
    assert hasattr(installer, "main")


def test_installer_toml_uses_write_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``tomli_w.dumps`` returns str, so the TOML path must use write_text
    (not write_bytes, which would TypeError). We test the wrapper by
    stubbing tomli_w to verify the right method is called.
    """
    from engraphis_prime_agent import installer

    target = tmp_path / "config.toml"
    captured: dict[str, object] = {}

    class _StubToml:
        @staticmethod
        def dumps(_data: dict) -> str:
            return "[tools.engraphis]\npackage = 'x'\n"

    monkeypatch.setattr(installer, "tomli_w", _StubToml, raising=False)
    monkeypatch.setitem(sys.modules, "tomli_w", _StubToml)

    real_write_text = Path.write_text
    real_write_bytes = Path.write_bytes

    def _spy_write_text(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        captured["method"] = "write_text"
        return real_write_text(self, *args, **kwargs)

    def _spy_write_bytes(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        captured["method"] = "write_bytes"
        return real_write_bytes(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", _spy_write_text)
    monkeypatch.setattr(Path, "write_bytes", _spy_write_bytes)

    installer.install(target, merge=False, dry_run=False)
    assert captured.get("method") == "write_text"
    assert target.exists()
    assert "package" in target.read_text(encoding="utf-8")


def test_installer_idempotent_install_uninstall_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from engraphis_prime_agent import installer

    target = tmp_path / "config.json"
    installer.install(target)
    installer.install(target)  # idempotent: same content
    cfg = json.loads(target.read_text(encoding="utf-8"))
    assert len(cfg["tools"]) == 1
    assert "engraphis" in cfg["tools"]
    installer.uninstall(target)
    cfg = json.loads(target.read_text(encoding="utf-8"))
    assert "engraphis" not in cfg.get("tools", {})


def test_installer_backup_preserves_source_permissions(tmp_path: Path) -> None:
    from engraphis_prime_agent import installer

    target = tmp_path / "config.json"
    target.write_text('{"tools": {"other": {}}}\n', encoding="utf-8")
    target.chmod(0o640)
    source_mode = stat.S_IMODE(target.stat().st_mode)

    backup = installer._backup(target)

    assert backup is not None
    assert stat.S_IMODE(backup.stat().st_mode) == source_mode


def test_installer_dry_run_does_not_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from engraphis_prime_agent import installer

    target = tmp_path / "config.json"
    installer.install(target, dry_run=True)
    assert not target.exists()


# ---- Fix 5: CLI install works without a source-tree scripts/ dir ----------


def test_cli_install_subcommand_uses_package_installer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI must dispatch through the package module, not runpy against
    a repo-level scripts/ directory that doesn't exist after pip install.
    """
    config_path = tmp_path / "config.json"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "engraphis_prime_agent",
            "install",
            "--config-path",
            str(config_path),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert config_path.exists()
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    assert "engraphis" in cfg["tools"]


# ---- Scripts wrapper: still works from a source checkout -----------------


def test_scripts_wrapper_imports_package(tmp_path: Path) -> None:
    """The repo-root scripts/install_prime_agent.py is a thin shim that
    delegates to engraphis_prime_agent.installer. Verify the import path
    when invoked from a source checkout (no editable install).
    """
    import io
    import contextlib

    script = (
        Path(__file__).resolve().parent.parent.parent.parent
        / "scripts"
        / "install_prime_agent.py"
    )
    assert script.exists(), f"missing {script}"
    config_path = tmp_path / "shim-config.json"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = subprocess.run(
            [sys.executable, str(script), "--config-path", str(config_path)],
            capture_output=True,
            text=True,
        )
    assert result.returncode == 0, result.stderr
    assert config_path.exists()
