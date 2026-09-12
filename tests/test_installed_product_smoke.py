"""Behavioral regressions for the offline installed-product release journey."""
import os
import socket

import pytest

from scripts.smoke_installed_product import isolated_environment, run_journey


_REAL_GETADDRINFO = socket.getaddrinfo


def test_smoke_environment_excludes_operator_secrets_paths_and_models(monkeypatch, tmp_path):
    for key in ("OPENAI_API_KEY", "ENGRAPHIS_CLOUD_ACCESS_TOKEN", "ENGRAPHIS_DB_KEY",
                "ENGRAPHIS_DB_PATH", "ENGRAPHIS_EMBED_MODEL", "PYTHONPATH", "HTTP_PROXY"):
        monkeypatch.setenv(key, "operator-private-value")
    env = isolated_environment(tmp_path)
    assert "operator-private-value" not in env.values()
    assert env["ENGRAPHIS_DB_PATH"] == str(tmp_path / "memory.db")
    assert env["ENGRAPHIS_EMBED_MODEL"] == ""
    assert env["HF_HUB_OFFLINE"] == "1"
    assert env["PYTHONNOUSERSITE"] == "1"
    assert "PYTHONPATH" not in env
    assert env["ENGRAPHIS_ENV_FILE"] == str(tmp_path / "config.env")


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_smoke_rejects_unbounded_timeout(timeout):
    with pytest.raises(ValueError, match="positive and finite"):
        run_journey(timeout=timeout)


@pytest.mark.parametrize("surface,dependencies", [
    ("mcp", ("mcp.server.fastmcp",)),
    ("server", ("fastapi", "uvicorn", "multipart")),
])
def test_real_process_journey_preserves_memory_history_after_restart(surface, dependencies, monkeypatch):
    for dependency in dependencies:
        pytest.importorskip(dependency)
    # conftest's offline DNS fixture points all lookups at example.com. This test
    # talks only to literal loopback addresses belonging to its own subprocesses.
    monkeypatch.setattr(socket, "getaddrinfo", _REAL_GETADDRINFO)
    # This is explicitly source-under-test evidence. The release workflow separately
    # invokes the CLI from clean installed wheels on all three operating systems.
    report = run_journey(surface, installed=False)
    assert report["installed_artifact"] is False
    assert report["platform"] == os.sys.platform
    assert report["checks"][surface]
